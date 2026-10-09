"""The trader driving the real Exchange wrapper against the fake venue: accounting follows
exchange confirmations, exits and flatten go through the exchange, and every await re-checks
the stop state. No network, no key."""

import asyncio
import math
import time

import pytest

from fake_hl import FakeHL
from jevhl.config import Config
from jevhl.exchange import Exchange
from jevhl.ledger import Ledger
from jevhl.risk import KillSwitch
from jevhl.rounding import AssetMeta
from jevhl.store import Store
from jevhl.trader import Trader
from test_trader import T0, Clock, StubModel, mkbook

BTC = AssetMeta("BTC", 5)
ADDR = "0x" + "ab" * 20


def run(coro):
    return asyncio.run(coro)


def venue(**kw):
    return FakeHL(
        bids=[[65000.0, 5.0], [64999.0, 5.0]], asks=[[65001.0, 5.0], [65002.0, 5.0]],
        t0_ms=int(T0 * 1000), **kw,
    )


def make(cfg=None, p=0.9, fake=None, store=None, tsleep=None, delays=None, **exkw):
    cfg = cfg or Config()
    fake = fake or venue()
    store = store or Store(":memory:")
    exkw.setdefault("sleep", lambda s: None)
    ex = Exchange(fake, fake, ADDR, BTC, "BTC", since_ms=int(T0 * 1000) - 60_000, **exkw)
    clock = Clock()
    model = StubModel(p=p)
    if delays is not None:  # flatten-retry waits that take no real time: record, move the clock
        async def tsleep(d):
            delays.append(d)
            clock.t += d
    tr = Trader(cfg, store, model, ex, Ledger.from_fills(store.fills()),
                KillSwitch(cfg, clock, day_start_pnl=0.0), clock=clock, perf=clock,
                **({"sleep": tsleep} if tsleep else {}))
    return tr, store, clock, fake, model


def fake_fills(fake):
    return [{"ts": f["time"] / 1000.0} for f in fake.raw_fills]


def order_calls(fake):
    return [c for c in fake.calls if c[0] == "order"]


def events(store, kind):
    return [e["detail"] for e in store.events() if e["kind"] == kind]


def test_entry_and_timed_exit_are_booked_from_exchange_confirmations():
    tr, store, clock, fake, model = make()
    run(tr.on_book(mkbook(recv_ts=T0)))
    (row,) = store.fills()
    assert row["tid"] == 5000 and row["oid"] == 1000 and row["side"] == "buy"
    assert row["px"] == 65001.0 and row["fee"] == pytest.approx(row["px"] * row["sz"] * 0.00045)
    assert tr.ledger.position == fake.position == row["sz"]
    assert tr.ledger.fees == pytest.approx(row["fee"])
    model.p = 0.5
    clock.t = T0 + 59.9
    run(tr.on_book(mkbook(recv_ts=clock.t)))
    assert len(order_calls(fake)) == 1  # not yet
    clock.t = T0 + 60.0
    run(tr.on_book(mkbook(recv_ts=clock.t)))
    (_, _, is_buy, _, _, _, reduce_only) = order_calls(fake)[1]
    assert (is_buy, reduce_only) == (False, True)
    assert [r["tid"] for r in store.fills()] == [5000, 5001]
    assert tr.ledger.position == 0.0 and fake.position == pytest.approx(0.0, abs=1e-12)


def test_a_failed_order_books_nothing_and_counts_as_an_error():
    tr, store, clock, fake, _ = make()
    fake.fail_orders = [ConnectionError("wifi off")]
    run(tr.on_book(mkbook(recv_ts=T0)))
    assert tr.ledger.position == 0.0 and store.fills() == []
    assert len(events(store, "order_error")) == 1 and not tr.stopped


def test_three_failed_orders_in_a_row_trip_the_kill_switch():
    tr, store, clock, fake, _ = make()
    fake.fail_orders = [ConnectionError("down")] * 3
    for i in range(3):
        clock.t = T0 + 5.3 * i
        run(tr.on_book(mkbook(recv_ts=clock.t)))
    assert tr.stopped and tr.kill_reason == "order_errors"
    assert events(store, "kill_switch") == ["order_errors"]


