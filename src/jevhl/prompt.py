"""DRAFT PROMPT, written by the assistant for the user to edit.

The wording below is a modelling decision that belongs to the user: it decides what the
decider sees. Everything you may want to change is in the constants right here; the function
only assembles them. The decider's reply is keyed by the question text, so the question and
the schema stay in sync automatically (see `question_text` and `OPTIONS`).
"""

from __future__ import annotations

import math

from .types import ModelInput

# ---- DRAFT WORDING: edit freely ----
MARKET_LINE = "Market: {coin} perpetual on Hyperliquid. Mid {mid}, spread {spread_bps:.2f} bps."
BIDS_HEADER = "Top 10 bids (price x size):"
ASKS_HEADER = "Top 10 asks:"
LEVEL_LINE = "  {px} x {sz:.4f}"
MIDS_LINE = "Last mids: {mids}"
QUESTION = "Will the mid price be higher in {horizon_s:.0f} seconds?"
QUESTION_PREFIX = "Question: "
OPTIONS = ("up", "down")  # first is "up": its probability becomes p_up
# ---- end of draft wording ----

LEVELS = 10
PRICE_SIG_FIGS = 5  # prices get this many significant figures, but at least 1 decimal


def price(x: float, extra_decimals: int = 0) -> str:
    """x with enough decimals that cheap coins keep their resolution (BTC 1, DOGE 5)."""
    digits = math.floor(math.log10(abs(x))) + 1 if x and math.isfinite(x) else 1
    return f"{x:.{max(1, PRICE_SIG_FIGS - digits) + extra_decimals}f}"


def question_text(horizon_s: float) -> str:
    return QUESTION.format(horizon_s=horizon_s)


def render_context(inp: ModelInput) -> str:
    lines = [MARKET_LINE.format(coin=inp.coin, mid=price(inp.mid, 1), spread_bps=inp.spread_bps),
             BIDS_HEADER]
    lines += [LEVEL_LINE.format(px=price(px), sz=sz) for px, sz in inp.bids[:LEVELS]]
    lines.append(ASKS_HEADER)
    lines += [LEVEL_LINE.format(px=price(px), sz=sz) for px, sz in inp.asks[:LEVELS]]
    lines.append(MIDS_LINE.format(mids=", ".join(price(m) for m in inp.recent_mids)))
    lines.append(QUESTION_PREFIX + question_text(inp.horizon_s))
    return "\n".join(lines)
