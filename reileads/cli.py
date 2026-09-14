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

OH_COUNTIES = sorted(pipeline_oh.REGISTRY)
GA_COUNTIES = ["fulton", "cobb", "cherokee", "douglas"]  # wired legal-ad sites


def main(argv=None):
    ap = argparse.ArgumentParser(prog="reileads",
        description="Multi-state real estate lead pipeline -> REI Reply")
    ap.add_argument("command", choices=["run", "status", "push", "seed-franklin"])
    ap.add_argument("--state", choices=["oh", "ga"], action="append",
                    help="repeatable; default both")
    ap.add_argument("--county", action="append",
                    help="repeatable; restricts to these counties within --state")
    ap.add_argument("--days-back", type=int, default=3,
                    help="GA legal-ad / probate-court lookback window")
    ap.add_argument("--limit", type=int, default=config.MAX_PUSH_PER_RUN)
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

    res = push(store, a.limit)
    print(f"\npending={res['pending']} pushed={res['pushed']} "
          f"failed={res['failed']} dry_run={res['dry_run']}")
    if res["csv"]:
        print(f"csv: {res['csv']}")
    return 0


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
