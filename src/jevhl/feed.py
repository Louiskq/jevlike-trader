"""Hyperliquid testnet l2Book feed: message parsing and a reconnecting WebSocket stream.

Testnet only: there is no mainnet URL in this code base.
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

import websockets

WS_URL = "wss://api.hyperliquid-testnet.xyz/ws"
INFO_URL = "https://api.hyperliquid-testnet.xyz/info"
TOP_LEVELS = 10


class ParseError(ValueError):
    pass


@dataclass(frozen=True)
class Book:
    """One l2Book snapshot. bids/asks are [[px, sz], ...], best first, top 10."""

    ts: float  # exchange time, epoch seconds
    recv_ts: float  # local receive time, epoch seconds (staleness is judged on this)
    coin: str
    bids: list[list[float]]
    asks: list[list[float]]

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


def _side(levels: object, name: str, descending: bool) -> list[list[float]]:
    if not isinstance(levels, list) or not levels:
        raise ParseError(f"{name} missing or empty")
    out: list[list[float]] = []
    for lv in levels[:TOP_LEVELS]:
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


def parse_message(raw: str | bytes | dict, coin: str, recv_ts: float) -> Book | None:
    """Parse one WS message. Returns a Book for an l2Book of `coin`, None for any other
    channel (subscription ack, pong, other coins). Raises ParseError for a malformed or
    crossed l2Book, so bad data never becomes a decision input."""
    if isinstance(raw, dict):
        msg = raw
    else:
        try:
            msg = json.loads(raw, parse_constant=_reject_constant)
        except ValueError as e:
            raise ParseError(f"invalid JSON: {e}") from None
    if not isinstance(msg, dict):
        raise ParseError("message is not an object")
    if msg.get("channel") != "l2Book":
        return None
    data = msg.get("data")
    if not isinstance(data, dict):
        raise ParseError("l2Book without data")
    if data.get("coin") != coin:
        return None
    levels = data.get("levels")
    if not isinstance(levels, list) or len(levels) != 2:
        raise ParseError("levels must be [bids, asks]")
    bids = _side(levels[0], "bids", descending=True)
    asks = _side(levels[1], "asks", descending=False)
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
