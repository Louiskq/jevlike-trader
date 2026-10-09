"""Live mode wiring in run.py: startup checks, the --live flag, and an end-to-end run against
the fake venue. No network, no key."""

import asyncio
import os
import signal
import threading
import time

import pytest

from fake_hl import FakeHL
from jevhl import run as run_mod
from jevhl import trader as trader_mod
from jevhl.config import Config
from jevhl.exchange import Exchange
from jevhl.rounding import AssetMeta
from jevhl.run import StartupError, dry_run, live_since_ms, prepare_live
from jevhl.store import Store
from test_trader import T0, Clock, StubModel, mkbook

BTC = AssetMeta("BTC", 5)
ADDR = "0x" + "ab" * 20


def venue(t0=T0, **kw):
    return FakeHL(
        bids=[[65000.0, 5.0], [64999.0, 5.0]], asks=[[65001.0, 5.0], [65002.0, 5.0]],
        t0_ms=int(t0 * 1000), **kw,
    )


def exch(fake, since=None):
    return Exchange(fake, fake, ADDR, BTC, "BTC", sleep=lambda s: None,
                    since_ms=fake.now_ms - 60_000 if since is None else since)


def test_dry_run_with_the_live_executor_trades_and_ends_flat():
    clock, store, fake = Clock(), Store(":memory:"), venue()

    async def books():
        for i in range(3):
            clock.t = T0 + 5.3 * i
            yield mkbook(recv_ts=clock.t)
            await asyncio.sleep(0.05)

    tr = asyncio.run(dry_run(Config(), store, BTC, books(), model=StubModel(0.9), clock=clock,
                             executor=exch(fake)))
    assert tr.error is None and tr.stopped
    tids = [r["tid"] for r in store.fills()]
    assert tids and all(t > 0 for t in tids)  # exchange trade ids, never paper's negative ones
    assert tr.ledger.position == 0.0 and fake.position == pytest.approx(0.0, abs=1e-12)


def test_live_since_is_the_last_stored_fill_or_now_with_an_overlap():
    store = Store(":memory:")
    assert live_since_ms(store, now_s=1000.0) == 1_000_000 - 60_000
    store.insert_fill(tid=1, oid=1, ts=500.5, coin="BTC", side="buy", sz=1, px=1, fee=0)
    store.insert_fill(tid=2, oid=1, ts=400.0, coin="BTC", side="buy", sz=1, px=1, fee=0)
    assert live_since_ms(store, now_s=1000.0) == 500_500 - 60_000


def test_prepare_live_refuses_a_database_with_paper_fills():
    store, fake = Store(":memory:"), venue()
    store.insert_fill(tid=-1, oid=-1, ts=T0, coin="BTC", side="buy", sz=1, px=1, fee=0)
    with pytest.raises(StartupError, match="paper"):
        prepare_live(store, exch(fake))


def test_prepare_live_books_missed_fills_then_checks_the_position():
    store, fake = Store(":memory:"), venue()
    fake._record(7, "BTC", True, 0.002, 65001.0)  # a fill the store has never seen
    ex = exch(fake)
    prepare_live(store, ex)
    assert [r["tid"] for r in store.fills()] == [5000]
    prepare_live(store, ex)  # again: idempotent
    assert len(store.fills()) == 1


def test_prepare_live_refuses_when_the_exchange_position_differs_from_the_ledger():
    store, fake = Store(":memory:"), venue()
    fake.position = 0.5  # open on the exchange, unknown to us
    with pytest.raises(StartupError, match="position"):
        prepare_live(store, exch(fake))


def test_prepare_live_position_tolerance_is_half_a_lot():
    store, fake = Store(":memory:"), venue()
    fake._record(7, "BTC", True, 0.002, 65001.0)
    fake.position = 0.002 + 0.0000049  # below half a lot (5e-6): float residue, fine
    prepare_live(store, exch(fake))
    store2, fake2 = Store(":memory:"), venue()
    fake2._record(7, "BTC", True, 0.002, 65001.0)
    fake2.position = 0.002 + 0.0000051
    with pytest.raises(StartupError):
        prepare_live(store2, exch(fake2))


# ---- main ----


def _env(monkeypatch, key=True, addr=True):
    for k in ("HL_API_KEY", "HL_ACCOUNT_ADDRESS", "NETWORK"):
        monkeypatch.delenv(k, raising=False)
    if key:
        monkeypatch.setenv("HL_API_KEY", "0x" + "11" * 32)
    if addr:
        monkeypatch.setenv("HL_ACCOUNT_ADDRESS", ADDR)


def _no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network touched")

    monkeypatch.setattr(run_mod, "fetch_meta", boom)


@pytest.mark.parametrize("key,addr,word", [(False, True, "HL_API_KEY"),
                                           (True, False, "HL_ACCOUNT_ADDRESS")])
