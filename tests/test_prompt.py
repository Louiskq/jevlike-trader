from jevhl.prompt import OPTIONS, question_text, render_context
from jevhl.types import ModelInput


def inp(**kw):
    base = dict(
        ts=1.0, coin="BTC", mid=65000.456, spread_bps=1.2345,
        bids=[[64999.94 - i, 1.23456 + i] for i in range(10)],
        asks=[[65001.04 + i, 0.5] for i in range(10)],
        recent_mids=[65000.04, 65001.26, 65002.0], horizon_s=60,
    )
    return ModelInput(**{**base, **kw})


def test_render_exact_wording():
    lines = render_context(inp()).split("\n")
    assert lines[0] == "Market: BTC perpetual on Hyperliquid. Mid 65000.46, spread 1.23 bps."
    assert lines[1] == "Top 10 bids (price x size):"
    assert lines[2] == "  64999.9 x 1.2346"
    assert lines[11] == "  64990.9 x 10.2346"
    assert lines[12] == "Top 10 asks:"
    assert lines[13] == "  65001.0 x 0.5000"
    assert lines[22] == "  65010.0 x 0.5000"
    assert lines[23] == "Last mids: 65000.0, 65001.3, 65002.0"
    assert lines[24] == "Question: Will the mid price be higher in 60 seconds?"
    assert len(lines) == 25


def test_question_uses_horizon_and_options():
    assert question_text(30) == "Will the mid price be higher in 30 seconds?"
    assert "in 300 seconds?" in render_context(inp(horizon_s=300))
    assert OPTIONS == ("up", "down")


def test_only_top_ten_levels_and_short_books():
    deep = inp(bids=[[100.0 - i, 1.0] for i in range(15)])
    assert render_context(deep).count("x 1.0000") == 10
    short = inp(bids=[[100.0, 1.0]], asks=[], recent_mids=[])
    text = render_context(short)
    assert "Top 10 asks:\nLast mids: \n" in text
