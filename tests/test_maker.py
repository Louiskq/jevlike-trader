"""The maker loop (STRATEGY=maker, milestone 8): one post-only quote on the model's side, one
tick inside the touch (our own order removed from the book first), replaced only when its
side or price changes, the cap forcing the other side, post-only rejections, partial fills,
cancel-all on stale, kill and shutdown, the order-action cap, rate-limit pauses and the
changed-book scoring rule. Paper and the fake venue; clocks are injected, nothing waits."""

import asyncio
from dataclasses import replace
from decimal import Decimal

import pytest

from fake_hl import HTTP429, FakeHL
from jevhl.config import Config
from jevhl.exchange import Exchange
from jevhl.feed import Book, Print
from jevhl.ledger import Ledger
from jevhl.maker import UNKNOWN, MakerTrader, Quote, without_own
from jevhl.paper import PaperExchange
from jevhl.policy import MAKER_GATE, maker_side
from jevhl.risk import KillSwitch
from jevhl.rounding import AssetMeta
from jevhl.store import Store
from jevhl.types import Action
from test_trader import T0, Clock, StubModel

BTC = AssetMeta("BTC", 5)
ADDR = "0x" + "ab" * 20
MCFG = Config(strategy="maker", max_position_usd=15.0)
SZ = 0.00016  # $11 at 65001, floored to a lot


def run(coro):
    return asyncio.run(coro)


def mkbook(bid=65000.0, ask=65003.0, recv_ts=T0, bid_sz=5.0, ask_sz=5.0):
    return Book(ts=recv_ts, recv_ts=recv_ts, coin="BTC",
                bids=[[bid - i, bid_sz] for i in range(20)],
                asks=[[ask + i, ask_sz] for i in range(20)])


def make_paper(cfg=MCFG, p=0.9, store=None, model=None):
    clock = Clock()
    store = store or Store(":memory:")
    model = model or StubModel(p=p)
    tr = MakerTrader(cfg, store, model, PaperExchange(store, BTC, "BTC"),
                     Ledger.from_fills(store.fills()), KillSwitch(cfg, clock, day_start_pnl=0.0),
                     clock=clock, perf=clock)
    return tr, store, clock, model


def make_live(cfg=MCFG, p=0.9, fake=None, store=None, model=None):
    clock = Clock()
    fake = fake or FakeHL(bids=[[65000.0, 5.0]], asks=[[65003.0, 5.0]], t0_ms=int(T0 * 1000))
    store = store or Store(":memory:")
    ex = Exchange(fake, fake, ADDR, BTC, "BTC", since_ms=int(T0 * 1000) - 60_000,
                  sleep=lambda s: None)
    model = model or StubModel(p=p)

    async def tsleep(d):  # flatten-retry waits take no real time
        clock.t += d

    tr = MakerTrader(cfg, store, model, ex, Ledger.from_fills(store.fills()),
                     KillSwitch(cfg, clock, day_start_pnl=0.0), clock=clock, perf=clock,
                     sleep=tsleep)
    return tr, store, clock, fake, model


def tick(tr, clock, t, **book_kw):
    clock.t = t
    run(tr.on_book(mkbook(recv_ts=t, **book_kw)))


def calls(fake, kind):
    return [c for c in fake.calls if c[0] == kind]


def quote_actions(fake):
    return [c for c in fake.calls if c[0] in ("order", "modify", "bulk_cancel")]


def events(store, kind):
    return [e["detail"] for e in store.events() if e["kind"] == kind]


def no_polls(tr, clock):
    """Push the live fill poll, open-orders check and budget read out of the way."""
    tr._last_poll = tr._last_reconcile = tr._last_budget = clock.t + 10_000


# ---- the side (policy) ----


def pick(p, pos_usd, cfg=MCFG, latency_ms=10.0):
    return maker_side(p, pos_usd, cfg, ts=1.0, latency_ms=latency_ms)


def test_maker_always_picks_a_side_at_half():
    assert MAKER_GATE == 0.5
    assert pick(0.5, 0.0).action is Action.BUY and pick(0.4999, 0.0).action is Action.SELL
    d = pick(0.51, 0.0)
    assert (d.reason, d.size_usd, d.gate) == ("signal", 11.0, 0.5)


def test_cap_blocks_buy_so_quote_goes_on_sell_logged_capped():
    d = pick(0.9, 10.4)
    assert (d.action, d.reason, d.p_up) == (Action.SELL, "capped", 0.9)
    assert pick(0.1, -10.4).action is Action.BUY and pick(0.1, -10.4).reason == "capped"


