"""Mainnet prices for the maker prompt's mainnet block (milestone 10, MAKER_PROMPT=mainnet).

The fine-tuned decider was trained with mainnet BTC prices in its prompt, so the maker needs
them live. This module computes the block's numbers the way the fine-tune's data did
(study/finetune_data.py uses this function too).
"""

from __future__ import annotations

import math
from collections.abc import Callable

from .prompt import MAINNET_MAX_AGE_S, MAINNET_WINDOWS_S
from .types import MainnetInput

# Hold this long after the run's first book. The fine-tune's data skipped each recording's
# first 310 s (study/finetune_data.py WARMUP_S), so the model never saw a prompt without 5
# minutes of history (user decision 2026-10-26).
WARMUP_S = 310.0


def mainnet_input(now: float, testnet_mid: float,
                  at: Callable[[float], tuple[float | None, float]]) -> MainnetInput | None:
    """The mainnet block's numbers at `now` (the testnet book's receive time), as the
    fine-tune's data computed them. at(t) gives the latest mainnet mid received at or before t
    and its age at t. None (no prompt can be built) when the latest mainnet mid is missing or
    over MAINNET_MAX_AGE_S old, or a value is not a finite positive number. A past mid over
    MAINNET_MAX_AGE_S older than its target time gives None for that window (shown "n/a")."""
    m_now, age = at(now)
    if m_now is None or age > MAINNET_MAX_AGE_S:
        return None
    if not (math.isfinite(m_now) and m_now > 0 and math.isfinite(testnet_mid)
            and testnet_mid > 0):
        return None
    rets: dict[int, float | None] = {}
    for w in MAINNET_WINDOWS_S:
        past, page = at(now - w)
        ok = past is not None and page <= MAINNET_MAX_AGE_S and math.isfinite(past) and past > 0
        rets[w] = (m_now / past - 1) * 1e4 if ok else None
    return MainnetInput(mid=m_now, gap_bps=(testnet_mid / m_now - 1) * 1e4, returns_bps=rets)
