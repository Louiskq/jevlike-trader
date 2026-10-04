"""SQLite storage. Parameterized SQL only."""

from __future__ import annotations

import math
import sqlite3

from .calibration import label
from .types import Decision

# v2: fills keyed on exchange trade id (tid).
# v3: per-horizon `outcomes` table replaces decisions.mid_future / label.
# No migration: an older database is refused.
SCHEMA_VERSION = 3

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
    mid REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS decisions_ts ON decisions(ts);
CREATE TABLE IF NOT EXISTS outcomes (
    decision_id INTEGER NOT NULL REFERENCES decisions(id),
    horizon_s INTEGER NOT NULL,
    mid_future REAL NOT NULL,
    label INTEGER,
    PRIMARY KEY (decision_id, horizon_s)
);
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
        has_version = self._db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'schema_version'"
        ).fetchone()[0]
        if has_version:
            row = self._db.execute("SELECT version FROM schema_version").fetchone()
            if row is not None and row[0] != SCHEMA_VERSION:
                self._db.close()
                raise RuntimeError(
                    f"database schema v{row[0]} is not v{SCHEMA_VERSION}; there is no migration"
                )
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

    def resolve(self, decision_id: int, horizon_s: int, mid_future: float) -> None:
        """Record the mid one horizon later and the label (None on a tie). Idempotent: the
        first outcome for (decision, horizon) stands."""
        if not (math.isfinite(mid_future) and mid_future > 0):
            raise ValueError(f"mid_future must be positive and finite, got {mid_future!r}")
        row = self._db.execute("SELECT mid FROM decisions WHERE id = ?", (decision_id,)).fetchone()
        if row is None:
            raise KeyError(decision_id)
        self._db.execute(
            "INSERT OR IGNORE INTO outcomes (decision_id, horizon_s, mid_future, label)"
            " VALUES (?, ?, ?, ?)",
            (decision_id, horizon_s, mid_future, label(row["mid"], mid_future)),
        )
        self._db.commit()

    def outcomes(self, decision_id: int) -> list[sqlite3.Row]:
        return self._db.execute(
            "SELECT * FROM outcomes WHERE decision_id = ? ORDER BY horizon_s", (decision_id,)
        ).fetchall()

    def decisions(self) -> list[sqlite3.Row]:
        return self._db.execute("SELECT * FROM decisions ORDER BY ts, id").fetchall()

    def recent_decisions(self, n: int) -> list[sqlite3.Row]:
        """The last n decisions, oldest first."""
        rows = self._db.execute(
            "SELECT * FROM decisions ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
        return rows[::-1]

    def decisions_after(self, decision_id: int, limit: int = 500) -> list[sqlite3.Row]:
        return self._db.execute(
            "SELECT * FROM decisions WHERE id > ? ORDER BY id LIMIT ?", (decision_id, limit)
        ).fetchall()

    def decision_counts(self) -> dict[str, int]:
        row = self._db.execute(
            "SELECT COUNT(*) AS total,"
            " COALESCE(SUM(action = 'hold'), 0) AS holds,"
            " COALESCE(SUM(reason = 'late'), 0) AS late FROM decisions"
        ).fetchone()
        return {"total": row["total"], "holds": row["holds"], "late": row["late"]}

    def pending(self, horizon_s: int, lo_ts: float, hi_ts: float) -> list[sqlite3.Row]:
        """Decisions with ts in [lo_ts, hi_ts] that have no outcome yet at this horizon. The
        lower bound stops a long-ago decision being resolved with a much later mid."""
        return self._db.execute(
            "SELECT d.* FROM decisions d WHERE d.ts BETWEEN ? AND ?"
            " AND NOT EXISTS (SELECT 1 FROM outcomes o"
            "                 WHERE o.decision_id = d.id AND o.horizon_s = ?)"
            " ORDER BY d.ts, d.id",
            (lo_ts, hi_ts, horizon_s),
        ).fetchall()

    def scored(self, horizon_s: int) -> list[tuple[float, int]]:
        """(p_up, label) at this horizon for every resolved, non-tie decision that has a
        probability (traded or held). Ties are dropped per horizon."""
        rows = self._db.execute(
            "SELECT d.p_up, o.label FROM decisions d JOIN outcomes o ON o.decision_id = d.id"
            " WHERE o.horizon_s = ? AND o.label IS NOT NULL AND d.p_up IS NOT NULL"
            " ORDER BY d.ts, d.id",
            (horizon_s,),
        ).fetchall()
        return [(r["p_up"], r["label"]) for r in rows]

    def latencies(self) -> list[float]:
        """Model latency (ms) of every decision that made a model call."""
        rows = self._db.execute(
            "SELECT latency_ms FROM decisions WHERE latency_ms IS NOT NULL ORDER BY id"
        ).fetchall()
        return [r["latency_ms"] for r in rows]

    def last_mid_before(self, ts: float) -> float | None:
        """Mid of the latest decision strictly before ts."""
        row = self._db.execute(
            "SELECT mid FROM decisions WHERE ts < ? ORDER BY ts DESC, id DESC LIMIT 1", (ts,)
        ).fetchone()
        return None if row is None else row["mid"]

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
        if not all(math.isfinite(x) for x in (sz, px, fee)) or sz <= 0 or px <= 0:
            raise ValueError(f"bad fill: sz={sz!r} px={px!r} fee={fee!r}")
        cur = self._db.execute(
            "INSERT OR IGNORE INTO fills (tid, oid, ts, coin, side, sz, px, fee)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (tid, oid, ts, coin, side, sz, px, fee),
        )
        self._db.commit()
        return cur.rowcount == 1

    def fills(self) -> list[sqlite3.Row]:
        return self._db.execute("SELECT * FROM fills ORDER BY ts, id").fetchall()

    def fills_before(self, ts: float) -> list[sqlite3.Row]:
        return self._db.execute(
            "SELECT * FROM fills WHERE ts < ? ORDER BY ts, id", (ts,)
        ).fetchall()

    def recent_fills(self, n: int) -> list[sqlite3.Row]:
        """The last n fills, oldest first."""
        rows = self._db.execute("SELECT * FROM fills ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return rows[::-1]

    def fills_after(self, fill_id: int, limit: int = 500) -> list[sqlite3.Row]:
        return self._db.execute(
            "SELECT * FROM fills WHERE id > ? ORDER BY id LIMIT ?", (fill_id, limit)
        ).fetchall()

    def min_tid(self) -> int:
        """Lowest stored tid (or 0); paper fills count down from here, below real tids."""
        return min(0, self._db.execute("SELECT COALESCE(MIN(tid), 0) FROM fills").fetchone()[0])

    # events
    def log_event(self, ts: float, kind: str, detail: str) -> None:
        self._db.execute(
            "INSERT INTO events (ts, kind, detail) VALUES (?, ?, ?)", (ts, kind, detail)
        )
        self._db.commit()

    def events(self) -> list[sqlite3.Row]:
        return self._db.execute("SELECT * FROM events ORDER BY ts, id").fetchall()

    def last_event(self, kind: str) -> sqlite3.Row | None:
        return self._db.execute(
            "SELECT * FROM events WHERE kind = ? ORDER BY id DESC LIMIT 1", (kind,)
        ).fetchone()
