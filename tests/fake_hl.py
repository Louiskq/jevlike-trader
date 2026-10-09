"""A fake Hyperliquid testnet: stands in for the SDK's Exchange and Info objects.

Responses copy the documented formats (order statuses filled / error, userFills with string
numbers and side "B"/"A"). No network, no key. Knobs let a test make the venue misbehave.
"""

from __future__ import annotations

FEE_RATE = 0.00045


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
        if order_type != {"limit": {"tif": "Ioc"}}:
            return _err_status("only IOC supported by the fake")
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

    def _record(self, oid, coin, is_buy, sz, px):
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
            "hash": "0x0", "oid": oid, "crossed": True, "fee": _s(px * sz * FEE_RATE),
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
