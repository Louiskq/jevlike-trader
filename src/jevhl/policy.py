"""Pure decision policy: gate, abstain, confidence sizing, position cap; and the maker's side
choice (milestone 8)."""

from __future__ import annotations

import math

from .config import Config
from .types import REASONS, Action, Decision

EPS = 1e-12  # float noise in 1 - gate
MAKER_GATE = 0.5  # the maker always quotes: buy at p_up >= 0.5, else sell
CAP_EPS_USD = 1e-9  # float noise in position + size against the cap


def hold(
    reason: str,
    *,
    ts: float,
    gate: float,
    p_up: float | None = None,
    latency_ms: float | None = None,
) -> Decision:
    if reason not in REASONS or reason == "signal":
        raise ValueError(f"not a hold reason: {reason!r}")
    return Decision(ts, p_up, Action.HOLD, reason, 0.0, gate, latency_ms)


def stale_hold(*, ts: float, gate: float) -> Decision:
    return hold("stale", ts=ts, gate=gate)


def decide(
    p_up: float | None,
    position_usd: float,
    cfg: Config,
    *,
    ts: float,
    latency_ms: float,
) -> Decision:
    """Turn a model probability into an action. position_usd is signed net (long > 0)."""

    def h(reason: str) -> Decision:
        return hold(reason, ts=ts, gate=cfg.gate, p_up=p_up, latency_ms=latency_ms)

    if latency_ms > cfg.deadline_s * 1000:
        return h("late")
    if p_up is None or not math.isfinite(p_up) or not 0.0 <= p_up <= 1.0:
        return h("invalid")

    if p_up >= cfg.gate - EPS:
        action, conf = Action.BUY, p_up
    elif 1.0 - p_up >= cfg.gate - EPS:
        action, conf = Action.SELL, 1.0 - p_up
    else:
        return h("below_gate")

    sign = 1 if action is Action.BUY else -1
    if position_usd * sign < 0:
        return h("blocked")

    frac = (conf - cfg.gate) / (cfg.full_size_p - cfg.gate)
    frac = min(max(frac, 0.0), 1.0)
    size = cfg.min_notional_usd + frac * (cfg.max_position_usd - cfg.min_notional_usd)
    room = cfg.max_position_usd - abs(position_usd)
    if room < cfg.min_notional_usd:
        return h("cap_reached")
    size = min(size, room)
    return Decision(ts, p_up, action, "signal", size, cfg.gate, latency_ms)


def maker_side(
    p_up: float | None,
    position_usd: float,
    cfg: Config,
    *,
    ts: float,
    latency_ms: float,
) -> Decision:
    """The maker's side: buy if p_up >= 0.5, else sell; hold only when late or invalid. A side
    is allowed if a fill of the full quote keeps the position within the cap, or if it reduces
    the position (so a position already over the cap, after a UI trade, can always shrink).
    When the wanted side is not allowed the quote goes on the other side, reason "capped"."""

    def h(reason: str) -> Decision:
        return hold(reason, ts=ts, gate=MAKER_GATE, p_up=p_up, latency_ms=latency_ms)

    if latency_ms > cfg.maker_deadline_s * 1000:
        return h("late")
    if p_up is None or not math.isfinite(p_up) or not 0.0 <= p_up <= 1.0:
        return h("invalid")
    size = cfg.maker_size_usd

    def allowed(sign: int) -> bool:
        reduces = position_usd * sign < 0
        return reduces or abs(position_usd + sign * size) <= cfg.max_position_usd + CAP_EPS_USD

    want = 1 if p_up >= MAKER_GATE else -1
    if allowed(want):
        sign, reason = want, "signal"
    elif allowed(-want):
        sign, reason = -want, "capped"
    else:  # cannot happen while MAKER_SIZE_USD <= MAX_POSITION_USD (config checks it)
        return h("cap_reached")
    action = Action.BUY if sign > 0 else Action.SELL
    return Decision(ts, p_up, action, reason, size, MAKER_GATE, latency_ms)
