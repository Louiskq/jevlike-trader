"""Dry-run fill simulation: marketable IOC orders walked against the l2Book snapshot.

Assumptions (not exchange facts): taker fee 4.5 bps of notional (base-tier perp taker rate);
the limit price is the best opposite price moved 50 bps in the aggressive direction, then
rounded to exchange rules; fills use the snapshot's top-of-book sizes only and never see
queue changes between snapshots. Paper fills get negative tids (counting down from the
lowest stored tid) so they can never collide with real exchange trade ids; oid = tid.

Maker quotes (milestone 8): one resting post-only quote at a time. It fills only when a real
trade print goes THROUGH its price (a taker sell below our bid, a taker buy above our ask):
at our price others are assumed queued ahead (user decision 2026-10-10). Only prints made and
received after the quote was placed count, and a modify starts the queue again. A fill is at
most the print's size and never more than rests. Maker fee 0.015% (base tier, until verified on
testnet). A quote that would cross the book it was priced on is rejected, as post-only is, and a
modify that changes the side is refused with the order left resting, as on Hyperliquid
(measured 2026-10-11).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .config import Config
from .feed import Book, Print
from .rounding import AssetMeta, aggressive_price, order_size, reduce_size, round_size
from .types import OpenQuote, QuoteAck

TAKER_FEE_RATE = 0.00045
MAKER_FEE_RATE = 0.00015


@dataclass
class _Quote:
    oid: int
    side: str
    px: Decimal
    remaining: Decimal
    placed_ts: float


def limit_price(side: str, book: Book, meta: AssetMeta) -> Decimal:
    ref = book.asks[0][0] if side == "buy" else book.bids[0][0]
    return aggressive_price(side, ref, meta)


class PaperExchange:
    live = False  # fills are simulated and stored here, synchronously

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
        self._quote: _Quote | None = None

    def open(self, book: Book, side: str, usd: float, ts: float) -> list[dict]:
        """Entry: IOC for a target notional (rounded up one lot to meet the minimum)."""
        ref = book.asks[0][0] if side == "buy" else book.bids[0][0]
        sz = order_size(usd, ref, self._meta, self._min_notional)
        return self._execute(book, side, sz, ts)

    def close(self, book: Book, side: str, size: float, position: float, ts: float) -> list[dict]:
        """Reduce-only IOC: never larger than the position, never in the growing direction."""
        sz = reduce_size(side, size, position, self._meta)
        if sz <= 0:
            return []
        return self._execute(book, side, sz, ts)

    def cancel_all(self) -> int:
        """IOC orders never rest; only a maker quote can."""
        n = 0 if self._quote is None else 1
        self._quote = None
        return n

    # ---- maker quotes ----

    def place_quote(self, side: str, sz: Decimal, px: Decimal, book: Book, ts: float) -> QuoteAck:
        bid, ask = Decimal(str(book.bids[0][0])), Decimal(str(book.asks[0][0]))
        if (px >= ask) if side == "buy" else (px <= bid):
            return QuoteAck("rejected", None, f"post only would have matched, bbo {bid}@{ask}")
        oid = self._next_tid  # one counter for paper tids and quote oids: never the same id
        self._next_tid -= 1
        self._quote = _Quote(oid, side, px, sz, ts)
        return QuoteAck("placed", oid)

    def modify_quote(self, oid: int, side: str, sz: Decimal, px: Decimal, book: Book,
                     ts: float) -> QuoteAck:
        """A modify replaces the order: a new oid and a new place in the queue. A crossing
        modify leaves no order behind. A side change is refused and the order stays."""
        if self._quote is None or self._quote.oid != oid:
            return QuoteAck("gone", None, "no such resting order")
        if self._quote.side != side:
            return QuoteAck("refused", None, "Attempted to modify to invalid new order")
        self._quote = None
        return self.place_quote(side, sz, px, book, ts)

    def cancel_oids(self, oids: list[int]) -> int:
        if self._quote is not None and self._quote.oid in oids:
            self._quote = None
            return 1
        return 0

    def open_quotes(self) -> list[OpenQuote]:
        q = self._quote
        return [] if q is None else [OpenQuote(q.oid, q.side, float(q.px), float(q.remaining))]

    def rate_budget(self) -> None:
        return None  # no address budget on paper

    def match_prints(self, prints: list[Print]) -> list[dict]:
        """Fill the resting quote from trade prints, in order (stored here, like other paper
        fills)."""
        fills = []
        for p in prints:
            q = self._quote
            if q is None:
                break
            if p.ts <= q.placed_ts or p.recv_ts <= q.placed_ts:
                continue
            ppx = Decimal(str(p.px))
            if not ((p.side == "sell" and ppx < q.px) if q.side == "buy"
                    else (p.side == "buy" and ppx > q.px)):
                continue
            take = min(q.remaining, round_size(Decimal(str(p.sz)), self._meta))
            if take <= 0:
                continue
            px, sz = float(q.px), float(take)
            fee = px * sz * MAKER_FEE_RATE
            tid = self._next_tid
            self._next_tid -= 1
            self._store.insert_fill(tid=tid, oid=q.oid, ts=p.recv_ts, coin=self._coin,
                                    side=q.side, sz=sz, px=px, fee=fee)
            fills.append({"tid": tid, "oid": q.oid, "side": q.side, "sz": sz, "px": px,
                          "fee": fee, "ts": p.recv_ts})
            q.remaining -= take
            if q.remaining <= 0:
                self._quote = None
        return fills

    def flatten(self, book: Book, position: float, ts: float) -> list[dict]:
        """Close the whole position (reduce-only IOC)."""
        return self.close(book, "sell" if position > 0 else "buy", abs(position), position, ts)

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