def test_live_without_credentials_stops_before_any_network_call(
    tmp_path, monkeypatch, capsys, key, addr, word
):
    _env(monkeypatch, key=key, addr=addr)
    _no_network(monkeypatch)
    db = tmp_path / "x.db"
    with pytest.raises(SystemExit) as e:
        run_mod.main(["--live", "--db", str(db), "--seconds", "1"])
    assert word in str(e.value) and e.value.code not in (0, None)


def _books_now(n=3):
    async def gen(*a, **k):
        for _ in range(n):
            yield mkbook(recv_ts=time.time())
            await asyncio.sleep(0.05)

    return gen


def test_dry_run_main_needs_no_key_and_never_builds_an_exchange(tmp_path, monkeypatch, capsys):
    _env(monkeypatch, key=False, addr=False)
    monkeypatch.setattr(run_mod, "fetch_meta", lambda coin: BTC)
    monkeypatch.setattr(run_mod, "stream_books", _books_now())
    monkeypatch.setattr(run_mod, "build_model", lambda cfg: StubModel(0.9))

    def boom(*a, **k):
        raise AssertionError("exchange built in a dry run")

    monkeypatch.setattr(run_mod, "build_exchange", boom)
    run_mod.main(["--db", str(tmp_path / "d.db"), "--seconds", "1"])
    out = capsys.readouterr().out
    assert "LIVE" not in out and "decisions 3" in out
    fills = Store(str(tmp_path / "d.db")).fills()
    assert fills and all(r["tid"] < 0 for r in fills)  # paper


def test_live_main_trades_on_the_fake_venue_and_reports_the_exchange_position(
    tmp_path, monkeypatch, capsys
):
    _env(monkeypatch)
    fake = venue(t0=time.time())
    monkeypatch.setattr(run_mod, "fetch_meta", lambda coin: BTC)
    monkeypatch.setattr(run_mod, "stream_books", _books_now())
    monkeypatch.setattr(run_mod, "build_model", lambda cfg: StubModel(0.9))
    seen = {}

    def fake_build(cfg, meta, since_ms):
        seen["cfg"], seen["since"] = cfg, since_ms
        return exch(fake, since=since_ms)

    monkeypatch.setattr(run_mod, "build_exchange", fake_build)
    run_mod.main(["--live", "--db", str(tmp_path / "l.db"), "--seconds", "1"])
    out = capsys.readouterr().out
    assert "LIVE testnet" in out and "max position $500" in out
    assert "exchange position 0" in out
    assert seen["cfg"].network == "testnet"
    assert abs(seen["since"] - (time.time() * 1000 - 60_000)) < 10_000  # empty db: now - 60 s
    fills = Store(str(tmp_path / "l.db")).fills()
    assert fills and all(r["tid"] > 0 for r in fills)
    assert fake.position == pytest.approx(0.0, abs=1e-12)


def test_live_main_refuses_a_paper_database(tmp_path, monkeypatch):
    _env(monkeypatch)
    db = tmp_path / "p.db"
    Store(str(db)).insert_fill(tid=-1, oid=-1, ts=T0, coin="BTC", side="buy", sz=1, px=1, fee=0)
    monkeypatch.setattr(run_mod, "fetch_meta", lambda coin: BTC)
    monkeypatch.setattr(run_mod, "build_exchange", lambda cfg, meta, since_ms: exch(venue()))
    with pytest.raises(SystemExit, match="paper"):
        run_mod.main(["--live", "--db", str(db), "--seconds", "1"])


def test_live_warns_when_max_position_is_above_the_first_hour_size():
    assert run_mod.live_size_warning(Config(max_position_usd=15)) is None
    msg = run_mod.live_size_warning(Config(max_position_usd=500))
    assert msg and "MAX_POSITION_USD" in msg and "500" in msg


def test_live_main_prints_the_size_warning_to_stderr_but_still_runs(
    tmp_path, monkeypatch, capsys
):
    _env(monkeypatch)
    monkeypatch.setenv("MAX_POSITION_USD", "500")
    fake = venue(t0=time.time())
    monkeypatch.setattr(run_mod, "fetch_meta", lambda coin: BTC)
    monkeypatch.setattr(run_mod, "stream_books", _books_now())
    monkeypatch.setattr(run_mod, "build_model", lambda cfg: StubModel(0.9))
    monkeypatch.setattr(run_mod, "build_exchange", lambda cfg, meta, since_ms: exch(fake, since_ms))
    run_mod.main(["--live", "--db", str(tmp_path / "l.db"), "--seconds", "1"])
    cap = capsys.readouterr()
    assert "WARNING" in cap.err and "MAX_POSITION_USD" in cap.err
    monkeypatch.setenv("MAX_POSITION_USD", "15")
    run_mod.main(["--live", "--db", str(tmp_path / "l2.db"), "--seconds", "1"])
    assert "WARNING" not in capsys.readouterr().err


