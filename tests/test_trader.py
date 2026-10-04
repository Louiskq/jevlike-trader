import asyncio
import time

import pytest

from jevhl.config import Config
from jevhl.feed import Book
from jevhl.ledger import Ledger
from jevhl.paper import PaperExchange
from jevhl.risk import KillSwitch
from jevhl.rounding import AssetMeta
from jevhl.store import Store
from jevhl.trader import Trader, kill_switch_from_store
from jevhl.types import ModelOutput

BTC = AssetMeta("BTC", 5)
T0 = 10 * 86400.0 + 3600.0  # mid-day UTC, far from midnight


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def mkbook(mid=65000.5, recv_ts=T0):
    bid, ask = mid - 0.5, mid + 0.5
    return Book(
        ts=recv_ts,
        recv_ts=recv_ts,
        coin="BTC",
        bids=[[bid - i, 5.0] for i in range(10)],
        asks=[[ask + i, 5.0] for i in range(10)],
    )


class StubModel:
    """Returns .p; optionally advances the shared mock clock (a slow model) or really sleeps."""

    def __init__(self, p=0.9, clock=None, advance=0.0, real_sleep=0.0):
        self.p, self.clock, self.advance, self.real_sleep = p, clock, advance, real_sleep
        self.calls = 0

    def predict(self, inp):
        self.calls += 1
        if self.clock is not None:
            self.clock.t += self.advance
        if self.real_sleep:
            time.sleep(self.real_sleep)
        if isinstance(self.p, Exception):
            raise self.p
        return ModelOutput(p_up=self.p)


def make(cfg=None, model=None, store=None, day_start_pnl=0.0, clock=None, perf=None):
    cfg = cfg or Config()
    clock = clock or Clock()
    store = store or Store(":memory:")
    model = model or StubModel()
    ledger = Ledger.from_fills(store.fills())
    kill = KillSwitch(cfg, clock, day_start_pnl=day_start_pnl)
    paper = PaperExchange(store, BTC, "BTC")
    tr = Trader(cfg, store, model, paper, ledger, kill, clock=clock, perf=perf or clock)
    return tr, store, clock, model


def run(coro):
    return asyncio.run(coro)


def reasons(store):
    return [(r["action"], r["reason"]) for r in store.decisions()]


# ---- decisions ----


def test_one_decision_per_snapshot_stamped_with_receive_time():
    tr, store, clock, _ = make(model=StubModel(p=0.5))
    for i in range(3):
        clock.t = T0 + 5.3 * i
        run(tr.on_book(mkbook(recv_ts=clock.t)))
    rows = store.decisions()
    assert [r["ts"] for r in rows] == pytest.approx([T0, T0 + 5.3, T0 + 10.6])
    assert all(r["reason"] == "below_gate" and r["p_up"] == 0.5 for r in rows)
    assert all(r["mid"] == 65000.5 and r["latency_ms"] == 0.0 for r in rows)


def test_late_hold_with_mocked_clock_valid_p_is_kept_but_not_traded():
    clock = Clock()
    model = StubModel(p=0.9, clock=clock, advance=2.5)  # 2.5 s > 2 s deadline
    tr, store, _, _ = make(model=model, clock=clock)
    run(tr.on_book(mkbook()))
    (r,) = store.decisions()
    assert (r["action"], r["reason"]) == ("hold", "late")
    assert r["latency_ms"] == pytest.approx(2500.0)
    assert r["p_up"] == 0.9  # still scored later, as in M1
    assert store.fills() == [] and tr.ledger.position == 0.0


def test_just_inside_deadline_trades_non_float_exact():
    clock = Clock()
    model = StubModel(p=0.9, clock=clock, advance=1.97)  # 1.97 s < 2 s
    tr, store, _, _ = make(model=model, clock=clock)
    run(tr.on_book(mkbook(recv_ts=clock.t)))
    assert reasons(store) == [("buy", "signal")]


def test_hung_model_times_out_to_late_hold_without_p():
    cfg = Config(deadline_s=0.05)
    model = StubModel(p=0.9, real_sleep=0.4)
    tr, store, _, _ = make(cfg=cfg, model=model, clock=Clock(), perf=time.perf_counter)
    t0 = time.perf_counter()
    run(tr.on_book(mkbook()))
    assert time.perf_counter() - t0 < 0.4 + 0.3  # asyncio.run joins the worker thread
    (r,) = store.decisions()
    assert (r["action"], r["reason"]) == ("hold", "late")
    assert r["p_up"] is None and r["latency_ms"] >= 50.0
    assert store.fills() == []


