"""Controlled release of the distressed-parcel backlog.

The daily pipelines only emit a lead when a parcel ENTERS distress, which
is correct for fresh signal but leaves everything that was already
distressed on day one permanently uncontacted -- 99,077 parcels as of
2026-09-14, against 19 events ever emitted. This module drips that
backlog out at a fixed rate per run, highest urgency score first.

What qualifies is decided by core/quality.py's vet(), the same gate the
Ohio and Georgia pipelines use -- same-owner-since-delinquency, vacant
land, street number, and the classifier. Scoring every lead is also what
makes "highest urgency first" here mean anything.

Backlog events use their own event name (backlog_tax_delinquent), so they
tag into REI Reply as signal-backlog-tax-delinquent and can be worked
with a different script than a fresh foreclosure -- these are older
situations, not someone who just got served.
"""
from __future__ import annotations

import json
import logging
import datetime as dt

from .core.store import Store
from .core import quality

log = logging.getLogger(__name__)

EVENT = "backlog_tax_delinquent"


def collect(store: Store, limit: int = 150, counties=None) -> tuple[list, dict]:
    """Return (top N qualifying leads, counts by reason rejected)."""
    sql = """SELECT p.county, p.parcel, p.payload
             FROM parcels p
             LEFT JOIN events e
               ON e.county = p.county AND e.parcel = p.parcel
             WHERE e.parcel IS NULL"""
    params = []
    if counties:
        sql += " AND p.county IN (%s)" % ",".join("?" * len(counties))
        params = list(counties)

    stats = {"scanned": 0, "no_owner_name": 0, "vacant_land": 0,
             "no_street_number": 0, "owner_changed": 0,
             "excluded": 0, "qualified": 0}
    scored = []

    for r in store.db.execute(sql, params):
        stats["scanned"] += 1
        p = json.loads(r["payload"])

        # No name means REI Reply rejects the contact outright (confirmed
        # 2026-09-14: HTTP 422, "Contacts without email, phone, firstName
        # and lastName are not allowed"). Summit is the whole county.
        ok, reason, p = quality.vet(p)
        if not ok:
            stats[{"classifier": "excluded"}.get(reason, reason)] += 1
            continue

        stats["qualified"] += 1
        scored.append((p["urgency_score"], r["county"], r["parcel"], p))

    # One lead per OWNER, not per parcel. Landlords and small investors
    # hold several delinquent parcels each -- six rows for one LLC is six
    # contacts REI Reply can't merge (no phone/email to dedupe on) and six
    # calls to the same person. The highest-scoring parcel represents the
    # owner; the rest stay in the backlog for a later run, and the count
    # rides along because "you're behind on six properties" is a stronger
    # opening than one address.
    best, counts, balances = {}, {}, {}
    for s, county, parcel, p in scored:
        key = (p.get("owner_full") or "").strip().upper() or f"{county}:{parcel}"
        counts[key] = counts.get(key, 0) + 1
        balances[key] = balances.get(key, 0) + float(p.get("delq_balance") or 0)
        if key not in best or s > best[key][0]:
            best[key] = (s, county, parcel, p)

    deduped = []
    for key, (s, county, parcel, p) in best.items():
        p["portfolio_count"] = counts[key]
        p["portfolio_delq_balance"] = round(balances[key], 2)
        deduped.append((s, county, parcel, p))

    stats["distinct_owners"] = len(deduped)
    deduped.sort(key=lambda t: (t[0], t[3].get("portfolio_count", 1)), reverse=True)
    return deduped[:limit], stats


def run(store: Store, limit: int = 150, counties=None, preview: bool = False) -> int:
    leads, stats = collect(store, limit=limit, counties=counties)

    log.info("backlog: %s scanned | rejected: %s no owner name, %s vacant land, "
             "%s no street number, %s sold since delinquency, %s classifier | "
             "%s qualified parcels across %s owners (%s released)",
             f"{stats['scanned']:,}", f"{stats['no_owner_name']:,}",
             f"{stats['vacant_land']:,}", f"{stats['no_street_number']:,}",
             f"{stats['owner_changed']:,}", f"{stats['excluded']:,}",
             f"{stats['qualified']:,}", f"{stats.get('distinct_owners', 0):,}",
             len(leads))

    if preview:
        for s, county, parcel, p in leads[:10]:
            log.info("  %-10s %-11s score=%-3s %-2s x%-2s $%-9s %-28s | %s",
                     county, parcel, s, p.get("tier"), p.get("portfolio_count"),
                     f"{p.get('portfolio_delq_balance') or 0:,.0f}",
                     (p.get("owner_full") or "")[:28],
                     (p.get("site_address") or "")[:38])
        return 0

    d = dt.date.today().isoformat()
    for s, county, parcel, p in leads:
        store.db.execute(
            "INSERT OR IGNORE INTO events (county,parcel,event,detected_on,payload) "
            "VALUES (?,?,?,?,?)",
            (county, parcel, EVENT, d, json.dumps(p, default=str)),
        )
    store.db.commit()
    store.log_run("backlog", stats["qualified"], len(leads), "ok")
    return len(leads)
