"""DRAFT PROMPT, written by the assistant for the user to edit.

The wording below is a modelling decision that belongs to the user: it decides what the
decider sees. Everything you may want to change is in the constants right here; the function
only assembles them. The decider's reply is keyed by the question text, so the question and
the schema stay in sync automatically (see `question_text` and `OPTIONS`).
"""

from __future__ import annotations

from .types import ModelInput

# ---- DRAFT WORDING: edit freely ----
MARKET_LINE = "Market: {coin} perpetual on Hyperliquid. Mid {mid:.2f}, spread {spread_bps:.2f} bps."
BIDS_HEADER = "Top 10 bids (price x size):"
ASKS_HEADER = "Top 10 asks:"
LEVEL_LINE = "  {px:.1f} x {sz:.4f}"
MIDS_LINE = "Last mids: {mids}"
MID_FORMAT = "{:.1f}"
QUESTION = "Will the mid price be higher in {horizon_s:.0f} seconds?"
QUESTION_PREFIX = "Question: "
OPTIONS = ("up", "down")  # first is "up": its probability becomes p_up
# ---- end of draft wording ----

LEVELS = 10


def question_text(horizon_s: float) -> str:
    return QUESTION.format(horizon_s=horizon_s)


def render_context(inp: ModelInput) -> str:
    lines = [MARKET_LINE.format(coin=inp.coin, mid=inp.mid, spread_bps=inp.spread_bps),
             BIDS_HEADER]
    lines += [LEVEL_LINE.format(px=px, sz=sz) for px, sz in inp.bids[:LEVELS]]
    lines.append(ASKS_HEADER)
    lines += [LEVEL_LINE.format(px=px, sz=sz) for px, sz in inp.asks[:LEVELS]]
    lines.append(MIDS_LINE.format(mids=", ".join(MID_FORMAT.format(m) for m in inp.recent_mids)))
    lines.append(QUESTION_PREFIX + question_text(inp.horizon_s))
    return "\n".join(lines)
