"""The trader driving the real Exchange wrapper against the fake venue: accounting follows
exchange confirmations, exits and flatten go through the exchange, and every await re-checks
the stop state. No network, no key."""

import asyncio
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


def make(cfg=None, p=0.9, fake=None, store=None, **exkw):
    cfg = cfg or Config()
    fake = fake or venue()
    store = store or Store(":memory:")
    exkw.setdefault("sleep", lambda s: None)
    ex = Exchange(fake, fake, ADDR, BTC, "BTC", since_ms=int(T0 * 1000) - 60_000, **exkw)
    clock = Clock()
    model = StubModel(p=p)
    tr = Trader(cfg, store, model, ex, Ledger.from_fills(store.fills()),
                KillSwitch(cfg, clock, day_start_pnl=0.0), clock=clock, perf=clock)
    return tr, store, clock, fake, model


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
    clock.t = T0 + 16  # past the poll interval, before the 60 s exit
    run(tr.on_book(mkbook(recv_ts=clock.t)))
    assert len(store.fills()) == 3  # 2 entry fills + the late part, closed by the residue exit
    assert tr.ledger.position == pytest.approx(0.003)  # the first exit is still pending
    assert fake.position == pytest.approx(0.003)
    clock.t = T0 + 61
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
    assert [r["oid"] for r in store.fills()] == [999, 1000]  # booked, then closed by us
    assert tr.ledger.position == 0.0 and fake.position == pytest.approx(0.0, abs=1e-12)
    clock.t = T0 + 32
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
    tr, store, clock, fake, _ = make(flatten_tries=1)
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