@pytest.mark.parametrize("bad", [1.5, -0.1, float("nan"), float("inf")])
def test_model_garbage_is_invalid_hold_and_never_scored(bad):
    tr, store, clock, _ = make(model=StubModel(p=bad))
    run(tr.on_book(mkbook()))
    (r,) = store.decisions()
    assert (r["action"], r["reason"]) == ("hold", "invalid") and r["p_up"] is None
    clock.t += 60
    run(tr.on_book(mkbook(mid=65100.5, recv_ts=clock.t)))
    assert store.scored(60) == []


def test_model_exception_is_invalid_hold_and_logged():
    tr, store, _, _ = make(model=StubModel(p=RuntimeError("boom")))
    run(tr.on_book(mkbook()))
    assert reasons(store) == [("hold", "invalid")]
    assert any(e["kind"] == "model_error" for e in store.events())


# ---- stale book ----


def test_stale_book_is_a_hold_and_model_is_not_called():
    model = StubModel()
    tr, store, clock, _ = make(model=model)
    run(tr.on_book(mkbook(recv_ts=clock.t - 15.01)))  # older than 15 s when handled
    (r,) = store.decisions()
    assert (r["action"], r["reason"]) == ("hold", "stale") and r["p_up"] is None
    assert model.calls == 0 and store.fills() == []


def test_book_just_inside_staleness_bound_is_decided():
    tr, store, clock, model = make()
    run(tr.on_book(mkbook(recv_ts=clock.t - 14.99)))
    assert model.calls == 1 and reasons(store) == [("buy", "signal")]


def test_book_exactly_at_staleness_bound_is_still_fresh():
    tr, store, clock, model = make()
    run(tr.on_book(mkbook(recv_ts=clock.t - 15.0)))  # bound is "older than 15 s"
    assert model.calls == 1


def test_stale_watchdog_trips_kill_switch_and_flattens():
    tr, store, clock, _ = make()
    run(tr.on_book(mkbook(recv_ts=clock.t)))  # buys
    assert tr.ledger.position > 0
    clock.t += 14.99
    assert tr.check_stale() is None and not tr.stopped
    clock.t += 0.02  # 15.01 s since the last book
    assert tr.check_stale() == "stale_book"
    assert tr.stopped
    assert abs(tr.ledger.position) < 1e-9  # flattened with a reduce-only order
    assert [e["detail"] for e in store.events() if e["kind"] == "kill_switch"] == ["stale_book"]


def test_no_decisions_after_stop():
    tr, store, clock, model = make()
    run(tr.on_book(mkbook()))
    clock.t += 20
    tr.check_stale()
    n = len(store.decisions())
    run(tr.on_book(mkbook(recv_ts=clock.t)))
    assert len(store.decisions()) == n


# ---- one decision in flight ----


def test_second_snapshot_while_decision_in_flight_is_skipped():
    model = StubModel(p=0.5, real_sleep=0.1)
    tr, store, clock, _ = make(model=model)

    async def both():
        await asyncio.gather(tr.on_book(mkbook()), tr.on_book(mkbook(recv_ts=clock.t + 1)))

    run(both())
    assert model.calls == 1 and len(store.decisions()) == 1
    assert [e["kind"] for e in store.events()] == ["skip"]


# ---- trading and exits ----


def test_entry_fills_then_time_exit_after_horizon():
    model = StubModel(p=0.9)
    tr, store, clock, _ = make(model=model)
    entry_ts = T0 + 5.3
    clock.t = entry_ts
    run(tr.on_book(mkbook(recv_ts=entry_ts)))
    pos = tr.ledger.position
    assert pos == pytest.approx(0.00769, abs=1e-5)  # ~$500 at 65001
    model.p = 0.5  # no new entries from here
    clock.t = entry_ts + 59.99
    run(tr.on_book(mkbook(recv_ts=clock.t)))
    assert tr.ledger.position == pytest.approx(pos)  # not yet
    clock.t = entry_ts + 60.01
    run(tr.on_book(mkbook(recv_ts=clock.t)))
    assert abs(tr.ledger.position) < 1e-9
    fills = store.fills()
    assert [f["side"] for f in fills] == ["buy", "sell"]
    assert fills[1]["ts"] == pytest.approx(entry_ts + 60.01)


