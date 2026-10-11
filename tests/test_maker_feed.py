"""Maker data (milestone 8): the REST l2Book snapshot, the WS trades channel, the trade tape,
the one-per-second REST poller and the reconnecting trades stream. Fixtures are real testnet
captures (2026-10-10); other users' addresses in the trades are replaced by placeholders."""

import asyncio
import copy
import json
import urllib.error
from pathlib import Path

import pytest

from jevhl.feed import (
    ParseError,
    Print,
    TradeTape,
    parse_l2book,
    parse_message,
    parse_trades,
    poll_books,
    stream_trades,
)
from test_feed import FakeWS, make_connect, no_sleep

FIX = Path(__file__).parent / "fixtures"
REST = json.loads((FIX / "rest_l2book_btc.json").read_text())
TRADES = json.loads((FIX / "ws_trades_btc.json").read_text())
SUB_ACK, SNAPSHOT, LIVE = TRADES[0], TRADES[1], TRADES[2]


def run(coro):
    return asyncio.run(coro)


# ---- REST l2Book ----


def test_parse_rest_l2book_fixture_keeps_20_levels():
    b = parse_l2book(REST, "BTC", recv_ts=5.0)
    assert len(b.bids) == 20 and len(b.asks) == 20
    assert b.bids[0] == [82621.0, 0.3] and b.asks[0] == [82630.0, 0.30025]
    assert b.ts == pytest.approx(1791650852.512) and b.recv_ts == 5.0 and b.read_ms is None


def test_ws_l2book_still_truncates_to_10():
    ws_msg = {"channel": "l2Book", "data": REST}
    b = parse_message(ws_msg, "BTC", 1.0)
    assert len(b.bids) == 10 and len(b.asks) == 10  # the taker's input is unchanged


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(coin="ETH"),  # REST answers for one coin: another coin is an error
    lambda d: d["levels"][0][0].update(px="nan"),
    lambda d: d["levels"][0][0].update(px="82630.0"),  # crossed
    lambda d: d.update(levels=[d["levels"][0]]),
    lambda d: d.update(time=0),
])
def test_bad_rest_l2book_raises(mutate):
    data = copy.deepcopy(REST)
    mutate(data)
    with pytest.raises(ParseError):
        parse_l2book(data, "BTC", 1.0)


# ---- trades ----


def test_parse_trades_fixture():
    prints = parse_trades(json.dumps(SNAPSHOT), "BTC", recv_ts=9.0)
    assert len(prints) == 30
    p = prints[0]
    assert p == Print(ts=1791650621.094, recv_ts=9.0, side="buy", px=82682.0, sz=0.00014,
                      tid=398356349106008)
    assert prints[1].side == "sell"  # "A": the taker sold
    assert parse_trades(LIVE, "BTC", 1.0)[0].px == 82630.0


@pytest.mark.parametrize("msg", [SUB_ACK, {"channel": "pong"}, {"channel": "l2Book", "data": {}}])
def test_parse_trades_ignores_other_channels(msg):
    assert parse_trades(msg, "BTC", 1.0) is None


def test_parse_trades_ignores_other_coins():
    msg = copy.deepcopy(LIVE)
    msg["data"][0]["coin"] = "ETH"
    prints = parse_trades(msg, "BTC", 1.0)
    assert [p.tid for p in prints] == [LIVE["data"][1]["tid"]]


def _bad(field, value):
    msg = copy.deepcopy(LIVE)
    msg["data"][0][field] = value
    return msg


@pytest.mark.parametrize("msg", [
    _bad("px", "nan"), _bad("px", "inf"), _bad("sz", "NaN"), _bad("px", "-1"), _bad("sz", "0"),
    _bad("side", "X"), _bad("side", None), _bad("tid", "12"), _bad("tid", True),
    _bad("time", 0), _bad("px", None),
    {"channel": "trades", "data": {"not": "a list"}},
    {"channel": "trades", "data": ["row"]},
    "not json",
    json.dumps(LIVE).replace('"82630.0"', "NaN"),
])
def test_parse_trades_rejects_nan_and_bad_side(msg):
    with pytest.raises(ParseError):
        parse_trades(msg, "BTC", 1.0)


