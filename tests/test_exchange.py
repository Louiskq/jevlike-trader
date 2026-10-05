"""The testnet exchange wrapper, against a fake venue (no network, no key)."""

from decimal import Decimal
from pathlib import Path

import pytest

from fake_hl import FakeHL
from jevhl import exchange as exchange_mod
from jevhl.config import Config, ConfigError
from jevhl.exchange import (
    Exchange,
    ExchangeError,
    FillNotConfirmed,
    FlattenFailed,
    build_exchange,
    parse_fill,
    parse_order_response,
)
from jevhl.feed import Book
from jevhl.paper import limit_price
from jevhl.rounding import AssetMeta

BTC = AssetMeta("BTC", 5)
ADDR = "0x" + "ab" * 20


def mkbook(bid=64999.0, ask=65001.0):
    return Book(ts=1.0, recv_ts=1.0, coin="BTC", bids=[[bid, 5.0]], asks=[[ask, 5.0]])


class Sleeps:
    def __init__(self):
        self.calls = []

    def __call__(self, s):
        self.calls.append(s)


def make(fake=None, **kw):
    fake = fake or FakeHL()
    sleeps = Sleeps()
    ex = Exchange(
        fake, fake, ADDR, BTC, "BTC", since_ms=fake.now_ms - 60_000, sleep=sleeps, **kw
    )
    return ex, fake, sleeps


def orders(fake):
    return [c for c in fake.calls if c[0] == "order"]


# ---- placing orders ----


def test_open_buy_is_an_ioc_limit_through_the_ask_and_returns_confirmed_fills():
    ex, fake, _ = make()
    fills = ex.open(mkbook(), "buy", 100.0, ts=1.0)
    (_, name, is_buy, sz, limit_px, otype, reduce_only) = orders(fake)[0]
    assert (name, is_buy, otype, reduce_only) == ("BTC", True, {"limit": {"tif": "Ioc"}}, False)
    assert limit_px == float(limit_price("buy", mkbook(), BTC))  # 65001 * 1.005, rounded up
    assert sz == float(Decimal("0.00153"))  # 100 / 65326 (limit), floored to a lot
    (f,) = fills
    assert (f["side"], f["sz"], f["px"], f["oid"]) == ("buy", sz, 65001.0, 1000)
    assert f["tid"] == 5000 and f["fee"] == pytest.approx(65001.0 * sz * 0.00045)
    assert f["ts"] == pytest.approx(fake.raw_fills[0]["time"] / 1000)


def test_open_sell_goes_through_the_bid():
    ex, fake, _ = make()
    (f,) = ex.open(mkbook(), "sell", 100.0, ts=1.0)
    assert orders(fake)[0][2] is False and f["side"] == "sell" and f["px"] == 64999.0


def test_entry_size_is_computed_at_the_limit_price_not_the_touch():
    ex, fake, _ = make()
    ex.open(mkbook(), "buy", 100.2, ts=1.0)
    # 100.2 / 65326 (limit) = 0.001534 -> 0.00153; at the touch (65001) it would be 0.00154
    assert orders(fake)[0][3] == 0.00153


def test_entry_notional_clears_the_minimum_at_the_limit_price_with_a_margin():
    ex, fake, _ = make()
    ex.open(mkbook(), "sell", 10.0, ts=1.0)
    (_, _, _, sz, limit_px, *_) = orders(fake)[0]
    assert sz * limit_px >= 10.0 * 1.05 - 1e-9  # sell limit is 50 bps below the bid


def test_open_rejects_below_minimum_notional():
    ex, fake, _ = make()
    with pytest.raises(ValueError):
        ex.open(mkbook(), "buy", 5.0, ts=1.0)
    assert not orders(fake)


def test_ioc_that_matches_nothing_is_not_an_error():
    fake = FakeHL(asks=[[70000.0, 5.0]])  # far above the limit
    ex, _, _ = make(fake)
    assert ex.open(mkbook(), "buy", 100.0, ts=1.0) == []


