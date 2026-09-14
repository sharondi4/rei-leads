"""Douglas County, GA probate court -- estate/Year's Support signal.

Source: georgiaprobaterecords.com (i3 Verticals "TrueFiling"), confirmed
2026-09-11: no login, no robots.txt, no Terms of Use page found anywhere
on the site (checked common paths and the homepage's actual links --
just a generic i3 Verticals corporate privacy policy, nothing about
automated access). Genuinely open, unlike GSCCCA, georgiapublicnotice.com,
and the GA newspaper sites this project has hit ToS walls on today.

Why Playwright and not plain `requests` (every other source in this repo
uses requests): this is a Telerik RadGrid/RadDropDownList UI that runs
its search through Telerik's client-side AJAX framework. A plain
synchronous form POST with all the right field values still returns zero
results -- confirmed by testing the simplest possible case (a bare last-
name search) and getting nothing back. The site itself isn't gating
anything; the UI just won't respond to a request shaped like a normal
browser wouldn't produce. Playwright drives the real page the way a
person would, which is a different thing from defeating a CAPTCHA or
working around an access restriction -- there is no restriction here.

Confirmed quirk: searching by county alone, with no date range, returns
nothing at all -- the site appears to require a Filed Date range to
actually execute a query. That's convenient here since a date range is
exactly what a daily "what's new" run needs anyway.

Detail-page enrichment (added 2026-09-11): each search-result row's
EstateDetails.aspx?RECID=... page carries the decedent's last known
street address (`#cpMain_lblStreetAddress` / `#cpMain_lblCityStateZip`,
confirmed stable element IDs, not text-position guessing) and a PARTIES
table -- a real ASP.NET repeater, indexed IDs
`cpMain_repParty_lbl{Relationship,Party,Address,CityStateZip,PartyType}_{i}`
-- listing every heir, administrator, attorney, and interested party
with their own name and mailing address.

This matters because the search grid only gives you the DECEDENT's name
-- calling or mailing the dead person is useless. The actual lead is
whichever party's responsibility (`lblPartyType_{i}`) contains "HEIR".
That party's own name/address becomes owner_full/mail_address; the
decedent's name and last address become site_address/decedent_name.
Honest caveat this doesn't resolve: the decedent's last address is a
*residence*, not a confirmed *owned property* -- some fraction of these
will be renters, not owners, and this source has no way to tell the
difference. Worth cross-checking against Douglas County's parcel records
before treating an address as confirmed real estate, same caution as
any owner-name-based enrichment elsewhere in this codebase.

Case-type inference is NOT attempted. Case numbers seen during testing
carried prefixes like "PC" and "E" (e.g. 00PC0034, 05E0320) that likely
distinguish petition-for-probate from estate administration, and this
project's research separately found "WS" used elsewhere in Georgia for
Year's Support -- but none of that is confirmed against an authoritative
source, so it is not guessed at here. Every row is tagged only as
`probate_opened`, the one thing that's certain.

Only Douglas is confirmed here. The county dropdown lists all 159 GA
counties, but that only means they're selectable, not that this vendor
actually has data for them -- Fulton and Cherokee's real probate records
were separately confirmed today to live on a different, paid/login
system (Tyler re:SearchGA), and Cobb was confirmed NOT present on this
platform at all. Do not add another county here without repeating this
same live verification.
"""
from __future__ import annotations

import re
import logging
import datetime as dt
from dataclasses import dataclass

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.georgiaprobaterecords.com/Estates/SearchEstates.aspx"

# CourtID values from the search form's county dropdown. Douglas was
# decoded from __VIEWSTATE 2026-09-11; the rest were read from the
# dropdown's own RadDropDownList ClientState on 2026-09-14, which anchors
# against Douglas's known-good value.
#
# The IDs are informational -- fetch() selects the county by its visible
# label, not this number. They're kept because they're the only stable
# identifier if the labels ever change.
#
# 94 of Georgia's 159 counties are on the estates search. Absent, and
# confirmed so rather than assumed: Fulton, Cobb, Cherokee, DeKalb and
# Gwinnett (Tyler Odyssey / re:SearchGA instead), plus Rockdale, and
# Forsyth (files on this vendor but publishes no estates).
#
# Membership in the dropdown does NOT prove a county returns rows or
# populates the PARTIES table -- coverage is per-court and uneven. Every
# entry below was probed live before being added; see module docstring.
COUNTIES = {
    "douglas": "1120",
}

# Probed live 2026-09-14 and confirmed to return ZERO estate rows -- not
# over 30 days, and for Clayton and Hall not over a full 365 days either,
# while Douglas returned 200 on the same code path. They are wired into
# the vendor for e-filing but publish no estate records through it.
# Recorded here so nobody spends the afternoon rediscovering it; adding
# any of them back costs ~30s of browser startup per daily run for
# nothing.
EMPTY_COUNTIES = {
    "clayton": "1109", "henry": "1017", "paulding": "1051",
    "coweta": "1054", "carroll": "1026", "newton": "1067",
    "bartow": "1001", "hall": "1009", "fayette": "1024",
}

