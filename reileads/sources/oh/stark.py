"""Stark County (Canton).

Source: the Auditor's own parcel layer, federated through Esri's
utility.arcgis.com proxy (the county's direct host,
scgisa.starkcountyohio.gov, timed out from this sandbox -- the proxy
serves the identical live data, confirmed 2026-09-10: 17,932 of 202,677
parcels carry a positive FINAL_BALANCE).

No boolean foreclosure flag on this layer -- FINAL_BALANCE > 0 (an unpaid
certified-delinquent balance) is the distress signal here, same role
DELQ_TAXES plays for Hamilton. A sibling layer, Auditor/HistoricForeclosures,
exists on the same server and has not been field-checked -- if Stark's
volume turns out too thin on the delinquency signal alone, that's the
next thing to look at, not a reason to guess at its shape now.
"""
from ...core.arcgis import ArcGISLayer
from ...core.normalize import strip_parcel, split_owner, clean_addr, absentee, is_entity

URL = ("https://utility.arcgis.com/usrsvcs/servers/"
       "067a37ee416e4d11bc23dd1446ad30ba/rest/services/"
       "Auditor/StarkCountyParcels/FeatureServer/0")

FIELDS = [
    "PIN", "OWNER", "SITE_ADDRESS", "MAILING_NAME",
    "MAILING_ADDRESS1", "MAILING_ADDRESS2", "MAILING_ADDRESS3",
    "FINAL_BALANCE", "TOTAL_BILLED", "TOTAL_PAID",
    "CERTIFIED_DELINQUENT_YEAR", "CERTIFIED_DELINQUENT_DATE",
    "MOST_RECENT_SALE_DATE", "MOST_RECENT_SALE_PRICE",
    "APPRAISED_TOTAL_VALUE", "HOMESTEAD_EXEMPTION", "LAND_USE_DESCRIPTION",
]

WHERE = "FINAL_BALANCE > 0"


def _yn(v) -> bool:
    return str(v or "").strip().upper() in ("Y", "YES", "TRUE", "1")


class Stark:
    county = "stark"
    label = "Stark (Canton)"

    @staticmethod
    def fetch():
        layer = ArcGISLayer(URL, page_size=2000, oid_field="OBJECTID")
        for a in layer.fetch_all(WHERE, FIELDS):
            parcel = strip_parcel(a.get("PIN"))
            if not parcel:
                continue
            owner = (a.get("OWNER") or "").strip()
            first, last, full = split_owner(owner)

            # SITE_ADDRESS on this layer is one free-text field (number,
            # street, city, state, zip all run together) -- no city/state
            # split available the way Cuyahoga/Hamilton have.
            site = clean_addr(a.get("SITE_ADDRESS"))

            mail_addr = clean_addr(a.get("MAILING_ADDRESS1"), a.get("MAILING_ADDRESS2"))
            # MAILING_ADDRESS3 is usually "CITY STATE ZIP" together; split
            # loosely rather than guess a fixed format.
            import re
            m3 = (a.get("MAILING_ADDRESS3") or "").strip()
            mm = re.match(r"^(.*?)[,\s]+([A-Z]{2})\s+(\d{5})", m3)
            mail_city, mail_state, mail_zip = (mm.groups() if mm else (m3, "", ""))

            balance = a.get("FINAL_BALANCE") or 0

            payload = {
                "county": "Stark",
                "state": "OH",
                "parcel": parcel,
                "parcel_display": a.get("PIN"),
                "owner_full": full or owner,
                "owner_first": first,
                "owner_last": last,
                "is_entity": is_entity(owner),
                "site_address": site,
                "site_city": "",
                "site_zip": "",
                "mail_name": a.get("MAILING_NAME"),
                "mail_address": mail_addr,
                "mail_city": mail_city.strip(),
                "mail_state": mail_state,
                "mail_zip": mail_zip,
                "absentee": absentee(site, mail_city.strip(), mail_state),
                "delq_balance": balance,
                "total_billed": a.get("TOTAL_BILLED"),
                "total_paid": a.get("TOTAL_PAID"),
                "delinquent_since_year": a.get("CERTIFIED_DELINQUENT_YEAR"),
                "last_sale_amount": a.get("MOST_RECENT_SALE_PRICE"),
                "market_value": a.get("APPRAISED_TOTAL_VALUE"),
                "homestead": _yn(a.get("HOMESTEAD_EXEMPTION")),
                "land_use": a.get("LAND_USE_DESCRIPTION"),
                # No foreclosure flag on this layer -- a positive balance
                # is the signal itself, same role DELQ_TAXES plays for
                # Hamilton. Not the same thing as a filed foreclosure.
                "foreclosure": 0,
                "signal": "delinquent_balance",
                "record_url": f"https://reweb.starkcountyohio.gov/Datalets/PrintDatalet.aspx?pin={parcel}",
            }
            yield {
                "parcel": parcel,
                "in_foreclosure": balance > 0,
                "delq_balance": balance,
                "payload": payload,
            }
