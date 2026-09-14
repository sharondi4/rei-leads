"""Insert DealMachine-sourced leads into the shared events table.

Same insert-and-dedupe shape as pipeline_ga.run()/run_probate() -- reuses
the shared `events` table so core/push.py doesn't know or care this
source exists, and reuses store.unpushed()'s existing-push check so a
person found again tomorrow doesn't get pushed twice.

Skips the classifier and skiptrace.py entirely: DealMachine's own filters
(tax delinquent / preforeclosure / vacant, ANDed with absentee + equity)
already select the "primary signal" this lead needs, and the phone comes
back in the same call, so there's nothing left for either stage to add.
event='dealmachine_hot' keeps this distinguishable from every
county-sourced signal in REI Reply.
"""
from __future__ import annotations

import datetime as dt
import json
import logging

from .core.store import Store
from .dealmachine_source import search_hot, to_event_payload, METROS

log = logging.getLogger(__name__)

EVENT = "dealmachine_hot"

# metro key -> (state, label used as the "county" tag in REI Reply)
METRO_META = {
    "cleveland":  ("OH", "Cleveland Metro"),
    "cincinnati": ("OH", "Cincinnati Metro"),
    "columbus":   ("OH", "Columbus Metro"),
    "savannah":   ("GA", "Savannah Metro"),
    "atlanta":    ("GA", "Atlanta Metro"),
}


def run(store: Store, metros: list[str], per_metro_limit: int = 50) -> int:
    d = dt.date.today().isoformat()
    new = 0

    for metro in metros:
        if metro not in METROS:
            log.warning("skipping unknown metro %r", metro)
            continue
        state, label = METRO_META[metro]

        try:
            people, credits = search_hot(metro, limit=per_metro_limit)
        except Exception as e:
            log.error("dealmachine %s failed: %s", metro, type(e).__name__)
            store.log_run(f"dealmachine_{metro}", 0, 0, "error", str(e))
            continue

        kept = 0
        for person in people:
            p = to_event_payload(person, metro, state, label)
            if not p.get("phone"):
                continue  # nothing to gate on skiptrace-style, but no
                          # number means no call, same rule as everywhere else
            pid = person.get("dm_person_id") or p["owner_full"]
            cur = store.db.execute(
                "SELECT 1 FROM events WHERE county=? AND parcel=? AND event=?",
                (f"dealmachine_{metro}", pid, EVENT),
            ).fetchone()
            if cur:
                continue
            store.db.execute(
                "INSERT INTO events (county,parcel,event,detected_on,payload) VALUES (?,?,?,?,?)",
                (f"dealmachine_{metro}", pid, EVENT, d, json.dumps(p, default=str)),
            )
            new += 1
            kept += 1

        store.db.commit()
        store.log_run(f"dealmachine_{metro}", len(people), kept, "ok",
                      f"{credits} credits")
        log.info("dealmachine %s (%s): %s found, %s with phone, %s credits",
                 metro, label, len(people), kept, credits)

    return new
