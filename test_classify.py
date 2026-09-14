"""Classifier tests. These encode Sharon's actual filter rules, so if one
of these ever fails the pipeline is sending her leads she said she does
not want."""
import sys, datetime as dt
sys.path.insert(0, ".")
from reileads.core.classify import score, tier, exclude

def d(months_ago):
    return (dt.date.today() - dt.timedelta(days=int(months_ago*30.44))).isoformat()

CASES = [
 # ---- the ones she explicitly does NOT want -------------------------
 ("Family bought 8 months ago with a mortgage, lives there", dict(
    owner_full="NGUYEN, DAVID", last_sale_date=d(8), last_sale_price=425000,
    has_purchase_money_security_deed=True, homestead_exemption=True,
    site_city="ALPHARETTA", mail_city="ALPHARETTA", mail_state="GA"), "EXCLUDE"),

 ("Bought 3 months ago, homestead, no distress at all", dict(
    owner_full="PATEL, RIYA", last_sale_date=d(3), homestead_exemption=True,
    site_city="MARIETTA", mail_city="MARIETTA", mail_state="GA"), "EXCLUDE"),

 ("City of Atlanta owns it", dict(
    owner_full="CITY OF ATLANTA", tax_delinquent_years=4), "EXCLUDE"),

 ("Fulton County Land Bank", dict(
    owner_full="FULTON COUNTY LAND BANK AUTHORITY", vacant=True), "EXCLUDE"),

 ("Long-time owner, no distress signal whatsoever", dict(
    owner_full="HARRIS, JEAN", last_sale_date=d(240), homestead_exemption=True,
    site_city="DECATUR", mail_city="DECATUR", mail_state="GA"), "EXCLUDE"),

 # ---- the ones she DOES want ----------------------------------------
 ("Foreclosure sale scheduled, absentee owner", dict(
    owner_full="BROOKS, ANTHONY", foreclosure_sale_date="2026-10-06",
    assignment_date=d(2), site_city="EAST POINT", mail_city="LAS VEGAS",
    mail_state="NV", homestead_exemption=False, has_open_security_deed=True,
    last_sale_date=d(96)), "KEEP"),

 ("Heir took title by Year's Support, lives out of state", dict(
    owner_full="WILLIAMS, DORIS", years_support=True, site_city="ATLANTA",
    mail_city="CHARLOTTE", mail_state="NC", homestead_exemption=False,
    has_open_security_deed=False, last_sale_date=d(4)), "KEEP"),

 ("Three years tax delinquent, vacant, no mortgage", dict(
    owner_full="COLEMAN, RUTH", tax_delinquent_years=3.0,
    tax_delinquent_amount=14200, vacant=True, has_open_security_deed=False,
    homestead_exemption=False, last_sale_date=d(300),
    site_city="ATLANTA", mail_city="ATLANTA", mail_state="GA"), "KEEP"),

 ("Investor LLC, bought CASH 6 months ago -- recent but wanted", dict(
    owner_full="BLUE RIDGE HOLDINGS LLC", is_entity=True, entity_owner=True,
    last_sale_date=d(6), has_purchase_money_security_deed=False,
    homestead_exemption=False, contractor_lien=True,
    site_city="SMYRNA", mail_city="ATLANTA", mail_state="GA"), "KEEP"),

 ("Contractor lien, stalled flip", dict(
    owner_full="REYES, MIGUEL", contractor_lien=True, vacant=True,
    homestead_exemption=False, site_city="AUSTELL", mail_city="AUSTELL",
    mail_state="GA", last_sale_date=d(20)), "KEEP"),

 ("Divorce lis pendens, long tenure, free and clear", dict(
    owner_full="SUTTON, KAREN", lis_pendens=True, last_sale_date=d(230),
    has_open_security_deed=False, homestead_exemption=True,
    site_city="ROSWELL", mail_city="ROSWELL", mail_state="GA"), "KEEP"),
]

print(f"{'':2} {'CASE':<52} {'EXPECT':<8} {'GOT':<8} {'SCORE':>5} {'TIER':<5} PERSONA / REASON")
print("-"*138)
fails = 0
for label, lead, expect in CASES:
    v = score(lead)
    got = "EXCLUDE" if v.excluded else "KEEP"
    ok = got == expect
    fails += (not ok)
    detail = v.reason if v.excluded else "; ".join(v.signals[:3])
    print(f"{'ok' if ok else 'XX':2} {label:<52} {expect:<8} {got:<8} "
          f"{v.score:>5} {tier(v.score) if not v.excluded else '-':<5} {v.persona} | {detail}")

print("-"*138)
print(f"{len(CASES)-fails}/{len(CASES)} passed")

print("\nRanking of the kept leads, highest urgency first:")
kept = [(score(l), n) for n, l, e in CASES if not score(l).excluded]
for v, n in sorted(kept, key=lambda x: -x[0].score):
    print(f"  {v.score:>3} [{tier(v.score)}] {n}")
sys.exit(1 if fails else 0)
