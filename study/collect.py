"""Record public BTC order books and trades from Hyperliquid mainnet and testnet side by side,
for the fine-tuning feasibility study (.pipeline/feasibility-plan.md).

Read-only market data: no key is loaded, nothing is signed, no order code is imported. The
bot itself stays testnet-only; the mainnet URLs live in this script alone, which only reads
the public `info` endpoint and the public `trades` WebSocket channel.

    uv run python study/collect.py --out data/collect      # runs until stopped

Writes one SQLite file per venue (`<out>/<venue>.db`): a REST l2Book (20 levels) about every
second, every public trade (deduped on tid), and connection events.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sqlite3
import time
import urllib.request
from pathlib import Path

from jevhl.feed import MAX_REST_BYTES, REST_TIMEOUT_S, _reject_constant, poll_books, stream_trades

VENUES = {
    "mainnet": ("https://api.hyperliquid.xyz/info", "wss://api.hyperliquid.xyz/ws"),
    "testnet": ("https://api.hyperliquid-testnet.xyz/info", "wss://api.hyperliquid-testnet.xyz/ws"),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (ts REAL NOT NULL, recv_ts REAL NOT NULL, bids TEXT NOT NULL,
                                  asks TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS trades (tid INTEGER PRIMARY KEY, ts REAL NOT NULL, recv_ts REAL NOT NULL,
                                   side TEXT NOT NULL, px REAL NOT NULL, sz REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events (ts REAL NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS books_ts ON books(ts);
CREATE INDEX IF NOT EXISTS trades_ts ON trades(ts);
"""


def open_db(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    return db


def make_fetch(info_url: str):
    def fetch(coin: str) -> object:
        req = urllib.request.Request(
            info_url, data=json.dumps({"type": "l2Book", "coin": coin}).encode(),
            headers={"content-type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=REST_TIMEOUT_S) as resp:  # noqa: S310
            return json.loads(resp.read(MAX_REST_BYTES), parse_constant=_reject_constant)
    return fetch


class Recorder:
    """Buffers rows and commits about every commit_s, so a crash loses at most that much."""

    def __init__(self, db: sqlite3.Connection, clock=time.time, commit_s: float = 5.0):
        self.db, self.clock, self.commit_s = db, clock, commit_s
        self.last_commit = clock()
        self.n_books = self.n_trades = 0

    def book(self, b) -> None:
        self.db.execute("INSERT INTO books VALUES (?,?,?,?)",
                        (b.ts, b.recv_ts, json.dumps(b.bids), json.dumps(b.asks)))
        self.n_books += 1
        self._maybe_commit()

    def trades(self, prints) -> None:
        cur = self.db.executemany(
            "INSERT OR IGNORE INTO trades VALUES (?,?,?,?,?,?)",
            [(p.tid, p.ts, p.recv_ts, p.side, p.px, p.sz) for p in prints])
        self.n_trades += max(cur.rowcount, 0)
        self._maybe_commit()

    def event(self, kind: str, detail: str) -> None:
        self.db.execute("INSERT INTO events VALUES (?,?,?)", (self.clock(), kind, detail[:500]))
        self._maybe_commit()

    def _maybe_commit(self) -> None:
        if self.clock() - self.last_commit >= self.commit_s:
            self.db.commit()
            self.last_commit = self.clock()


async def run_venue(name: str, out: Path, coin: str, seconds: float | None) -> None:
    info_url, ws_url = VENUES[name]
    rec = Recorder(open_db(out / f"{name}.db"))
    rec.event("start", json.dumps({"coin": coin, "info": info_url, "ws": ws_url}))

    async def books():
        async for b in poll_books(coin, time.time, fetch=make_fetch(info_url), on_event=rec.event):
            rec.book(b)

    async def trades():
        async for ps in stream_trades(coin, time.time, url=ws_url, on_event=rec.event):
            rec.trades(ps)

    async def report():
        while True:
            await asyncio.sleep(600)
            rec.event("progress", json.dumps({"books": rec.n_books, "trades": rec.n_trades}))

    tasks = [asyncio.create_task(t()) for t in (books, trades, report)]
    try:
        if seconds is None:
            await asyncio.gather(*tasks)
        else:
            await asyncio.sleep(seconds)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        rec.event("stop", json.dumps({"books": rec.n_books, "trades": rec.n_trades}))
        rec.db.commit()
        rec.db.close()
        print(f"{name}: {rec.n_books} books, {rec.n_trades} trades")


async def main_async(args) -> None:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    loop = asyncio.get_running_loop()
    main = asyncio.current_task()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, main.cancel)
    try:
        await asyncio.gather(*(run_venue(v, out, args.coin, args.seconds) for v in args.venues))
    except asyncio.CancelledError:
        pass


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default="data/collect")
    ap.add_argument("--coin", default="BTC")
    ap.add_argument("--venues", nargs="+", default=list(VENUES), choices=list(VENUES))
    ap.add_argument("--seconds", type=float, default=None, help="stop after this long")
    asyncio.run(main_async(ap.parse_args(argv)))


if __name__ == "__main__":
    main()