def test_boundary_cap_uses_non_float_exact_values():
    assert pick(0.9, 3.99).reason == "signal"  # 3.99 + 11 = 14.99 <= 15
    assert pick(0.9, 4.01).reason == "capped"  # 15.01 > 15
    assert pick(0.2, -3.99).reason == "signal" and pick(0.2, -4.01).reason == "capped"
    cfg = Config(strategy="maker", max_position_usd=15.0, maker_size_usd=11.37)
    assert pick(0.9, 3.63, cfg).reason == "signal" and pick(0.9, 3.64, cfg).reason == "capped"


def test_reducing_side_allowed_when_position_is_over_cap():
    d = pick(0.1, 31.0)  # a UI trade left us long $31, over the $15 cap: selling reduces
    assert (d.action, d.reason) == (Action.SELL, "signal")
    assert pick(0.9, 31.0).action is Action.SELL  # buying would grow it: capped to sell
    assert pick(0.9, -31.0).action is Action.BUY


def test_maker_side_late_and_invalid_hold():
    assert pick(0.9, 0.0, latency_ms=1000.01).reason == "late"  # maker deadline 1 s
    assert pick(0.9, 0.0, latency_ms=999.9).reason == "signal"
    for bad in (None, float("nan"), 1.01, -0.01):
        assert pick(bad, 0.0).reason == "invalid"


# ---- pricing against our own order ----


def test_without_own_removes_our_size_at_our_price_only():
    b = mkbook(bid=65001.0, bid_sz=0.30016)
    q = Quote(1, "buy", Decimal("65001"), SZ, T0)
    view = without_own(b, q, BTC)
    assert view.bids[0] == [65001.0, 0.3] and view.asks == b.asks
    alone = without_own(mkbook(bid=65001.0, bid_sz=SZ), q, BTC)
    assert alone.bids[0][0] == 65000.0  # our order was the whole level: it is dropped
    assert without_own(b, None, BTC) is b and without_own(b, UNKNOWN, BTC) is b


def test_own_resting_order_is_not_treated_as_the_touch():
    tr, store, clock, fake, _ = make_live()
    tick(tr, clock, T0)
    (order,) = calls(fake, "order")
    assert order[4] == 65001.0  # one tick above the 65000 bid
    no_polls(tr, clock)
    # the next books show our own order as the best bid: re-pricing off it would walk up
    for i in range(1, 4):
        tick(tr, clock, T0 + i, bid=65001.0, bid_sz=SZ)
    assert calls(fake, "modify") == [] and len(calls(fake, "order")) == 1
    # someone else joins at our price: the touch excluding us is now 65001, so step inside
    tick(tr, clock, T0 + 4, bid=65001.0, bid_sz=SZ + 0.5)
    (mod,) = calls(fake, "modify")
    assert mod[5] == 65002.0


# ---- replace only on change ----


def test_no_tracked_order_places_one_alo():
    tr, store, clock, fake, _ = make_live()
    tick(tr, clock, T0)
    (order,) = calls(fake, "order")
    assert order == ("order", "BTC", True, SZ, 65001.0, {"limit": {"tif": "Alo"}}, False)
    assert len(events(store, "quote_place")) == 1
    assert tr.maker_view()["quote"]["px"] == 65001.0


def test_unchanged_side_and_price_sends_no_action():
    tr, store, clock, fake, _ = make_live()
    for i in range(10):
        tick(tr, clock, T0 + i)
    assert len(quote_actions(fake)) == 1 and len(store.decisions()) == 10
    assert len(fake.resting) == 1


def test_price_change_sends_one_modify_not_cancel_and_place():
    tr, store, clock, fake, _ = make_live()
    tick(tr, clock, T0)
    fake.bids = [[65001.0, 5.0]]
    tick(tr, clock, T0 + 1, bid=65001.0)  # the bid moved up (not ours: size 5)
    assert [c[0] for c in quote_actions(fake)] == ["order", "modify"]
    assert calls(fake, "modify")[0][1] == 1000 and calls(fake, "modify")[0][5] == 65002.0
    assert tr.maker_view()["quote"]["oid"] == 1001  # the modify's answer is adopted


def test_side_change_is_one_modify():
    tr, store, clock, fake, model = make_live()
    tick(tr, clock, T0)
    model.p = 0.2
    tick(tr, clock, T0 + 1)
    (mod,) = calls(fake, "modify")
    assert (mod[3], mod[5]) == (False, 65002.0)  # a sell one tick below the ask
    assert len(calls(fake, "order")) == 1 and calls(fake, "bulk_cancel") == []