def test_unconfirmed_part_of_an_entry_is_booked_later_by_the_backstop_and_closed():
    fake = venue()
    fake.asks = [[65001.0, 0.003], [65002.0, 5.0]]
    tr, store, clock, _, model = make(fake=fake, confirm_tries=2)
    real = fake.user_fills_by_time
    fake.user_fills_by_time = lambda *a, **k: real(*a, **k)[:1]  # 2nd fill not visible yet
    run(tr.on_book(mkbook(recv_ts=T0)))
    assert tr.ledger.position == pytest.approx(0.003)  # only the confirmed part
    assert len(events(store, "order_error")) == 1  # counted, and the confirmed part still booked
    assert fake.position > 0.003
    fake.user_fills_by_time = real
    model.p = 0.5
    late_ts = max(f["ts"] for f in fake_fills(fake))  # when the late part filled
    clock.t = T0 + 16  # past the poll interval, before any exit is due
    run(tr.on_book(mkbook(recv_ts=clock.t)))
    assert len(store.fills()) == 2  # the confirmed part + the late part, now booked
    assert tr.ledger.position == pytest.approx(fake.position) and fake.position > 0.003
    assert len(order_calls(fake)) == 1  # nothing closed early: each entry lives 60 s
    clock.t = T0 + 61  # the first exit (0.003) is due; the late part is not, it filled later
    run(tr.on_book(mkbook(recv_ts=clock.t)))
    assert tr.ledger.position > 0  # the late part is still open
    assert len(order_calls(fake)) == 2
    clock.t = late_ts + 60.0
    run(tr.on_book(mkbook(recv_ts=clock.t)))
    assert tr.ledger.position == 0.0 and fake.position == pytest.approx(0.0, abs=1e-12)


def test_backstop_poll_runs_on_an_interval_not_every_book():
    tr, store, clock, fake, _ = make(p=0.5)
    polls = lambda: len([c for c in fake.calls if c[0] == "user_fills_by_time"])  # noqa: E731
    for t in (0, 5, 14.9):
        clock.t = T0 + t
        run(tr.on_book(mkbook(recv_ts=clock.t)))
    assert polls() == 1
    clock.t = T0 + 15
    run(tr.on_book(mkbook(recv_ts=clock.t)))
    assert polls() == 2


def test_a_fill_made_outside_the_bot_is_booked_once_and_closed():
    tr, store, clock, fake, _ = make(p=0.5)
    run(tr.on_book(mkbook(recv_ts=T0)))
    fake._record(999, "BTC", True, 0.001, 65001.0)  # e.g. a click in the testnet UI
    clock.t = T0 + 16
    run(tr.on_book(mkbook(recv_ts=clock.t)))
    assert [r["oid"] for r in store.fills()] == [999]  # booked; its 60 s has not passed
    assert tr.ledger.position == pytest.approx(0.001)
    clock.t = fake.now_ms / 1000.0 + 60  # 60 s after that fill's timestamp
    run(tr.on_book(mkbook(recv_ts=clock.t)))
    assert [r["oid"] for r in store.fills()] == [999, 1000]  # then closed by us
    assert tr.ledger.position == 0.0 and fake.position == pytest.approx(0.0, abs=1e-12)
    clock.t += 16
    run(tr.on_book(mkbook(recv_ts=clock.t)))  # the same fill is re-read: nothing doubles
    assert len(store.fills()) == 2 and tr.ledger.position == 0.0


def test_every_exit_is_covered_so_no_residue_exit_is_added():
    tr, store, clock, fake, _ = make()
    run(tr.on_book(mkbook(recv_ts=T0)))
    assert len(tr._exits) == 1
    tr._cover_residue()
    assert len(tr._exits) == 1


