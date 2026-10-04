"""Dry-run entry point: real testnet market data, mock model, simulated fills, no key.

    uv run python -m jevhl.run --seconds 120 [--db jevhl.db] [--dashboard [--port 8765]]

Env: COIN, GATE, MAX_POSITION_USD, MAX_DAILY_LOSS_USD, ... (see config). Testnet only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import urllib.request
from collections.abc import AsyncIterator, Callable

from .calibration import all_horizon_reports
from .config import Config
from .feed import INFO_URL, Book, stream_books
from .latency import latency_report
from .ledger import Ledger
from .model import MockModel
from .paper import PaperExchange
from .rounding import AssetMeta
from .server import DashboardStartError, create_app, make_server, serve
from .store import Store
from .trader import Trader, kill_switch_from_store


def fetch_meta(coin: str) -> AssetMeta:
    req = urllib.request.Request(
        INFO_URL, data=json.dumps({"type": "meta"}).encode(),
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 (fixed https URL)
        return AssetMeta.from_meta(json.load(resp), coin)


async def _watchdog(trader: Trader, every_s: float = 1.0) -> None:
    while not trader.stopped:
        await trader.check_stale()
        await asyncio.sleep(every_s)


async def dry_run(
    cfg: Config,
    store: Store,
    meta: AssetMeta,
    books: AsyncIterator[Book],
    *,
    model=None,
    clock: Callable[[], float] = time.time,
    duration_s: float | None = None,
    on_trader: Callable[[Trader], None] | None = None,
) -> Trader:
    trader = Trader(
        cfg, store, model or MockModel(), PaperExchange(store, meta, cfg.coin),
        Ledger.from_fills(store.fills()), kill_switch_from_store(cfg, store, clock), clock=clock,
    )
    if on_trader is not None:
        on_trader(trader)
    dog = asyncio.create_task(_watchdog(trader))
    try:
        await asyncio.wait_for(trader.run(books), duration_s)
    except TimeoutError:
        pass  # duration reached; run()'s cleanup already flattened
    finally:
        trader.stopped = True
        dog.cancel()
    return trader


def trader_live(trader: Trader, clock: Callable[[], float]) -> Callable[[], dict]:
    """The dashboard's live view of a trader: current mid, feed freshness, kill state."""

    def live() -> dict:
        book = trader.last_book
        if book is None:
            feed, mid = "waiting", None
        else:
            feed = "live" if clock() - book.recv_ts <= trader.cfg.stale_s else "stale"
            mid = book.mid
        return {"mid": mid, "feed": feed, "kill_reason": trader.kill_reason,
                "stopped": trader.stopped}

    return live


async def dry_run_with_dashboard(
    cfg: Config,
    db_path: str,
    store: Store,
    meta: AssetMeta,
    books: AsyncIterator[Book],
    *,
    port: int,
    model=None,
    clock: Callable[[], float] = time.time,
    duration_s: float | None = None,
    on_ready: Callable[[int], None] | None = None,
    linger_s: float | None = 0.0,
) -> Trader:
    """dry_run plus the dashboard on 127.0.0.1:port (0 = any free port) in the same event
    loop. If the kill switch stopped the trader, the dashboard stays up for linger_s seconds
    (None = until the server is stopped, e.g. Ctrl-C) so the trip can be read on the page."""
    traders: list[Trader] = []

    def live() -> dict:
        if traders:
            return trader_live(traders[0], clock)()
        return {"mid": None, "feed": "waiting", "kill_reason": None, "stopped": False}

    box: dict = {}
    app = create_app(db_path, live, should_stop=lambda: box["server"].should_exit)
    server = box["server"] = make_server(app, port=port)
    server_task = asyncio.create_task(serve(server))

    def server_done(_: asyncio.Future) -> None:  # Ctrl-C or a server failure stops the bot
        for t in traders:
            t.stopped = True

    server_task.add_done_callback(server_done)
    while not server.started:
        if server_task.done():
            await server_task  # raises DashboardStartError on a startup failure
            raise DashboardStartError("the dashboard server exited before starting")
        await asyncio.sleep(0.02)
    if on_ready is not None:
        on_ready(server.servers[0].sockets[0].getsockname()[1])
    try:
        trader = await dry_run(
            cfg, store, meta, books, model=model, clock=clock, duration_s=duration_s,
            on_trader=traders.append,
        )
        if trader.kill_reason and not server_task.done():
            await asyncio.wait({server_task}, timeout=linger_s)
    finally:
        server.should_exit = True
        await server_task
    return trader


def _f(x: float | None, nd: int = 4) -> str:
    return "n/a" if x is None else f"{x:.{nd}f}"


def format_report(store: Store, trader: Trader) -> str:
    lat = latency_report(store)
    lines = [
        f"decisions {lat['decisions']}  late holds {lat['late_holds']}  "
        f"gap p50 {_f(lat['gap_p50_s'], 2)} s  p90 {_f(lat['gap_p90_s'], 2)} s",
        f"model latency ms (n={lat['n']}): p50 {_f(lat['p50'], 3)}  p90 {_f(lat['p90'], 3)}  "
        f"p99 {_f(lat['p99'], 3)}  max {_f(lat['max'], 3)}",
    ]
    for r in all_horizon_reports(store):
        lines.append(
            f"calibration {r['horizon_s']} s: n={r['n']}  brier {_f(r['brier'])}  "
            f"baseline(running) {_f(r['baseline_running'])}"
        )
    ev: dict[str, int] = {}
    for e in store.events():
        ev[e["kind"]] = ev.get(e["kind"], 0) + 1
    lines.append(f"events {ev}  fills {len(store.fills())}  fees ${trader.ledger.fees:.4f}")
    lines.append(f"stopped={trader.stopped} kill={trader._kill.reason}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=None, help="stop after this long")
    ap.add_argument("--db", default="jevhl.db")
    ap.add_argument("--dashboard", action="store_true", help="serve the dashboard on 127.0.0.1")
    ap.add_argument("--port", type=int, default=8765, help="dashboard port")
    args = ap.parse_args(argv)
    cfg = Config.from_env()
    store = Store(args.db)
    meta = fetch_meta(cfg.coin)

    def on_event(kind: str, detail: str) -> None:
        store.log_event(time.time(), kind, detail)

    async def go() -> Trader:
        if args.dashboard:
            def announce(port: int) -> None:  # only once the server is really listening
                print(f"dashboard: http://127.0.0.1:{port}/ (Ctrl-C to stop)", flush=True)

            return await dry_run_with_dashboard(
                cfg, args.db, store, meta, stream_books(cfg.coin, time.time, on_event=on_event),
                port=args.port, duration_s=args.seconds, linger_s=None, on_ready=announce,
            )
        return await dry_run(
            cfg, store, meta, stream_books(cfg.coin, time.time, on_event=on_event),
            duration_s=args.seconds,
        )

    try:
        trader = asyncio.run(go())
    except DashboardStartError as e:
        raise SystemExit(f"error: {e}") from None
    print(format_report(store, trader))


if __name__ == "__main__":
    main()
