"""Maker wiring (milestone 8): the dry run with REST books and the trades stream, live startup,
the dashboard summary and history, the report, reconcile's maker/taker split, the soak
warning and the database strategy warning. No network, no key."""

import asyncio
import time
from decimal import Decimal

import pytest

from fake_hl import FakeHL
from jevhl import reconcile as rc
from jevhl import run as run_mod
from jevhl.config import Config
from jevhl.exchange import Exchange
from jevhl.feed import Book, Print
from jevhl.maker import MakerTrader
from jevhl.rounding import AssetMeta
from jevhl.run import dry_run, format_report, prepare_live, strategy_warning, trader_live
from jevhl.server import HISTORY_DECISIONS, HISTORY_DECISIONS_MAKER, build_snapshot, build_summary
from jevhl.soak import MIN_DECISIONS_FOR_STUCK, mark_run_start, soak_report
from jevhl.store import Store
from jevhl.trader import Trader
from jevhl.types import Action, Decision
from test_live_run import _env
from test_maker import MCFG, SZ, make_live, mkbook, tick
from test_trader import T0, Clock, StubModel

BTC = AssetMeta("BTC", 5)
ADDR = "0x" + "ab" * 20


def books_at(clock, n, step=1.0):
    async def gen():
        for i in range(n):
            clock.t = T0 + i * step
            yield mkbook(recv_ts=clock.t)
            await asyncio.sleep(0.02)

    return gen()


def test_maker_dry_run_end_to_end_with_fake_feeds():
    clock = Clock()
    store = Store(":memory:")

    async def trades():
        while not any(e["kind"] == "quote_place" for e in store.events()):
            await asyncio.sleep(0.005)
        yield [Print(T0 + 0.5, T0 + 0.5, "sell", 64999.0, 1.0, 1)]  # through our 65001 bid
        await asyncio.Event().wait()

    tr = asyncio.run(dry_run(MCFG, store, BTC, books_at(clock, 8), model=StubModel(0.9),
                             clock=clock, trades=trades()))
    assert isinstance(tr, MakerTrader) and tr.error is None
    assert len(store.decisions()) == 8
    assert [e["detail"] for e in store.events() if e["kind"] == "strategy"] == ["maker"]
    first = store.fills()[0]
    assert (first["side"], first["sz"], first["px"]) == ("buy", SZ, 65001.0)
    assert first["fee"] == pytest.approx(65001.0 * SZ * 0.00015)
    assert abs(tr.ledger.position) < 1e-9 and tr._executor.open_quotes() == []  # shut down flat
    text = format_report(store, tr)
    assert "maker rest ms" in text and "maker action ms" in text and "maker loop ms" in text


def test_taker_is_the_default_and_unchanged():
    clock = Clock()
    store = Store(":memory:")
    tr = asyncio.run(dry_run(Config(), store, BTC, books_at(clock, 3, step=5.3),
                             model=StubModel(0.9), clock=clock))
    assert type(tr) is Trader
    assert [e["detail"] for e in store.events() if e["kind"] == "strategy"] == ["taker"]
    assert "maker" not in format_report(store, tr)
    assert "maker" not in trader_live(tr, clock)()


def test_a_broken_trades_stream_is_logged_and_the_run_goes_on():
    clock = Clock()
    store = Store(":memory:")

    async def trades():
        raise RuntimeError("bad stream")
        yield  # pragma: no cover

    tr = asyncio.run(dry_run(MCFG, store, BTC, books_at(clock, 3), model=StubModel(0.9),
                             clock=clock, trades=trades()))
    assert len(store.decisions()) == 3 and tr.error is None
    assert any("bad stream" in e["detail"] for e in store.events() if e["kind"] == "trades_error")