def test_restart_with_a_live_position_closes_it_at_the_first_book():
    tr, store, clock, fake, _ = make()
    run(tr.on_book(mkbook(recv_ts=T0)))
    assert fake.position > 0
    tr2, _, clock2, _, _ = make(fake=fake, store=store, p=0.5)  # same venue, same store
    assert tr2.ledger.position == tr.ledger.position
    clock2.t = T0 + 5
    run(tr2.on_book(mkbook(recv_ts=clock2.t)))
    assert tr2.ledger.position == 0.0 and fake.position == pytest.approx(0.0, abs=1e-12)


def test_a_failed_exit_is_retried_at_the_next_book():
    tr, store, clock, fake, model = make()
    run(tr.on_book(mkbook(recv_ts=T0)))
    model.p = 0.5
    fake.fail_orders = [ConnectionError("blip")]
    clock.t = T0 + 60
    run(tr.on_book(mkbook(recv_ts=clock.t)))
    assert tr.ledger.position > 0 and len(events(store, "order_error")) == 1
    clock.t = T0 + 66
    run(tr.on_book(mkbook(recv_ts=clock.t)))
    assert tr.ledger.position == 0.0


def test_daily_loss_flattens_through_the_exchange():
    tr, store, clock, fake, _ = make(cfg=Config(max_daily_loss_usd=10))
    run(tr.on_book(mkbook(recv_ts=T0)))
    fake.bids, fake.asks, fake.mid = [[63000.0, 5.0]], [[63001.0, 5.0]], 63000.5
    clock.t = T0 + 5.3
    run(tr.on_book(mkbook(mid=63000.5, recv_ts=clock.t)))
    assert tr.stopped and events(store, "kill_switch") == ["daily_loss"]
    assert tr.ledger.position == 0.0 and fake.position == pytest.approx(0.0, abs=1e-12)
    assert order_calls(fake)[-1][6] is True  # reduce-only


def test_stale_book_watchdog_flattens_the_exchange_position():
    tr, store, clock, fake, _ = make()
    run(tr.on_book(mkbook(recv_ts=T0)))
    clock.t = T0 + 15.01
    assert run(tr.check_stale()) == "stale_book"
    assert tr.stopped and fake.position == pytest.approx(0.0, abs=1e-12)
    assert tr.ledger.position == 0.0


def test_flatten_that_cannot_reach_the_venue_is_logged_and_the_bot_still_stops():
    tr, store, clock, fake, _ = make(cfg=Config(flatten_retry_window_s=0), flatten_tries=1)
    run(tr.on_book(mkbook(recv_ts=T0)))
    fake.fail_state = [ConnectionError("down")] * 50
    run(tr.shutdown())
    assert tr.stopped and fake.position > 0
    assert len(events(store, "flatten_failed")) == 1


def test_fills_of_an_unconfirmed_flatten_are_picked_up_by_the_final_sync():
    tr, store, clock, fake, _ = make(confirm_tries=2, flatten_tries=1)
    run(tr.on_book(mkbook(recv_ts=T0)))
    fake.hide_polls = 2  # the closing fill stays invisible through the confirmation window
    run(tr.shutdown())
    assert fake.position == pytest.approx(0.0, abs=1e-12)
    assert tr.ledger.position == 0.0 and len(store.fills()) == 2


def test_a_halt_while_an_entry_waits_for_the_exchange_sends_no_order():
    tr, store, clock, fake, _ = make()
    tr._last_poll = T0  # skip the backstop so the entry is what waits

    async def scenario():
        await tr._lock.acquire()  # the exchange is busy (say, flattening)
        t = asyncio.create_task(tr.on_book(mkbook(recv_ts=T0)))
        await asyncio.sleep(0.1)  # the model has answered; the entry waits for the lock
        tr.stopped = True  # halted meanwhile
        tr._lock.release()
        await t

    run(scenario())
    assert order_calls(fake) == [] and store.fills() == []


