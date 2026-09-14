"""Controlled release of the distressed-parcel backlog.

The daily pipelines only emit a lead when a parcel ENTERS distress, which
is correct for fresh signal but leaves everything that was already
distressed on day one permanently uncontacted -- 99,077 parcels as of
2026-09-14, against 19 events ever emitted. This module drips that
backlog out at a fixed rate per run, highest urgency score first.

Two filters decide what qualifies, both of them Sharon's asks:

  1. Same owner since the delinquency began. If the county's own records
     show a sale AFTER the tax delinquency started, the debt was almost
     certainly cleared at closing and the current owner never had the
     problem -- a dead lead wearing a distressed parcel's clothes.
     See owner_unchanged_since_delinquency().

  2. The classifier. pipeline_oh.py never calls score()/exclude() at all,
     so no Ohio lead has ever been scored or filtered. Backlog leads are,
     which is also what makes "highest urgency first" mean anything.

Backlog events use their own event name (backlog_tax_delinquent), so they
tag into REI Reply as signal-backlog-tax-delinquent and can be worked
with a different script than a fresh foreclosure -- these are older
situations, not someone who just got served.
"""
from __future__ import annotations

import json
import logging
import datetime as dt

from .core.store import Store
from .core.classify import score, tier
from .core.normalize import year_of

log = logging.getLogger(__name__)

EVENT = "backlog_tax_delinquent"

# Where each county records when the delinquency started. Checked in
# order; they don't overlap, different counties expose different ones.
_DELQ_START_KEYS = ("certified_delinquent_date", "delinquent_since_year",
                    "delinquent_since", "prev_tax_year")


def _delinquency_start_year(p: dict):
    for key in _DELQ_START_KEYS:
        y = year_of(p.get(key))
        if y:
            return y
    return None


def owner_unchanged_since_delinquency(p: dict) -> bool:
    """True when the current owner is the one who ran up the delinquency.

    Unknown dates return True on purpose: a missing sale date is not
    evidence that a sale happened, and excluding on absent data would
    silently drop whole counties whose layers don't publish it.

    A sale in the SAME year the delinquency was certified counts as a
    change of hands. Ohio certifies roughly two years into non-payment,
    so a sale inside that window would have cleared the taxes at closing.
    """
    sale_year = year_of(p.get("last_sale_date"))
    delq_year = _delinquency_start_year(p)
    if sale_year is None or delq_year is None:
        return True
    return sale_year < delq_year


def _scoreable(p: dict) -> dict:
    """Map Ohio county field names onto the keys classify.py reads.

    The classifier was written against Georgia's vocabulary
    (tax_delinquent_amount, homestead_exemption). Ohio sources publish
    the same facts under their own names, which is the other reason
    scoring an Ohio payload straight out of the store returns nothing
    useful.
    """
    d = dict(p)

    bal = p.get("delq_balance")
    if bal:
        d["tax_delinquent_amount"] = bal

    delq_year = _delinquency_start_year(p)
    if delq_year:
        years = dt.date.today().year - delq_year
        if years > 0:
            d["tax_delinquent_years"] = years

    if p.get("homestead") is not None and "homestead_exemption" not in d:
        d["homestead_exemption"] = bool(p.get("homestead"))

    return d


def collect(store: Store, limit: int = 150, counties=None) -> tuple[list, dict]:
    """Return (top N qualifying leads, counts by reason rejected)."""
    sql = """SELECT p.county, p.parcel, p.payload
             FROM parcels p
             LEFT JOIN events e
               ON e.county = p.county AND e.parcel = p.parcel
             WHERE e.parcel IS NULL"""
    params = []
    if counties:
        sql += " AND p.county IN (%s)" % ",".join("?" * len(counties))
        params = list(counties)

    stats = {"scanned": 0, "no_owner_name": 0, "owner_changed": 0,
             "excluded": 0, "qualified": 0}
    scored = []

    for r in store.db.execute(sql, params):
        stats["scanned"] += 1
        p = json.loads(r["payload"])

        # No name means REI Reply rejects the contact outright (confirmed
        # 2026-09-14: HTTP 422, "Contacts without email, phone, firstName
        # and lastName are not allowed"). Summit is the whole county.
        if not (p.get("owner_full") or p.get("owner_last") or p.get("owner_first")):
            stats["no_owner_name"] += 1
            continue

        if not owner_unchanged_since_delinquency(p):
            stats["owner_changed"] += 1
            continue

        v = score(_scoreable(p))
        if v.excluded:
            stats["excluded"] += 1
            continue

        stats["qualified"] += 1
        p["urgency_score"] = v.score
        p["tier"] = tier(v.score)
        p["persona"] = v.persona
        p["signals"] = v.signals
        scored.append((v.score, r["county"], r["parcel"], p))

    # One lead per OWNER, not per parcel. Landlords and small investors
    # hold several delinquent parcels each -- six rows for one LLC is six
    # contacts REI Reply can't merge (no phone/email to dedupe on) and six
    # calls to the same person. The highest-scoring parcel represents the
    # owner; the rest stay in the backlog for a later run, and the count
    # rides along because "you're behind on six properties" is a stronger
    # opening than one address.
    best, counts, balances = {}, {}, {}
    for s, county, parcel, p in scored:
        key = (p.get("owner_full") or "").strip().upper() or f"{county}:{parcel}"
        counts[key] = counts.get(key, 0) + 1
        balances[key] = balances.get(key, 0) + float(p.get("delq_balance") or 0)
        if key not in best or s > best[key][0]:
            best[key] = (s, county, parcel, p)

    deduped = []
    for key, (s, county, parcel, p) in best.items():
        p["portfolio_count"] = counts[key]
        p["portfolio_delq_balance"] = round(balances[key], 2)
        deduped.append((s, county, parcel, p))

    stats["distinct_owners"] = len(deduped)
    deduped.sort(key=lambda t: (t[0], t[3].get("portfolio_count", 1)), reverse=True)
    return deduped[:limit], stats


def run(store: Store, limit: int = 150, counties=None, preview: bool = False) -> int:
    leads, stats = collect(store, limit=limit, counties=counties)

    log.info("backlog: %s scanned, %s no owner name, %s sold since delinquency, "
             "%s excluded by classifier, %s qualified parcels across %s owners "
             "(%s released this run)",
             f"{stats['scanned']:,}", f"{stats['no_owner_name']:,}",
             f"{stats['owner_changed']:,}", f"{stats['excluded']:,}",
             f"{stats['qualified']:,}", f"{stats.get('distinct_owners', 0):,}",
             len(leads))

    if preview:
        for s, county, parcel, p in leads[:10]:
            log.info("  %-10s %-11s score=%-3s %-2s x%-2s $%-9s %-28s | %s",
                     county, parcel, s, p.get("tier"), p.get("portfolio_count"),
                     f"{p.get('portfolio_delq_balance') or 0:,.0f}",
                     (p.get("owner_full") or "")[:28],
                     (p.get("site_address") or "")[:38])
        return 0

    d = dt.date.today().isoformat()
    for s, county, parcel, p in leads:
        store.db.execute(
            "INSERT OR IGNORE INTO events (county,parcel,event,detected_on,payload) "
            "VALUES (?,?,?,?,?)",
            (county, parcel, EVENT, d, json.dumps(p, default=str)),
        )
    store.db.commit()
    store.log_run("backlog", stats["qualified"], len(leads), "ok")
    return len(leads)
