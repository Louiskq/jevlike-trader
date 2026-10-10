"""A stale-book kill pauses the bot (cancel, flatten, no entries) and it resumes once books have
flowed steadily for a settling period, at most stale_resumes_max times per run. Daily-loss and
order-error kills stay permanent. Clocks are injected; nothing really waits."""

import asyncio

import pytest

from jevhl.config import Config
from test_trader import T0, mkbook
from test_trader import make as make_paper
from test_trader_live import down, events, order_calls
from test_trader_live import make as make_live

STALL = 15.01  # seconds after the last book


def run(coro):
    return asyncio.run(coro)


def stall(tr, clock, since=T0):
    """One book at `since`, then silence past the stale bound; the watchdog fires."""
    run(tr.on_book(mkbook(recv_ts=since)))
    clock.t = since + STALL
    return run(tr.check_stale())


def books(tr, clock, start, count, step=5.0):
    for i in range(count):
        clock.t = start + i * step
        run(tr.on_book(mkbook(recv_ts=clock.t)))


def holds(store, reason):
    return [d for d in store.decisions() if d["reason"] == reason]


# ---- the pause ----


def test_a_stale_kill_pauses_instead_of_stopping_and_still_flattens():
    tr, store, clock, fake, _ = make_live()
    assert stall(tr, clock) == "stale_book"
    assert tr.paused and not tr.stopped and tr.kill_reason is None
    assert fake.position == pytest.approx(0.0, abs=1e-12) and tr.ledger.position == 0.0
    assert order_calls(fake)[-1][6] is True  # reduce-only
    assert len(events(store, "stale_pause")) == 1 and events(store, "kill_switch") == []
    assert tr.stale_pauses == 1 and tr.stale_resumes == 0


def test_pause_prints_one_terminal_line(capsys):
    tr, store, clock, _ = make_paper()
    stall(tr, clock)
    err = capsys.readouterr().err
    assert err.count("STALE PAUSE") == 1


def test_no_entries_while_paused_and_decisions_are_held_as_paused():
    tr, store, clock, model = make_paper()
    stall(tr, clock)
    calls = model.calls
    books(tr, clock, T0 + 20, 5)
    assert model.calls == calls  # the model is not even asked
    assert len(holds(store, "paused")) == 5 and len(store.fills()) == 2  # entry and flatten
    assert tr.ledger.position == 0.0 and tr.paused


def test_repeated_watchdog_ticks_do_not_pause_twice():
    tr, store, clock, _ = make_paper()
    stall(tr, clock)
    for _ in range(3):
        clock.t += 20
        run(tr.check_stale())
    assert tr.stale_pauses == 1 and len(events(store, "stale_pause")) == 1


def test_a_decision_in_flight_when_the_pause_starts_places_no_order():
    from test_trader import StubModel

    tr, store, clock, model = make_paper(model=StubModel(p=0.9, real_sleep=0.2))
    model.real_sleep = 0.0
    run(tr.on_book(mkbook(recv_ts=T0)))  # long, so we have something to flatten
    n_fills = len(store.fills())
    model.real_sleep = 0.2

    async def scenario():
        t = asyncio.create_task(tr.on_book(mkbook(recv_ts=T0 + 1)))
        await asyncio.sleep(0.05)
        clock.t = T0 + 20
        await tr.check_stale()
        await t

    run(scenario())
    assert [f["side"] for f in store.fills()][n_fills:] == ["sell"]  # only the flatten
    assert len(holds(store, "paused")) == 1 and holds(store, "killed") == []


# ---- resuming ----


def test_trading_resumes_after_books_have_flowed_for_the_settling_period():
    tr, store, clock, model = make_paper()
    stall(tr, clock)
    books(tr, clock, T0 + 20, 12)  # T0+20 .. T0+75: 55 s of steady books
    assert tr.paused
    books(tr, clock, T0 + 80, 1)  # 60 s since the first book of the run of books
    assert not tr.paused and tr.stale_resumes == 1
    assert len(events(store, "stale_resume")) == 1
    books(tr, clock, T0 + 85, 1)
    assert tr.ledger.position > 0  # entries are allowed again
    assert max(d["ts"] for d in holds(store, "paused")) < T0 + 80  # none after the resume


def test_resume_prints_one_terminal_line(capsys):
    tr, store, clock, _ = make_paper()
    stall(tr, clock)
    books(tr, clock, T0 + 20, 13)
    assert capsys.readouterr().err.count("STALE RESUME") == 1