def test_a_halt_while_an_exit_waits_for_the_exchange_sends_no_second_close():
    tr, store, clock, fake, model = make()
    run(tr.on_book(mkbook(recv_ts=T0)))
    model.p = 0.5
    tr._last_poll = T0 + 60

    async def scenario():
        await tr._lock.acquire()
        clock.t = T0 + 60
        t = asyncio.create_task(tr.on_book(mkbook(recv_ts=clock.t)))
        await asyncio.sleep(0.05)
        tr.stopped = True
        tr._lock.release()
        await t

    run(scenario())
    assert len(order_calls(fake)) == 1  # only the entry


def test_a_slow_order_does_not_block_the_event_loop():
    tr, store, clock, fake, _ = make()
    orig = fake.order

    def slow(*a, **k):
        time.sleep(0.3)
        return orig(*a, **k)

    fake.order = slow
    ticks = []

    async def scenario():
        async def ticker():
            while True:
                ticks.append(1)
                await asyncio.sleep(0.02)

        t = asyncio.create_task(ticker())
        await tr.on_book(mkbook(recv_ts=T0))
        t.cancel()

    run(scenario())
    assert len(ticks) >= 8 and len(store.fills()) == 1


def _order_times(fake, clock):
    times, orig = [], fake.order

    def spy(*a, **k):
        times.append(clock.t)
        return orig(*a, **k)

    fake.order = spy
    return times


def test_live_orders_stay_within_about_two_per_minute_at_default_config():
    tr, store, clock, fake, _ = make(p=0.65)
    times = _order_times(fake, clock)
    for i in range(40):  # ~3.5 minutes of books, the model always wants to add
        clock.t = T0 + 5.3 * i
        run(tr.on_book(mkbook(recv_ts=clock.t)))
    assert len(times) >= 4  # it still trades
    for t in times:
        assert len([x for x in times if t <= x < t + 60]) <= 2
    assert events(store, "order_skipped")  # the extra entries were skipped and logged
    assert any("rate" in d for d in events(store, "order_skipped"))


def test_the_rate_guard_never_blocks_an_exit():
    tr, store, clock, fake, model = make(p=0.9)
    run(tr.on_book(mkbook(recv_ts=T0)))
    model.p = 0.5
    # a burst of other orders just before the exit is due must not stop the exit
    tr._order_times = [T0 + 59.0, T0 + 59.5, T0 + 59.9]
    clock.t = T0 + 60
    run(tr.on_book(mkbook(recv_ts=clock.t)))
    assert tr.ledger.position == 0.0 and fake.position == pytest.approx(0.0, abs=1e-12)


def test_the_rate_guard_does_not_apply_to_the_paper_executor():
    from jevhl.paper import PaperExchange

    cfg, store, clock = Config(), Store(":memory:"), Clock()
    ex = PaperExchange(store, BTC, "BTC")
    kill = KillSwitch(cfg, clock, day_start_pnl=0.0)
    tr = Trader(cfg, store, StubModel(p=0.65), ex, Ledger(), kill, clock=clock, perf=clock)
    for i in range(6):
        clock.t = T0 + 5.3 * i
        run(tr.on_book(mkbook(recv_ts=clock.t)))
    assert len([f for f in store.fills() if f["side"] == "buy"]) >= 4


def test_an_entry_that_waited_for_the_lock_past_stale_s_is_skipped():
    tr, store, clock, fake, _ = make()
    tr._last_poll = T0  # skip the backstop so the entry is what waits

    async def scenario():
        await tr._lock.acquire()  # the exchange is busy
        t = asyncio.create_task(tr.on_book(mkbook(recv_ts=T0)))
        await asyncio.sleep(0.1)  # the model has answered; the entry waits for the lock
        clock.t = T0 + tr.cfg.stale_s + 1  # the book is now stale, the feed still healthy
        tr._lock.release()
        await t

    run(scenario())
    assert order_calls(fake) == [] and store.fills() == []
    assert any("stale" in d for d in events(store, "order_skipped"))


# ---- tests added for mutants that survived (reviewer break-it, B1-B3 and weak tests) ----


