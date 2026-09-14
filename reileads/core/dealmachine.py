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

    def enrich_name(self, last_name: str, first_name: str = "",
                    zip_code: str = "", state: str = "",
                    per_page: int = 3, estimate: bool = False) -> tuple[list, int]:
        """Look up one named person. Returns (people, credits charged).

        One person per call, deliberately. This endpoint returns EVERY
        person matching the name in the area and bills 1 credit per
        person returned -- batching 25 names into one call would let a
        single common surname return dozens of people and spend dozens of
        credits on one lead. per_page is the actual spending control.

        Narrowed by the owner's mailing ZIP where we have one, since name
        plus state alone would match strangers. include_properties stays
        off: property data would add a property credit per record for
        facts the county already gave us.
        """
        if not last_name:
            return [], 0
        person = {"last_name": last_name}
        if first_name:
            person["first_name"] = first_name
        body = {
            "data": [person],
            "fields": PERSON_FIELDS,
            "include_properties": False,
            "per_page": per_page,
            "page": 1,
        }
        if zip_code:
            body["location"] = {"type": "zip_code", "code": str(zip_code)[:5]}
        elif state:
            body["location"] = {"type": "state", "code": state[:2].upper()}
        if estimate:
            # Their own cost preview -- returns what the call WOULD spend
            # without spending it. This is what makes a trial free.
            body["estimate_cost"] = True
        data = self._post("/enrichment/name", body)
        credits = int((data.get("credits") or {}).get("used") or 0)
        return data.get("data") or [], credits


def all_phones(result: dict) -> list[dict]:
    """Every number found, best first.

    DNC status outranks line type, deliberately. A mobile is the better
    number to reach someone on, but Georgia's mini-TCPA carries a private
    right of action up to $2,000 per knowing violation -- so a clean
    landline beats a mobile on the Do Not Call list every time, and the
    ordering must not be the other way round.

    Nothing is discarded: DNC numbers ride along flagged, because the
    caller needs to see that a number exists and why it isn't the one
    being dialled.
    """
    phones = []
    for c in (result.get("contacts") or [result]):
        phones.extend(c.get("phones") or [])
    phones.sort(key=lambda p: (bool(p.get("do_not_call")), p.get("type") != "wireless"))
    return phones


def best_phone(result: dict) -> tuple[str, str, bool]:
    """(number, type, do_not_call) for the single best number, or blanks."""
    phones = all_phones(result)
    if not phones:
        return "", "", False
    top = phones[0]
    return str(top.get("number") or ""), str(top.get("type") or ""), bool(top.get("do_not_call"))


def best_email(result: dict) -> str:
    for c in (result.get("contacts") or [result]):
        for e in (c.get("emails") or []):
            addr = e if isinstance(e, str) else e.get("address") or e.get("email")
            if addr:
                return str(addr)
    return ""