def test_quote_priced_from_a_paper_book_and_paper_book_is_not_stripped():
    tr, store, clock, _ = make_paper()
    tick(tr, clock, T0, bid=65001.0, bid_sz=SZ)  # a paper quote is not in the real book
    (q,) = tr._executor.open_quotes()
    assert (q.side, q.px) == ("buy", 65002.0)


def test_old_book_is_not_quoted():
    tr, store, clock, fake, _ = make_live()
    clock.t = T0 + 3.01
    run(tr.on_book(mkbook(recv_ts=T0)))  # 3.01 s old by the time it is quoted
    assert calls(fake, "order") == [] and events(store, "quote_skipped")
    assert len(store.decisions()) == 1  # still a decision, just no quote


# ---- the cap in the loop ----


def test_cap_forces_the_quote_to_the_other_side_in_the_loop():
    tr, store, clock, fake, _ = make_live()
    tick(tr, clock, T0)
    fake.taker_hits(1000, SZ)  # our buy filled: long $10.40 of a $15 cap
    tick(tr, clock, T0 + 5)  # the 5 s fill poll books it
    assert tr.ledger.position == pytest.approx(SZ)
    tick(tr, clock, T0 + 6)
    assert store.decisions()[-1]["reason"] == "capped"
    assert tr.maker_view()["capped"] is True
    order = calls(fake, "order")[-1]
    assert (order[2], order[4]) == (False, 65002.0)  # a sell, though the model said buy


# ---- post-only rejection ----


def test_rejected_quote_is_logged_not_counted_as_order_error():
    fake = FakeHL(bids=[[65000.0, 5.0]], asks=[[65001.0, 5.0]], t0_ms=int(T0 * 1000))
    tr, store, clock, _, _ = make_live(fake=fake)  # the venue's ask moved to our price
    tick(tr, clock, T0)
    assert len(events(store, "quote_rejected")) == 1 and events(store, "order_error") == []
    assert tr.maker_view()["quote"] is None


def test_three_rejections_do_not_trip_the_kill_switch():
    fake = FakeHL(bids=[[65000.0, 5.0]], asks=[[65001.0, 5.0]], t0_ms=int(T0 * 1000))
    tr, store, clock, _, _ = make_live(fake=fake)
    for i in range(4):
        tick(tr, clock, T0 + i)
    assert len(events(store, "quote_rejected")) == 4
    assert not tr.stopped and tr.kill_reason is None


def test_rejected_quote_is_repriced_at_next_decision():
    fake = FakeHL(bids=[[65000.0, 5.0]], asks=[[65001.0, 5.0]], t0_ms=int(T0 * 1000))
    tr, store, clock, _, _ = make_live(fake=fake)
    tick(tr, clock, T0)
    tick(tr, clock, T0 + 1, ask=65001.0)  # the book caught up: one-tick spread
    assert [c[4] for c in calls(fake, "order")] == [65001.0, 65000.0]  # now at the touch
    assert tr.maker_view()["quote"]["px"] == 65000.0


def test_rejected_modify_reconciles_open_orders():
    tr, store, clock, fake, _ = make_live()
    tick(tr, clock, T0)
    no_polls(tr, clock)
    fake.asks = [[65002.0, 5.0]]  # the venue moved; our book has not yet
    fake.bids = [[65001.0, 5.0]]
    tick(tr, clock, T0 + 1, bid=65001.0)  # modify to 65002 crosses: rejected
    assert len(events(store, "quote_rejected")) == 1
    assert tr.maker_view()["quote"] == "unknown"
    n_reads = len(calls(fake, "open_orders"))
    tick(tr, clock, T0 + 2, bid=65000.0, ask=65003.0)
    assert len(calls(fake, "open_orders")) > n_reads  # re-read before acting again
    assert calls(fake, "order")[-1][4] == 65001.0  # nothing rested, so a fresh place


def test_unknown_quote_adopts_a_single_open_order():
    tr, store, clock, fake, _ = make_live()
    tick(tr, clock, T0)
    no_polls(tr, clock)
    tr._quote = UNKNOWN
    tick(tr, clock, T0 + 1)
    assert tr.maker_view()["quote"]["oid"] == 1000 and len(quote_actions(fake)) == 1
    assert events(store, "quote_adopted")


def test_unknown_quote_with_several_open_orders_cancels_them_all():
    tr, store, clock, fake, _ = make_live()
    tr._executor.place_quote("buy", SZ, 64990, None, 0.0)
    tr._executor.place_quote("buy", SZ, 64991, None, 0.0)
    tr._quote = UNKNOWN
    tick(tr, clock, T0)
    assert calls(fake, "bulk_cancel")[0][1] == [{"coin": "BTC", "oid": 1000},
                                               {"coin": "BTC", "oid": 1001}]
    assert [o["oid"] for o in fake.resting] == [1002]  # then our own quote


