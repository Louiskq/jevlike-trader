import pytest

from jevhl.calibration import (
    baseline_brier_insample,
    baseline_brier_running,
    brier,
    label,
    reliability_bins,
    summary,
)


def test_label_up_down_tie():
    assert label(100.0, 100.5) == 1
    assert label(100.0, 99.5) == 0
    assert label(100.0, 100.0) is None


def test_brier_known_value():
    assert brier([0.9, 0.2], [1, 0]) == pytest.approx((0.01 + 0.04) / 2)
    assert brier([0.5, 0.5], [1, 0]) == pytest.approx(0.25)
    assert brier([1.0, 0.0], [1, 0]) == 0.0
    assert brier([0.0], [1]) == 1.0


def test_brier_empty_is_none():
    assert brier([], []) is None


def test_brier_length_mismatch():
    with pytest.raises(ValueError):
        brier([0.5], [1, 0])


def test_baseline_insample_is_b_times_one_minus_b():
    ys = [1, 1, 1, 0]
    assert baseline_brier_insample(ys) == pytest.approx(0.75 * 0.25)
    assert baseline_brier_insample([1, 1]) == 0.0
    assert baseline_brier_insample([]) is None


def test_baseline_insample_equals_brier_of_mean_predictor():
    ys = [1, 0, 0, 1, 1, 0, 1]
    b = sum(ys) / len(ys)
    assert baseline_brier_insample(ys) == pytest.approx(brier([b] * len(ys), ys))


def test_baseline_running_uses_only_prior_labels():
    # predictions: 0.5 (no history), 1.0, 1.0, 2/3
    ys = [1, 1, 0, 1]
    expected = ((0.5 - 1) ** 2 + 0 + 1 + (2 / 3 - 1) ** 2) / 4
    assert baseline_brier_running(ys) == pytest.approx(expected)
    assert baseline_brier_running([]) is None
    assert baseline_brier_running([1]) == pytest.approx(0.25)


def test_running_baseline_not_affected_by_future_labels():
    # The running baseline of a prefix is the mean of the first k per-sample terms,
    # so appending labels changes the average only through the new terms.
    ys = [1, 0, 1]
    terms = [(0.5 - 1) ** 2, (1.0 - 0) ** 2, (0.5 - 1) ** 2]
    assert baseline_brier_running(ys) == pytest.approx(sum(terms) / 3)
    ys2 = ys + [0]  # new term uses mean of ys (2/3)
    assert baseline_brier_running(ys2) == pytest.approx((sum(terms) + (2 / 3) ** 2) / 4)


def test_bins_ten_equal_width_and_empty_included():
    bins = reliability_bins([0.05, 0.95], [0, 1])
    assert len(bins) == 10
    assert bins[0].lo == 0.0 and bins[0].hi == pytest.approx(0.1)
    assert bins[0].count == 1 and bins[0].mean_p == 0.05 and bins[0].freq == 0.0
    assert bins[9].count == 1 and bins[9].freq == 1.0
    assert bins[4].count == 0 and bins[4].mean_p is None and bins[4].freq is None


def test_bin_edges_and_p_one_in_last_bin():
    bins = reliability_bins([0.0, 0.1, 0.5, 1.0], [0, 0, 1, 1])
    assert bins[0].count == 1  # 0.0
    assert bins[1].count == 1  # 0.1 belongs to [0.1, 0.2)
    assert bins[5].count == 1  # 0.5
    assert bins[9].count == 1  # 1.0 included in last bin
    assert sum(b.count for b in bins) == 4


def test_bin_means():
    bins = reliability_bins([0.61, 0.69, 0.65], [1, 0, 1])
    b = bins[6]
    assert b.count == 3
    assert b.mean_p == pytest.approx((0.61 + 0.69 + 0.65) / 3)
    assert b.freq == pytest.approx(2 / 3)


def test_bins_custom_n():
    assert len(reliability_bins([0.3], [1], n=4)) == 4
    assert reliability_bins([0.3], [1], n=4)[1].count == 1


def test_summary():
    s = summary([0.9, 0.2, 0.7], [1, 0, 1])
    assert s["n"] == 3
    assert s["brier"] == pytest.approx((0.01 + 0.04 + 0.09) / 3)
    assert s["baseline_insample"] == pytest.approx((2 / 3) * (1 / 3))
    assert s["baseline_running"] is not None
    e = summary([], [])
    assert e["n"] == 0 and e["brier"] is None and e["baseline_running"] is None
