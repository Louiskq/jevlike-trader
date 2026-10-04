import pytest

from jevhl.latency import latency_report, percentile
from jevhl.store import Store
from jevhl.types import Action, Decision


def test_percentile_linear_interpolation():
    xs = [10.0, 20.0, 30.0, 40.0]
    assert percentile(xs, 0) == 10.0
    assert percentile(xs, 100) == 40.0
    assert percentile(xs, 50) == pytest.approx(25.0)
    assert percentile(xs, 90) == pytest.approx(37.0)
    assert percentile([7.0], 99) == 7.0
    assert percentile([], 50) is None


def test_percentile_unsorted_input():
    assert percentile([30.0, 10.0, 20.0], 50) == 20.0


def test_latency_report_from_store_logs():
    s = Store(":memory:")
    for i, (lat, reason) in enumerate([(10.0, "signal"), (20.0, "below_gate"), (3000.0, "late")]):
        s.insert_decision(
            Decision(100.0 + 5.3 * i, 0.5, Action.HOLD, reason, 0.0, 0.6, lat), mid=1.0
        )
    s.insert_decision(Decision(200.0, None, Action.HOLD, "stale", 0.0, 0.6, None), mid=1.0)
    r = latency_report(s)
    assert r["n"] == 3  # the stale hold made no model call
    assert r["p50"] == 20.0
    assert r["p90"] == pytest.approx(2404.0)
    assert r["max"] == 3000.0
    assert r["late_holds"] == 1
    assert r["decisions"] == 4
    assert r["gap_p50_s"] == pytest.approx(5.3)


def test_latency_report_empty():
    r = latency_report(Store(":memory:"))
    assert r["n"] == 0 and r["p50"] is None and r["gap_p50_s"] is None
