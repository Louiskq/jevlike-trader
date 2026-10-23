"""Out-of-sample evaluation shared by the feasibility study scripts (.pipeline/feasibility-plan.md).

A forecast series is evaluated on a time-ordered split: fit on the first part, skip one
horizon so no outcome window crosses the split, score the rest. Intervals are a moving-block
bootstrap (blocks one horizon long, 2000 resamples, seed 20261014), the same method as
scripts/evidence.py, written with numpy because the book data run to ~10^5 decisions.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np

from evidence import MIN_BLOCKS, verdict

N_BOOT = 2000
SEED = 20261014
TRAIN_SHARE = 0.6
CONFIDENT_SHARE = 0.2  # confident calls: this share of training points with the largest |p-0.5|
COSTS_BPS = {"maker": 1.35, "taker": 9.0}


def split_with_gap(n: int, gap: int, train_share: float = TRAIN_SHARE) -> tuple[range, range]:
    """Indices of a time-ordered split: train is the first train_share of n, then `gap` points
    are skipped (one horizon in points, so no training outcome overlaps a test decision), then
    test runs to the end."""
    cut = int(n * train_share)
    return range(0, cut), range(min(n, cut + gap), n)


def confident_threshold(train_ps: Sequence[float], share: float = CONFIDENT_SHARE) -> float:
    """The |p - 0.5| above which a training point is among the `share` most confident."""
    dist = sorted((abs(p - 0.5) for p in train_ps), reverse=True)
    if not dist:
        return float("inf")
    k = max(1, round(len(dist) * share))
    return dist[k - 1]


def signed_moves(ps: Sequence[float], moves_bps: Sequence[float], threshold: float) -> list[float]:
    """For each forecast at least `threshold` away from 0.5: the realised move in the called
    direction, in bps (positive means the call was right by that much)."""
    return [(m if p > 0.5 else -m) for p, m in zip(ps, moves_bps, strict=True)
            if abs(p - 0.5) >= threshold and p != 0.5]


def running_base(ys: np.ndarray) -> np.ndarray:
    """At each step the share of earlier labels that were up (0.5 first), as in evidence.py."""
    prior = np.concatenate([[0.0], np.cumsum(ys)[:-1]])
    count = np.arange(len(ys))
    return np.where(count > 0, prior / np.maximum(count, 1), 0.5)


def auc_np(ps: np.ndarray, ys: np.ndarray) -> float | None:
    """Mann-Whitney AUC with ties counted half; None with one class only."""
    n1 = int(ys.sum())
    n0 = len(ys) - n1
    if n1 == 0 or n0 == 0:
        return None
    order = np.argsort(ps, kind="mergesort")
    sp = ps[order]
    ranks = np.empty(len(ps))
    # average ranks over ties
    _, first, counts = np.unique(sp, return_index=True, return_counts=True)
    avg = first + (counts - 1) / 2 + 1
    ranks[order] = np.repeat(avg, counts)
    return float((ranks[ys == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def block_indices(n: int, length: int, rng: np.random.Generator) -> np.ndarray:
    """One moving-block resample of range(n): blocks of `length` consecutive indices with
    uniform random starts, concatenated and cut to n."""
    length = max(1, min(length, n))
    k = math.ceil(n / length)
    starts = rng.integers(0, n - length + 1, size=k)
    return (starts[:, None] + np.arange(length)).ravel()[:n]


def bootstrap(stat: Callable[[np.ndarray], float | None], n: int, length: int
              ) -> tuple[float | None, float | None]:
    """95% percentile interval of stat(indices) over block resamples."""
    if n < 2:
        return None, None
    rng = np.random.default_rng(SEED)
    vals = [v for v in (stat(block_indices(n, length, rng)) for _ in range(N_BOOT))
            if v is not None]
    if not vals:
        return None, None
    return float(np.percentile(vals, 2.5)), float(np.percentile(vals, 97.5))


@dataclass
class Result:
    n_test: int
    n_blocks: float
    up_rate: float
    abs_move_mean: float
    abs_move_median: float
    brier_diff: float
    brier_lo: float | None
    brier_hi: float | None
    brier_verdict: str
    auc: float | None
    auc_lo: float | None
    auc_hi: float | None
    n_confident: int
    edge_mean: float | None
    edge_lo: float | None
    edge_hi: float | None
    block: int = 1

    def edge_verdict(self) -> str:
        """The plan's two criteria, read together."""
        if self.n_blocks < MIN_BLOCKS or self.n_confident < MIN_BLOCKS * min(
                self.block, max(self.n_confident, 1)):
            return "too few samples"
        learnable = (self.auc_lo is not None and self.auc_lo > 0.5
                     and self.brier_verdict == "better than baseline")
        clears = [k for k, c in COSTS_BPS.items() if self.edge_lo is not None and self.edge_lo > c]
        if learnable and clears:
            return f"edge (clears {', '.join(clears)} cost)"
        if learnable:
            return "learnable, not tradable"
        return "nothing found"


def evaluate(ps_test: Sequence[float], ys_all: Sequence[int], test: range,
             moves_test: Sequence[float], threshold: float, block: int) -> Result:
    """Score test forecasts. ys_all is every label in time order (train and test), so the
    running base rate at a test point has seen all earlier labels, as a live baseline would.
    `block` is one horizon in points, the bootstrap block length."""
    ys_all_a = np.asarray(ys_all, dtype=float)
    base = running_base(ys_all_a)[test.start:test.stop]
    ys = ys_all_a[test.start:test.stop]
    ps = np.asarray(ps_test, dtype=float)
    diffs = (ps - ys) ** 2 - (base - ys) ** 2
    n = len(ys)
    n_blocks = n / block if block else 0.0
    lo, hi = bootstrap(lambda ix: float(diffs[ix].mean()), n, block)
    alo, ahi = bootstrap(lambda ix: auc_np(ps[ix], ys[ix]), n, block)
    edge = np.asarray(signed_moves(list(ps), list(moves_test), threshold))
    # Confident calls are a subset, so one horizon holds fewer of them than `block`; using the
    # full block length in calls makes their blocks longer than a horizon (wider, cautious).
    elo, ehi = bootstrap(lambda ix: float(edge[ix].mean()), len(edge), min(block, len(edge)))
    abs_moves = np.abs(np.asarray(moves_test, dtype=float))
    return Result(
        n_test=n, n_blocks=n_blocks, up_rate=float(ys.mean()) if n else 0.0,
        abs_move_mean=float(abs_moves.mean()) if n else 0.0,
        abs_move_median=float(np.median(abs_moves)) if n else 0.0,
        brier_diff=float(diffs.mean()) if n else 0.0, brier_lo=lo, brier_hi=hi,
        brier_verdict=verdict(lo, hi, n_blocks),
        auc=auc_np(ps, ys), auc_lo=alo, auc_hi=ahi,
        n_confident=len(edge), edge_mean=float(edge.mean()) if len(edge) else None,
        edge_lo=elo, edge_hi=ehi, block=block,
    )