def pr(tid, ts=100.0, side="buy", px=100.0, sz=1.0, recv_ts=None):
    return Print(ts=ts, recv_ts=ts if recv_ts is None else recv_ts, side=side, px=px, sz=sz,
                 tid=tid)


def test_trade_tape_flow_window_and_dedupe_on_tid():
    tape = TradeTape()
    assert tape.add([pr(1, 100.0, "buy", 100.0, 2.0), pr(2, 130.0, "sell", 102.0, 1.0)], 130.0)
    assert tape.add([pr(2, 130.0, "sell", 102.0, 1.0)], 131.0) == []  # same tid: not again
    tape.add([pr(3, 150.0, "buy", 101.0, 0.5)], 150.0)
    f = tape.flow(30.0, now=150.0)  # window (120, 150]: tids 2 and 3
    assert f["count"] == 2 and f["buy_sz"] == 0.5 and f["sell_sz"] == 1.0
    assert f["net_sz"] == pytest.approx(-0.5)
    assert f["vwap"] == pytest.approx((102.0 * 1.0 + 101.0 * 0.5) / 1.5)
    assert (f["last_px"], f["last_side"]) == (101.0, "buy")
    assert tape.flow(30.0, now=200.0)["count"] == 0
    assert [p.tid for p in tape.recent(2)] == [2, 3]


def test_trade_tape_since_returns_each_print_once_in_arrival_order():
    tape = TradeTape()
    tape.add([pr(5), pr(6)], 100.0)
    first, cur = tape.since(0)
    tape.add([pr(6), pr(7)], 101.0)
    second, cur = tape.since(cur)
    assert [p.tid for p in first] == [5, 6] and [p.tid for p in second] == [7]
    assert tape.since(cur) == ([], cur)


def test_trade_tape_forgets_old_prints_and_stays_bounded():
    tape = TradeTape(keep_s=600.0, max_prints=3)
    tape.add([pr(1, 0.0)], 0.0)
    tape.add([pr(2, 700.0)], 700.0)  # tid 1 is older than 600 s now
    assert [p.tid for p in tape.recent(10)] == [2]
    tape.add([pr(i, 700.0) for i in range(3, 8)], 700.0)
    assert [p.tid for p in tape.recent(10)] == [5, 6, 7]


# ---- the REST poller ----


class Sched:
    """A fake monotonic clock: sleep and fetch durations move it."""

    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def __call__(self):
        return self.t

    async def sleep(self, s):
        assert s >= 0
        self.sleeps.append(s)
        self.t += s


def test_poll_books_never_two_requests_in_flight_or_faster_than_1_s():
    sched = Sched()
    starts, inflight, peak = [], [0], [0]
    durations = iter([0.2, 0.3, 2.5, 0.1, 0.1, 0.1])

    def fetch(coin):
        inflight[0] += 1
        peak[0] = max(peak[0], inflight[0])
        starts.append(sched.t)
        sched.t += next(durations)
        inflight[0] -= 1
        return REST

    async def go():
        out = []
        async for b in poll_books("BTC", lambda: sched.t, fetch=fetch, sleep=sched.sleep,
                                  monotonic=sched):
            out.append(b)
            if len(out) == 6:
                break
        return out

    books = run(go())
    assert peak[0] == 1
    assert starts == [0.0, 1.0, 2.0, 5.0, 6.0, 7.0]  # the 2.5 s read skips slots 3 and 4
    assert all(b - a >= 1.0 for a, b in zip(starts, starts[1:], strict=False))
    assert books[0].read_ms == pytest.approx(200.0) and len(books[0].bids) == 20


def test_rest_error_logged_and_no_book_yielded():
    sched = Sched()
    answers = iter([OSError("dns"), {"bad": "body"}, REST])
    events = []

    def fetch(coin):
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a

    async def go():
        async for b in poll_books("BTC", lambda: sched.t, fetch=fetch, sleep=sched.sleep,
                                  monotonic=sched, on_event=lambda k, d: events.append((k, d))):
            return b

    book = run(go())
    assert book.recv_ts == 2.0  # the third read, two slots later
    assert [k for k, _ in events] == ["rest_error", "rest_error"]
    assert "dns" in events[0][1]