def test_any_other_order_error_raises():
    ex, fake, _ = make()
    fake.order_response = {"status": "ok", "response": {"type": "order", "data": {"statuses": [
        {"error": "Order must have minimum value of $10."}]}}}
    with pytest.raises(ExchangeError, match="minimum value"):
        ex.open(mkbook(), "buy", 100.0, ts=1.0)


def test_non_ok_status_raises():
    ex, fake, _ = make()
    fake.order_response = {"status": "err", "response": "User or API Wallet does not exist."}
    with pytest.raises(ExchangeError, match="does not exist"):
        ex.open(mkbook(), "buy", 100.0, ts=1.0)


def test_a_resting_ioc_is_cancelled_and_reported():
    ex, fake, _ = make()
    fake.order_response = {"status": "ok", "response": {"type": "order", "data": {"statuses": [
        {"resting": {"oid": 77}}]}}}
    fake.resting = [{"oid": 77, "coin": "BTC"}]
    with pytest.raises(ExchangeError, match="rested"):
        ex.open(mkbook(), "buy", 100.0, ts=1.0)
    assert fake.resting == []


def test_transport_errors_propagate_so_the_caller_counts_them():
    ex, fake, _ = make()
    fake.fail_orders = [ConnectionError("wifi off")]
    with pytest.raises(ConnectionError):
        ex.open(mkbook(), "buy", 100.0, ts=1.0)


@pytest.mark.parametrize("resp", [
    None, [], {"status": "ok"}, {"status": "ok", "response": {"data": {"statuses": []}}},
    {"status": "ok", "response": {"data": {"statuses": [{"filled": {"totalSz": "nan",
                                                                    "avgPx": "1", "oid": 1}}]}}},
    {"status": "ok", "response": {"data": {"statuses": [{"filled": {"totalSz": "1",
                                                                    "avgPx": "1"}}]}}},
    {"status": "ok", "response": {"data": {"statuses": [{"weird": 1}]}}},
])
def test_malformed_order_responses_raise(resp):
    with pytest.raises(ExchangeError):
        parse_order_response(resp)


# ---- confirmation ----


def test_open_waits_for_the_fill_to_appear_in_user_fills():
    fake = FakeHL()
    fake.hide_polls = 2
    ex, _, sleeps = make(fake)
    (f,) = ex.open(mkbook(), "buy", 100.0, ts=1.0)
    assert f["tid"] == 5000
    assert len(sleeps.calls) == 2  # polled three times, waited twice


def test_unconfirmed_fill_raises_but_carries_what_was_seen():
    fake = FakeHL()
    fake.hide_polls = 100
    ex, _, sleeps = make(fake, confirm_tries=4)
    with pytest.raises(FillNotConfirmed) as e:
        ex.open(mkbook(), "buy", 100.0, ts=1.0)
    assert e.value.fills == [] and len(sleeps.calls) == 3


def test_partially_confirmed_fill_carries_the_confirmed_part():
    fake = FakeHL(asks=[[65001.0, 0.001], [65002.0, 5.0]])
    ex, _, _ = make(fake, confirm_tries=2)
    real = fake.user_fills_by_time

    def drop_last(*a, **k):  # the second fill is not visible yet
        return real(*a, **k)[:1]

    fake.user_fills_by_time = drop_last
    with pytest.raises(FillNotConfirmed) as e:
        ex.open(mkbook(), "buy", 100.0, ts=1.0)
    assert [f["sz"] for f in e.value.fills] == [0.001]


def test_confirmation_ignores_other_orders_fills():
    ex, fake, _ = make()
    ex.open(mkbook(), "buy", 100.0, ts=1.0)
    fills = ex.close(mkbook(), "sell", 0.00153, position=0.00153, ts=2.0)
    assert [f["oid"] for f in fills] == [1001] and fills[0]["side"] == "sell"


# ---- closing, cancelling, position ----


def test_close_is_reduce_only_and_clamped_to_the_position():
    fake = FakeHL()
    fake.position = 0.5
    ex, _, _ = make(fake)
    ex.close(mkbook(), "sell", 3.0, position=0.00016, ts=1.0)
    (_, _, is_buy, sz, limit_px, _, reduce_only) = orders(fake)[0]
    assert (is_buy, sz, reduce_only) == (False, 0.00016, True)
    assert limit_px == float(limit_price("sell", mkbook(), BTC))