# ---- fills ----


def test_partial_fill_booked_once_and_remaining_reduced():
    tr, store, clock, fake, _ = make_live()
    tick(tr, clock, T0)
    fake.taker_hits(1000, 0.00005)
    tick(tr, clock, T0 + 5)
    tick(tr, clock, T0 + 10)  # polled again: the same fill is not booked twice
    (f,) = store.fills()
    assert (f["oid"], f["sz"], f["side"]) == (1000, 0.00005, "buy")
    assert f["fee"] == pytest.approx(65001.0 * 0.00005 * 0.00015)
    assert tr.ledger.position == pytest.approx(0.00005)
    assert tr.maker_view()["quote"]["remaining"] == pytest.approx(0.00011)


def test_partial_fill_same_price_keeps_remainder_resting():
    tr, store, clock, fake, _ = make_live()
    tick(tr, clock, T0)
    fake.taker_hits(1000, 0.00005)
    for i in range(5, 12):
        tick(tr, clock, T0 + i)
    assert len(quote_actions(fake)) == 1  # the remainder rests, keeping its queue place
    assert fake.resting[0]["oid"] == 1000


def test_fully_filled_quote_is_cleared_and_replaced():
    tr, store, clock, fake, model = make_live()
    tick(tr, clock, T0)
    fake.taker_hits(1000, SZ)
    model.p = 0.2  # flat again after a sell would be fine; ask for a sell
    tick(tr, clock, T0 + 5)  # poll books the fill, clears tracking, places a fresh quote
    assert tr.ledger.position == pytest.approx(SZ)
    assert [c[0] for c in quote_actions(fake)] == ["order", "order"]
    assert calls(fake, "order")[-1][2] is False


def test_modify_of_a_filled_order_syncs_fills_without_double_booking():
    tr, store, clock, fake, model = make_live()
    tick(tr, clock, T0)
    no_polls(tr, clock)
    fake.taker_hits(1000, SZ)  # filled; the poll has not seen it
    model.p = 0.2
    tick(tr, clock, T0 + 1)  # side change: modify of a filled order is "gone"
    assert len(events(store, "quote_gone")) == 1
    assert len(store.fills()) == 1 and tr.ledger.position == pytest.approx(SZ)
    tr._last_poll = 0.0
    tick(tr, clock, T0 + 2)  # the regular poll reads the same fill again
    assert len(store.fills()) == 1 and tr.ledger.position == pytest.approx(SZ)


def test_open_orders_reconcile_clears_vanished_and_cancels_stray():
    tr, store, clock, fake, _ = make_live()
    tick(tr, clock, T0)
    fake.taker_hits(1000, SZ)  # ours vanishes (filled)
    fake.resting.append({"coin": "BTC", "side": "A", "limitPx": "66000.0", "sz": "0.001",
                         "oid": 77, "timestamp": 1, "origSz": "0.001"})  # placed in the UI
    tr._last_poll = clock.t + 10_000  # only the open-orders check runs
    tick(tr, clock, T0 + 15)
    assert len(events(store, "stray_order")) == 1
    assert {"coin": "BTC", "oid": 77} in calls(fake, "bulk_cancel")[0][1]
    assert len(store.fills()) == 1  # the vanished quote's fill was synced
    assert all(o["oid"] != 77 for o in fake.resting)


def test_live_fill_poll_runs_every_5_s_not_every_tick():
    tr, store, clock, fake, _ = make_live()
    for i in range(11):
        tick(tr, clock, T0 + i)
    assert len(calls(fake, "user_fills_by_time")) == 3  # at 0, 5 and 10 s


def test_paper_print_through_the_quote_is_booked_by_the_trader():
    tr, store, clock, _ = make_paper()
    tick(tr, clock, T0)  # buy quote at 65001
    tr.tape.add([Print(T0 + 0.5, T0 + 0.5, "sell", 65001.0, 1.0, 1),  # at our price: no fill
                 Print(T0 + 0.6, T0 + 0.6, "sell", 65000.0, 1.0, 2)], T0 + 0.6)
    tick(tr, clock, T0 + 1)
    (f,) = store.fills()
    assert (f["side"], f["sz"], f["px"]) == ("buy", SZ, 65001.0)
    assert tr.ledger.position == pytest.approx(SZ)
    tick(tr, clock, T0 + 2)  # the same prints are never matched twice
    assert len(store.fills()) == 1


