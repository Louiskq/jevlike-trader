"""The mainnet block in the maker prompt (milestone 10): the fine-tuned decider was trained on
prompts rendered by study/finetune_data.py, so the bot must send exactly the same text for the
same inputs. The training renderer is frozen below as it was at fc94a57 (the commit the
fine-tune's data came from): do not edit it. If one of these tests fails after a wording
change, the live prompt no longer matches what the fine-tuned model was trained on."""

import math
import random
from dataclasses import replace

import pytest
from finetune_data import Series

from jevhl import prompt
from jevhl.mainnet import WARMUP_S, mainnet_input
from jevhl.prompt import MISSING, QUESTION_PREFIX, price, render_maker_context
from jevhl.types import MainnetInput
from test_maker_prompt import built

# ---- frozen training renderer (study/finetune_data.py at fc94a57; do not edit) ----
_T_MAINNET_LINE = ("Mainnet (the main Hyperliquid market) BTC mid {mid}; this market's mid is "
                   "{gap:+.1f} bps from it.")
_T_MAINNET_RETURNS_LINE = "Mainnet mid change: {parts}"
_T_MAINNET_RETURN_PART = "{w:g} s {r}"


def _trained_mainnet_block(m_mid, gap_bps, rets):
    parts = [_T_MAINNET_RETURN_PART.format(w=w, r=MISSING if r is None else f"{r:+.1f} bps")
             for w, r in rets.items()]
    return "\n".join([_T_MAINNET_LINE.format(mid=price(m_mid, 1), gap=gap_bps),
                      _T_MAINNET_RETURNS_LINE.format(parts=", ".join(parts))])


def _trained_with_mainnet(context, block):
    head, sep, q = context.rpartition("\n" + QUESTION_PREFIX)
    assert sep, "prompt has no question line"
    return f"{head}\n{block}{sep}{q}"
# ---- end of frozen training renderer ----


GOLDEN = (
    "Market: BTC perpetual on Hyperliquid. Mid 1000.00, spread 8.00 bps.\n"
    "Book imbalance within 1% of mid: +0.455 (+1 all bids, -1 all asks).\n"
    "Resting size within a distance of mid (bids / asks):\n"
    "  5 bps: 1.0000 / 0.5000\n"
    "  10 bps: 3.0000 / 0.7500\n"
    "  20 bps: 6.0000 / 1.7500\n"
    "Top 5 levels each side (price x size), bids | asks:\n"
    "  999.60 x 1.00000 | 1000.4 x 0.50000\n"
    "  999.20 x 2.00000 | 1000.7 x 0.25000\n"
    "  998.30 x 3.00000 | 1001.9 x 1.00000\n"
    "  995.00 x 4.00000 | 1009.0 x 2.00000\n"
    "  985.00 x 50.00000 | 1011.0 x 9.00000\n"
    "Mid change: 5 s -5.0 bps, 15 s +50.3 bps, 60 s +50.3 bps, 300 s +101.0 bps\n"
    "Mid every 5 s over the last 60 s (oldest first): 995.00, 995.00, 995.00, 995.00, 995.00, "
    "995.00, 995.00, 995.00, 995.00, 995.00, 1000.5, 1000.5, 1000.0\n"
    "Taker trades in the last 60 s: 3 prints, buy 0.50000, sell 0.10000, net +0.40000, "
    "vwap 1000.3.\n"
    "Last 10 prints (age s, taker side, price x size):\n"
    "  90.0 s ago buy 1000.0 x 9.00000\n"
    "  30.0 s ago buy 1000.4 x 0.30000\n"
    "  20.0 s ago sell 999.60 x 0.10000\n"
    "  2.5 s ago buy 1000.4 x 0.20000\n"
    "Taker flow is the strongest signal: net taker buying (buy minus sell size) and the side of "
    "the last prints show who is hitting the book. Then compare resting depth near the mid on "
    "each side: thin depth on one side means the price moves easily that way. The mid changes "
    "and recent mids show the path.\n"
    "Mainnet (the main Hyperliquid market) BTC mid 999.620; this market's mid is +3.8 bps from "
    "it.\n"
    "Mainnet mid change: 5 s +1.2 bps, 15 s n/a, 60 s -12.1 bps\n"
    "Question: Will the mid price be higher in 60 seconds?"
)


def with_main(inp, m_mid, gap, rets):
    return replace(inp, mainnet=MainnetInput(mid=m_mid, gap_bps=gap, returns_bps=rets))


def test_standard_maker_prompt_is_unchanged_without_mainnet():
    inp = built()
    assert inp.mainnet is None
    text = render_maker_context(inp)
    assert "Mainnet" not in text
    assert GOLDEN.startswith(text.rsplit("\nQuestion: ", 1)[0] + "\nMainnet")


def test_full_mainnet_prompt_is_the_trained_text():
    inp = built()
    m = 999.62
    text = render_maker_context(with_main(inp, m, (inp.mid / m - 1) * 1e4,
                                          {5: 1.234, 15: None, 60: -12.06}))
    assert text == GOLDEN


