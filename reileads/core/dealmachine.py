"""DealMachine enrichment: turn a known owner into a phone number.

Why people enrichment rather than the property/APN endpoints: we already
hold the property facts from the county (address, owner name, mailing
address, assessed value). The only thing missing is a way to reach the
person. Their credit model charges 1 credit per unique property AND 1 per
unique person returned, so an APN lookup costs two credits for data we
half-own already. Anchoring on the person costs one.

Credits are the hard constraint. The Basic plan grants 10,000 per billing
month and Sharon does not want to buy more, so:

  - every trace is recorded in `skiptraces`, keyed by (county, parcel), so
    a rerun never re-spends a credit on a parcel we've already done;
  - DealMachine itself deduplicates within a billing period, but that
    won't survive a month boundary and our table does;
  - a configurable monthly ceiling stops tracing before the plan's limit,
    leaving headroom rather than discovering the wall by hitting it;
  - leads are traced highest-urgency first, so if the ceiling is ever
    reached it's the weakest leads that go without.

Every response carries a `credits` object ({used, properties, people,
deduplicated}); `used` is what actually gets charged after their own
deduplication, and that -- not our estimate -- is what gets recorded.
"""
from __future__ import annotations

import logging
import time

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from . import config

log = logging.getLogger(__name__)

BASE = "https://api.v2.dealmachine.com/v1"

# Only what's needed to reach somebody. Their docs are explicit that
# requesting fewer fields means smaller, faster responses, and property
# fields beyond address context would add a property credit per record.
PERSON_FIELDS = ["full_name", "phones", "emails"]


class DealMachineError(RuntimeError):
    pass


class DealMachine:
    def __init__(self, api_key: str = None):
        self.api_key = api_key or config.DEALMACHINE_API_KEY
        if not self.api_key:
            raise DealMachineError(
                "DEALMACHINE_API_KEY is not set -- add it to .env on the server. "
                "Nothing is pushed to REI Reply untraced, so this blocks the run."
            )
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        self._last = 0.0

    def _pace(self):
        gap = time.time() - self._last
        if gap < 0.25:
            time.sleep(0.25 - gap)
        self._last = time.time()

    @retry(stop=stop_after_attempt(3),
           wait=wait_exponential(multiplier=2, min=2, max=20),
           retry=retry_if_exception_type(requests.RequestException),
           reraise=True)
    def _post(self, path: str, body: dict) -> dict:
        self._pace()
        r = self.s.post(f"{BASE}{path}", json=body, timeout=config.REQUEST_TIMEOUT)
        if r.status_code == 429:
            time.sleep(15)
            raise requests.RequestException("429 rate limited, retrying")
        if r.status_code >= 400:
            raise DealMachineError(f"http_{r.status_code}: {r.text[:300]}")
        return r.json()

    def account(self) -> dict:
        """Plan and remaining-credit info. Read before a batch so the
        ceiling is checked against DealMachine's own count, not only
        ours -- credits spent outside this pipeline count too."""
        self._pace()
        r = self.s.get(f"{BASE}/account", timeout=config.REQUEST_TIMEOUT)
        if r.status_code >= 400:
            raise DealMachineError(f"account http_{r.status_code}: {r.text[:200]}")
        return r.json()

    def enrich_people(self, records: list[dict]) -> tuple[list, int]:
        """Look up contact details for people we can already name.

        records: [{"full_name", "address", "city", "state", "zip"}]
        Returns (per-input results, credits actually charged).
        """
        if not records:
            return [], 0
        body = {
            "data": records,
            "fields": PERSON_FIELDS,
            "contact_audience": "owners",
        }
        data = self._post("/people/enrich/name", body)
        credits = int((data.get("credits") or {}).get("used") or 0)
        return data.get("data") or [], credits


def best_phone(result: dict) -> tuple[str, str, bool]:
    """Pick a number to call: (number, type, do_not_call).

    Mobile first -- these are acquisition calls to individuals and a
    landline for a house someone inherited and moved out of is usually
    dead. DNC numbers are returned rather than hidden so the caller can
    decide; Georgia's mini-TCPA carries a private right of action up to
    $2,000 per knowing violation, so this flag must reach the CRM rather
    than being quietly dropped here.
    """
    phones = []
    for c in (result.get("contacts") or [result]):
        phones.extend(c.get("phones") or [])
    if not phones:
        return "", "", False
    phones.sort(key=lambda p: (p.get("type") != "wireless", bool(p.get("do_not_call"))))
    top = phones[0]
    return str(top.get("number") or ""), str(top.get("type") or ""), bool(top.get("do_not_call"))


def best_email(result: dict) -> str:
    for c in (result.get("contacts") or [result]):
        for e in (c.get("emails") or []):
            addr = e if isinstance(e, str) else e.get("address") or e.get("email")
            if addr:
                return str(addr)
    return ""