def test_follow_trades_feeds_the_tape():
    tr, store, clock, _ = make_paper()

    async def stream():
        yield [Print(T0, T0, "buy", 1.0, 1.0, 1)]
        yield [Print(T0, T0, "buy", 1.0, 1.0, 1), Print(T0, T0, "sell", 1.0, 2.0, 2)]

    run(tr.follow_trades(stream()))
    assert [p.tid for p in tr.tape.recent(5)] == [1, 2]


# ---- stopping cancels every resting order ----


@pytest.mark.parametrize("mode", ["paper", "live"])
def test_stale_pause_cancels_resting_quote_before_closing(mode):
    if mode == "paper":
        tr, store, clock, _ = make_paper()
        tick(tr, clock, T0)
        assert tr._executor.open_quotes()
    else:
        tr, store, clock, fake, _ = make_live()
        tick(tr, clock, T0)
        assert fake.resting
    clock.t = T0 + 15.01
    assert run(tr.check_stale()) == "stale_book"
    assert tr.paused and tr.maker_view()["quote"] is None
    assert tr._executor.open_quotes() == []
    assert events(store, "quote_cancel")


def test_no_quote_while_paused_decisions_held_paused():
    tr, store, clock, _ = make_paper()
    tick(tr, clock, T0)
    clock.t = T0 + 15.01
    run(tr.check_stale())
    for i in range(5):
        tick(tr, clock, T0 + 20 + i)
    assert tr._executor.open_quotes() == []
    assert [d["reason"] for d in store.decisions()][-5:] == ["paused"] * 5


def test_resume_blocked_while_an_order_still_rests():
    tr, store, clock, fake, _ = make_live(cfg=replace(MCFG, resume_after_s=0))
    tick(tr, clock, T0)
    clock.t = T0 + 15.01
    run(tr.check_stale())
    fake.resting.append({"coin": "BTC", "side": "B", "limitPx": "64000.0", "sz": "0.001",
                         "oid": 55, "timestamp": 1, "origSz": "0.001"})  # left behind
    tick(tr, clock, T0 + 20)
    assert tr.paused and events(store, "stale_resume_blocked")
    assert fake.resting == []  # the blocked resume flattened again, which cancels
    tick(tr, clock, T0 + 21)
    assert not tr.paused


def test_daily_loss_kill_cancels_quote():
    cfg = replace(MCFG, max_daily_loss_usd=0.01)
    tr, store, clock, fake, _ = make_live(cfg=cfg)
    tick(tr, clock, T0)
    fake.taker_hits(1000, 0.00005)  # part fills, the rest still rests
    fake.mid = 64800.0
    tick(tr, clock, T0 + 5, bid=64800.0, ask=64803.0)  # long and $200 lower: over the limit
    assert tr.stopped and tr.kill_reason == "daily_loss"
    assert fake.resting == [] and fake.position == pytest.approx(0.0, abs=1e-12)


def test_order_error_kill_cancels_quote():
    tr, store, clock, fake, model = make_live()
    tick(tr, clock, T0)
    no_polls(tr, clock)
    fake.fail_modify = [ConnectionError("down")] * 3
    model.p = 0.2  # a sell: each try is a modify of the buy, which is re-read and adopted
    for i in range(3):
        tick(tr, clock, T0 + 1 + i)
    assert tr.stopped and tr.kill_reason == "order_errors"
    assert fake.resting == [] and len(events(store, "order_error")) == 3


def test_shutdown_cancels_resting_quote():
    tr, store, clock, fake, _ = make_live()
    tick(tr, clock, T0)
    run(tr.shutdown())
    assert fake.resting == [] and tr.maker_view()["quote"] is None


def test_halt_cancel_is_never_blocked_by_the_cap():
    tr, store, clock, fake, _ = make_live()
    tick(tr, clock, T0)
    tr._actions = [clock.t] * 25  # well over the cap
    run(tr.shutdown())
    assert fake.resting == [] and calls(fake, "bulk_cancel")


@pytest.mark.parametrize("how", ["halt", "pause"])
def test_quote_waiting_for_lock_is_not_sent_after_halt_or_pause(how):
    tr, store, clock, fake, _ = make_live()
    no_polls(tr, clock)

    async def scenario():
        await tr._lock.acquire()
        t = asyncio.create_task(tr.on_book(mkbook(recv_ts=T0)))
        await asyncio.sleep(0.05)  # decided, now waiting for the lock to quote
        h = asyncio.create_task(tr._halt("daily_loss") if how == "halt" else tr._pause())
        await asyncio.sleep(0.05)
        tr._lock.release()
        await t
        await h

    run(scenario())
    assert calls(fake, "order") == [] and events(store, "quote_skipped")