_CASE_NO_RE = re.compile(r"^\d{2}[A-Z]{1,3}\d+$")


@dataclass
class ProbateHit:
    case_number: str
    decedent: str
    city: str
    state: str
    died: str
    county: str
    detail_url: str
    # Filled in by _fetch_detail(), not the search grid -- see there.
    street_address: str = ""
    decedent_city_state_zip: str = ""


@dataclass
class Party:
    relationship: str
    name: str
    address: str
    city_state_zip: str
    party_type: str


class DouglasProbate:
    county = "douglas_probate"
    label = "Douglas County GA Probate Court"

    @staticmethod
    def fetch(county_key: str = "douglas", days_back: int = 7,
              offset_days: int = 0):
        """Yield events for cases filed between `days_back` and
        `offset_days` days ago.

        Filed-date range, not deceased-date -- filing is what makes a
        case newly discoverable, same reasoning as every date-diff
        source elsewhere in this codebase.

        offset_days exists so a long lookback can be walked in month
        chunks: paging ~20 deep through this RadGrid dies with the rows
        detached from the DOM, so callers ask for one month at a time
        rather than one large range. See pipeline_ga.run_probate().
        """
        from playwright.sync_api import sync_playwright

        court_id = COUNTIES[county_key]
        today = dt.date.today() - dt.timedelta(days=offset_days)
        start = dt.date.today() - dt.timedelta(days=days_back)
        # Zero-padded MM/DD/YYYY -- confirmed required 2026-09-11: the
        # RadDatePicker silently rejects "3/20/2021" (no leading zero)
        # and the search then behaves as if no date range were given at
        # all, which returns zero rows (see module docstring on why a
        # date range is required in the first place).
        start_s = start.strftime("%m/%d/%Y")
        end_s = today.strftime("%m/%d/%Y")

        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page()
                # Deliberately NOT setting config.USER_AGENT here, unlike
                # every requests-based source in this codebase -- confirmed
                # 2026-09-11: overriding the UA breaks this site's Telerik
                # client-side rendering (likely UA-sniffed feature
                # detection) and the search silently returns zero rows
                # with no error. Playwright's real Chromium UA is what
                # this site needs to see.
                page.goto(SEARCH_URL, timeout=30000)
                page.wait_for_load_state("networkidle")

                page.click("#ctl00_cpMain_ddlCounty")
                page.wait_for_timeout(400)
                # Match by CourtID's county name via the li's own text --
                # COUNTIES keys are lowercase, li text is title case.
                county_label = county_key.replace("_", " ").title()
                page.click(f'li.rddlItem:text-is("{county_label}")')
                page.wait_for_timeout(300)

                page.fill("#ctl00_cpMain_txtFiledStartDate_dateInput", start_s)
                page.fill("#ctl00_cpMain_txtFiledEndDate_dateInput", end_s)
                page.click("#ctl00_cpMain_btnSearch_input")
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(1500)

                hits = _read_all_pages(page, county_label)
                log.info("Douglas probate: %s new case(s) filed in last %s days",
                         len(hits), days_back)

                # Detail-page enrichment is a second pass, after the
                # search-results page is done with -- navigating away
                # from it mid-pagination would lose the RadGrid's state.
                for hit in hits:
                    parties = []
                    if hit.detail_url:
                        try:
                            parties = _fetch_detail(page, hit)
                        except Exception as e:
                            log.warning("detail fetch failed for %s: %s", hit.case_number, e)
                    yield _to_event(hit, parties)
            finally:
                browser.close()


def _read_all_pages(page, county_label: str, max_pages: int = 20) -> list:
    hits = []
    seen_case_numbers = set()
    for _ in range(max_pages):
        rows = page.locator("#ctl00_cpMain_rgEstates_ctl00 tbody tr")
        any_data_row = False
        for i in range(rows.count()):
            row = rows.nth(i)
            cells = row.locator("td")
            if cells.count() != 6:
                continue
            case_no = cells.nth(0).inner_text().strip()
            if not _CASE_NO_RE.match(case_no):
                continue
            any_data_row = True
            if case_no in seen_case_numbers:
                continue
            seen_case_numbers.add(case_no)

            link = row.locator("a").first
            href = link.get_attribute("href") if link.count() else None

            hits.append(ProbateHit(
                case_number=case_no,
                decedent=cells.nth(1).inner_text().strip(),
                city=cells.nth(2).inner_text().strip(),
                state=cells.nth(3).inner_text().strip(),
                died=cells.nth(4).inner_text().strip(),
                county=cells.nth(5).inner_text().strip() or county_label.upper(),
                detail_url=(f"https://www.georgiaprobaterecords.com/Estates/{href}"
                            if href else ""),
            ))

        if not any_data_row:
            break

        next_page = page.locator("a.rgCurrentPage").locator(
            "xpath=following-sibling::a[1]")
        if next_page.count() == 0:
            break
        next_page.first.click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1200)
    return hits


