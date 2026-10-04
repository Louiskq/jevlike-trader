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


REASONS = frozenset(
    {"signal", "below_gate", "blocked", "late", "stale", "cap_reached", "killed", "invalid"}
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
class ModelOutput:
    p_up: float