# ---- stopping: Ctrl-C and --seconds while a flatten is (or may be) retrying ----


def _live_main_env(tmp_path, monkeypatch, fake, books):
    _env(monkeypatch)
    monkeypatch.setattr(run_mod, "fetch_meta", lambda coin: BTC)
    monkeypatch.setattr(run_mod, "stream_books", books)
    monkeypatch.setattr(run_mod, "build_model", lambda cfg: StubModel(0.9))
    monkeypatch.setattr(run_mod, "build_exchange",
                        lambda cfg, meta, since_ms: exch(fake, since=since_ms))
    return str(tmp_path / "l.db")


def _main_after_sigints(db):
    """run_mod.main(--live) with SIGINTs already scheduled; a KeyboardInterrupt that escapes
    main (the handler is gone) fails the test instead of aborting the pytest session."""
    try:
        run_mod.main(["--live", "--db", db])
    except KeyboardInterrupt:
        pytest.fail("KeyboardInterrupt escaped main: Ctrl-C was not turned into a clean exit")


def _sigint_at(*delays):
    for d in delays:
        threading.Timer(d, lambda: os.kill(os.getpid(), signal.SIGINT)).start()


def test_one_ctrl_c_stops_a_live_run_with_the_normal_report_and_no_gave_up(
    tmp_path, monkeypatch, capsys
):
    fake = venue(t0=time.time())

    async def books(*a, **k):
        yield mkbook(recv_ts=time.time())
        await asyncio.sleep(3600)

    db = _live_main_env(tmp_path, monkeypatch, fake, books)
    _sigint_at(0.5)
    with pytest.raises(SystemExit) as e:
        _main_after_sigints(db)
    out = capsys.readouterr().out
    assert e.value.code == 130
    assert "GAVE UP" not in out
    assert "decisions 1" in out and "exchange position 0" in out
    assert fake.position == pytest.approx(0.0, abs=1e-12)
    kinds = [r["kind"] for r in Store(db).events()]
    assert "flatten_gave_up" not in kinds


def test_a_second_ctrl_c_mid_attempt_returns_at_once_and_says_the_position_may_be_open(
    tmp_path, monkeypatch, capsys
):
    class Slow(FakeHL):
        hang = False

        def open_orders(self, address, dex=""):
            if self.hang:
                time.sleep(2)  # an HTTP call running into its timeout
                raise TimeoutError("read timeout")
            return super().open_orders(address, dex)

    fake = Slow(bids=[[65000.0, 5.0]], asks=[[65001.0, 5.0]], t0_ms=int(time.time() * 1000))

    async def books(*a, **k):
        yield mkbook(recv_ts=time.time())
        await asyncio.sleep(0.1)
        fake.hang = True
        await asyncio.sleep(3600)

    db = _live_main_env(tmp_path, monkeypatch, fake, books)
    _sigint_at(0.4, 0.8)
    t0 = time.monotonic()
    with pytest.raises(SystemExit) as e:
        _main_after_sigints(db)
    took = time.monotonic() - t0
    out = capsys.readouterr().out
    assert e.value.code == 130 and out.rstrip().endswith("by hand") and "GAVE UP" in out
    assert took < 1.5  # not the 1 s the blocked call still needs after the second Ctrl-C
    kinds = [r["kind"] for r in Store(db).events()]
    assert kinds.count("flatten_gave_up") == 1


def test_seconds_does_not_cut_a_flatten_retry_short(monkeypatch):
    monkeypatch.setattr(trader_mod, "FLATTEN_RETRY_FIRST_S", 0.1)
    fake, store = venue(t0=time.time()), Store(":memory:")
    ex = Exchange(fake, fake, ADDR, BTC, "BTC", since_ms=fake.now_ms - 60_000,
                  sleep=lambda s: None, flatten_tries=1)

    async def books():
        yield mkbook(recv_ts=time.time())
        await asyncio.sleep(0.05)
        fake.fail_state = [ConnectionError("dns")] * 6  # three failed attempts
        await asyncio.sleep(3600)
        yield

    # the watchdog kills on the stale book at ~1 s; attempts follow at ~1.0, 1.1, 1.3, 1.7 s
    cfg = Config(stale_s=0.3, flatten_retry_window_s=600)
    tr = asyncio.run(dry_run(cfg, store, BTC, books(), model=StubModel(0.9), executor=ex,
                             duration_s=1.2))
    kinds = [r["kind"] for r in store.events()]
    assert "flatten_gave_up" not in kinds and "flatten_recovered" in kinds
    assert not tr.flatten_unresolved and fake.position == pytest.approx(0.0, abs=1e-12)