def test_exit_exactly_at_horizon_boundary_non_float_exact():
    model = StubModel(p=0.9)
    tr, store, clock, _ = make(model=model)
    clock.t = T0 + 5.3
    run(tr.on_book(mkbook(recv_ts=clock.t)))
    model.p = 0.5
    clock.t = T0 + 5.3 + 60.0
    run(tr.on_book(mkbook(recv_ts=clock.t)))
    assert abs(tr.ledger.position) < 1e-9


def test_opposite_signal_while_open_is_blocked():
    model = StubModel(p=0.9)
    tr, store, clock, _ = make(model=model)
    run(tr.on_book(mkbook()))
    model.p = 0.1
    clock.t += 5.3
    run(tr.on_book(mkbook(recv_ts=clock.t)))
    assert reasons(store) == [("buy", "signal"), ("hold", "blocked")]
    assert tr.ledger.position > 0


def test_each_entry_keeps_its_own_exit_timer():
    model = StubModel(p=0.7)  # small size, room for adds under the cap
    tr, store, clock, _ = make(model=model)
    run(tr.on_book(mkbook()))
    first = tr.ledger.position
    clock.t += 30
    run(tr.on_book(mkbook(recv_ts=clock.t)))  # second same-side entry
    both = tr.ledger.position
    assert both > first
    model.p = 0.5
    clock.t = T0 + 60.5  # only the first entry is due
    run(tr.on_book(mkbook(recv_ts=clock.t)))
    assert tr.ledger.position == pytest.approx(both - first, abs=1e-9)
    clock.t = T0 + 90.5
    run(tr.on_book(mkbook(recv_ts=clock.t)))
    assert abs(tr.ledger.position) < 1e-9


def test_restart_with_open_position_closes_it_at_first_book():
    store = Store(":memory:")
    tr1, _, clock, _ = make(store=store)
    run(tr1.on_book(mkbook()))
    assert tr1.ledger.position > 0
    # restart: same store, fresh objects (timers are lost, so the trader must rebuild an exit)
    model2 = StubModel(p=0.5)
    tr2, _, _, _ = make(store=store, model=model2, clock=Clock(T0 + 5))
    run(tr2.on_book(mkbook(recv_ts=T0 + 5)))
    assert abs(tr2.ledger.position) < 1e-9
    assert [f["side"] for f in store.fills()] == ["buy", "sell"]


def test_shutdown_flattens():
    tr, store, _, _ = make()
    run(tr.on_book(mkbook()))
    run(tr.shutdown())
    assert abs(tr.ledger.position) < 1e-9
    assert tr.stopped


# ---- risk ----


def test_daily_loss_trips_flattens_and_stops():
    cfg = Config(max_daily_loss_usd=10)
    tr, store, clock, _ = make(cfg=cfg)
    run(tr.on_book(mkbook()))  # long ~0.0077 BTC
    clock.t += 5.3
    run(tr.on_book(mkbook(mid=63000.5, recv_ts=clock.t)))  # about -$15 plus fees
    assert tr.stopped
    assert abs(tr.ledger.position) < 1e-9
    assert [e["detail"] for e in store.events() if e["kind"] == "kill_switch"] == ["daily_loss"]


def test_kill_switch_baseline_from_store_survives_restart():
    cfg = Config()
    store = Store(":memory:")
    midnight = 10 * 86400.0
    # earlier today (after midnight): bought and sold at a $60 loss
    legs = [(1, midnight + 100, "buy", 65000.0), (2, midnight + 200, "sell", 59000.0)]
    for tid, ts, side, px in legs:
        store.insert_fill(tid=tid, oid=tid, ts=ts, coin="BTC", side=side, sz=0.01, px=px, fee=0.0)
    clock = Clock(midnight + 3600)
    ks = kill_switch_from_store(cfg, store, clock)
    ks.update_pnl(Ledger.from_fills(store.fills()).pnl(59000.0))  # -60 vs baseline 0
    assert not ks.tripped
    ks.update_pnl(-100.0)
    assert ks.reason == "daily_loss"  # a restart did not grant a fresh $100


def test_kill_switch_baseline_excludes_yesterdays_loss():
    cfg = Config()
    store = Store(":memory:")
    midnight = 10 * 86400.0
    legs = [(1, midnight - 200, "buy", 65000.0), (2, midnight - 100, "sell", 55000.0)]
    for tid, ts, side, px in legs:
        store.insert_fill(tid=tid, oid=tid, ts=ts, coin="BTC", side=side, sz=0.01, px=px, fee=0.0)
    ks = kill_switch_from_store(cfg, store, Clock(midnight + 3600))
    ks.update_pnl(-100.0)  # yesterday's -100 is the baseline, so today is flat
    assert not ks.tripped


