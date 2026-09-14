"""Summit County (Akron).

Source: a purpose-built ArcGIS-hosted foreclosure feed (confirmed live
2026-09-10, real addresses returned). Unlike Cuyahoga/Hamilton/Stark this
is not a general parcel layer with a distress column added on -- its only
job is listing foreclosure-adjacent parcels, so every row here already
qualifies; there's no flag or balance threshold to filter on.

Real gap, not an oversight: this layer has no owner-name field at all
(confirmed via its own ?f=json metadata -- 10 fields total, none of them
an owner). `docname`, `recorddt`, `transdt`, `liber`, `page` were also
empty on every sampled row, so whatever transfer-document data this layer
is meant to carry isn't populated yet, or isn't populated for these rows.
A Summit County Auditor general parcel layer likely exists to join owner
name in by parcel id, but wasn't found reachable in a quick search
2026-09-10 -- worth another look, not worth guessing a URL for.

Until that's found, Summit leads carry an address and a parcel id but no
owner name -- the classifier's derive()/persona() already treat a missing
owner as unknown rather than excluding the lead outright.
"""
from ...core.arcgis import ArcGISLayer
from ...core.normalize import strip_parcel, clean_addr

URL = ("https://services3.arcgis.com/3Ukh5HzAdI6WZ3KP/arcgis/rest/services/"
       "TaxParcelForeclosures__dashboard/FeatureServer/0")

FIELDS = ["parcelid", "fulladdr", "cvttxcd", "docname", "recorddt", "transdt",
          "liber", "page", "saleamnt"]


class Summit:
    county = "summit"
    label = "Summit (Akron)"

    @staticmethod
    def fetch():
        layer = ArcGISLayer(URL, page_size=2000, oid_field="OBJECTID")
        for a in layer.fetch_all("1=1", FIELDS):
            parcel = strip_parcel(a.get("parcelid"))
            if not parcel:
                continue
            payload = {
                "county": "Summit",
                "state": "OH",
                "parcel": parcel,
                "parcel_display": a.get("parcelid"),
                # No owner field on this layer -- see module docstring.
                # None, not "", so derive()/persona() read it as unknown.
                "owner_full": None,
                "owner_first": None,
                "owner_last": None,
                "site_address": clean_addr(a.get("fulladdr")),
                "site_city": "",
                "site_zip": "",
                "conveyance_tax_code": a.get("cvttxcd"),
                "transfer_doc_name": a.get("docname"),
                "recorded_date": a.get("recorddt"),
                "transfer_date": a.get("transdt"),
                "deed_book": a.get("liber"),
                "deed_page": a.get("page"),
                "last_sale_amount": a.get("saleamnt"),
                # Every row on this layer is already foreclosure-adjacent
                # by construction -- there's no separate flag to read.
                "foreclosure": 1,
                "signal": "county_foreclosure_list",
            }
            yield {
                "parcel": parcel,
                "in_foreclosure": True,
                "delq_balance": None,
                "payload": payload,
            }
