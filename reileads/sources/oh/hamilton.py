"""Hamilton County (Cincinnati).

Source: CAGIS open data parcel polygons, 420,219 parcels. Carries
FORECL_FLAG, DELQ_TAXES, DLNQDT (delinquency date) and a contract/payment
plan flag, plus owner and separate mailing address.

Cadence is not published by the service. Diff DLNQDT and DELQ_TAXES over the
first two weeks to establish the real refresh interval before you promise
anyone a same-day feed.
"""
from ...core.arcgis import ArcGISLayer
from ...core.normalize import strip_parcel, split_owner, clean_addr, absentee, is_entity

URL = ("https://services.arcgis.com/JyZag7oO4NteHGiq/arcgis/rest/services/"
       "Open_Data_Feature_Collection/FeatureServer/0")

FIELDS = [
    "PARCELID", "AUDPCLID", "OWNNM1", "OWNNM2",
    "ADDRNO", "ADDRST", "ADDRSF", "LOC_ST_DIR",
    "MLNM1", "MLADR1", "MLADR2", "OWNADCITY", "OWNADSTATE", "OWNADZIP",
    "DELQ_TAXES", "DELQ_TAXES_PD", "DLNQDT", "FORECL_FLAG",
    "CNTFLG", "ANNUAL_TAXES", "TAXES_PAID",
    "MKT_TOTAL_VAL", "CLASS", "SALDAT", "SALAMT",
    "HMSD_FLAG", "RENT_REG_FLAG", "BOR_FLAG",
    "PAR_DELETED", "CURYR_FLAG",
]

# FORECL_FLAG is a string field in this layer, so compare as text.
# Exclude deleted parcels or you will chase records that no longer exist.
WHERE = ("(FORECL_FLAG IS NOT NULL AND FORECL_FLAG <> '' AND FORECL_FLAG <> 'N') "
         "OR DELQ_TAXES > 0")

_TRUE = {"Y", "YES", "T", "TRUE", "1"}


def _flag(v) -> bool:
    if v is None:
        return False
    return str(v).strip().upper() in _TRUE


class Hamilton:
    county = "hamilton"
    label = "Hamilton (Cincinnati)"

    @staticmethod
    def fetch():
        layer = ArcGISLayer(URL, page_size=2000, oid_field="OBJECTID")
        for a in layer.fetch_all(WHERE, FIELDS):
            if _flag(a.get("PAR_DELETED")):
                continue
            parcel = strip_parcel(a.get("PARCELID") or a.get("AUDPCLID"))
            if not parcel:
                continue
            owner = (a.get("OWNNM1") or "").strip()
            first, last, full = split_owner(owner)
            mail_city = (a.get("OWNADCITY") or "").strip()
            mail_state = (a.get("OWNADSTATE") or "").strip()
            site = clean_addr(a.get("ADDRNO"), a.get("LOC_ST_DIR"),
                              a.get("ADDRST"), a.get("ADDRSF"))

            delq = a.get("DELQ_TAXES") or 0
            paid = a.get("DELQ_TAXES_PD") or 0
            net = round(float(delq) - float(paid), 2)

            payload = {
                "county": "Hamilton",
                "state": "OH",
                "parcel": parcel,
                "parcel_display": a.get("PARCELID"),
                "owner_full": full or owner,
                "owner_first": first,
                "owner_last": last,
                "owner_second": (a.get("OWNNM2") or "").strip(),
                "is_entity": is_entity(owner),
                "site_address": site,
                "site_city": "",  # CAGIS carries no situs city on this layer
                "site_zip": "",
                "mail_name": a.get("MLNM1"),
                "mail_address": clean_addr(a.get("MLADR1"), a.get("MLADR2")),
                "mail_city": mail_city,
                "mail_state": mail_state,
                "mail_zip": a.get("OWNADZIP"),
                "absentee": absentee("", mail_city, mail_state),
                "delq_balance": net,
                "delinquent_since": a.get("DLNQDT"),
                "annual_taxes": a.get("ANNUAL_TAXES"),
                "market_value": a.get("MKT_TOTAL_VAL"),
                "foreclosure": 1 if _flag(a.get("FORECL_FLAG")) else 0,
                "payment_plan": 1 if _flag(a.get("CNTFLG")) else 0,
                "homestead": 1 if _flag(a.get("HMSD_FLAG")) else 0,
                "rental_registered": 1 if _flag(a.get("RENT_REG_FLAG")) else 0,
                "bor_flag": a.get("BOR_FLAG"),
                "last_sale_amount": a.get("SALAMT"),
                "property_class": a.get("CLASS"),
                "record_url": f"https://wedge1.hcauditor.org/view/re/{parcel}/2025/payment_details",
            }
            yield {
                "parcel": parcel,
                "in_foreclosure": _flag(a.get("FORECL_FLAG")),
                "delq_balance": net,
                "payload": payload,
            }
