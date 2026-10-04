import json
from decimal import Decimal
from pathlib import Path

import pytest

from jevhl.rounding import AssetMeta, notional_ok, order_size, round_price, round_size

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
