import pytest

from jevhl.ledger import Ledger, day_start_pnl
from jevhl.store import Store
from jevhl.types import Action, Decision

DAY = 86400.0


def test_round_trip_pnl_before_and_after_fees():
    lg = Ledger()
    lg.apply_fill("buy", 0.002, 65000.0, 0.0585)
    assert lg.position == pytest.approx(0.002)
    lg.apply_fill("sell", 0.002, 65100.0, 0.0586)
    assert lg.position == pytest.approx(0.0)
    assert lg.pnl_before_fees(65100.0) == pytest.approx(0.2)
    assert lg.pnl(65100.0) == pytest.approx(0.2 - 0.0585 - 0.0586)
    assert lg.fees == pytest.approx(0.1171)


def test_realized_pnl_is_gross_of_fees_and_average_cost():
    lg = Ledger()
    lg.apply_fill("buy", 0.002, 65000.0, 0.05)
    lg.apply_fill("buy", 0.002, 65200.0, 0.05)  # average entry 65100
    assert lg.realized == 0.0
    lg.apply_fill("sell", 0.001, 65200.0, 0.05)
    assert lg.realized == pytest.approx(0.1)
    lg.apply_fill("sell", 0.003, 65000.0, 0.05)  # closes 0.003 and flips short 0 (exactly flat)
    assert lg.realized == pytest.approx(0.1 + 0.003 * (65000.0 - 65100.0) * 1)
    assert lg.position == pytest.approx(0.0)


def test_realized_pnl_on_a_flip_and_on_shorts():
    lg = Ledger()
    lg.apply_fill("sell", 0.001, 65000.0, 0)
    lg.apply_fill("buy", 0.003, 64900.0, 0)  # covers 0.001 at +100, opens 0.002 long at 64900
    assert lg.realized == pytest.approx(0.1)
    lg.apply_fill("sell", 0.002, 65000.0, 0)
    assert lg.realized == pytest.approx(0.1 + 0.2)


def test_short_then_unrealized():
    lg = Ledger()
    lg.apply_fill("sell", 0.001, 65000.0, 0.03)
    assert lg.position == pytest.approx(-0.001)
    assert lg.pnl_before_fees(64900.0) == pytest.approx(0.1)  # short gains as price falls
    assert lg.pnl(64900.0) == pytest.approx(0.07)


def test_flat_ledger_pnl_zero():
    assert Ledger().pnl(1.0) == 0.0


def test_rejects_bad_fill():
    lg = Ledger()
    for sd, sz, px in [("hold", 1, 1), ("buy", float("nan"), 1), ("buy", 1, float("inf")),
                       ("buy", -1, 1)]:
        with pytest.raises(ValueError):
            lg.apply_fill(sd, sz, px, 0.0)


def _fill(store, tid, ts, side, sz, px, fee):
    store.insert_fill(tid=tid, oid=tid, ts=ts, coin="BTC", side=side, sz=sz, px=px, fee=fee)


def test_from_store_matches_incremental():
    s = Store(":memory:")
    _fill(s, 1, 10.0, "buy", 0.002, 65000.0, 0.05)
    _fill(s, 2, 70.0, "sell", 0.001, 65200.0, 0.03)
    lg = Ledger.from_fills(s.fills())
    inc = Ledger()
    inc.apply_fill("buy", 0.002, 65000.0, 0.05)
    inc.apply_fill("sell", 0.001, 65200.0, 0.03)
    assert lg.position == pytest.approx(inc.position) and lg.pnl(65100.0) == inc.pnl(65100.0)


def test_day_start_pnl_empty_store_is_zero():
    assert day_start_pnl(Store(":memory:"), 10 * DAY + 500) == 0.0


def test_day_start_pnl_uses_only_fills_before_midnight_marked_at_last_mid():
    s = Store(":memory:")
    midnight = 10 * DAY
    _fill(s, 1, midnight - 100, "buy", 0.002, 65000.0, 0.05)
    s.insert_decision(Decision(midnight - 10, 0.5, Action.HOLD, "below_gate", 0, 0.6, 1.0),
                      mid=64900.0)
    # after midnight: must not count
    _fill(s, 2, midnight + 50, "sell", 0.002, 60000.0, 0.05)
    s.insert_decision(Decision(midnight + 60, 0.5, Action.HOLD, "below_gate", 0, 0.6, 1.0),
                      mid=1.0)
    expected = -0.002 * 65000.0 + 0.002 * 64900.0 - 0.05
    assert day_start_pnl(s, midnight + 3600) == pytest.approx(expected)


def test_day_start_pnl_falls_back_to_last_fill_price_without_decisions():
    s = Store(":memory:")
    midnight = 3 * DAY
    _fill(s, 1, midnight - 100, "buy", 0.002, 65000.0, 0.05)
    assert day_start_pnl(s, midnight + 1) == pytest.approx(-0.05)  # marked at 65000


def test_day_start_pnl_boundary_is_utc_midnight_not_now():
    s = Store(":memory:")
    midnight = 3 * DAY
    _fill(s, 1, midnight - 1, "buy", 0.001, 100.0, 0.0)  # position, marked at its own px
    _fill(s, 2, midnight + 0.5, "sell", 0.001, 90.0, 0.0)  # realises a loss, today
    assert day_start_pnl(s, midnight + DAY - 1) == pytest.approx(0.0)


def test_float_residue_snaps_to_exactly_flat():
    lg = Ledger()
    for side, sz in [("buy", 0.00384), ("buy", 0.00385), ("sell", 0.00384), ("sell", 0.00385)]:
        lg.apply_fill(side, sz, 65000.0, 0.0)
    assert lg.position == 0.0
