"""Dry-run fill simulation: marketable IOC orders walked against the l2Book snapshot.

Assumptions (not exchange facts): taker fee 4.5 bps of notional (base-tier perp taker rate);
the limit price is the best opposite price moved 50 bps in the aggressive direction, then
rounded to exchange rules; fills use the snapshot's top-of-book sizes only and never see
queue changes between snapshots. Paper fills get negative tids (counting down from the
lowest stored tid) so they can never collide with real exchange trade ids; oid = tid.
"""

from __future__ import annotations

from decimal import Decimal

from .config import Config
from .feed import Book
from .rounding import AssetMeta, order_size, round_price, round_size

TAKER_FEE_RATE = 0.00045
SLIPPAGE_BPS = 50


def limit_price(side: str, book: Book, meta: AssetMeta) -> Decimal:
    slip = Decimal(SLIPPAGE_BPS) / Decimal(10_000)
    if side == "buy":
        return round_price(Decimal(str(book.asks[0][0])) * (1 + slip), meta, up=True)
    return round_price(Decimal(str(book.bids[0][0])) * (1 - slip), meta, up=False)


class PaperExchange:
    def __init__(
        self,
        store,
        meta: AssetMeta,
        coin: str,
        min_notional: float = Config.min_notional_usd,
    ):
        self._store = store
        self.meta = self._meta = meta
        self._coin = coin
        self._min_notional = min_notional
        self._next_tid = store.min_tid() - 1

    def open(self, book: Book, side: str, usd: float, ts: float) -> list[dict]:
        """Entry: IOC for a target notional (rounded up one lot to meet the minimum)."""
        ref = book.asks[0][0] if side == "buy" else book.bids[0][0]
        sz = order_size(usd, ref, self._meta, self._min_notional)
        return self._execute(book, side, sz, ts)

    def close(self, book: Book, side: str, size: float, position: float, ts: float) -> list[dict]:
        """Reduce-only IOC: never larger than the position, never in the growing direction."""
        room = -position if side == "buy" else position
        if room <= 0:
            return []
        sz = round_size(min(Decimal(str(size)), Decimal(str(room))), self._meta)
        if sz <= 0:
            return []
        return self._execute(book, side, sz, ts)

    def _execute(self, book: Book, side: str, sz: Decimal, ts: float) -> list[dict]:
        limit = limit_price(side, book, self._meta)
        levels = book.asks if side == "buy" else book.bids
        left = sz
        fills = []
        for lvl_px, lvl_sz in levels:
            if left <= 0:
                break
            if (Decimal(str(lvl_px)) > limit) if side == "buy" else (Decimal(str(lvl_px)) < limit):
                break
            take = min(left, round_size(Decimal(str(lvl_sz)), self._meta))
            if take <= 0:
                continue
            left -= take
            fee = lvl_px * float(take) * TAKER_FEE_RATE
            tid = self._next_tid
            self._next_tid -= 1
            self._store.insert_fill(
                tid=tid, oid=tid, ts=ts, coin=self._coin, side=side,
                sz=float(take), px=lvl_px, fee=fee,
            )
            fills.append(
                {"tid": tid, "side": side, "sz": float(take), "px": lvl_px, "fee": fee, "ts": ts}
            )
        return fills