def test_late_answer_leaves_quote_untouched():
    tr, store, clock, fake, _ = make_live()
    tick(tr, clock, T0)
    tr._model = StubModel(p=0.2, clock=clock, advance=1.01)  # over the 1 s maker deadline
    for i in range(1, 4):
        tick(tr, clock, T0 + 2 * i)
    assert [d["reason"] for d in store.decisions()][1:] == ["late"] * 3
    assert len(quote_actions(fake)) == 1 and fake.resting[0]["oid"] == 1000


def test_invalid_answer_leaves_quote_untouched():
    tr, store, clock, fake, _ = make_live()
    tick(tr, clock, T0)
    tr._model = StubModel(p=ValueError("broken"))
    tick(tr, clock, T0 + 1)
    tr._model = StubModel(p=1.5)
    tick(tr, clock, T0 + 2)
    assert [d["reason"] for d in store.decisions()][1:] == ["invalid"] * 2
    assert len(quote_actions(fake)) == 1 and len(fake.resting) == 1


def test_a_failed_quote_counts_as_an_order_error_and_is_rechecked():
    tr, store, clock, fake, _ = make_live()
    fake.fail_orders = [ConnectionError("wifi off")]
    tick(tr, clock, T0)
    assert len(events(store, "order_error")) == 1 and tr.maker_view()["quote"] == "unknown"
    tick(tr, clock, T0 + 1)
    assert tr.maker_view()["quote"]["px"] == 65001.0


# ---- the order-action cap and rate limits ----


def flip(tr, clock, model, n, start=T0, step=1.0):
    for i in range(n):
        model.p = 0.9 if i % 2 == 0 else 0.2
        tick(tr, clock, start + i * step)


def test_actions_capped_at_20_per_rolling_minute():
    tr, store, clock, fake, model = make_live()
    no_polls(tr, clock)
    flip(tr, clock, model, 25)  # every decision wants a new quote
    assert len(quote_actions(fake)) == 19  # one slot of the 20 is kept for a cancel
    # capped at decisions 20, 22 and 24; 21, 23 and 25 want the buy that still rests
    assert len(events(store, "action_cap")) == 3
    assert tr.maker_view()["actions_last_min"] == 19


def test_action_window_rolls_off_after_60_s():
    tr, store, clock, fake, model = make_live()
    no_polls(tr, clock)
    flip(tr, clock, model, 19)  # T0 .. T0+18: the cap is full; the last quote is a buy
    model.p = 0.2
    tick(tr, clock, T0 + 60.0)  # the first action (T0) is exactly 60 s old: out of the window
    assert len(quote_actions(fake)) == 20
    model.p = 0.9
    tick(tr, clock, T0 + 60.37)  # T0+1 is still 59.37 s old: capped
    assert len(quote_actions(fake)) == 20 and events(store, "action_cap")
    tick(tr, clock, T0 + 61.01)
    assert len(quote_actions(fake)) == 21


def test_failed_and_rejected_actions_count_towards_the_cap():
    fake = FakeHL(bids=[[65000.0, 5.0]], asks=[[65001.0, 5.0]], t0_ms=int(T0 * 1000))
    tr, store, clock, _, _ = make_live(fake=fake)
    no_polls(tr, clock)
    for i in range(22):
        tick(tr, clock, T0 + i)
    assert len(calls(fake, "order")) == 19


def test_rate_limit_error_cancels_and_pauses_quoting():
    tr, store, clock, fake, model = make_live()
    tick(tr, clock, T0)
    no_polls(tr, clock)
    fake.fail_modify = [HTTP429()]
    model.p = 0.2
    tick(tr, clock, T0 + 1)
    assert len(events(store, "rate_limited")) == 1 and fake.resting == []
    assert tr.maker_view()["rate_paused_until"] == pytest.approx(T0 + 61)
    for i in range(2, 30):
        tick(tr, clock, T0 + i)
    assert [d["reason"] for d in store.decisions()][2:] == ["rate_paused"] * 28
    assert len(calls(fake, "order")) == 1 and not tr.stopped


def test_quoting_resumes_after_rate_pause():
    tr, store, clock, fake, model = make_live()
    no_polls(tr, clock)
    fake.fail_orders = [HTTP429()]
    tick(tr, clock, T0)
    tick(tr, clock, T0 + 59.9)
    assert len(calls(fake, "order")) == 1
    tick(tr, clock, T0 + 60.0)
    assert len(calls(fake, "order")) == 2 and fake.resting


def test_three_rate_limit_errors_trip_order_error_kill():
    tr, store, clock, fake, model = make_live()
    no_polls(tr, clock)
    fake.fail_orders = [HTTP429()] * 3
    for i in range(3):
        tick(tr, clock, T0 + 61 * i)
    assert tr.stopped and tr.kill_reason == "order_errors"


