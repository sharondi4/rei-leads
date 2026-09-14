"""SQLite state. The whole pipeline is a daily snapshot plus a diff.

parcels    -- latest observed state, one row per (county, parcel)
events     -- every transition worth calling about
pushes     -- what we sent to REI Reply, so a rerun never double-pushes
"""
import sqlite3
import json
import datetime as dt
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS parcels (
  county        TEXT NOT NULL,
  parcel        TEXT NOT NULL,
  first_seen    TEXT NOT NULL,
  last_seen     TEXT NOT NULL,
  in_foreclosure INTEGER NOT NULL DEFAULT 0,
  delq_balance  REAL,
  payload       TEXT NOT NULL,
  PRIMARY KEY (county, parcel)
);
CREATE TABLE IF NOT EXISTS events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  county      TEXT NOT NULL,
  parcel      TEXT NOT NULL,
  event       TEXT NOT NULL,
  detected_on TEXT NOT NULL,
  payload     TEXT NOT NULL,
  UNIQUE (county, parcel, event, detected_on)
);
CREATE TABLE IF NOT EXISTS pushes (
  county      TEXT NOT NULL,
  parcel      TEXT NOT NULL,
  event       TEXT NOT NULL,
  pushed_at   TEXT NOT NULL,
  contact_id  TEXT,
  status      TEXT NOT NULL,
  PRIMARY KEY (county, parcel, event)
);
CREATE TABLE IF NOT EXISTS runs (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  county     TEXT NOT NULL,
  started_at TEXT NOT NULL,
  rows       INTEGER,
  new_events INTEGER,
  status     TEXT,
  note       TEXT
);
CREATE TABLE IF NOT EXISTS overlays (
  county      TEXT NOT NULL,
  parcel      TEXT NOT NULL,
  signal      TEXT NOT NULL,
  source      TEXT NOT NULL,
  detected_on TEXT NOT NULL,
  detail      TEXT,
  PRIMARY KEY (county, parcel, signal)
);
CREATE TABLE IF NOT EXISTS skiptraces (
  county      TEXT NOT NULL,
  parcel      TEXT NOT NULL,
  traced_at   TEXT NOT NULL,
  status      TEXT NOT NULL,
  phone       TEXT,
  phone_type  TEXT,
  do_not_call INTEGER NOT NULL DEFAULT 0,
  email       TEXT,
  credits     INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (county, parcel)
);
CREATE INDEX IF NOT EXISTS idx_events_date ON events(detected_on);
CREATE INDEX IF NOT EXISTS idx_skiptraces_date ON skiptraces(traced_at);
CREATE INDEX IF NOT EXISTS idx_overlays_parcel ON overlays(county, parcel);
"""


def today() -> str:
    return dt.date.today().isoformat()


class Store:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self):
        self.db.close()

    # ---------- snapshot + diff ----------

    def prior_state(self, county: str) -> dict:
        cur = self.db.execute(
            "SELECT parcel, in_foreclosure, delq_balance FROM parcels WHERE county=?",
            (county,),
        )
        return {r["parcel"]: (r["in_foreclosure"], r["delq_balance"]) for r in cur}

    def apply_snapshot(self, county: str, rows: list, vet=None) -> list:
        """rows: list of dicts with keys parcel, in_foreclosure, delq_balance, payload.

        Returns the list of new events. A parcel is a lead when it ENTERS
        foreclosure, not merely because it is in foreclosure -- otherwise the
        first run dumps every distressed parcel in the county into the CRM.

        vet: optional callable(payload) -> (ok, reason, payload). Applied to
        events only, never to the parcels table -- a rejected parcel still
        gets tracked, so tomorrow's diff and the backlog both keep seeing
        it. Passed in rather than imported so this module stays free of
        classifier and county knowledge.
        """
        prior = self.prior_state(county)
        first_run = len(prior) == 0
        d = today()
        events = []

        for r in rows:
            parcel = r["parcel"]
            fc = 1 if r["in_foreclosure"] else 0
            bal = r.get("delq_balance")
            payload = json.dumps(r.get("payload", {}), default=str)

            was = prior.get(parcel)
            if was is None:
                ev = None if first_run else ("new_parcel_in_foreclosure" if fc else None)
            else:
                prev_fc, prev_bal = was
                if fc and not prev_fc:
                    ev = "entered_foreclosure"
                elif not fc and prev_fc:
                    ev = "exited_foreclosure"
                elif fc and bal is not None and prev_bal is not None and bal > (prev_bal or 0) * 1.05:
                    ev = "balance_increased"
                else:
                    ev = None

            self.db.execute(
                """INSERT INTO parcels (county,parcel,first_seen,last_seen,in_foreclosure,delq_balance,payload)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(county,parcel) DO UPDATE SET
                     last_seen=excluded.last_seen,
                     in_foreclosure=excluded.in_foreclosure,
                     delq_balance=excluded.delq_balance,
                     payload=excluded.payload""",
                (county, parcel, d, d, fc, bal, payload),
            )

            if ev and ev != "exited_foreclosure":
                ev_payload = r.get("payload", {})
                if vet is not None:
                    ok, _reason, ev_payload = vet(ev_payload)
                    if not ok:
                        continue
                cur = self.db.execute(
                    """INSERT OR IGNORE INTO events (county,parcel,event,detected_on,payload)
                       VALUES (?,?,?,?,?)""",
                    (county, parcel, ev, d, json.dumps(ev_payload, default=str)),
                )
                if cur.rowcount:
                    # Same shape as unpushed() returns, so downstream code
                    # (CSV writer, contact mapper) handles both identically.
                    events.append({
                        "county": county,
                        "parcel": parcel,
                        "event": ev,
                        "detected_on": d,
                        "payload": ev_payload,
                    })

        self.db.commit()
        return events

    # ---------- overlays: extra distress lists stacked onto the bank ----------

    def add_overlay(self, county, parcel, signal, source, detail=""):
        """Record that some other list also names this parcel.

        `signal` is a key classify.py already scores -- vacant,
        code_violation, probate_opened -- so a stacked list raises a
        lead's score without any change to the classifier.
        """
        self.db.execute(
            """INSERT INTO overlays (county,parcel,signal,source,detected_on,detail)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(county,parcel,signal) DO UPDATE SET
                 source=excluded.source,
                 detected_on=excluded.detected_on,
                 detail=excluded.detail""",
            (county, parcel, signal, source, today(), detail),
        )

    def overlays_for(self, county: str = None) -> dict:
        """{(county, parcel): {signal: detail}} -- loaded once per run
        rather than queried per lead, since the backlog pass walks ~99k
        parcels and a query each would dominate its runtime."""
        sql = "SELECT county, parcel, signal, detail FROM overlays"
        params = ()
        if county:
            sql += " WHERE county=?"
            params = (county,)
        out = {}
        for r in self.db.execute(sql, params):
            out.setdefault((r["county"], r["parcel"]), {})[r["signal"]] = r["detail"]
        return out

    # ---------- push bookkeeping ----------

    def unpushed(self, limit: int) -> list:
        cur = self.db.execute(
            """SELECT e.county, e.parcel, e.event, e.detected_on, e.payload
               FROM events e
               LEFT JOIN pushes p
                 ON p.county=e.county AND p.parcel=e.parcel AND p.event=e.event
               WHERE p.parcel IS NULL
               ORDER BY e.detected_on DESC, e.id DESC
               LIMIT ?""",
            (limit,),
        )
        out = []
        for r in cur:
            d = dict(r)
            d["payload"] = json.loads(d["payload"])
            out.append(d)
        return out

    def mark_pushed(self, county, parcel, event, contact_id, status):
        self.db.execute(
            """INSERT OR REPLACE INTO pushes (county,parcel,event,pushed_at,contact_id,status)
               VALUES (?,?,?,?,?,?)""",
            (county, parcel, event, dt.datetime.now().isoformat(timespec="seconds"), contact_id, status),
        )
        self.db.commit()

    def log_run(self, county, rows, new_events, status, note=""):
        self.db.execute(
            "INSERT INTO runs (county,started_at,rows,new_events,status,note) VALUES (?,?,?,?,?,?)",
            (county, dt.datetime.now().isoformat(timespec="seconds"), rows, new_events, status, note),
        )
        self.db.commit()
