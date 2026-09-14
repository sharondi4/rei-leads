"""Parcel, name and address normalization shared across counties."""
import re
import datetime as dt

_SUFFIXES = {"JR", "SR", "II", "III", "IV", "TRUSTEE", "TR", "ETAL", "ET AL"}
# Matched as whole words so that "TRUSTEE" (a human acting as one) is not
# mistaken for "TRUST" (an entity).
_ENTITY_WORDS = {
    "LLC", "LC", "INC", "CORP", "CORPORATION", "COMPANY", "CO", "TRUST",
    "LP", "LLP", "LTD", "BANK", "ASSOC", "ASSOCIATION", "PARTNERS",
    "PARTNERSHIP", "PROPERTIES", "HOLDINGS", "CHURCH", "MINISTRIES",
    "AUTHORITY", "DEVELOPMENT", "INVESTMENTS", "ENTERPRISES", "REALTY",
    "MORTGAGE", "SERVICING", "FUND", "GROUP", "VENTURES",
}
_ENTITY_PHRASES = (
    "CITY OF", "VILLAGE OF", "COUNTY OF", "STATE OF", "LAND BANK",
    "BOARD OF", "ESTATE OF", "L L C", "DEPARTMENT",
    "HOUSING AUTH", "METROPOLITAN",
)


def strip_parcel(pid) -> str:
    """Dash/space-free uppercase parcel id. Cuyahoga MyPlace and the Clerk
    use dashed form; ArcGIS uses bare. Join on this."""
    if pid is None:
        return ""
    return re.sub(r"[^A-Z0-9]", "", str(pid).upper())


def is_entity(name: str) -> bool:
    """True if the owner looks like a company, trust or government body.
    Entities are not skip-traceable the same way and often should not be
    called at all, so the pipeline tags them rather than dropping them."""
    if not name:
        return True
    u = name.upper()
    if any(ph in u for ph in _ENTITY_PHRASES):
        return True
    words = set(re.split(r"[^A-Z]+", u)) - {""}
    return bool(words & _ENTITY_WORDS)


def split_owner(name: str):
    """Best-effort first/last from county owner strings.

    County format is overwhelmingly 'LAST, FIRST M' and sometimes
    'LAST, FIRST & SPOUSE'. Returns (first, last, full)."""
    full = " ".join((name or "").split()).strip(" ,")
    if not full:
        return "", "", ""
    if is_entity(full):
        return "", "", full

    if "," in full:
        last, _, rest = full.partition(",")
        last = last.strip()
        rest = rest.strip()
        # 'JOHN & MARY' -> take the first given name only
        rest = re.split(r"\s*&\s*|\s+AND\s+", rest)[0].strip()
        parts = [p for p in rest.split() if p.upper() not in _SUFFIXES]
        first = parts[0] if parts else ""
        return first.title(), last.title(), full

    # No comma. Ohio county files use "LAST FIRST MIDDLE" here, and the
    # middle is usually a bare initial. Drop initials, then last-first.
    parts = [p for p in full.split()
             if p.upper() not in _SUFFIXES and len(p.strip(".")) > 1]
    if len(parts) >= 2:
        return parts[1].title(), parts[0].title(), full
    return "", full.title(), full


def clean_addr(*parts) -> str:
    out = " ".join(str(p).strip() for p in parts if p and str(p).strip() and str(p).strip().upper() != "NONE")
    return " ".join(out.split())


def year_of(v):
    """Best-effort calendar year out of the several date shapes county
    layers use. Returns None when there's nothing usable -- callers must
    treat that as unknown, never as a default.

    Epoch milliseconds are checked BEFORE any regex: a raw ArcGIS
    timestamp like 1421971200000 contains the digits "1971", so a naive
    4-digit-year search reads it as the year 1971.
    """
    if v in (None, "", 0):
        return None
    s = str(v).strip()
    if s.isdigit():
        if len(s) >= 11:                       # epoch milliseconds
            try:
                return dt.datetime.fromtimestamp(int(s) / 1000, dt.timezone.utc).year
            except (ValueError, OverflowError, OSError):
                return None
        if len(s) == 4:                        # bare year, e.g. "2014"
            return int(s)
    m = re.match(r"(\d{4})-", s) or re.search(r"\b(19|20)\d{2}\b", s)
    return int(m.group(0)[:4]) if m else None


def arcgis_date(v) -> str:
    """ArcGIS date fields arrive as epoch milliseconds. Return ISO
    'YYYY-MM-DD', or "" when absent -- classify.py reads a missing sale
    date as unknown, so returning "" is safe and inventing one is not."""
    if v in (None, "", 0):
        return ""
    s = str(v).strip()
    if s.isdigit() and len(s) >= 11:
        try:
            return dt.datetime.fromtimestamp(int(s) / 1000, dt.timezone.utc).date().isoformat()
        except (ValueError, OverflowError, OSError):
            return ""
    return s[:10]


def absentee(site_city: str, mail_city: str, mail_state: str) -> bool:
    """Owner mails somewhere other than the property. Strongest single
    predictor in a distress list."""
    if not mail_city:
        return False
    if mail_state and mail_state.strip().upper() not in ("OH", "OHIO", ""):
        return True
    return site_city.strip().upper() != mail_city.strip().upper()
