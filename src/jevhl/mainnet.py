"""Mainnet prices for the maker prompt's mainnet block (milestone 10, MAKER_PROMPT=mainnet).

The fine-tuned decider was trained with mainnet BTC prices in its prompt, so the maker needs
them live. This is the only module in the bot with a mainnet URL, and it is read-only: one
unauthenticated `l2Book` request for BTC to the public info endpoint about once a second, no
key, no signing. It imports no exchange or config code and takes no settings, so it cannot
see a key (tests/test_mainnet.py enforces all of this). It also computes the block's numbers
the way the fine-tune's data did (study/finetune_data.py uses mainnet_input too).
"""

from __future__ import annotations

import asyncio
import bisect
import json
import math
import time
import urllib.request
from collections.abc import AsyncIterator, Callable

from .feed import MAX_REST_BYTES, REST_TIMEOUT_S, Book, _reject_constant, poll_books
from .prompt import MAINNET_MAX_AGE_S, MAINNET_WINDOWS_S
from .types import MainnetInput

MAINNET_INFO_URL = "https://api.hyperliquid.xyz/info"
MAINNET_COIN = "BTC"
# Mids kept, by receive time. A decision's 60 s lookup with its 5 s freshness needs 65 s; the
# rest covers a decision that runs a few seconds after the latest mainnet read.
KEEP_S = 120.0

# Hold this long after the run's first book. The fine-tune's data skipped each recording's
# first 310 s (study/finetune_data.py WARMUP_S), so the model never saw a prompt without 5
# minutes of history (user decision 2026-10-26).
WARMUP_S = 310.0


def fetch_mainnet_l2book(coin: str) -> object:
    """One public l2Book read of mainnet BTC (blocking; the poller runs it in a thread)."""
    if coin != MAINNET_COIN:
        raise ValueError(f"the mainnet reader reads {MAINNET_COIN} only, not {coin!r}")
    req = urllib.request.Request(
        MAINNET_INFO_URL, data=json.dumps({"type": "l2Book", "coin": MAINNET_COIN}).encode(),
        headers={"content-type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=REST_TIMEOUT_S) as resp:  # noqa: S310 (fixed URL)
        return json.loads(resp.read(MAX_REST_BYTES), parse_constant=_reject_constant)


def poll_mainnet(
    clock: Callable[[], float],
    *,
    on_event: Callable[[str, str], None] = lambda kind, detail: None,
    fetch: Callable[[str], object] = fetch_mainnet_l2book,
    sleep: Callable = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> AsyncIterator[Book]:
    """Mainnet BTC books about once a second, through feed.poll_books (its parsing, finite
    and crossed-book checks, and one read per second). Events are named mainnet_*. An HTTP
    429 is only logged: it never pauses the testnet quoting (mainnet's limits are its own);
    without fresh books the maker holds with reason no_mainnet."""
    return poll_books(MAINNET_COIN, clock, fetch=fetch, sleep=sleep, monotonic=monotonic,
                      on_event=lambda kind, detail: on_event(f"mainnet_{kind}", detail))


class MainnetHistory:
    """Mainnet mids by local receive time, for mainnet_input: at(t) is the latest mid received
    at or before t and its age at t, as the fine-tune's data looked them up."""

    def __init__(self) -> None:
        self._ts: list[float] = []
        self._mids: list[float] = []

    def __len__(self) -> int:
        return len(self._ts)

    def add(self, recv_ts: float, mid: float) -> None:
        i = bisect.bisect_right(self._ts, recv_ts)
        self._ts.insert(i, recv_ts)
        self._mids.insert(i, mid)
        cut = bisect.bisect_left(self._ts, self._ts[-1] - KEEP_S)
        del self._ts[:cut], self._mids[:cut]

    def at(self, t: float) -> tuple[float | None, float]:
        i = bisect.bisect_right(self._ts, t) - 1
        return (None, float("inf")) if i < 0 else (self._mids[i], t - self._ts[i])

    def latest(self) -> tuple[float, float] | None:
        """(receive time, mid) of the newest mainnet book, None before the first."""
        return (self._ts[-1], self._mids[-1]) if self._ts else None

    def state(self, now: float) -> str:
        """For the dashboard: waiting (no book yet), fresh or stale (newest book over
        MAINNET_MAX_AGE_S old at `now`)."""
        if not self._ts:
            return "waiting"
        return "fresh" if now - self._ts[-1] <= MAINNET_MAX_AGE_S else "stale"


def mainnet_input(now: float, testnet_mid: float,
                  at: Callable[[float], tuple[float | None, float]]) -> MainnetInput | None:
    """The mainnet block's numbers at `now` (the testnet book's receive time), as the
    fine-tune's data computed them. at(t) gives the latest mainnet mid received at or before t
    and its age at t. None (no prompt can be built) when the latest mainnet mid is missing or
    over MAINNET_MAX_AGE_S old, or a value is not a finite positive number. A past mid over
    MAINNET_MAX_AGE_S older than its target time gives None for that window (shown "n/a")."""
    m_now, age = at(now)
    if m_now is None or age > MAINNET_MAX_AGE_S:
        return None
    if not (math.isfinite(m_now) and m_now > 0 and math.isfinite(testnet_mid)
            and testnet_mid > 0):
        return None
    rets: dict[int, float | None] = {}
    for w in MAINNET_WINDOWS_S:
        past, page = at(now - w)
        ok = past is not None and page <= MAINNET_MAX_AGE_S and math.isfinite(past) and past > 0
        rets[w] = (m_now / past - 1) * 1e4 if ok else None
    return MainnetInput(mid=m_now, gap_bps=(testnet_mid / m_now - 1) * 1e4, returns_bps=rets)
