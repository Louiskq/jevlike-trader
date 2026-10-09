"""Configuration from environment. Testnet only: any other network is rejected."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
_LOOPBACK = {"127.0.0.1", "localhost", "::1"}
MODELS = ("mock", "decider")


class ConfigError(ValueError):
    pass


def _num(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from None
    if not math.isfinite(value):
        raise ConfigError(f"{name} must be finite, got {raw!r}")
    return value


def parse_decider_url(raw: str) -> str:
    """The decider is reached through an SSH tunnel, so only plain http to loopback is
    accepted. Returns the URL without a trailing slash."""
    url = raw.strip()
    if not url:
        raise ConfigError("DECIDER_URL is required when MODEL=decider")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise ConfigError(f"DECIDER_URL is not a valid URL: {raw!r}") from None
    if parts.scheme != "http":
        raise ConfigError("DECIDER_URL must be http:// (it points at a local tunnel)")
    if parts.hostname not in _LOOPBACK:
        raise ConfigError("DECIDER_URL must point at loopback (127.0.0.1, localhost or [::1])")
    extras = (parts.username, parts.password, parts.query, parts.fragment)
    if any(extras) or parts.path not in ("", "/"):
        raise ConfigError("DECIDER_URL must be just http://host:port")
    if port is not None and port == 0:
        raise ConfigError("DECIDER_URL port must be 1-65535")
    return url.rstrip("/")


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
    # Keep retrying a failed live flatten for this long; 0 = once. The window bounds when an
    # attempt may START, not when the retry ends: an attempt begun just inside it can overrun
    # by up to one attempt (about 43 s with the default exchange timeouts and tries).
    flatten_retry_window_s: float = 600
    model: str = "mock"
    decider_url: str | None = None
    api_key: str | None = field(default=None, repr=False)
    # The MAIN account's public address (not the API wallet's): the API wallet only signs, and
    # asking info endpoints about the agent address returns nothing. Not a secret.
    account_address: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        network = env.get("NETWORK", "testnet")
        if network != "testnet":
            raise ConfigError(f"only NETWORK=testnet is supported, got {network!r}")
        model = env.get("MODEL") or "mock"
        decider_url = None
        if model == "decider":
            decider_url = parse_decider_url(env.get("DECIDER_URL", ""))
        cfg = cls(
            network=network,
            model=model,
            decider_url=decider_url,
            coin=env.get("COIN") or "BTC",
            gate=_num(env, "GATE", 0.60),
            horizon_s=_num(env, "HORIZON_S", 60),
            deadline_s=_num(env, "DECIDER_DEADLINE_S", 2.0),
            max_position_usd=_num(env, "MAX_POSITION_USD", 500),
            max_daily_loss_usd=_num(env, "MAX_DAILY_LOSS_USD", 100),
            flatten_retry_window_s=_num(env, "FLATTEN_RETRY_WINDOW_S", 600),
            api_key=env.get("HL_API_KEY") or None,
            account_address=env.get("HL_ACCOUNT_ADDRESS") or None,
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.account_address is not None and not _ADDRESS.fullmatch(self.account_address):
            raise ConfigError("HL_ACCOUNT_ADDRESS must be 0x followed by 40 hex digits")
        if self.model not in MODELS:
            raise ConfigError(f"MODEL must be one of {MODELS}, got {self.model!r}")
        if self.model == "decider":
            parse_decider_url(self.decider_url or "")
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
        if not math.isfinite(self.flatten_retry_window_s) or self.flatten_retry_window_s < 0:
            raise ConfigError("FLATTEN_RETRY_WINDOW_S must be finite and >= 0")
