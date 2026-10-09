import asyncio
import json
import time
import urllib.request
from dataclasses import replace
from pathlib import Path

import pytest

from jevhl.config import Config
from jevhl.feed import parse_message
from jevhl.latency import latency_report
from jevhl.model import MockModel
from jevhl.rounding import AssetMeta
from jevhl.run import (
    dry_run,
    dry_run_with_dashboard,
    format_report,
    trader_live,
)
from jevhl.store import Store
from jevhl.trader import Trader

SESSION = json.loads((Path(__file__).parent / "fixtures" / "ws_session_btc.json").read_text())
REAL = [parse_message(m, "BTC", 0.0) for m in (SESSION[1], SESSION[3])]
T0 = 10 * 86400.0 + 3600.0


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


def test_dry_run_replays_real_books_one_decision_each_and_reports():
    clock = Clock()
    store = Store(":memory:")

    async def books():
        for i in range(6):
            clock.t = T0 + 5.3 * i
            yield replace(REAL[i % 2], recv_ts=clock.t)
            await asyncio.sleep(0.05)

    tr = asyncio.run(
        dry_run(Config(), store, AssetMeta("BTC", 5), books(), model=MockModel(seed=1), clock=clock)
    )
    assert len(store.decisions()) == 6
    assert tr.error is None
    rep = latency_report(store)
    assert rep["decisions"] == 6 and rep["p50"] is not None
    assert rep["gap_p50_s"] == pytest.approx(5.3)
    text = format_report(store, tr)
    assert "latency" in text and "60 s" in text and "30 s" in text and "300 s" in text


def test_dry_run_duration_stops_and_flattens():
    clock = Clock()
    store = Store(":memory:")

    async def books():
        i = 0
        while True:
            clock.t = T0 + 5.3 * i
            yield replace(REAL[i % 2], recv_ts=clock.t)
            i += 1
            await asyncio.sleep(0.05)

    class Buyer:
        def predict(self, inp):
            from jevhl.types import ModelOutput

            return ModelOutput(0.9)

    tr = asyncio.run(
        dry_run(Config(), store, AssetMeta("BTC", 5), books(), model=Buyer(), clock=clock,
                duration_s=0.3)
    )
    assert store.decisions() and tr.stopped
    assert abs(tr.ledger.position) < 1e-9


# ---- dashboard wiring ----



def _trader(clock, **cfg_kw):
    from jevhl.ledger import Ledger
    from jevhl.paper import PaperExchange
    from jevhl.risk import KillSwitch

    cfg = Config(**cfg_kw)
    store = Store(":memory:")
    meta = AssetMeta("BTC", 5)
    return Trader(cfg, store, MockModel(), PaperExchange(store, meta, "BTC"), Ledger(),
                  KillSwitch(cfg, clock), clock=clock), cfg


def test_trader_live_reports_waiting_live_stale_and_kill():
    clock = Clock()

    async def go():
        tr, cfg = _trader(clock, stale_resumes_max=0)
        live = trader_live(tr, clock)
        out = [live()]
        await tr.on_book(replace(REAL[0], recv_ts=clock.t))
        out.append(live())
        clock.t += cfg.stale_s - 0.5  # inside the bound
        out.append(live())
        clock.t += 0.5  # exactly stale_s old: still live (the trader's own test is `>`)
        out.append(live())
        clock.t += 0.5  # past the bound
        out.append(live())
        await tr.check_stale()
        out.append(live())
        return out

    waiting, fresh, inside, exact, stale, killed = asyncio.run(go())
    assert waiting == {"mid": None, "feed": "waiting", "kill_reason": None, "stopped": False,
                       "paused": False}
    assert fresh["feed"] == "live" and fresh["mid"] == REAL[0].mid
    assert inside["feed"] == "live" and exact["feed"] == "live" and stale["feed"] == "stale"
    assert killed["kill_reason"] == "stale_book" and killed["stopped"] is True
    assert killed["paused"] is False


