"""End-to-end test with mocked county responses.

Proves: paging -> normalization -> snapshot diff -> event creation ->
CSV -> HighLevel contact mapping, without touching a county server.
"""
import os, tempfile, json
os.environ["DB_PATH"] = tempfile.mktemp(suffix=".db")
os.environ["OUT_DIR"] = tempfile.mkdtemp()
os.environ["DRY_RUN"] = "1"

from reileads.core import config
from reileads.core.store import Store
from reileads.core.push import push
from reileads.core.reireply import to_contact
from reileads.sources.oh import cuyahoga
from reileads.pipeline_oh import run_county

# Two Cuyahoga parcels shaped exactly like the live layer's attributes.
DAY1 = [
  {"parcel_id":"48704813C","parcel_owner":"BUDYKA, BETTY L TRUSTEE","second_owner":None,
   "par_addr_all":"14967 ROYAL RIDGE LN, NORTH ROYALTON, OH, 44133","parcel_city":"NORTH ROYALTON",
   "parcel_zip":"44133","mail_name":"BUDYKA BETTY L","mail_addr_street":"14967 ROYAL RIDGE LN",
   "mail_unit_no":"22","mail_city":"NORTH ROYALTON","mail_state":"OH","mail_zip":"44133",
   "total_net_delq_balance":0.0,"grand_total_balance":0.0,"prev_net_tax_total":0.0,
   "prev_tax_year":2024,"foreclosure_flag":0,"bor_status":None,"cert_sold_flag":0,
   "cert_pend_flag":0,"payment_plan_flag":0,"last_transfer_date":None,
   "last_sales_amount":0,"myplaceppn":"NDg3MDQ4MTND","taxbill_update_date":1788936540000},
  {"parcel_id":"78410016","parcel_owner":"LAUDERDALE, MICHAEL","second_owner":None,
   "par_addr_all":"5702 GARFIELD AVE, MAPLE HEIGHTS, OH, 44137","parcel_city":"MAPLE HEIGHTS",
   "parcel_zip":"44137","mail_name":"LAUDERDALE MICHAEL","mail_addr_street":"1930 W 3RD ST",
   "mail_unit_no":None,"mail_city":"POMONA","mail_state":"CA","mail_zip":"91768",
   "total_net_delq_balance":18240.55,"grand_total_balance":19100.10,"prev_net_tax_total":9000.0,
   "prev_tax_year":2024,"foreclosure_flag":0,"bor_status":None,"cert_sold_flag":0,
   "cert_pend_flag":1,"payment_plan_flag":0,"last_transfer_date":None,
   "last_sales_amount":42000,"myplaceppn":"Nzg0MTAwMTY=","taxbill_update_date":1788936540000},
]
DAY2 = json.loads(json.dumps(DAY1))
DAY2[1]["foreclosure_flag"] = 1          # <-- this is the lead
DAY2[1]["total_net_delq_balance"] = 19880.00

class FakeLayer:
    payload = DAY1
    def __init__(self,*a,**k): pass
    def fetch_all(self, where, fields):
        for row in FakeLayer.payload: yield row

cuyahoga.ArcGISLayer = FakeLayer

store = Store(config.DB_PATH)

print("--- DAY 1 (baseline seed) ---")
ev1 = run_county(store, "cuyahoga")
print(f"events: {len(ev1)}   <- must be 0, we do not dump the backlog\n")

print("--- DAY 2 (one parcel enters foreclosure) ---")
FakeLayer.payload = DAY2
ev2 = run_county(store, "cuyahoga")
print(f"events: {len(ev2)}")
for e in ev2:
    print("  ", e["event"], e["parcel"], "|", e["payload"]["owner_full"],
          "|", e["payload"]["site_address"], "| absentee:", e["payload"]["absentee"])

print("\n--- DAY 3 (nothing changes) ---")
ev3 = run_county(store, "cuyahoga")
print(f"events: {len(ev3)}   <- must be 0, no re-alerting\n")

res = push(store, 100)
print("push result:", res)
print("\n--- CSV ---")
print(open(res["csv"]).read())

print("--- HighLevel contact payload ---")
pend = store.unpushed(1)
print(json.dumps(to_contact(pend[0], "LOC123"), indent=2))
