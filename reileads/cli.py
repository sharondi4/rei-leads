"""Unified CLI: python -m reileads.cli run --state oh --county cuyahoga
                python -m reileads.cli run --state ga --county fulton

One store, one push mechanism, two very different collection strategies
underneath -- see pipeline_oh.py and pipeline_ga.py for why they differ.
"""
import argparse
import logging
import sys

from .core import config
from .core.store import Store
from .core.push import push
from . import pipeline_oh
from . import pipeline_ga
from . import backfill
from . import skiptrace as skiptrace_mod
from . import pipeline_dealmachine

OH_COUNTIES = sorted(pipeline_oh.REGISTRY)
GA_COUNTIES = ["fulton", "cobb", "cherokee", "douglas"]  # wired legal-ad sites


def main(argv=None):
    ap = argparse.ArgumentParser(prog="reileads",
        description="Multi-state real estate lead pipeline -> REI Reply")
    ap.add_argument("command", choices=["run", "status", "push", "seed-franklin",
                                        "backfill", "skiptrace", "dealmachine",
                                        "breakdown"])
    ap.add_argument("--state", choices=["oh", "ga"], action="append",
                    help="repeatable; default both")
    ap.add_argument("--county", action="append",
                    help="repeatable; restricts to these counties within --state")
    ap.add_argument("--days-back", type=int, default=3,
                    help="GA legal-ad / probate-court lookback window")
    ap.add_argument("--limit", type=int, default=config.MAX_PUSH_PER_RUN)
    ap.add_argument("--backlog", type=int, default=0,
                    help="release N backlog leads alongside a run (0 = off)")
    ap.add_argument("--preview", action="store_true",
                    help="backfill: report counts and a sample, insert nothing")
    ap.add_argument("--event-like", default=None,
                    help="skiptrace: restrict to events whose name matches, "
                         "e.g. 'ga_%%' or 'backlog_%%'")
    ap.add_argument("--min-score", type=int, default=0,
                    help="backlog: only release leads scoring at least this "
                         "(35 = tier B and above, i.e. worth calling)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

    store = Store(config.DB_PATH)
    states = a.state or ["oh", "ga"]

    if a.command == "seed-franklin":
        from .sources.oh.franklin import Franklin
        parcels = Franklin.seed_from_lien_list()
        path = config.OUT_DIR / "franklin-watchlist.txt"
        path.write_text("\n".join(parcels))
        print(f"{len(parcels):,} parcels -> {path}")
        return 0

    if a.command == "status":
        _status(store)
        return 0

    if a.command == "breakdown":
        _breakdown(store)
        return 0

    if a.command == "dealmachine":
        metros = a.county or ["cleveland", "cincinnati", "columbus", "savannah", "atlanta"]
        n = pipeline_dealmachine.run(store, metros, per_metro_limit=a.backlog or 50)
        print(f"\n{n} dealmachine-sourced leads inserted")
        res = push(store, a.limit)
        print(f"pending={res['pending']} pushed={res['pushed']} failed={res['failed']} dry_run={res['dry_run']}")
        return 0

    if a.command == "skiptrace":
        skiptrace_mod.run(store, limit=a.backlog or 100,
                          event_like=a.event_like, trial=a.preview)
        return 0

    if a.command == "backfill":
        n = backfill.run(store, limit=a.backlog or 150, counties=a.county,
                         preview=a.preview, min_score=a.min_score)
        if a.preview:
            return 0
        print(f"\n{n} backlog leads released")

    if a.command == "run":
        total = 0
        if "oh" in states:
            # franklin needs a seeded watchlist, so it's opt-in via --county;
            # montgomery isn't wired up yet (see sources/oh/ -- no module
            # exists), so it can't be in this list until it is.
            oh_counties = a.county or ["cuyahoga", "hamilton", "stark", "mahoning", "summit"]
            for c in oh_counties:
                if c not in pipeline_oh.REGISTRY:
                    continue
                kwargs = {}
                if c == "franklin":
                    wl = config.OUT_DIR / "franklin-watchlist.txt"
                    if wl.exists():
                        kwargs["watchlist"] = [l.strip() for l in
                                               wl.read_text().splitlines() if l.strip()]
                total += len(pipeline_oh.run_county(store, c, **kwargs))
        if "ga" in states:
            ga_counties = a.county or GA_COUNTIES
            total += pipeline_ga.run(store, counties=ga_counties, days_back=a.days_back)
            # Separate from the (currently disabled) legal-ad path above --
            # see pipeline_ga.py's collect_probate() docstring.
            total += pipeline_ga.run_probate(store, days_back=a.days_back)
        print(f"\n{total} new events")
        # Backlog release runs after the fresh pass so a parcel that just
        # produced a real event today is already in `events` and won't be
        # double-emitted as backlog.
        if a.backlog:
            released = backfill.run(store, limit=a.backlog, counties=a.county,
                                    min_score=a.min_score)
            print(f"{released} backlog leads released")
            total += released

    res = push(store, a.limit)
    print(f"\npending={res['pending']} pushed={res['pushed']} "
          f"failed={res['failed']} dry_run={res['dry_run']}")
    if res["csv"]:
        print(f"csv: {res['csv']}")
    return 0


def _breakdown(store: Store, date: str = None):
    """Exactly what was pushed to REI Reply on one day, by state/county/
    event -- the answer to "what did this morning's run actually do,"
    built because a log line saying "N pushed" doesn't say what N is."""
    import datetime as dt
    date = date or dt.date.today().isoformat()
    rows = store.db.execute(
        """SELECT e.event, e.county,
                  json_extract(e.payload,'$.state') AS state,
                  COUNT(*) n
           FROM events e
           JOIN pushes p ON p.county=e.county AND p.parcel=e.parcel AND p.event=e.event
           WHERE substr(p.pushed_at,1,10)=?
           GROUP BY e.event, e.county, state
           ORDER BY state, e.county""",
        (date,),
    ).fetchall()
    if not rows:
        print(f"No pushes recorded for {date}.")
        return
    total = 0
    print(f"Pushed to REI Reply on {date}:\n")
    for r in rows:
        print(f"  {r['state'] or '??':<4} {r['county']:<20} {r['event']:<26} {r['n']:>4}")
        total += r["n"]
    print(f"\n  TOTAL: {total}")


def _status(store: Store):
    for r in store.db.execute(
            "SELECT county, COUNT(*) n, SUM(in_foreclosure) fc FROM parcels GROUP BY county"):
        print(f"{r['county']:<10} {r['n']:>8,} tracked  {r['fc'] or 0:>7,} in foreclosure")
    print()
    for r in store.db.execute(
            "SELECT detected_on, county, event, COUNT(*) n FROM events "
            "GROUP BY detected_on, county, event ORDER BY detected_on DESC LIMIT 20"):
        print(f"{r['detected_on']}  {r['county']:<10} {r['event']:<28} {r['n']:>6,}")
    unp = len(store.unpushed(100000))
    print(f"\n{unp:,} events not yet pushed")


if __name__ == "__main__":
    sys.exit(main())