def test_the_settling_period_is_configurable_and_zero_resumes_at_the_first_book():
    tr, store, clock, _ = make_paper(cfg=Config(resume_after_s=0))
    stall(tr, clock)
    books(tr, clock, T0 + 20, 1)
    assert not tr.paused


def test_a_gap_over_the_bound_restarts_the_settling_period():
    tr, store, clock, _ = make_paper()
    stall(tr, clock)
    books(tr, clock, T0 + 20, 10)  # T0+20 .. T0+65, 45 s settled
    books(tr, clock, T0 + 74.01, 1)  # a 9.01 s gap: restart at this book
    books(tr, clock, T0 + 79.01, 11)  # up to T0+129.01: 55 s since the restart
    assert tr.paused
    books(tr, clock, T0 + 134.01, 1)  # 60 s since the restart
    assert not tr.paused


def test_a_gap_exactly_at_the_bound_does_not_restart():
    tr, store, clock, _ = make_paper()
    stall(tr, clock)
    books(tr, clock, T0 + 20, 1)
    books(tr, clock, T0 + 28, 1)  # gap 8.0 == bound: fine
    books(tr, clock, T0 + 80, 1)  # but this gap is 52 s: restart
    assert tr.paused
    clock.t = T0 + 80
    t = [T0 + 80 + 8 * i for i in range(1, 9)]  # steady 8 s gaps to 64 s
    for x in t:
        clock.t = x
        run(tr.on_book(mkbook(recv_ts=x)))
    assert not tr.paused


def test_a_second_stall_while_paused_does_not_pause_again_but_restarts_settling():
    tr, store, clock, _ = make_paper()
    stall(tr, clock)
    books(tr, clock, T0 + 20, 6)  # 25 s settled
    clock.t = T0 + 100  # silence again: the watchdog must not re-pause
    run(tr.check_stale())
    assert tr.stale_pauses == 1
    books(tr, clock, T0 + 100, 12)  # 55 s since the new run of books
    assert tr.paused
    books(tr, clock, T0 + 160, 1)
    assert not tr.paused and tr.stale_resumes == 1


# ---- bounded recoveries ----


def test_the_stale_kill_after_the_last_allowed_resume_is_permanent(capsys):
    tr, store, clock, _ = make_paper(cfg=Config(stale_resumes_max=2, resume_after_s=10))
    t = T0
    for _ in range(2):  # two recoveries
        stall(tr, clock, since=t)
        books(tr, clock, t + 20, 3)
        assert not tr.paused
        t += 100
    capsys.readouterr()
    stall(tr, clock, since=t)
    assert tr.stopped and not tr.paused
    assert tr.kill_reason == "stale_book"
    assert events(store, "kill_switch") == ["stale_book"]
    assert len(events(store, "stale_gave_up")) == 1
    assert tr.stale_pauses == 2 and tr.stale_resumes == 2
    assert "STALE GAVE UP" in capsys.readouterr().err
    n = len(store.decisions())
    books(tr, clock, t + 40, 20)
    assert len(store.decisions()) == n and tr.stopped  # never resumes


def test_zero_allowed_resumes_makes_the_first_stale_kill_permanent():
    tr, store, clock, _ = make_paper(cfg=Config(stale_resumes_max=0))
    stall(tr, clock)
    assert tr.stopped and tr.kill_reason == "stale_book" and not tr.paused
    assert events(store, "stale_pause") == [] and len(events(store, "stale_gave_up")) == 1


def test_the_resume_count_is_never_reset_within_a_run():
    tr, store, clock, _ = make_paper(cfg=Config(stale_resumes_max=1, resume_after_s=0))
    stall(tr, clock)
    books(tr, clock, T0 + 20, 1)
    # a long healthy stretch changes nothing
    books(tr, clock, T0 + 25, 100)
    stall(tr, clock, since=T0 + 1000)
    assert tr.stopped


# ---- other kills stay permanent ----


def test_a_daily_loss_kill_is_permanent_even_with_instant_resume():
    tr, store, clock, _ = make_paper(cfg=Config(max_daily_loss_usd=10, resume_after_s=0))
    run(tr.on_book(mkbook(recv_ts=T0)))
    clock.t = T0 + 5.3
    run(tr.on_book(mkbook(mid=50000.5, recv_ts=clock.t)))
    assert tr.stopped and tr.kill_reason == "daily_loss" and not tr.paused
    assert events(store, "stale_pause") == []
    books(tr, clock, T0 + 20, 20)
    assert tr.stopped and tr.kill_reason == "daily_loss"


