import asyncio
import json
import math
import threading

import pytest
from fastapi.testclient import TestClient

from jevhl import server
from jevhl.ledger import Ledger
from jevhl.server import HOST, build_snapshot, build_summary, create_app, make_server
from jevhl.store import Store
from jevhl.types import Action, Decision


def dec(ts, p=0.7, action=Action.BUY, reason="signal", size=50.0, lat=12.0):
    return Decision(ts, p, action, reason, size, 0.6, lat)


def fill(store, tid, side, sz, px, fee, ts=None):
    store.insert_fill(tid=tid, oid=tid, ts=ts or float(tid), coin="BTC", side=side, sz=sz, px=px,
                      fee=fee)


@pytest.fixture
def db(tmp_path):
    return str(tmp_path / "t.db")


def seeded(db):
    s = Store(db)
    a = s.insert_decision(dec(1.0, p=0.9), 100.0)
    b = s.insert_decision(dec(2.0, p=0.2, action=Action.HOLD, reason="below_gate"), 101.0)
    c = s.insert_decision(dec(3.0, p=None, action=Action.HOLD, reason="late"), 102.0)
    for h in (30, 60, 300):
        s.resolve(a, h, 101.0)  # up
        s.resolve(b, h, 100.0)  # down
    fill(s, 1, "buy", 0.5, 100.0, 0.02)
    fill(s, 2, "sell", 0.25, 104.0, 0.01)
    return s, (a, b, c)


def test_summary_pnl_comes_from_the_ledger_not_recomputed(db):
    s, _ = seeded(db)
    live = {"mid": 103.3, "feed": "live", "kill_reason": None, "stopped": False}
    out = build_summary(s, live)
    lg = Ledger.from_fills(s.fills())
    assert out["pnl_after_fees"] == lg.pnl(103.3)
    assert out["pnl_before_fees"] == lg.pnl_before_fees(103.3)
    assert out["fees"] == lg.fees == pytest.approx(0.03)
    assert out["pnl_before_fees"] - out["pnl_after_fees"] == pytest.approx(out["fees"])
    assert out["position"] == pytest.approx(0.25)
    assert out["position_usd"] == pytest.approx(0.25 * 103.3)
    assert out["mid"] == 103.3 and out["n_fills"] == 2


def test_summary_counts_and_hold_rate(db):
    s, _ = seeded(db)
    out = build_summary(s, None)
    assert out["decisions"] == 3 and out["holds"] == 2 and out["late_holds"] == 1
    assert out["hold_rate"] == pytest.approx(2 / 3)
    assert build_summary(Store(":memory:"), None)["hold_rate"] is None


def test_summary_calibration_three_horizons_with_sample_counts(db):
    s, _ = seeded(db)
    cal = build_summary(s, None)["calibration"]
    assert [c["horizon_s"] for c in cal] == [60, 30, 300]  # headline first
    c60 = cal[0]
    assert c60["n"] == 2
    assert c60["brier"] == pytest.approx(((0.9 - 1) ** 2 + (0.2 - 0) ** 2) / 2)
    assert c60["baseline_running"] is not None
    assert sum(b["count"] for b in c60["bins"]) == 2 and len(c60["bins"]) == 10
    assert c60["bins"][9]["mean_p"] == pytest.approx(0.9) and c60["bins"][9]["freq"] == 1.0


def test_summary_without_live_marks_at_last_decision_mid(db):
    s, _ = seeded(db)
    out = build_summary(s, None)
    assert out["mid"] == 102.0 and out["feed"] == "offline" and out["kill"]["state"] == "unknown"
    assert out["pnl_after_fees"] == Ledger.from_fills(s.fills()).pnl(102.0)


def test_summary_empty_store_is_flat_zero(db):
    out = build_summary(Store(db), None)
    assert out["mid"] is None and out["position"] == 0.0 and out["pnl_after_fees"] == 0.0
    assert out["decision"] is None


def test_summary_kill_state_from_live(db):
    s, _ = seeded(db)
    live = {"mid": 100.0, "feed": "stale", "kill_reason": "stale_book", "stopped": True}
    out = build_summary(s, live)
    assert out["kill"] == {"state": "tripped", "reason": "stale_book"}
    assert out["stopped"] is True and out["feed"] == "stale"
    ok = build_summary(s, {**live, "kill_reason": None, "stopped": False})
    assert ok["kill"] == {"state": "ok", "reason": None}