def test_shutdown_closes_the_exchange_position_even_when_the_ledger_is_flat():  # B1
    tr, store, clock, fake, _ = make(p=0.5)
    fake._record(999, "BTC", True, 0.002, 65001.0)  # a position the ledger never saw
    assert tr.ledger.position == 0.0 and fake.position > 0
    run(tr.shutdown())
    assert fake.position == pytest.approx(0.0, abs=1e-12)
    assert tr.ledger.position == 0.0 and [r["oid"] for r in store.fills()][0] == 999


def test_a_successful_exit_resets_the_consecutive_order_error_count():  # B3
    tr, store, clock, fake, model = make()
    run(tr.on_book(mkbook(recv_ts=T0)))
    model.p = 0.5
    fake.fail_orders = [ConnectionError("blip")] * 2
    for t in (60, 66):  # the exit fails twice
        clock.t = T0 + t
        run(tr.on_book(mkbook(recv_ts=clock.t)))
    clock.t = T0 + 72  # and then succeeds
    run(tr.on_book(mkbook(recv_ts=clock.t)))
    assert tr.ledger.position == 0.0 and not tr.stopped
    model.p = 0.9
    fake.fail_orders = [ConnectionError("blip")] * 2
    for t in (140, 145):  # two more failures: 2 in a row, not 4
        clock.t = T0 + t
        run(tr.on_book(mkbook(recv_ts=clock.t)))
    assert len(events(store, "order_error")) == 4 and not tr.stopped


def test_the_stop_flag_is_set_before_the_flatten_starts():
    tr, store, clock, fake, _ = make()
    run(tr.on_book(mkbook(recv_ts=T0)))
    seen = []
    real = tr._executor.flatten

    def spy(*a, **k):
        seen.append(tr.stopped)
        return real(*a, **k)

    tr._executor.flatten = spy
    clock.t = T0 + 15.01
    assert run(tr.check_stale()) == "stale_book"
    assert seen == [True]


def test_a_poll_that_waited_for_the_lock_does_nothing_if_the_bot_stopped_meanwhile():
    tr, store, clock, fake, _ = make(p=0.5)

    async def scenario():
        await tr._lock.acquire()
        t = asyncio.create_task(tr.on_book(mkbook(recv_ts=T0)))  # the poll is due at once
        await asyncio.sleep(0.05)
        tr.stopped = True
        tr._lock.release()
        await t

    run(scenario())
    assert [c for c in fake.calls if c[0] == "user_fills_by_time"] == []


# ---- keep retrying a failed flatten (issue #12) ----


def down(n):
    """The venue is unreachable for n user_state reads (flatten_tries=1 reads twice per try)."""
    return [ConnectionError("dns")] * n


def test_flatten_retries_with_growing_delay_until_the_network_returns():
    delays: list[float] = []
    tr, store, clock, fake, _ = make(flatten_tries=1, delays=delays)
    run(tr.on_book(mkbook(recv_ts=T0)))
    fake.fail_state = down(6)  # three whole attempts fail, the fourth works
    run(tr.shutdown())
    assert delays == [1, 2, 4]
    assert fake.position == pytest.approx(0.0, abs=1e-12) and tr.ledger.position == 0.0
    assert len(store.fills()) == 2  # entry and the close, booked
    assert len(events(store, "flatten_failed")) == 3
    assert len(events(store, "flatten_recovered")) == 1 and events(store, "flatten_gave_up") == []
    assert not tr.flatten_unresolved
    assert all(c[6] for c in order_calls(fake)[1:])  # only reduce-only closes


def test_the_delay_doubles_to_a_cap_and_the_window_bounds_the_total_time():
    delays: list[float] = []
    cfg = Config(flatten_retry_window_s=100)
    tr, store, clock, fake, _ = make(
        cfg=cfg, flatten_tries=1, delays=delays
    )
    run(tr.on_book(mkbook(recv_ts=T0)))
    fake.fail_state = down(10_000)
    start = clock.t
    run(tr.shutdown())
    assert delays == [1, 2, 4, 8, 16, 30, 30, 9]  # capped at 30 s, last one trimmed to the window
    assert clock.t - start == pytest.approx(100)
    assert fake.position > 0 and tr.flatten_unresolved
    assert len(events(store, "flatten_failed")) == 9  # one per attempt
    (gave_up,) = events(store, "flatten_gave_up")
    assert "still open" in gave_up or "unknown" in gave_up.lower()
    assert events(store, "flatten_recovered") == []


