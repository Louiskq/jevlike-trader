"""Position, fees and P&L from fills (cash-flow accounting).

pnl = cash + position * mark - fees, which is realized + unrealized after fees. Position is
in base units (BTC), signed. In the dry run the fills come from `paper`; from milestone 4
they come only from exchange confirmations.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

SECONDS_PER_DAY = 86400
FLAT_EPS = 1e-12  # float residue from summing lots; far below any real lot size


class Ledger:
    def __init__(self) -> None:
        self.position = 0.0
        self.cash = 0.0
        self.fees = 0.0

    def apply_fill(self, side: str, sz: float, px: float, fee: float) -> None:
        if side not in ("buy", "sell"):
            raise ValueError(f"bad side {side!r}")
        if not all(math.isfinite(x) for x in (sz, px, fee)) or sz <= 0 or px <= 0:
            raise ValueError(f"bad fill: sz={sz!r} px={px!r} fee={fee!r}")
        sign = 1 if side == "buy" else -1
        self.position += sign * sz
        if abs(self.position) < FLAT_EPS:
            self.position = 0.0
        self.cash -= sign * sz * px
        self.fees += fee

    def pnl_before_fees(self, mark: float) -> float:
        return self.cash + self.position * mark

    def pnl(self, mark: float) -> float:
        return self.pnl_before_fees(mark) - self.fees

    @classmethod
    def from_fills(cls, rows: Iterable) -> Ledger:
        lg = cls()
        for r in rows:
            lg.apply_fill(r["side"], r["sz"], r["px"], r["fee"])
        return lg


def day_start_pnl(store, now: float) -> float:
    """Cumulative realized + unrealized - fees at 00:00 UTC of the day containing `now`,
    rebuilt from stored fills. Marked at the last decision mid before midnight, else the last
    fill price. This is the KillSwitch baseline, so a restart cannot forgive earlier losses."""
    midnight = now - now % SECONDS_PER_DAY
    rows = store.fills_before(midnight)
    if not rows:
        return 0.0
    mark = store.last_mid_before(midnight)
    if mark is None:
        mark = rows[-1]["px"]
    return Ledger.from_fills(rows).pnl(mark)
