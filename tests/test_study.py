"""Feasibility study helpers (study/evaluate.py, study/candles.py)."""

import random

import pytest
from candles import features_and_labels
from evaluate import confident_threshold, evaluate, signed_moves, split_with_gap


def test_split_skips_one_horizon_between_train_and_test():
    train, test = split_with_gap(100, gap=5)
    assert list(train) == list(range(60))
    assert test.start == 65 and test.stop == 100
    assert train.stop + 5 == test.start


def test_split_gap_larger_than_rest_leaves_empty_test():
    _, test = split_with_gap(10, gap=50)
    assert len(test) == 0


def test_confident_threshold_takes_the_top_share():
    ps = [0.5, 0.52, 0.45, 0.6, 0.3, 0.51, 0.49, 0.7, 0.5, 0.55]  # |p-0.5|: .2 .2 .1 .05 ...
    assert confident_threshold(ps, share=0.2) == pytest.approx(0.2)
    assert confident_threshold(ps, share=0.3) == pytest.approx(0.1)


def test_signed_moves_score_the_called_direction():
    ps = [0.8, 0.2, 0.55, 0.5]
    moves = [3.0, 3.0, -1.0, 9.0]
    assert signed_moves(ps, moves, threshold=0.1) == [3.0, -3.0]
    assert signed_moves(ps, moves, threshold=0.0) == [3.0, -3.0, -1.0]  # p == 0.5 is no call


def test_features_use_only_the_past_and_labels_only_the_future():
    closes = [100.0 + i for i in range(200)]
    ts = [60.0 * i for i in range(200)]
    rows = features_and_labels(ts, closes, horizon_bars=5, lags=(1, 5), vol_bars=10)
    first = rows[0]
    i = first["i"]
    assert i >= 10  # needs vol_bars of history
    assert first["x"][0] == pytest.approx((closes[i] / closes[i - 1] - 1) * 1e4)
    assert first["move_bps"] == pytest.approx((closes[i + 5] / closes[i] - 1) * 1e4)
    assert rows[-1]["i"] + 5 <= 199  # every label has its future close


def test_features_change_if_a_past_close_changes_never_a_future_one():
    rng = random.Random(1)
    closes = [100 + rng.gauss(0, 1) for _ in range(120)]
    ts = [60.0 * i for i in range(120)]
    base = features_and_labels(ts, closes, horizon_bars=3, lags=(1, 5), vol_bars=10)
    k = 70
    bumped = closes[:]
    bumped[k + 3] += 5  # the close one horizon after decision k
    after = features_and_labels(ts, bumped, horizon_bars=3, lags=(1, 5), vol_bars=10)
    row = next(r for r in base if r["i"] == k)
    row2 = next(r for r in after if r["i"] == k)
    assert row["x"] == row2["x"]  # the features at k do not see a later close
    assert row["move_bps"] != row2["move_bps"]  # the label does


def test_evaluate_finds_a_planted_edge_and_not_a_fake_one():
    rng = random.Random(7)
    n = 3000
    moves = [rng.gauss(0, 5) for _ in range(n)]
    ys = [1 if m > 0 else 0 for m in moves]
    _, test = split_with_gap(n, gap=1)
    good = [0.5 + (0.3 if moves[i] > 0 else -0.3) * (1 if rng.random() < 0.8 else -1)
            for i in test]
    r = evaluate(good, ys, test, [moves[i] for i in test], threshold=0.1, block=1)
    assert r.auc_lo > 0.5 and r.brier_verdict == "better than baseline"
    assert r.edge_lo > 1.35
    noise = [rng.random() for _ in test]
    r2 = evaluate(noise, ys, test, [moves[i] for i in test], threshold=0.3, block=1)
    assert r2.edge_verdict() == "nothing found"


def test_numpy_auc_and_base_rate_match_the_evidence_script():
    import numpy as np
    from evaluate import auc_np, running_base

    from evidence import auc, running_baseline_probs
    rng = random.Random(3)
    ps = [round(rng.random(), 1) for _ in range(500)]  # rounded, so many ties
    ys = [int(rng.random() < 0.3 + 0.4 * p) for p in ps]
    assert auc_np(np.array(ps), np.array(ys)) == pytest.approx(auc(ps, ys))
    assert list(running_base(np.array(ys, dtype=float))) == pytest.approx(
        running_baseline_probs(ys))
    assert auc_np(np.array([0.2, 0.8]), np.array([1, 1])) is None


def test_block_indices_are_runs_of_consecutive_points():
    import numpy as np
    from evaluate import block_indices
    ix = block_indices(103, 10, np.random.default_rng(0))
    assert len(ix) == 103 and ix.min() >= 0 and ix.max() <= 102
    assert all(ix[k + 1] == ix[k] + 1 for k in range(9))  # first block is consecutive


def _venue(n=2000, seed=0):
    import numpy as np
    from books import Venue
    rng = np.random.default_rng(seed)
    t = np.arange(n, dtype=float)
    mid = 80000 + np.cumsum(rng.normal(0, 2, n))
    tr_t = np.sort(rng.uniform(0, n, 4 * n))
    sz = rng.uniform(0.01, 1, len(tr_t))
    return Venue(t=t, mid=mid, spread_bps=np.full(n, 0.12), imb=rng.uniform(-1, 1, (n, 3)),
                 depth=rng.uniform(0, 3, (n, 6)), tr_t=tr_t,
                 tr_signed=sz * rng.choice([-1, 1], len(sz)), tr_size=sz,
                 hours=n / 3600, n_trades=len(sz), gaps=0)


def test_book_features_never_see_the_future():
    from books import build
    v = _venue()
    base = build(v, 60, stride=5, main=_venue(seed=1))
    k = len(base["t"]) // 2
    t_k = base["t"][k]
    v2 = _venue()
    after = v2.t > t_k
    v2.mid[after] += 500  # every later book changes
    v2.tr_signed[v2.tr_t > t_k] *= -1  # and every later trade
    changed = build(v2, 60, stride=5, main=_venue(seed=1))
    j = list(changed["t"]).index(t_k)
    assert (changed["x"][j] == base["x"][k]).all()
    assert changed["move"][j] != base["move"][k]


def test_label_is_dropped_when_the_future_book_is_stale():
    import numpy as np
    from books import build
    v = _venue()
    keep = (v.t < 1000) | (v.t > 1100)  # a 100 s hole in the books
    v.t, v.mid = v.t[keep], v.mid[keep]
    v.spread_bps, v.imb, v.depth = v.spread_bps[keep], v.imb[keep], v.depth[keep]
    d = build(v, 60, stride=1)
    target = d["t"] + 60
    assert not np.any((target > 1005) & (target <= 1100))
