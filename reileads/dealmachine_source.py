"""Source leads directly from DealMachine's own property database.

Separate from, and does not touch, the county-scrape pipeline
(pipeline_oh.py / pipeline_ga.py / backfill.py) -- Sharon asked for those
left exactly as they are. This is an additional, parallel source.

The reasoning for building it: DealMachine already licenses the exact
distress signals this codebase scrapes county-by-county (tax delinquency,
pre-foreclosure with an actual auction date, vacancy) PLUS a phone number,
in one call, for cities and counties we have no scraper for at all --
Columbus (Franklin County, OH) and Savannah (Chatham County, GA)
specifically, neither of which appears anywhere else in this repo. No new
scraper needed for either; DealMachine already covers them.

Cost: 1 credit per unique PERSON returned, regardless of how many
property filters (tax/foreclosure/equity/vacancy) were used to find them.
Confirmed from DealMachine's own docs: filtering on property criteria is
free, and property-context fields (full address, lat/long) do not add a
property credit -- only requesting property FACTS (value, equity, tax,
mortgage, sale, listing) does. So PERSON_FIELDS below deliberately
excludes every one of those.

Filters are ANDed only -- DealMachine's API has no OR across filter
fields ("There is no OR grouping or nesting for filters," their own
docs). So "hot" means running one search per primary distress criterion
and merging the results, rather than one query for
"delinquent OR preforeclosure OR vacant".
"""
from __future__ import annotations

import logging
import time

from .core.dealmachine import DealMachine, best_phone, best_email

log = logging.getLogger(__name__)

# County FIPS codes. "Metro" here means whatever DealMachine's location
# filter actually accepts (state/zip_code/county/city) -- there is no
# metro-area primitive, so a metro is approximated as its core county
# (Cleveland, Cincinnati, Columbus, Savannah) or its handful of core
# counties (Atlanta).
METROS = {
    "cleveland":  [{"type": "county", "code": "39035"}],   # Cuyahoga, OH
    "cincinnati": [{"type": "county", "code": "39061"}],   # Hamilton, OH
    "columbus":   [{"type": "county", "code": "39049"}],   # Franklin, OH
    "savannah":   [{"type": "county", "code": "13051"}],   # Chatham, GA
    "atlanta":    [{"type": "county", "code": c} for c in
                   ("13121", "13089", "13135", "13067", "13063")],
                   # Fulton, DeKalb, Gwinnett, Cobb, Clayton, GA
}

# Each ANDed with is_absentee_owner and a minimum equity floor -- an
# absentee owner with real equity and ANY of these is a plausible seller,
# not just a name appearing on a distress list with nothing else true
# about them.
HOT_CRITERIA = [
    ("preforeclosure", {"filter_id": "is_preforeclosure", "value": True}),
    ("tax_delinquent",  {"filter_id": "is_tax_delinquent", "value": True}),
    ("vacant",          {"filter_id": "is_vacant_home", "value": True}),
]

# Confirmed 2026-09-14 via GET /v1/filters (free, no credits) rather than
# trusting the docs' illustrative example -- the real filter is
# has_absentee_owners, not is_absentee_owner, and estimated_equity_percentage,
# not equity_percent. The docs example 400'd on both.
ABSENTEE_FILTER = {"filter_id": "has_absentee_owners", "value": True}
EQUITY_FILTER_ID = "estimated_equity_percentage"
MIN_EQUITY_PERCENT = 30

# Contact fields plus free address context. Deliberately excludes value,
# equity, tax, mortgage, sale and listing fields -- those are what would
# add a property credit on top of the person credit, per DealMachine's
# "Return owners without property credits" guidance. We already filtered
# on the facts we care about; we don't need them echoed back.
PERSON_FIELDS = ["full_name", "phones", "emails", "full_address",
                 "address", "city", "state", "zip"]


def _extract_address(person: dict) -> dict:
    """Response shape for a people-anchored property search isn't fully
    documented, so this checks the plausible shapes rather than assuming
    one -- top-level fields, or nested under 'property'/'residence'."""
    for src in (person, person.get("property") or {}, person.get("residence") or {}):
        if src.get("address") or src.get("full_address"):
            return {
                "address": src.get("address") or src.get("full_address") or "",
                "city": src.get("city") or "",
                "state": src.get("state") or "",
                "zip": str(src.get("zip") or src.get("postal_code") or ""),
            }
    return {"address": "", "city": "", "state": "", "zip": ""}


def search_hot(metro: str, limit: int = 50) -> tuple[list[dict], int]:
    """One metro, every hot criterion, deduped by person, most stacked
    signals first. Returns (people, total credits spent)."""
    if metro not in METROS:
        raise ValueError(f"unknown metro {metro!r}, have: {sorted(METROS)}")

    dm = DealMachine()
    locations = METROS[metro]
    by_person: dict = {}
    total_credits = 0

    for label, criterion in HOT_CRITERIA:
        body = {
            "locations": locations,
            "filters": [
                criterion,
                ABSENTEE_FILTER,
                {"filter_id": EQUITY_FILTER_ID, "operator": "greater_than_or_equal",
                 "value": MIN_EQUITY_PERCENT},
            ],
            "anchor": "people",
            "contact_audience": "owners",
            "fields": PERSON_FIELDS,
            "per_page": min(limit, 100),
            "page": 1,
        }
        try:
            data = dm._post("/properties/search", body)
        except Exception as e:
            log.error("dealmachine search failed metro=%s criterion=%s: %s",
                      metro, label, e)
            continue

        total_credits += int((data.get("credits") or {}).get("used") or 0)
        for person in data.get("data") or []:
            pid = person.get("dm_person_id") or person.get("full_name")
            if not pid:
                continue
            rec = by_person.setdefault(pid, dict(person, hot_signals=set()))
            rec["hot_signals"].add(label)
        time.sleep(0.3)

    out = sorted(by_person.values(), key=lambda r: -len(r["hot_signals"]))
    log.info("dealmachine %s: %s unique people across %s criteria, %s credits",
             metro, len(out), len(HOT_CRITERIA), total_credits)
    return out[:limit], total_credits


def to_event_payload(person: dict, metro: str, state: str, county_label: str) -> dict:
    """Shape a DealMachine person result into this codebase's lead
    payload -- same keys core/reireply.py and core/classify.py read,
    so it flows through push() exactly like a county-sourced lead. Phone
    is already present, so this bypasses skiptrace.py's gate entirely.
    """
    addr = _extract_address(person)
    phone, ptype, dnc = best_phone(person)
    email = best_email(person)
    full = (person.get("full_name") or "").strip()
    parts = full.split()
    first, last = (parts[0], " ".join(parts[1:])) if len(parts) > 1 else ("", full)

    return {
        "county": county_label,
        "state": state,
        "metro": metro,
        "owner_full": full,
        "owner_first": first,
        "owner_last": last,
        "is_entity": False,          # anchor=people only returns individuals
        "site_address": addr["address"],
        "site_city": addr["city"],
        "site_zip": addr["zip"],
        "mail_address": addr["address"],
        "mail_city": addr["city"],
        "mail_state": addr["state"] or state,
        "mail_zip": addr["zip"],
        "phone": phone,
        "phone_type": ptype,
        "do_not_call": dnc,
        "email": email,
        "absentee": True,            # the filter that selected them
        "signals": sorted(person.get("hot_signals") or []),
        "source": "dealmachine",
    }
