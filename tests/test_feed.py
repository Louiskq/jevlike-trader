import asyncio
import copy
import json
from pathlib import Path

import pytest

from jevhl.feed import Book, ParseError, parse_message, stream_books

FIX = Path(__file__).parent / "fixtures" / "ws_session_btc.json"
SESSION = json.loads(FIX.read_text())  # real testnet capture: subscribe ack, l2Book, pong, l2Book
SUB_ACK, L2, PONG = SESSION[0], SESSION[1], SESSION[2]


def l2(**over):
    m = copy.deepcopy(L2)
    m["data"].update(over)
    return m


def test_parse_real_l2book():
    b = parse_message(json.dumps(L2), "BTC", recv_ts=1000.0)
    assert isinstance(b, Book)
    assert b.coin == "BTC" and b.recv_ts == 1000.0
    assert b.ts == pytest.approx(1791131861.445)
    assert len(b.bids) == 10 and len(b.asks) == 10  # top 10 of the 20 sent
    assert b.bids[0] == [85517.0, 0.3] and b.asks[0] == [85518.0, 0.00005]
    assert b.mid == pytest.approx(85517.5)
    assert b.spread_bps == pytest.approx(1.0 / 85517.5 * 1e4)
    assert all(b.bids[i][0] > b.bids[i + 1][0] for i in range(9))  # best first
    assert all(b.asks[i][0] < b.asks[i + 1][0] for i in range(9))


def test_parse_accepts_bytes_and_dict():
    raw = json.dumps(L2).encode()
    assert parse_message(raw, "BTC", 1.0).mid == parse_message(L2, "BTC", 1.0).mid


@pytest.mark.parametrize("msg", [SUB_ACK, PONG, {"channel": "trades", "data": []}])
def test_non_book_channels_ignored(msg):
    assert parse_message(msg, "BTC", 1.0) is None


def test_other_coin_ignored():
    assert parse_message(l2(coin="ETH"), "BTC", 1.0) is None


def mutate(fn):
    m = copy.deepcopy(L2)
    fn(m["data"])
    return m


@pytest.mark.parametrize(
    "bad",
    [
        "not json",
        "[1, 2]",
        json.dumps({"channel": "l2Book"}),  # no data
        mutate(lambda d: d.update(levels=[d["levels"][0]])),  # one side
        mutate(lambda d: d.update(levels=[[], d["levels"][1]])),  # empty bids
        mutate(lambda d: d.update(levels=[d["levels"][0], []])),  # empty asks
        mutate(lambda d: d["levels"][0][0].update(px="nan")),
        mutate(lambda d: d["levels"][0][0].update(px="inf")),
        mutate(lambda d: d["levels"][1][0].update(sz="nan")),
        mutate(lambda d: d["levels"][1][0].update(sz="0")),
        mutate(lambda d: d["levels"][0][0].update(px="-5")),
        mutate(lambda d: d["levels"][0][0].update(px="abc")),
        mutate(lambda d: d["levels"][0][0].pop("px")),
        mutate(lambda d: d["levels"][0].reverse()),  # bids not best first
        mutate(lambda d: d["levels"][0][0].update(px="85518.0")),  # crossed (bid == ask)
        mutate(lambda d: d["levels"][0][0].update(px="90000.0")),  # crossed (bid > ask)
        mutate(lambda d: d.update(time="nan")),
        mutate(lambda d: d.update(time=0)),
        mutate(lambda d: d.update(time=None)),
    ],
)
def test_malformed_l2book_raises(bad):
    with pytest.raises(ParseError):
        parse_message(bad, "BTC", 1.0)


def test_nan_json_literal_rejected():
    raw = json.dumps(L2).replace('"85517.0"', "NaN")  # bare NaN is not valid JSON
    with pytest.raises(ParseError):
        parse_message(raw, "BTC", 1.0)


# ---- stream_books with a fake connection ----


