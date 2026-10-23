"""Out-of-sample evaluation shared by the feasibility study scripts (.pipeline/feasibility-plan.md).

A forecast series is evaluated on a time-ordered split: fit on the first part, skip one
horizon so no outcome window crosses the split, score the rest. Intervals reuse the moving-
block bootstrap from scripts/evidence.py so the numbers read the same way as results/.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from evidence import auc_of_pairs, block_bootstrap, mean, running_baseline_probs, verdict

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

    def edge_verdict(self) -> str:
        """The plan's two criteria, read together."""
        learnable = (self.auc_lo is not None and self.auc_lo > 0.5
                     and self.brier_verdict == "better than baseline")
        if self.n_blocks < 10:
            return "too few samples"
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
    base = running_baseline_probs(ys_all)
    ys = [ys_all[i] for i in test]
    bs = [base[i] for i in test]
    diffs = [(p - y) ** 2 - (b - y) ** 2 for p, y, b in zip(ps_test, ys, bs, strict=True)]
    n = len(ys)
    n_blocks = n / block if block else 0
    lo, hi = block_bootstrap(mean, [diffs], [block], N_BOOT, SEED)
    pairs = list(zip(ps_test, ys, strict=True))
    alo, ahi = block_bootstrap(auc_of_pairs, [pairs], [block], N_BOOT, SEED)
    # Confident calls are a subset, so one horizon holds fewer of them than `block`; using the
    # full block length in calls makes their blocks longer than a horizon (wider, cautious).
    edge = signed_moves(ps_test, moves_test, threshold)
    elo = ehi = None
    if len(edge) >= 2:
        elo, ehi = block_bootstrap(mean, [edge], [max(1, min(block, len(edge)))], N_BOOT, SEED)
    abs_moves = sorted(abs(m) for m in moves_test)
    return Result(
        n_test=n, n_blocks=n_blocks, up_rate=mean(ys) or 0.0,
        abs_move_mean=mean(abs_moves) or 0.0,
        abs_move_median=abs_moves[len(abs_moves) // 2] if abs_moves else 0.0,
        brier_diff=mean(diffs) or 0.0, brier_lo=lo, brier_hi=hi,
        brier_verdict=verdict(lo, hi, n_blocks),
        auc=auc_of_pairs(pairs), auc_lo=alo, auc_hi=ahi,
        n_confident=len(edge), edge_mean=mean(edge), edge_lo=elo, edge_hi=ehi,
    )
