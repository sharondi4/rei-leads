"""Mahoning County (Youngstown).

Source: the county's own land-bank/delinquent-properties layer, purpose-
built for exactly this signal (confirmed live 2026-09-10: 25,464 records).

Quirk found while verifying this layer: requesting a single named field
via `outFields` (e.g. just `PARCEL_ID`) makes the server return a hard
400 "Failed to execute query," even though `returnCountOnly` and
`outFields=*` both work fine on the same layer. Cause unconfirmed --
possibly the duplicate-looking PARCEL_ID/PARCEL_ID_1 columns in this
layer's schema confusing whatever view backs it. Fix is simply to always
request `outFields=*` here rather than a field list; ArcGISLayer.fetch_all
doesn't support that, so this module builds its own query instead of
using the shared client. Don't "simplify" this back to a field list
without retesting -- it will silently start 400ing.

DELINQUENT is a dollar amount, not a flag -- there is no boolean
foreclosure field on this layer, same situation as Stark.
"""
import re
import time
import logging
import requests

from ...core import config
from ...core.normalize import strip_parcel, split_owner, clean_addr, absentee, is_entity

log = logging.getLogger(__name__)

URL = ("https://gisapp.mahoningcountyoh.gov/arcgis/rest/services/"
       "LANDBANK_DELINQUENT_PROPERTIES/MapServer/0/query")

WHERE = "DELINQUENT > 0"


class Mahoning:
    county = "mahoning"
    label = "Mahoning (Youngstown)"

    @staticmethod
    def fetch():
        s = requests.Session()
        s.headers["User-Agent"] = config.USER_AGENT
        offset = 0
        page_size = 2000
        while True:
            r = s.get(URL, params={
                "where": WHERE, "outFields": "*", "f": "json",
                "returnGeometry": "false", "resultOffset": offset,
                "resultRecordCount": page_size,
            }, timeout=config.REQUEST_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                log.error("Mahoning query failed: %s", data["error"])
                return
            feats = data.get("features", [])
            if not feats:
                break
            for f in feats:
                yield from _to_event(f["attributes"])
            if len(feats) < page_size and not data.get("exceededTransferLimit"):
                break
            offset += page_size
            time.sleep(config.PAGE_PAUSE_SECONDS)


def _to_event(a: dict):
    parcel = strip_parcel(a.get("PARCEL_ID"))
    if not parcel:
        return
    owner = (a.get("OWNNAME1") or "").strip()
    first, last, full = split_owner(owner)

    site = clean_addr(a.get("LOCNUM"), a.get("LOCPREF"), a.get("LOCSTREET"), a.get("LOCSUFFIX"))
    site_city = (a.get("LOCCITY") or "").strip()
    site_state = (a.get("LOCSTATE") or "").strip()

    mail_addr = clean_addr(a.get("MAILNUM"), a.get("MAILSTREET"))
    mail_city = (a.get("MAILCITY") or "").strip()
    mail_state = (a.get("MAILSTATE") or "").strip()

    balance = a.get("DELINQUENT") or 0

    payload = {
        "county": "Mahoning",
        "state": "OH",
        "parcel": parcel,
        "parcel_display": a.get("PARCEL_ID"),
        "owner_full": full or owner,
        "owner_first": first,
        "owner_last": last,
        "owner_second": (a.get("OWNNAME2") or "").strip(),
        "is_entity": is_entity(owner),
        "site_address": site,
        "site_city": site_city,
        "site_zip": (a.get("LOCZIP") or "").strip(),
        "mail_address": mail_addr,
        "mail_city": mail_city,
        "mail_state": mail_state,
        "mail_zip": (a.get("MAILZIP") or "").strip(),
        "absentee": absentee(site_city, mail_city, mail_state),
        "delq_balance": balance,
        "delinquent_since_year": a.get("CERTDELQ_YEAR"),
        "last_sale_amount": a.get("SALEAMOUNT"),
        "market_value": a.get("TOTALMARKET"),
        "homestead": str(a.get("HOMESTEAD") or "").strip().upper() == "Y",
        "bor_flag": a.get("BORFLAG"),
        # No boolean foreclosure field on this layer -- a positive
        # delinquent balance is the signal itself, same situation as
        # Stark. Not the same thing as a filed foreclosure.
        "foreclosure": 0,
        "signal": "delinquent_balance",
    }
    yield {
        "parcel": parcel,
        "in_foreclosure": balance > 0,
        "delq_balance": balance,
        "payload": payload,
    }
