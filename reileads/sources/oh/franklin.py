"""Franklin County (Columbus). The awkward one.

Franklin's GIS parcel layer has the tax columns in its schema but they are
EMPTY across all 494,740 parcels (verified: TOTCNTTXOD IS NOT NULL -> 0).
So there is no countywide foreclosure flag to diff, the way there is in
Cuyahoga and Hamilton.

What works instead is a WATCHLIST, not a sweep:

  1. Seed from the Treasurer's annual tax-lien-sale CSV plus any parcel we
     have already seen carrying a prior-year balance.
  2. Poll each watchlist parcel's Treasurer detail page daily. That page is
     a stable deep-linkable GET and exposes "Balance Due" and "Prior Total",
     which is the delinquency carry-forward.
  3. Escalation in those two numbers is the lead signal.

A few thousand parcels is a fine daily poll. 494,740 is not, so do not be
tempted to widen the watchlist to the whole county.

NOT YET WIRED: the Clerk of Courts Case Information Online new-filing
search is the true daily leading indicator for Franklin, but it sits behind
a terms-acceptance interstitial whose form shape we could not map without a
browser session. See README, "Franklin gap".
"""
import re
import csv
import io
import time
import logging
import requests

from ...core import config
from ...core.arcgis import ArcGISLayer
from ...core.normalize import strip_parcel, split_owner, clean_addr, absentee, is_entity

log = logging.getLogger(__name__)

GIS_URL = ("https://gis.franklincountyohio.gov/hosting/rest/services/"
           "ParcelFeatures/Parcel_Features/FeatureServer/0")

GIS_FIELDS = [
    "PARCELID", "OWNERNME1", "OWNERNME2", "SITEADDRESS", "ZIPCD",
    "PSTLNME1", "PSTLADDRES", "PSTLCITYSTZIP",
    "TOTVALUEBASE", "CLASSDSCRP", "OWNEROCCUPIED", "RENTAL",
    "SALEDATE", "SALEPRICE",
]

TREASURER_DETAIL = "https://treapropsearch.franklincountyohio.gov/Details.aspx"
LIEN_PAGE = "https://treasurer.franklincountyohio.gov/Delinquent-Taxes/Tax-Lien-Sale"

_MONEY = r"\$?\s*([\d,]+\.\d{2})"


def split_parcel_id(pid: str):
    """'050-010323-00' -> ('050','010323','00'). Returns None if unparseable."""
    m = re.match(r"^\s*(\d{3})-?(\d{6})-?(\d{2})\s*$", str(pid or "").strip())
    return m.groups() if m else None


def _money(text: str, label: str):
    m = re.search(re.escape(label) + r"\s*:?\s*" + _MONEY, text, re.I)
    return float(m.group(1).replace(",", "")) if m else None


