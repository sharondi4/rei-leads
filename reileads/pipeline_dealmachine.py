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
                people: list, cap: int = None) -> int:
    """cap: stop once this many NEW rows have been inserted, even if
    `people` has more -- the hard budget ceiling. Checked against actual
    inserts, not len(people), since some of a batch will always be
    duplicates or phone-less and shouldn't count against the cap."""
    d = dt.date.today().isoformat()
    kept = 0
    for person in people:
        if cap is not None and kept >= cap:
            break
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
                per_page: int = 100, max_rounds: int = 6) -> int:
    """Hit `target` NEW leads total across `metros`, spread across ALL of
    them rather than drained from whichever comes first.

    Round-robin, one page per metro per round: confirmed live 2026-09-16
    that a depth-first version (exhaust metro 1 before ever trying metro
    2) let Cleveland and Atlanta each independently blow past their whole
    150 share, so Cincinnati/Columbus/Savannah never got touched at all
    that day -- "one-sided," Sharon's word for it, and correct. Round-
    robin means every metro gets a turn before any one of them gets a
    second page.

    Also a hard cap, not a soft one: this is budget management, per
    Sharon explicitly ("I don't have budget for more than 300 leads a
    day") -- the same run that was one-sided also overshot 300 by 111,
    because a full page was inserted before ever checking the total. Each
    round now only takes however many leads remain against `target`, so
    the total can undershoot on a thin day but can never exceed it.

    Page still advances per metro across calls -- the fix for day-over-
    day undershoot: re-asking page=1 every day just re-finds people
    already delivered, so this keeps moving down the list.
    """
    new = 0
    pages = {m: 1 for m in metros}
    exhausted = set()

    for round_num in range(1, max_rounds + 1):
        if new >= target or len(exhausted) >= len(metros):
            break
        for metro in metros:
            if new >= target:
                break
            if metro not in METROS:
                exhausted.add(metro)
                continue
            if metro in exhausted:
                continue
            state, label = METRO_META[metro]
            page = pages[metro]
            try:
                people, credits = search_hot(metro, limit=per_page, page=page)
            except Exception as e:
                log.error("dealmachine %s page %s failed: %s", metro, page, type(e).__name__)
                exhausted.add(metro)
                continue
            pages[metro] += 1

            room = target - new
            kept = _insert_new(store, metro, state, label, people, cap=room)
            new += kept
            log.info("dealmachine %s (%s) round %s page %s: %s found, %s new "
                     "(capped at %s room), %s credits (running total %s/%s)",
                     metro, label, round_num, page, len(people), kept, room,
                     credits, new, target)
            store.log_run(f"dealmachine_{metro}", len(people), kept, "ok",
                          f"round {round_num} page {page}, {credits} credits")
            if kept == 0:
                # Nothing new this round -- either genuinely exhausted for
                # today's filters, or the end of what DealMachine has.
                # Drop it from the rotation rather than paying for empty
                # pages on it every round.
                exhausted.add(metro)

    if new < target:
        log.warning("dealmachine: hit %s/%s target, out of metros to try", new, target)
    return new