class FakeWS:
    def __init__(self, script):
        self.script = list(script)  # str messages, "TIMEOUT", or an Exception to raise
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def recv(self):
        while True:
            if not self.script:
                await asyncio.sleep(3600)
            item = self.script.pop(0)
            if item == "TIMEOUT":
                await asyncio.sleep(3600)  # outer wait_for times out
            if isinstance(item, Exception):
                raise item
            return item


def run(coro):
    return asyncio.run(coro)


async def take(agen, n):
    out = []
    async for b in agen:
        out.append(b)
        if len(out) == n:
            break
    await agen.aclose()
    return out


def make_connect(sessions):
    conns = []
    it = iter(sessions)

    def connect(url):
        nxt = next(it)
        if isinstance(nxt, Exception):
            raise nxt
        conns.append(nxt)
        return nxt

    connect.conns = conns
    return connect


async def no_sleep(_):
    return None


def test_stream_subscribes_and_yields_books_skipping_noise():
    ws = FakeWS([json.dumps(SUB_ACK), json.dumps(L2), json.dumps(PONG), json.dumps(SESSION[3])])
    times = iter(float(i) for i in range(1, 100))
    events = []
    books = run(
        take(
            stream_books("BTC", lambda: next(times), connect=make_connect([ws]),
                         on_event=lambda k, d: events.append(k), sleep=no_sleep),
            2,
        )
    )
    assert ws.sent[0] == {"method": "subscribe", "subscription": {"type": "l2Book", "coin": "BTC"}}
    assert [b.recv_ts for b in books] == [2.0, 4.0]  # stamped per message, books are #2 and #4
    assert "connect" in events


def test_stream_skips_malformed_and_reports():
    ws = FakeWS(["garbage", json.dumps(L2)])
    events = []
    books = run(
        take(
            stream_books("BTC", lambda: 1.0, connect=make_connect([ws]),
                         on_event=lambda k, d: events.append(k), sleep=no_sleep),
            1,
        )
    )
    assert len(books) == 1 and "parse_error" in events


def test_stream_reconnects_and_resubscribes_after_drop():
    ws1 = FakeWS([json.dumps(L2), ConnectionError("boom")])
    ws2 = FakeWS([json.dumps(SESSION[3])])
    sleeps = []

    async def sleep(s):
        sleeps.append(s)

    events = []
    conn = make_connect([OSError("refused"), ws1, ws2])
    books = run(
        take(
            stream_books("BTC", lambda: 1.0, connect=conn,
                         on_event=lambda k, d: events.append(k), sleep=sleep),
            2,
        )
    )
    assert len(books) == 2
    assert ws2.sent[0]["method"] == "subscribe"  # resubscribed on the new connection
    assert events.count("disconnect") == 2  # refused connect, then dropped stream
    assert sleeps == [1.0, 1.0]  # a received book reset the backoff before the drop


def test_stream_pings_when_idle():
    ws = FakeWS(["TIMEOUT", json.dumps(L2)])
    books = run(
        take(
            stream_books("BTC", lambda: 1.0, connect=make_connect([ws]), ping_s=0.01,
                         sleep=no_sleep),
            1,
        )
    )
    assert len(books) == 1
    assert {"method": "ping"} in ws.sent


def test_stream_backoff_doubles_on_failures_and_resets_after_a_book():
    ws1 = FakeWS([json.dumps(L2), ConnectionError("boom")])
    ws2 = FakeWS([json.dumps(SESSION[3])])
    sleeps = []

    async def sleep(s):
        sleeps.append(s)

    conn = make_connect([OSError("a"), OSError("b"), ws1, OSError("c"), ws2])
    books = run(take(stream_books("BTC", lambda: 1.0, connect=conn, sleep=sleep), 2))
    assert len(books) == 2
    # two refusals double the wait; the book resets it; then the drop waits 1 and the next
    # refusal waits 2
    assert sleeps == [1.0, 2.0, 1.0, 2.0]


