"""DRAFT PROMPTS, written by the assistant for the user to edit.

The wording below is a modelling decision that belongs to the user: it decides what the
decider sees. Everything you may want to change is in the constants right here; the functions
only assemble them. The decider's reply is keyed by the question text, so the question and
the schema stay in sync automatically (see `question_text` and `OPTIONS`).

Two drafts: the taker's (render_context, unchanged since milestone 5) and the maker's
(render_maker_context, milestone 8). The maker block also holds the feature settings (depth
bands, return windows, flow window, number of prints): what the model is shown is the user's
call too. The maker draft does not show the position or which side the cap allows (user
decision 2026-10-10); it asks the same 60 s question as the taker.
"""

from __future__ import annotations

import math

from .types import MakerInput, ModelInput

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


# ---- MAKER DRAFT WORDING: edit freely (assistant's draft, milestone 8) ----
IMBALANCE_BAND_PCT = 1.0  # book imbalance counts levels within this % of mid
DEPTH_BANDS_BPS = (5, 10, 20)  # 20 REST levels of BTC reach about 20 bps
RETURN_WINDOWS_S = (5, 15, 60, 300)
MID_SAMPLE_S, MID_SPAN_S = 5, 60  # recent mids: one every 5 s over the last 60 s
FLOW_WINDOW_S = 60
N_LEVELS = 5
N_PRINTS = 10
MAKER_MARKET_LINE = MARKET_LINE
MAKER_IMBALANCE_LINE = ("Book imbalance within {band:g}% of mid: {imbalance:+.3f} "
                        "(+1 all bids, -1 all asks).")
MAKER_DEPTH_HEADER = "Resting size within a distance of mid (bids / asks):"
MAKER_DEPTH_LINE = "  {bps:g} bps: {bid:.4f} / {ask:.4f}"
MAKER_LEVELS_HEADER = "Top {n} levels each side (price x size), bids | asks:"
MAKER_LEVEL_LINE = "  {bid} | {ask}"
MAKER_LEVEL = "{px} x {sz:.5f}"
MAKER_RETURNS_LINE = "Mid change: {parts}"
MAKER_RETURN_PART = "{w:g} s {r}"  # r is "+1.2 bps" or "n/a"
MAKER_MIDS_LINE = "Mid every {step:g} s over the last {span:g} s (oldest first): {mids}"
MAKER_FLOW_LINE = ("Taker trades in the last {window:g} s: {count} prints, buy {buy:.5f}, "
                   "sell {sell:.5f}, net {net:+.5f}, vwap {vwap}.")
MAKER_PRINTS_HEADER = "Last {n} prints (age s, taker side, price x size):"
MAKER_PRINT_LINE = "  {age:.1f} s ago {side} {px} x {sz:.5f}"
MAKER_NO_PRINTS = "  none"
MISSING = "n/a"
MAKER_INSTRUCTIONS = (
    "Taker flow is the strongest signal: net taker buying (buy minus sell size) and the side "
    "of the last prints show who is hitting the book. Then compare resting depth near the mid "
    "on each side: thin depth on one side means the price moves easily that way. The mid "
    "changes and recent mids show the path."
)
# ---- end of maker draft wording ----


def render_context(inp: ModelInput) -> str:
    lines = [MARKET_LINE.format(coin=inp.coin, mid=price(inp.mid, 1), spread_bps=inp.spread_bps),
             BIDS_HEADER]
    lines += [LEVEL_LINE.format(px=price(px), sz=sz) for px, sz in inp.bids[:LEVELS]]
    lines.append(ASKS_HEADER)
    lines += [LEVEL_LINE.format(px=price(px), sz=sz) for px, sz in inp.asks[:LEVELS]]
    lines.append(MIDS_LINE.format(mids=", ".join(price(m) for m in inp.recent_mids)))
    lines.append(QUESTION_PREFIX + question_text(inp.horizon_s))
    return "\n".join(lines)


def render_maker_context(inp: MakerInput) -> str:
    def opt(x: float | None, fmt: str) -> str:
        return MISSING if x is None else fmt.format(x)

    lines = [
        MAKER_MARKET_LINE.format(coin=inp.coin, mid=price(inp.mid, 1), spread_bps=inp.spread_bps),
        MAKER_IMBALANCE_LINE.format(band=IMBALANCE_BAND_PCT, imbalance=inp.imbalance),
        MAKER_DEPTH_HEADER,
    ]
    lines += [MAKER_DEPTH_LINE.format(bps=bps, bid=b, ask=a) for bps, (b, a) in inp.depth.items()]
    lines.append(MAKER_LEVELS_HEADER.format(n=N_LEVELS))
    for (bp, bs), (ap, az) in zip(inp.bids[:N_LEVELS], inp.asks[:N_LEVELS], strict=False):
        lines.append(MAKER_LEVEL_LINE.format(bid=MAKER_LEVEL.format(px=price(bp), sz=bs),
                                             ask=MAKER_LEVEL.format(px=price(ap), sz=az)))
    parts = [MAKER_RETURN_PART.format(w=w, r=opt(r, "{:+.1f} bps"))
             for w, r in inp.returns_bps.items()]
    lines.append(MAKER_RETURNS_LINE.format(parts=", ".join(parts)))
    lines.append(MAKER_MIDS_LINE.format(
        step=MID_SAMPLE_S, span=MID_SPAN_S,
        mids=", ".join(MISSING if m is None else price(m) for m in inp.sampled_mids),
    ))
    f = inp.flow
    lines.append(MAKER_FLOW_LINE.format(
        window=FLOW_WINDOW_S, count=f.get("count", 0), buy=f.get("buy_sz", 0.0),
        sell=f.get("sell_sz", 0.0), net=f.get("net_sz", 0.0),
        vwap=MISSING if f.get("vwap") is None else price(f["vwap"]),
    ))
    lines.append(MAKER_PRINTS_HEADER.format(n=N_PRINTS))
    lines += [MAKER_PRINT_LINE.format(age=age, side=side, px=price(px), sz=sz)
              for age, side, px, sz in inp.prints] or [MAKER_NO_PRINTS]
    lines.append(MAKER_INSTRUCTIONS)
    lines.append(QUESTION_PREFIX + question_text(inp.horizon_s))
    return "\n".join(lines)
