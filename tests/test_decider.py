import asyncio
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from decider_server import Q60, FakeDecider, good_body
from jevhl.config import Config, ConfigError
from jevhl.feed import Book
from jevhl.ledger import Ledger
from jevhl.model import DeciderError, DeciderModel
from jevhl.paper import PaperExchange
from jevhl.prompt import render_context
from jevhl.risk import KillSwitch
from jevhl.rounding import AssetMeta
from jevhl.store import Store
from jevhl.trader import Trader
from jevhl.types import ModelInput

T0 = 10 * 86400.0 + 3600.0


def inp():
    return ModelInput(
        ts=1.0, coin="BTC", mid=100.5, spread_bps=99.0,
        bids=[[100.0 - i, 1.0] for i in range(10)], asks=[[101.0 + i, 1.0] for i in range(10)],
        recent_mids=[100.5], horizon_s=60,
    )


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---- success and request shape ----

def test_success_returns_p_up_from_probabilities_and_sends_contract():
    with FakeDecider(good_body(0.3832)) as srv:
        out = DeciderModel(srv.url, 2.0).predict(inp())
    assert out.p_up == pytest.approx(0.3832)
    (req,) = srv.requests
    assert req["path"] == "/decide"
    assert req["json"]["context"] == render_context(inp())
    assert req["json"]["schema"] == {Q60: {"type": "choice", "options": ["up", "down"]}}


def test_p_up_is_the_up_probability_not_the_choice_confidence():
    body = good_body(0.3832)
    body[Q60]["confidence"] = 0.9
    with FakeDecider(body) as srv:
        assert DeciderModel(srv.url, 2.0).predict(inp()).p_up == pytest.approx(0.3832)


def test_boundaries_zero_and_one_are_valid():
    with FakeDecider(good_body(1.0)) as srv:
        assert DeciderModel(srv.url, 2.0).predict(inp()).p_up == 1.0
    with FakeDecider(good_body(0.0)) as srv:
        assert DeciderModel(srv.url, 2.0).predict(inp()).p_up == 0.0


# ---- failures all raise DeciderError ----

def probs(up, down):
    return {Q60: {"choice": "up", "probabilities": {"up": up, "down": down}}}


BAD_BODIES = {
    "nan": probs(float("nan"), 0.5),
    "inf": probs(float("inf"), 0.0),
    "above_one": probs(1.2, -0.2),
    "negative": probs(-0.1, 1.1),
    "string": probs("0.6", 0.4),
    "bool": probs(True, 0.0),
    "null": probs(None, 0.5),
    "missing_down": {Q60: {"probabilities": {"up": 0.6}}},
    "missing_up": {Q60: {"probabilities": {"down": 0.4}}},
    "sum_not_one": probs(0.6, 0.6),
    "no_probabilities": {Q60: {"choice": "up"}},
    "probabilities_list": {Q60: {"probabilities": [0.6, 0.4]}},
    "wrong_question": {"other?": good_body()[Q60]},
    "top_level_list": [1, 2],
    "answer_not_dict": {Q60: 0.6},
}


@pytest.mark.parametrize("name", list(BAD_BODIES))
def test_invalid_payload_raises(name):
    body = BAD_BODIES[name]
    with FakeDecider(mode="raw", raw=json.dumps(body).encode()) as srv:  # json allows NaN
        with pytest.raises(DeciderError):
            DeciderModel(srv.url, 2.0).predict(inp())


@pytest.mark.parametrize("raw", [b"not json", b"", b"{", b"\xff\xfe"])
def test_malformed_json_raises(raw):
    with FakeDecider(mode="raw", raw=raw) as srv, pytest.raises(DeciderError):
        DeciderModel(srv.url, 2.0).predict(inp())


@pytest.mark.parametrize("status", [500, 503, 404, 400])
def test_http_error_status_raises(status):
    with FakeDecider(mode="raw", status=status, raw=json.dumps(good_body()).encode()) as srv:
        with pytest.raises(DeciderError):
            DeciderModel(srv.url, 2.0).predict(inp())


def test_oversized_body_raises():
    with FakeDecider(mode="raw", raw=json.dumps(good_body()).encode() + b" " * (1 << 20)) as srv:
        with pytest.raises(DeciderError):
            DeciderModel(srv.url, 2.0).predict(inp())