def test_main_wires_the_book_pollers_rate_limit_to_the_maker(tmp_path, monkeypatch):
    _env(monkeypatch, key=False, addr=False)
    monkeypatch.setenv("STRATEGY", "maker")
    monkeypatch.setenv("MAX_POSITION_USD", "15")
    monkeypatch.setattr(run_mod, "fetch_meta", lambda coin: BTC)

    async def rest(*a, on_rate_limit, **k):
        yield mkbook(recv_ts=time.time())
        await asyncio.sleep(0.05)
        on_rate_limit(RuntimeError("HTTP Error 429: Too Many Requests"))
        for _ in range(2):
            yield mkbook(recv_ts=time.time())
            await asyncio.sleep(0.05)

    async def trades(*a, **k):
        await asyncio.Event().wait()
        yield  # pragma: no cover

    monkeypatch.setattr(run_mod, "poll_books", rest)
    monkeypatch.setattr(run_mod, "stream_trades", trades)
    monkeypatch.setattr(run_mod, "build_model", lambda cfg: StubModel(0.9))
    run_mod.main(["--db", str(tmp_path / "m.db"), "--seconds", "1"])
    store = Store(str(tmp_path / "m.db"))
    assert [e for e in store.events() if e["kind"] == "rate_limited"]
    assert [d["reason"] for d in store.decisions()][1:] == ["rate_paused"] * 2


def test_open_feed_and_trades_for_the_maker(monkeypatch):
    seen = {}
    monkeypatch.setattr(run_mod, "poll_books",
                        lambda coin, clock, **kw: seen.update(poll=coin, limit=kw["on_rate_limit"]))
    monkeypatch.setattr(run_mod, "stream_trades", lambda coin, clock, **kw: seen.update(kw))

    def boom(*a, **k):
        raise AssertionError("the maker does not use the WS book")

    monkeypatch.setattr(run_mod, "stream_books", boom)
    cfg = Config(strategy="maker", feed_ping_s=2.5, feed_dead_s=3.5)
    limit = print
    run_mod.open_feed(cfg, lambda k, d: None, limit)
    run_mod.open_trades(cfg, lambda k, d: None)
    assert seen["poll"] == "BTC" and (seen["ping_s"], seen["dead_s"]) == (2.5, 3.5)
    assert seen["limit"] is limit


def test_main_maker_dry_run_uses_rest_books_and_the_trades_stream(tmp_path, monkeypatch, capsys):
    _env(monkeypatch, key=False, addr=False)
    monkeypatch.setenv("STRATEGY", "maker")
    monkeypatch.setenv("MAX_POSITION_USD", "15")
    monkeypatch.setattr(run_mod, "fetch_meta", lambda coin: BTC)

    async def rest(*a, **k):
        for _ in range(3):
            yield mkbook(recv_ts=time.time())
            await asyncio.sleep(0.05)

    async def trades(*a, **k):
        await asyncio.Event().wait()
        yield  # pragma: no cover

    monkeypatch.setattr(run_mod, "poll_books", rest)
    monkeypatch.setattr(run_mod, "stream_trades", trades)
    monkeypatch.setattr(run_mod, "stream_books", None)
    monkeypatch.setattr(run_mod, "build_model", lambda cfg: StubModel(0.9))
    run_mod.main(["--db", str(tmp_path / "m.db"), "--seconds", "1"])
    out = capsys.readouterr().out
    assert "decisions 3" in out and "maker rest ms" in out


def test_prepare_live_maker_cancels_leftover_orders():
    fake = FakeHL(bids=[[65000.0, 5.0]], asks=[[65003.0, 5.0]])
    ex = Exchange(fake, fake, ADDR, BTC, "BTC", since_ms=0, sleep=lambda s: None)
    ex.place_quote("buy", SZ, Decimal("64000"), None, 0.0)
    assert prepare_live(Store(":memory:"), ex, cancel_orders=True) == 1
    assert fake.resting == []
    ex.place_quote("buy", SZ, Decimal("64000"), None, 0.0)
    assert prepare_live(Store(":memory:"), ex) == 0 and len(fake.resting) == 1  # taker: as before


def test_live_report_lists_open_orders_at_the_end():
    tr, store, clock, fake, _ = make_live()
    tick(tr, clock, T0)
    assert "open orders on BTC: 1 (should be 0)" in format_report(store, tr, tr._executor)
    asyncio.run(tr.shutdown())
    assert "open orders on BTC: 0 (should be 0)" in format_report(store, tr, tr._executor)


# ---- dashboard ----


def test_summary_carries_quote_and_action_counter():
    tr, store, clock, fake, _ = make_live()
    tick(tr, clock, T0)
    live = trader_live(tr, clock)()
    assert live["maker"]["quote"]["side"] == "buy" and live["maker"]["quote"]["px"] == 65001.0
    assert live["maker"]["actions_last_min"] == 1 and live["maker"]["actions_cap"] == 20
    assert build_summary(store, live)["maker"] == live["maker"]
    assert build_summary(store, None)["maker"] is None


