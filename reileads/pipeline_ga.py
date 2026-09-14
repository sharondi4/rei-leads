"""Georgia pipeline: legal ads -> parcel enrichment -> classify -> store.

Distinct from the Ohio pipeline (pipeline.py / cli.py in the parent
package this was copied from) because Georgia's shape is different: there
is no county open-data foreclosure flag to diff, so every run re-derives
leads fresh from the newspaper feed rather than diffing yesterday's
snapshot. The store is still used, so a lead already pushed is never
pushed twice.

DISABLED 2026-09-10: sources/ga/legal_ads.py (Fulton/Cobb/Cherokee/Douglas
newspaper scraping) is turned off here, not deleted. Each paper's Terms of
Use -- confirmed on fultonneighbor.com's, which explicitly cross-references
mdjonline.com in its own text, meaning this is the shared TownNews-network
template these four papers all carry -- bars exactly this: "use any robot,
spider, script, service, software, or any manual or automatic device,
tool, or process designed to data-mine, scrape, crawl, or otherwise
collect or extract the Content ... without our prior express written
consent." That clause was added/updated 2026-07-01, so it may postdate
when this scraper was first written.

This means `collect()` currently returns nothing for Georgia -- there is
no lawful GA notice source wired up right now. Do not re-enable
LegalAdsForeclosure without written permission from each paper (same bar
already applied to GSCCCA and georgiapublicnotice.com in this codebase).
The two paths actually being pursued instead: the GSCCCA permission
letter (GSCCCA_permission_request.md) and a GeorgiaPublicNotice.com
Smart Search subscription (a paid product built for automated daily
delivery, a different agreement than scraping the free search UI).
"""
import logging
import datetime as dt

from .core import config
from .core.store import Store
from .core.classify import score, tier
from .sources.ga.fulton_parcel import FultonParcels

log = logging.getLogger(__name__)

# Only Fulton has a wired-up open parcel enrichment source right now.
# Cobb/Cherokee/Douglas ads still flow through -- they just carry no
# owner/mailing address yet, which the classifier's derive() treats as
# unknown (not "no data means exclude").
ENRICHERS = {"Fulton": FultonParcels}


def _no_lawful_ga_source(counties=None, days_back=3):
    """Stand-in for LegalAdsForeclosure.fetch() -- see module docstring
    for why the newspaper scraper is disabled rather than deleted."""
    log.warning(
        "GA notice source disabled: newspaper ToS bars automated access "
        "(see pipeline_ga.py docstring). Awaiting GSCCCA permission or a "
        "Smart Search subscription before this produces any leads."
    )
    return iter(())


def collect(counties=None, days_back=3) -> list[dict]:
    """One pass: pull ads, enrich, score. Returns scored, non-excluded
    leads only -- excluded leads are logged, not silently dropped."""
    enrichers = {}
    leads = []
    excluded_count = 0

    from .core import quality

    for raw in _no_lawful_ga_source(counties=counties, days_back=days_back):
        p = dict(raw["payload"])

        enricher_cls = ENRICHERS.get(p["county"])
        if enricher_cls:
            enricher = enrichers.setdefault(p["county"], enricher_cls())
            hit = enricher.lookup(p["site_address"])
            if hit:
                p.update(hit)

        # Same gate as every other path. No same-owner check: a Georgia
        # foreclosure notice is the event itself, not a historical debt
        # that a later sale could have cleared.
        ok, reason, p = quality.vet(p, require_same_owner=False)
        if not ok:
            excluded_count += 1
            log.debug("rejected %s (%s): %s", p.get("site_address"), p.get("county"), reason)
            continue

        leads.append({"parcel": raw["parcel"], "payload": p})

    log.info("Georgia pass: %s leads kept, %s excluded", len(leads), excluded_count)
    return leads


def run(store: Store, counties=None, days_back=3) -> int:
    """Insert new leads into the shared events table, skipping ones
    already seen (same fingerprint = same address + county + sale date).

    Inserting into the same `events` table the Ohio pipeline uses is what
    lets core.push.push() serve both states with one code path -- it does
    not know or care which pipeline created a given row.
    """
    import json
    d = dt.date.today().isoformat()
    new = 0
    for lead in collect(counties=counties, days_back=days_back):
        cur = store.db.execute(
            "SELECT 1 FROM events WHERE county=? AND parcel=? AND event='ga_notice'",
            ("ga", lead["parcel"]),
        ).fetchone()
        if cur:
            continue
        store.db.execute(
            "INSERT INTO events (county,parcel,event,detected_on,payload) VALUES (?,?,?,?,?)",
            ("ga", lead["parcel"], "ga_notice", d, json.dumps(lead["payload"], default=str)),
        )
        new += 1
    store.db.commit()
    store.log_run("ga", new, new, "ok")
    return new


def collect_probate(county_key: str = "douglas", days_back: int = 7) -> list[dict]:
    """Same shape as collect(), for sources/ga/douglas_probate.py.

    Kept separate from collect()/run() rather than merged in: probate
    cases carry no address, no foreclosure_sale_date, and no parcel
    enrichment source, so forcing them through the same ENRICHERS lookup
    as legal-ad leads would just be dead code for every row. Added
    2026-09-11, confirmed live -- see douglas_probate.py's docstring for
    what this can and can't do, and why only Douglas is wired up.
    """
    from collections import Counter
    from .core import quality
    from .sources.ga.douglas_probate import DouglasProbate

    leads, rejected = [], Counter()

    for raw in DouglasProbate.fetch(county_key=county_key, days_back=days_back):
        # Same gate Ohio and the backlog use. require_same_owner is off:
        # there is no tax delinquency here to have predated a sale, and
        # the decedent's own last transfer is not a change of hands away
        # from the estate.
        ok, reason, p = quality.vet(dict(raw["payload"]), require_same_owner=False)
        if not ok:
            rejected[reason] += 1
            log.debug("rejected probate case %s: %s", raw["payload"].get("case_number"), reason)
            continue
        leads.append({"parcel": raw["parcel"], "payload": p})

    log.info("Georgia probate pass (%s): %s leads kept%s", county_key, len(leads),
             (" (rejected: " + ", ".join(f"{v} {k}" for k, v in rejected.most_common()) + ")")
             if rejected else "")
    return leads


def run_probate(store: Store, county_key: str = "douglas", days_back: int = 7) -> int:
    """Same dedup/insert shape as run(), event type 'ga_probate' so it
    never collides with a legal-ad notice sharing the same case/parcel id
    space (they don't overlap today, but the event name keeps it that
    way if they ever do)."""
    import json
    d = dt.date.today().isoformat()
    new = 0
    try:
        leads = collect_probate(county_key=county_key, days_back=days_back)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        log.error("GA probate (%s) fetch failed: %s", county_key, err)
        store.log_run(f"ga_probate_{county_key}", 0, 0, "error", err)
        return 0

    for lead in leads:
        cur = store.db.execute(
            "SELECT 1 FROM events WHERE county=? AND parcel=? AND event='ga_probate'",
            ("ga", lead["parcel"]),
        ).fetchone()
        if cur:
            continue
        store.db.execute(
            "INSERT INTO events (county,parcel,event,detected_on,payload) VALUES (?,?,?,?,?)",
            ("ga", lead["parcel"], "ga_probate", d, json.dumps(lead["payload"], default=str)),
        )
        new += 1
    store.db.commit()
    store.log_run(f"ga_probate_{county_key}", new, new, "ok")
    return new
