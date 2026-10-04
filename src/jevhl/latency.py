"""Latency percentiles computed from the bot's own SQLite logs."""

from __future__ import annotations

from collections.abc import Sequence


def percentile(values: Sequence[float], q: float) -> float | None:
    """q in [0, 100], linear interpolation between order statistics."""
    if not values:
        return None
    xs = sorted(values)
    pos = (len(xs) - 1) * q / 100
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def latency_report(store) -> dict:
    """Model latency percentiles (ms), the late-hold count and the gap between consecutive
    decisions (s), all from the decisions table."""
    lats = store.latencies()
    rows = store.decisions()
    ts = [r["ts"] for r in rows]
    gaps = [b - a for a, b in zip(ts, ts[1:], strict=False)]
    return {
        "decisions": len(rows),
        "n": len(lats),
        "p50": percentile(lats, 50),
        "p90": percentile(lats, 90),
        "p99": percentile(lats, 99),
        "max": max(lats) if lats else None,
        "late_holds": sum(1 for r in rows if r["reason"] == "late"),
        "gap_p50_s": percentile(gaps, 50),
        "gap_p90_s": percentile(gaps, 90),
    }