def test_close_snaps_float_position_residue_to_the_lot():
    ex, fake, _ = make()
    fake.position = 0.00016
    ex.close(mkbook(), "sell", 0.00016, position=0.00007 + 0.00009, ts=1.0)
    assert orders(fake)[0][3] == 0.00016


def test_close_of_a_flat_or_wrong_way_position_places_nothing():
    ex, fake, _ = make()
    assert ex.close(mkbook(), "sell", 1.0, position=0.0, ts=1.0) == []
    assert ex.close(mkbook(), "sell", 1.0, position=-1.0, ts=1.0) == []
    assert not orders(fake)


def test_cancel_all_cancels_only_this_coin():
    ex, fake, _ = make()
    fake.resting = [{"oid": 1, "coin": "BTC"}, {"oid": 2, "coin": "ETH"}, {"oid": 3, "coin": "BTC"}]
    assert ex.cancel_all() == 2
    assert fake.resting == [{"oid": 2, "coin": "ETH"}]
    (_, reqs) = [c for c in fake.calls if c[0] == "bulk_cancel"][0]
    assert reqs == [{"coin": "BTC", "oid": 1}, {"coin": "BTC", "oid": 3}]


def test_cancel_all_with_nothing_resting_sends_nothing():
    ex, fake, _ = make()
    assert ex.cancel_all() == 0
    assert not [c for c in fake.calls if c[0] == "bulk_cancel"]


def test_cancel_failure_status_raises():
    ex, fake, _ = make()
    fake.resting = [{"oid": 1, "coin": "BTC"}]
    fake.bulk_cancel = lambda reqs: {"status": "err", "response": "nope"}
    with pytest.raises(ExchangeError):
        ex.cancel_all()


def test_position_reads_the_account_address_not_the_agent():
    ex, fake, _ = make()
    fake.position = -0.25
    assert ex.position() == -0.25
    assert ("user_state", ADDR) in fake.calls
    fake.position = 0.0
    assert ex.position() == 0.0


def test_non_finite_position_raises():
    ex, fake, _ = make()
    fake.user_state = lambda a, dex="": {"assetPositions": [
        {"position": {"coin": "BTC", "szi": "nan"}}]}
    with pytest.raises(ExchangeError):
        ex.position()


# ---- fills ----


def raw(**kw):
    f = {"coin": "BTC", "px": "65001.0", "sz": "0.00153", "side": "B", "time": 1_700_000_005_000,
         "oid": 9, "fee": "0.044", "tid": 77, "feeToken": "USDC", "hash": "0x0", "crossed": True,
         "closedPnl": "0.0"}
    f.update(kw)
    return f


def test_parse_fill_maps_the_documented_fields():
    f = parse_fill(raw())
    assert f == {"tid": 77, "oid": 9, "side": "buy", "sz": 0.00153, "px": 65001.0,
                 "fee": 0.044, "ts": 1_700_000_005.0, "closed_pnl": 0.0}
    assert parse_fill(raw(closedPnl="-1.25"))["closed_pnl"] == -1.25
    assert parse_fill(raw(side="A", fee="-0.002"))["side"] == "sell"  # a rebate is negative
    assert parse_fill(raw(side="A", fee="-0.002"))["fee"] == -0.002


@pytest.mark.parametrize("bad", [
    {"closedPnl": "nan"}, {"px": "nan"}, {"sz": "inf"}, {"fee": "NaN"}, {"sz": "0"},
    {"px": "-1"}, {"side": "X"},
    {"tid": None}, {"feeToken": "HYPE"}, {"time": "soon"}, {"sz": "abc"},
])
def test_parse_fill_rejects_bad_data(bad):
    with pytest.raises(ExchangeError):
        parse_fill(raw(**bad))


def test_sync_fills_returns_this_coins_fills_only():
    ex, fake, _ = make()
    fake.raw_fills = [raw(tid=1), raw(tid=2, coin="ETH"), raw(tid=3, side="A")]
    assert [f["tid"] for f in ex.sync_fills()] == [1, 3]