def test_trader_live_reports_a_stale_pause_as_paused_not_killed():
    clock = Clock()

    async def go():
        tr, cfg = _trader(clock)
        await tr.on_book(replace(REAL[0], recv_ts=clock.t))
        clock.t += cfg.stale_s + 1
        await tr.check_stale()
        return trader_live(tr, clock)()

    live = asyncio.run(go())
    assert live["paused"] is True and live["stopped"] is False and live["kill_reason"] is None


def _read_events(url, wanted, max_lines=400):
    """Read an SSE stream until every event name in `wanted` has been seen after the
    snapshot; returns the event names in order."""
    names = []
    with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 (loopback)
        for _ in range(max_lines):
            line = r.readline().decode()
            if line.startswith("event: "):
                names.append(line[7:].strip())
                if set(wanted) <= set(names[1:]):
                    break
    return names


def _get(url, n_lines=None):
    with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 (loopback)
        if n_lines is None:
            return r.read().decode()
        return [r.readline().decode() for _ in range(n_lines)]


def test_dry_run_with_dashboard_serves_page_and_stream_then_shuts_down(tmp_path):
    """End to end on a real loopback socket. An SSE client is still connected when the run
    ends: shutdown must not hang on it."""
    clock = Clock()
    db = str(tmp_path / "e2e.db")
    store = Store(db)
    seen = {}

    async def books():
        i = 0
        while True:
            clock.t = T0 + 5.3 * i
            yield replace(REAL[i % 2], recv_ts=clock.t)
            i += 1
            await asyncio.sleep(0.05)

    async def client(port):
        base = f"http://127.0.0.1:{port}"
        seen["page"] = await asyncio.to_thread(_get, base + "/")
        await asyncio.sleep(0.4)
        seen["state"] = json.loads(await asyncio.to_thread(_get, base + "/api/state"))
        seen["sse"] = await asyncio.to_thread(_get, base + "/events", 4)  # left open
        seen["names"] = await asyncio.to_thread(
            _read_events, base + "/events", {"decision", "summary"}
        )

    async def go():
        tasks = []

        def ready(port):
            tasks.append(asyncio.create_task(client(port)))

        tr = await asyncio.wait_for(
            dry_run_with_dashboard(
                Config(), db, store, AssetMeta("BTC", 5), books(), port=0, model=MockModel(seed=1),
                clock=clock, duration_s=4.0, on_ready=ready,
            ),
            20,
        )
        await asyncio.gather(*tasks)
        return tr

    tr = asyncio.run(go())
    assert tr.stopped and "jevhl" in seen["page"]
    assert seen["state"]["summary"]["decisions"] >= 1
    assert seen["sse"][0].startswith("retry:") and seen["sse"][2] == "event: snapshot\n"
    assert seen["state"]["summary"]["feed"] == "live"
    # the live stream, not just its first lines: new decisions and fresh summaries follow
    assert seen["names"][0] == "snapshot"
    assert "decision" in seen["names"][1:] and "summary" in seen["names"][1:]


def test_dashboard_stays_up_after_kill_switch_and_shows_it(tmp_path):
    """After the kill switch stops the trader, the page must still be reachable (for the
    linger period) and report the trip."""
    clock = Clock()
    db = str(tmp_path / "kill.db")
    store = Store(db)
    seen = {}

    async def books():
        for i in range(2):
            clock.t = T0 + i
            yield replace(REAL[i], recv_ts=clock.t)
        await asyncio.sleep(3600)  # feed goes silent

    async def client(port):
        url = f"http://127.0.0.1:{port}/api/state"
        for _ in range(60):
            seen["state"] = json.loads(await asyncio.to_thread(_get, url))
            if seen["state"]["summary"]["kill"]["state"] == "tripped":
                # The trader has stopped by now; the server must still answer a moment later.
                await asyncio.sleep(1.0)
                seen["state"] = json.loads(await asyncio.to_thread(_get, url))
                return
            await asyncio.sleep(0.1)

    async def go():
        tasks = []
        # the mock clock never advances on its own, so staleness is driven by real time here
        tr = await asyncio.wait_for(
            dry_run_with_dashboard(
                Config(stale_s=1, stale_resumes_max=0), db, store, AssetMeta("BTC", 5),
                books(), port=0,
                model=MockModel(seed=1), clock=time.time, duration_s=None,
                on_ready=lambda port: tasks.append(asyncio.create_task(client(port))),
                linger_s=3.0,
            ),
            30,
        )
        await asyncio.gather(*tasks)
        return tr

    tr = asyncio.run(go())
    assert tr.stopped
    assert seen["state"]["summary"]["kill"] == {"state": "tripped", "reason": "stale_book"}
    assert seen["state"]["summary"]["stopped"] is True