def test_address_budget_logged_every_minute():
    tr, store, clock, fake, _ = make_live()
    for i in range(0, 125, 5):
        tick(tr, clock, T0 + i)
    assert events(store, "address_budget") == ["321/11234"] * 3  # at 0, 60 and 120 s
    assert tr.maker_view()["budget"] == {"used": 321, "cap": 11234}


# ---- calibration: score a decision only on a changed book ----


def test_scores_only_decisions_on_a_changed_book():
    tr, store, clock, _ = make_paper()
    for i in range(4):
        tick(tr, clock, T0 + i, bid=65000.0 if i < 3 else 65001.0)
    for i in range(30, 40):
        tick(tr, clock, T0 + i, bid=65005.0, ask=65008.0)
    ids = [d["id"] for d in store.decisions()]
    resolved = [i for i in ids[:4] if store.outcomes(i)]
    assert resolved == [ids[0], ids[3]]  # the 2nd and 3rd saw the same book as the 1st
    assert len(store.decisions()) == 14  # every decision is still recorded


def test_held_decisions_without_p_up_do_not_reset_the_scored_book():
    tr, store, clock, model = make_paper()
    tick(tr, clock, T0)
    tr._model = StubModel(p=ValueError("x"))
    tick(tr, clock, T0 + 1, bid=65001.0)
    tr._model = model
    tick(tr, clock, T0 + 2)  # same book as the first scored one: not scored again
    tick(tr, clock, T0 + 3, bid=65001.0)  # differs from the last SCORED book: scored
    for i in range(33, 40):
        tick(tr, clock, T0 + i)
    ids = [d["id"] for d in store.decisions()]
    assert [i for i in ids[:4] if store.outcomes(i)] == [ids[0], ids[3]]


def test_maker_holds_are_stored_with_the_maker_gate():
    tr, store, clock, _ = make_paper()
    clock.t = T0 + 20
    run(tr.on_book(mkbook(recv_ts=T0)))  # stale
    assert store.decisions()[0]["gate"] == 0.5 and store.decisions()[0]["reason"] == "stale"


def test_maker_decision_record():
    tr, store, clock, _ = make_paper()
    tick(tr, clock, T0)
    d = store.decisions()[0]
    assert (d["action"], d["reason"], d["size_usd"], d["gate"], d["p_up"]) == (
        "buy", "signal", 11.0, 0.5, 0.9)


# ---- error branches ----


def test_unclear_modify_reply_reads_open_orders_before_the_next_quote():
    tr, store, clock, fake, model = make_live()
    tick(tr, clock, T0)
    no_polls(tr, clock)
    fake.modify_response = {"status": "ok", "response": {"type": "default"}}
    model.p = 0.2
    tick(tr, clock, T0 + 1)
    assert tr.maker_view()["quote"] == "unknown" and events(store, "quote_unknown")
    n_reads = len(calls(fake, "open_orders"))
    tick(tr, clock, T0 + 2)
    assert len(calls(fake, "open_orders")) == n_reads + 1
    assert events(store, "quote_adopted") == ["oid 1000"]  # the venue still had the buy


def test_a_post_only_order_that_took_liquidity_books_its_fills():
    tr, store, clock, fake, _ = make_live()
    fake.order_response = {"status": "ok", "response": {"type": "order", "data": {"statuses": [
        {"filled": {"totalSz": "0.00016", "avgPx": "65001.0", "oid": 999}}]}}}
    fake._record(999, "BTC", True, SZ, 65001.0)  # what the venue booked
    tr._last_poll = clock.t + 10_000
    tick(tr, clock, T0)
    assert events(store, "quote_filled") and tr.maker_view()["quote"] == "unknown"
    assert len(store.fills()) == 1 and tr.ledger.position == pytest.approx(SZ)


def test_a_failed_cancel_on_stop_is_logged_and_the_flatten_still_cancels():
    tr, store, clock, fake, _ = make_live()
    tick(tr, clock, T0)
    fake.fail_cancel = [ConnectionError("blip")]
    run(tr.shutdown())
    assert events(store, "cancel_error") and fake.resting == []  # the flatten's own cancel
    assert tr.maker_view()["quote"] is None


def test_a_failed_rate_limit_cancel_leaves_the_quote_unknown():
    tr, store, clock, fake, model = make_live()
    tick(tr, clock, T0)
    no_polls(tr, clock)
    fake.fail_modify = [HTTP429()]
    fake.fail_cancel = [ConnectionError("blip")]
    model.p = 0.2
    tick(tr, clock, T0 + 1)
    assert events(store, "cancel_error") and tr.maker_view()["quote"] == "unknown"


