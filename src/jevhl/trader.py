"""The dry-run decision loop. One decision per fresh l2Book snapshot, one in flight at a time.

Per snapshot: housekeeping (resolve outcomes, run due exits, update P&L and the kill switch),
then, if no decision is in flight and the book is fresh, ask the model with a hard deadline
(late means hold) and act on the policy's answer. Fills come from `paper` here; the same flow
is driven by exchange confirmations from milestone 4.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from .config import Config
from .feed import Book
from .ledger import Ledger, day_start_pnl
from .paper import PaperExchange
from .policy import decide, hold
from .risk import KillSwitch
from .types import HORIZONS_S, Action, Decision, ModelInput

RECENT_MIDS = 20


@dataclass
class _Exit:
    due_ts: float
    side: str  # the closing side
    sz: float


def kill_switch_from_store(cfg: Config, store, clock: Callable[[], float]) -> KillSwitch:
    """Kill switch whose daily-loss baseline is rebuilt from stored fills, so a restart
    cannot forgive losses already taken today."""
    return KillSwitch(cfg, clock, day_start_pnl=day_start_pnl(store, clock()))


class Trader:
    def __init__(
        self,
        cfg: Config,
        store,
        model,
        paper: PaperExchange,
        ledger: Ledger,
        kill: KillSwitch,
        clock: Callable[[], float] = time.time,
        perf: Callable[[], float] = time.perf_counter,
    ):
        self.cfg, self.store, self.ledger = cfg, store, ledger
        self._model, self._paper, self._kill = model, paper, kill
        self._clock, self._perf = clock, perf
        self._inflight = False
        self._last_book: Book | None = None
        self._recent: list[float] = []
        self._exits: list[_Exit] = []
        self.stopped = False
        self.error: BaseException | None = None
        lot = 10.0 ** -paper.meta.sz_decimals
        self._dust = lot / 2
        if abs(ledger.position) >= self._dust:
            # Restarted with an open position: its timers are gone, so close it at the
            # first book rather than leave it open.
            self._exits.append(_Exit(0.0, "sell" if ledger.position > 0 else "buy",
                                     abs(ledger.position)))

    # ---- per snapshot ----

    async def on_book(self, book: Book) -> None:
        if self.stopped:
            return
        self._last_book = book
        self._kill.on_book()
        self._recent = (self._recent + [book.mid])[-RECENT_MIDS:]
        self._resolve_outcomes(book)
        self._run_exits(book)
        if self._risk(book):
            return
        if self._inflight:
            self.store.log_event(self._clock(), "skip", "decision in flight")
            return
        self._inflight = True
        try:
            await self._decide(book)
        finally:
            self._inflight = False
        self._risk(book)

    def _resolve_outcomes(self, book: Book) -> None:
        """Outcome mid = the first book at or after decision ts + horizon, but only if it
        arrived within stale_s of that mark (after an outage the decision stays unscored)."""
        for h in HORIZONS_S:
            lo = book.recv_ts - h - self.cfg.stale_s
            for row in self.store.pending(h, lo, book.recv_ts - h):
                self.store.resolve(row["id"], h, book.mid)

    def _run_exits(self, book: Book) -> None:
        keep = []
        for ex in self._exits:
            if ex.due_ts > book.recv_ts:
                keep.append(ex)
                continue
            fills = self._paper.close(book, ex.side, ex.sz, self.ledger.position, book.recv_ts)
            self._apply(fills)
            left = ex.sz - sum(f["sz"] for f in fills)
            if left >= self._dust and abs(self.ledger.position) >= self._dust:
                keep.append(_Exit(ex.due_ts, ex.side, left))  # partial: retry at next book
        self._exits = keep

    def _apply(self, fills: list[dict]) -> None:
        for f in fills:
            self.ledger.apply_fill(f["side"], f["sz"], f["px"], f["fee"])

    def _risk(self, book: Book) -> bool:
        """Update P&L, check the kill switch. Returns True if the bot halted."""
        self._kill.update_pnl(self.ledger.pnl(book.mid))
        if self._kill.tripped:
            self._halt(self._kill.reason)
            return True
        return False

    def check_stale(self) -> str | None:
        """Watchdog, called about once a second: no book for stale_s trips the kill switch."""
        if self.stopped:
            return self._kill.reason
        reason = self._kill.check()
        if reason:
            self._halt(reason)
        return reason

    # ---- decisions ----

    async def _decide(self, book: Book) -> None:
        cfg = self.cfg
        ts = book.recv_ts
        if self._clock() - book.recv_ts > cfg.stale_s:
            d = hold("stale", ts=ts, gate=cfg.gate)
            self.store.insert_decision(d, book.mid)
            return
        inp = ModelInput(ts, book.coin, book.mid, book.spread_bps,
                         book.bids, book.asks, list(self._recent), cfg.horizon_s)
        d = await self._ask(inp, ts)
        decision_id = self.store.insert_decision(d, book.mid)
        if d.action is Action.HOLD:
            return
        self._enter(book, d, decision_id)

    async def _ask(self, inp: ModelInput, ts: float) -> Decision:
        cfg = self.cfg
        t0 = self._perf()
        try:
            out = await asyncio.wait_for(
                asyncio.to_thread(self._model.predict, inp), cfg.deadline_s
            )
        except TimeoutError:
            ms = max((self._perf() - t0) * 1000, cfg.deadline_s * 1000)
            return hold("late", ts=ts, gate=cfg.gate, latency_ms=ms)
        except Exception as e:  # a broken model must not stop the bot: hold and log
            self.store.log_event(self._clock(), "model_error", f"{type(e).__name__}: {e}")
            return hold("invalid", ts=ts, gate=cfg.gate, latency_ms=(self._perf() - t0) * 1000)
        latency_ms = (self._perf() - t0) * 1000
        position_usd = self.ledger.position * inp.mid
        return decide(out.p_up, position_usd, cfg, ts=ts, latency_ms=latency_ms)

    def _enter(self, book: Book, d: Decision, decision_id: int) -> None:
        side = d.action.value
        try:
            fills = self._paper.open(book, side, d.size_usd, book.recv_ts)
        except Exception as e:
            self._kill.on_order_result(False)
            self.store.log_event(self._clock(), "order_error", f"{type(e).__name__}: {e}")
            return
        self._kill.on_order_result(True)
        if not fills:
            self.store.log_event(self._clock(), "no_fill", f"decision {decision_id}")
            return
        self._apply(fills)
        self._exits.append(
            _Exit(book.recv_ts + self.cfg.horizon_s, "sell" if side == "buy" else "buy",
                  sum(f["sz"] for f in fills))
        )

    # ---- stopping ----

    def _flatten(self) -> None:
        book = self._last_book
        pos = self.ledger.position
        if book is None or abs(pos) < self._dust:
            return
        side = "sell" if pos > 0 else "buy"
        self._apply(self._paper.close(book, side, abs(pos), pos, self._clock()))
        self._exits = []

    def _halt(self, reason: str | None) -> None:
        self.store.log_event(self._clock(), "kill_switch", reason or "")
        self._flatten()
        self.stopped = True

    async def shutdown(self) -> None:
        """Clean shutdown: flatten, stop."""
        self._flatten()
        self.stopped = True

    async def run(self, books: AsyncIterator[Book]) -> None:
        """Consume snapshots until the stream ends or the bot stops. Each snapshot is handled
        in its own task so a slow decision never blocks the reader (it is skipped instead)."""
        tasks: set[asyncio.Task] = set()

        def done(t: asyncio.Task) -> None:
            tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                self.error = t.exception()
                self.stopped = True

        try:
            async for book in books:
                if self.stopped:
                    break
                t = asyncio.create_task(self.on_book(book))
                tasks.add(t)
                t.add_done_callback(done)
        finally:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await self.shutdown()
        if self.error is not None:
            raise self.error