def test_snapshot_history_is_larger_for_the_maker():
    store = Store(":memory:")
    for i in range(HISTORY_DECISIONS_MAKER + 5):
        store.insert_decision(Decision(float(i), 0.6, Action.BUY, "signal", 11.0, 0.5, 1.0), 1.0)
    taker = build_snapshot(store, {"mid": 1.0, "feed": "live", "kill_reason": None,
                                   "stopped": False})
    maker = build_snapshot(store, {"mid": 1.0, "feed": "live", "kill_reason": None,
                                   "stopped": False, "maker": {"quote": None}})
    assert len(taker["decisions"]) == HISTORY_DECISIONS == 300
    assert len(maker["decisions"]) == HISTORY_DECISIONS_MAKER == 1800
    assert maker["history_decisions"] == 1800 and "history_decisions" not in taker


# ---- reconcile, soak, strategy warning ----


def test_reconcile_reports_maker_taker_split_and_open_orders():
    fake = FakeHL(bids=[[65000.0, 5.0]], asks=[[65003.0, 5.0]])
    ex = Exchange(fake, fake, ADDR, BTC, "BTC", since_ms=fake.now_ms - 60_000,
                  sleep=lambda s: None)
    book = Book(ts=1.0, recv_ts=1.0, coin="BTC", bids=[[65000.0, 5.0]], asks=[[65003.0, 5.0]])
    ex.open(book, "buy", 11.0, 1.0)  # taker
    oid = ex.place_quote("sell", SZ, Decimal("65002"), None, 0.0).oid
    fake.taker_hits(oid, SZ)  # maker
    store = Store(":memory:")
    for f in ex.sync_fills():
        store.insert_fill(tid=f["tid"], oid=f["oid"], ts=f["ts"], coin="BTC", side=f["side"],
                          sz=f["sz"], px=f["px"], fee=f["fee"])
    text, ok = rc.run_reconcile(store, fake, ADDR, "BTC", BTC)
    assert ok, text
    assert "maker fills 1: fees $0.001560 (1.50 bps of notional)" in text
    assert "taker fills 1:" in text and "(4.50 bps of notional)" in text
    assert "open orders on BTC: 0" in text
    ex.place_quote("buy", SZ, Decimal("64000"), None, 0.0)
    assert "open orders on BTC: 1" in rc.run_reconcile(store, fake, ADDR, "BTC", BTC)[0]


def _decisions(store, n, action, reason):
    for i in range(n):
        store.insert_decision(Decision(T0 + i, 0.7, Action(action), reason, 0.0, 0.5, 1.0), 1.0)


def test_maker_zero_hold_rate_is_not_a_warning():
    store = Store(":memory:")
    mark_run_start(store, lambda: T0)
    store.log_event(T0, "strategy", "maker")
    _decisions(store, MIN_DECISIONS_FOR_STUCK, "buy", "signal")
    assert soak_report(store)["warnings"] == []  # the maker always quotes: 0% is expected
    store2 = Store(":memory:")
    mark_run_start(store2, lambda: T0)
    store2.log_event(T0, "strategy", "maker")
    _decisions(store2, MIN_DECISIONS_FOR_STUCK, "hold", "late")
    assert "100%" in soak_report(store2)["warnings"][0]


def test_soak_reads_the_strategy_of_the_latest_run_only():
    store = Store(":memory:")
    mark_run_start(store, lambda: T0)
    store.log_event(T0, "strategy", "maker")
    mark_run_start(store, lambda: T0 + 1)
    store.log_event(T0 + 1, "strategy", "taker")
    _decisions(store, MIN_DECISIONS_FOR_STUCK, "buy", "signal")
    assert "0%" in soak_report(store)["warnings"][0]


def test_strategy_warning_for_a_db_used_by_the_other_strategy():
    store = Store(":memory:")
    assert strategy_warning(store, MCFG) is None  # a new database
    store.insert_decision(Decision(T0, 0.7, Action.BUY, "signal", 10.0, 0.6, 1.0), 1.0)
    assert "STRATEGY=taker" in strategy_warning(store, MCFG)  # pre-maker databases are taker
    assert strategy_warning(store, Config()) is None
    store.log_event(T0, "strategy", "maker")
    assert "STRATEGY=maker" in strategy_warning(store, Config())
    assert strategy_warning(store, MCFG) is None
