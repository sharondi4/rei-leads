"""Georgia foreclosure legal ads: Fulton, Cobb, Cherokee, Douglas.

This is the earliest PUBLIC signal for a Georgia foreclosure. Georgia is a
non-judicial foreclosure state, so the Notice of Sale Under Power is never
recorded anywhere -- it is only published, once a week for four weeks,
before the first-Tuesday sale (O.C.G.A. 9-13-141). GSCCCA never sees it.

Four counties, one CMS, but NOT one URL shape -- confirmed 2026-09-10 by
live navigation and cross-referencing indexed article URLs, after the
original code's assumption ("all four use the same
/classifieds/community/announcements/legal/ path") turned out to be
wrong for two of the four:

  douglas   -- /classifieds/community/announcements/legal/...,
               filename ad_<uuid>.html. This is the one county that
               actually matches the original assumption.
  cobb      -- /legal_notices/cobb_county_legal_notices/ (category page),
               filename article_<uuid>.html.
  cherokee  -- /legal_notices/cherokee_legal_notices/, same shape as Cobb.
               (Cobb and Cherokee's foreclosure notices both route to
               @mdjonline.com per their own legal-notice contact page,
               so this is likely one publisher's shared back end, not a
               coincidence.)
  fulton    -- unresolved. fultonneighbor.com has a "Legals" page and a
               separate inklynk.com-hosted "Legal Notice Order Entry
               Platform," neither of which has been confirmed to be a
               plain scrapable HTML listing. Do not guess a path here --
               `list_path` is left None and that county is skipped with
               a warning until someone opens fultonneighbor.com/legals/
               from a real browser and finds the actual notice archive.
               This matters because Fulton is the one county with parcel
               enrichment already wired up (fulton_parcel.py) -- it's
               the most valuable county to fix, not just another gap.

The site-wide `/search/?q=...` endpoint the original code used for all
four is also gone from this file: it renders as a JS app
(`tncms.page.app=searc` in the page's own script tags) rather than
server-rendered HTML, and separately, every host on this shared CDN
started serving 429s / a "Security Check" interstitial to this
environment's traffic after a handful of requests across the whole
TownNews network -- not just the one site being polled. Whatever you
deploy this on, expect the same shared rate limiter and budget your
polling interval accordingly (DEPLOY.md's daily cron, not tighter).

Why the address is easy to parse: O.C.G.A. 44-14-162(a) requires the ad
itself to print the street address, city and ZIP code IN BOLD TYPE. That
statutory requirement is what makes this reliably machine-readable rather
than free text.

NOT covered here, and why:
  Gwinnett  -- its paper's legal-notice path is disallowed in robots.txt.
              Use the Gwinnett tax commissioner's weekly file instead.
  DeKalb    -- notices are scanned PDF pages of the printed newspaper, not
              HTML. Needs PDF text extraction / OCR, not this parser.
"""
from __future__ import annotations

import re
import time
import logging
import datetime as dt
from dataclasses import dataclass

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from ...core import config

log = logging.getLogger(__name__)

# BLOX CMS legal-notice search, one per publisher. All four run the same
# software; only the host and county tag differ.
SITES = {
    # Verified 2026-09-10 (live navigation + cross-referenced indexed
    # article URLs). Two of the four original hostnames were wrong and
    # are fixed here; recheck this table periodically since these are
    # small-market papers that get rebranded. `list_path` is the archive
    # page actually confirmed to list these notices -- see module
    # docstring for why it differs per county, and why fulton is None.
    "fulton":   {"host": "www.fultonneighbor.com",        "county": "Fulton",
                 "list_path": None},
    "cobb":     {"host": "www.mdjonline.com",             "county": "Cobb",
                 "list_path": "/legal_notices/cobb_county_legal_notices/"},
    "cherokee": {"host": "www.tribuneledgernews.com",     "county": "Cherokee",
                 "list_path": "/legal_notices/cherokee_legal_notices/"},
    "douglas":  {"host": "www.douglascountysentinel.com", "county": "Douglas",
                 "list_path": "/classifieds/community/announcements/legal/"},
}