# ---- outcomes ----


def test_outcomes_recorded_at_30_60_300_with_per_horizon_labels():
    model = StubModel(p=0.5)
    tr, store, clock, _ = make(model=model)
    run(tr.on_book(mkbook(mid=100.5)))  # decision at T0, mid 100.5
    for dt, mid in [(30.2, 101.5), (60.4, 99.5), (300.1, 100.5)]:
        clock.t = T0 + dt
        run(tr.on_book(mkbook(mid=mid, recv_ts=clock.t)))
    first = store.decisions()[0]["id"]
    got = {o["horizon_s"]: (o["mid_future"], o["label"]) for o in store.outcomes(first)}
    assert got == {30: (101.5, 1), 60: (99.5, 0), 300: (100.5, None)}
    assert store.scored(30)[0] == (0.5, 1) and store.scored(60)[0] == (0.5, 0)


def test_outcome_not_resolved_from_a_much_later_book():
    tr, store, clock, _ = make(model=StubModel(p=0.5))
    run(tr.on_book(mkbook()))
    clock.t = T0 + 120  # feed outage: first book after the 30 s and 60 s marks is far too late
    run(tr.on_book(mkbook(mid=70000.5, recv_ts=clock.t)))
    first = store.decisions()[0]["id"]
    assert store.outcomes(first) == []


# ---- run loop ----


def test_run_consumes_books_one_decision_each_and_flattens_at_end():
    tr, store, clock, _ = make()

    async def books():
        for i in range(3):
            clock.t = T0 + 5.3 * i
            yield mkbook(recv_ts=clock.t)
            await asyncio.sleep(0.1)

    run(tr.run(books()))
    assert len(store.decisions()) == 3
    assert abs(tr.ledger.position) < 1e-9  # clean shutdown flattened


# ---- kill switch during a decision ----


def test_kill_switch_tripping_mid_decision_does_not_trade_and_holds_killed():
    tr, store, clock, model = make(model=StubModel(p=0.9), perf=time.perf_counter)
    run(tr.on_book(mkbook()))  # long
    assert tr.ledger.position > 0
    model.real_sleep = 0.2

    async def scenario():
        t = asyncio.create_task(tr.on_book(mkbook(recv_ts=clock.t + 1)))
        await asyncio.sleep(0.05)  # model call is in flight
        clock.t += 20
        assert tr.check_stale() == "stale_book"  # watchdog flattens and stops
        await t

    run(scenario())
    assert [f["side"] for f in store.fills()] == ["buy", "sell"]  # no fresh entry
    assert reasons(store)[-1] == ("hold", "killed")
    assert abs(tr.ledger.position) < 1e-9


# ---- float residue ----


def test_float_residue_after_closing_two_entries_does_not_block_opposite_signal():
    model = StubModel(p=0.7)
    tr, store, clock, _ = make(model=model)
    run(tr.on_book(mkbook()))
    clock.t += 10
    model.p = 0.65  # different lot size, same side (leaves 4e-19 residue)
    run(tr.on_book(mkbook(recv_ts=clock.t)))
    model.p = 0.5
    clock.t = T0 + 100
    run(tr.on_book(mkbook(recv_ts=clock.t)))  # both exits run
    assert [f["side"] for f in store.fills()] == ["buy", "buy", "sell", "sell"]
    model.p = 0.1
    clock.t += 5.3
    run(tr.on_book(mkbook(recv_ts=clock.t)))
    assert reasons(store)[-1] == ("sell", "signal")
    assert tr.ledger.position < 0


def test_run_returns_promptly_after_watchdog_trips_while_feed_is_down():
    tr, store, clock, _ = make()

    async def books():
        yield mkbook(recv_ts=clock.t)
        await asyncio.Event().wait()  # feed down: reconnecting, yields nothing, forever

    async def scenario():
        task = asyncio.create_task(tr.run(books()))
        await asyncio.sleep(0.1)
        clock.t += 20
        assert tr.check_stale() == "stale_book"
        await asyncio.wait_for(task, 1.0)

    run(scenario())
    assert tr.stopped and abs(tr.ledger.position) < 1e-9
