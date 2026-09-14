"""End-to-end GA test: mocked newspaper page + mocked Fulton parcel layer.
No network touched. Proves: address parse -> parcel join -> classify ->
exclude/keep -> store -> HighLevel contact mapping."""
import os, sys, tempfile, json
sys.path.insert(0, ".")
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
os.environ["OUT_DIR"] = tempfile.mkdtemp()
os.environ["DRY_RUN"] = "1"

from reileads.core import config
from reileads.core.store import Store
from reileads.core.reireply import to_contact
from reileads.sources.ga import legal_ads, fulton_parcel
from reileads import pipeline_ga

# ---- mock the newspaper: one hot foreclosure lead, one that should be
# excluded (recent owner-occupant purchase -- simulated by giving it a
# homestead exemption in the mocked parcel data and a very recent sale).
ARTICLE_HOT = """<html><body>NOTICE OF SALE UNDER POWER STATE OF GEORGIA
COUNTY OF FULTON Because of default in the payment of the indebtedness
secured by a Security Deed executed by Anthony Brooks to Acme Mortgage LLC,
will sell at public outcry to the highest bidder for cash before the
courthouse door of Fulton County, Georgia, within the legal hours of sale
on the first Tuesday in October, 2026, the following described property:
1245 Peachtree Industrial Blvd NE, Atlanta, GA 30309.</body></html>"""

ARTICLE_NEWOWNER = """<html><body>NOTICE OF SALE UNDER POWER STATE OF GEORGIA
COUNTY OF FULTON Because of default in the payment of the indebtedness
secured by a Security Deed executed by Riya Patel to Acme Mortgage LLC,
will sell at public outcry ... on the first Tuesday in October, 2026,
the following described property: 200 Marietta St NW, Atlanta, GA 30303.
</body></html>"""

class FakeResp:
    def __init__(self, text): self.text = text
    def raise_for_status(self): pass

class FakeSession:
    def __init__(self):
        self.calls = []
    def get(self, url, timeout=None, **kw):
        self.calls.append(url)
        # Article URLs are nested under the listing path, so check them
        # first -- "test-notices" alone would also match the listing page.
        if "hot" in url:
            return FakeResp(ARTICLE_HOT)
        if "newowner" in url:
            return FakeResp(ARTICLE_NEWOWNER)
        if "test-notices" in url:
            return FakeResp(
                '<a href="https://www.fultonneighbor.com/test-notices/'
                'notice-of-sale-hot.html">x</a>'
                '<a href="https://www.fultonneighbor.com/test-notices/'
                'notice-of-sale-newowner.html">x</a>'
            )
        raise AssertionError("unexpected url " + url)

legal_ads.LegalAdsClient.__init__ = lambda self: setattr(self, "s", FakeSession())

# Fulton's real notice-archive path is unresolved (see legal_ads.py's module
# docstring -- list_path is None so real runs skip it with a warning rather
# than guess). This test isn't about Fulton's archive page, it's about the
# address-parse -> parcel-join -> classify chain once notices arrive, so it
# patches in a placeholder path to keep exercising that chain.
legal_ads.SITES["fulton"] = {**legal_ads.SITES["fulton"], "list_path": "/test-notices/"}

# pipeline_ga.collect() no longer calls the real newspaper scraper by
# default (see pipeline_ga.py's module docstring -- disabled 2026-09-10,
# every GA paper's ToS bars automated access). This test still exists to
# prove the address-parse -> parcel-join -> classify -> store chain works,
# so it patches the disabled call site back to the real (mocked-HTTP)
# fetch for this test run only. Production stays disabled.
pipeline_ga._no_lawful_ga_source = legal_ads.LegalAdsForeclosure.fetch

# ---- mock Fulton parcel data: the hot lead is a long-time absentee owner;
# the "new owner" lead has a recent purchase + homestead (should exclude).
class FakeFultonParcels(fulton_parcel.FultonParcels):
    def __init__(self):
        self._loaded = True
        self._index = {
            fulton_parcel._norm_street("1245 Peachtree Industrial Blvd"): {
                "ParcelID": "14-0210-0003-045-2", "Owner": "BROOKS, ANTHONY",
                "Address": "1245 PEACHTREE INDUSTRIAL BLVD",
                "OwnerAddr1": "8800 SUNSET BLVD", "OwnerAddr2": "LOS ANGELES CA 90069",
                "TotAppr": 298000, "TotAssess": 119200, "LUCode": "101", "ClassCode": "R3",
            },
            fulton_parcel._norm_street("200 Marietta St"): {
                "ParcelID": "14-0099-0001-010-1", "Owner": "PATEL, RIYA",
                "Address": "200 MARIETTA ST", "OwnerAddr1": "200 MARIETTA ST",
                "OwnerAddr2": "ATLANTA GA 30303", "TotAppr": 410000, "TotAssess": 164000,
                "LUCode": "101", "ClassCode": "R3",
            },
        }

pipeline_ga.ENRICHERS["Fulton"] = FakeFultonParcels

# The parcel layer alone can't tell us "recent purchase + homestead" --
# in the real pipeline that comes from a second enrichment (deed history /
# assessor exemption file) we haven't built yet. Simulate it landing on
# the lead dict the way that future source will, to prove the classifier
# reacts to it correctly even though this specific enrichment is stubbed.
_orig_lookup = FakeFultonParcels.lookup
def patched_lookup(self, addr):
    hit = _orig_lookup(self, addr)
    if hit and hit["owner_full"] == "PATEL, RIYA":   # split_owner keeps source casing
        hit["homestead_exemption"] = True
        hit["last_sale_date"] = __import__("datetime").date.today().isoformat()
        hit["has_purchase_money_security_deed"] = True
    return hit
FakeFultonParcels.lookup = patched_lookup

# ---------------------------------------------------------------- run it
store = Store(config.DB_PATH)
n = pipeline_ga.run(store, counties=["fulton"])
print(f"new events this run: {n}  (expect 1 -- the recent-owner one must be excluded)")

rows = list(store.db.execute("SELECT parcel, payload FROM events WHERE county='ga'"))
assert len(rows) == 1, f"expected exactly 1 kept lead, got {len(rows)}"
payload = json.loads(rows[0]["payload"])
print(json.dumps({k: payload[k] for k in
    ("county","site_address","owner_full","mail_address","urgency_score","tier","persona","signals")
    if k in payload}, indent=2))

assert payload["owner_full"] == "BROOKS, ANTHONY", "wrong owner joined"
assert "LOS ANGELES" in payload["mail_address_2"], "absentee mailing address not carried through"
assert payload["tier"] in ("A", "B"), f"expected a hot tier, got {payload['tier']}"
print("\nHighLevel contact payload:")
ev = {"county": "Fulton", "parcel": rows[0]["parcel"], "event": "ga_notice",
      "detected_on": "2026-09-10", "payload": payload}
print(json.dumps(to_contact(ev, "LOC_GA"), indent=2))

print("\nRe-running to confirm idempotency (no double-push)...")
n2 = pipeline_ga.run(store, counties=["fulton"])
assert n2 == 0, f"expected 0 new events on rerun, got {n2}"
print("OK: 0 new events on identical rerun")
print("\nALL GA E2E CHECKS PASSED")
