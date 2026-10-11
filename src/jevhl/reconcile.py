"""Reconcile the local fill history against the exchange's, for the testnet UI check.

    uv run --env-file .env python -m jevhl.reconcile --db live.db

Reads only (needs HL_ACCOUNT_ADDRESS, no API key). Compares every stored live fill with the
exchange's userFills by trade id (side, size, price, order id, fee), and the ledger's position
with the exchange's. Also compares the ledger's realized P&L (before fees) with the sum of the
exchange's own closedPnl. Exits 1 on any mismatch. Prints fees and P&L so they can be compared
with the testnet UI by eye, the maker/taker split of the exchange's fills (from its own
`crossed` flag: this checks the maker fee rate) and the open orders left on the coin.
"""

from __future__ import annotations

import argparse
import time

from .config import Config
from .exchange import BASE_URL, fetch_position, parse_fill
from .ledger import Ledger
from .rounding import AssetMeta
from .store import Store

FEE_TOL = 1e-9  # float noise only; the exchange reports fees to ~6 decimals
PNL_TOL = 1e-4  # dollars: closedPnl is rounded by the exchange, the ledger's is not


def compare(
    local: list[dict], remote: list[dict], local_pos: float, remote_pos: float, dust: float
) -> list[str]:
    """Problems found, as readable lines. Paper fills (negative tid) are ignored."""
    loc = {f["tid"]: f for f in local if f["tid"] > 0}
    rem = {f["tid"]: f for f in remote}
    problems = [f"fill tid {t} is stored locally but not on the exchange"
                for t in sorted(loc.keys() - rem.keys())]
    problems += [f"fill tid {t} is on the exchange but not stored locally"
                 for t in sorted(rem.keys() - loc.keys())]
    for tid in sorted(loc.keys() & rem.keys()):
        a, b = loc[tid], rem[tid]
        for field in ("oid", "side", "sz", "px"):
            if a[field] != b[field]:
                problems.append(f"tid {tid}: {field} local {a[field]!r} exchange {b[field]!r}")
        if abs(a["fee"] - b["fee"]) > FEE_TOL:
            problems.append(f"tid {tid}: fee local {a['fee']!r} exchange {b['fee']!r}")
    if abs(local_pos - remote_pos) >= dust:
        problems.append(f"position: ledger {local_pos} exchange {remote_pos}")
    return problems


def maker_taker_split(rows: list[dict]) -> list[str]:
    """Count, fees and fee rate of the exchange's maker (crossed false) and taker fills."""
    lines = []
    for name, crossed in (("maker", False), ("taker", True)):
        fs = [parse_fill(r) for r in rows if bool(r.get("crossed", True)) is crossed]
        if not fs:
            continue
        fees = sum(f["fee"] for f in fs)
        notional = sum(f["sz"] * f["px"] for f in fs)
        lines.append(f"{name} fills {len(fs)}: fees ${fees:.6f} "
                     f"({fees / notional * 1e4:.2f} bps of notional)")
    return lines


def run_reconcile(
    store: Store, info, address: str, coin: str, meta: AssetMeta
) -> tuple[str, bool]:
    """(report text, ok). Only exchange fills from the first stored live fill on are compared,
    so earlier sessions' history is not flagged."""
    local = [dict(r) for r in store.fills() if r["tid"] > 0]
    start_ms = int(min(f["ts"] for f in local) * 1000) - 1000 if local else int(time.time() * 1000)
    rows = [r for r in info.user_fills_by_time(address, start_ms) if r.get("coin") == coin]
    remote = [parse_fill(r) for r in rows]
    remote_pos = fetch_position(info, address, coin)
    ledger, remote_ledger = Ledger.from_fills(local), Ledger.from_fills(remote)
    problems = compare(local, remote, ledger.position, remote_pos, 10.0 ** -meta.sz_decimals / 2)
    exchange_realized = sum(f["closed_pnl"] for f in remote)  # the exchange's own figure
    if abs(ledger.realized - exchange_realized) > PNL_TOL:
        problems.append(
            f"realized P&L: ledger {ledger.realized:.6f} exchange closedPnl {exchange_realized:.6f}"
        )
    lines = [f"reconcile {coin} for {address}"]
    if not local:
        lines.append("no live fills stored locally")
    lines.append(f"fills {len(local)} local, {len(remote)} on the exchange")
    lines.append(f"fees local ${ledger.fees:.6f}  exchange ${remote_ledger.fees:.6f}")
    lines.append(f"position local {ledger.position}  exchange {remote_pos}")
    lines.append(
        f"realized P&L before fees: ledger ${ledger.realized:.4f}  "
        f"exchange closedPnl ${exchange_realized:.4f}"
    )
    try:
        mid = float(info.all_mids()[coin])
        lines.append(
            f"P&L at mid {mid}: local before fees ${ledger.pnl_before_fees(mid):.4f} "
            f"after fees ${ledger.pnl(mid):.4f}"
        )
    except Exception as e:
        lines.append(f"P&L: no mid available ({type(e).__name__})")
    lines += maker_taker_split(rows)
    try:
        n_open = sum(1 for o in info.open_orders(address) if o.get("coin") == coin)
        lines.append(f"open orders on {coin}: {n_open} (should be 0 after a run)")
    except Exception as e:
        lines.append(f"open orders: unknown ({type(e).__name__})")
    lines += [f"MISMATCH: {p}" for p in problems]
    lines.append(f"{len(problems)} MISMATCH(ES)" if problems else "OK: local and exchange agree")
    return "\n".join(lines), not problems


def _connect(coin: str):
    from hyperliquid.info import Info

    info = Info(BASE_URL, skip_ws=True)
    return info, AssetMeta.from_meta(info.meta(), coin)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="jevhl.db")
    args = ap.parse_args(argv)
    cfg = Config.from_env()
    if not cfg.account_address:
        raise SystemExit("error: reconcile needs HL_ACCOUNT_ADDRESS (see .env.example)")
    info, meta = _connect(cfg.coin)
    text, ok = run_reconcile(Store(args.db), info, cfg.account_address, cfg.coin, meta)
    print(text)
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
