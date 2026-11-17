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
STRATEGIES = ("taker", "maker")
MAKER_PROMPTS = ("standard", "mainnet")


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


def _whole(env: Mapping[str, str], name: str, default: int) -> int:
    value = _num(env, name, default)
    if value != int(value):
        raise ConfigError(f"{name} must be a whole number, got {env.get(name)!r}")
    return int(value)


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
    # Feed liveness: ping after feed_ping_s of silence; if nothing at all arrives within
    # feed_dead_s more, drop the socket and reconnect. Must finish before stale_s.
    feed_ping_s: float = 4.0
    feed_dead_s: float = 6.0
    # A stale-book kill pauses instead of stopping: trading resumes once books have arrived
    # with no gap over resume_max_gap_s for resume_after_s. At most stale_resumes_max resumes
    # per run; the next stale kill is permanent. (Books come about every 5 s, max seen 6.1 s.)
    resume_after_s: float = 60.0
    resume_max_gap_s: float = 8.0
    stale_resumes_max: int = 5
    model: str = "mock"
    # STRATEGY=maker (milestone 8): post-only quotes about once a second instead of IOC entries.
    # The quote size is fixed; the maker's model deadline replaces deadline_s in maker mode.
    strategy: str = "taker"
    maker_size_usd: float = 11.0
    maker_deadline_s: float = 1.0
    # MAKER_PROMPT=mainnet (milestone 10): the maker prompt gains the mainnet block and the
    # bot reads mainnet's public BTC book (read-only, jevhl.mainnet) for it.
    maker_prompt: str = "standard"
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
        strategy = env.get("STRATEGY") or "taker"
        maker = {}  # read (and checked) only for the maker: a leftover value never stops the taker
        if strategy == "maker":
            maker = {"maker_size_usd": _num(env, "MAKER_SIZE_USD", 11.0),
                     "maker_deadline_s": _num(env, "MAKER_DECIDER_DEADLINE_S", 1.0),
                     "maker_prompt": env.get("MAKER_PROMPT") or "standard"}
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
            feed_ping_s=_num(env, "FEED_PING_S", 4.0),
            feed_dead_s=_num(env, "FEED_DEAD_S", 6.0),
            resume_after_s=_num(env, "RESUME_AFTER_S", 60.0),
            resume_max_gap_s=_num(env, "RESUME_MAX_GAP_S", 8.0),
            stale_resumes_max=_whole(env, "STALE_RESUMES_MAX", 5),
            strategy=strategy,
            api_key=env.get("HL_API_KEY") or None,
            account_address=env.get("HL_ACCOUNT_ADDRESS") or None,
            **maker,
        )
        cfg.validate()
        return cfg

    @property
    def decision_deadline_s(self) -> float:
        """The model's deadline for this strategy (the maker decides about once a second)."""
        return self.maker_deadline_s if self.strategy == "maker" else self.deadline_s

    @property
    def mainnet_prompt(self) -> bool:
        """The maker with MAKER_PROMPT=mainnet: the mainnet block and reader are on."""
        return self.strategy == "maker" and self.maker_prompt == "mainnet"

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
        for name, v in (("FEED_PING_S", self.feed_ping_s), ("FEED_DEAD_S", self.feed_dead_s),
                        ("RESUME_MAX_GAP_S", self.resume_max_gap_s)):
            if not math.isfinite(v) or v <= 0:
                raise ConfigError(f"{name} must be finite and positive")
        if self.feed_ping_s + self.feed_dead_s >= self.stale_s:
            raise ConfigError("FEED_PING_S + FEED_DEAD_S must be below the stale-book bound")
        if self.resume_max_gap_s >= self.stale_s:
            raise ConfigError("RESUME_MAX_GAP_S must be below the stale-book bound")
        if not math.isfinite(self.resume_after_s) or self.resume_after_s < 0:
            raise ConfigError("RESUME_AFTER_S must be finite and >= 0")
        if self.stale_resumes_max < 0:
            raise ConfigError("STALE_RESUMES_MAX must be >= 0")
        if self.strategy not in STRATEGIES:
            raise ConfigError(f"STRATEGY must be one of {STRATEGIES}, got {self.strategy!r}")
        if self.strategy == "maker":  # unused, so never checked, for the taker
            if not math.isfinite(self.maker_deadline_s) or self.maker_deadline_s <= 0:
                raise ConfigError("MAKER_DECIDER_DEADLINE_S must be finite and positive")
            if not math.isfinite(self.maker_size_usd):
                raise ConfigError("MAKER_SIZE_USD must be finite")
            if not self.min_notional_usd <= self.maker_size_usd <= self.max_position_usd:
                raise ConfigError("MAKER_SIZE_USD must be between the minimum notional and "
                                  "MAX_POSITION_USD")
            if self.maker_prompt not in MAKER_PROMPTS:
                raise ConfigError(f"MAKER_PROMPT must be one of {MAKER_PROMPTS}, "
                                  f"got {self.maker_prompt!r}")
            if self.maker_prompt == "mainnet" and self.coin != "BTC":
                raise ConfigError("MAKER_PROMPT=mainnet needs COIN=BTC (the mainnet block "
                                  "shows mainnet BTC)")
