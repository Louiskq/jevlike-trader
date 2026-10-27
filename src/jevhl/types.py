"""Shared data types. Position is a single signed USD number (long > 0)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class Action(StrEnum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


# Outcomes are recorded at these horizons for every decision (measurement only; trading, the
# gate and exits use Config.horizon_s). Caveat: with one decision per ~5 s snapshot, samples
# at longer horizons overlap more, so they are less independent (a 300 s window shares ~98%
# of its span with its neighbour). Read 300 s Brier gaps with that in mind.
HORIZONS_S = (30, 60, 300)

REASONS = frozenset(
    {"signal", "below_gate", "blocked", "late", "stale", "cap_reached", "killed", "paused",
     "invalid",
     # maker (milestone 8): the cap moved the quote to the other side; quoting paused after a
     # rate-limit error
     "capped", "rate_paused"}
)


@dataclass(frozen=True)
class Decision:
    ts: float
    p_up: float | None
    action: Action
    reason: str
    size_usd: float
    gate: float
    latency_ms: float | None = None


@dataclass(frozen=True)
class ModelInput:
    """One decision's input. bids/asks are [[px, sz], ...], best first, top 10."""

    ts: float
    coin: str
    mid: float
    spread_bps: float
    bids: list[list[float]]
    asks: list[list[float]]
    recent_mids: list[float] = field(default_factory=list)
    horizon_s: float = 60


@dataclass(frozen=True)
class MainnetInput:
    """Mainnet prices for the maker prompt's mainnet block (milestone 10), from
    maker.mainnet_input. returns_bps: window s -> mainnet mid change, None when no mainnet
    mid fresh enough was there at that time."""

    mid: float
    gap_bps: float  # (testnet mid / mainnet mid - 1) * 1e4
    returns_bps: dict[int, float | None]


@dataclass(frozen=True)
class MakerInput(ModelInput):
    """The maker's input (milestone 8): the book (all 20 REST levels, our own resting order
    removed) plus features built in maker.build_input; rendered by prompt.render_maker_context.
    It is a ModelInput, so the mock model reads its bids and asks as before. `mainnet` is set
    only with MAKER_PROMPT=mainnet (milestone 10)."""

    imbalance: float = 0.0  # (bid - ask) / (bid + ask) size near the mid, -1..1
    depth: dict[float, tuple[float, float]] = field(default_factory=dict)  # bps -> (bid, ask)
    returns_bps: dict[float, float | None] = field(default_factory=dict)  # window s -> bps
    sampled_mids: list[float | None] = field(default_factory=list)  # oldest first
    flow: dict = field(default_factory=dict)  # TradeTape.flow over the flow window
    prints: list[tuple[float, str, float, float]] = field(default_factory=list)  # age, side, px, sz
    mainnet: MainnetInput | None = None


@dataclass(frozen=True)
class ModelOutput:
    p_up: float


@dataclass(frozen=True)
class QuoteAck:
    """The venue's answer to a post-only quote or a modify (milestone 8). status:
    placed (resting, oid), rejected (post-only would have crossed), gone (the order to modify
    was already filled or cancelled), refused (the venue would not make the modify, e.g. a side
    change; the old order still rests), filled (it took liquidity; should never happen for a
    post-only order), unknown (a reply without an order status: re-read the open orders)."""

    status: str
    oid: int | None
    detail: str = ""


@dataclass(frozen=True)
class OpenQuote:
    """One of our resting orders on the coin, as the venue reports it."""

    oid: int
    side: str
    px: float
    sz: float  # remaining
