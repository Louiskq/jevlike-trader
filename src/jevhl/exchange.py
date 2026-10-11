"""Hyperliquid testnet order wrapper over the official SDK (signing and orders only there).

Every method is blocking (HTTP): the trader runs them in a worker thread. Taker orders are
marketable IOC limits; the maker (milestone 8) rests post-only (`Alo`) quotes. An order's
acknowledgement is not trusted for accounting: fills are read back from `userFillsByTime`
(they carry the exchange trade id `tid` and the fee), and only those confirmed fills are
returned to the ledger.

Facts from the Hyperliquid docs (verified 2026-10-04): order response statuses are
`filled {totalSz, avgPx, oid}`, `resting {oid}` or `error`; userFills rows have string numbers,
side "B"/"A", `time` in ms, `fee`, `feeToken`, `tid`, `oid`; info queries need the MAIN
account address, because the API wallet only signs and querying its address returns nothing.
Not verified without an account: the exact text of an IOC "nothing matched" error and whether
reduce-only orders below $10 notional are exempt from the minimum.

Not verified without a live maker run (the parser matches these texts, see
parse_quote_response): the post-only rejection text, the reply to a modify (its statuses, or a
bare `default`) and whether it keeps the oid, the "cannot modify" text, and the rate-limit
error forms.

No transfer or withdrawal code lives here, and only the testnet URL.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from decimal import Decimal

from .config import Config, ConfigError
from .feed import Book
from .paper import limit_price
from .rounding import AssetMeta, aggressive_price, order_size, reduce_size
from .types import OpenQuote, QuoteAck

BASE_URL = "https://api.hyperliquid-testnet.xyz"
HTTP_TIMEOUT_S = 10
MIN_NOTIONAL_MARGIN = 1.05  # entries aim above the $10 minimum so a falling exit stays above it
IOC = {"limit": {"tif": "Ioc"}}
ALO = {"limit": {"tif": "Alo"}}  # post-only: rejected instead of taking liquidity
FILL_OVERLAP_MS = 60_000  # each fill query re-reads the last minute; tid makes that harmless


class ExchangeError(Exception):
    pass


class RateLimited(ExchangeError):
    """The venue refused a request for rate limiting (HTTP 429, or a "too many requests"
    status). The maker cancels and pauses quoting on it."""


class FillNotConfirmed(ExchangeError):
    """The order was acknowledged as filled but its fills never showed up in userFills.
    `fills` holds whatever was confirmed; the periodic fill sync will pick up the rest."""

    def __init__(self, message: str, fills: list[dict]):
        super().__init__(message)
        self.fills = fills


class FlattenFailed(ExchangeError):
    """The position could not be closed. `fills` holds the closing fills that did happen."""

    def __init__(self, message: str, fills: list[dict]):
        super().__init__(message)
        self.fills = fills


class _Ack:
    def __init__(self, oid: int, total_sz: float, resting: bool):
        self.oid, self.total_sz, self.resting = oid, total_sz, resting


def _finite(x: object, what: str) -> float:
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise ExchangeError(f"{what} is not a number: {x!r}") from None
    if not math.isfinite(v):
        raise ExchangeError(f"{what} is not finite: {x!r}")
    return v


def parse_order_response(resp: object) -> _Ack | None:
    """The acknowledgement of one order. None means the IOC matched nothing (a normal
    outcome, not an error). Anything unexpected raises."""
    if not isinstance(resp, dict) or resp.get("status") != "ok":
        detail = resp.get("response") if isinstance(resp, dict) else resp
        raise ExchangeError(f"order rejected: {detail!r}")
    try:
        statuses = resp["response"]["data"]["statuses"]
        if len(statuses) != 1 or not isinstance(statuses[0], dict):
            raise KeyError("statuses")
        st = statuses[0]
    except (KeyError, TypeError):
        raise ExchangeError(f"unexpected order response: {resp!r}") from None
    if "error" in st:
        if "immediately match" in str(st["error"]):
            return None
        raise ExchangeError(f"order error: {st['error']}")
    if "resting" in st:
        return _Ack(int(st["resting"]["oid"]), 0.0, resting=True)
    if "filled" in st:
        f = st["filled"]
        if not isinstance(f, dict) or "oid" not in f:
            raise ExchangeError(f"unexpected filled status: {st!r}")
        _finite(f.get("avgPx"), "avgPx")
        return _Ack(int(f["oid"]), _finite(f.get("totalSz"), "totalSz"), resting=False)
    raise ExchangeError(f"unexpected order status: {st!r}")


def _rate_text(text: str) -> bool:
    t = text.lower()
    return "too many" in t or "rate limit" in t


def parse_quote_response(resp: object) -> QuoteAck:
    """The answer to one post-only order or one modify. A post-only rejection or a gone order
    is a normal outcome (an ack, not an error); a rate limit raises RateLimited; anything else
    unexpected raises ExchangeError."""
    if not isinstance(resp, dict) or resp.get("status") != "ok":
        detail = repr(resp.get("response") if isinstance(resp, dict) else resp)
        if _rate_text(detail):
            raise RateLimited(f"quote refused: {detail}")
        raise ExchangeError(f"quote refused: {detail}")
    response = resp.get("response")
    if isinstance(response, dict) and response.get("type") == "default":
        return QuoteAck("unknown", None, "reply without an order status")
    try:
        statuses = response["data"]["statuses"]  # type: ignore[index]
        if len(statuses) != 1:
            raise KeyError("statuses")
        st = statuses[0]
    except (KeyError, TypeError):
        raise ExchangeError(f"unexpected quote response: {resp!r}") from None
    if isinstance(st, str):  # e.g. "success": no order status to read
        return QuoteAck("unknown", None, st)
    if not isinstance(st, dict):
        raise ExchangeError(f"unexpected quote status: {st!r}")
    try:
        if "error" in st:
            err = str(st["error"])
            low = err.lower()
            if "post only" in low and "immediately match" in low:
                return QuoteAck("rejected", None, err)
            if "canceled or filled" in low or "already canceled" in low:
                return QuoteAck("gone", None, err)
            if _rate_text(err):
                raise RateLimited(err)
            raise ExchangeError(f"quote error: {err}")
        if "resting" in st:
            return QuoteAck("placed", int(st["resting"]["oid"]), "")
        if "filled" in st:
            return QuoteAck("filled", int(st["filled"]["oid"]), "post-only order took liquidity")
    except (KeyError, TypeError, ValueError):
        raise ExchangeError(f"unexpected quote status: {st!r}") from None
    raise ExchangeError(f"unexpected quote status: {st!r}")


def parse_fill(raw: dict) -> dict:
    """One userFills row as the fill dict the ledger and store use."""
    try:
        side = {"B": "buy", "A": "sell"}[raw["side"]]
        fill = {
            "tid": int(raw["tid"]), "oid": int(raw["oid"]), "side": side,
            "sz": _finite(raw["sz"], "sz"), "px": _finite(raw["px"], "px"),
            "fee": _finite(raw["fee"], "fee"), "ts": _finite(raw["time"], "time") / 1000.0,
            "closed_pnl": _finite(raw["closedPnl"], "closedPnl"),  # gross of fees
        }
        token = raw.get("feeToken", "USDC")
    except (KeyError, TypeError, ValueError):
        raise ExchangeError(f"malformed fill: {raw!r}") from None
    if fill["sz"] <= 0 or fill["px"] <= 0:
        raise ExchangeError(f"malformed fill: {raw!r}")
    if str(token).strip() != "USDC":
        raise ExchangeError(f"fee not in USDC: {raw!r}")
    return fill


def fetch_position(info, address: str, coin: str) -> float:
    """Signed position in base units from the exchange's own account state."""
    state = info.user_state(address)
    try:
        for ap in state["assetPositions"]:
            if ap["position"]["coin"] == coin:
                return _finite(ap["position"]["szi"], "szi")
    except (KeyError, TypeError):
        raise ExchangeError(f"unexpected user state: {state!r}") from None
    return 0.0


