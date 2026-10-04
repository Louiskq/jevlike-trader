"""SQLite storage. Parameterized SQL only."""

from __future__ import annotations

import math
import sqlite3

from .calibration import label
from .types import Decision

SCHEMA_VERSION = 2  # v2: fills keyed on exchange trade id (tid); no migration from v1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    p_up REAL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    size_usd REAL NOT NULL,
    gate REAL NOT NULL,
    latency_ms REAL,
    mid REAL NOT NULL,
    mid_future REAL,
    label INTEGER
);
CREATE INDEX IF NOT EXISTS decisions_ts ON decisions(ts);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tid INTEGER NOT NULL UNIQUE,
    oid INTEGER NOT NULL,
    ts REAL NOT NULL,
    coin TEXT NOT NULL,
    side TEXT NOT NULL,
    sz REAL NOT NULL,
    px REAL NOT NULL,
    fee REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str = ":memory:"):
        self._db = sqlite3.connect(path)
        self._db.row_factory = sqlite3.Row
        if path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        if self._db.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0:
            self._db.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def schema_version(self) -> int:
        return self._db.execute("SELECT version FROM schema_version").fetchone()[0]

    # decisions
    def insert_decision(self, d: Decision, mid: float) -> int:
        # An invalid probability (non-finite or outside [0, 1]) is never stored, even when
        # the decision was held as "late", so it cannot reach calibration.
        p = d.p_up
        if p is not None and not (math.isfinite(p) and 0.0 <= p <= 1.0):
            p = None
        cur = self._db.execute(
            "INSERT INTO decisions (ts, p_up, action, reason, size_usd, gate, latency_ms, mid)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (d.ts, p, d.action.value, d.reason, d.size_usd, d.gate, d.latency_ms, mid),
        )
        self._db.commit()
        return int(cur.lastrowid)

    def resolve(self, decision_id: int, mid_future: float) -> None:
        """Record the mid one horizon later and the resulting label (None on a tie)."""
        mid = self._db.execute("SELECT mid FROM decisions WHERE id = ?", (decision_id,)).fetchone()
        if mid is None:
            raise KeyError(decision_id)
        self._db.execute(
            "UPDATE decisions SET mid_future = ?, label = ? WHERE id = ?",
            (mid_future, label(mid["mid"], mid_future), decision_id),
        )
        self._db.commit()

    def decisions(self) -> list[sqlite3.Row]:
        return self._db.execute("SELECT * FROM decisions ORDER BY ts, id").fetchall()

    def pending(self, before_ts: float) -> list[sqlite3.Row]:
        return self._db.execute(
            "SELECT * FROM decisions WHERE mid_future IS NULL AND ts <= ? ORDER BY ts, id",
            (before_ts,),
        ).fetchall()

    def scored(self) -> list[tuple[float, int]]:
        """(p_up, label) for every resolved, non-tie decision that has a probability
        (traded or held)."""
        rows = self._db.execute(
            "SELECT p_up, label FROM decisions"
            " WHERE label IS NOT NULL AND p_up IS NOT NULL ORDER BY ts, id"
        ).fetchall()
        return [(r["p_up"], r["label"]) for r in rows]

    # fills
    def insert_fill(
        self,
        *,
        tid: int,
        oid: int,
        ts: float,
        coin: str,
        side: str,
        sz: float,
        px: float,
        fee: float,
    ) -> bool:
        """Insert a fill; returns False if that exchange trade id (tid) was already stored."""
        cur = self._db.execute(
            "INSERT OR IGNORE INTO fills (tid, oid, ts, coin, side, sz, px, fee)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (tid, oid, ts, coin, side, sz, px, fee),
        )
        self._db.commit()
        return cur.rowcount == 1

    def fills(self) -> list[sqlite3.Row]:
        return self._db.execute("SELECT * FROM fills ORDER BY ts, id").fetchall()

    # events
    def log_event(self, ts: float, kind: str, detail: str) -> None:
        self._db.execute(
            "INSERT INTO events (ts, kind, detail) VALUES (?, ?, ?)", (ts, kind, detail)
        )
        self._db.commit()

    def events(self) -> list[sqlite3.Row]:
        return self._db.execute("SELECT * FROM events ORDER BY ts, id").fetchall()
