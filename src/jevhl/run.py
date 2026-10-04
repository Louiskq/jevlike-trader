"""Dry-run entry point: real testnet market data, mock model, simulated fills, no key.

    uv run python -m jevhl.run --seconds 120 [--db jevhl.db]

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
        trader.check_stale()
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
) -> Trader:
    trader = Trader(
        cfg, store, model or MockModel(), PaperExchange(store, meta, cfg.coin),
        Ledger.from_fills(store.fills()), kill_switch_from_store(cfg, store, clock), clock=clock,
    )
    dog = asyncio.create_task(_watchdog(trader))
    try:
        await asyncio.wait_for(trader.run(books), duration_s)
    except TimeoutError:
        pass  # duration reached; run()'s cleanup already flattened
    finally:
        trader.stopped = True
        dog.cancel()
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
    args = ap.parse_args(argv)
    cfg = Config.from_env()
    store = Store(args.db)
    meta = fetch_meta(cfg.coin)

    def on_event(kind: str, detail: str) -> None:
        store.log_event(time.time(), kind, detail)

    async def go() -> Trader:
        return await dry_run(
            cfg, store, meta, stream_books(cfg.coin, time.time, on_event=on_event),
            duration_s=args.seconds,
        )

    trader = asyncio.run(go())
    print(format_report(store, trader))


if __name__ == "__main__":
    main()