def test_connection_refused_raises_and_repeats_do_not_hang():
    m = DeciderModel(f"http://127.0.0.1:{free_port()}", 2.0)
    t0 = time.monotonic()
    for _ in range(20):
        with pytest.raises(DeciderError):
            m.predict(inp())
    assert time.monotonic() - t0 < 5


def test_recovers_after_failures():
    srv = FakeDecider(mode="raw", status=500, raw=b"x")
    with srv:
        m = DeciderModel(srv.url, 2.0)
        with pytest.raises(DeciderError):
            m.predict(inp())
        srv.mode, srv.status = "ok", 200
        assert m.predict(inp()).p_up == pytest.approx(0.3832)


def test_socket_timeout_raises_promptly():
    with FakeDecider(mode="sleep") as srv:
        t0 = time.monotonic()
        with pytest.raises(DeciderError):
            DeciderModel(srv.url, 0.3).predict(inp())
        assert time.monotonic() - t0 < 3


def test_server_hangs_up_mid_response_raises():
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["content-length"]))
            self.send_response(200)
            self.send_header("content-length", "500")
            self.end_headers()
            self.wfile.write(b"{")  # short body, then close

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pytest.raises(DeciderError):
            DeciderModel(f"http://127.0.0.1:{srv.server_address[1]}", 2.0).predict(inp())
    finally:
        srv.shutdown()
        srv.server_close()


# ---- abort wakes a request in flight ----

def test_abort_wakes_request_in_flight():
    with FakeDecider(mode="sleep") as srv:
        m = DeciderModel(srv.url, 30.0)
        result = {}

        def call():
            try:
                m.predict(inp())
            except DeciderError as e:
                result["err"] = e

        t = threading.Thread(target=call)
        t0 = time.monotonic()
        t.start()
        assert srv.arrived.wait(5)
        m.abort()
        t.join(5)
        assert not t.is_alive()
        assert "err" in result and time.monotonic() - t0 < 5


def test_predict_after_abort_raises_without_connecting():
    with FakeDecider() as srv:
        m = DeciderModel(srv.url, 2.0)
        m.abort()
        with pytest.raises(DeciderError):
            m.predict(inp())
        assert srv.requests == []


# ---- config ----

@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8000", "http://localhost:8000", "http://[::1]:8000/", "http://127.0.0.1",
])
def test_config_accepts_loopback_http(url):
    c = Config.from_env({"MODEL": "decider", "DECIDER_URL": url})
    assert c.model == "decider" and c.decider_url == url.rstrip("/")


@pytest.mark.parametrize("url", [
    "", "   ", "https://127.0.0.1:8000", "http://example.com:8000", "http://10.0.0.5:8000",
    "http://127.0.0.1.evil.com", "ftp://127.0.0.1", "127.0.0.1:8000", "http://user:pw@127.0.0.1:8000",
    "http://127.0.0.1:99999", "http://127.0.0.1:8000/x?y=1",
])
def test_config_rejects_bad_decider_url(url):
    with pytest.raises(ConfigError):
        Config.from_env({"MODEL": "decider", "DECIDER_URL": url})


def test_config_decider_needs_url_and_model_must_be_known():
    with pytest.raises(ConfigError):
        Config.from_env({"MODEL": "decider"})
    with pytest.raises(ConfigError):
        Config.from_env({"MODEL": "gpt"})
    assert Config.from_env({}).model == "mock"
    assert Config.from_env({"MODEL": "mock", "DECIDER_URL": "junk"}).model == "mock"


def test_config_reuses_deadline():
    c = Config.from_env({"MODEL": "decider", "DECIDER_URL": "http://127.0.0.1:1",
                         "DECIDER_DEADLINE_S": "0.5"})
    assert c.deadline_s == 0.5


# ---- through the trader ----

def mkbook(recv_ts=T0):
    return Book(ts=recv_ts, recv_ts=recv_ts, coin="BTC",
                bids=[[65000.0 - i, 5.0] for i in range(10)],
                asks=[[65001.0 + i, 5.0] for i in range(10)])