def test_window_zero_is_a_single_attempt():
    delays: list[float] = []
    tr, store, clock, fake, _ = make(
        cfg=Config(flatten_retry_window_s=0), flatten_tries=1,
        delays=delays,
    )
    run(tr.on_book(mkbook(recv_ts=T0)))
    fake.fail_state = down(10_000)
    run(tr.shutdown())
    assert delays == [] and len(events(store, "flatten_failed")) == 1
    assert len(events(store, "flatten_gave_up")) == 1 and tr.flatten_unresolved


def test_fills_of_failed_attempts_are_booked_before_the_next_try_and_each_retry_rereads():
    waits: list[float] = []
    booked: list[float] = []

    async def sleep(d):
        waits.append(d)
        booked.append(tr.ledger.position)  # what is booked while waiting
        clock.t += d

    tr, store, clock, fake, _ = make(flatten_tries=1, tsleep=sleep)
    run(tr.on_book(mkbook(recv_ts=T0)))
    entry = fake.position
    # each order closes a third (rounded up to a lot): every flatten attempt falls short
    fake.max_fill_sz = math.ceil(entry / 3 * 1e5 - 1e-9) / 1e5
    run(tr.shutdown())
    assert booked[0] < entry and booked == sorted(booked, reverse=True)  # shrinking as booked
    assert fake.position == pytest.approx(0.0, abs=1e-12)
    assert tr.ledger.position == pytest.approx(0.0, abs=1e-9)
    assert len(store.fills()) > 2 and len(events(store, "flatten_recovered")) == 1


def test_the_retry_wait_does_not_block_the_event_loop_or_hold_the_lock():
    seen = {"locked": [], "ticks": 0}

    async def sleep(d):
        seen["locked"].append(tr._lock.locked())
        await asyncio.sleep(0.02)  # really yields

    tr, store, clock, fake, _ = make(flatten_tries=1, tsleep=sleep)
    run(tr.on_book(mkbook(recv_ts=T0)))
    fake.fail_state = down(4)

    async def ticker():
        while True:
            seen["ticks"] += 1
            await asyncio.sleep(0.005)

    async def scenario():
        t = asyncio.create_task(ticker())
        await tr.shutdown()
        t.cancel()

    run(scenario())
    assert seen["locked"] == [False, False] and seen["ticks"] > 3
    assert fake.position == pytest.approx(0.0, abs=1e-12)


def test_an_interrupt_during_the_retry_wait_ends_it_promptly_and_says_so():
    tr, store, clock, fake, _ = make(flatten_tries=1)  # real asyncio.sleep: 1 s, 2 s ...
    run(tr.on_book(mkbook(recv_ts=T0)))
    fake.fail_state = down(10_000)

    async def scenario():
        t0 = time.monotonic()
        t = asyncio.create_task(tr.shutdown())
        await asyncio.sleep(0.1)  # first attempt failed, now waiting (1 s if it were blocked)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        return time.monotonic() - t0

    assert run(scenario()) < 0.5
    assert len(events(store, "flatten_failed")) == 1
    (gave_up,) = events(store, "flatten_gave_up")
    assert "interrupted" in gave_up and tr.flatten_unresolved
    assert fake.position > 0


def test_each_failed_attempt_and_the_outcome_are_printed_to_stderr(capsys):
    delays: list[float] = []
    tr, store, clock, fake, _ = make(flatten_tries=1, delays=delays)
    run(tr.on_book(mkbook(recv_ts=T0)))
    fake.fail_state = down(4)  # two attempts fail (two reads each), the third works
    run(tr.shutdown())
    err = capsys.readouterr().err.splitlines()
    assert len(err) == 3
    assert "attempt 1" in err[0] and "dns" in err[0] and "retrying in 1 s" in err[0]
    assert "attempt 2" in err[1] and "retrying in 2 s" in err[1]
    assert "recovered" in err[2] and "3 attempts" in err[2]


