"""Skip trace pending leads before any of them becomes a CRM contact.

Order of operations matters here and is deliberate: trace first, push
second. A contact created without a phone can't be dialled, and HighLevel
deduplicates on phone or email, so an untraced contact is also a contact
that will silently duplicate later when the same owner turns up again.

Spending is bounded three ways, because the plan's 10,000 monthly credits
are a hard ceiling Sharon doesn't intend to raise:

  1. `skiptraces` records every attempt by (county, parcel), so a rerun
     never pays twice for the same parcel -- DealMachine deduplicates
     within a billing period but not across one.
  2. A configured monthly cap stops the run short of the plan limit.
  3. Leads are traced highest urgency first, so a cap that does bite
     costs the weakest leads rather than an arbitrary slice.
"""
from __future__ import annotations

import datetime as dt
import json
import logging

from .core import config
from .core.store import Store
from .core.dealmachine import DealMachine, best_phone, best_email, all_phones

log = logging.getLogger(__name__)

BATCH = 25


def credits_used_this_month(store: Store) -> int:
    first = dt.date.today().replace(day=1).isoformat()
    row = store.db.execute(
        "SELECT COALESCE(SUM(credits),0) c FROM skiptraces WHERE traced_at >= ?",
        (first,),
    ).fetchone()
    return int(row["c"])


def pending(store: Store, limit: int, event_like: str = None) -> list:
    """Unpushed events with no trace on record, best leads first."""
    sql = """SELECT e.county, e.parcel, e.event, e.payload
             FROM events e
             LEFT JOIN pushes p
               ON p.county=e.county AND p.parcel=e.parcel AND p.event=e.event
             LEFT JOIN skiptraces s
               ON s.county=e.county AND s.parcel=e.parcel
             WHERE p.parcel IS NULL AND s.parcel IS NULL"""
    params = []
    if event_like:
        sql += " AND e.event LIKE ?"
        params.append(event_like)
    rows = [dict(r) for r in store.db.execute(sql, params)]
    for r in rows:
        r["payload"] = json.loads(r["payload"])
    rows.sort(key=lambda r: r["payload"].get("urgency_score") or 0, reverse=True)
    return rows[:limit]


_US_STATES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN",
    "IA","KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV",
    "NH","NJ","NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN",
    "TX","UT","VT","VA","WA","WV","WI","WY","DC",
}


def _to_query(p: dict) -> dict:
    """What we hand DealMachine: a name, narrowed by where they get mail.

    The owner's MAILING ZIP is used, not the property's -- for an heir who
    inherited a house in another city, or an absentee landlord, the
    property is precisely where they don't live, so searching there finds
    strangers. Falls back to the property's ZIP only when no mailing
    address was published.

    Entity owners are skipped upstream: a name lookup for "PALM PROPERTY
    MANAGEMENT LLC" searches a person index for a company and returns
    nothing worth a credit.
    """
    # Garbage in either field 400s the whole request rather than just
    # matching nothing -- confirmed live 2026-09-15 with mail_state="AB",
    # a fragment of a mis-parsed county address field, not a real state.
    # Validated here rather than trusted from any county source.
    state = (p.get("mail_state") or p.get("state") or "").strip().upper()
    zipc = str(p.get("mail_zip") or p.get("site_zip") or "").strip()
    return {
        "first_name": (p.get("owner_first") or "").strip(),
        "last_name": (p.get("owner_last") or "").strip(),
        "state": state if state in _US_STATES else "",
        "zip": zipc if zipc[:5].isdigit() else "",
    }