def test_a_daily_loss_while_paused_stops_for_good():
    tr, store, clock, _ = make_paper(cfg=Config(max_daily_loss_usd=10, resume_after_s=0))
    stall(tr, clock)
    assert tr.paused
    tr.ledger.apply_fill("buy", 0.001, 65000.0, 50.0)  # a big loss is booked meanwhile
    books(tr, clock, T0 + 20, 1)
    assert tr.stopped and not tr.paused and tr.kill_reason == "daily_loss"
    assert events(store, "kill_switch") == ["daily_loss"]


def test_an_order_error_kill_is_permanent():
    tr, store, clock, fake, _ = make_live(cfg=Config(resume_after_s=0))
    fake.fail_orders = [ConnectionError("down")] * 3
    for i in range(3):
        clock.t = T0 + 5.3 * i
        run(tr.on_book(mkbook(recv_ts=clock.t)))
    assert tr.stopped and tr.kill_reason == "order_errors" and not tr.paused
    books(tr, clock, T0 + 40, 5)
    assert tr.stopped and events(store, "stale_resume") == []


# ---- interaction with the flatten retry ----


def test_an_unresolved_flatten_blocks_resume_until_a_flatten_succeeds():
    delays = []
    cfg = Config(flatten_retry_window_s=0)
    tr, store, clock, fake, _ = make_live(cfg=cfg, flatten_tries=1, delays=delays)
    run(tr.on_book(mkbook(recv_ts=T0)))
    fake.fail_state = down(40)  # the venue stays unreachable
    clock.t = T0 + STALL
    run(tr.check_stale())
    assert tr.paused and tr.flatten_unresolved and fake.position > 0
    books(tr, clock, T0 + 20, 30)  # 145 s of steady books: settled several times over
    assert tr.paused and tr.stale_resumes == 0 and events(store, "stale_resume") == []
    fake.fail_state = []  # the venue is back
    books(tr, clock, T0 + 200, 14)
    assert not tr.paused and tr.stale_resumes == 1
    assert not tr.flatten_unresolved and len(events(store, "flatten_recovered")) == 1


def test_a_flatten_still_retrying_blocks_resume_even_when_the_books_have_settled():
    gate = {}
    tr, store, clock, fake, _ = make_live(flatten_tries=1)

    async def hold_sleep(d):
        await gate["open"].wait()

    tr._sleep = hold_sleep
    run(tr.on_book(mkbook(recv_ts=T0)))
    fake.fail_state = down(2)  # the first attempt fails, the retry would work

    async def scenario():
        gate["open"] = asyncio.Event()
        clock.t = T0 + STALL
        flat = asyncio.create_task(tr.check_stale())
        await asyncio.sleep(0.05)  # the flatten is now waiting to retry
        assert tr._flatten_lock.locked() and tr.paused
        for i in range(14):  # 65 s of steady books while it retries
            clock.t = T0 + 20 + 5 * i
            await tr.on_book(mkbook(recv_ts=clock.t))
        assert tr.paused and tr.stale_resumes == 0
        gate["open"].set()
        await flat
        clock.t += 5
        await tr.on_book(mkbook(recv_ts=clock.t))

    run(asyncio.wait_for(scenario(), 5))  # a resume that ignores the running flatten deadlocks
    assert not tr.paused and tr.stale_resumes == 1
    assert len(events(store, "flatten_recovered")) == 1  # it finished first, then we resumed


def test_resume_checks_the_exchange_position_is_flat_and_closes_a_leftover():
    tr, store, clock, fake, _ = make_live()
    stall(tr, clock)
    fake._record(7, "BTC", True, 0.001, 65001.0)  # a position appears while paused
    books(tr, clock, T0 + 20, 13)  # the poll books the fill; settled at the 13th book
    assert tr.paused and len(events(store, "stale_resume_blocked")) == 1
    assert fake.position == pytest.approx(0.0, abs=1e-12)  # closed, reduce-only
    assert order_calls(fake)[-1][6] is True
    books(tr, clock, T0 + 85, 1)
    assert tr.paused  # a fresh settling period, not an instant resume
    books(tr, clock, T0 + 90, 10)  # to T0+135: 55 s since the restart at T0+80
    assert tr.paused
    books(tr, clock, T0 + 140, 1)
    assert not tr.paused