def test_giving_up_prints_a_final_line_to_stderr(capsys):
    tr, store, clock, fake, _ = make(cfg=Config(flatten_retry_window_s=0), flatten_tries=1)
    run(tr.on_book(mkbook(recv_ts=T0)))
    fake.fail_state = down(10_000)
    run(tr.shutdown())
    err = capsys.readouterr().err.splitlines()
    assert len(err) == 1 and "GAVE UP" in err[0] and "1 attempts" in err[0]
    assert "by hand" in err[0]


def test_a_drop_right_after_the_close_order_is_placed_is_booked_by_the_final_fill_read():
    tr, store, clock, fake, _ = make(flatten_tries=1)
    run(tr.on_book(mkbook(recv_ts=T0)))
    assert tr.ledger.position > 0
    fake.fail_fills = [ConnectionError("dns")]  # the close fills but its confirmation read fails
    run(tr.shutdown())
    assert fake.position == pytest.approx(0.0, abs=1e-12)  # the order went through
    assert tr.ledger.position == pytest.approx(0.0, abs=1e-12)  # and was still booked
    assert not tr.flatten_unresolved and not events(store, "flatten_gave_up")
    assert [r["side"] for r in store.fills()] == ["buy", "sell"]


def test_the_window_bounds_when_an_attempt_may_start_not_how_long_it_runs():
    tr, store, clock, fake, _ = make(
        cfg=Config(flatten_retry_window_s=10), flatten_tries=1, delays=[]
    )
    run(tr.on_book(mkbook(recv_ts=T0)))
    fake.fail_state = down(10_000)
    real_user_state = fake.user_state

    def slow_user_state(address, dex=""):
        clock.t += 20  # one attempt takes longer than the whole window
        return real_user_state(address, dex)

    fake.user_state = slow_user_state
    run(tr.shutdown())
    assert len(events(store, "flatten_failed")) == 1  # no second attempt starts
    assert tr.flatten_unresolved


def test_cancelling_during_a_blocked_attempt_returns_at_once_and_the_thread_is_tracked():
    class Slow(FakeHL):
        def open_orders(self, address, dex=""):
            time.sleep(1.5)  # an HTTP call running into its timeout
            raise TimeoutError("read timeout")

    fake = Slow(bids=[[65000.0, 5.0]], asks=[[65001.0, 5.0]], t0_ms=int(T0 * 1000))
    tr, store, clock, fake, _ = make(fake=fake, flatten_tries=1)
    run(tr.on_book(mkbook(recv_ts=T0)))

    async def scenario():
        t = asyncio.create_task(tr.shutdown())
        await asyncio.sleep(0.1)  # inside the blocked call
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t

    t0 = time.monotonic()
    run(scenario())  # asyncio.run also waits for the default executor's threads
    assert time.monotonic() - t0 < 0.8
    assert len(events(store, "flatten_attempt_abandoned")) == 1
    assert len(tr.abandoned_threads) == 1 and tr.flatten_unresolved


def test_cancelling_during_the_final_fill_read_also_returns_at_once():
    class Slow(FakeHL):
        armed, reads = False, 0

        def user_fills_by_time(self, address, start_time, end_time=None, aggregate_by_time=False):
            if self.armed:
                self.reads += 1
                if self.reads == 2:  # the 1st confirms the close, the 2nd is the final re-read
                    time.sleep(1.5)
            return super().user_fills_by_time(address, start_time, end_time)

    fake = Slow(bids=[[65000.0, 5.0]], asks=[[65001.0, 5.0]], t0_ms=int(T0 * 1000))
    tr, store, clock, fake, _ = make(fake=fake, flatten_tries=1)
    run(tr.on_book(mkbook(recv_ts=T0)))

    async def scenario():
        fake.armed = True
        t = asyncio.create_task(tr.shutdown())
        await asyncio.sleep(0.2)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t

    t0 = time.monotonic()
    run(scenario())
    assert time.monotonic() - t0 < 0.8