def test_dashboard_history_reloads_after_restart(tmp_path):
    db = str(tmp_path / "restart.db")

    def one_run(seconds):
        clock = Clock()
        store = Store(db)
        out = {}

        async def books():
            i = 0
            while True:
                clock.t = T0 + 5.3 * i
                yield replace(REAL[i % 2], recv_ts=clock.t)
                i += 1
                await asyncio.sleep(0.05)

        async def client(port):
            await asyncio.sleep(0.05)
            out["first"] = json.loads(
                await asyncio.to_thread(_get, f"http://127.0.0.1:{port}/api/state")
            )

        async def go():
            tasks = []
            await dry_run_with_dashboard(
                Config(), db, store, AssetMeta("BTC", 5), books(), port=0,
                model=MockModel(seed=1), clock=clock, duration_s=seconds,
                on_ready=lambda p: tasks.append(asyncio.create_task(client(p))),
            )
            await asyncio.gather(*tasks)

        asyncio.run(go())
        store.close()
        return out["first"]

    one_run(0.6)
    n_before = len(Store(db).decisions())
    assert n_before >= 3
    snap = one_run(0.3)  # a fresh process: its first snapshot already holds the old history
    assert len(snap["decisions"]) >= n_before
    assert snap["summary"]["decisions"] >= n_before


def test_stopping_the_server_stops_the_trader(monkeypatch):
    """Ctrl-C reaches uvicorn first; the bot must then flatten and stop too, promptly and
    because of the server-done callback (the books never end and there is no duration)."""
    import tempfile

    import jevhl.run as run_mod
    from jevhl.types import ModelOutput

    clock = Clock()
    captured = {}
    real = run_mod.make_server

    def spy(app, **kw):
        captured["server"] = real(app, **kw)
        return captured["server"]

    monkeypatch.setattr(run_mod, "make_server", spy)

    class Buyer:
        def predict(self, inp):
            return ModelOutput(0.9)

    async def books():
        i = 0
        while True:
            clock.t = T0 + 5.3 * i
            yield replace(REAL[i % 2], recv_ts=clock.t)
            i += 1
            await asyncio.sleep(0.05)

    async def go(db):
        async def stopper(port):
            await asyncio.sleep(0.5)
            captured["stopped_at"] = time.monotonic()
            captured["server"].should_exit = True

        tasks = []
        tr = await asyncio.wait_for(
            dry_run_with_dashboard(
                Config(), db, Store(db), AssetMeta("BTC", 5), books(), port=0,
                model=Buyer(), clock=clock, duration_s=None,
                on_ready=lambda p: tasks.append(asyncio.create_task(stopper(p))),
            ),
            5,  # without the callback this hangs forever; fail fast rather than after 15 s
        )
        captured["returned_at"] = time.monotonic()
        return tr

    with tempfile.TemporaryDirectory() as d:
        db = d + "/s.db"
        tr = asyncio.run(go(db))
        n_decisions = len(Store(db).decisions())
        n_fills = len(Store(db).fills())
    assert captured["returned_at"] - captured["stopped_at"] < 3
    assert tr.stopped and n_decisions >= 2
    assert n_fills >= 2  # it traded (bought) and then flattened on stop
    assert abs(tr.ledger.position) < 1e-9


