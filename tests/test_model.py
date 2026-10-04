import math

import pytest

from jevhl.model import MockModel
from jevhl.types import ModelInput


def inp(bid_sz=1.0, ask_sz=1.0, levels=10):
    bids = [[100.0 - i, bid_sz] for i in range(levels)]
    asks = [[101.0 + i, ask_sz] for i in range(levels)]
    return ModelInput(ts=1.0, coin="BTC", mid=100.5, spread_bps=99.0, bids=bids, asks=asks)


def test_deterministic_for_seed():
    a = MockModel(seed=7)
    b = MockModel(seed=7)
    xs = [a.predict(inp(2, 1)).p_up for _ in range(5)]
    ys = [b.predict(inp(2, 1)).p_up for _ in range(5)]
    assert xs == ys


def test_different_seeds_differ():
    assert MockModel(seed=1).predict(inp()).p_up != MockModel(seed=2).predict(inp()).p_up


def test_balanced_book_no_noise_is_half():
    assert MockModel(seed=0, noise=0).predict(inp()).p_up == pytest.approx(0.5)


def test_imbalance_direction_and_formula():
    m = MockModel(seed=0, noise=0, k=2.0)
    imb = (3 - 1) / (3 + 1)
    assert m.predict(inp(bid_sz=3, ask_sz=1)).p_up == pytest.approx(0.5 + 0.3 * math.tanh(2 * imb))
    assert m.predict(inp(bid_sz=1, ask_sz=3)).p_up < 0.5


def test_uses_only_top_ten_levels():
    m = MockModel(seed=0, noise=0)
    deep = inp(levels=15)
    deep.bids[12][1] = 1e6
    assert m.predict(deep).p_up == pytest.approx(0.5)


def test_output_clipped():
    m = MockModel(seed=0, noise=5.0)
    ps = [m.predict(inp(5, 1)).p_up for _ in range(200)]
    assert min(ps) >= 0.01 and max(ps) <= 0.99
    assert 0.01 in ps or 0.99 in ps


def test_empty_book_is_neutral():
    empty = ModelInput(ts=1, coin="BTC", mid=1, spread_bps=0, bids=[], asks=[])
    assert MockModel(seed=0, noise=0).predict(empty).p_up == 0.5


def test_injected_delay_calls_sleep():
    slept = []
    m = MockModel(seed=0, delay_s=3.0, sleep=slept.append)
    m.predict(inp())
    assert slept == [3.0]


def test_no_delay_by_default():
    slept = []
    MockModel(seed=0, sleep=slept.append).predict(inp())
    assert slept == []
