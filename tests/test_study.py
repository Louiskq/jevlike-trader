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