def test_summary_current_decision_is_latest(db):
    s, ids = seeded(db)
    d = build_summary(s, None)["decision"]
    assert d["id"] == ids[2] and d["reason"] == "late" and d["p_up"] is None


def test_snapshot_history_reloads_after_restart(db):
    s, ids = seeded(db)
    s.close()
    s2 = Store(db)  # "restart"
    snap = build_snapshot(s2, None)
    assert [d["id"] for d in snap["decisions"]] == list(ids)
    assert [f["tid"] for f in snap["fills"]] == [1, 2]
    assert snap["last_decision_id"] == ids[2] and snap["last_fill_id"] == s2.fills()[-1]["id"]
    assert snap["summary"]["fees"] == pytest.approx(0.03)


def test_snapshot_limits_history_to_recent(db):
    s = Store(db)
    for i in range(server.HISTORY_DECISIONS + 5):
        s.insert_decision(dec(float(i)), 100.0)
    snap = build_snapshot(s, None)
    assert len(snap["decisions"]) == server.HISTORY_DECISIONS
    assert snap["decisions"][-1]["ts"] == server.HISTORY_DECISIONS + 4.0


def test_dumps_is_strict_json_with_non_finite_as_null():
    out = server._dumps({"a": math.nan, "b": [math.inf, 1.5], "c": {"d": -math.inf}})
    assert json.loads(out) == {"a": None, "b": [None, 1.5], "c": {"d": None}}


# ---- SSE ----

def parse_sse(text):
    events = []
    for block in text.split("\n\n"):
        name = data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name:
            events.append((name, data))
    return events


def stop_after(n):
    calls = {"n": 0}

    def should_stop():
        calls["n"] += 1
        return calls["n"] > n

    return should_stop


async def nosleep(poll_s, should_stop):
    await asyncio.sleep(0)


def stream(db, should_stop, live=None):
    return server.event_stream(db, live, should_stop, 0.001, sleep=nosleep)


def collect(gen):
    async def go():
        return [e async for e in gen]

    return asyncio.run(go())


def test_stream_sends_retry_hint_snapshot_then_new_rows(db):
    s, ids = seeded(db)
    state = {"polls": 0}

    def should_stop():
        state["polls"] += 1
        if state["polls"] == 1:  # during the first wait: a decision and a fill arrive
            d = s.insert_decision(dec(4.0, p=0.65), 105.0)
            state["new"] = d
            fill(s, 3, "buy", 0.1, 105.0, 0.005)
        return state["polls"] > 2

    chunks = collect(stream(db, should_stop))
    assert chunks[0].startswith("retry: ")
    ev = parse_sse("".join(chunks))
    names = [n for n, _ in ev]
    assert names[0] == "snapshot"
    assert [d["id"] for n, d in ev if n == "decision"] == [state["new"]]
    assert [f["tid"] for n, f in ev if n == "fill"] == [3]
    assert "summary" in names
    assert ev[0][1]["decisions"][-1]["id"] == ids[2]  # snapshot did not include the new row
    summaries = [d for n, d in ev if n == "summary"]
    assert summaries[-1]["decisions"] == 4 and summaries[-1]["n_fills"] == 3


def test_stream_never_builds_a_summary_on_the_event_loop_thread(db, monkeypatch):
    """build_summary reads every fill and scored row; on the trader's loop it would delay
    model-latency timing and book stamping. It must run off the loop thread."""
    seeded(db)
    threads = []
    real = server.build_summary

    def spy(store, live):
        threads.append(threading.get_ident())
        return real(store, live)

    monkeypatch.setattr(server, "build_summary", spy)

    async def go():
        loop_thread = threading.get_ident()
        out = [e async for e in stream(db, stop_after(3))]
        return loop_thread, out

    loop_thread, chunks = asyncio.run(go())
    assert len(threads) == 4  # the snapshot and each of 3 polls
    assert loop_thread not in threads
    assert [n for n, _ in parse_sse("".join(chunks))].count("summary") == 3


def test_stream_does_not_repeat_rows(db):
    seeded(db)
    ev = parse_sse("".join(collect(stream(db, stop_after(3)))))
    assert [n for n, _ in ev].count("decision") == 0  # all were in the snapshot