def test_sync_fills_start_time_advances_with_an_overlap():
    ex, fake, _ = make()
    fake.raw_fills = [raw(tid=1, time=fake.now_ms + 500_000)]
    ex.sync_fills()
    ex.sync_fills()
    starts = [c[2] for c in fake.calls if c[0] == "user_fills_by_time"]
    assert starts[0] == fake.now_ms - 60_000
    assert starts[1] == fake.now_ms + 500_000 - 60_000  # watermark moved, 60 s overlap kept


def test_sync_fills_malformed_fill_raises_rather_than_dropping_it():
    ex, fake, _ = make()
    fake.raw_fills = [raw(px="nan")]
    with pytest.raises(ExchangeError):
        ex.sync_fills()


def test_sync_fills_never_goes_backwards():
    ex, fake, _ = make()
    fake.raw_fills = [raw(tid=1, time=fake.now_ms + 500_000)]
    ex.sync_fills()
    mark = fake.now_ms + 500_000 - 60_000
    fake.raw_fills = [raw(tid=2, time=mark + 30_000)]  # newer than the mark, 30 s after it
    ex.sync_fills()
    ex.sync_fills()
    starts = [c[2] for c in fake.calls if c[0] == "user_fills_by_time"]
    assert starts[1] == starts[2] == mark


# ---- flatten ----


def test_flatten_closes_the_exchange_position_not_the_callers_belief():
    fake = FakeHL()
    fake.position = 0.003
    ex, _, _ = make(fake)
    fills = ex.flatten(mkbook(), position=0.001, ts=1.0)  # caller thinks 0.001
    assert fake.position == 0.0 and sum(f["sz"] for f in fills) == pytest.approx(0.003)
    assert len(orders(fake)) == 1  # sized from the exchange's 0.003, in one go
    (_, _, is_buy, _, limit_px, _, reduce_only) = orders(fake)[0]
    assert (is_buy, reduce_only) == (False, True)
    assert limit_px == 64675.0  # mid 65000 * 0.995 rounded down: priced from a fresh mid


def test_flatten_cancels_resting_orders_first():
    fake = FakeHL()
    fake.position = -0.002
    fake.resting = [{"oid": 5, "coin": "BTC"}]
    ex, _, _ = make(fake)
    ex.flatten(mkbook(), -0.002, 1.0)
    names = [c[0] for c in fake.calls]
    assert names.index("bulk_cancel") < names.index("order")
    assert orders(fake)[0][2] is True  # buy to close a short


def test_flatten_when_already_flat_places_nothing():
    ex, fake, _ = make()
    assert ex.flatten(mkbook(), 0.0, 1.0) == []
    assert not orders(fake)


def test_flatten_retries_after_a_failed_attempt():
    fake = FakeHL()
    fake.position = 0.002
    fake.fail_orders = [ConnectionError("blip")]
    ex, _, sleeps = make(fake)
    fills = ex.flatten(mkbook(), 0.002, 1.0)
    assert fake.position == 0.0 and fills and len(orders(fake)) == 2 and sleeps.calls


def test_flatten_retries_a_failed_position_read_and_a_failed_mid_read():
    fake = FakeHL()
    fake.position = 0.002
    fake.fail_state = [TimeoutError("slow")]
    fake.fail_mids = [TimeoutError("slow")]
    ex, _, _ = make(fake)
    ex.flatten(mkbook(), 0.002, 1.0)
    assert fake.position == 0.0


def test_flatten_keeps_going_after_a_partial_fill():
    fake = FakeHL()
    fake.position = 0.003
    fake.max_fill_sz = 0.001
    ex, _, _ = make(fake, flatten_tries=5)
    fills = ex.flatten(mkbook(), 0.003, 1.0)
    assert fake.position == pytest.approx(0.0, abs=1e-9)
    assert len(orders(fake)) == 3 and sum(f["sz"] for f in fills) == pytest.approx(0.003)


