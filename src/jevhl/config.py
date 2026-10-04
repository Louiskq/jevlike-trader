"""Configuration from environment. Testnet only: any other network is rejected."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field


class ConfigError(ValueError):
    pass


def _num(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None


@dataclass(frozen=True)
class Config:
    network: str = "testnet"
    coin: str = "BTC"
    gate: float = 0.60
    horizon_s: float = 60
    deadline_s: float = 2.0
    max_position_usd: float = 500
    max_daily_loss_usd: float = 100
    min_notional_usd: float = 10
    full_size_p: float = 0.80
    stale_s: float = 15
    max_order_errors: int = 3
    api_key: str | None = field(default=None, repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        network = env.get("NETWORK", "testnet")
        if network != "testnet":
            raise ConfigError(f"only NETWORK=testnet is supported, got {network!r}")
        cfg = cls(
            network=network,
            coin=env.get("COIN") or "BTC",
            gate=_num(env, "GATE", 0.60),
            horizon_s=_num(env, "HORIZON_S", 60),
            deadline_s=_num(env, "DECIDER_DEADLINE_S", 2.0),
            max_position_usd=_num(env, "MAX_POSITION_USD", 500),
            max_daily_loss_usd=_num(env, "MAX_DAILY_LOSS_USD", 100),
            api_key=env.get("HL_API_KEY") or None,
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not 0.5 < self.gate < 1:
            raise ConfigError("GATE must be in (0.5, 1)")
        if self.gate >= self.full_size_p:
            raise ConfigError("GATE must be below the full-size probability")
        if self.horizon_s <= 0:
            raise ConfigError("HORIZON_S must be positive")
        if self.deadline_s <= 0:
            raise ConfigError("DECIDER_DEADLINE_S must be positive")
        if self.max_position_usd < self.min_notional_usd:
            raise ConfigError("MAX_POSITION_USD must be at least the minimum notional")
        if self.max_daily_loss_usd <= 0:
            raise ConfigError("MAX_DAILY_LOSS_USD must be positive")
