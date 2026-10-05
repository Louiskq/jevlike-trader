from decimal import Decimal

import pytest

from jevhl.feed import Book
from jevhl.paper import TAKER_FEE_RATE, PaperExchange, limit_price
from jevhl.rounding import SLIPPAGE_BPS, AssetMeta
from jevhl.store import Store

BTC = AssetMeta("BTC", 5)


def book(bids=None, asks=None):
    bids = bids or [[99.0, 1.0], [98.0, 1.0], [97.0, 5.0]]
    asks = asks or [[100.0, 1.0], [101.0, 1.0], [102.0, 5.0]]
    return Book(ts=1.0, recv_ts=1.0, coin="BTC", bids=bids, asks=asks)


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def px(side, b):
    return limit_price(side, b, BTC)


def test_limit_price_rounds_in_the_aggressive_direction():
    b = book(bids=[[85517.0, 1.0]], asks=[[85518.0, 1.0]])
    assert SLIPPAGE_BPS == 50
    assert limit_price("buy", b, BTC) == Decimal("85946")  # 85945.59 rounded up
    assert limit_price("sell", b, BTC) == Decimal("85089")  # 85089.415 rounded down


def test_buy_walks_asks_within_limit_and_is_partial_ioc(store):
    ex = PaperExchange(store, BTC, "BTC")
    # limit 100 * 1.005 = 100.5 -> only the 100.0 level is inside it
    fills = ex.open(book(), "buy", usd=250.0, ts=5.0)
    assert [(f["px"], f["sz"]) for f in fills] == [(100.0, 1.0)]  # wanted 2.5, one level fits


def test_buy_sweeps_two_levels_with_distinct_tids(store):
    ex = PaperExchange(store, BTC, "BTC")
    b = book(asks=[[100.0, 1.0], [100.2, 1.0], [102.0, 5.0]])
    fills = ex.open(b, "buy", usd=200.0, ts=5.0)  # 200 / 100.0 = 2.0
    assert [(f["px"], f["sz"]) for f in fills] == [(100.0, 1.0), (100.2, 1.0)]
    rows = store.fills()
    assert len(rows) == 2 and len({r["tid"] for r in rows}) == 2
    assert all(r["tid"] < 0 for r in rows)  # paper tids never collide with exchange tids
    assert all(r["side"] == "buy" and r["ts"] == 5.0 and r["coin"] == "BTC" for r in rows)


def test_sell_walks_bids_within_limit(store):
    ex = PaperExchange(store, BTC, "BTC")
    # limit 99 * 0.995 = 98.505 -> rounded down to 98.5, so the 98.0 level is outside it
    fills = ex.open(book(), "sell", usd=250.0, ts=1.0)
    assert [(f["px"], f["sz"], f["side"]) for f in fills] == [(99.0, 1.0, "sell")]


def test_fee_is_taker_rate_on_notional(store):
    ex = PaperExchange(store, BTC, "BTC")
    b = book(asks=[[65000.0, 1.0]], bids=[[64999.0, 1.0]])
    (f,) = ex.open(b, "buy", usd=130.0, ts=1.0)  # 130 / 65000 = 0.002 BTC
    assert f["sz"] == pytest.approx(0.002)
    assert TAKER_FEE_RATE == 0.00045
    assert f["fee"] == pytest.approx(0.0585)  # 130 * 0.00045
    assert store.fills()[0]["fee"] == pytest.approx(0.0585)


def test_minimum_notional_rounds_up_one_lot(store):
    ex = PaperExchange(store, BTC, "BTC")
    b = book(asks=[[85518.0, 1.0]], bids=[[85517.0, 1.0]])
    (f,) = ex.open(b, "buy", usd=10.0, ts=1.0)
    assert f["sz"] * f["px"] >= 10.0


def test_thin_best_level_gives_partial_fill(store):
    ex = PaperExchange(store, BTC, "BTC")
    b = book(asks=[[100.0, 0.00001]], bids=[[99.0, 1.0]])
    fills = ex.open(b, "buy", usd=100.0, ts=1.0)
    assert sum(f["sz"] for f in fills) == pytest.approx(0.00001)


def test_close_is_reduce_only_and_clamped_to_position(store):
    ex = PaperExchange(store, BTC, "BTC")
    b = book(bids=[[99.0, 10.0]], asks=[[100.0, 10.0]])
    fills = ex.close(b, "sell", size=3.0, position=1.5, ts=2.0)  # long 1.5, asked 3.0
    assert sum(f["sz"] for f in fills) == pytest.approx(1.5)
    assert ex.close(b, "sell", size=1.0, position=0.0, ts=2.0) == []  # flat: nothing
    assert ex.close(b, "sell", size=1.0, position=-1.0, ts=2.0) == []  # wrong side: nothing
    assert len(store.fills()) == 1


def test_close_buy_side_for_short(store):
    ex = PaperExchange(store, BTC, "BTC")
    b = book(bids=[[99.0, 10.0]], asks=[[100.0, 10.0]])
    fills = ex.close(b, "buy", size=0.5, position=-2.0, ts=2.0)
    assert sum(f["sz"] for f in fills) == pytest.approx(0.5) and fills[0]["side"] == "buy"


def test_tids_continue_below_existing_after_restart(store):
    ex1 = PaperExchange(store, BTC, "BTC")
    b = book(asks=[[100.0, 10.0]])
    ex1.open(b, "buy", usd=20.0, ts=1.0)
    ex2 = PaperExchange(store, BTC, "BTC")  # restart: same store
    ex2.open(b, "buy", usd=20.0, ts=2.0)
    tids = [r["tid"] for r in store.fills()]
    assert len(set(tids)) == 2


def test_open_rejects_below_minimum_notional(store):
    ex = PaperExchange(store, BTC, "BTC")
    with pytest.raises(ValueError):
        ex.open(book(), "buy", usd=5.0, ts=1.0)