def test_flatten_returns_the_fills_it_confirmed_even_if_the_rest_was_not():
    fake = FakeHL(bids=[[64999.0, 0.001], [64998.0, 5.0]])
    fake.position = 0.002
    ex, _, _ = make(fake, confirm_tries=1, flatten_tries=1)
    real = fake.user_fills_by_time
    fake.user_fills_by_time = lambda *a, **k: real(*a, **k)[:1]  # the 2nd fill is not visible
    fills = ex.flatten(mkbook(), 0.002, 1.0)
    assert [f["sz"] for f in fills] == [0.001] and fake.position == pytest.approx(0.0, abs=1e-12)


def test_flatten_gives_up_loudly_with_the_fills_it_did_get():
    fake = FakeHL()
    fake.position = 0.003
    fake.max_fill_sz = 0.001
    ex, _, _ = make(fake, flatten_tries=2)
    with pytest.raises(FlattenFailed) as e:
        ex.flatten(mkbook(), 0.003, 1.0)
    assert len(orders(fake)) == 2 and sum(f["sz"] for f in e.value.fills) == pytest.approx(0.002)


def test_flatten_gives_up_when_the_venue_is_down():
    fake = FakeHL()
    fake.position = 0.002
    fake.fail_state = [ConnectionError("down")] * 20
    ex, _, sleeps = make(fake, flatten_tries=3)
    with pytest.raises(FlattenFailed, match="down"):
        ex.flatten(mkbook(), 0.002, 1.0)
    assert not orders(fake) and len(sleeps.calls) == 3


# ---- construction and hard constraints ----


class FakeSDKExchange:
    def __init__(self, wallet, base_url=None, meta=None, vault_address=None,
                 account_address=None, spot_meta=None, perp_dexs=None, timeout=None):
        self.kw = {"base_url": base_url, "account_address": account_address, "timeout": timeout,
                   "vault_address": vault_address}
        self.wallet = wallet
        self.info = FakeHL()


KEY = "0x" + "11" * 32


def cfg(**kw):
    env = {"HL_API_KEY": KEY, "HL_ACCOUNT_ADDRESS": ADDR}
    return Config.from_env({**env, **kw})


def test_build_exchange_targets_testnet_with_the_account_address_and_a_timeout():
    ex = build_exchange(cfg(), BTC, since_ms=0, exchange_cls=FakeSDKExchange)
    sdk = ex._sdk
    assert sdk.kw == {"base_url": "https://api.hyperliquid-testnet.xyz", "account_address": ADDR,
                      "timeout": 10, "vault_address": None}
    assert sdk.wallet.address != ADDR  # signs as the API wallet, reads as the account
    assert ex.live is True


def test_build_exchange_needs_key_and_address():
    with pytest.raises(ConfigError, match="HL_API_KEY"):
        build_exchange(cfg(HL_API_KEY=""), BTC, 0, exchange_cls=FakeSDKExchange)
    with pytest.raises(ConfigError, match="HL_ACCOUNT_ADDRESS"):
        build_exchange(cfg(HL_ACCOUNT_ADDRESS=""), BTC, 0, exchange_cls=FakeSDKExchange)


def test_a_bad_key_error_never_contains_the_key():
    secret = "not-a-hex-key-SECRET123"
    with pytest.raises(ConfigError) as e:
        build_exchange(cfg(HL_API_KEY=secret), BTC, 0, exchange_cls=FakeSDKExchange)
    assert "SECRET123" not in str(e.value) and e.value.__cause__ is None
    assert e.value.__suppress_context__


def test_the_wrapper_exposes_no_transfer_or_withdrawal_and_no_mainnet_url():
    forbidden = ("transfer", "withdraw", "send_asset", "approve_agent", "sub_account")
    for name in dir(Exchange):
        assert not any(w in name for w in forbidden), name
    src = Path(exchange_mod.__file__).parent
    for py in src.glob("*.py"):
        text = py.read_text()
        assert "api.hyperliquid.xyz" not in text, py.name
        assert "MAINNET" not in text, py.name
        for w in ("usd_transfer", "withdraw_from_bridge", "send_asset", "usd_class_transfer",
                  "spot_transfer", "approve_agent", "vault_usd_transfer"):
            assert w not in text, (py.name, w)