def test_resume_does_not_ask_the_exchange_when_the_executor_is_paper():
    tr, store, clock, _ = make_paper()
    stall(tr, clock)
    books(tr, clock, T0 + 20, 13)
    assert not tr.paused  # (PaperExchange has no position(): the ledger is the truth)


# ---- run loop and shutdown ----


def test_shutdown_during_a_pause_ends_the_run_and_clears_paused():
    tr, store, clock, _ = make_paper()

    async def feed():
        yield mkbook(recv_ts=T0)
        await asyncio.Event().wait()

    async def scenario():
        task = asyncio.create_task(tr.run(feed()))
        await asyncio.sleep(0.05)
        clock.t = T0 + 20
        await tr.check_stale()
        assert tr.paused and not task.done()  # the run keeps reading the feed
        tr.stopped = True
        await asyncio.wait_for(task, 1.0)

    run(scenario())
    assert tr.stopped and not tr.paused


# ---- every await re-checks the pause ----


def test_an_entry_waiting_for_the_exchange_is_skipped_if_the_bot_pauses_meanwhile():
    tr, store, clock, fake, _ = make_live()
    tr._last_poll = T0  # skip the backstop so the entry is what waits

    async def scenario():
        await tr._lock.acquire()  # the exchange is busy
        # the book is newer than the clock, so it is still fresh when the pause starts
        t = asyncio.create_task(tr.on_book(mkbook(recv_ts=T0 + 10)))
        await asyncio.sleep(0.1)  # the model has answered; the entry waits for the lock
        clock.t = T0 + STALL
        pausing = asyncio.create_task(tr.check_stale())  # its flatten queues behind the entry
        await asyncio.sleep(0.05)
        assert tr.paused
        tr._lock.release()
        await asyncio.gather(t, pausing)

    run(scenario())
    assert order_calls(fake) == [] and store.fills() == []
    assert any("decision" in d for d in events(store, "order_skipped"))


def test_an_exit_waiting_for_the_exchange_is_not_sent_if_the_bot_is_paused():
    tr, store, clock, fake, model = make_live()
    run(tr.on_book(mkbook(recv_ts=T0)))
    model.p = 0.5
    tr._last_poll = T0 + 60

    async def scenario():
        await tr._lock.acquire()
        clock.t = T0 + 60
        t = asyncio.create_task(tr.on_book(mkbook(recv_ts=clock.t)))
        await asyncio.sleep(0.05)
        tr._paused = True  # (the flatten that comes with a real pause owns the position)
        tr._lock.release()
        await t

    run(scenario())
    assert len(order_calls(fake)) == 1  # only the entry


def test_the_pause_flag_is_set_before_the_flatten_starts():
    tr, store, clock, fake, _ = make_live()
    run(tr.on_book(mkbook(recv_ts=T0)))
    seen = []
    real = tr._executor.flatten

    def spy(*a, **k):
        seen.append((tr.paused, tr.stopped))
        return real(*a, **k)

    tr._executor.flatten = spy
    clock.t = T0 + STALL
    run(tr.check_stale())
    assert seen == [(True, False)]


# ---- gaps found by break-it ----


def test_every_pause_gets_its_own_flatten_retry_window():
    """The first pause's retry sequence gave up; a later pause must still retry, not make a
    single attempt because the earlier sequence was spent."""
    delays = []
    cfg = Config(flatten_retry_window_s=100, resume_after_s=0)
    tr, store, clock, fake, model = make_live(cfg=cfg, flatten_tries=1, delays=delays)
    model.p = 0.5  # no entries: only the setup position matters
    fake._record(7, "BTC", True, 0.001, 65001.0)
    tr._last_poll = T0 - 100  # book it
    run(tr.on_book(mkbook(recv_ts=T0)))
    fake.fail_state = down(400)  # unreachable for the whole first pause
    clock.t = T0 + STALL
    run(tr.check_stale())
    assert tr.flatten_unresolved and len(delays) >= 3
    fake.fail_state = []
    books(tr, clock, T0 + 200, 2)  # recovers and resumes at once (resume_after_s=0)
    assert not tr.paused and not tr.flatten_unresolved
    delays.clear()
    fake._record(8, "BTC", True, 0.001, 65001.0)
    tr._last_poll = 0
    run(tr.on_book(mkbook(recv_ts=clock.t + 1)))
    fake.fail_state = down(4)
    clock.t += 1 + STALL
    run(tr.check_stale())
    assert delays == [1, 2]  # retried (and recovered), a fresh window
    assert not tr.flatten_unresolved


