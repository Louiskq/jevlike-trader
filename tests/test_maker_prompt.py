"""The maker's model input (milestone 8): book imbalance, depth bands, returns over time
windows, sampled mids, taker flow and last prints; the DRAFT prompt that renders it; and the
decider using that renderer in maker mode. The wording itself is the user's to edit, so these
tests pin the maths and the sections, plus a few lines of the current draft."""

import pytest

from decider_server import Q60, FakeDecider, good_body
from jevhl import prompt
from jevhl.config import Config
from jevhl.feed import Book, Print, TradeTape
from jevhl.maker import build_input
from jevhl.model import DeciderModel, MockModel, build_model
from jevhl.prompt import question_text, render_context, render_maker_context
from jevhl.types import MakerInput

T = 1_000_000.0
BIDS = [[999.6, 1.0], [999.2, 2.0], [998.3, 3.0], [995.0, 4.0], [985.0, 50.0]]
ASKS = [[1000.4, 0.5], [1000.7, 0.25], [1001.9, 1.0], [1009.0, 2.0], [1011.0, 9.0]]


def book(t=T, bids=BIDS, asks=ASKS):
    return Book(ts=t - 0.5, recv_ts=t, coin="BTC", bids=bids, asks=asks)


def mids_history():
    """(ts, mid) with irregular gaps, ending at the decision's own book (mid 1000.0)."""
    pts = [(0, 990.0), (100, 995.0), (290, 1000.5), (296, 1001.0), (299, 1002.0), (300, 1000.0)]
    return [(T - 300 + t, m) for t, m in pts]


def tape():
    tp = TradeTape()
    tp.add([
        Print(T - 90, T - 90, "buy", 1000.0, 9.0, 1),  # outside the 60 s flow window
        Print(T - 30, T - 30, "buy", 1000.4, 0.3, 2),
        Print(T - 20, T - 20, "sell", 999.6, 0.1, 3),
        Print(T - 2.5, T - 2.4, "buy", 1000.4, 0.2, 4),
    ], T)
    return tp


def built():
    return build_input(book(), mids_history(), tape(), recent=[999.0, 1000.0], horizon_s=60)


def test_imbalance_and_depth_band_maths():
    inp = built()
    assert isinstance(inp, MakerInput) and inp.mid == 1000.0
    # within 1% of mid: bids 1+2+3+4 = 10 (985 is out), asks 0.5+0.25+1+2 = 3.75 (1011 is out)
    assert inp.imbalance == pytest.approx((10.0 - 3.75) / 13.75)
    assert inp.depth == {5: (1.0, 0.5), 10: (3.0, 0.75), 20: (6.0, 1.75)}


def test_returns_windows_by_time_not_count():
    inp = built()
    # mid at or before now - w: 5 s -> the book at -10 s (1000.5), 15 and 60 s -> -200 s (995),
    # 300 s -> the first book (990). Five books back would give 990 for 5 s.
    assert inp.returns_bps[5] == pytest.approx((1000.0 / 1000.5 - 1) * 1e4)
    assert inp.returns_bps[15] == pytest.approx((1000.0 / 995.0 - 1) * 1e4)
    assert inp.returns_bps[60] == pytest.approx((1000.0 / 995.0 - 1) * 1e4)
    assert inp.returns_bps[300] == pytest.approx((1000.0 / 990.0 - 1) * 1e4)


def test_returns_are_none_before_the_history_reaches_back():
    inp = build_input(book(), [(T - 7, 999.0), (T, 1000.0)], TradeTape(), recent=[],
                      horizon_s=60)
    assert inp.returns_bps[5] == pytest.approx((1000.0 / 999.0 - 1) * 1e4)
    assert inp.returns_bps[15] is None and inp.returns_bps[300] is None
    assert inp.sampled_mids[-1] == 1000.0 and inp.sampled_mids[0] is None


def test_sampled_mids_every_5_s_over_60_s_oldest_first():
    inp = built()
    assert inp.sampled_mids == [995.0] * 10 + [1000.5, 1000.5, 1000.0]


