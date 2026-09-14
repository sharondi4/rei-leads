"""Motivated-seller scoring and the owner-occupant exclusion.

Two jobs, deliberately separated:

  exclude()  -- hard filter. Removes the leads Sharon explicitly does not
                want: the family who just bought a house to live in, and
                owners who cannot sell (government, land bank).

  score()    -- soft ranking. Everything that survives the filter gets a
                0-100 urgency score and a persona, so the dialer works the
                top of the list first.

Nothing here touches the network, so it is fully unit-testable and is the
one part of the pipeline you can trust before a single county responds.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

TODAY = dt.date.today

# ---------------------------------------------------------------- helpers

def _months_since(d) -> float | None:
    if not d:
        return None
    if isinstance(d, str):
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
            try:
                d = dt.datetime.strptime(d[:10], fmt).date()
                break
            except ValueError:
                continue
        else:
            return None
    if isinstance(d, dt.datetime):
        d = d.date()
    return (TODAY() - d).days / 30.44


def _years_since(d) -> float | None:
    m = _months_since(d)
    return None if m is None else m / 12.0


# ---------------------------------------------------------------- exclude

GOV_MARKERS = (
    "CITY OF", "COUNTY OF", "STATE OF", "LAND BANK", "HOUSING AUTHORITY",
    "BOARD OF EDUCATION", "DEPARTMENT OF", "UNITED STATES", "DEVELOPMENT AUTHORITY",
    "URBAN REDEVELOPMENT", "DEPT OF TRANSPORTATION",
)

# How recent counts as "just bought". 18 months is deliberate: shorter and
# you keep people still unpacking; much longer and you start discarding
# genuine early-distress cases (job loss, divorce) that surface in year two.
RECENT_PURCHASE_MONTHS = 18


@dataclass
class Verdict:
    excluded: bool = False
    reason: str = ""
    score: int = 0
    persona: str = ""
    signals: list = field(default_factory=list)


def exclude(lead: dict) -> tuple[bool, str]:
    """Return (excluded, reason).

    The headline rule: a recent purchase, financed with a purchase-money
    security deed, on a property claiming homestead, is a family that just
    moved in. All three together, not any one alone -- a recent CASH
    purchase with no homestead is an investor, and we want those.
    """
    owner = (lead.get("owner_full") or "").upper()

    if any(m in owner for m in GOV_MARKERS):
        return True, "government or land bank owner, cannot sell to you"

    months = _months_since(lead.get("last_sale_date"))
    recent = months is not None and months <= RECENT_PURCHASE_MONTHS
    financed = bool(lead.get("has_purchase_money_security_deed"))
    homestead = bool(lead.get("homestead_exemption"))

    if recent and financed and homestead:
        return True, f"owner-occupant bought {months:.0f} months ago with a mortgage"

    # A homesteaded recent purchase with no distress signal at all is the
    # same population even when we could not confirm the security deed.
    if recent and homestead and not _any_distress(lead):
        return True, f"homesteaded purchase {months:.0f} months ago, no distress signal"

    return False, ""


DISTRESS_KEYS = (
    "foreclosure_sale_date", "tax_delinquent_years", "tax_delinquent_amount",
    "estate_signal", "years_support", "probate_opened", "lis_pendens",
    "contractor_lien", "tax_fifa", "vacant", "code_violation",
    "assignment_recent",
)


def _any_distress(lead: dict) -> bool:
    return any(lead.get(k) for k in DISTRESS_KEYS)


# ------------------------------------------------------------------ score

# PRIMARY signals are actual events: something happened to this property or
# this owner. At least one is required for a lead to exist at all.
PRIMARY = [
    ("foreclosure_sale_date", 35, "foreclosure sale scheduled"),
    ("years_support",         26, "Year's Support, heir took title"),
    ("estate_signal",         24, "estate deed recorded"),
    ("probate_opened",        20, "probate case opened"),
    # An investor bought the lien: they can force foreclosure, so the
    # clock is already running whatever the county flag says.
    ("tax_cert_sold",         18, "tax lien certificate sold to an investor"),
    ("vacant",                16, "property appears vacant"),
    ("lis_pendens",           15, "lis pendens, litigation on title"),
    ("contractor_lien",       14, "contractor lien, stalled renovation"),
    ("tax_fifa",              12, "tax execution recorded"),
    ("assignment_recent",     12, "security deed assigned, foreclosure prep"),
    ("code_violation",        11, "code violation on record"),
]

# AMPLIFIERS describe the owner's situation. They raise urgency but never
# create a lead on their own -- somebody owning a house for 20 years is not
# a motivated seller, they are just a homeowner.
AMPLIFIERS = [
    ("absentee",              12, "owner mails elsewhere"),
    ("has_equity",            12, "owes little against the property's value"),
    ("free_and_clear",        10, "no mortgage on record"),
    ("out_of_state",           8, "owner is out of state"),
    ("no_homestead",           8, "no homestead exemption"),
    ("long_tenure",            8, "owned 15+ years"),
    ("confirmed_rental",       6, "registered rental, not owner-occupied"),
    ("entity_owner",           5, "entity owner, business decision"),
]

# Facts that make a lead WORSE. Scored rather than excluded: someone on a
# payment plan is still reachable, just less likely to sell today, and
# should rank below someone ignoring the bill entirely.
PENALTIES = [
    ("payment_plan",         -12, "on a payment plan, actively managing the debt"),
    ("underwater",           -15, "owes more against it than it looks worth"),
]

WEIGHTS = PRIMARY + AMPLIFIERS


def _tax_points(lead: dict) -> tuple[int, str]:
    yrs = lead.get("tax_delinquent_years") or 0
    amt = float(lead.get("tax_delinquent_amount") or 0)
    if not yrs and not amt:
        return 0, ""
    if yrs >= 3:
        return 25, f"{yrs:.0f} years tax delinquent"
    if yrs >= 2:
        return 18, "2 years tax delinquent"
    if yrs >= 1 or amt > 0:
        return 10, "tax delinquent"
    return 0, ""


def derive(lead: dict) -> dict:
    """Fill the derived booleans the scorer reads, from raw source fields.

    Kept separate from score() so a county source only has to supply the
    raw facts it actually has, and missing data degrades to False rather
    than to a wrong answer.
    """
    d = dict(lead)

    site_city = (d.get("site_city") or "").strip().upper()
    mail_city = (d.get("mail_city") or "").strip().upper()
    mail_state = (d.get("mail_state") or "").strip().upper()
    home_state = (d.get("state") or "GA").strip().upper()

    if mail_city:
        d["absentee"] = bool(site_city) and mail_city != site_city
    if mail_state:
        d["out_of_state"] = mail_state not in ("", home_state)
        if d["out_of_state"]:
            d["absentee"] = True

    # None means "we don't know", and must never be treated as a confirmed
    # False -- that would silently inflate a lead's score (no_homestead is
    # an amplifier) purely because a source hasn't looked this fact up yet.
    if d.get("homestead_exemption") is not None:
        d["no_homestead"] = not d["homestead_exemption"]

    if d.get("has_open_security_deed") is not None:
        d["free_and_clear"] = not d["has_open_security_deed"]

    yrs = _years_since(d.get("last_sale_date"))
    if yrs is not None:
        d["long_tenure"] = yrs >= 15
        d["years_owned"] = round(yrs, 1)

    m = _months_since(d.get("assignment_date"))
    if m is not None:
        d["assignment_recent"] = m <= 6

    return d


def persona(lead: dict) -> str:
    if lead.get("years_support") or lead.get("estate_signal") or lead.get("probate_opened"):
        return "heir"
    if lead.get("entity_owner") or lead.get("is_entity"):
        return "entity or investor"
    if lead.get("absentee") or lead.get("no_homestead"):
        return "absentee landlord"
    if lead.get("foreclosure_sale_date") or lead.get("tax_delinquent_years"):
        return "distressed occupant"
    return "owner occupant"


def score(lead: dict) -> Verdict:
    d = derive(lead)

    ex, why = exclude(d)
    if ex:
        return Verdict(excluded=True, reason=why, persona=persona(d))

    pts, signals, primary_hits = 0, [], 0

    for key, w, label in PRIMARY:
        if d.get(key):
            pts += w
            primary_hits += 1
            if key == "foreclosure_sale_date":
                label = f"foreclosure sale {d['foreclosure_sale_date']}"
            signals.append(label)

    tp, tl = _tax_points(d)
    if tp:
        pts += tp
        primary_hits += 1
        signals.insert(min(1, len(signals)), tl)

    # No event, no lead -- however attractive the owner profile looks.
    if primary_hits == 0:
        return Verdict(excluded=True, reason="no distress event, only owner profile",
                       persona=persona(d))

    for key, w, label in AMPLIFIERS:
        if d.get(key):
            pts += w
            signals.append(label)

    for key, w, label in PENALTIES:
        if d.get(key):
            pts += w
            signals.append(label)

    return Verdict(
        excluded=False,
        score=max(0, min(pts, 100)),
        persona=persona(d),
        signals=signals,
    )


def tier(score_value: int) -> str:
    """Buckets for the dialer queue. Call A before B."""
    if score_value >= 60:
        return "A"
    if score_value >= 35:
        return "B"
    return "C"
