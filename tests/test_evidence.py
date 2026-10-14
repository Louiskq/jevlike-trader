"""Statistics behind results/ (scripts/evidence.py), checked on small synthetic data where the
answer is known by hand."""

import random

import pytest

import evidence as ev
from jevhl.calibration import baseline_brier_running, brier

# ---- running baseline and per-decision differences ----


def test_running_baseline_predictions_are_the_mean_of_earlier_labels():
    assert ev.running_baseline_probs([1, 0, 1, 1]) == [0.5, 1.0, 0.5, 2 / 3]


def test_brier_diffs_average_to_brier_minus_the_dashboard_baseline():
    rng = random.Random(1)
    ps = [rng.random() for _ in range(200)]
    ys = [rng.randint(0, 1) for _ in range(200)]
    d = ev.brier_diffs(ps, ys)
    assert sum(d) / len(d) == pytest.approx(brier(ps, ys) - baseline_brier_running(ys))


# ---- block length and block bootstrap ----


def test_block_len_is_the_horizon_in_decisions():
    ts = [5.0 * i for i in range(100)]
    assert ev.block_len(ts, 60) == 12
    assert ev.block_len(ts, 30) == 6
    assert ev.block_len(ts, 7) == 2  # rounded up: a block must cover the whole horizon


def test_block_len_is_at_least_one_and_at_most_n():
    assert ev.block_len([0.0], 60) == 1
    assert ev.block_len([0.0, 1.0, 2.0], 300) == 3
    assert ev.block_len([0.0, 10.0, 20.0], 1) == 1


def test_block_resample_is_contiguous_blocks_of_length_n():
    rng = random.Random(0)
    idx = ev.block_resample(23, 5, rng)
    assert len(idx) == 23
    assert all(0 <= i < 23 for i in idx)
    for start in range(0, 20, 5):  # every full block is a run of consecutive indices
        block = idx[start:start + 5]
        assert block == list(range(block[0], block[0] + 5))


def test_block_resample_with_one_block_is_the_original_order():
    assert ev.block_resample(7, 7, random.Random(3)) == list(range(7))


def test_bootstrap_of_a_constant_series_has_zero_width():
    lo, hi = ev.block_bootstrap(ev.mean, [[0.25] * 50], [5], n_boot=200, seed=0)
    assert lo == pytest.approx(0.25) and hi == pytest.approx(0.25)


def test_bootstrap_interval_contains_the_mean_of_iid_noise():
    rng = random.Random(2)
    xs = [rng.gauss(1.0, 1.0) for _ in range(400)]
    lo, hi = ev.block_bootstrap(ev.mean, [xs], [1], n_boot=500, seed=0)
    assert lo < ev.mean(xs) < hi
    assert hi - lo < 0.4  # about 4 standard errors (sd 1, n 400)


def test_block_bootstrap_widens_the_interval_for_autocorrelated_data():
    # Runs of 20 identical values: only 20 independent draws, not 400. Blocks of 1 pretend
    # there are 400 and give a far too narrow interval; blocks of 20 do not.
    rng = random.Random(4)
    xs = [v for _ in range(20) for v in [rng.gauss(0, 1)] * 20]
    lo1, hi1 = ev.block_bootstrap(ev.mean, [xs], [1], n_boot=500, seed=0)
    lo20, hi20 = ev.block_bootstrap(ev.mean, [xs], [20], n_boot=500, seed=0)
    assert (hi20 - lo20) > 2 * (hi1 - lo1)


def test_block_bootstrap_resamples_each_group_on_its_own():
    # Two groups with different constant values: every resample keeps 10 of each, so the
    # mean of the pooled resample is always exactly 0.5.
    lo, hi = ev.block_bootstrap(ev.mean, [[0.0] * 10, [1.0] * 10], [3, 2], n_boot=100, seed=0)
    assert lo == pytest.approx(0.5) and hi == pytest.approx(0.5)


def test_block_bootstrap_is_deterministic_for_a_seed():
    xs = [float(i % 7) for i in range(100)]
    a = ev.block_bootstrap(ev.mean, [xs], [5], n_boot=100, seed=11)
    b = ev.block_bootstrap(ev.mean, [xs], [5], n_boot=100, seed=11)
    assert a == b


# ---- permutation test ----


def test_permutation_of_a_constant_forecast_gives_p_value_one():
    p, null_mean = ev.permutation_pvalue([0.6] * 30, [1, 0] * 15, n_perm=100, seed=0)
    assert p == 1.0
    assert null_mean == pytest.approx(brier([0.6] * 30, [1, 0] * 15))


def test_permutation_of_a_perfect_forecast_gives_the_smallest_p_value():
    ys = [1, 0, 0, 1, 1, 0, 1, 0, 0, 1] * 5
    ps = [0.9 if y else 0.1 for y in ys]
    p, _ = ev.permutation_pvalue(ps, ys, n_perm=200, seed=0)
    assert p == pytest.approx(1 / 201)


def test_permutation_of_an_unrelated_forecast_is_not_significant():
    rng = random.Random(5)
    ps = [rng.random() for _ in range(300)]
    ys = [rng.randint(0, 1) for _ in range(300)]
    p, _ = ev.permutation_pvalue(ps, ys, n_perm=500, seed=0)
    assert p > 0.05


# ---- momentum baseline ----


def test_momentum_signs_look_back_at_the_last_mid_at_least_30_s_old():
    ts = [0.0, 10.0, 20.0, 30.0, 40.0, 50.0]
    mids = [100.0, 101.0, 99.0, 101.0, 100.0, 99.0]
    # t=30 compares with t=0 (+1); t=40 with t=10 (101 -> 100: -1); t=50 with t=20 (tie: 0);
    # t=20 has nothing 30 s old (0).
    assert ev.momentum_signs(ts, mids, [20.0, 30.0, 40.0, 50.0], [99.0, 101.0, 100.0, 99.0]) == [
        0, 1, -1, 0
    ]