def mktrader(model, deadline_s=2.0):
    cfg = Config(deadline_s=deadline_s)
    store = Store(":memory:")
    clock = lambda: T0  # noqa: E731
    tr = Trader(cfg, store, model, PaperExchange(store, AssetMeta("BTC", 5), "BTC"), Ledger(),
                KillSwitch(cfg, clock), clock=clock)
    return tr, store


def decide_once(model, deadline_s=2.0):
    async def go():
        tr, store = mktrader(model, deadline_s)
        await tr.on_book(mkbook())
        return store

    return asyncio.run(go())


def test_trader_acts_on_decider_answer():
    with FakeDecider(good_body(0.95)) as srv:
        store = decide_once(DeciderModel(srv.url, 2.0))
    (d,) = store.decisions()
    assert d["reason"] == "signal" and d["action"] == "buy" and d["p_up"] == pytest.approx(0.95)


@pytest.mark.parametrize("name", ["nan", "above_one", "missing_down", "sum_not_one"])
def test_trader_holds_invalid_and_never_stores_bad_p(name):
    with FakeDecider(mode="raw", raw=json.dumps(BAD_BODIES[name]).encode()) as srv:
        store = decide_once(DeciderModel(srv.url, 2.0))
    (d,) = store.decisions()
    assert d["reason"] == "invalid" and d["action"] == "hold" and d["p_up"] is None
    assert [e["kind"] for e in store.events()] == ["model_error"]


def test_trader_http_500_and_refused_are_invalid_holds_not_crashes():
    with FakeDecider(mode="raw", status=500, raw=b"boom") as srv:
        store = decide_once(DeciderModel(srv.url, 2.0))
    assert store.decisions()[0]["reason"] == "invalid"
    store = decide_once(DeciderModel(f"http://127.0.0.1:{free_port()}", 2.0))
    assert store.decisions()[0]["reason"] == "invalid"


def test_trader_slow_decider_is_late_hold_and_blocks_second_call():
    with FakeDecider(good_body(0.95), mode="sleep") as srv:
        model = DeciderModel(srv.url, 5.0)

        async def go():
            tr, store = mktrader(model, deadline_s=0.2)
            await tr.on_book(mkbook(T0))
            await tr.on_book(mkbook(T0 + 1))  # first request still running in its thread
            srv.release.set()
            return store

        store = asyncio.run(go())
    assert [d["reason"] for d in store.decisions()] == ["late", "late"]
    assert len(srv.requests) == 1  # no second request while the first still runs


def test_repeated_failures_keep_the_bot_alive():
    model = DeciderModel(f"http://127.0.0.1:{free_port()}", 1.0)

    async def go():
        tr, store = mktrader(model)
        for i in range(15):
            await tr.on_book(mkbook(T0 + i))
        return tr, store

    tr, store = asyncio.run(go())
    assert [d["reason"] for d in store.decisions()] == ["invalid"] * 15
    assert not tr.stopped


def test_stop_wakes_a_decider_request_in_flight():
    with FakeDecider(mode="sleep") as srv:
        model = DeciderModel(srv.url, 30.0)

        async def go():
            tr, store = mktrader(model, deadline_s=30.0)
            task = asyncio.create_task(tr.on_book(mkbook()))
            while not srv.arrived.is_set():
                await asyncio.sleep(0.01)
            t0 = time.monotonic()
            tr.stopped = True
            await asyncio.wait_for(task, 5)
            return store, time.monotonic() - t0

        store, took = asyncio.run(go())
    assert took < 3
    assert store.decisions()[0]["reason"] == "killed"


def test_stop_calls_model_abort():
    class M:
        aborted = 0

        def predict(self, inp):
            raise AssertionError

        def abort(self):
            self.aborted += 1

    m = M()
    tr, _ = mktrader(m)
    assert m.aborted == 0
    tr.stopped = True
    assert m.aborted == 1


def test_build_model_selects_backend():
    from jevhl.model import MockModel, build_model

    assert isinstance(build_model(Config()), MockModel)
    m = build_model(Config.from_env({"MODEL": "decider", "DECIDER_URL": "http://127.0.0.1:8000"}))
    assert isinstance(m, DeciderModel)