def fetch_fills(info, address: str, coin: str, start_ms: int) -> list[dict]:
    rows = info.user_fills_by_time(address, int(start_ms))
    return [parse_fill(r) for r in rows if r.get("coin") == coin]


class Exchange:
    live = True  # blocking calls (run in a thread); fills are stored by the caller

    def __init__(
        self,
        sdk,
        info,
        address: str,
        meta: AssetMeta,
        coin: str,
        *,
        since_ms: int,
        min_notional: float = Config.min_notional_usd,
        sleep: Callable[[float], None] = time.sleep,
        confirm_tries: int = 6,
        confirm_wait_s: float = 0.5,
        flatten_tries: int = 3,
        flatten_wait_s: float = 1.0,
    ):
        """sdk: the SDK's Exchange (order, bulk_cancel). info: its Info (reads). address: the
        MAIN account's address. since_ms: where the first fill query starts."""
        self._sdk, self._info, self._address = sdk, info, address
        self.meta, self.coin, self._min_notional = meta, coin, min_notional
        self._since_ms = int(since_ms)
        self._sleep = sleep
        self._confirm_tries, self._confirm_wait_s = confirm_tries, confirm_wait_s
        self._flatten_tries, self._flatten_wait_s = flatten_tries, flatten_wait_s
        self._dust = 10.0 ** -meta.sz_decimals / 2

    # ---- orders ----

    def open(self, book: Book, side: str, usd: float, ts: float) -> list[dict]:
        """Entry: IOC for a target notional, sized at the limit price (the price the exchange
        checks the minimum against) with a margin above the $10 minimum."""
        limit = limit_price(side, book, self.meta)
        floor = self._min_notional * MIN_NOTIONAL_MARGIN
        if usd < self._min_notional or not math.isfinite(usd):
            raise ValueError(f"notional {usd!r} is below the minimum {self._min_notional}")
        sz = order_size(max(usd, floor), float(limit), self.meta, floor)
        return self._place(side, sz, limit, reduce_only=False)

    def close(self, book: Book, side: str, size: float, position: float, ts: float) -> list[dict]:
        """Reduce-only IOC: never larger than the position, never in the growing direction."""
        sz = reduce_size(side, size, position, self.meta)
        if sz <= 0:
            return []
        return self._place(side, sz, limit_price(side, book, self.meta), reduce_only=True)

    def _place(self, side: str, sz: Decimal, limit: Decimal, reduce_only: bool) -> list[dict]:
        resp = self._sdk.order(
            self.coin, side == "buy", float(sz), float(limit), IOC, reduce_only
        )
        ack = parse_order_response(resp)
        if ack is None:
            return []
        if ack.resting:  # an IOC must never rest; if it did, take it off the book
            self.cancel_all()
            raise ExchangeError(f"IOC order {ack.oid} rested")
        return self._confirm(ack)

    def _confirm(self, ack: _Ack) -> list[dict]:
        fills: list[dict] = []
        for attempt in range(self._confirm_tries):
            if attempt:
                self._sleep(self._confirm_wait_s)
            fills = [f for f in self.sync_fills() if f["oid"] == ack.oid]
            if ack.total_sz - sum(f["sz"] for f in fills) < self._dust:
                return fills
        raise FillNotConfirmed(
            f"order {ack.oid}: acknowledged {ack.total_sz}, confirmed "
            f"{sum(f['sz'] for f in fills)}", fills,
        )

    def cancel_all(self) -> int:
        """Cancel every resting order on this coin. Returns how many were cancelled."""
        mine = [o for o in self._info.open_orders(self._address) if o.get("coin") == self.coin]
        if not mine:
            return 0
        resp = self._sdk.bulk_cancel([{"coin": self.coin, "oid": o["oid"]} for o in mine])
        if not isinstance(resp, dict) or resp.get("status") != "ok":
            raise ExchangeError(f"cancel failed: {resp!r}")
        return len(mine)

    def flatten(self, book: Book | None, position: float, ts: float) -> list[dict]:
        """Cancel everything, then close the EXCHANGE's position (not the caller's belief)
        with reduce-only IOCs priced from a fresh mid, retrying; `book` and `position` are
        accepted for the executor interface and unused. Raises FlattenFailed carrying the
        fills so far if the position is still open after the last attempt."""
        fills: list[dict] = []
        err: Exception | None = None
        for attempt in range(self._flatten_tries + 1):
            try:
                self.cancel_all()
                pos = fetch_position(self._info, self._address, self.coin)
                if abs(pos) < self._dust:
                    return fills
                if attempt == self._flatten_tries:
                    err = ExchangeError(f"position {pos} still open")
                    break
                side = "sell" if pos > 0 else "buy"
                ref = _finite(self._info.all_mids()[self.coin], "mid")
                # snap to a lot first: a float position of 0.0000099999 must not floor to nothing
                sz = reduce_size(side, round(abs(pos), self.meta.sz_decimals), pos, self.meta)
                fills += self._place(
                    side, sz, aggressive_price(side, ref, self.meta), reduce_only=True
                )
            except FillNotConfirmed as e:
                fills += e.fills
                err = e
            except Exception as e:
                err = e
            if attempt < self._flatten_tries:
                self._sleep(self._flatten_wait_s)
        raise FlattenFailed(f"could not flatten {self.coin}: {err}", fills)

    # ---- maker quotes (milestone 8) ----

    def place_quote(self, side: str, sz: Decimal, px: Decimal, book: Book | None,
                    ts: float) -> QuoteAck:
        """One post-only limit order (never reduce-only). book and ts are for the executor
        interface (the paper venue needs them) and unused here."""
        return self._quote_call(
            lambda: self._sdk.order(self.coin, side == "buy", float(sz), float(px), ALO, False)
        )

    def modify_quote(self, oid: int, side: str, sz: Decimal, px: Decimal, book: Book | None,
                     ts: float) -> QuoteAck:
        """Move a resting quote (one batchModify action; it can change price and side)."""
        return self._quote_call(lambda: self._sdk.modify_order(
            oid, self.coin, side == "buy", float(sz), float(px), ALO
        ))

    def _quote_call(self, send: Callable[[], object]) -> QuoteAck:
        try:
            resp = send()
        except Exception as e:
            if getattr(e, "status_code", None) == 429:  # the SDK's ClientError
                raise RateLimited(f"HTTP 429: {getattr(e, 'error_message', e)}") from None
            raise
        return parse_quote_response(resp)

    def cancel_oids(self, oids: list[int]) -> int:
        """Cancel these orders on this coin (one bulk cancel, weight 1 under 40 orders)."""
        if not oids:
            return 0
        resp = self._sdk.bulk_cancel([{"coin": self.coin, "oid": o} for o in oids])
        if not isinstance(resp, dict) or resp.get("status") != "ok":
            raise ExchangeError(f"cancel failed: {resp!r}")
        return len(oids)

    def open_quotes(self) -> list[OpenQuote]:
        """Our resting orders on this coin."""
        out = []
        for o in self._info.open_orders(self._address):
            if o.get("coin") != self.coin:
                continue
            try:
                side = {"B": "buy", "A": "sell"}[o["side"]]
                out.append(OpenQuote(int(o["oid"]), side, _finite(o["limitPx"], "limitPx"),
                                     _finite(o["sz"], "sz")))
            except (KeyError, TypeError, ValueError):
                raise ExchangeError(f"malformed open order: {o!r}") from None
        return out

    def rate_budget(self) -> dict:
        """The address's request budget (requests used and allowed so far)."""
        r = self._info.user_rate_limit(self._address)
        if not isinstance(r, dict):
            raise ExchangeError(f"unexpected userRateLimit reply: {r!r}")
        return {"used": r.get("nRequestsUsed"), "cap": r.get("nRequestsCap")}

    # ---- reads ----

    def position(self) -> float:
        return fetch_position(self._info, self._address, self.coin)

    def sync_fills(self) -> list[dict]:
        """This coin's fills since the watermark (with a minute of overlap, so the caller
        must dedupe on tid). The watermark only moves forward."""
        fills = fetch_fills(self._info, self._address, self.coin, self._since_ms)
        if fills:
            newest = int(max(f["ts"] for f in fills) * 1000)
            self._since_ms = max(self._since_ms, newest - FILL_OVERLAP_MS)
        return fills


def build_exchange(
    cfg: Config, meta: AssetMeta, since_ms: int, *, exchange_cls=None
) -> Exchange:
    """The real thing: SDK Exchange on testnet, signing as the API wallet from HL_API_KEY and
    reading as HL_ACCOUNT_ADDRESS. exchange_cls is for tests."""
    if not cfg.api_key:
        raise ConfigError("HL_API_KEY is required for live trading (API wallet key, in .env)")
    if not cfg.account_address:
        raise ConfigError("HL_ACCOUNT_ADDRESS is required for live trading (your main address)")
    from eth_account import Account

    try:
        wallet = Account.from_key(cfg.api_key)
    except Exception:  # the message could echo key material: say nothing about it
        raise ConfigError("HL_API_KEY is not a valid private key") from None
    if exchange_cls is None:
        from hyperliquid.exchange import Exchange as SDKExchange

        exchange_cls = SDKExchange
    sdk = exchange_cls(
        wallet, base_url=BASE_URL, account_address=cfg.account_address, timeout=HTTP_TIMEOUT_S
    )
    return Exchange(
        sdk, sdk.info, cfg.account_address, meta, cfg.coin, since_ms=since_ms,
        min_notional=cfg.min_notional_usd,
    )