ADDR_RE = re.compile(
    r"""(?P<street>
          \d{1,6}\s+[A-Za-z0-9.'\- ]{3,60}?
          \s+(?:ST(?:REET)?|AVE(?:NUE)?|RD|ROAD|DR(?:IVE)?|LN|LANE|CT|COURT|
                WAY|BLVD|BOULEVARD|CIR(?:CLE)?|TRL|TRAIL|PL(?:ACE)?|PKWY|
                PARKWAY|TER(?:RACE)?|LOOP|XING|CROSSING|PATH|RUN|POINTE?|SQ)
          (?:\s+(?:N|S|E|W|NE|NW|SE|SW))?
          (?:\s*,?\s*(?:UNIT|STE|SUITE|APT|BLDG|\#)\s*[A-Za-z0-9\-]+)?
        )
        \s*,?\s*(?P<city>[A-Za-z .\-]{2,40}?)\s*,?\s*GA\s*(?P<zip>\d{5})""",
    re.IGNORECASE | re.VERBOSE,
)

SALE_DATE_RE = re.compile(
    r"first\s+Tuesday\s+in\s+([A-Za-z]+)[,\s]+(\d{4})", re.IGNORECASE
)

# Deed language is "Security Deed executed by GRANTOR to GRANTEE". Anchor
# specifically on "executed by" -- a bare "by" or "from" also matches
# earlier, unrelated boilerplate ("secured BY a Security Deed"), which
# swallows the whole clause into the captured name instead of just the
# grantor. Stop at "to" first, or any of the other closing phrases,
# whichever comes first in the text.
GRANTOR_RE = re.compile(
    r"""(?:against|executed\s+by)\s+
        (?P<grantor>[A-Z][A-Za-z.'\- ]{2,60}?)
        \s*,?\s*(?:\bto\b|formerly|a/?k/?a|f/?k/?a|will\s+sell|the\s+following)""",
    re.IGNORECASE | re.VERBOSE,
)

_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January","February","March","April","May","June","July","August",
     "September","October","November","December"], start=1)}


@dataclass
class NoticeHit:
    county: str
    url: str
    published: str
    site_address: str
    site_city: str
    site_zip: str
    sale_date: str | None
    grantor_raw: str
    raw_excerpt: str


