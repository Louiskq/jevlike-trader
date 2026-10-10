"""The maker strategy (STRATEGY=maker, milestone 8).

This module starts with the model input: the book the model sees (our own resting order
removed, see without_own) plus features from the mid history and the trade tape. What is
shown, and with which settings, is the user's draft in prompt.py.
"""

from __future__ import annotations

from .feed import Book, TradeTape
from .prompt import (
    DEPTH_BANDS_BPS,
    FLOW_WINDOW_S,
    IMBALANCE_BAND_PCT,
    MID_SAMPLE_S,
    MID_SPAN_S,
    N_PRINTS,
    RETURN_WINDOWS_S,
)
from .types import MakerInput


def _mid_at(mids: list[tuple[float, float]], t: float) -> float | None:
    """The last mid at or before t (mids are (ts, mid), oldest first), None if none."""
    out = None
    for ts, m in mids:
        if ts > t:
            break
        out = m
    return out


def _size_within(levels: list[list[float]], mid: float, frac: float) -> float:
    return sum(sz for px, sz in levels if abs(px - mid) <= mid * frac)


def build_input(book: Book, mids: list[tuple[float, float]], tape: TradeTape,
                recent: list[float], horizon_s: float) -> MakerInput:
    """mids: (recv_ts, mid) of every book so far, oldest first, ending with this one. Returns
    and sampled mids are taken by time, not by count, since most seconds repeat a book."""
    now, mid = book.recv_ts, book.mid
    band = IMBALANCE_BAND_PCT / 100
    b, a = _size_within(book.bids, mid, band), _size_within(book.asks, mid, band)
    returns = {}
    for w in RETURN_WINDOWS_S:
        past = _mid_at(mids, now - w)
        returns[w] = None if past is None else (mid / past - 1) * 1e4
    steps = int(MID_SPAN_S // MID_SAMPLE_S)
    return MakerInput(
        ts=now, coin=book.coin, mid=mid, spread_bps=book.spread_bps,
        bids=book.bids, asks=book.asks, recent_mids=list(recent), horizon_s=horizon_s,
        imbalance=(b - a) / (b + a) if b + a > 0 else 0.0,
        depth={bps: (_size_within(book.bids, mid, bps / 1e4),
                     _size_within(book.asks, mid, bps / 1e4)) for bps in DEPTH_BANDS_BPS},
        returns_bps=returns,
        sampled_mids=[_mid_at(mids, now - k * MID_SAMPLE_S) for k in range(steps, -1, -1)],
        flow=tape.flow(FLOW_WINDOW_S, now),
        prints=[(now - p.ts, p.side, p.px, p.sz) for p in tape.recent(N_PRINTS)],
    )