def test_flow_and_prints_in_the_input():
    inp = built()
    assert inp.flow["count"] == 3 and inp.flow["buy_sz"] == pytest.approx(0.5)
    assert inp.flow["sell_sz"] == pytest.approx(0.1)
    assert inp.prints[-1] == (2.5, "buy", 1000.4, 0.2)  # (age s, taker side, px, sz), newest last
    assert len(inp.prints) == 4
    assert inp.bids == BIDS and inp.recent_mids == [999.0, 1000.0] and inp.horizon_s == 60


def test_mock_model_still_reads_a_maker_input():
    p = MockModel(seed=1).predict(built()).p_up
    assert 0.0 < p < 1.0


def test_prompt_module_marks_the_maker_wording_as_a_draft_for_the_user():
    src = open(prompt.__file__).read()
    assert "MAKER DRAFT WORDING" in src and "DRAFT" in prompt.__doc__


def test_maker_prompt_has_every_section():
    text = render_maker_context(built())
    lines = text.split("\n")
    assert lines[0] == "Market: BTC perpetual on Hyperliquid. Mid 1000.00, spread 8.00 bps."
    for needle in ("Book imbalance within 1% of mid: +0.455",
                   "Resting size within a distance of mid (bids / asks):",
                   "  5 bps: 1.0000 / 0.5000", "  20 bps: 6.0000 / 1.7500",
                   "Top 5 levels each side (price x size), bids | asks:",
                   "  999.60 x 1.00000 | 1000.4 x 0.50000",
                   "Mid change: 5 s -5.0 bps, 15 s +50.3 bps, 60 s +50.3 bps, 300 s +101.0 bps",
                   "Mid every 5 s over the last 60 s (oldest first): ",
                   "Taker trades in the last 60 s: 3 prints, buy 0.50000, sell 0.10000, "
                   "net +0.40000",
                   "Last 10 prints (age s, taker side, price x size):",
                   "  2.5 s ago buy 1000.4 x 0.20000",
                   prompt.MAKER_INSTRUCTIONS):
        assert needle in text, needle
    level_lines = [x for x in lines if x.startswith("  ") and " | " in x]
    assert len(level_lines) == 5  # top 5 levels, not all of them
    assert lines[-1] == "Question: " + question_text(60)


def test_maker_prompt_shows_missing_values_as_n_a():
    inp = build_input(book(), [(T, 1000.0)], TradeTape(), recent=[], horizon_s=60)
    text = render_maker_context(inp)
    assert "5 s n/a" in text and "vwap n/a" in text and "n/a, n/a" in text
    assert "Last 10 prints (age s, taker side, price x size):\n  none" in text


def test_maker_prompt_question_is_the_schema_key():
    inp = built()
    with FakeDecider(good_body(0.61)) as srv:
        out = DeciderModel(srv.url, 2.0, render=render_maker_context).predict(inp)
    assert out.p_up == pytest.approx(0.61)
    (req,) = srv.requests
    assert req["json"]["context"] == render_maker_context(inp)
    assert req["json"]["schema"] == {Q60: {"type": "choice", "options": ["up", "down"]}}


def test_decider_uses_maker_renderer_when_strategy_maker():
    inp = built()
    env = {"MODEL": "decider", "DECIDER_URL": "http://127.0.0.1:1", "STRATEGY": "maker",
           "MAX_POSITION_USD": "15"}
    with FakeDecider(good_body(0.4)) as srv:
        env["DECIDER_URL"] = srv.url
        maker = build_model(Config.from_env(env))
        maker.predict(inp)
        taker = build_model(Config.from_env({**env, "STRATEGY": "taker"}))
        taker.predict(inp)
    assert srv.requests[0]["json"]["context"] == render_maker_context(inp)
    assert srv.requests[1]["json"]["context"] == render_context(inp)
    assert maker._timeout_s == 1.0 and taker._timeout_s == 2.0  # each strategy's deadline
