"""Fulton County parcel enrichment.

Open ArcGIS MapServer, no auth, no terms restricting automated access --
confirmed distinct from GSCCCA, which this pipeline does not touch.

Gives us the piece a newspaper ad cannot: owner name, owner mailing
address (the address to actually contact), assessed value, and land use.

Homestead exemption: this layer has an `ExCode` field (exemption code),
which is a real, live field -- not something we're guessing exists. But
we have not been able to confirm what values it takes (H1/S1-style GA
homestead codes, something else, or blank-means-none) from this sandbox,
because the GA-hosted lookup pages that document it aren't reachable from
here. So `ExCode` is captured RAW below and `homestead_exemption` is left
None -- unknown -- rather than guessed. The fix is one afternoon once this
runs somewhere with real network access: pull ~20 parcels you know the
homestead status of by hand, see what ExCode says for each, and fill in
`_HOMESTEAD_CODES` below. Do not skip that step and assume "blank ExCode
= no homestead" -- that was exactly the class of bug (None treated as a
confirmed False) that got caught and fixed once already in classify.py.

Sale date / sale price / tenure length: not on this layer at all, and no
other Fulton open-data layer surfaced one either. `long_tenure` cannot be
computed for Fulton leads yet.
"""
from __future__ import annotations

import re
import logging

from ...core.arcgis import ArcGISLayer
from ...core.normalize import strip_parcel, split_owner, clean_addr, is_entity

log = logging.getLogger(__name__)

URL = ("https://gismaps.fultoncountyga.gov/arcgispub2/rest/services/"
       "PropertyMapViewer/PropertyMapViewer/MapServer/11")

FIELDS = [
    "ParcelID", "Address", "AddrNumber", "AddrPreDir", "AddrStreet",
    "AddrSuffix", "AddrPosDir", "AddrUntTyp", "AddrUnit",
    "Owner", "OwnerAddr1", "OwnerAddr2",
    "TotAssess", "TotAppr", "LUCode", "ClassCode", "LandAcres", "ExCode",
]

# Fill this in once you know what ExCode actually encodes (see module
# docstring). Example shape, once confirmed:
#   _HOMESTEAD_CODES = {"H1", "H2", "H3", "S1", "S3", "S4", "S5", "SC"}
# Left empty on purpose -- an empty set here means "we don't know," so
# _to_lead() below reports homestead_exemption=None for everyone rather
# than silently guessing every parcel has no homestead.
_HOMESTEAD_CODES: set[str] = set()

_STREET_NORM = re.compile(r"[^A-Z0-9]+")


def _norm_street(s: str) -> str:
    """Loose match key: strip punctuation/case/spacing so '1245 Peachtree
    Industrial Blvd NE' and '1245 PEACHTREE INDUSTRIAL BLVD NE' compare
    equal, and so do minor suffix-abbreviation differences."""
    return _STREET_NORM.sub("", (s or "").upper())


class FultonParcels:
    county = "fulton"
    label = "Fulton County parcel (owner/address enrichment)"

    def __init__(self):
        self._layer = ArcGISLayer(URL, page_size=2000, oid_field="OBJECTID")
        self._index: dict[str, dict] = {}
        self._loaded = False

    def _load(self):
        if self._loaded:
            return
        n = 0
        for a in self._layer.fetch_all("1=1", FIELDS):
            key = _norm_street(a.get("Address"))
            if key:
                self._index[key] = a
            n += 1
        log.info("Fulton parcel index loaded: %s parcels", f"{n:,}")
        self._loaded = True

    def lookup(self, site_address: str) -> dict | None:
        """Best-effort match by normalized street address.

        A newspaper ad's address string and the assessor's Address field
        are both free text from different systems, so this is a fuzzy
        join, not a key lookup. Returns None rather than a wrong parcel
        when nothing matches -- a wrong owner is worse than no owner.
        """
        self._load()
        key = _norm_street(site_address)
        if key in self._index:
            return self._to_lead(self._index[key])
        # tolerate a missing/extra post-directional (NE vs nothing)
        for suffix in ("NE", "NW", "SE", "SW", "N", "S", "E", "W"):
            if key.endswith(suffix) and key[: -len(suffix)] in self._index:
                return self._to_lead(self._index[key[: -len(suffix)]])
            alt = key + suffix
            if alt in self._index:
                return self._to_lead(self._index[alt])
        return None

    @staticmethod
    def _to_lead(a: dict) -> dict:
        owner = (a.get("Owner") or "").strip()
        first, last, full = split_owner(owner)
        ex_code = (a.get("ExCode") or "").strip().upper() or None
        if _HOMESTEAD_CODES:
            homestead = ex_code in _HOMESTEAD_CODES
        else:
            # _HOMESTEAD_CODES not filled in yet -- see module docstring.
            # Unknown, not False.
            homestead = None
        return {
            "parcel_display": a.get("ParcelID"),
            "parcel": strip_parcel(a.get("ParcelID")),
            "owner_full": full or owner,
            "owner_first": first,
            "owner_last": last,
            "is_entity": is_entity(owner),
            "entity_owner": is_entity(owner),
            "mail_address": clean_addr(a.get("OwnerAddr1")),
            "mail_address_2": clean_addr(a.get("OwnerAddr2")),
            "market_value": a.get("TotAppr"),
            "assessed_value": a.get("TotAssess"),
            "land_use_code": a.get("LUCode"),
            "property_class": a.get("ClassCode"),
            "exemption_code_raw": ex_code,
            "homestead_exemption": homestead,
        }
