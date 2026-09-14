"""Shared REI Reply push, used by every state pipeline identically.

Deliberately state-agnostic: it reads store.unpushed(), which already
returns events from whichever state inserted them, and to_contact()
resolves state/county from the payload rather than from the caller. So
Ohio and Georgia leads flow through this exact same code, get the exact
same CSV format, and are subject to the exact same DRY_RUN and
MAX_PUSH_PER_RUN safety rails.
"""
import csv
import logging
import datetime as dt

from . import config
from .store import Store
from .reireply import ReiReply, to_contact

log = logging.getLogger(__name__)

CSV_COLUMNS = [
    "county", "state", "parcel", "event", "detected_on",
    "owner_full", "owner_first", "owner_last", "is_entity", "absentee",
    "site_address", "site_city", "site_zip",
    "mail_name", "mail_address", "mail_city", "mail_state", "mail_zip",
    "delq_balance", "urgency_score", "tier", "persona",
    "market_value", "record_url", "notice_url",
]


def write_csv(events: list, path) -> str | None:
    if not events:
        return None
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for e in events:
            row = {"county": e["county"], "parcel": e["parcel"],
                   "event": e["event"], "detected_on": e["detected_on"]}
            row.update(e["payload"])
            row["signals"] = "; ".join(e["payload"].get("signals", []))
            w.writerow(row)
    return str(path)


def push(store: Store, limit: int) -> dict:
    pending = store.unpushed(limit)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M")
    out = config.OUT_DIR / f"leads-{stamp}.csv"
    csv_path = write_csv(pending, out)

    result = {"pending": len(pending), "csv": csv_path,
              "pushed": 0, "failed": 0, "dry_run": config.DRY_RUN}
    if not pending:
        return result

    if config.DRY_RUN:
        log.warning("DRY_RUN=1 -- %s leads written to %s, nothing sent to REI Reply",
                    len(pending), out)
        return result

    if not config.REIREPLY_TOKEN or not config.REIREPLY_LOCATION_ID:
        raise RuntimeError("REIREPLY_TOKEN and REIREPLY_LOCATION_ID must be set "
                           "when DRY_RUN=0")

    api = ReiReply()
    for ev in pending:
        contact = to_contact(ev, config.REIREPLY_LOCATION_ID)
        try:
            cid, status = api.upsert(contact)
        except Exception as e:
            cid, status = None, f"exception: {type(e).__name__}: {e}"
        ok = cid is not None
        store.mark_pushed(ev["county"], ev["parcel"], ev["event"], cid,
                          status if ok else f"FAILED {status}")
        result["pushed" if ok else "failed"] += 1
        if not ok:
            log.error("push failed %s/%s: %s", ev["county"], ev["parcel"], status)
    return result