def test_momentum_k_is_half_when_the_sign_always_predicts_the_label():
    assert ev.fit_momentum_k([1, -1, 1, -1], [1, 0, 1, 0]) == pytest.approx(0.5)
    assert ev.fit_momentum_k([1, -1, 1, -1], [0, 1, 0, 1]) == pytest.approx(-0.5)


def test_momentum_k_is_least_squares_and_clipped():
    # mean(s * (y - 0.5)) / mean(s^2) = (0.5 + 0.5 - 0.5 + 0.5) / 4 = 0.25
    assert ev.fit_momentum_k([1, 1, 1, -1], [1, 1, 0, 0]) == pytest.approx(0.25)
    assert ev.fit_momentum_k([0, 0], [1, 0]) == 0.0  # no signal at all


def test_momentum_probs():
    assert ev.momentum_probs([1, 0, -1], 0.2) == pytest.approx([0.7, 0.5, 0.3])


# ---- isotonic recalibration ----


def test_isotonic_fit_pools_adjacent_violators():
    model = ev.isotonic_fit([0.1, 0.2, 0.3, 0.4], [1, 0, 1, 1])
    assert ev.isotonic_predict(model, [0.1, 0.2, 0.3, 0.4]) == pytest.approx([0.5, 0.5, 1, 1])


def test_isotonic_fit_sorts_by_probability_first():
    model = ev.isotonic_fit([0.4, 0.1, 0.3, 0.2], [1, 1, 1, 0])
    assert ev.isotonic_predict(model, [0.1, 0.2, 0.3, 0.4]) == pytest.approx([0.5, 0.5, 1, 1])


def test_isotonic_predict_is_a_step_function_between_and_beyond_fitted_points():
    model = ev.isotonic_fit([0.2, 0.4, 0.6, 0.8], [0, 0, 1, 1])
    assert ev.isotonic_predict(model, [0.0, 0.3, 0.5, 0.7, 0.99]) == pytest.approx(
        [0.0, 0.0, 0.0, 1.0, 1.0]
    )


def test_isotonic_ties_in_probability_share_one_value():
    model = ev.isotonic_fit([0.5, 0.5, 0.5, 0.5], [1, 0, 0, 0])
    assert ev.isotonic_predict(model, [0.5]) == pytest.approx([0.25])


# ---- AUC ----


def test_auc_perfect_reversed_and_tied():
    assert ev.auc([0.1, 0.2, 0.8, 0.9], [0, 0, 1, 1]) == 1.0
    assert ev.auc([0.9, 0.8, 0.2, 0.1], [0, 0, 1, 1]) == 0.0
    assert ev.auc([0.5] * 4, [0, 1, 0, 1]) == 0.5


def test_auc_counts_pairs_and_half_ties():
    # positives 0.6, 0.4; negatives 0.5, 0.4: pairs (0.6>0.5) (0.6>0.4) (0.4<0.5) (0.4=0.4)
    assert ev.auc([0.6, 0.4, 0.5, 0.4], [1, 1, 0, 0]) == pytest.approx(2.5 / 4)


def test_auc_is_undefined_with_one_class():
    assert ev.auc([0.3, 0.7], [1, 1]) is None


# ---- gate sweep ----


def test_gate_sweep_counts_brier_and_hit_rate_of_would_be_trades():
    ps = [0.70, 0.62, 0.50, 0.30, 0.41, 0.66]
    ys = [1, 0, 1, 0, 0, 1]
    rows = {r["gate"]: r for r in ev.gate_sweep(ps, ys, [0.55, 0.65])}
    g = rows[0.55]  # trades: 0.70 buy hit, 0.62 buy miss, 0.30 sell hit, 0.41 sell hit, 0.66 hit
    assert (g["n"], g["n_buy"], g["n_sell"]) == (5, 3, 2)
    assert g["hit_rate"] == pytest.approx(4 / 5)
    assert g["up_rate"] == pytest.approx(2 / 5)
    assert g["brier"] == pytest.approx(brier([0.70, 0.62, 0.30, 0.41, 0.66], [1, 0, 0, 0, 1]))
    h = rows[0.65]  # 0.70 buy hit, 0.30 sell hit, 0.66 buy hit
    assert (h["n"], h["n_buy"], h["n_sell"], h["hit_rate"]) == (3, 2, 1, 1.0)


def test_gate_sweep_with_no_trades():
    (r,) = ev.gate_sweep([0.5, 0.52], [1, 0], [0.70])
    assert r["n"] == 0 and r["brier"] is None and r["hit_rate"] is None


def test_gate_boundary_is_inclusive_for_non_float_exact_values():
    # The bot trades at p >= gate and p <= 1 - gate; 1 - 0.67 is not exactly 0.33 in floats.
    (r,) = ev.gate_sweep([0.67, 0.33], [1, 0], [0.67])
    assert r["n"] == 2


# ---- reading an interval ----


def test_verdict_reads_the_interval_against_zero():
    assert ev.verdict(-0.03, -0.01, 50) == "better than baseline"
    assert ev.verdict(0.01, 0.03, 50) == "worse than baseline"
    assert ev.verdict(-0.01, 0.03, 50) == "no difference shown"


def test_verdict_refuses_to_judge_with_fewer_than_ten_blocks():
    # 225 decisions in one 225-decision block: the bootstrap interval has zero width and
    # would look decisive.
    assert ev.verdict(0.19, 0.19, 1.0) == "too few samples"
    assert ev.verdict(0.01, 0.03, 9.9) == "too few samples"
    assert ev.verdict(None, None, 50) == "too few samples"
