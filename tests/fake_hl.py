"""A fake Hyperliquid testnet: stands in for the SDK's Exchange and Info objects.

Responses copy the documented formats (order statuses filled / error, userFills with string
numbers and side "B"/"A"). No network, no key. Knobs let a test make the venue misbehave.
"""

from __future__ import annotations

FEE_RATE = 0.00045
MAKER_FEE_RATE = 0.00015
ALO = {"limit": {"tif": "Alo"}}
POST_ONLY_ERR = "Post only order would have immediately matched, bbo was {bid}@{ask}. asset=0"
GONE_ERR = "Cannot modify canceled or filled order"
INVALID_MODIFY_ERR = "Attempted to modify to invalid new order"  # a side change (measured)


class HTTP429(Exception):
    """What the SDK raises for an HTTP 429 (its ClientError carries status_code)."""

    status_code = 429
    error_message = "Too Many Requests"


class FakeHL:
    def __init__(self, bids=None, asks=None, t0_ms=1_700_000_000_000):
        self.bids = bids if bids is not None else [[64999.0, 5.0], [64998.0, 5.0]]
        self.asks = asks if asks is not None else [[65001.0, 5.0], [65002.0, 5.0]]
        self.position = 0.0
        self.now_ms = t0_ms
        self.next_oid = 1000
        self.next_tid = 5000
        self.raw_fills: list[dict] = []
        self.resting: list[dict] = []
        self.calls: list[tuple] = []
        self.mid = 65000.0
        # knobs
        self.fail_orders: list[Exception] = []  # popped one per order call
        self.order_response: dict | None = None  # overrides the next response
        self.hide_polls = 0  # fills are invisible for this many fill queries after an order
        self.max_fill_sz: float | None = None  # liquidity cap per order
        self.fail_state: list[Exception] = []  # popped one per user_state call
        self.fail_mids: list[Exception] = []
        self.fail_cancel: list[Exception] = []
        self.fail_fills: list[Exception] = []  # popped one per user_fills_by_time call
        self._avg = 0.0  # average entry price of the open position, for closedPnl
        # maker knobs (milestone 8)
        self.modify_response: dict | None = None  # overrides the next modify response
        self.fail_modify: list[Exception] = []
        self.new_oid_on_modify = True  # a modify answers with a fresh oid (measured 2026-10-11)
        self.rate_limit = {"cumVlm": "1234.5", "nRequestsUsed": 321, "nRequestsCap": 11234}

    def _tick(self) -> int:
        self.now_ms += 1000
        return self.now_ms

    # ---- SDK Exchange ----
    def order(self, name, is_buy, sz, limit_px, order_type, reduce_only=False, cloid=None):
        self.calls.append(("order", name, is_buy, sz, limit_px, order_type, reduce_only))
        if self.fail_orders:
            raise self.fail_orders.pop(0)
        if self.order_response is not None:
            resp, self.order_response = self.order_response, None
            return resp
        if order_type == ALO:
            return self._rest(name, is_buy, sz, limit_px)
        if order_type != {"limit": {"tif": "Ioc"}}:
            return _err_status("only IOC and Alo supported by the fake")
        levels = self.asks if is_buy else self.bids
        want = sz
        if reduce_only:
            room = -self.position if is_buy else self.position
            if room <= 1e-12:
                return _err_status("Reduce only order would increase position.")
            want = min(want, room)
        if self.max_fill_sz is not None:
            want = min(want, self.max_fill_sz)
        oid = self.next_oid
        self.next_oid += 1
        taken, notional = 0.0, 0.0
        for px, lsz in levels:
            if (px > limit_px) if is_buy else (px < limit_px):
                break
            take = min(want - taken, lsz)
            if take <= 1e-12:
                break
            taken += take
            notional += take * px
            self._record(oid, name, is_buy, take, px)
        if taken <= 1e-12:
            return _err_status("Order could not immediately match against any resting orders.")
        return {"status": "ok", "response": {"type": "order", "data": {"statuses": [
            {"filled": {"totalSz": _s(taken), "avgPx": _s(notional / taken), "oid": oid}}
        ]}}}

    def _crosses(self, is_buy, px):
        return px >= self.asks[0][0] if is_buy else px <= self.bids[0][0]

    def _rest(self, name, is_buy, sz, px, oid=None):
        if self._crosses(is_buy, px):
            return _err_status(POST_ONLY_ERR.format(bid=self.bids[0][0], ask=self.asks[0][0]))
        if oid is None:
            oid = self.next_oid
            self.next_oid += 1
        self.resting.append({"coin": name, "side": "B" if is_buy else "A", "limitPx": _s(px),
                             "sz": _s(sz), "oid": oid, "timestamp": self._tick(),
                             "origSz": _s(sz)})
        return {"status": "ok", "response": {"type": "order", "data": {"statuses": [
            {"resting": {"oid": oid}}]}}}

    def modify_order(self, oid, name, is_buy, sz, limit_px, order_type, reduce_only=False,
                     cloid=None):
        self.calls.append(("modify", oid, name, is_buy, sz, limit_px, order_type, reduce_only))
        if self.fail_modify:
            raise self.fail_modify.pop(0)
        if self.modify_response is not None:
            resp, self.modify_response = self.modify_response, None
            return resp
        old = [o for o in self.resting if o["oid"] == oid]
        if not old:
            return _err_status(GONE_ERR)
        if (old[0]["side"] == "B") != is_buy:  # measured: refused, the order stays as it was
            return _err_status(INVALID_MODIFY_ERR)
        self.resting.remove(old[0])  # assumed: a crossing modify leaves no order behind
        return self._rest(name, is_buy, sz, limit_px, None if self.new_oid_on_modify else oid)

    def taker_hits(self, oid, sz):
        """Someone trades against our resting order: a maker fill (crossed False)."""
        (o,) = [o for o in self.resting if o["oid"] == oid]
        left = float(o["sz"]) - sz
        assert left > -1e-12, "hit larger than the order"
        self._record(oid, o["coin"], o["side"] == "B", sz, float(o["limitPx"]), maker=True)
        if left <= 1e-12:
            self.resting.remove(o)
        else:
            o["sz"] = _s(left)

    def user_rate_limit(self, user):
        self.calls.append(("user_rate_limit", user))
        return dict(self.rate_limit)

    def _record(self, oid, coin, is_buy, sz, px, maker=False):
        sign = 1 if is_buy else -1
        pos = self.position
        closed = min(sz, abs(pos)) if pos * sign < 0 else 0.0
        closed_pnl = closed * (px - self._avg) * (1 if pos > 0 else -1)
        if pos * sign >= 0 or abs(pos) < 1e-12:
            self._avg = (abs(pos) * self._avg + sz * px) / (abs(pos) + sz)
        elif sz > closed:
            self._avg = px
        self.position += sz if is_buy else -sz
        tid = self.next_tid
        self.next_tid += 1
        self.raw_fills.append({
            "coin": coin, "px": _s(px), "sz": _s(sz), "side": "B" if is_buy else "A",
            "time": self._tick(), "startPosition": "0.0", "dir": "x", "closedPnl": _s(closed_pnl),
            "hash": "0x0", "oid": oid, "crossed": not maker,
            "fee": _s(px * sz * (MAKER_FEE_RATE if maker else FEE_RATE)),
            "tid": tid, "feeToken": "USDC",
        })
        self.raw_fills[-1]["_hidden_for"] = self.hide_polls

    def bulk_cancel(self, requests):
        self.calls.append(("bulk_cancel", list(requests)))
        if self.fail_cancel:
            raise self.fail_cancel.pop(0)
        gone = {r["oid"] for r in requests}
        self.resting = [o for o in self.resting if o["oid"] not in gone]
        return {"status": "ok", "response": {"type": "cancel", "data": {
            "statuses": ["success"] * len(requests)}}}

    # ---- SDK Info ----
    def user_state(self, address, dex=""):
        self.calls.append(("user_state", address))
        if self.fail_state:
            raise self.fail_state.pop(0)
        positions = []
        if abs(self.position) > 1e-12:
            pos = {"coin": "BTC", "szi": _s(self.position)}
            positions.append({"type": "oneWay", "position": pos})
        return {"assetPositions": positions}

    def open_orders(self, address, dex=""):
        self.calls.append(("open_orders", address))
        return [dict(o) for o in self.resting]

    def all_mids(self, dex=""):
        self.calls.append(("all_mids",))
        if self.fail_mids:
            raise self.fail_mids.pop(0)
        return {"BTC": _s(self.mid), "ETH": "3000.0"}

    def user_fills_by_time(self, address, start_time, end_time=None, aggregate_by_time=False):
        self.calls.append(("user_fills_by_time", address, start_time))
        if self.fail_fills:
            raise self.fail_fills.pop(0)
        out = []
        for f in self.raw_fills:
            if f.get("_hidden_for", 0) > 0:
                continue
            if f["time"] >= start_time:
                out.append({k: v for k, v in f.items() if not k.startswith("_")})
        for f in self.raw_fills:
            if f.get("_hidden_for", 0) > 0:
                f["_hidden_for"] -= 1
        return out


def _s(x: float) -> str:
    return repr(float(x))


def _err_status(msg: str) -> dict:
    return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"error": msg}]}}}
