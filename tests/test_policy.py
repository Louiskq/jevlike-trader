import math

import pytest

from jevhl.config import Config
from jevhl.policy import decide, hold
from jevhl.types import Action, Decision

CFG = Config.from_env({})  # gate .6, cap 500, min 10, full .8


def d(p, pos=0.0, latency_ms=10.0, cfg=CFG) -> Decision:
    return decide(p, pos, cfg, ts=1.0, latency_ms=latency_ms)


def test_buy_at_gate_boundary_min_size():
    r = d(0.60)
    assert r.action is Action.BUY and r.reason == "signal"
    assert r.size_usd == pytest.approx(10)


def test_sell_at_gate_boundary_min_size():
    r = d(0.40)
    assert r.action is Action.SELL and r.reason == "signal"
    assert r.size_usd == pytest.approx(10)


@pytest.mark.parametrize("p", [0.5, 0.5999, 0.4001, 0.41, 0.59])
def test_abstain_between_thresholds(p):
    r = d(p)
    assert r.action is Action.HOLD and r.reason == "below_gate"
    assert r.size_usd == 0


def test_sizing_linear_and_capped():
    assert d(0.70).size_usd == pytest.approx(10 + 0.5 * 490)
    assert d(0.80).size_usd == pytest.approx(500)
    assert d(0.95).size_usd == pytest.approx(500)
    assert d(1.0).size_usd == pytest.approx(500)
    # symmetric for sells
    assert d(0.30).size_usd == pytest.approx(10 + 0.5 * 490)
    assert d(0.0).size_usd == pytest.approx(500)


def test_sizing_monotonic():
    sizes = [d(p).size_usd for p in (0.60, 0.65, 0.70, 0.75, 0.80)]
    assert sizes == sorted(sizes) and len(set(sizes)) == 5


def test_opposite_side_blocked_while_position_open():
    r = d(0.9, pos=100)
    assert r.action is Action.BUY  # same side allowed
    r = d(0.1, pos=100)
    assert r.action is Action.HOLD and r.reason == "blocked"
    r = d(0.9, pos=-100)
    assert r.action is Action.HOLD and r.reason == "blocked"
    r = d(0.1, pos=-100)
    assert r.action is Action.SELL


def test_blocked_only_when_signal_present():
    assert d(0.5, pos=100).reason == "below_gate"


def test_same_side_add_limited_by_room():
    r = d(0.80, pos=300)
    assert r.action is Action.BUY and r.size_usd == pytest.approx(200)
    r = d(0.20, pos=-300)
    assert r.action is Action.SELL and r.size_usd == pytest.approx(200)


def test_room_below_min_notional_holds_cap_reached():
    r = d(0.80, pos=491)  # room 9 < 10
    assert r.action is Action.HOLD and r.reason == "cap_reached"
    r = d(0.80, pos=500)
    assert r.reason == "cap_reached"
    r = d(0.80, pos=490)  # room exactly 10 is enough
    assert r.action is Action.BUY and r.size_usd == pytest.approx(10)


def test_cap_reached_does_not_block_exit_side_logic():
    # opposite side is blocked, not cap_reached, even at the cap
    assert d(0.1, pos=500).reason == "blocked"


@pytest.mark.parametrize("p", [float("nan"), -0.1, 1.1, float("inf"), None])
def test_invalid_p_holds(p):
    r = d(p)
    assert r.action is Action.HOLD and r.reason == "invalid"


def test_late_holds_even_with_strong_signal():
    r = d(0.99, latency_ms=2001)
    assert r.action is Action.HOLD and r.reason == "late"
    assert r.size_usd == 0


def test_exactly_at_deadline_not_late():
    assert d(0.99, latency_ms=2000).action is Action.BUY


def test_late_with_missing_output():
    assert d(None, latency_ms=5000).reason == "late"


def test_decision_records_fields():
    r = d(0.7, latency_ms=12.5)
    assert r.ts == 1.0 and r.p_up == 0.7 and r.gate == 0.60 and r.latency_ms == 12.5


def test_hold_helper():
    r = hold("stale", ts=3.0, gate=0.6)
    assert r.action is Action.HOLD and r.reason == "stale" and r.size_usd == 0
    assert r.p_up is None
    r = hold("killed", ts=3.0, gate=0.6, p_up=0.9)
    assert r.reason == "killed" and r.p_up == 0.9


def test_hold_rejects_unknown_reason():
    with pytest.raises(ValueError):
        hold("whatever", ts=0, gate=0.6)


def test_custom_gate_and_cap():
    cfg = Config.from_env({"GATE": "0.7", "MAX_POSITION_USD": "1000"})
    assert d(0.69, cfg=cfg).action is Action.HOLD
    assert d(0.70, cfg=cfg).action is Action.BUY
    assert d(0.30, cfg=cfg).action is Action.SELL  # 1 - 0.7 float noise
    assert d(0.80, cfg=cfg).size_usd == pytest.approx(1000)
    assert not math.isnan(d(0.75, cfg=cfg).size_usd)