# ---- port already in use ----


def _occupied_port():
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    return sock, sock.getsockname()[1]


def test_port_in_use_is_a_clean_specific_error_and_nothing_is_traded(tmp_path):
    from jevhl.server import DashboardStartError

    clock = Clock()
    db = str(tmp_path / "busy.db")
    store = Store(db)
    ready = []

    async def books():
        i = 0
        while True:
            clock.t = T0 + 5.3 * i
            yield replace(REAL[i % 2], recv_ts=clock.t)
            i += 1
            await asyncio.sleep(0.05)

    sock, port = _occupied_port()
    try:
        with pytest.raises(DashboardStartError, match=str(port)):
            asyncio.run(
                asyncio.wait_for(
                    dry_run_with_dashboard(
                        Config(), db, store, AssetMeta("BTC", 5), books(), port=port,
                        model=MockModel(seed=1), clock=clock, duration_s=1.0,
                        on_ready=ready.append,
                    ),
                    10,
                )
            )
    finally:
        sock.close()
    assert ready == [] and store.decisions() == []


def test_main_port_in_use_prints_one_error_line_no_traceback_no_url(
    tmp_path, monkeypatch, capsys
):
    import jevhl.run as run_mod

    monkeypatch.setattr(run_mod, "fetch_meta", lambda coin: AssetMeta("BTC", 5))
    monkeypatch.setattr(run_mod, "stream_books", lambda *a, **k: _never_books())
    db = tmp_path / "busy.db"
    sock, port = _occupied_port()
    try:
        with pytest.raises(SystemExit) as exc:
            run_mod.main(["--dashboard", "--port", str(port), "--db", str(db), "--seconds", "1"])
    finally:
        sock.close()
    out, err = capsys.readouterr()
    assert exc.value.code not in (0, None)
    assert str(port) in err and "already in use" in err
    assert "Traceback" not in err and "dashboard: http" not in out
    assert Store(str(db)).decisions() == []


async def _never_books():
    for _ in ():
        yield  # an async generator that yields nothing


class _Ex:
    coin = "BTC"

    def __init__(self, pos=None):
        self.pos = pos

    def position(self):
        if self.pos is None:
            raise ConnectionError("dns")
        return self.pos


def test_report_ends_with_a_clear_line_when_the_flatten_gave_up():
    store = Store(":memory:")
    tr = asyncio.run(dry_run(Config(), store, AssetMeta("BTC", 5), _none(), model=MockModel()))
    assert "GAVE UP" not in format_report(store, tr)
    tr.flatten_unresolved = True
    last = format_report(store, tr, _Ex()).splitlines()[-1]
    assert last.startswith("FLATTEN GAVE UP") and "by hand" in last


async def _none():
    return
    yield


def test_open_feed_passes_the_configured_liveness_timers(monkeypatch):
    import jevhl.run as run_mod

    seen = {}
    monkeypatch.setattr(run_mod, "stream_books", lambda coin, clock, **kw: seen.update(kw))
    run_mod.open_feed(Config(feed_ping_s=2.5, feed_dead_s=3.5), lambda k, d: None)
    assert (seen["ping_s"], seen["dead_s"]) == (2.5, 3.5)


def test_report_counts_stale_pauses_and_resumes():
    clock = Clock()

    async def go():
        tr, cfg = _trader(clock, resume_after_s=0)
        await tr.on_book(replace(REAL[0], recv_ts=clock.t))
        clock.t += cfg.stale_s + 1
        await tr.check_stale()
        out = [format_report(tr.store, tr)]
        clock.t += 1
        await tr.on_book(replace(REAL[1], recv_ts=clock.t))
        clock.t += cfg.stale_s + 1
        await tr.check_stale()
        out.append(format_report(tr.store, tr))
        return out

    paused, again = asyncio.run(go())
    assert "stale pauses 1  resumes 0  (paused at the end)" in paused
    assert "stale pauses 2  resumes 1  (paused at the end)" in again