class SilentWS(FakeWS):
    """Stays open and says nothing at all, even to a ping (the incident's socket)."""

    def __init__(self):
        super().__init__([])
        self.closed = False

    async def __aexit__(self, *a):
        self.closed = True
        return False


class PongWS(FakeWS):
    """Answers each of the first `pongs` pings with a pong, then sends `then`, then goes quiet."""

    def __init__(self, pongs, then):
        super().__init__([])
        self.pongs, self.then, self.given = pongs, list(then), 0

    async def recv(self):
        while True:
            pings = sum(1 for m in self.sent if m == {"method": "ping"})
            if self.given < self.pongs and pings > self.given:
                self.given += 1
                return json.dumps(PONG)
            if self.given >= self.pongs and self.then:
                return self.then.pop(0)
            await asyncio.sleep(0.001)


def guarded(coro, s=3.0):
    return run(asyncio.wait_for(coro, s))  # a hang is a failure, not a stuck test run


def test_silent_open_socket_is_pinged_then_dropped_and_reconnected():
    dead = SilentWS()
    ws2 = FakeWS([json.dumps(L2)])
    sleeps, events = [], []

    async def sleep(s):
        sleeps.append(s)

    books = guarded(take(
        stream_books("BTC", lambda: 1.0, connect=make_connect([dead, ws2]), ping_s=0.01,
                     dead_s=0.02, on_event=lambda k, d: events.append((k, d)), sleep=sleep), 1))
    assert len(books) == 1
    assert dead.sent[0]["method"] == "subscribe" and {"method": "ping"} in dead.sent
    assert dead.closed
    assert ws2.sent[0]["method"] == "subscribe"  # resubscribed on the new connection
    assert [k for k, _ in events].count("disconnect") == 1
    assert "no data" in dict(events)["disconnect"]
    assert sleeps == [1.0]  # through the existing backoff


def test_the_dead_wait_is_dead_s_not_ping_s():
    """A reply that arrives after ping_s but inside dead_s keeps the connection."""
    ws = FakeWS([])
    sent_at = []

    async def recv():
        while not ws.sent or ws.sent[-1] != {"method": "ping"}:
            await asyncio.sleep(0.001)
        sent_at.append(1)
        await asyncio.sleep(0.05)  # 5 x ping_s, well inside dead_s
        if len(sent_at) == 1:
            return json.dumps(L2)
        await asyncio.sleep(3600)

    ws.recv = recv
    events = []
    books = guarded(take(
        stream_books("BTC", lambda: 1.0, connect=make_connect([ws]), ping_s=0.01, dead_s=1.0,
                     on_event=lambda k, d: events.append(k), sleep=no_sleep), 1))
    assert len(books) == 1 and "disconnect" not in events


class PongOnlyWS(FakeWS):
    """Answers every ping with a pong and never sends an l2Book (the incident's likely shape)."""

    def __init__(self):
        super().__init__([])
        self.q, self.closed = asyncio.Queue(), False

    async def __aexit__(self, *a):
        self.closed = True
        return False

    async def send(self, m):
        await super().send(m)
        if json.loads(m).get("method") == "ping":
            await self.q.put(json.dumps(PONG))

    async def recv(self):
        return await self.q.get()


def test_a_pong_is_not_proof_of_life_for_books_a_pong_only_socket_is_dropped():
    ws, ws2 = PongOnlyWS(), FakeWS([json.dumps(L2)])
    events, sleeps = [], []

    async def sleep(s):
        sleeps.append(s)

    books = guarded(take(
        stream_books("BTC", lambda: 1.0, connect=make_connect([ws, ws2]), ping_s=0.01,
                     dead_s=0.04, on_event=lambda k, d: events.append((k, d)), sleep=sleep), 1))
    assert len(books) == 1 and ws.closed
    assert [k for k, _ in events].count("disconnect") == 1 and sleeps == [1.0]
    assert "no book" in dict(events)["disconnect"]


