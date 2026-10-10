"""The maker strategy (STRATEGY=maker, milestone 8).

About once a second, on a REST book plus the trades since the last decision: ask the model,
then keep one post-only quote on its side (buy if p_up >= 0.5, else sell), one tick inside the
touch. The quote is replaced (one modify) only when its side or price changes, so an
unchanged quote keeps its queue place. Our own resting order is removed from the book before
pricing and prompting: it is the touch in the next book, and pricing off it would walk the
quote a tick further every second.

Shared with the taker (inherited from Trader): the loop, outcomes, fill booking (idempotent
on tid), the kill switch, the M6 flatten retry and the M7 stale pause. The maker adds:
- every stop (stale pause, kill, shutdown) cancels all resting orders first, never blocked by
  the action cap;
- at most MAKER_ACTIONS_PER_MIN order actions in any 60 s (SPEC hard constraint), one slot kept
  for an emergency cancel;
- a rate-limit error cancels, counts as an order error and pauses quoting for RATE_PAUSE_S;
- live, fills are polled every MAKER_FILL_POLL_S and the open orders re-read every
  ORDERS_RECONCILE_S (a modify's reply is unverified: after an unclear answer the quote is
  "unknown" until the open orders are re-read);
- paper, the resting quote fills from trade prints that go through its price;
- calibration scores a decision only when the book it saw differs from the last scored one.
No time exits: the position is reduced by quoting the other side.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from decimal import Decimal

from .exchange import RateLimited
from .feed import Book, Print, TradeTape
from .latency import percentile
from .policy import MAKER_GATE, hold, maker_side
from .prompt import (
    DEPTH_BANDS_BPS,
    FLOW_WINDOW_S,
    IMBALANCE_BAND_PCT,
    MID_SAMPLE_S,
    MID_SPAN_S,
    N_PRINTS,
    RETURN_WINDOWS_S,
)
from .rounding import AssetMeta, maker_price, order_size
from .trader import Trader
from .types import HORIZONS_S, Action, Decision, MakerInput, ModelInput, QuoteAck

MAKER_ACTIONS_PER_MIN = 20  # SPEC hard constraint, not configurable
ACTION_WINDOW_S = 60.0
MAKER_BOOK_MAX_AGE_S = 3.0  # never send a quote priced on an older book
MAKER_FILL_POLL_S = 5.0  # live: read the exchange's fills this often
ORDERS_RECONCILE_S = 15.0  # live: re-read the open orders this often
BUDGET_EVERY_S = 60.0  # live: log the address request budget this often
LATENCY_EVERY_S = 60.0
RATE_PAUSE_S = 60.0  # no quotes for this long after a rate-limit error
LATENCY_KEEP = 3600  # samples kept per latency series (about an hour)
UNKNOWN = "unknown"  # quote tracking after an unclear answer: re-read the open orders


@dataclass
class Quote:
    oid: int
    side: str
    px: Decimal
    remaining: float
    placed_ts: float


# ---- model input ----


def _mid_at(mids: list[tuple[float, float]], t: float) -> float | None:
    """The last mid at or before t (mids are (ts, mid), oldest first), None if none."""
    out = None
    for ts, m in mids:
        if ts > t:
            break
        out = m
    return out


def _size_within(levels: list[list[float]], mid: float, frac: float) -> float:
    return sum(sz for px, sz in levels if abs(px - mid) <= mid * frac)


def build_input(book: Book, mids: list[tuple[float, float]], tape: TradeTape,
                recent: list[float], horizon_s: float) -> MakerInput:
    """mids: (recv_ts, mid) of every book so far, oldest first, ending with this one. Returns
    and sampled mids are taken by time, not by count, since most seconds repeat a book."""
    now, mid = book.recv_ts, book.mid
    band = IMBALANCE_BAND_PCT / 100
    b, a = _size_within(book.bids, mid, band), _size_within(book.asks, mid, band)
    returns = {}
    for w in RETURN_WINDOWS_S:
        past = _mid_at(mids, now - w)
        returns[w] = None if past is None else (mid / past - 1) * 1e4
    steps = int(MID_SPAN_S // MID_SAMPLE_S)
    return MakerInput(
        ts=now, coin=book.coin, mid=mid, spread_bps=book.spread_bps,
        bids=book.bids, asks=book.asks, recent_mids=list(recent), horizon_s=horizon_s,
        imbalance=(b - a) / (b + a) if b + a > 0 else 0.0,
        depth={bps: (_size_within(book.bids, mid, bps / 1e4),
                     _size_within(book.asks, mid, bps / 1e4)) for bps in DEPTH_BANDS_BPS},
        returns_bps=returns,
        sampled_mids=[_mid_at(mids, now - k * MID_SAMPLE_S) for k in range(steps, -1, -1)],
        flow=tape.flow(FLOW_WINDOW_S, now),
        prints=[(now - p.ts, p.side, p.px, p.sz) for p in tape.recent(N_PRINTS)],
    )


def without_own(book: Book, quote, meta: AssetMeta) -> Book | None:
    """The book with our resting quote's remaining size taken out of its price level (the
    level is dropped if nothing else rests there). None if that empties the side. Without a
    known quote (none, or unknown) the book is returned as it is."""
    if not isinstance(quote, Quote):
        return book
    levels = book.bids if quote.side == "buy" else book.asks
    out, done = [], False
    for px, sz in levels:
        if not done and Decimal(str(px)) == quote.px:
            done = True
            left = round(sz - quote.remaining, meta.sz_decimals)
            if left < 10.0 ** -meta.sz_decimals / 2:
                continue
            sz = left
        out.append([px, sz])
    if not out:
        return None
    return replace(book, bids=out) if quote.side == "buy" else replace(book, asks=out)


# ---- the trader ----


class MakerTrader(Trader):
    def __init__(self, *args, tape: TradeTape | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.tape = tape if tape is not None else TradeTape()
        self._tape_cursor = 0
        self._quote: Quote | str | None = None
        self._actions: list[float] = []  # order actions sent, for the per-minute cap
        self.rate_paused_until = 0.0
        self._mids: deque[tuple[float, float]] = deque()
        self._last_scored: tuple | None = None  # the book of the last scored decision
        self._score_ids: dict[int, float] = {}  # decision id -> ts, outcomes resolved for these
        self._last_reconcile = self._last_budget = self._last_latency = 0.0
        self.budget: dict | None = None
        self.capped = False
        self.latency: dict[str, deque] = {k: deque(maxlen=LATENCY_KEEP)
                                          for k in ("rest", "action", "loop")}

    @property
    def _gate(self) -> float:
        return MAKER_GATE

    def _cover_residue(self, due_ts: float = 0.0) -> None:
        """No time exits in maker mode: the position is reduced by quoting the other side."""

    async def follow_trades(self, stream: AsyncIterator[list[Print]]) -> None:
        """Feed the trade tape from the trades stream (run as its own task)."""
        async for prints in stream:
            self.tape.add(prints, self._clock())

    # ---- per book ----

    async def on_book(self, book: Book) -> None:
        if not self.stopped:
            self._mids.append((book.recv_ts, book.mid))
            keep_from = book.recv_ts - max(RETURN_WINDOWS_S) - 10
            while self._mids and self._mids[0][0] < keep_from:
                self._mids.popleft()
            if book.read_ms is not None:
                self.latency["rest"].append(book.read_ms)
        await super().on_book(book)
        if self._clock() - self._last_latency >= LATENCY_EVERY_S:
            self._last_latency = self._clock()
            self.store.log_event(self._clock(), "maker_latency", json.dumps(self.latency_summary()))

    def _resolve_outcomes(self, book: Book) -> None:
        """As the taker's, but only for the decisions chosen for scoring (see _mark_scored),
        and with the market's mid: our own quote removed, as for the decision's mid."""
        mid = (self._seen(book) or book).mid
        for h in HORIZONS_S:
            lo = book.recv_ts - h - self.cfg.stale_s
            for row in self.store.pending(h, lo, book.recv_ts - h):
                if row["id"] in self._score_ids:
                    self.store.resolve(row["id"], h, mid)
        oldest = book.recv_ts - max(HORIZONS_S) - self.cfg.stale_s
        self._score_ids = {i: t for i, t in self._score_ids.items() if t >= oldest}

    def _mark_scored(self, decision_id: int, d: Decision, seen: Book) -> None:
        """Score a decision only if the book it saw differs from the last scored one, so the
        ~60 decisions on one repeated book do not count 60 times (user decision 2026-10-10)."""
        if d.p_up is None:
            return
        key = (tuple(map(tuple, seen.bids)), tuple(map(tuple, seen.asks)))
        if key != self._last_scored:
            self._last_scored = key
            self._score_ids[decision_id] = d.ts

    # ---- decisions ----

    def _policy(self, p_up: float, inp: ModelInput, ts: float, latency_ms: float) -> Decision:
        return maker_side(p_up, self._position_usd(inp.mid), self.cfg, ts=ts,
                          latency_ms=latency_ms)

    def _seen(self, book: Book) -> Book | None:
        """The book as the model and the pricing see it (live: our own order removed)."""
        if not self._executor.live:
            return book
        return without_own(book, self._quote, self._executor.meta)

    async def _decide(self, book: Book) -> None:
        """Decisions store the market's mid (our own quote removed): live, our quote one tick
        inside the touch moves the raw mid toward our side, which follows p_up, and would
        bias the calibration labels."""
        cfg, ts = self.cfg, book.recv_ts
        seen = self._seen(book) or book
        if self._clock() - ts > cfg.stale_s:
            self.store.insert_decision(hold("stale", ts=ts, gate=self._gate), seen.mid)
            return
        if self._clock() < self.rate_paused_until:
            self.store.insert_decision(hold("rate_paused", ts=ts, gate=self._gate), seen.mid)
            return
        inp = build_input(seen, list(self._mids), self.tape, list(self._recent), cfg.horizon_s)
        d = await self._ask(inp, ts)
        if self.stopped or self._kill.tripped:  # tripped while the model was thinking
            d = hold("killed", ts=ts, gate=self._gate, p_up=d.p_up, latency_ms=d.latency_ms)
        elif self._paused:
            d = hold("paused", ts=ts, gate=self._gate, p_up=d.p_up, latency_ms=d.latency_ms)
        decision_id = self.store.insert_decision(d, seen.mid)
        self._mark_scored(decision_id, d, seen)
        self.capped = d.reason == "capped"
        if d.action is Action.HOLD:  # late or invalid: the resting quote stays as it is
            return
        await self._quote_to(book, d.action.value, decision_id)

    def _blocked(self) -> bool:
        return (self.stopped or self._kill.tripped or self._paused
                or self._clock() < self.rate_paused_until)

    def _skip(self, decision_id: int, why: str) -> None:
        self.store.log_event(self._clock(), "quote_skipped", f"decision {decision_id}: {why}")

    async def _quote_to(self, book: Book, side: str, decision_id: int) -> None:
        """Keep one quote at the price for `side`: nothing if it is already there, else one
        modify (or one place if nothing rests)."""
        ex, meta = self._executor, self._executor.meta
        async with self._lock:
            if self._blocked():  # halted, paused or rate-paused while waiting for the lock
                return self._skip(decision_id, "halted or paused")
            if self._clock() - book.recv_ts > MAKER_BOOK_MAX_AGE_S:
                return self._skip(decision_id, "old book")
            if self._quote == UNKNOWN:
                await self._reconcile_locked()
                if self._blocked() or self._quote == UNKNOWN:
                    return self._skip(decision_id, "open orders unknown")
            view = self._seen(book)
            if view is None:
                return self._skip(decision_id, "our order is the whole side")
            px = maker_price(side, view.bids[0][0], view.asks[0][0], meta)
            q = self._quote
            if q is not None and q.side == side and q.px == px:
                return  # unchanged: keep the queue place
            if not self._room():
                self.store.log_event(self._clock(), "action_cap",
                                     f"decision {decision_id}: {side} @ {px} not sent")
                return
            sz = order_size(self.cfg.maker_size_usd, float(px), meta, self.cfg.min_notional_usd)
            self.latency["loop"].append((self._clock() - book.recv_ts) * 1000)
            self._note_action()
            t0 = self._perf()
            try:
                if q is None:
                    ack = await self._run(ex.place_quote, side, sz, px, book, self._clock())
                else:
                    ack = await self._run(ex.modify_quote, q.oid, side, sz, px, book,
                                          self._clock())
            except RateLimited as e:
                await self._rate_limited_locked(e)
                return
            except Exception as e:  # it may or may not rest: re-read before the next quote
                self._order_failed(e, "quote")
                self._quote = UNKNOWN
                return
            self.latency["action"].append((self._perf() - t0) * 1000)
            await self._on_ack(ack, side, sz, px, q)

    async def _on_ack(self, ack: QuoteAck, side: str, sz: Decimal, px: Decimal,
                      prev: Quote | None) -> None:
        """Track the quote from the venue's answer (caller holds the lock). Every answer
        means the venue is reachable, so none counts as an order error."""
        self._kill.on_order_result(True)
        what = "place" if prev is None else "modify"
        if ack.status == "placed":
            self._quote = Quote(ack.oid, side, px, float(sz), self._clock())
            self.store.log_event(self._clock(), f"quote_{what}",
                                 f"{side} {sz} @ {px} oid {ack.oid}")
        elif ack.status == "rejected":  # would have crossed: re-priced at the next decision
            self.store.log_event(self._clock(), "quote_rejected", f"{what} {side} @ {px}: "
                                 f"{ack.detail}")
            self._quote = None if prev is None else UNKNOWN  # a failed modify may leave it
        elif ack.status == "gone":  # the order we modified had filled or was cancelled
            self.store.log_event(self._clock(), "quote_gone", f"oid {prev.oid if prev else '?'}")
            self._quote = None
            await self._sync_locked()
        else:  # filled (a post-only order should never take) or a reply without a status
            self.store.log_event(self._clock(), f"quote_{ack.status}", ack.detail)
            self._quote = UNKNOWN
            await self._sync_locked()

    async def _sync_locked(self) -> None:
        """Book the exchange's fills now (live; caller holds the lock)."""
        if not self._executor.live or self.stopped:
            return
        try:
            self._apply(await self._run(self._executor.sync_fills))
        except Exception as e:
            self.store.log_event(self._clock(), "sync_error", f"{type(e).__name__}: {e}")

    def _apply(self, fills: list[dict]) -> list[dict]:
        """Book fills; a fill of the tracked quote lowers what still rests."""
        done = super()._apply(fills)
        q = self._quote
        if isinstance(q, Quote):
            filled = sum(f["sz"] for f in done if f.get("oid") == q.oid)
            if filled:
                q.remaining -= filled
                if q.remaining < self._dust:
                    self._quote = None
        return done

    # ---- the action cap and rate limits ----

    def _room(self) -> bool:
        """A quote action fits in the last 60 s with one slot kept for an emergency cancel."""
        now = self._clock()
        self._actions = [t for t in self._actions if t > now - ACTION_WINDOW_S]
        return len(self._actions) + 1 <= MAKER_ACTIONS_PER_MIN - 1

    def _note_action(self) -> None:
        self._actions.append(self._clock())

    async def _rate_limited_locked(self, e: Exception) -> None:
        """Rate limited: count it as an order error (three in a row stop the bot), cancel the
        quote and send no quotes for RATE_PAUSE_S. No flatten (user decision 2026-10-10)."""
        self._kill.on_order_result(False)
        self.rate_paused_until = self._clock() + RATE_PAUSE_S
        self.store.log_event(self._clock(), "rate_limited",
                             f"{e}; cancelling, no quotes for {RATE_PAUSE_S:g} s")
        await self._cancel_all_locked("rate limit")

    async def _cancel_all_locked(self, why: str) -> None:
        """Cancel every resting order on the coin (caller holds the lock). Counted, but never
        blocked by the action cap."""
        self._note_action()
        try:
            n = await self._run(self._executor.cancel_all)
        except Exception as e:
            self._quote = UNKNOWN
            self.store.log_event(self._clock(), "cancel_error", f"{why}: {type(e).__name__}: {e}")
            return
        self._quote = None
        self.store.log_event(self._clock(), "quote_cancel", f"{why}: {n} order(s)")

    # ---- live fills and open orders ----

    async def _poll_fills(self) -> None:
        """Paper: fill the quote from the prints since the last book. Live: read the fills
        every MAKER_FILL_POLL_S, the open orders every ORDERS_RECONCILE_S and the address
        budget every BUDGET_EVERY_S."""
        if not self._executor.live:
            prints, self._tape_cursor = self.tape.since(self._tape_cursor)
            if prints and not self.stopped:
                self._apply(self._executor.match_prints(prints))
            return
        now = self._clock()
        if now - self._last_poll >= MAKER_FILL_POLL_S:
            self._last_poll = now
            async with self._lock:
                await self._sync_locked()
        if now - self._last_reconcile >= ORDERS_RECONCILE_S:
            self._last_reconcile = now
            async with self._lock:
                if not (self.stopped or self._paused):  # a pause checks orders at resume
                    await self._reconcile_locked()
        if now - self._last_budget >= BUDGET_EVERY_S:
            self._last_budget = now
            async with self._lock:
                if self.stopped:
                    return
                try:
                    self.budget = await self._run(self._executor.rate_budget)
                except Exception as e:
                    self.store.log_event(self._clock(), "sync_error",
                                         f"budget: {type(e).__name__}: {e}")
                    return
                self.store.log_event(self._clock(), "address_budget",
                                     f"{self.budget['used']}/{self.budget['cap']}")

    async def _reconcile_locked(self) -> None:
        """Re-read our open orders (caller holds the lock). The tracked quote's remaining
        size comes from the exchange; a tracked quote that is gone has filled or was cancelled
        (its fills are read now); any other order on the coin is cancelled as a stray. An
        unknown quote adopts a single open order, and cancels several."""
        try:
            orders = await self._run(self._executor.open_quotes)
        except Exception as e:
            self.store.log_event(self._clock(), "sync_error", f"orders: {type(e).__name__}: {e}")
            return
        if self.stopped:
            return
        q = self._quote
        mine = None
        if isinstance(q, Quote):
            mine = next((o for o in orders if o.oid == q.oid), None)
        elif q == UNKNOWN and len(orders) == 1:
            mine = orders[0]
        strays = [o.oid for o in orders if o is not mine]
        if strays:
            self._note_action()
            try:
                await self._run(self._executor.cancel_oids, strays)
            except Exception as e:
                self.store.log_event(self._clock(), "cancel_error",
                                     f"strays {strays}: {type(e).__name__}: {e}")
                return
            self.store.log_event(self._clock(), "stray_order", f"cancelled {strays}")
        if mine is not None:
            placed = q.placed_ts if isinstance(q, Quote) else self._clock()
            if not isinstance(q, Quote):
                self.store.log_event(self._clock(), "quote_adopted", f"oid {mine.oid}")
            self._quote = Quote(mine.oid, mine.side, Decimal(str(mine.px)), mine.sz, placed)
            return
        self._quote = None
        if isinstance(q, Quote):
            self.store.log_event(self._clock(), "quote_vanished", f"oid {q.oid}")
            await self._sync_locked()

    # ---- stopping ----

    async def _flatten(self) -> None:
        """Cancel every resting order first, then close as the taker does (live, each flatten
        attempt cancels again). Paper needs the cancel here: its flatten does nothing when the
        ledger is flat."""
        async with self._lock:
            await self._cancel_all_locked("stop")
        await super()._flatten()
        self._quote = UNKNOWN if self.flatten_unresolved else None

    async def _is_flat(self) -> bool:
        """A resume also needs no order resting on the coin."""
        if not await super()._is_flat():
            return False
        async with self._lock:
            try:
                orders = await self._run(self._executor.open_quotes)
            except Exception as e:
                self.store.log_event(self._clock(), "sync_error", f"{type(e).__name__}: {e}")
                return False
        if orders:
            self.store.log_event(self._clock(), "orders_resting", f"{len(orders)} at resume")
            return False
        return True

    # ---- reporting ----

    def latency_summary(self) -> dict:
        """n, p50, p90 and max (ms) of REST reads, quote action round trips and book-to-action
        times, over the last LATENCY_KEEP samples of each."""
        out = {}
        for k, xs in self.latency.items():
            v = list(xs)
            out[k] = {"n": len(v), "p50": percentile(v, 50), "p90": percentile(v, 90),
                      "max": max(v) if v else None}
        return out

    def maker_view(self) -> dict:
        """The dashboard's maker panel."""
        now, q = self._clock(), self._quote
        if isinstance(q, Quote):
            quote: dict | str | None = {"side": q.side, "px": float(q.px), "remaining": q.remaining,
                                        "oid": q.oid, "age_s": now - q.placed_ts}
        else:
            quote = q
        return {
            "quote": quote,
            "actions_last_min": sum(1 for t in self._actions if t > now - ACTION_WINDOW_S),
            "actions_cap": MAKER_ACTIONS_PER_MIN,
            "rate_paused_until": self.rate_paused_until if self.rate_paused_until > now else None,
            "budget": self.budget,
            "capped": self.capped,
        }