def test_a_failed_stray_cancel_is_logged_and_retried_at_the_next_check():
    tr, store, clock, fake, _ = make_live()
    tick(tr, clock, T0)
    fake.resting.append({"coin": "BTC", "side": "A", "limitPx": "66000.0", "sz": "0.001",
                         "oid": 77, "timestamp": 1, "origSz": "0.001"})
    fake.fail_cancel = [ConnectionError("blip")]
    tick(tr, clock, T0 + 15)
    assert events(store, "cancel_error") and any(o["oid"] == 77 for o in fake.resting)
    assert tr.maker_view()["quote"]["oid"] == 1000  # our own quote is still tracked
    tick(tr, clock, T0 + 30)
    assert events(store, "stray_order") and all(o["oid"] != 77 for o in fake.resting)


def test_unreadable_open_orders_keep_the_quote_as_it_was():
    tr, store, clock, fake, _ = make_live()
    tick(tr, clock, T0)
    real = fake.open_orders
    fake.open_orders = lambda *a: (_ for _ in ()).throw(ConnectionError("down"))
    tick(tr, clock, T0 + 15)
    assert tr.maker_view()["quote"]["oid"] == 1000 and events(store, "sync_error")
    tr._quote = UNKNOWN
    tick(tr, clock, T0 + 16)  # unknown and unreadable: no quote is sent
    assert events(store, "quote_skipped")[-1].endswith("open orders unknown")
    assert len(calls(fake, "order")) == 1
    fake.open_orders = real


def test_resume_waits_when_open_orders_cannot_be_read():
    tr, store, clock, fake, _ = make_live(cfg=replace(MCFG, resume_after_s=0))
    tick(tr, clock, T0)
    clock.t = T0 + 15.01
    run(tr.check_stale())
    real = fake.open_orders
    fake.open_orders = lambda *a: (_ for _ in ()).throw(ConnectionError("down"))
    fake.fail_cancel = []
    tick(tr, clock, T0 + 20)
    assert tr.paused
    fake.open_orders = real
    tick(tr, clock, T0 + 21)
    tick(tr, clock, T0 + 22)
    assert not tr.paused


def test_budget_read_failure_is_logged_and_does_not_stop_the_bot():
    tr, store, clock, fake, _ = make_live()
    fake.user_rate_limit = lambda user: (_ for _ in ()).throw(ConnectionError("down"))
    tick(tr, clock, T0)
    assert any(e.startswith("budget:") for e in events(store, "sync_error"))
    assert not tr.stopped and tr.maker_view()["budget"] is None and calls(fake, "order")


def test_no_quote_when_our_order_is_the_whole_side():
    tr, store, clock, fake, model = make_live()
    tick(tr, clock, T0)
    no_polls(tr, clock)
    clock.t = T0 + 1
    alone = Book(ts=clock.t, recv_ts=clock.t, coin="BTC", bids=[[65001.0, SZ]],
                 asks=[[65003.0, 5.0]])
    run(tr.on_book(alone))
    assert events(store, "quote_skipped")[-1].endswith("our order is the whole side")
    assert len(quote_actions(fake)) == 1 and len(store.decisions()) == 2


def test_maker_latency_is_logged_with_rest_action_and_loop_times():
    import json

    tr, store, clock, fake, _ = make_live()
    clock.t = T0 + 0.2
    run(tr.on_book(replace(mkbook(recv_ts=T0), read_ms=123.0)))
    tick(tr, clock, T0 + 60.5)
    first, second = (json.loads(e) for e in events(store, "maker_latency"))
    assert first["rest"]["n"] == 1 and second["rest"]["max"] == 123.0
    assert second["action"]["n"] == 1
    assert second["loop"]["p50"] == pytest.approx(200.0)  # book received to quote sent


def test_decision_and_outcome_mids_exclude_our_own_quote():
    """Live, our quote sits one tick inside the touch and moves the raw mid toward our side
    (which follows p_up): labels must use the market's mid without it."""
    tr, store, clock, fake, _ = make_live()
    tick(tr, clock, T0)  # buy quote at 65001; market mid 65001.5
    no_polls(tr, clock)
    tick(tr, clock, T0 + 1, bid=65001.0, bid_sz=SZ)  # our bid is the touch: raw mid 65002.0
    first, second = store.decisions()
    assert first["mid"] == 65001.5 and second["mid"] == 65001.5
    tick(tr, clock, T0 + 30, bid=65001.0, bid_sz=SZ)
    (o30,) = store.outcomes(first["id"])
    assert o30["mid_future"] == 65001.5 and o30["label"] is None  # unchanged market: a tie
