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


def _insert_new(store: Store, metro: str, state: str, label: str,
                people: list) -> int:
    d = dt.date.today().isoformat()
    kept = 0
    for person in people:
        p = to_event_payload(person, metro, state, label)
        if not p.get("phone"):
            continue  # no number means no call, same rule as everywhere else
        # dm_person_id is preferred, but not always present in every
        # criterion's result -- confirmed live 2026-09-14, the same
        # person came back once with an id and once without, fell back
        # to two different keys, and was pushed twice. Phone number is
        # the backstop: two records with the same number are the same
        # person regardless of what DealMachine did or didn't attach.
        pid = person.get("dm_person_id") or p.get("phone") or p["owner_full"]
        cur = store.db.execute(
            "SELECT 1 FROM events WHERE county=? AND (parcel=? OR "
            "json_extract(payload,'$.phone')=?) AND event=?",
            (f"dealmachine_{metro}", pid, p.get("phone") or "\x00", EVENT),
        ).fetchone()
        if cur:
            continue
        store.db.execute(
            "INSERT INTO events (county,parcel,event,detected_on,payload) VALUES (?,?,?,?,?)",
            (f"dealmachine_{metro}", pid, EVENT, d, json.dumps(p, default=str)),
        )
        kept += 1
    store.db.commit()
    return kept


def run(store: Store, metros: list[str], per_metro_limit: int = 50) -> int:
    """Original per-metro-fixed-count path. Kept for one-off/manual runs;
    fill_target() below is what the daily schedule actually uses."""
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
        kept = _insert_new(store, metro, state, label, people)
        new += kept
        store.log_run(f"dealmachine_{metro}", len(people), kept, "ok", f"{credits} credits")
        log.info("dealmachine %s (%s): %s found, %s with phone & new, %s credits",
                 metro, label, len(people), kept, credits)
    return new


def fill_target(store: Store, metros: list[str], target: int,
                per_page: int = 100, max_pages: int = 6) -> int:
    """Hit `target` NEW leads total across `metros`, rather than a fixed
    count per metro. "If you finish the first list, do the second" --
    a metro that's run dry (Savannah has less inventory than Atlanta)
    gets skipped and the shortfall rolls onto whichever metro still has
    supply, instead of leaving the day short.

    Also the fix for day-over-day undershoot: page advances per metro
    until it stops finding anyone NEW, rather than always re-asking
    page=1 and re-finding people already delivered on a prior day.
    """
    new = 0
    for metro in metros:
        if metro not in METROS or new >= target:
            continue
        state, label = METRO_META[metro]

        for page in range(1, max_pages + 1):
            if new >= target:
                break
            try:
                people, credits = search_hot(metro, limit=per_page, page=page)
            except Exception as e:
                log.error("dealmachine %s page %s failed: %s", metro, page, type(e).__name__)
                break
            kept = _insert_new(store, metro, state, label, people)
            new += kept
            log.info("dealmachine %s (%s) page %s: %s found, %s new, %s credits "
                     "(running total %s/%s)",
                     metro, label, page, len(people), kept, credits, new, target)
            store.log_run(f"dealmachine_{metro}", len(people), kept, "ok",
                          f"page {page}, {credits} credits")
            if kept == 0:
                # Nothing new on this page -- either the metro's genuinely
                # exhausted for today's filters, or we've reached the end
                # of what DealMachine has. Move to the next metro rather
                # than burning pages (and credits) for zero return.
                break

    if new < target:
        log.warning("dealmachine: hit %s/%s target, out of metros to try", new, target)
    return new
