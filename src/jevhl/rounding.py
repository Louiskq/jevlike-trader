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
