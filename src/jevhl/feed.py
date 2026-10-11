"""Hyperliquid testnet market data: l2Book parsing and a reconnecting WebSocket stream; for
the maker (milestone 8) also a REST l2Book poller, the WS trades channel and a trade tape.

Testnet only: there is no mainnet URL in this code base.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
import urllib.request
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, replace

import websockets

WS_URL = "wss://api.hyperliquid-testnet.xyz/ws"
INFO_URL = "https://api.hyperliquid-testnet.xyz/info"
TOP_LEVELS = 10
REST_LEVELS = 20  # all a REST l2Book returns; the maker's depth bands need them
REST_TIMEOUT_S = 2.0
MAX_REST_BYTES = 256 * 1024


class ParseError(ValueError):
    pass


@dataclass(frozen=True)
class Book:
    """One l2Book snapshot. bids/asks are [[px, sz], ...], best first: top 10 from the WS feed,
    all 20 from a REST read."""

    ts: float  # exchange time, epoch seconds
    recv_ts: float  # local receive time, epoch seconds (staleness is judged on this)
    coin: str
    bids: list[list[float]]
    asks: list[list[float]]
    read_ms: float | None = None  # REST round trip, for the maker's latency log

    @property
    def mid(self) -> float:
        return (self.bids[0][0] + self.asks[0][0]) / 2

    @property
    def spread_bps(self) -> float:
        return (self.asks[0][0] - self.bids[0][0]) / self.mid * 1e4


def _num(x: object, what: str) -> float:
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ParseError(f"{what} is not a number: {x!r}") from None
    if not math.isfinite(v):
        raise ParseError(f"{what} is not finite: {x!r}")
    return v


def _side(levels: object, name: str, descending: bool, top: int) -> list[list[float]]:
    if not isinstance(levels, list) or not levels:
        raise ParseError(f"{name} missing or empty")
    out: list[list[float]] = []
    for lv in levels[:top]:
        if not isinstance(lv, dict) or "px" not in lv or "sz" not in lv:
            raise ParseError(f"bad {name} level: {lv!r}")
        px, sz = _num(lv["px"], f"{name} px"), _num(lv["sz"], f"{name} sz")
        if px <= 0 or sz <= 0:
            raise ParseError(f"{name} level must be positive: {lv!r}")
        if out and (px >= out[-1][0] if descending else px <= out[-1][0]):
            raise ParseError(f"{name} not strictly best-first at {px}")
        out.append([px, sz])
    return out


def _reject_constant(c: str) -> None:
    raise ParseError(f"non-finite JSON constant {c}")


def _load(raw: str | bytes | dict) -> dict:
    if isinstance(raw, dict):
        msg = raw
    else:
        try:
            msg = json.loads(raw, parse_constant=_reject_constant)
        except ValueError as e:
            raise ParseError(f"invalid JSON: {e}") from None
    if not isinstance(msg, dict):
        raise ParseError("message is not an object")
    return msg


def parse_message(raw: str | bytes | dict, coin: str, recv_ts: float) -> Book | None:
    """Parse one WS message. Returns a Book for an l2Book of `coin`, None for any other
    channel (subscription ack, pong, other coins). Raises ParseError for a malformed or
    crossed l2Book, so bad data never becomes a decision input."""
    msg = _load(raw)
    if msg.get("channel") != "l2Book":
        return None
    data = msg.get("data")
    if not isinstance(data, dict):
        raise ParseError("l2Book without data")
    if data.get("coin") != coin:
        return None
    return _book(data, coin, recv_ts, TOP_LEVELS)


def parse_l2book(data: object, coin: str, recv_ts: float) -> Book:
    """Parse a REST l2Book response (all 20 levels kept). Same checks as the WS book; a
    response for another coin is an error here, since it was asked for this one."""
    if not isinstance(data, dict):
        raise ParseError("l2Book response is not an object")
    if data.get("coin") != coin:
        raise ParseError(f"l2Book response for {data.get('coin')!r}, asked for {coin!r}")
    return _book(data, coin, recv_ts, REST_LEVELS)


def _book(data: dict, coin: str, recv_ts: float, top: int) -> Book:
    levels = data.get("levels")
    if not isinstance(levels, list) or len(levels) != 2:
        raise ParseError("levels must be [bids, asks]")
    bids = _side(levels[0], "bids", descending=True, top=top)
    asks = _side(levels[1], "asks", descending=False, top=top)
    if bids[0][0] >= asks[0][0]:
        raise ParseError(f"crossed book: bid {bids[0][0]} >= ask {asks[0][0]}")
    t_ms = _num(data.get("time"), "time")
    if t_ms <= 0:
        raise ParseError("time must be positive")
    return Book(ts=t_ms / 1000.0, recv_ts=recv_ts, coin=coin, bids=bids, asks=asks)


async def stream_books(
    coin: str,
    clock: Callable[[], float],
    *,
    url: str = WS_URL,
    connect: Callable = websockets.connect,
    on_event: Callable[[str, str], None] = lambda kind, detail: None,
    sleep: Callable = asyncio.sleep,
    ping_s: float = 4.0,
    dead_s: float = 6.0,
    max_backoff_s: float = 10.0,
) -> AsyncIterator[Book]:
    """Yield books forever: subscribe, ping when idle, reconnect with backoff on any
    connection failure. A socket that stays open but silent is pinged after ping_s; if
    nothing at all arrives within dead_s more it is dropped and reconnected the same way.
    Only a book proves the feed is alive: a socket that answers pings but sends no book for
    ping_s + dead_s is dropped too.
    Malformed messages are reported through on_event and skipped."""
    backoff = 1.0
    sub = {"method": "subscribe", "subscription": {"type": "l2Book", "coin": coin}}
    while True:
        try:
            async with connect(url) as ws:
                await ws.send(json.dumps(sub))
                on_event("connect", url)
                pinged = False
                loop = asyncio.get_running_loop()
                book_by = loop.time() + ping_s + dead_s  # a book must arrive before this
                while True:
                    wait = dead_s if pinged else ping_s
                    book_left = book_by - loop.time()
                    try:
                        raw = await asyncio.wait_for(ws.recv(), min(wait, max(book_left, 0)))
                    except TimeoutError:
                        if pinged:
                            raise ConnectionError(
                                f"no data for {ping_s + dead_s:g} s (pinged, no reply)"
                            ) from None
                        if book_left <= wait:  # pongs came, books did not
                            raise ConnectionError(
                                f"no book for {ping_s + dead_s:g} s (pings answered)"
                            ) from None
                        await ws.send(json.dumps({"method": "ping"}))
                        pinged = True
                        continue
                    pinged = False
                    try:
                        book = parse_message(raw, coin, clock())
                    except ParseError as e:
                        on_event("parse_error", str(e))
                        continue
                    if book is not None:
                        backoff = 1.0
                        yield book
                        book_by = loop.time() + ping_s + dead_s  # the consumer's time is not ours
        except (OSError, websockets.WebSocketException) as e:
            on_event("disconnect", f"{type(e).__name__}: {e}")
            await sleep(backoff)
            backoff = min(backoff * 2, max_backoff_s)


# ---- maker data (milestone 8) ----


def fetch_l2book(coin: str) -> object:
    """One REST l2Book read (blocking; the poller runs it in a worker thread)."""
    req = urllib.request.Request(
        INFO_URL, data=json.dumps({"type": "l2Book", "coin": coin}).encode(),
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=REST_TIMEOUT_S) as resp:  # noqa: S310 (fixed URL)
        return json.loads(resp.read(MAX_REST_BYTES), parse_constant=_reject_constant)


async def poll_books(
    coin: str,
    clock: Callable[[], float],
    *,
    every_s: float = 1.0,
    fetch: Callable[[str], object] = fetch_l2book,
    sleep: Callable = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    on_event: Callable[[str, str], None] = lambda kind, detail: None,
    on_rate_limit: Callable[[Exception], None] = lambda e: None,
) -> AsyncIterator[Book]:
    """Yield a REST book about every every_s, forever. Reads start on a fixed schedule, one at
    a time: a read (or a consumer) that overruns skips the missed slots instead of catching up
    in a burst, so there is never more than one read per every_s (SPEC hard constraint). A
    failed read or a bad response is reported as `rest_error` and yields nothing; a long
    outage is the stale-book kill's job. An HTTP 429 is also passed to on_rate_limit (the
    maker cancels and pauses quoting on it)."""
    slot = monotonic()
    while True:
        t0 = monotonic()
        book = None
        try:
            data = await asyncio.to_thread(fetch, coin)
            book = parse_l2book(data, coin, clock())
        except Exception as e:  # any failure: skip this read (CancelledError is not caught)
            on_event("rest_error", f"{type(e).__name__}: {e}")
            if getattr(e, "code", None) == 429:  # urllib's HTTPError
                on_rate_limit(e)
        if book is not None:
            yield replace(book, read_ms=(monotonic() - t0) * 1000)
        now = monotonic()
        slot += every_s
        if slot < now:
            slot += math.ceil((now - slot) / every_s) * every_s
        await sleep(slot - now)


@dataclass(frozen=True)
class Print:
    """One public trade. side is the taker's side: "buy" lifted an ask, "sell" hit a bid."""

    ts: float  # exchange time, epoch seconds
    recv_ts: float  # local receive time
    side: str
    px: float
    sz: float
    tid: int


