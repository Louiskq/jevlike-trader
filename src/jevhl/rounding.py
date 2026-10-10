"""Price and size rounding to Hyperliquid perp rules.

Price: at most 5 significant figures and at most (6 - szDecimals) decimals; integer
prices are always allowed. Size: rounded to szDecimals. Decimal throughout.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

MAX_SIG_FIGS = 5
MAX_PERP_DECIMALS = 6
SLIPPAGE_BPS = 50  # how far through the reference price a marketable IOC limit is set


@dataclass(frozen=True)
class AssetMeta:
    name: str
    sz_decimals: int

    @classmethod
    def from_meta(cls, meta: dict, coin: str) -> AssetMeta:
        """Build from the `meta` info response ({"universe": [...]})."""
        for a in meta["universe"]:
            if a["name"] == coin:
                return cls(name=coin, sz_decimals=int(a["szDecimals"]))
        raise KeyError(f"{coin} not in universe")


def _dec(x: float | Decimal | int) -> Decimal:
    return x if isinstance(x, Decimal) else Decimal(str(x))


def round_price(px: float | Decimal, meta: AssetMeta, *, up: bool) -> Decimal:
    """Round to a valid price. up=True rounds toward +inf (buys), else toward -inf (sells)."""
    p = _dec(px)
    if not p.is_finite() or p <= 0:
        raise ValueError(f"price must be positive and finite, got {px!r}")
    decimals = min(MAX_SIG_FIGS - 1 - p.adjusted(), MAX_PERP_DECIMALS - meta.sz_decimals)
    decimals = max(decimals, 0)  # integers are always allowed
    return p.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_CEILING if up else ROUND_FLOOR)


def round_size(sz: float | Decimal, meta: AssetMeta) -> Decimal:
    """Floor to szDecimals."""
    s = _dec(sz)
    if not s.is_finite() or s < 0:
        raise ValueError(f"size must be non-negative and finite, got {sz!r}")
    return s.quantize(Decimal(1).scaleb(-meta.sz_decimals), rounding=ROUND_FLOOR)


def notional_ok(sz: float | Decimal, px: float | Decimal, min_notional: float) -> bool:
    return _dec(sz) * _dec(px) >= _dec(min_notional)


def order_size(usd: float, px: float, meta: AssetMeta, min_notional: float) -> Decimal:
    """Size for a target notional: floored to a lot, plus one lot if that falls below the
    exchange minimum notional (so a minimum-size order is never rejected)."""
    if not math.isfinite(usd) or usd < min_notional:
        raise ValueError(f"notional {usd!r} is below the minimum {min_notional}")
    sz = round_size(_dec(usd) / _dec(px), meta)
    if not notional_ok(sz, px, min_notional):
        sz += Decimal(1).scaleb(-meta.sz_decimals)
    return sz


def aggressive_price(side: str, ref: float | Decimal, meta: AssetMeta) -> Decimal:
    """Marketable IOC limit: the reference price moved SLIPPAGE_BPS in the aggressive
    direction, then rounded outward (buys up, sells down) to exchange rules."""
    slip = Decimal(SLIPPAGE_BPS) / Decimal(10_000)
    if side == "buy":
        return round_price(_dec(ref) * (1 + slip), meta, up=True)
    return round_price(_dec(ref) * (1 - slip), meta, up=False)


def reduce_size(side: str, size: float, position: float, meta: AssetMeta) -> Decimal:
    """Size of a reduce-only order: never above the position, never in the growing direction
    (returns 0 then). The float position carries summing error (0.000159999... for 0.00016),
    so it is snapped to the nearest lot before the floor, or the last lot is left open."""
    room = -position if side == "buy" else position
    if room <= 0:
        return Decimal(0)
    room_d = Decimal(str(room)).quantize(Decimal(1).scaleb(-meta.sz_decimals))
    return round_size(min(Decimal(str(size)), room_d), meta)


def next_price(px: float | Decimal, meta: AssetMeta, *, up: bool) -> Decimal:
    """The nearest valid price strictly above (up) or below px. Stepping by the finest
    possible tick and then rounding handles the decade boundary, where the tick changes
    (10.000 - one tick is 9.9999 for a coin allowed 4 decimals)."""
    step = Decimal(1).scaleb(-(MAX_PERP_DECIMALS - meta.sz_decimals))
    p = _dec(px)
    return round_price(p + step, meta, up=True) if up else round_price(p - step, meta, up=False)


def maker_price(side: str, best_bid: float | Decimal, best_ask: float | Decimal,
                meta: AssetMeta) -> Decimal:
    """Post-only quote price: one tick inside the touch, or at the touch when the spread is
    one tick. A buy is always below the best ask and a sell above the best bid, so the
    quote never crosses (which would be a post-only rejection)."""
    bid, ask = _dec(best_bid), _dec(best_ask)
    if not bid < ask:
        raise ValueError(f"crossed or locked book: bid {bid} ask {ask}")
    if side == "buy":
        px = next_price(bid, meta, up=True)
        return px if px < ask else round_price(bid, meta, up=False)
    px = next_price(ask, meta, up=False)
    return px if px > bid else round_price(ask, meta, up=True)