def test_the_pong_only_drop_comes_at_ping_plus_dead_after_the_last_book():
    ws, ws2 = PongOnlyWS(), FakeWS([json.dumps(L2)])
    loop_times = []

    async def sleep(s):
        loop_times.append(asyncio.get_running_loop().time())

    async def go():
        t0 = asyncio.get_running_loop().time()
        await take(stream_books("BTC", lambda: 1.0, connect=make_connect([ws, ws2]),
                                ping_s=0.05, dead_s=0.15, sleep=sleep), 1)
        return loop_times[0] - t0

    took = guarded(go())
    assert 0.18 <= took < 0.6  # about ping_s + dead_s, not earlier, not never


class ChattyWS(PongOnlyWS):
    """Sends a non-book message every 2 ms, faster than any ping timer, and never a book."""

    async def recv(self):
        await asyncio.sleep(0.002)
        return json.dumps(PONG)


def test_a_socket_chattering_without_books_is_dropped_though_no_timer_ever_fires():
    ws, ws2 = ChattyWS(), FakeWS([json.dumps(L2)])
    events = []
    books = guarded(take(
        stream_books("BTC", lambda: 1.0, connect=make_connect([ws, ws2]), ping_s=0.05,
                     dead_s=0.1, on_event=lambda k, d: events.append((k, d)), sleep=no_sleep), 1))
    assert len(books) == 1 and ws.closed and not ws.sent[1:]  # never even pinged
    assert "no book" in dict(events)["disconnect"]


def test_the_book_bound_restarts_at_each_book_not_at_connect():
    class Slow(FakeWS):
        async def recv(self):
            await asyncio.sleep(0.04)  # a book every 40 ms; the bound is 80 ms; run is 240 ms
            return json.dumps(L2)

    events = []
    books = guarded(take(
        stream_books("BTC", lambda: 1.0, connect=make_connect([Slow([])]), ping_s=0.03,
                     dead_s=0.05, on_event=lambda k, d: events.append(k), sleep=no_sleep), 6))
    assert len(books) == 6 and "disconnect" not in events


def test_a_quiet_spell_shorter_than_ping_plus_dead_is_not_dropped():
    ws = PongWS(pongs=3, then=[json.dumps(L2)])  # pings at 0.03 s steps, book after ~0.09 s
    events = []
    books = guarded(take(
        stream_books("BTC", lambda: 1.0, connect=make_connect([ws]), ping_s=0.03, dead_s=0.5,
                     on_event=lambda k, d: events.append(k), sleep=no_sleep), 1))
    assert len(books) == 1
    assert sum(1 for m in ws.sent if m == {"method": "ping"}) >= 3
    assert "disconnect" not in events and events.count("connect") == 1


def test_books_arriving_inside_the_bound_keep_the_socket_even_with_pongs_between():
    ws = FakeWS([json.dumps(L2)] * 3)
    events = []
    books = guarded(take(
        stream_books("BTC", lambda: 1.0, connect=make_connect([ws]), ping_s=0.01, dead_s=0.5,
                     on_event=lambda k, d: events.append(k), sleep=no_sleep), 3))
    assert len(books) == 3 and "disconnect" not in events


def test_a_book_after_a_drop_resets_the_backoff_for_a_silent_socket_too():
    ws1 = FakeWS([json.dumps(L2)])  # one book, then silence
    ws1_sleeps = []

    async def sleep(s):
        ws1_sleeps.append(s)

    ws2 = FakeWS([json.dumps(SESSION[3])])
    books = guarded(take(
        stream_books("BTC", lambda: 1.0, connect=make_connect([ws1, ws2]), ping_s=0.01,
                     dead_s=0.02, sleep=sleep), 2))
    assert len(books) == 2 and ws1_sleeps == [1.0]
