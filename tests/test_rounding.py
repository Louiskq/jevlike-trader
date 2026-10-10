import json
from decimal import Decimal
from pathlib import Path

import pytest

from jevhl.rounding import (
    AssetMeta,
    aggressive_price,
    maker_price,
    next_price,
    notional_ok,
    order_size,
    reduce_size,
    round_price,
    round_size,
)

META = json.loads((Path(__file__).parent / "fixtures" / "meta_btc.json").read_text())
BTC = AssetMeta.from_meta(META, "BTC")
ETH = AssetMeta.from_meta(META, "ETH")
DOGE = AssetMeta.from_meta(META, "DOGE")


def test_from_meta():
    assert BTC.sz_decimals == 5
    assert ETH.sz_decimals == 4
    with pytest.raises(KeyError):
        AssetMeta.from_meta(META, "NOPE")


def test_btc_price_five_sig_figs():
    assert round_price(65432.123, BTC, up=False) == Decimal("65432")
    assert round_price(65432.123, BTC, up=True) == Decimal("65433")


def test_eth_price_one_decimal():
    assert round_price(3456.789, ETH, up=False) == Decimal("3456.7")
    assert round_price(3456.789, ETH, up=True) == Decimal("3456.8")


def test_exact_ticks_unchanged():
    assert round_price(3456.7, ETH, up=True) == Decimal("3456.7")
    assert round_price(3456.7, ETH, up=False) == Decimal("3456.7")


def test_decimals_capped_by_sz_decimals():
    # DOGE szDecimals=0 -> max 6 decimals; 0.123456789 has 5 sig figs -> 0.12345
    assert round_price(0.123456789, DOGE, up=False) == Decimal("0.12345")
    # BTC szDecimals=5 -> max 1 decimal even though sig figs would allow more
    assert round_price(1.23456, BTC, up=False) == Decimal("1.2")


def test_small_price_decimal_cap_binds():
    # DOGE: 0.0123456 -> 5 sig figs gives 0.012345 (6 decimals), cap is 6
    assert round_price(0.0123456, DOGE, up=False) == Decimal("0.012345")
    # 0.00123456 -> sig figs would allow 7 decimals, cap 6 binds
    assert round_price(0.00123456, DOGE, up=False) == Decimal("0.001234")
    assert round_price(0.00123456, DOGE, up=True) == Decimal("0.001235")


def test_large_integer_price_allowed():
    assert round_price(1234567.8, BTC, up=False) == Decimal("1234567")
    assert round_price(1234567.8, BTC, up=True) == Decimal("1234568")


def test_round_up_carry():
    assert round_price(99999.5, BTC, up=True) == Decimal("100000")


def test_price_must_be_positive():
    for bad in (0, -1, float("nan")):
        with pytest.raises(ValueError):
            round_price(bad, BTC, up=True)


def test_size_floors_to_sz_decimals():
    assert round_size(0.123456789, BTC) == Decimal("0.12345")
    assert round_size(0.00001, BTC) == Decimal("0.00001")
    assert round_size(0.000019, BTC) == Decimal("0.00001")
    assert round_size(0.000009, BTC) == Decimal("0")
    assert round_size(12.9, DOGE) == Decimal("12")


def test_size_negative_raises():
    with pytest.raises(ValueError):
        round_size(-1, BTC)


def test_notional_ok_boundary():
    assert notional_ok(Decimal("0.00015"), 66666.67, 10)  # 10.0000005
    assert notional_ok(Decimal("0.1"), 100, 10)  # exactly 10
    assert not notional_ok(Decimal("0.0999"), 100, 10)


def test_order_size_meets_min_notional():
    # $10 at 65432 -> floor gives 0.00015 = $9.81, so one lot up
    sz = order_size(10, 65432, BTC, 10)
    assert sz == Decimal("0.00016")
    assert sz * 65432 >= 10


def test_order_size_floors_when_above_min():
    assert order_size(100, 65432, BTC, 10) == Decimal("0.00152")


def test_order_size_below_min_raises():
    with pytest.raises(ValueError):
        order_size(9.99, 65432, BTC, 10)