def test_stream_exits_when_stopped_even_while_waiting(db):
    seeded(db)
    stop = {"v": False}

    async def go():
        gen = server.event_stream(db, None, lambda: stop["v"], 30.0)  # long poll
        got = [await anext(gen), await anext(gen)]  # retry hint, snapshot
        task = asyncio.ensure_future(anext(gen, None))
        await asyncio.sleep(0.05)
        stop["v"] = True
        res = await asyncio.wait_for(task, 3.0)  # must wake without waiting 30 s
        return got, res

    got, res = asyncio.run(go())
    assert res is None


def test_stream_stop_checked_after_the_wait(db):
    s, _ = seeded(db)
    state = {"n": 0}

    def should_stop():
        state["n"] += 1
        if state["n"] == 1:  # stop arrives while sleeping, and a row lands at the same time
            s.insert_decision(dec(9.0), 100.0)
        return True

    ev = parse_sse("".join(collect(stream(db, should_stop))))
    assert [n for n, _ in ev] == ["snapshot"]  # nothing acted on after the stop


def test_stream_closes_its_store_on_exit(db, monkeypatch):
    seeded(db)
    opened = []
    real = server.Store

    def spy(path):
        st = real(path)
        opened.append(st)
        return st

    monkeypatch.setattr(server, "Store", spy)
    collect(stream(db, stop_after(1)))
    with pytest.raises(Exception, match="closed"):
        opened[0].decisions()


def test_reconnect_gets_full_history_again(db):
    """A dropped stream is replaced by a new one: its snapshot carries everything."""
    seeded(db)
    first = parse_sse("".join(collect(stream(db, stop_after(1)))))
    second = parse_sse("".join(collect(stream(db, stop_after(1)))))
    assert first[0][1]["decisions"] == second[0][1]["decisions"] != []


# ---- HTTP, host binding ----

def client(db, **kw):
    app = create_app(db, **kw)
    return TestClient(app, base_url=f"http://{HOST}")


def test_page_and_assets_served_with_csp_and_no_external_urls(db):
    c = client(db)
    r = c.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "script-src 'self'" in csp
    for path in ("/", "/static/app.js", "/static/logic.js", "/static/style.css"):
        body = c.get(path).text
        assert "http://" not in body and "https://" not in body, path
    assert "innerHTML" not in c.get("/static/app.js").text
    assert "innerHTML" not in c.get("/static/logic.js").text


def test_unknown_static_path_is_404_and_no_traversal(db):
    c = client(db)
    assert c.get("/static/nope.js").status_code == 404
    assert c.get("/static/..%2Fserver.py").status_code == 404


def test_api_state_returns_snapshot(db):
    seeded(db)
    r = client(db).get("/api/state")
    assert r.status_code == 200 and r.json()["summary"]["decisions"] == 3


def test_events_endpoint_streams_sse(db):
    seeded(db)
    c = client(db, should_stop=stop_after(2), poll_s=0.001)
    r = c.get("/events")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache"
    assert parse_sse(r.text)[0][0] == "snapshot"


@pytest.mark.parametrize(
    "host", ["evil.example.com", "192.168.1.5", "0.0.0.0", "127.0.0.1.evil.com"]
)
def test_foreign_host_header_rejected(db, host):
    c = TestClient(create_app(db), base_url="http://127.0.0.1")
    assert c.get("/", headers={"host": host}).status_code == 403
    assert c.get("/api/state", headers={"host": host}).status_code == 403


def test_localhost_host_header_with_port_allowed(db):
    c = TestClient(create_app(db), base_url="http://127.0.0.1")
    assert c.get("/", headers={"host": "127.0.0.1:8765"}).status_code == 200
    assert c.get("/", headers={"host": "localhost:8765"}).status_code == 200


@pytest.mark.parametrize("host", ["0.0.0.0", "", "::", "192.168.1.5", "localhost", "example.com"])
def test_make_server_rejects_non_loopback_bind(db, host):
    with pytest.raises(ValueError, match="127.0.0.1"):
        make_server(create_app(db), host=host, port=8765)


def test_make_server_binds_127_0_0_1(db):
    srv = make_server(create_app(db), port=8765)
    assert srv.config.host == "127.0.0.1" and srv.config.port == 8765