def parse_trades(raw: str | bytes | dict, coin: str, recv_ts: float) -> list[Print] | None:
    """Parse one WS message: the prints of `coin` for a trades message, None for any other
    channel. Raises ParseError for a malformed row (NaN, bad side, missing tid)."""
    msg = _load(raw)
    if msg.get("channel") != "trades":
        return None
    data = msg.get("data")
    if not isinstance(data, list):
        raise ParseError("trades data is not a list")
    out = []
    for row in data:
        if not isinstance(row, dict):
            raise ParseError(f"bad trade row: {row!r}")
        if row.get("coin") != coin:
            continue
        side = {"B": "buy", "A": "sell"}.get(row.get("side"))  # type: ignore[arg-type]
        if side is None:
            raise ParseError(f"bad trade side: {row.get('side')!r}")
        px, sz = _num(row.get("px"), "trade px"), _num(row.get("sz"), "trade sz")
        t_ms = _num(row.get("time"), "trade time")
        tid = row.get("tid")
        if px <= 0 or sz <= 0 or t_ms <= 0:
            raise ParseError(f"trade values must be positive: {row!r}")
        if not isinstance(tid, int) or isinstance(tid, bool):
            raise ParseError(f"bad trade tid: {tid!r}")
        out.append(Print(t_ms / 1000.0, recv_ts, side, px, sz, tid))
    return out