def test_an_abandoned_call_that_finishes_later_does_not_disturb_the_loop():
    class Slow(FakeHL):
        def open_orders(self, address, dex=""):
            time.sleep(0.3)
            raise TimeoutError("read timeout")

    fake = Slow(bids=[[65000.0, 5.0]], asks=[[65001.0, 5.0]], t0_ms=int(T0 * 1000))
    tr, store, clock, fake, _ = make(fake=fake, flatten_tries=1)
    run(tr.on_book(mkbook(recv_ts=T0)))
    problems: list[dict] = []

    async def scenario():
        asyncio.get_running_loop().set_exception_handler(lambda loop, ctx: problems.append(ctx))
        t = asyncio.create_task(tr.shutdown())
        await asyncio.sleep(0.1)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        await asyncio.sleep(1.2)  # the blocked call ends and reports to a cancelled future

    run(scenario())
    assert problems == []


def test_a_flatten_after_a_spent_retry_is_one_attempt_not_a_second_window():
    delays: list[float] = []
    cfg = Config(flatten_retry_window_s=10)
    tr, store, clock, fake, _ = make(
        cfg=cfg, flatten_tries=1, delays=delays
    )
    run(tr.on_book(mkbook(recv_ts=T0)))
    fake.fail_state = down(10_000)
    clock.t = T0 + 15.01
    assert run(tr.check_stale()) == "stale_book"  # the kill's flatten uses the window
    spent = list(delays)
    failed = len(events(store, "flatten_failed"))
    assert spent and tr.flatten_unresolved
    run(tr.shutdown())  # what run() does on its way out
    assert delays == spent  # no new waiting
    assert len(events(store, "flatten_failed")) == failed + 1


def test_a_late_shutdown_that_finds_the_venue_back_closes_and_reports_recovery():
    delays: list[float] = []
    tr, store, clock, fake, _ = make(
        cfg=Config(flatten_retry_window_s=0), flatten_tries=1,
        delays=delays,
    )
    run(tr.on_book(mkbook(recv_ts=T0)))
    fake.fail_state = down(2)
    clock.t = T0 + 15.01
    run(tr.check_stale())
    assert tr.flatten_unresolved
    run(tr.shutdown())
    assert not tr.flatten_unresolved and fake.position == pytest.approx(0.0, abs=1e-12)
    assert len(events(store, "flatten_recovered")) == 1


def test_a_second_flatten_while_one_is_retrying_joins_it_instead_of_starting_over():
    delays: list[float] = []

    async def sleep(d):
        delays.append(d)
        clock.t += d
        await asyncio.sleep(0.01)

    tr, store, clock, fake, _ = make(flatten_tries=1, tsleep=sleep)
    run(tr.on_book(mkbook(recv_ts=T0)))
    fake.fail_state = down(4)

    async def scenario():
        a = asyncio.create_task(tr.shutdown())
        await asyncio.sleep(0.005)
        b = asyncio.create_task(tr.shutdown())  # e.g. run()'s cleanup after a Ctrl-C
        await asyncio.gather(a, b)

    run(scenario())
    assert delays == [1, 2]  # one retry sequence, not two
    assert len(events(store, "flatten_recovered")) == 1


def test_paper_flatten_is_not_retried():
    from test_trader import make as make_paper

    tr, store, clock, _ = make_paper()
    run(tr.on_book(mkbook(recv_ts=T0)))
    calls = []

    def boom(*a):
        calls.append(1)
        raise ConnectionError("x")

    tr._executor.flatten = boom
    tr._sleep = None  # any retry would blow up on this
    run(tr.shutdown())
    assert calls == [1] and len(events(store, "flatten_failed")) == 1
    assert events(store, "flatten_gave_up") == []
