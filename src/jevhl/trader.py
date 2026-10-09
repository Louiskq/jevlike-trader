"""The dry-run decision loop. One decision per fresh l2Book snapshot, one in flight at a time.

Per snapshot: housekeeping (resolve outcomes, run due exits, update P&L and the kill switch),
then, if no decision is in flight and the book is fresh, ask the model with a hard deadline
(late means hold) and act on the policy's answer.

The executor is either `paper` (simulated, synchronous) or `exchange` (live testnet, blocking
HTTP run in a worker thread). Live accounting follows exchange confirmations only: each
executor call runs under one lock (so a halt can never interleave with an order), its
confirmed fills are stored (idempotent on tid) and booked before the lock is released, and a
periodic poll books any fill an order call did not return. After every await the stop state is
checked again before acting on the result.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

from .config import Config
from .feed import Book
from .ledger import Ledger, day_start_pnl
from .policy import decide, hold
from .risk import KillSwitch
from .types import HORIZONS_S, Action, Decision, ModelInput

RECENT_MIDS = 20
POLL_S = 15.0  # live backstop: re-read the exchange's fills this often
MAX_ORDERS_PER_MIN = 2  # SPEC rate constraint, live only; entries reserve room for their exit
RATE_WINDOW_S = 60.0
FLATTEN_RETRY_FIRST_S = 1.0  # first wait between flatten retries; doubles up to the cap
FLATTEN_RETRY_CAP_S = 30.0


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
        executor,
        ledger: Ledger,
        kill: KillSwitch,
        clock: Callable[[], float] = time.time,
        perf: Callable[[], float] = time.perf_counter,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.cfg, self.store, self.ledger = cfg, store, ledger
        self._model, self._executor, self._kill = model, executor, kill
        self._lock = asyncio.Lock()  # one executor call at a time
        self._flatten_lock = asyncio.Lock()  # one flatten (and its retries) at a time
        self._sleep = sleep
        self._retry_spent = False  # a retry sequence already ran out; later flattens try once
        self.flatten_unresolved = False  # the last live flatten gave up: position may be open
        # A stale-book kill pauses instead of stopping (see _pause). Counted for the report.
        self._paused = False
        self._ok_since: float | None = None  # start of the current run of steady books
        self._resuming = False  # a resume check is running (it awaits the exchange)
        self.stale_pauses = 0
        self.stale_resumes = 0
        self.abandoned_threads: list[threading.Thread] = []  # cut-off flatten calls, see above
        self._last_poll = 0.0
        self._clock, self._perf = clock, perf
        self._inflight = False
        self._model_task: asyncio.Future | None = None  # the worker thread's call, if running
        self._last_book: Book | None = None
        self._recent: list[float] = []
        self._exits: list[_Exit] = []
        self._claimed = 0  # due exits taken by _run_exits and not yet sent
        self._order_times: list[float] = []  # live order sends, for the rate guard
        self._stop = asyncio.Event()
        self.error: BaseException | None = None
        lot = 10.0 ** -executor.meta.sz_decimals
        self._dust = lot / 2
        # Restarted with an open position: its timers are gone, so close it at the first book.
        self._cover_residue()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    @stopped.setter
    def stopped(self, value: bool) -> None:
        if value:
            self._stop.set()
            abort = getattr(self._model, "abort", None)
            if abort is not None:  # wake a model request in flight
                abort()
        else:
            self._stop.clear()

    @property
    def paused(self) -> bool:
        """Paused after a stale-book kill: no new orders, the feed is still read. A stop
        (shutdown, or a permanent kill while paused) ends the pause."""
        return self._paused and not self.stopped

    @property
    def last_book(self) -> Book | None:
        return self._last_book

    @property
    def kill_reason(self) -> str | None:
        return self._kill.reason

    # ---- per snapshot ----

    async def on_book(self, book: Book) -> None:
        if self.stopped:
            return
        prev, self._last_book = self._last_book, book
        self._kill.on_book()
        self._recent = (self._recent + [book.mid])[-RECENT_MIDS:]
        self._resolve_outcomes(book)
        await self._poll_fills()
        await self._run_exits(book)
        if self.stopped or await self._risk(book):
            return
        if self._paused:
            await self._settle(book, prev)
            if self.stopped:
                return
            if self._paused:
                self.store.insert_decision(
                    hold("paused", ts=book.recv_ts, gate=self.cfg.gate), book.mid
                )
                return
        if self._inflight:
            self.store.log_event(self._clock(), "skip", "decision in flight")
            return
        self._inflight = True
        try:
            await self._decide(book)
        finally:
            self._inflight = False
        await self._risk(book)

    def _resolve_outcomes(self, book: Book) -> None:
        """Outcome mid = the first book at or after decision ts + horizon, but only if it
        arrived within stale_s of that mark (after an outage the decision stays unscored)."""
        for h in HORIZONS_S:
            lo = book.recv_ts - h - self.cfg.stale_s
            for row in self.store.pending(h, lo, book.recv_ts - h):
                self.store.resolve(row["id"], h, book.mid)

    async def _run(self, fn, *args):
        """An executor call: in a worker thread if it blocks on the network."""
        if self._executor.live:
            return await asyncio.to_thread(fn, *args)
        return fn(*args)

    async def _run_abandonable(self, fn, *args):
        """Like _run, but in a daemon thread that a cancel may leave behind, so a Ctrl-C
        during a blocked HTTP call returns at once (a default-executor thread would make
        asyncio.run wait for it, 20-50 s). The thread is not orphaned: it is kept in
        abandoned_threads and logged, and it dies with the process. Whatever it still does
        (an order sent, its fills) is not booked here; the next start reads those fills back
        from the exchange."""
        if not self._executor.live:
            return fn(*args)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()

        def settle(result, error) -> None:
            if fut.done():  # cancelled meanwhile
                return
            if error is not None:
                fut.set_exception(error)
            else:
                fut.set_result(result)

        def work() -> None:
            try:
                result, error = fn(*args), None
            except Exception as e:
                result, error = None, e
            try:
                loop.call_soon_threadsafe(settle, result, error)
            except RuntimeError:  # the loop is closed: nobody is waiting any more
                pass

        thread = threading.Thread(target=work, daemon=True, name=f"abandonable-{fn.__name__}")
        thread.start()
        try:
            return await fut
        except asyncio.CancelledError:
            self.abandoned_threads = [t for t in self.abandoned_threads if t.is_alive()]
            self.abandoned_threads.append(thread)
            self.store.log_event(
                self._clock(), "flatten_attempt_abandoned", f"{fn.__name__} still running"
            )
            raise

    def _apply(self, fills: list[dict]) -> list[dict]:
        """Book fills. A live executor leaves storage to us: a fill whose tid is already
        stored is skipped, so re-reading the same fills can never double-count."""
        done = []
        for f in fills:
            if self._executor.live and not self.store.insert_fill(
                tid=f["tid"], oid=f["oid"], ts=f["ts"], coin=self.cfg.coin, side=f["side"],
                sz=f["sz"], px=f["px"], fee=f["fee"],
            ):
                continue
            self.ledger.apply_fill(f["side"], f["sz"], f["px"], f["fee"])
            done.append(f)
        return done

    def _order_failed(self, e: Exception, what: str) -> list[dict]:
        """An executor call raised: count it, log it, and still book the fills it did
        confirm (an order can partly fill before the error)."""
        applied = self._apply(getattr(e, "fills", []))
        self._kill.on_order_result(False)
        self.store.log_event(self._clock(), "order_error", f"{what}: {type(e).__name__}: {e}")
        return applied

    def _note_order(self) -> None:
        self._order_times.append(self._clock())

    def _rate_limited(self) -> bool:
        """Live entries only: an entry is allowed when the orders sent in the last minute, plus
        every exit still owed, plus this entry and its own exit, fit in the budget. Exits are
        never gated, so the budget can never leave a position open."""
        if not self._executor.live:
            return False
        now = self._clock()
        self._order_times = [t for t in self._order_times if t > now - RATE_WINDOW_S]
        owed = len(self._exits) + self._claimed
        return len(self._order_times) + owed + 2 > MAX_ORDERS_PER_MIN

    def _cover_residue(self, due_ts: float = 0.0) -> None:
        """Give every part of the position that has no exit timer an exit at due_ts (0: now)."""
        covered = sum(e.sz if e.side == "sell" else -e.sz for e in self._exits)
        uncovered = self.ledger.position - covered
        if abs(uncovered) >= self._dust:
            self._exits.append(_Exit(due_ts, "sell" if uncovered > 0 else "buy", abs(uncovered)))

    async def _poll_fills(self) -> None:
        """Live backstop: book fills no order call returned (a confirmation that timed out, a
        trade made in the UI)."""
        if not self._executor.live or self._clock() - self._last_poll < POLL_S:
            return
        self._last_poll = self._clock()
        async with self._lock:
            if self.stopped:
                return
            try:
                fills = await self._run(self._executor.sync_fills)
            except Exception as e:
                self.store.log_event(self._clock(), "sync_error", f"{type(e).__name__}: {e}")
                return
            if applied := self._apply(fills):
                self.store.log_event(self._clock(), "fill_backstop", "booked unreturned fills")
                # like any entry, closed horizon_s after it filled (not at the next book)
                self._cover_residue(max(f["ts"] for f in applied) + self.cfg.horizon_s)

    async def _run_exits(self, book: Book) -> None:
        due = [e for e in self._exits if e.due_ts <= book.recv_ts]
        self._exits = [e for e in self._exits if e.due_ts > book.recv_ts]  # claimed: no double send
        self._claimed += len(due)
        try:
            await self._send_exits(book, due)
        finally:
            self._claimed -= len(due)

    async def _send_exits(self, book: Book, due: list[_Exit]) -> None:
        for ex in due:
            async with self._lock:
                if self.stopped or self._paused:  # halted while waiting: the flatten owns it now
                    return
                try:
                    fills = await self._run(
                        self._executor.close, book, ex.side, ex.sz, self.ledger.position,
                        book.recv_ts,
                    )
                    self._note_order()
                    self._kill.on_order_result(True)
                    applied = self._apply(fills)
                except Exception as e:
                    applied = self._order_failed(e, "exit")
                left = ex.sz - sum(f["sz"] for f in applied)
                if left >= self._dust and abs(self.ledger.position) >= self._dust:
                    self._exits.append(_Exit(ex.due_ts, ex.side, left))  # retry at next book

    async def _risk(self, book: Book) -> bool:
        """Update P&L, check the kill switch. Returns True if the bot halted."""
        self._kill.update_pnl(self.ledger.pnl(book.mid))
        if self._kill.tripped:
            await self._halt(self._kill.reason)
            return True
        return False

    async def check_stale(self) -> str | None:
        """Watchdog, called about once a second: no book for stale_s trips the kill switch."""
        if self.stopped:
            return self._kill.reason
        if self._paused:  # settling is judged from the gaps between books, see _settle
            return None
        reason = self._kill.check()
        if reason:
            await self._halt(reason)
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
        if self.stopped or self._kill.tripped:  # tripped while the model was thinking
            d = hold("killed", ts=ts, gate=cfg.gate, p_up=d.p_up, latency_ms=d.latency_ms)
        elif self._paused:  # paused while the model was thinking
            d = hold("paused", ts=ts, gate=cfg.gate, p_up=d.p_up, latency_ms=d.latency_ms)
        decision_id = self.store.insert_decision(d, book.mid)
        if d.action is Action.HOLD:
            return
        await self._enter(book, d, decision_id)

    async def _ask(self, inp: ModelInput, ts: float) -> Decision:
        cfg = self.cfg
        if self._model_task is not None and not self._model_task.done():
            # A timed-out call is still running in its thread: never start a second one.
            return hold("late", ts=ts, gate=cfg.gate, latency_ms=cfg.deadline_s * 1000)
        t0 = self._perf()
        task = asyncio.ensure_future(asyncio.to_thread(self._model.predict, inp))
        task.add_done_callback(lambda t: t.cancelled() or t.exception())  # mark retrieved
        self._model_task = task
        try:
            out = await asyncio.wait_for(asyncio.shield(task), cfg.deadline_s)
        except TimeoutError:
            ms = max((self._perf() - t0) * 1000, cfg.deadline_s * 1000)
            return hold("late", ts=ts, gate=cfg.gate, latency_ms=ms)
        except Exception as e:  # a broken model must not stop the bot: hold and log
            self.store.log_event(self._clock(), "model_error", f"{type(e).__name__}: {e}")
            return hold("invalid", ts=ts, gate=cfg.gate, latency_ms=(self._perf() - t0) * 1000)
        latency_ms = (self._perf() - t0) * 1000
        pos = self.ledger.position
        position_usd = (0.0 if abs(pos) < self._dust else pos) * inp.mid  # below a lot = flat
        return decide(out.p_up, position_usd, cfg, ts=ts, latency_ms=latency_ms)

    async def _enter(self, book: Book, d: Decision, decision_id: int) -> None:
        side = d.action.value
        async with self._lock:
            if self.stopped or self._kill.tripped or self._paused:  # halted/paused while waiting
                self.store.log_event(self._clock(), "order_skipped", f"decision {decision_id}")
                return
            if self._clock() - book.recv_ts > self.cfg.stale_s:  # the wait made the book stale
                self.store.log_event(
                    self._clock(), "order_skipped", f"decision {decision_id}: stale book"
                )
                return
            if self._rate_limited():
                self.store.log_event(
                    self._clock(), "order_skipped", f"decision {decision_id}: rate limit"
                )
                return
            try:
                fills = await self._run(
                    self._executor.open, book, side, d.size_usd, book.recv_ts
                )
                self._note_order()  # a raised call is not counted: the kill switch bounds those
                self._kill.on_order_result(True)
                applied = self._apply(fills)
            except Exception as e:
                applied = self._order_failed(e, "entry")
            if not applied:
                self.store.log_event(self._clock(), "no_fill", f"decision {decision_id}")
                return
            self._exits.append(
                _Exit(book.recv_ts + self.cfg.horizon_s, "sell" if side == "buy" else "buy",
                      sum(f["sz"] for f in applied))
            )

    # ---- stopping ----

    async def _flatten(self) -> None:
        """Cancel and close everything. Live, the exchange's own position is what gets
        closed (the ledger may be behind it), and its fills are re-read afterwards so a
        confirmation that timed out is still booked.

        A failed live flatten (network down) is retried with a growing delay for up to
        cfg.flatten_retry_window_s (it bounds when an attempt may start, so the end can overrun
        by one attempt, about 43 s), each try re-reading the exchange's position and placing
        only reduce-only closes. The wait is an awaited sleep and the lock is free during it,
        so cancelling the task (a Ctrl-C) ends it at once. One retry sequence per run: a later
        flatten (shutdown after a kill) joins it, or makes a single attempt if it was spent."""
        async with self._flatten_lock:
            live = self._executor.live
            window = 0.0 if not live or self._retry_spent else self.cfg.flatten_retry_window_s
            deadline = self._clock() + window
            delay, attempts, failed = FLATTEN_RETRY_FIRST_S, 0, False
            try:
                while True:
                    attempts += 1
                    async with self._lock:
                        err = await self._flatten_once(attempts)
                    if err is None:
                        break
                    failed = True
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        break
                    wait = min(delay, remaining)
                    self._say(f"flatten attempt {attempts} failed ({err}); retrying in {wait:g} s")
                    await self._sleep(wait)
                    delay = min(delay * 2, FLATTEN_RETRY_CAP_S)
                if live:
                    if err is None:
                        if failed or self.flatten_unresolved:
                            self.store.log_event(
                                self._clock(), "flatten_recovered", f"flat after {attempts} tries"
                            )
                            self._say(f"flatten recovered: flat after {attempts} attempts")
                        self.flatten_unresolved = False
                    else:
                        self._give_up(f"{attempts} attempts, last error {err}")
                        self._say(f"FLATTEN GAVE UP after {attempts} attempts ({err}): the "
                                  "position may still be open on the exchange, close it by hand")
                async with self._lock:
                    if live:
                        try:
                            self._apply(await self._run_abandonable(self._executor.sync_fills))
                        except Exception as e:
                            self.store.log_event(
                                self._clock(), "sync_error", f"{type(e).__name__}: {e}"
                            )
            except asyncio.CancelledError:
                if live:
                    self._give_up(f"interrupted after {attempts} attempts")
                raise
            finally:
                self._exits = []

    def _say(self, msg: str) -> None:
        """Tell the operator at the terminal (the events table is not watched during a retry)."""
        print(msg, file=sys.stderr, flush=True)

    def _give_up(self, why: str) -> None:
        self._retry_spent = True
        self.flatten_unresolved = True
        self.store.log_event(
            self._clock(), "flatten_gave_up",
            f"exchange position unknown, may still be open ({why})",
        )

    async def _flatten_once(self, attempt: int) -> str | None:
        """One flatten try, caller holds the lock. Returns None if done, else the error."""
        book, pos = self._last_book, self.ledger.position
        live = self._executor.live
        if not live and (book is None or abs(pos) < self._dust):
            return None
        try:
            fills = await self._run_abandonable(
                self._executor.flatten, book, pos, self._clock()
            )
            err = None
        except Exception as e:  # keep stopping even if the venue is unreachable
            fills = getattr(e, "fills", [])
            err = f"{type(e).__name__}: {e}"
            self.store.log_event(self._clock(), "flatten_failed", f"attempt {attempt}: {err}")
        self._apply(fills)
        return err

    async def _halt(self, reason: str | None) -> None:
        if self.stopped:  # already halted (e.g. the watchdog tripped mid-decision)
            return
        if reason == "stale_book":
            if self.stale_resumes < self.cfg.stale_resumes_max:
                await self._pause()
                return
            detail = (f"stale book again after {self.stale_resumes} resumes "
                      f"(limit {self.cfg.stale_resumes_max}): stopping for good")
            self.store.log_event(self._clock(), "stale_gave_up", detail)
            self._say(f"STALE GAVE UP: {detail}")
        self.stopped = True  # first: nothing new may start, and the run loop wakes
        self.store.log_event(self._clock(), "kill_switch", reason or "")
        await self._flatten()

    async def _pause(self) -> None:
        """A stale book: cancel and flatten as for any kill, but keep running, read the feed and
        place nothing until _settle finds the feed healthy again. Flags first, before any
        await, so nothing starts in between."""
        self._paused = True
        self.stale_pauses += 1
        self._kill.clear_stale()
        self._retry_spent = False  # each pause gets its own flatten retry window
        detail = (f"no book for over {self.cfg.stale_s:g} s; pause {self.stale_pauses}, "
                  f"{self.stale_resumes} of {self.cfg.stale_resumes_max} resumes used")
        self.store.log_event(self._clock(), "stale_pause", detail)
        self._say(f"STALE PAUSE: {detail}; flattening, resuming after "
                  f"{self.cfg.resume_after_s:g} s of steady books")
        await self._flatten()

    async def _settle(self, book: Book, prev: Book | None) -> None:
        """While paused, one call per book. Steady means no gap between books over
        resume_max_gap_s; a gap restarts the clock. Once steady for resume_after_s, resume,
        but never while a flatten is running or unresolved, and only if the exchange (live) and
        the ledger are flat. Anything not flat gets one more flatten and a fresh settling
        period."""
        cfg = self.cfg
        if (self._ok_since is None or prev is None
                or book.recv_ts - prev.recv_ts > cfg.resume_max_gap_s):
            self._ok_since = book.recv_ts
        if book.recv_ts - self._ok_since < cfg.resume_after_s:
            return
        if self._resuming or self._flatten_lock.locked():
            return
        self._resuming = True
        try:
            if self.flatten_unresolved:
                await self._flatten()  # a spent retry sequence: one attempt
                if self.stopped or not self._paused:
                    return
                if self.flatten_unresolved:
                    self._ok_since = book.recv_ts
                    return
            flat = await self._is_flat()
            if self.stopped or not self._paused:  # changed while we asked the exchange
                return
            if not flat:
                self.store.log_event(self._clock(), "stale_resume_blocked", "position not flat")
                self._say("STALE RESUME BLOCKED: position not flat, flattening again")
                await self._flatten()
                self._ok_since = book.recv_ts
                return
            self._paused = False
            self.stale_resumes += 1
            detail = (f"books steady for {cfg.resume_after_s:g} s; resume {self.stale_resumes} "
                      f"of {cfg.stale_resumes_max}")
            self.store.log_event(self._clock(), "stale_resume", detail)
            self._say(f"STALE RESUME: {detail}")
        finally:
            self._resuming = False

    async def _is_flat(self) -> bool:
        if abs(self.ledger.position) >= self._dust:
            return False
        if not self._executor.live:
            return True
        async with self._lock:
            try:
                return abs(await self._run(self._executor.position)) < self._dust
            except Exception as e:
                self.store.log_event(self._clock(), "sync_error", f"{type(e).__name__}: {e}")
                return False

    async def shutdown(self) -> None:
        """Clean shutdown: stop (queued orders re-check this), flatten."""
        self.stopped = True
        await self._flatten()

    async def run(self, books: AsyncIterator[Book]) -> None:
        """Consume snapshots until the stream ends or the bot stops. Each snapshot is handled
        in its own task so a slow decision never blocks the reader (it is skipped instead)."""
        tasks: set[asyncio.Task] = set()

        def done(t: asyncio.Task) -> None:
            tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                self.error = t.exception()
                self.stopped = True

        stop_wait = asyncio.create_task(self._stop.wait())
        nxt: asyncio.Task | None = None
        try:
            while not self.stopped:
                # Race the next book against stopping: a down feed yields nothing, and
                # the watchdog's stop must still end the run.
                nxt = asyncio.ensure_future(anext(books, None))
                await asyncio.wait({nxt, stop_wait}, return_when=asyncio.FIRST_COMPLETED)
                if not nxt.done() or self.stopped:
                    break
                book = nxt.result()
                nxt = None
                if book is None:
                    break
                t = asyncio.create_task(self.on_book(book))
                tasks.add(t)
                t.add_done_callback(done)
        finally:
            for pending in (stop_wait, nxt):
                if pending is not None and not pending.done():
                    pending.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await self.shutdown()
        if self.error is not None:
            raise self.error