class TradeTape:
    """Recent prints, deduped on tid (a reconnect re-sends the last trades), in arrival
    order, kept for keep_s by exchange time and at most max_prints."""

    def __init__(self, keep_s: float = 600.0, max_prints: int = 5000):
        self._keep_s, self._max = keep_s, max_prints
        self._prints: deque[tuple[int, Print]] = deque()
        self._tids: set[int] = set()
        self._seq = 0

    def add(self, prints: Iterable[Print], now: float) -> list[Print]:
        """Add prints; returns the ones not seen before."""
        new = []
        for p in prints:
            if p.tid in self._tids:
                continue
            self._seq += 1
            self._prints.append((self._seq, p))
            self._tids.add(p.tid)
            new.append(p)
        while self._prints and (len(self._prints) > self._max
                                or self._prints[0][1].ts < now - self._keep_s):
            self._tids.discard(self._prints.popleft()[1].tid)
        return new

    def since(self, cursor: int) -> tuple[list[Print], int]:
        """Prints added after cursor (0 = all), and the new cursor."""
        out = [p for seq, p in self._prints if seq > cursor]
        return out, self._seq

    def recent(self, n: int) -> list[Print]:
        return [p for _, p in list(self._prints)[-n:]] if n > 0 else []

    def flow(self, window_s: float, now: float) -> dict:
        """Taker flow over the last window_s by exchange time: count, buy and sell size, net,
        volume-weighted price, and the last print's price and side."""
        ps = [p for _, p in self._prints if p.ts > now - window_s]
        buy = sum(p.sz for p in ps if p.side == "buy")
        sell = sum(p.sz for p in ps if p.side == "sell")
        vol = buy + sell
        return {
            "count": len(ps), "buy_sz": buy, "sell_sz": sell, "net_sz": buy - sell,
            "vwap": sum(p.px * p.sz for p in ps) / vol if vol > 0 else None,
            "last_px": ps[-1].px if ps else None, "last_side": ps[-1].side if ps else None,
        }


async def stream_trades(
    coin: str,
    clock: Callable[[], float],
    *,
    url: str = WS_URL,
    connect: Callable = websockets.connect,
    on_event: Callable[[str, str], None] = lambda kind, detail: None,
    sleep: Callable = asyncio.sleep,
    ping_s: float = 4.0,
    dead_s: float = 6.0,
    max_backoff_s: float = 10.0,
) -> AsyncIterator[list[Print]]:
    """Yield batches of prints forever, on its own socket with one subscription. Same
    reconnect and backoff as stream_books, but prints are bursty (35 in 40 s on testnet), so
    liveness is judged by ping and pong only: a socket is dropped when a ping gets no reply
    within dead_s. Events are named trades_* so they are not confused with the book feed's."""
    backoff = 1.0
    sub = {"method": "subscribe", "subscription": {"type": "trades", "coin": coin}}
    while True:
        try:
            async with connect(url) as ws:
                await ws.send(json.dumps(sub))
                on_event("trades_connect", url)
                pinged = False
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), dead_s if pinged else ping_s)
                    except TimeoutError:
                        if pinged:
                            raise ConnectionError(
                                f"trades: no data for {ping_s + dead_s:g} s (pinged, no reply)"
                            ) from None
                        await ws.send(json.dumps({"method": "ping"}))
                        pinged = True
                        continue
                    pinged = False
                    try:
                        prints = parse_trades(raw, coin, clock())
                    except ParseError as e:
                        on_event("trades_parse_error", str(e))
                        continue
                    if prints:
                        backoff = 1.0
                        yield prints
        except (OSError, websockets.WebSocketException) as e:
            on_event("trades_disconnect", f"{type(e).__name__}: {e}")
            await sleep(backoff)
            backoff = min(backoff * 2, max_backoff_s)