def run(store: Store, limit: int = 100, event_like: str = None,
        trial: bool = False) -> dict:
    """Trace up to `limit` pending leads. Returns a summary dict.

    trial=True traces for real (there is no sandbox that returns real
    phone numbers) but writes nothing back to the events, so a small
    batch can be judged on match rate and credit cost before the whole
    pipeline depends on it.
    """
    used = credits_used_this_month(store)
    room = config.DEALMACHINE_MONTHLY_CREDIT_CAP - used
    out = {"attempted": 0, "matched": 0, "phones": 0, "emails": 0, "dnc": 0,
           "credits": 0, "used_before": used, "cap": config.DEALMACHINE_MONTHLY_CREDIT_CAP}

    if room <= 0:
        log.warning("skip trace: monthly cap reached (%s/%s credits) -- not tracing",
                    used, config.DEALMACHINE_MONTHLY_CREDIT_CAP)
        return out

    leads = pending(store, min(limit, room), event_like=event_like)
    if not leads:
        log.info("skip trace: nothing pending")
        return out

    dm = DealMachine()
    today = dt.date.today().isoformat()

    for lead in leads:
        p = lead["payload"]

        # Never spend a credit looking for a company in a person index.
        # "PALM PROPERTY MANAGEMENT LLC" has no date of birth and no
        # mobile; the lookup either returns nothing or returns a stranger
        # who happens to share a word with the company name. Entities are
        # ~14% of the bank, so this is real money.
        if p.get("is_entity"):
            out["skipped_entity"] = out.get("skipped_entity", 0) + 1
            continue

        q = _to_query(p)
        if not q["last_name"]:
            out["skipped_no_name"] = out.get("skipped_no_name", 0) + 1
            continue
        try:
            # Real lookup even on a trial: estimate_cost returns a price
            # preview with no people in it, so it can price a batch but
            # can't tell us the one thing a trial is for -- how often we
            # actually get a number. There is no sandbox that returns
            # real phones, so a trial costs real credits; `trial` only
            # stops the result being written back.
            people, credits = dm.enrich_name(
                last_name=q["last_name"], first_name=q["first_name"],
                zip_code=q["zip"], state=q["state"])
        except Exception as e:
            # One bad record must never cost the rest of the batch --
            # confirmed live 2026-09-15: a single mis-parsed entity
            # (Youngstown Choice Homes, no corporate suffix in the
            # county's field, split into first="Choice" last="Youngstown"
            # with a garbage state code) 400'd, `break` fired, and the
            # other 24 good leads in that morning's batch were never
            # even attempted. Log it, count it, keep going.
            log.error("skip trace failed for %s/%s (%s): %s -- skipping, "
                      "continuing batch", lead["county"], lead["parcel"],
                      p.get("owner_full"), e)
            out["errors"] = out.get("errors", 0) + 1
            continue

        out["credits"] += credits
        res = people[0] if people else {}
        out["attempted"] += 1
        phone, ptype, dnc = best_phone(res)
        email = best_email(res)
        matched = bool(phone or email)
        out["matched"] += matched
        out["phones"] += bool(phone)
        out["emails"] += bool(email)
        out["dnc"] += bool(dnc)

        if trial:
            log.info("  %-9s %-11s %-22s zip=%-5s -> %s matches, %s %s%s",
                     lead["county"], lead["parcel"],
                     (p.get("owner_full") or "")[:22], q["zip"] or "-",
                     len(people), phone or "(no phone)", ptype,
                     " DNC" if dnc else "")
            continue

        store.db.execute(
            """INSERT OR REPLACE INTO skiptraces
               (county,parcel,traced_at,status,phone,phone_type,do_not_call,email,credits)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (lead["county"], lead["parcel"], today,
             "matched" if matched else "no_match",
             phone, ptype, int(dnc), email, credits),
        )
        p["phone"] = phone
        p["phone_type"] = ptype
        p["do_not_call"] = dnc
        p["email"] = email
        # Every number found, not just the chosen one -- so the caller can
        # see there was a second option rather than trusting our ranking,
        # and can see which ones are DNC and why they weren't picked.
        p["phones_all"] = "; ".join(
            f"{x.get('number')} ({x.get('type')}{', DNC' if x.get('do_not_call') else ''})"
            for x in all_phones(res))
        alts = all_phones(res)[1:2]
        p["phone_alt"] = str(alts[0].get("number")) if alts else ""
        store.db.execute(
            "UPDATE events SET payload=? WHERE county=? AND parcel=? AND event=?",
            (json.dumps(p, default=str), lead["county"], lead["parcel"], lead["event"]),
        )
        store.db.commit()

    log.info("skip trace%s: %s attempted, %s matched (%s phones, %s emails, %s DNC), "
             "%s credits -- month now %s/%s",
             " TRIAL" if trial else "", out["attempted"], out["matched"],
             out["phones"], out["emails"], out["dnc"], out["credits"],
             used + out["credits"], config.DEALMACHINE_MONTHLY_CREDIT_CAP)
    return out