def test_rest_rate_limit_is_reported_to_on_rate_limit():
    sched = Sched()
    err = urllib.error.HTTPError("https://example.invalid/info", 429, "Too Many Requests", {},
                                 None)
    answers = iter([OSError("dns"), err, REST])
    limited = []

    def fetch(coin):
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a

    async def go():
        async for b in poll_books("BTC", lambda: sched.t, fetch=fetch, sleep=sched.sleep,
                                  monotonic=sched, on_rate_limit=limited.append):
            return b

    assert run(go()).recv_ts == 2.0
    assert limited == [err]  # the 429 only, not the DNS failure


# ---- the trades stream ----


async def take(agen, n):
    out = []
    async for x in agen:
        out.append(x)
        if len(out) == n:
            break
    await agen.aclose()
    return out


def test_stream_trades_subscribes_and_yields_print_batches():
    ws = FakeWS([json.dumps(SUB_ACK), json.dumps(SNAPSHOT), json.dumps({"channel": "pong"}),
                 json.dumps(LIVE)])
    batches = run(take(stream_trades("BTC", lambda: 3.0, connect=make_connect([ws]),
                                     sleep=no_sleep), 2))
    assert ws.sent[0] == {"method": "subscribe", "subscription": {"type": "trades", "coin": "BTC"}}
    assert [len(b) for b in batches] == [30, 2]


def test_stream_trades_reconnects_with_backoff():
    ws1 = FakeWS([ConnectionError("boom")])
    ws2 = FakeWS([json.dumps(LIVE), ConnectionError("again")])
    ws3 = FakeWS([json.dumps(LIVE)])
    sleeps, events = [], []

    async def sleep(s):
        sleeps.append(s)

    conn = make_connect([OSError("refused"), ws1, ws2, ws3])
    batches = run(take(stream_trades("BTC", lambda: 1.0, connect=conn, sleep=sleep,
                                     on_event=lambda k, d: events.append(k)), 2))
    assert len(batches) == 2 and ws3.sent[0]["method"] == "subscribe"
    assert events.count("trades_disconnect") == 3 and "disconnect" not in events
    assert sleeps == [1.0, 2.0, 1.0]  # doubled, then reset by the prints on ws2


def test_stream_trades_pings_when_quiet_and_drops_a_dead_socket():
    ws1 = FakeWS(["TIMEOUT"])  # silent, never answers the ping
    ws2 = FakeWS([json.dumps(LIVE)])
    events = []
    batches = run(asyncio.wait_for(  # bounded: a socket never dropped fails, not hangs
        take(stream_trades("BTC", lambda: 1.0, connect=make_connect([ws1, ws2]),
                           sleep=no_sleep, ping_s=0.01, dead_s=0.01,
                           on_event=lambda k, d: events.append(k)), 1), 5))
    assert {"method": "ping"} in ws1.sent
    assert "trades_disconnect" in events and len(batches) == 1


def test_stream_trades_answered_pings_keep_a_quiet_socket():
    pongs = [json.dumps({"channel": "pong"})] * 3

    class Replying(FakeWS):  # answers each ping with a pong, then finally a trade
        async def send(self, data):
            await super().send(data)
            if json.loads(data).get("method") == "ping":
                self.script.insert(0, pongs.pop() if pongs else json.dumps(LIVE))

    ws = Replying(["TIMEOUT"])
    events = []
    batches = run(take(stream_trades("BTC", lambda: 1.0, connect=make_connect([ws]),
                                     sleep=no_sleep, ping_s=0.01, dead_s=0.05,
                                     on_event=lambda k, d: events.append(k)), 1))
    assert len(batches) == 1 and "trades_disconnect" not in events


def test_stream_trades_reports_malformed_messages_and_continues():
    ws = FakeWS(["garbage", json.dumps(LIVE)])
    events = []
    batches = run(take(stream_trades("BTC", lambda: 1.0, connect=make_connect([ws]),
                                     sleep=no_sleep, on_event=lambda k, d: events.append(k)), 1))
    assert len(batches) == 1 and "trades_parse_error" in events
