"""Entry point. Default: dry run (real testnet market data, mock model, simulated fills, no key).

    uv run python -m jevhl.run --seconds 120 [--db jevhl.db] [--dashboard [--port 8765]]

With --live: real orders on the Hyperliquid TESTNET, signed by the API wallet key. Use a
separate database for live runs. Credentials come from the environment, e.g.

    uv run --env-file .env python -m jevhl.run --live --db live.db --dashboard

Env: COIN, GATE, MAX_POSITION_USD, MAX_DAILY_LOSS_USD, HL_API_KEY, HL_ACCOUNT_ADDRESS, ...
(see config and .env.example). Testnet only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.request
from collections.abc import AsyncIterator, Callable

from .calibration import all_horizon_reports
from .config import Config
from .exchange import FILL_OVERLAP_MS, Exchange, build_exchange
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


class StartupError(RuntimeError):
    pass


FIRST_HOUR_MAX_USD = 15.0  # SPEC: the first live hour runs at minimum size


def live_size_warning(cfg: Config) -> str | None:
    if cfg.max_position_usd <= FIRST_HOUR_MAX_USD:
        return None
    return (
        f"WARNING: MAX_POSITION_USD is ${cfg.max_position_usd:g}; the first live hour should "
        f"run at minimum size (MAX_POSITION_USD={FIRST_HOUR_MAX_USD:g}). Set it in .env."
    )


def live_since_ms(store: Store, now_s: float) -> int:
    """Where the first fill query starts: a minute before the newest stored fill, else now."""
    rows = store.fills()
    newest = max((r["ts"] for r in rows), default=now_s)
    return int(newest * 1000) - FILL_OVERLAP_MS


def prepare_live(store: Store, exchange: Exchange) -> None:
    """Before trading live: book any fill the store missed (a crash, a UI trade), then refuse
    to start unless the exchange's position matches the ledger rebuilt from the store."""
    if any(r["tid"] < 0 for r in store.fills()):
        raise StartupError("this database holds paper fills: use a separate --db for live runs")
    for f in exchange.sync_fills():
        store.insert_fill(
            tid=f["tid"], oid=f["oid"], ts=f["ts"], coin=exchange.coin, side=f["side"],
            sz=f["sz"], px=f["px"], fee=f["fee"],
        )
    ledger = Ledger.from_fills(store.fills())
    on_exchange = exchange.position()
    if abs(on_exchange - ledger.position) >= 10.0 ** -exchange.meta.sz_decimals / 2:
        raise StartupError(
            f"exchange position {on_exchange} differs from the ledger's {ledger.position}: "
            "close or reconcile it first (python -m jevhl.reconcile)"
        )


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
    executor=None,
) -> Trader:
    """The trading run. executor: the live Exchange, or None for simulated (paper) fills."""
    trader = Trader(
        cfg, store, model or MockModel(), executor or PaperExchange(store, meta, cfg.coin),
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
    executor=None,
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
            on_trader=traders.append, executor=executor,
        )
        if trader.kill_reason and not server_task.done():
            await asyncio.wait({server_task}, timeout=linger_s)
    finally:
        server.should_exit = True
        await server_task
    return trader


def _f(x: float | None, nd: int = 4) -> str:
    return "n/a" if x is None else f"{x:.{nd}f}"


def format_report(store: Store, trader: Trader, exchange: Exchange | None = None) -> str:
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
    lines.append(f"stopped={trader.stopped} kill={trader.kill_reason}")
    if exchange is not None:
        try:
            lines.append(f"exchange position {exchange.position()} {exchange.coin} (should be 0)")
        except Exception as e:  # never hide the rest of the report
            lines.append(f"exchange position UNKNOWN ({type(e).__name__}): check it by hand")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=None, help="stop after this long")
    ap.add_argument("--db", default="jevhl.db")
    ap.add_argument("--dashboard", action="store_true", help="serve the dashboard on 127.0.0.1")
    ap.add_argument("--port", type=int, default=8765, help="dashboard port")
    ap.add_argument("--live", action="store_true",
                    help="place real orders on the testnet (needs HL_API_KEY and "
                         "HL_ACCOUNT_ADDRESS)")
    args = ap.parse_args(argv)
    cfg = Config.from_env()
    if args.live:
        creds = {"HL_API_KEY": cfg.api_key, "HL_ACCOUNT_ADDRESS": cfg.account_address}
        for name, value in creds.items():
            if not value:
                raise SystemExit(f"error: --live needs {name} (see .env.example)")
    store = Store(args.db)
    meta = fetch_meta(cfg.coin)
    exchange = None
    if args.live:
        exchange = build_exchange(cfg, meta, live_since_ms(store, time.time()))
        try:
            prepare_live(store, exchange)
        except StartupError as e:
            raise SystemExit(f"error: {e}") from None
        if warning := live_size_warning(cfg):
            print(warning, file=sys.stderr, flush=True)
        print(f"LIVE testnet orders, {cfg.coin}: max position ${cfg.max_position_usd:g}, "
              f"max daily loss ${cfg.max_daily_loss_usd:g}", flush=True)

    def on_event(kind: str, detail: str) -> None:
        store.log_event(time.time(), kind, detail)

    async def go() -> Trader:
        if args.dashboard:
            def announce(port: int) -> None:  # only once the server is really listening
                print(f"dashboard: http://127.0.0.1:{port}/ (Ctrl-C to stop)", flush=True)

            return await dry_run_with_dashboard(
                cfg, args.db, store, meta, stream_books(cfg.coin, time.time, on_event=on_event),
                port=args.port, duration_s=args.seconds, linger_s=None, on_ready=announce,
                executor=exchange,
            )
        return await dry_run(
            cfg, store, meta, stream_books(cfg.coin, time.time, on_event=on_event),
            duration_s=args.seconds, executor=exchange,
        )

    try:
        trader = asyncio.run(go())
    except DashboardStartError as e:
        raise SystemExit(f"error: {e}") from None
    print(format_report(store, trader, exchange))


if __name__ == "__main__":
    main()