class Franklin:
    county = "franklin"
    label = "Franklin (Columbus)"

    # ---------- watchlist seeding ----------

    @staticmethod
    def lien_list_url(session=None) -> str:
        """Scrape the Tax Lien Sale page for the current final list CSV.

        Do not hardcode the URL: it carries a version segment (/v/1/) that
        increments on re-upload, and the year is in the slug.
        """
        s = session or requests.Session()
        s.headers["User-Agent"] = config.USER_AGENT
        r = s.get(LIEN_PAGE, timeout=config.REQUEST_TIMEOUT)
        r.raise_for_status()
        hits = re.findall(r'href="([^"]*final-tax-lien-list[^"]*\.csv)"', r.text, re.I)
        if not hits:
            raise RuntimeError("no final-tax-lien-list CSV link found on " + LIEN_PAGE)
        url = sorted(hits)[-1]
        if url.startswith("/"):
            url = "https://treasurer.franklincountyohio.gov" + url
        return url

    @staticmethod
    def seed_from_lien_list() -> list:
        """Return parcel ids from the Treasurer's annual lien sale CSV.

        Column names are not published, so find the parcel column by
        matching the 050-010323-00 shape rather than by header text.
        """
        s = requests.Session()
        s.headers["User-Agent"] = config.USER_AGENT
        url = Franklin.lien_list_url(s)
        log.info("Franklin lien list: %s", url)
        r = s.get(url, timeout=config.REQUEST_TIMEOUT)
        r.raise_for_status()
        rows = list(csv.reader(io.StringIO(r.text)))
        if not rows:
            return []
        parcels, pat = [], re.compile(r"^\d{3}-?\d{6}-?\d{2}$")
        for row in rows[1:]:
            for cell in row:
                if pat.match((cell or "").strip()):
                    parcels.append(cell.strip())
                    break
        log.info("Franklin lien list: %s parcels", f"{len(parcels):,}")
        return parcels

    # ---------- daily poll ----------

    @staticmethod
    def poll_treasurer(parcel_display: str, session):
        parts = split_parcel_id(parcel_display)
        if not parts:
            return None
        district, parcel, ext = parts
        r = session.get(
            TREASURER_DETAIL,
            params={"district": district, "parcel": parcel, "ext": ext},
            timeout=config.REQUEST_TIMEOUT,
        )
        if r.status_code != 200:
            return None
        text = re.sub(r"<[^>]+>", " ", r.text)
        text = re.sub(r"&nbsp;?", " ", text)
        text = " ".join(text.split())
        return {
            "balance_due": _money(text, "Balance Due"),
            "prior_total": _money(text, "Prior Total"),
            "raw_len": len(text),
        }

    @staticmethod
    def fetch(watchlist=None):
        """Yield snapshot rows for the watchlist parcels.

        watchlist: list of display-format parcel ids ('050-010323-00').
        If None, seeds from the lien list.
        """
        if watchlist is None:
            watchlist = Franklin.seed_from_lien_list()
        if not watchlist:
            log.warning("Franklin watchlist is empty -- nothing to poll")
            return

        gis = _gis_lookup(watchlist)

        s = requests.Session()
        s.headers["User-Agent"] = config.USER_AGENT
        for i, pid in enumerate(watchlist, 1):
            tax = Franklin.poll_treasurer(pid, s)
            if tax is None:
                continue
            key = strip_parcel(pid)
            g = gis.get(key, {})
            owner = (g.get("OWNERNME1") or "").strip()
            first, last, full = split_owner(owner)
            mail_csz = (g.get("PSTLCITYSTZIP") or "").strip()
            mcity, mstate, mzip = _split_csz(mail_csz)

            balance = tax.get("balance_due") or 0.0
            prior = tax.get("prior_total") or 0.0

            payload = {
                "county": "Franklin",
                "state": "OH",
                "parcel": key,
                "parcel_display": pid,
                "owner_full": full or owner,
                "owner_first": first,
                "owner_last": last,
                "owner_second": (g.get("OWNERNME2") or "").strip(),
                "is_entity": is_entity(owner),
                "site_address": clean_addr(g.get("SITEADDRESS")),
                "site_city": "",
                "site_zip": g.get("ZIPCD"),
                "mail_name": g.get("PSTLNME1"),
                "mail_address": clean_addr(g.get("PSTLADDRES")),
                "mail_city": mcity,
                "mail_state": mstate,
                "mail_zip": mzip,
                "absentee": absentee("", mcity, mstate),
                "delq_balance": prior or balance,
                "balance_due": balance,
                "prior_total": prior,
                "market_value": g.get("TOTVALUEBASE"),
                "property_class": g.get("CLASSDSCRP"),
                "owner_occupied": g.get("OWNEROCCUPIED"),
                # Franklin has no foreclosure flag. A carried prior-year
                # total is the closest proxy the open data gives us.
                "foreclosure": 1 if prior > 0 else 0,
                "signal": "prior_year_balance_proxy",
                "record_url": (f"{TREASURER_DETAIL}?district={pid[:3]}"
                               f"&parcel={pid[4:10]}&ext={pid[-2:]}"),
            }
            yield {
                "parcel": key,
                "in_foreclosure": prior > 0,
                "delq_balance": prior or balance,
                "payload": payload,
            }
            if i % 25 == 0:
                log.info("  Franklin %s/%s polled", i, len(watchlist))
            time.sleep(0.8)   # deliberately gentle: this is an HTML page, not an API


def _split_csz(csz: str):
    m = re.match(r"^(.*?)[,\s]+([A-Z]{2})\s+(\d{5})", (csz or "").strip(), re.I)
    if m:
        return m.group(1).strip(), m.group(2).upper(), m.group(3)
    return csz.strip(), "", ""


def _gis_lookup(parcels) -> dict:
    """Batch-fetch owner/address for the watchlist from the GIS layer.

    Chunked because a where-IN clause has a practical URL length limit.
    """
    out = {}
    layer = ArcGISLayer(GIS_URL, page_size=3000, oid_field="OBJECTID")
    chunk = 150
    for i in range(0, len(parcels), chunk):
        ids = "','".join(p.replace("'", "") for p in parcels[i:i + chunk])
        where = f"PARCELID IN ('{ids}')"
        try:
            for a in layer.fetch_all(where, GIS_FIELDS):
                out[strip_parcel(a.get("PARCELID"))] = a
        except Exception as e:      # enrichment is optional, never fatal
            log.warning("Franklin GIS enrichment chunk failed: %s", e)
    return out