def _fetch_detail(page, hit: "ProbateHit") -> list:
    """Navigate to this case's detail page and return its PARTIES list.

    Also fills in hit.city/state from the decedent's actual last address
    (`lblCityStateZip`) if the search grid's own city/state were blank --
    confirmed 2026-09-11 some rows (e.g. case 00PC0221) have empty
    city/state in the grid but a real address on the detail page.
    """
    page.goto(hit.detail_url, timeout=30000)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(800)

    def _text(selector: str) -> str:
        loc = page.locator(selector)
        return loc.inner_text().strip() if loc.count() else ""

    hit.street_address = _text("#cpMain_lblStreetAddress")
    csz = _text("#cpMain_lblCityStateZip")
    hit.decedent_city_state_zip = csz

    parties = []
    i = 0
    while True:
        rel = page.locator(f"#cpMain_repParty_lblRelationship_{i}")
        if rel.count() == 0:
            break
        parties.append(Party(
            relationship=rel.inner_text().strip(),
            name=_text(f"#cpMain_repParty_lblParty_{i}"),
            address=_text(f"#cpMain_repParty_lblAddress_{i}"),
            city_state_zip=_text(f"#cpMain_repParty_lblCityStateZip_{i}"),
            party_type=_text(f"#cpMain_repParty_lblPartyType_{i}"),
        ))
        i += 1
    return parties


_CSZ_RE = re.compile(r"^(.*?)[,\s]+([A-Z]{2})\s+(\d{5})")


def _split_name(full_name: str):
    from ...core.normalize import _SUFFIXES
    parts = [p for p in full_name.split() if p]
    # Strip trailing suffixes (JR/SR/II/III/...) before treating the last
    # word as a surname, or "JETHROE MOORE II" reads as last name "II".
    while len(parts) > 2 and parts[-1].upper().strip(".") in _SUFFIXES:
        parts.pop()
    if len(parts) >= 2:
        return parts[0].title(), parts[-1].title()
    return "", full_name.title()


def _to_event(hit: ProbateHit, parties: list) -> dict:
    from ...core.normalize import is_entity

    dec_first, dec_last = _split_name(hit.decedent)

    # Prefer the detail page's own city/state (some search-grid rows come
    # back blank there, e.g. case 00PC0221, even though the detail page
    # has a real address) -- see _fetch_detail()'s docstring.
    m = _CSZ_RE.match(hit.decedent_city_state_zip or "")
    site_city = (m.group(1).strip() if m else hit.city).title()
    site_state = (m.group(2) if m else hit.state) or "GA"
    site_zip = m.group(3) if m else ""

    heirs = [p for p in parties if "HEIR" in p.party_type.upper()]
    admins = [p for p in parties if "ADMINISTRATOR" in p.party_type.upper()]
    # The actual lead: whoever inherits, not the decedent. Fall back to
    # an administrator only if no party is explicitly tagged an heir.
    contact_pool = heirs or admins
    contact = contact_pool[0] if contact_pool else None
    co_contacts = [p.name.title() for p in contact_pool[1:]]

    mail_city, mail_state, mail_zip = "", "", ""
    contact_first, contact_last = "", ""
    if contact:
        mm = _CSZ_RE.match(contact.city_state_zip or "")
        if mm:
            mail_city, mail_state, mail_zip = mm.group(1).strip().title(), mm.group(2), mm.group(3)
        contact_first, contact_last = _split_name(contact.name)

    payload = {
        "county": hit.county.title(),
        "state": "GA",
        "source": "douglas_probate",
        "case_number": hit.case_number,
        "notice_url": hit.detail_url,
        "decedent_name": hit.decedent,
        "deceased_date": hit.died,
        # The decedent's last known address -- likely, not confirmed, to
        # be the property in the estate. See module docstring.
        "site_address": hit.street_address,
        "site_city": site_city,
        "site_state": site_state,
        "site_zip": site_zip,
        # The actual contact: an heir if one is named on the case,
        # otherwise the administrator, otherwise nobody -- never the
        # decedent. `contact_relationship` records which of those three
        # actually happened, so a missing contact is visible, not silent.
        "owner_full": contact.name.title() if contact else "",
        "owner_first": contact_first if contact else "",
        "owner_last": contact_last if contact else "",
        "is_entity": is_entity(contact.name) if contact else False,
        "contact_relationship": (contact.party_type.title() if contact else "none found"),
        "co_heirs": co_contacts,
        "mail_address": contact.address.title() if contact else "",
        "mail_city": mail_city,
        "mail_state": mail_state,
        "mail_zip": mail_zip,
        # Computed here (not core.normalize.absentee(), which is
        # hardcoded to Ohio and confirmed 2026-09-11 to mark every
        # Georgia address as absentee regardless of city match) so it's
        # a visible CSV column same as the Ohio sources, not just an
        # internal scoring input. classify.py's derive() recomputes this
        # the same way for scoring, so the two never disagree.
        "absentee": bool(mail_city) and (mail_city.upper() != site_city.upper() or mail_state.upper() not in ("GA", "")),
        # The one thing confirmed by every row regardless of enrichment --
        # see module docstring for why estate_signal/years_support aren't
        # also set.
        "probate_opened": True,
        "homestead_exemption": None,
    }
    return {
        "parcel": hit.case_number,
        "in_foreclosure": False,
        "delq_balance": None,
        "payload": payload,
    }
