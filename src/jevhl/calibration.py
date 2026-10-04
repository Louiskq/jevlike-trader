"""Calibration maths: labels, Brier score, baselines, reliability bins."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from math import isfinite


def label(mid_t: float, mid_future: float) -> int | None:
    """1 if the mid rose, 0 if it fell, None on a tie (ties are dropped)."""
    if mid_future > mid_t:
        return 1
    if mid_future < mid_t:
        return 0
    return None


def _check_probs(ps: Sequence[float]) -> None:
    for p in ps:
        if not (isfinite(p) and 0.0 <= p <= 1.0):
            raise ValueError(f"probability outside [0, 1]: {p!r}")


def brier(ps: Sequence[float], ys: Sequence[int]) -> float | None:
    if len(ps) != len(ys):
        raise ValueError("ps and ys must have the same length")
    _check_probs(ps)
    if not ps:
        return None
    return sum((p - y) ** 2 for p, y in zip(ps, ys, strict=True)) / len(ps)


def baseline_brier_insample(ys: Sequence[int]) -> float | None:
    """Brier of the constant predictor b = mean(ys), which is b * (1 - b).
    Uses all labels, so it is slightly optimistic (it has seen the future)."""
    if not ys:
        return None
    b = sum(ys) / len(ys)
    return b * (1 - b)


def baseline_brier_running(ys: Sequence[int]) -> float | None:
    """Brier of a predictor that, at each step, outputs the mean of the labels before it
    (0.5 for the first sample). No lookahead: this is the fair baseline to beat."""
    if not ys:
        return None
    total = 0.0
    wins = 0
    for i, y in enumerate(ys):
        p = wins / i if i else 0.5
        total += (p - y) ** 2
        wins += y
    return total / len(ys)


@dataclass(frozen=True)
class Bin:
    lo: float
    hi: float
    count: int
    mean_p: float | None
    freq: float | None


def reliability_bins(ps: Sequence[float], ys: Sequence[int], n: int = 10) -> list[Bin]:
    """n equal-width bins over [0, 1]; p = 1.0 falls in the last bin; empty bins kept."""
    if len(ps) != len(ys):
        raise ValueError("ps and ys must have the same length")
    _check_probs(ps)
    members: list[list[tuple[float, int]]] = [[] for _ in range(n)]
    for p, y in zip(ps, ys, strict=True):
        members[min(int(p * n), n - 1)].append((p, y))
    out = []
    for i, m in enumerate(members):
        if m:
            out.append(
                Bin(i / n, (i + 1) / n, len(m), sum(p for p, _ in m) / len(m),
                    sum(y for _, y in m) / len(m))
            )
        else:
            out.append(Bin(i / n, (i + 1) / n, 0, None, None))
    return out


def summary(ps: Sequence[float], ys: Sequence[int]) -> dict:
    """Headline numbers: n, Brier, running (headline) and in-sample baselines."""
    return {
        "n": len(ps),
        "brier": brier(ps, ys),
        "baseline_running": baseline_brier_running(ys),
        "baseline_insample": baseline_brier_insample(ys),
    }