def test_aggressive_price_moves_through_the_reference_and_rounds_outward():
    assert aggressive_price("buy", 85518.0, BTC) == Decimal("85946")  # 85945.59 up
    assert aggressive_price("sell", 85517.0, BTC) == Decimal("85089")  # 85089.415 down


def test_reduce_size_snaps_float_residue_to_the_nearest_lot():
    # 0.00016 summed from floats is 0.00015999999999999999: flooring it would leave a lot open
    pos = 0.00007 + 0.00009
    assert pos < 0.00016
    assert reduce_size("sell", 0.00016, pos, BTC) == Decimal("0.00016")
    assert reduce_size("buy", 0.00016, -pos, BTC) == Decimal("0.00016")


def test_reduce_size_clamps_to_position_and_refuses_wrong_direction():
    assert reduce_size("sell", 3.0, 1.5, BTC) == Decimal("1.5")
    assert reduce_size("sell", 1.0, 0.0, BTC) == 0
    assert reduce_size("sell", 1.0, -1.0, BTC) == 0
    assert reduce_size("buy", 1.0, 1.0, BTC) == 0
    assert reduce_size("sell", 0.000001, 1.0, BTC) == 0  # below one lot


# ---- maker quote prices (milestone 8) ----

SZ2 = AssetMeta("X", 2)  # at most 4 decimals: 9.9999 and 10.000 are both valid


def test_buy_quote_is_one_tick_above_best_bid():
    assert maker_price("buy", 85517.0, 85520.0, BTC) == Decimal("85518")


def test_sell_quote_is_one_tick_below_best_ask():
    assert maker_price("sell", 85517.0, 85520.0, BTC) == Decimal("85519")


@pytest.mark.parametrize("side,want", [("buy", "85517"), ("sell", "85518")])
def test_one_tick_spread_quotes_at_the_touch(side, want):
    assert maker_price(side, 85517.0, 85518.0, BTC) == Decimal(want)


def test_two_tick_spread_both_sides_meet_in_the_middle_without_crossing():
    assert maker_price("buy", 85517.0, 85519.0, BTC) == Decimal("85518")
    assert maker_price("sell", 85517.0, 85519.0, BTC) == Decimal("85518")


def test_quote_price_at_decade_boundary_never_crosses():
    assert next_price(9.9999, SZ2, up=True) == Decimal("10.000")
    assert next_price(10.0, SZ2, up=False) == Decimal("9.9999")
    assert maker_price("buy", 9.9998, 10.001, SZ2) == Decimal("9.9999")
    assert maker_price("buy", 9.9999, 10.001, SZ2) == Decimal("10.000")
    assert maker_price("sell", 9.9998, 10.0, SZ2) == Decimal("9.9999")  # finer tick below 10
    # one tick apart across the decade: both sides stay at their own touch
    assert maker_price("buy", 9.9999, 10.0, SZ2) == Decimal("9.9999")
    assert maker_price("sell", 9.9999, 10.0, SZ2) == Decimal("10.000")


def test_quote_price_for_coin_with_sz_decimals_4():
    assert maker_price("buy", 3456.7, 3457.0, ETH) == Decimal("3456.8")
    assert maker_price("sell", 3456.7, 3457.0, ETH) == Decimal("3456.9")


def test_quote_price_rejects_a_crossed_or_locked_book():
    with pytest.raises(ValueError):
        maker_price("buy", 85518.0, 85518.0, BTC)


def test_random_books_never_produce_a_crossing_quote():
    import random

    rng = random.Random(7)
    for _ in range(3000):
        meta = AssetMeta("R", rng.choice([0, 1, 2, 4, 5]))
        mag = 10.0 ** rng.uniform(-2, 5.5)
        bid = round_price(mag, meta, up=True)  # up: a tiny price never rounds to 0
        ask = bid
        for _ in range(rng.choice([1, 1, 2, 3, 7])):
            ask = next_price(ask, meta, up=True)
        for side in ("buy", "sell"):
            px = maker_price(side, bid, ask, meta)
            assert bid <= px <= ask
            assert (px < ask) if side == "buy" else (px > bid), (meta, bid, ask, side, px)
            assert round_price(px, meta, up=True) == px == round_price(px, meta, up=False)