class LegalAdsClient:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = config.USER_AGENT

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=2, max=20),
           retry=retry_if_exception_type(requests.RequestException), reraise=True)
    def _get(self, url, **kw):
        r = self.s.get(url, timeout=config.REQUEST_TIMEOUT, **kw)
        r.raise_for_status()
        return r

    def list_article_urls(self, site_key: str, days_back: int = 3) -> list[str]:
        """Article links found on that county's own notice-archive page.

        `days_back` isn't enforced here -- these archive pages are
        newest-first but not date-filterable by URL, and the articles
        themselves don't reliably embed a parseable date (see
        `_published_date`). Treat this as "recent" rather than "exactly
        N days," same as the rest of this module's unverified pieces.
        """
        site = SITES[site_key]
        if not site["list_path"]:
            raise NotImplementedError(
                f"{site_key}: no confirmed notice-archive page yet -- see module docstring")
        url = f"https://{site['host']}{site['list_path']}"
        r = self._get(url)
        esc = re.escape(site["list_path"])
        hrefs = set(re.findall(
            rf'href="(https?://[^"]*?{esc}[^"]+?\.html)"', r.text))
        # relative variant
        hrefs |= {f"https://{site['host']}{h}" for h in re.findall(
            rf'href="({esc}[^"]+?\.html)"', r.text)}
        return sorted(hrefs)

    def fetch_notice(self, site_key: str, url: str) -> NoticeHit | None:
        site = SITES[site_key]
        r = self._get(url)
        text = re.sub(r"<script.*?</script>", " ", r.text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"&nbsp;?", " ", text)
        text = re.sub(r"&amp;", "&", text)
        text = " ".join(text.split())

        if "notice of sale under power" not in text.lower():
            return None  # search can surface unrelated legal notices too

        m = ADDR_RE.search(text)
        if not m:
            log.warning("no address matched in %s", url)
            return None

        sale = SALE_DATE_RE.search(text)
        sale_date = None
        if sale:
            mon = _MONTHS.get(sale.group(1).lower())
            if mon:
                # first Tuesday of that month/year
                d = dt.date(int(sale.group(2)), mon, 1)
                offset = (1 - d.weekday()) % 7  # Tuesday = weekday 1
                sale_date = (d + dt.timedelta(days=offset)).isoformat()

        g = GRANTOR_RE.search(text)

        return NoticeHit(
            county=site["county"],
            url=url,
            published=_published_date(url),
            site_address=" ".join(m.group("street").split()).title(),
            site_city=m.group("city").strip().title(),
            site_zip=m.group("zip"),
            sale_date=sale_date,
            grantor_raw=(g.group("grantor").strip().title() if g else ""),
            raw_excerpt=text[:2000],
        )

    def fetch_county(self, site_key: str, days_back: int = 3):
        # A gap in one county (Fulton, today) must not take the other
        # three counties down with it -- they're independent sites.
        try:
            urls = self.list_article_urls(site_key, days_back)
        except NotImplementedError as e:
            log.warning("%s", e)
            return
        except requests.RequestException as e:
            log.warning("%s: notice-archive fetch failed: %s", site_key, e)
            return
        for i, url in enumerate(urls):
            if i:
                time.sleep(config.ARTICLE_FETCH_PAUSE_SECONDS)
            try:
                hit = self.fetch_notice(site_key, url)
            except requests.RequestException as e:
                log.warning("fetch failed %s: %s", url, e)
                continue
            if hit:
                yield hit


def _published_date(url: str) -> str:
    """BLOX article URLs commonly embed /YYYY/MM/DD/ in the path."""
    m = re.search(r"/(\d{4})/(\d{2})/(\d{2})/", url)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


class LegalAdsForeclosure:
    """Adapter matching the source interface used elsewhere in the
    pipeline: fetch() yields {parcel, in_foreclosure, delq_balance, payload}.

    There is no county parcel number here -- a newspaper ad has no parcel
    ID. We key on a hash of county+address+sale_date instead, and the
    downstream enrichment step (parcel lookup) is what attaches a real
    parcel number and owner name before scoring.
    """
    county = "ga_legal_ads"
    label = "Georgia foreclosure notices (Fulton/Cobb/Cherokee/Douglas)"

    @staticmethod
    def fetch(counties=None, days_back=3):
        import hashlib
        client = LegalAdsClient()
        for key in (counties or list(SITES)):
            for hit in client.fetch_county(key, days_back=days_back):
                fp = hashlib.sha1(
                    f"{hit.county}|{hit.site_address}|{hit.sale_date}".encode()
                ).hexdigest()[:16]
                payload = {
                    "county": hit.county,
                    "state": "GA",
                    "source": "legal_ad",
                    "notice_url": hit.url,
                    "published": hit.published,
                    "site_address": hit.site_address,
                    "site_city": hit.site_city,
                    "site_zip": hit.site_zip,
                    "foreclosure_sale_date": hit.sale_date,
                    "grantor_name_raw": hit.grantor_raw,
                    # Filled in later by the parcel-enrichment step:
                    "owner_full": "", "parcel_display": "",
                    "mail_address": "", "mail_city": "", "mail_state": "",
                    "homestead_exemption": None,
                }
                yield {
                    "parcel": fp,
                    "in_foreclosure": True,
                    "delq_balance": None,
                    "payload": payload,
                }