def test_mainnet_block_sits_just_before_the_question():
    lines = render_maker_context(with_main(built(), 999.62, 3.8, {5: 0.1, 15: 0.2, 60: 0.3}))
    lines = lines.split("\n")
    assert lines[-1].startswith("Question: ")
    assert lines[-3].startswith("Mainnet (the main Hyperliquid market) BTC mid ")
    assert lines[-2].startswith("Mainnet mid change: ")
    assert lines[-4] == prompt.MAKER_INSTRUCTIONS


def test_bot_mainnet_prompt_equals_the_training_renderer():
    rng = random.Random(10)
    inp = built()
    mids = [999.62, 80000.5, 81234.0, 99999.95, 100000.0, 9999.5, 0.123456]
    gaps = [0.0, 0.04, -0.04, 0.05, -0.05, 3.8014, -12.75, 250.0, -1e-9]
    for m in mids:
        for gap in gaps:
            rets = {w: None if rng.random() < 0.3 else rng.choice([-1, 1]) * rng.random() * 30
                    for w in (5, 15, 60)}
            want = _trained_with_mainnet(render_maker_context(inp),
                                         _trained_mainnet_block(m, gap, rets))
            assert render_maker_context(with_main(inp, m, gap, rets)) == want, (m, gap, rets)


def test_mainnet_wording_is_marked_as_the_assistants_draft():
    src = open(prompt.__file__).read()
    assert "MAINNET DRAFT WORDING" in src
    assert prompt.MAINNET_WINDOWS_S == (5, 15, 60)
    assert prompt.MAINNET_MAX_AGE_S == 5.0


def test_warmup_matches_the_training_warmup():
    from finetune_data import WARMUP_S as TRAINING_WARMUP_S
    assert WARMUP_S == TRAINING_WARMUP_S == 310.0


# ---- the maths: mainnet_input(now, testnet_mid, at) ----

NOW = 1_000_000.0


def series(points):
    """points: (seconds before NOW, mainnet mid), any order."""
    pts = sorted((NOW - dt, m) for dt, m in points)
    return Series([t for t, _ in pts], [m for _, m in pts]).at


def test_mainnet_input_gap_and_changes_maths():
    at = series([(0.4, 80010.0), (5.2, 80000.0), (15.3, 79990.0), (60.1, 80020.0)])
    mi = mainnet_input(NOW, 80050.0, at)
    assert mi.mid == 80010.0
    assert mi.gap_bps == pytest.approx((80050.0 / 80010.0 - 1) * 1e4)  # testnet above: +
    assert mi.gap_bps > 0
    assert mi.returns_bps[5] == pytest.approx((80010.0 / 80000.0 - 1) * 1e4)
    assert mi.returns_bps[15] == pytest.approx((80010.0 / 79990.0 - 1) * 1e4)
    assert mi.returns_bps[60] == pytest.approx((80010.0 / 80020.0 - 1) * 1e4)
    assert list(mi.returns_bps) == [5, 15, 60]


def test_mainnet_input_is_none_when_latest_mainnet_mid_is_older_than_5s():
    assert mainnet_input(NOW, 80000.0, series([(5.37, 80000.0), (70.0, 80000.0)])) is None
    assert mainnet_input(NOW, 80000.0, series([(4.63, 80000.0), (70.0, 80000.0)])) is not None
    assert mainnet_input(NOW, 80000.0, series([])) is None  # none at all yet


def test_mainnet_age_of_exactly_5s_is_fresh_as_in_training():
    # training skipped a row only when the age was over 5 s (`mage > MAX_BOOK_AGE_S`)
    at = Series([NOW - 5.0], [80000.0]).at
    assert mainnet_input(NOW, 80000.0, at) is not None
    at = Series([NOW - 65.0, NOW], [80000.0, 80001.0]).at  # past mid exactly 5 s old at -60
    assert mainnet_input(NOW, 80000.0, at).returns_bps[60] is not None


def test_a_past_mainnet_mid_older_than_5s_at_its_target_shows_n_a():
    # at NOW - 15 the latest mid is 5.37 s older than the target; at NOW - 60, 4.63 s older
    at = series([(0.2, 80010.0), (20.37, 80000.0), (64.63, 79990.0)])
    mi = mainnet_input(NOW, 80000.0, at)
    assert mi.returns_bps[15] is None
    assert mi.returns_bps[60] == pytest.approx((80010.0 / 79990.0 - 1) * 1e4)
    text = render_maker_context(replace(built(), mainnet=mi))
    assert "15 s n/a" in text


def test_mainnet_input_ignores_mids_received_after_the_book():
    at = series([(1.0, 80000.0), (-0.3, 90000.0)])  # one received 0.3 s after the book
    assert mainnet_input(NOW, 80000.0, at).mid == 80000.0


def test_mainnet_input_is_none_for_non_finite_values():
    def nan_at(t):
        return (math.nan, 0.1)

    def inf_at(t):
        return (math.inf, 0.1)

    assert mainnet_input(NOW, 80000.0, nan_at) is None
    assert mainnet_input(NOW, 80000.0, inf_at) is None
    assert mainnet_input(NOW, math.nan, series([(0.5, 80000.0)])) is None
    assert mainnet_input(NOW, 80000.0, series([(0.5, 0.0)])) is None  # never a zero mid
