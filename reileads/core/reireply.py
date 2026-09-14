"""REI Reply push. REI Reply is a white-label of HighLevel, so the API is
LeadConnector's, not a rebranded one.

Auth: sub-account -> Settings -> Other Settings -> Private Integrations.
Scopes: contacts.write (and contacts.readonly if you add lookups).

Rate limit is 100 requests / 10 seconds per sub-account, so we pace at 8/s.
Upsert is used rather than create, so a rerun updates instead of duplicating.
"""
import time
import logging
import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from . import config
from .normalize import looks_like_address, is_junk_text

log = logging.getLogger(__name__)


class ReiReply:
    def __init__(self, token=None, location_id=None):
        self.token = token or config.REIREPLY_TOKEN
        self.location_id = location_id or config.REIREPLY_LOCATION_ID
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {self.token}",
            "Version": config.REIREPLY_VERSION,
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        self._last = 0.0

    def _pace(self):
        gap = time.time() - self._last
        if gap < 0.125:              # 8 req/s, comfortably under 100/10s
            time.sleep(0.125 - gap)
        self._last = time.time()

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        retry=retry_if_exception_type(requests.RequestException),
        reraise=True,
    )
    def _post(self, path, body):
        self._pace()
        r = self.s.post(f"{config.REIREPLY_BASE}{path}", json=body,
                        timeout=config.REQUEST_TIMEOUT)
        if r.status_code == 429:
            time.sleep(10)
            raise requests.RequestException("429 rate limited, retrying")
        return r

    def upsert(self, contact: dict):
        """/contacts/upsert requires an email or phone to dedupe against --
        it 400s with "Pass at least one of number, email query parameter"
        otherwise (confirmed live 2026-09-14, all 19 real leads failed this
        way). Our leads have neither at push time -- phone comes later from
        skip trace -- so route those through plain /contacts/ create
        instead, which only requires locationId. Once a lead does carry a
        phone (post skip-trace repush, a future feature), upsert is used so
        a rerun updates the existing contact instead of duplicating it.
        """
        has_id = contact.get("phone") or contact.get("email")
        path = "/contacts/upsert" if has_id else "/contacts/"
        r = self._post(path, contact)
        if r.status_code >= 400:
            return None, f"http_{r.status_code}: {r.text[:180]}"
        data = r.json()
        c = data.get("contact") or {}
        return c.get("id"), ("new" if data.get("new", True) else "updated")


def to_contact(ev: dict, location_id: str) -> dict:
    """Map one pipeline event into a HighLevel contact.

    Deliberate choices:
      - No email is invented. HighLevel dedupes on email or phone; a fake
        email would collide records. Phone arrives later from skip trace.
      - The property address goes in custom fields, NOT address1. address1
        is the OWNER's mailing address, because that is where mail must go.
        Conflating the two is the classic way to mail a vacant house.
      - Everything is tagged so a workflow can trigger on tag, and so you
        can measure which county and which signal actually converts.
    """
    p = ev["payload"]
    county = p.get("county", "")
    # Every source module sets "state" explicitly (see sources/oh/* and
    # sources/ga/*) precisely so this mapper never has to guess it from
    # the county name.
    state = (p.get("state") or "").upper() or "??"
    tags = [
        "rei-leads",
        # Sharon's own label for this feed, used as the trigger on her
        # REI Reply automation. Kept alongside rei-leads rather than
        # replacing it: rei-leads is what this codebase guarantees, and
        # renaming the trigger tag would silently break her workflow.
        "county-records",
        f"state-{state.lower()}",
        f"county-{county.lower()}",
        f"signal-{ev['event'].replace('_','-')}",
        f"src-{ev['detected_on']}",
    ]
    if p.get("absentee"):
        tags.append("absentee-owner")
    if p.get("is_entity"):
        tags.append("entity-owner")
    if p.get("payment_plan"):
        tags.append("on-payment-plan")     # usually a weaker lead
    if p.get("tax_cert_sold"):
        tags.append("tax-cert-sold")
    if (p.get("portfolio_count") or 0) > 1:
        tags.append("portfolio-owner")     # several delinquent parcels, one owner
    if p.get("do_not_call"):
        # The only number we found is on the DNC list. Tagged rather than
        # withheld: Georgia's mini-TCPA carries a private right of action
        # up to $2,000 per knowing violation, so this must be filterable
        # in the CRM before anyone dials, not buried in a field.
        tags.append("dnc-do-not-dial")

    name = p.get("owner_full") or "Unknown Owner"
    mail = p.get("mail_address") or ""
    mail_ok = looks_like_address(mail) and not is_junk_text(p.get("mail_city"))
    contact = {
        "locationId": location_id,
        # No "name" field: confirmed live 2026-09-14 that HighLevel doesn't
        # store it as its own field at all -- it only uses it to DERIVE
        # firstName/lastName (split on the first space) and then silently
        # discards whatever firstName/lastName we sent alongside it. That's
        # how "ANOINTED FOUNTAIN CHURCH INC" became First="ANOINTED",
        # Last="FOUNTAIN CHURCH INC" the first time. The API also has no
        # company-contact type (contactType), so for an entity owner the
        # full name goes directly in firstName, lastName left blank --
        # the closest honest representation this API supports.
        "phone": p.get("phone") or None,
        "email": p.get("email") or None,
        "firstName": p.get("owner_first") or (name if p.get("is_entity") else None),
        "lastName": p.get("owner_last") or None,
        # HighLevel's actual company-name slot (shows in the "Business name"
        # column), separate from firstName/lastName -- added 2026-09-14
        # alongside the firstName workaround above so an entity owner shows
        # up correctly in both places, not just firstName.
        "companyName": name if p.get("is_entity") else None,
        # Mailing address -- where the owner actually receives mail. Falls
        # back to the property address when the county's mailing columns
        # hold something that isn't an address: Mahoning files tax
        # abatement notes there ("CRA 75% N / C 15 YR TY00-14", city "SEE
        # ABATED"), which reached live contacts on 2026-09-14.
        "address1": mail if mail_ok else (p.get("site_address") or None),
        "city": (p.get("mail_city") or None) if mail_ok else None,
        "state": (p.get("mail_state") or None) if mail_ok else None,
        "postalCode": (str(p.get("mail_zip") or "") or None) if mail_ok else None,
        "source": f"REI Leads {state} {county} feed",
        "tags": tags,
        "customFields": [
            {"key": "property_address", "field_value": p.get("site_address", "")},
            {"key": "parcel_number", "field_value": p.get("parcel_display", "")},
            {"key": "county", "field_value": county},
            {"key": "delinquent_balance", "field_value": str(p.get("delq_balance") or "")},
            {"key": "market_value", "field_value": str(p.get("market_value") or "")},
            {"key": "county_record_url", "field_value": p.get("record_url", "")},
            {"key": "signal_detected_on", "field_value": ev["detected_on"]},
        ],
    }
    return {k: v for k, v in contact.items() if v is not None}
