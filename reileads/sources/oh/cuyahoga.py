"""Cuyahoga County (Cleveland).

Source: the Fiscal Officer's own ArcGIS Enterprise server, layer 1 of the
Parcel Fabric (the non-spatial table -- faster than the polygon layer).
520,912 parcels, rebuilt DAILY. Verified: update_date on sampled rows
matched the day of checking.

This is county open data, not the Clerk of Courts docket. Cuyahoga's clerk
site forbids automated access and threatens permanent bans; this layer
carries no such restriction and gives us the same signal, because BOR
expedited tax foreclosures are reflected in foreclosure_flag.
"""
from ...core.arcgis import ArcGISLayer
from ...core.normalize import strip_parcel, split_owner, clean_addr, absentee, is_entity

URL = ("https://gis.cuyahogacounty.gov/server/rest/services/CCFO/"
       "Parcel_Fabric_Taxparcels/FeatureServer/1")

FIELDS = [
    "parcel_id", "parcel_owner", "second_owner",
    "par_addr_all", "parcel_city", "parcel_zip",
    "mail_name", "mail_addr_street", "mail_unit_no",
    "mail_city", "mail_state", "mail_zip",
    "total_net_delq_balance", "grand_total_balance",
    "prev_net_tax_total", "prev_tax_year",
    "foreclosure_flag", "bor_status", "cert_sold_flag", "cert_pend_flag",
    "payment_plan_flag", "last_transfer_date", "last_sales_amount",
    "myplaceppn", "taxbill_update_date",
]

# Pull anything in foreclosure OR carrying a delinquent balance, so the
# store can also see a parcel escalate before the flag flips.
WHERE = "foreclosure_flag = 1 OR total_net_delq_balance > 0"


class Cuyahoga:
    county = "cuyahoga"
    label = "Cuyahoga (Cleveland)"

    @staticmethod
    def fetch():
        layer = ArcGISLayer(URL, page_size=5000, oid_field="ObjectID")
        for a in layer.fetch_all(WHERE, FIELDS):
            parcel = strip_parcel(a.get("parcel_id"))
            if not parcel:
                continue
            owner = (a.get("parcel_owner") or "").strip()
            first, last, full = split_owner(owner)
            site_city = (a.get("parcel_city") or "").strip()
            mail_city = (a.get("mail_city") or "").strip()
            mail_state = (a.get("mail_state") or "").strip()

            payload = {
                "county": "Cuyahoga",
                "state": "OH",
                "parcel": parcel,
                "parcel_display": a.get("parcel_id"),
                "owner_full": full or owner,
                "owner_first": first,
                "owner_last": last,
                "owner_second": (a.get("second_owner") or "").strip(),
                "is_entity": is_entity(owner),
                "site_address": clean_addr(a.get("par_addr_all")),
                "site_city": site_city,
                "site_zip": a.get("parcel_zip"),
                "mail_name": a.get("mail_name"),
                "mail_address": clean_addr(a.get("mail_addr_street"), a.get("mail_unit_no")),
                "mail_city": mail_city,
                "mail_state": mail_state,
                "mail_zip": a.get("mail_zip"),
                "absentee": absentee(site_city, mail_city, mail_state),
                "delq_balance": a.get("total_net_delq_balance"),
                "total_balance": a.get("grand_total_balance"),
                "prior_year_tax": a.get("prev_net_tax_total"),
                "foreclosure": int(a.get("foreclosure_flag") or 0),
                "bor_status": a.get("bor_status"),
                "tax_cert_sold": int(a.get("cert_sold_flag") or 0),
                "tax_cert_pending": int(a.get("cert_pend_flag") or 0),
                "payment_plan": int(a.get("payment_plan_flag") or 0),
                "last_sale_amount": a.get("last_sales_amount"),
                "record_url": _myplace(a.get("myplaceppn"), a.get("parcel_id")),
            }
            yield {
                "parcel": parcel,
                "in_foreclosure": bool(a.get("foreclosure_flag")),
                "delq_balance": a.get("total_net_delq_balance"),
                "payload": payload,
            }


def _myplace(ppn, parcel_id):
    """MyPlace deep link. The county's own myplaceppn field is base64 of the
    bare parcel id; city=OTk= is base64 '99' meaning entire county."""
    import base64
    key = ppn or (base64.b64encode(str(parcel_id).encode()).decode() if parcel_id else "")
    if not key:
        return ""
    return f"https://myplace.cuyahogacounty.gov/{key}?city=OTk=&searchBy=UGFyY2Vs"
