"""Kill switch state machine. No I/O: the caller flattens and stops when it trips.

Triggers: daily loss, consecutive order errors, stale book. Latched until reset();
the first trigger's reason is kept.
"""

from __future__ import annotations

from collections.abc import Callable

from .config import Config

SECONDS_PER_DAY = 86400


class KillSwitch:
    def __init__(self, cfg: Config, clock: Callable[[], float]):
        self._cfg = cfg
        self._clock = clock  # epoch seconds (UTC)
        self.reset()

    def reset(self) -> None:
        self.reason: str | None = None
        self._errors = 0
        self._last_book: float | None = None
        self._day: int | None = None
        self._baseline: float | None = None
        self._pnl: float | None = None

    @property
    def tripped(self) -> bool:
        return self.reason is not None

    def _trip(self, reason: str) -> None:
        if self.reason is None:
            self.reason = reason

    def on_book(self) -> None:
        self._last_book = self._clock()

    def on_order_result(self, ok: bool) -> None:
        if ok:
            self._errors = 0
            return
        self._errors += 1
        if self._errors >= self._cfg.max_order_errors:
            self._trip("order_errors")

    def update_pnl(self, pnl_usd: float) -> None:
        """pnl_usd: cumulative realized + unrealized - fees. Loss is measured per UTC day."""
        day = int(self._clock() // SECONDS_PER_DAY)
        if self._baseline is None:
            self._baseline = pnl_usd
        elif day != self._day:
            # New UTC day: baseline is the last P&L seen before this update.
            self._baseline = self._pnl if self._pnl is not None else pnl_usd
        self._day = day
        self._pnl = pnl_usd
        if pnl_usd - self._baseline <= -self._cfg.max_daily_loss_usd:
            self._trip("daily_loss")

    def check(self) -> str | None:
        """Poll for staleness. Returns the trip reason if tripped."""
        if self._last_book is not None and self._clock() - self._last_book > self._cfg.stale_s:
            self._trip("stale_book")
        return self.reason