def test_two_books_settling_at_once_make_one_resume_check():
    tr, store, clock, fake, _ = make_live(cfg=Config(resume_after_s=0))
    stall(tr, clock)
    reads = []
    real = tr._executor.position

    def slow_position():
        import time

        reads.append(1)
        time.sleep(0.1)
        return real()

    tr._executor.position = slow_position

    async def scenario():
        clock.t = T0 + 20
        await asyncio.gather(tr.on_book(mkbook(recv_ts=T0 + 20)),
                             tr.on_book(mkbook(recv_ts=T0 + 21)))

    run(scenario())
    assert len(reads) == 1 and tr.stale_resumes == 1


def test_a_stop_while_the_resume_check_reads_the_exchange_cancels_the_resume():
    tr, store, clock, fake, _ = make_live(cfg=Config(resume_after_s=0))
    stall(tr, clock)
    real = tr._executor.position

    def stopping_position():
        tr.stopped = True  # e.g. Ctrl-C while the position read is in flight
        return real()

    tr._executor.position = stopping_position
    books(tr, clock, T0 + 20, 1)
    assert tr.stopped and tr.stale_resumes == 0 and events(store, "stale_resume") == []


def test_a_failed_position_read_blocks_the_resume():
    tr, store, clock, fake, _ = make_live(cfg=Config(resume_after_s=0))
    stall(tr, clock)

    def boom():
        raise ConnectionError("dns")

    tr._executor.position = boom
    books(tr, clock, T0 + 20, 1)
    assert tr.paused and len(events(store, "sync_error")) >= 1


def test_a_ledger_that_is_not_flat_blocks_the_resume_for_the_paper_executor_too():
    tr, store, clock, _ = make_paper()
    stall(tr, clock)
    tr.ledger.apply_fill("buy", 0.001, 65000.0, 0.0)  # the ledger believes it holds some
    books(tr, clock, T0 + 20, 13)
    assert tr.paused and len(events(store, "stale_resume_blocked")) == 1
    assert tr.ledger.position == 0.0  # flattened again


def test_a_flatten_that_fails_again_at_resume_time_keeps_the_bot_paused():
    """Even if the exchange then reads flat: an unresolved flatten is a failure to be sure."""
    tr, store, clock, fake, model = make_live(
        cfg=Config(flatten_retry_window_s=0), flatten_tries=1)
    model.p = 0.5  # nothing is ever opened: the exchange reads flat throughout
    fake.fail_state = down(2)
    stall(tr, clock)
    assert tr.paused and tr.flatten_unresolved
    fake.fail_state = down(2)  # the venue is still down for the flatten, then reads fine
    books(tr, clock, T0 + 20, 13)  # settled: one more flatten attempt, which fails
    assert tr.paused and tr.flatten_unresolved and events(store, "stale_resume") == []
    books(tr, clock, T0 + 85, 1)  # the settling period restarted
    assert tr.paused
    fake.fail_state = []
    books(tr, clock, T0 + 90, 13)  # settled again: the flatten works, then it resumes
    assert not tr.paused and not tr.flatten_unresolved


def test_a_stop_during_the_resume_time_flatten_ends_the_resume_before_any_position_read():
    tr, store, clock, fake, model = make_live(
        cfg=Config(flatten_retry_window_s=0), flatten_tries=1)
    model.p = 0.5
    fake.fail_state = down(2)
    stall(tr, clock)
    assert tr.paused and tr.flatten_unresolved
    fake.fail_state = []  # the venue is back: the resume-time flatten will succeed
    real_sync, flat_checks = tr._executor.sync_fills, []

    def stopping_sync():
        if tr._flatten_lock.locked():  # not the poll at the top of each book
            tr.stopped = True  # Ctrl-C lands while the resume-time flatten is running
        return real_sync()

    tr._executor.sync_fills = stopping_sync
    real_is_flat = tr._is_flat

    async def counting_is_flat():
        flat_checks.append(1)
        return await real_is_flat()

    tr._is_flat = counting_is_flat
    books(tr, clock, T0 + 20, 13)
    assert tr.stopped and flat_checks == []  # no exchange read for a resume that cannot happen
    assert tr.stale_resumes == 0 and events(store, "stale_resume") == []


def test_a_stale_pause_is_impossible_before_the_first_book():
    """So _settle always has a previous book (it needs no `prev is None` case)."""
    tr, store, clock, _ = make_paper()
    clock.t = T0 + 1000
    assert run(tr.check_stale()) is None and not tr.paused and tr.stale_pauses == 0
