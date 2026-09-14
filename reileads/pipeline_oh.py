"""Ohio pipeline: county open-data snapshot -> diff -> event.

Ohio's shape (see sources/oh/*): Cuyahoga and Hamilton publish a daily
foreclosure_flag directly in county open data, so each run is a full
snapshot that store.apply_snapshot() diffs against yesterday. Franklin has
no such flag and runs off a watchlist instead. See sources/oh/franklin.py
for why.

Pushing to REI Reply is handled by core.push, shared with pipeline_ga.
"""
import logging
from collections import Counter

from .core.store import Store
from .core import quality
from .sources.oh.cuyahoga import Cuyahoga
from .sources.oh.hamilton import Hamilton
from .sources.oh.franklin import Franklin
from .sources.oh.stark import Stark
from .sources.oh.mahoning import Mahoning
from .sources.oh.summit import Summit

log = logging.getLogger(__name__)

REGISTRY = {c.county: c for c in (Cuyahoga, Hamilton, Franklin, Stark, Mahoning, Summit)}


def run_county(store: Store, name: str, **kwargs) -> list:
    src = REGISTRY[name]
    log.info("=== %s ===", src.label)
    try:
        rows = list(src.fetch(**kwargs))
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        log.error("%s fetch failed: %s", src.label, err)
        store.log_run(name, 0, 0, "error", err)
        return []

    # Until 2026-09-14 Ohio events went straight to the CRM: unscored,
    # untiered, and with none of Sharon's exclusions applied -- the
    # classifier only ever ran on Georgia. Same gate the backlog uses.
    rejected = Counter()

    def vet(payload):
        ok, reason, p = quality.vet(payload)
        if not ok:
            rejected[reason] += 1
        return ok, reason, p

    events = store.apply_snapshot(name, rows, vet=vet)
    log.info("%s: %s rows, %s new events%s", src.label, f"{len(rows):,}", len(events),
             (" (rejected: " + ", ".join(f"{v} {k}" for k, v in rejected.most_common()) + ")")
             if rejected else "")
    store.log_run(name, len(rows), len(events), "ok")
    return events
