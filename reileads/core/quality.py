"""One quality gate, applied to every lead whatever produced it.

This started life inside backfill.py, which meant only backlog leads were
checked -- fresh Ohio foreclosures went to the CRM unscored and
unvalidated, and Georgia probate was scored but never address-checked.
Same data problems, three code paths, one of them covered.

Every check below is skip-on-missing by design: a source that doesn't
publish land-use codes shouldn't have its leads dropped for it. That
makes the gate safe to apply uniformly -- a check only fires where the
data exists to fire on.

vet() is the only thing callers need. It returns
(ok, reason, payload-with-score-attached).
"""
from __future__ import annotations

import datetime as dt

from .classify import score, tier
from .normalize import year_of, has_house_number

# Ohio land-use codes for land with nothing built on it. Numeric prefixes
# because counties append sub-codes. Any other 5xx is residential WITH a
# structure (510 single family, 520 two family, 550 multi).
_VACANT_LAND_CODES = ("500", "501", "100", "101", "300", "400")
_VACANT_LAND_WORDS = ("VACANT", "UNIMPROVED", "RAW LAND")

# Where each county records when the delinquency started.
_DELQ_START_KEYS = ("certified_delinquent_date", "delinquent_since_year",
                    "delinquent_since", "prev_tax_year")


def is_vacant_land(p: dict) -> bool:
    """True when the county itself says nothing is built on the parcel.

    Mahoning's source is the county land-bank layer, majority code 500 --
    92 of the first 150 released leads on 2026-09-14 were empty lots.
    """
    code = str(p.get("land_use") or "").strip()
    if code and code[:3] in _VACANT_LAND_CODES:
        return True
    desc = f"{p.get('land_use') or ''} {p.get('property_class') or ''}".upper()
    return any(w in desc for w in _VACANT_LAND_WORDS)


def delinquency_start_year(p: dict):
    for key in _DELQ_START_KEYS:
        y = year_of(p.get(key))
        if y:
            return y
    return None


def owner_unchanged_since_delinquency(p: dict) -> bool:
    """True when the current owner is the one who ran up the delinquency.

    Unknown dates return True on purpose: a missing sale date is not
    evidence a sale happened, and excluding on absent data would silently
    drop whole counties whose layers don't publish it.

    A sale in the SAME year the delinquency was certified counts as a
    change of hands -- Ohio certifies roughly two years into non-payment,
    so a sale inside that window cleared the taxes at closing.
    """
    sale_year = year_of(p.get("last_sale_date"))
    delq_year = delinquency_start_year(p)
    if sale_year is None or delq_year is None:
        return True
    return sale_year < delq_year


def normalize_signals(p: dict) -> dict:
    """Map county field names onto the keys classify.py actually reads.

    The classifier was written against Georgia's vocabulary
    (tax_delinquent_amount, homestead_exemption); Ohio sources publish the
    same facts under their own names. Without this, score() reads an Ohio
    payload as having no distress event at all and excludes every one.
    """
    d = dict(p)

    bal = p.get("delq_balance")
    if bal and not d.get("tax_delinquent_amount"):
        d["tax_delinquent_amount"] = bal

    delq_year = delinquency_start_year(p)
    if delq_year and not d.get("tax_delinquent_years"):
        years = dt.date.today().year - delq_year
        if years > 0:
            d["tax_delinquent_years"] = years

    # A county foreclosure flag is a real primary signal, but Ohio layers
    # carry no scheduled sale date, so it can't ride on
    # foreclosure_sale_date without inventing a date we don't have.
    if p.get("foreclosure") and not d.get("lis_pendens"):
        d["lis_pendens"] = True

    if p.get("homestead") is not None and d.get("homestead_exemption") is None:
        d["homestead_exemption"] = bool(p.get("homestead"))

    return d


def vet(p: dict, require_structure: bool = True,
        require_same_owner: bool = True) -> tuple[bool, str, dict]:
    """Gate one lead. Returns (ok, reason, payload).

    On success the payload comes back carrying urgency_score, tier,
    persona and signals. On failure `reason` is a short stable key
    suitable for counting.
    """
    if not (p.get("owner_full") or p.get("owner_last") or p.get("owner_first")):
        # REI Reply rejects these outright: HTTP 422, "Contacts without
        # email, phone, firstName and lastName are not allowed."
        return False, "no_owner_name", p

    if require_structure:
        if is_vacant_land(p):
            return False, "vacant_land", p
        # No street number means no structure. Separate from the land-use
        # check because Cuyahoga publishes no land-use code at all.
        if not has_house_number(p.get("site_address")):
            return False, "no_street_number", p

    if require_same_owner and not owner_unchanged_since_delinquency(p):
        return False, "owner_changed", p

    v = score(normalize_signals(p))
    if v.excluded:
        return False, "classifier", p

    out = dict(p)
    out["urgency_score"] = v.score
    out["tier"] = tier(v.score)
    out["persona"] = v.persona
    out["signals"] = v.signals
    return True, "", out
