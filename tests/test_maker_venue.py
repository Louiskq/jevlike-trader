"""Venue support for post-only maker quotes (milestone 8): the live wrapper's place, modify,
cancel, open-orders and rate-budget calls against the fake venue, the response parser, and the
paper exchange's quotes filled by real trade prints. No network, no key."""

from decimal import Decimal

import pytest

from fake_hl import ALO, GONE_ERR, HTTP429, FakeHL, _err_status
from jevhl.exchange import Exchange, ExchangeError, RateLimited, parse_quote_response
from jevhl.feed import Book, Print
from jevhl.paper import MAKER_FEE_RATE, PaperExchange
from jevhl.rounding import AssetMeta
from jevhl.store import Store
from jevhl.types import OpenQuote, QuoteAck

BTC = AssetMeta("BTC", 5)
ADDR = "0x" + "ab" * 20
SZ = Decimal("0.00013")


def ok(*statuses):
    return {"status": "ok", "response": {"type": "order", "data": {"statuses": list(statuses)}}}


# ---- parsing the reply to a post-only order or a modify ----


def test_parse_quote_resting_is_placed_with_its_oid():
    assert parse_quote_response(ok({"resting": {"oid": 77}})) == QuoteAck("placed", 77, "")


def test_parse_post_only_rejection():
    text = "Post only order would have immediately matched, bbo was 82621.0@82630.0. asset=0"
    ack = parse_quote_response(ok({"error": text}))
    assert ack.status == "rejected" and ack.oid is None and "82621.0@82630.0" in ack.detail


@pytest.mark.parametrize("text", [GONE_ERR, "Order was never placed, already canceled, or filled."])
def test_parse_modify_of_a_canceled_or_filled_order_is_gone(text):
    assert parse_quote_response(ok({"error": text})).status == "gone"


def test_parse_filled_status_is_reported_as_taken():
    ack = parse_quote_response(ok({"filled": {"totalSz": "0.0001", "avgPx": "1.0", "oid": 5}}))
    assert (ack.status, ack.oid) == ("filled", 5)


@pytest.mark.parametrize("resp", [
    {"status": "ok", "response": {"type": "default"}},  # a modify may answer without statuses
    ok("success"),
])
def test_parse_reply_without_an_order_status_is_unknown(resp):
    assert parse_quote_response(resp).status == "unknown"


@pytest.mark.parametrize("resp", [
    ok({"error": "Too many cumulative requests sent (10001 > 10000) for cumulative volume"}),
    {"status": "err", "response": "Too many requests"},
    ok({"error": "rate limit exceeded"}),
])
def test_rate_limit_text_raises_rate_limited(resp):
    with pytest.raises(RateLimited):
        parse_quote_response(resp)


@pytest.mark.parametrize("resp", [
    ok({"error": "Order must have minimum value of $10."}),
    {"status": "err", "response": "User or API Wallet does not exist."},
    ok({"surprise": 1}),
    ok(),
    "nonsense",
    ok({"resting": {"oid": "x"}}),
])
def test_other_quote_errors_raise_exchange_error(resp):
    with pytest.raises(ExchangeError) as e:
        parse_quote_response(resp)
    assert not isinstance(e.value, RateLimited)


# ---- the live wrapper on the fake venue ----


def live(fake=None):
    fake = fake or FakeHL(bids=[[65000.0, 5.0]], asks=[[65003.0, 5.0]])
    return Exchange(fake, fake, ADDR, BTC, "BTC", since_ms=0, sleep=lambda s: None), fake


def test_order_sent_is_post_only_alo():
    ex, fake = live()
    ack = ex.place_quote("buy", SZ, Decimal("65001"), None, 0.0)
    assert ack == QuoteAck("placed", 1000, "")
    (call,) = [c for c in fake.calls if c[0] == "order"]
    assert call == ("order", "BTC", True, 0.00013, 65001.0, ALO, False)  # never reduce-only
    assert ex.open_quotes() == [OpenQuote(1000, "buy", 65001.0, 0.00013)]


def test_live_crossing_quote_is_rejected_not_raised():
    ex, fake = live()
    ack = ex.place_quote("sell", SZ, Decimal("65000"), None, 0.0)
    assert ack.status == "rejected" and ex.open_quotes() == []


def test_modify_sends_one_batch_modify_with_alo_and_returns_the_new_oid():
    ex, fake = live()
    ex.place_quote("buy", SZ, Decimal("65001"), None, 0.0)
    ack = ex.modify_quote(1000, "sell", SZ, Decimal("65002"), None, 0.0)
    assert ack == QuoteAck("placed", 1001, "")
    (call,) = [c for c in fake.calls if c[0] == "modify"]
    assert call == ("modify", 1000, "BTC", False, 0.00013, 65002.0, ALO, False)
    assert ex.open_quotes() == [OpenQuote(1001, "sell", 65002.0, 0.00013)]


def test_modify_of_a_gone_order_reports_gone():
    ex, fake = live()
    assert ex.modify_quote(4242, "buy", SZ, Decimal("65001"), None, 0.0).status == "gone"


def test_http_429_and_too_many_text_both_detected():
    ex, fake = live()
    fake.fail_orders = [HTTP429()]
    with pytest.raises(RateLimited):
        ex.place_quote("buy", SZ, Decimal("65001"), None, 0.0)
    fake.fail_modify = [HTTP429()]
    with pytest.raises(RateLimited):
        ex.modify_quote(1, "buy", SZ, Decimal("65001"), None, 0.0)
    fake.order_response = _err_status("Too many cumulative requests sent")
    with pytest.raises(RateLimited):
        ex.place_quote("buy", SZ, Decimal("65001"), None, 0.0)


def test_other_http_errors_are_not_rate_limits():
    ex, fake = live()

    class HTTP500(Exception):
        status_code = 500

    fake.fail_orders = [HTTP500("server")]
    with pytest.raises(HTTP500):
        ex.place_quote("buy", SZ, Decimal("65001"), None, 0.0)


def test_open_quotes_only_this_coin_and_cancel_oids():
    ex, fake = live()
    ex.place_quote("buy", SZ, Decimal("65001"), None, 0.0)
    ex.place_quote("buy", SZ, Decimal("65000"), None, 0.0)
    fake.resting.append({"coin": "ETH", "side": "A", "limitPx": "3000.0", "sz": "0.01",
                         "oid": 9, "timestamp": 1, "origSz": "0.01"})
    assert [q.oid for q in ex.open_quotes()] == [1000, 1001]
    assert ex.cancel_oids([1001]) == 1
    assert [q.oid for q in ex.open_quotes()] == [1000]
    assert ex.cancel_oids([]) == 0 and fake.calls[-1][0] != "bulk_cancel"


def test_open_quotes_rejects_a_malformed_order():
    ex, fake = live()
    fake.resting.append({"coin": "BTC", "side": "B", "limitPx": "nan", "sz": "0.1", "oid": 3})
    with pytest.raises(ExchangeError):
        ex.open_quotes()


def test_rate_budget_reads_the_address_request_budget():
    ex, fake = live()
    assert ex.rate_budget() == {"used": 321, "cap": 11234}
    assert fake.calls[-1] == ("user_rate_limit", ADDR)


def test_live_maker_fill_comes_back_with_the_maker_fee():
    ex, fake = live()
    ex.place_quote("buy", SZ, Decimal("65001"), None, 0.0)
    fake.taker_hits(1000, 0.00005)
    (f,) = ex.sync_fills()
    assert (f["oid"], f["side"], f["sz"], f["px"]) == (1000, "buy", 0.00005, 65001.0)
    assert f["fee"] == pytest.approx(65001.0 * 0.00005 * 0.00015)
    assert ex.open_quotes() == [OpenQuote(1000, "buy", 65001.0, pytest.approx(0.00008))]


# ---- paper quotes filled by real prints ----


def book(bid=100.0, ask=103.0, t=1.0):
    return Book(ts=t, recv_ts=t, coin="BTC", bids=[[bid, 1.0]], asks=[[ask, 1.0]])


def prt(tid, side, px, sz=1.0, ts=20.0, recv_ts=None):
    return Print(ts=ts, recv_ts=ts if recv_ts is None else recv_ts, side=side, px=px, sz=sz,
                 tid=tid)


@pytest.fixture
def paper():
    store = Store(":memory:")
    yield PaperExchange(store, AssetMeta("BTC", 2), "BTC"), store
    store.close()


def test_paper_rejects_a_crossing_quote(paper):
    ex, _ = paper
    assert ex.place_quote("buy", Decimal("1"), Decimal("103"), book(), 10.0).status == "rejected"
    assert ex.place_quote("sell", Decimal("1"), Decimal("100"), book(), 10.0).status == "rejected"
    assert ex.open_quotes() == []


def test_buy_quote_fills_only_on_a_sell_print_through_its_price(paper):
    ex, store = paper
    ack = ex.place_quote("buy", Decimal("1"), Decimal("101"), book(), 10.0)
    assert ack.status == "placed" and ack.oid < 0
    assert ex.match_prints([prt(1, "sell", 101.0)]) == []  # at the price: others are ahead
    (f,) = ex.match_prints([prt(2, "sell", 100.99)])  # through it
    assert (f["oid"], f["side"], f["sz"], f["px"], f["ts"]) == (ack.oid, "buy", 1.0, 101.0, 20.0)
    assert ex.open_quotes() == []  # fully filled
    assert [r["tid"] for r in store.fills()] == [f["tid"]] and f["tid"] < 0


def test_sell_quote_fills_on_a_buy_print_above_its_price(paper):
    ex, _ = paper
    ex.place_quote("sell", Decimal("1"), Decimal("102"), book(), 10.0)
    assert ex.match_prints([prt(1, "buy", 102.0)]) == []
    assert ex.match_prints([prt(2, "buy", 102.5)])[0]["side"] == "sell"


def test_same_side_print_does_not_fill(paper):
    ex, _ = paper
    ex.place_quote("buy", Decimal("1"), Decimal("101"), book(), 10.0)
    assert ex.match_prints([prt(1, "buy", 99.0)]) == []  # a taker buy never hits our bid
    ex.cancel_all()
    ex.place_quote("sell", Decimal("1"), Decimal("102"), book(), 10.0)
    assert ex.match_prints([prt(2, "sell", 105.0)]) == []


@pytest.mark.parametrize("ts,recv_ts", [(10.0, 20.0), (20.0, 10.0), (9.0, 9.0)])
def test_print_before_placement_does_not_fill(paper, ts, recv_ts):
    """Both clocks must be after the placement: a reconnect re-sends old trades with a fresh
    receive time, and a print received before we placed cannot have hit us."""
    ex, _ = paper
    ex.place_quote("buy", Decimal("1"), Decimal("101"), book(), 10.0)
    assert ex.match_prints([prt(1, "sell", 99.0, ts=ts, recv_ts=recv_ts)]) == []


def test_paper_print_smaller_than_quote_fills_partially(paper):
    ex, store = paper
    ex.place_quote("buy", Decimal("1"), Decimal("101"), book(), 10.0)
    (f,) = ex.match_prints([prt(1, "sell", 100.0, sz=0.304)])
    assert f["sz"] == 0.3  # floored to a lot
    assert ex.open_quotes() == [OpenQuote(f["oid"], "buy", 101.0, 0.7)]
    fills = ex.match_prints([prt(2, "sell", 100.0, sz=5.0), prt(3, "sell", 100.0, sz=5.0)])
    assert [x["sz"] for x in fills] == [0.7]  # never more than what rests
    assert ex.open_quotes() == [] and sum(r["sz"] for r in store.fills()) == pytest.approx(1.0)


def test_paper_maker_fee_rate(paper):
    ex, _ = paper
    ex.place_quote("buy", Decimal("0.5"), Decimal("101"), book(), 10.0)
    (f,) = ex.match_prints([prt(1, "sell", 100.0)])
    assert MAKER_FEE_RATE == 0.00015
    assert f["fee"] == pytest.approx(101.0 * 0.5 * 0.00015)


def test_paper_modify_moves_the_quote_and_loses_the_queue_place(paper):
    ex, _ = paper
    oid = ex.place_quote("buy", Decimal("1"), Decimal("101"), book(), 10.0).oid
    ack = ex.modify_quote(oid, "buy", Decimal("1"), Decimal("102"), book(), 30.0)
    assert ack.status == "placed"
    (q,) = ex.open_quotes()
    assert (q.oid, q.px) == (ack.oid, 102.0)
    assert ex.match_prints([prt(1, "sell", 99.0, ts=25.0)]) == []  # before the modify
    assert len(ex.match_prints([prt(2, "sell", 99.0, ts=31.0)])) == 1


def test_paper_modify_of_a_filled_or_unknown_order_is_gone(paper):
    ex, _ = paper
    oid = ex.place_quote("buy", Decimal("1"), Decimal("101"), book(), 10.0).oid
    ex.match_prints([prt(1, "sell", 99.0)])
    assert ex.modify_quote(oid, "buy", Decimal("1"), Decimal("101"), book(), 30.0).status == "gone"


def test_paper_crossing_modify_is_rejected_and_leaves_no_order(paper):
    ex, _ = paper
    oid = ex.place_quote("buy", Decimal("1"), Decimal("101"), book(), 10.0).oid
    ack = ex.modify_quote(oid, "buy", Decimal("1"), Decimal("103"), book(), 30.0)
    assert ack.status == "rejected" and ex.open_quotes() == []


def test_paper_cancel_all_and_cancel_oids_clear_the_quote(paper):
    ex, _ = paper
    oid = ex.place_quote("buy", Decimal("1"), Decimal("101"), book(), 10.0).oid
    assert ex.cancel_oids([oid - 1]) == 0 and len(ex.open_quotes()) == 1
    assert ex.cancel_oids([oid]) == 1 and ex.open_quotes() == []
    ex.place_quote("buy", Decimal("1"), Decimal("101"), book(), 10.0)
    assert ex.cancel_all() == 1 and ex.cancel_all() == 0
    assert ex.match_prints([prt(1, "sell", 99.0)]) == []


def test_paper_quote_oids_are_negative_and_unique(paper):
    ex, _ = paper
    a = ex.place_quote("buy", Decimal("1"), Decimal("101"), book(), 10.0).oid
    ex.cancel_all()
    b = ex.place_quote("buy", Decimal("1"), Decimal("101"), book(), 10.0).oid
    assert a < 0 and b < 0 and a != b
    assert ex.rate_budget() is None  # paper has no address budget


def test_paper_quote_oids_never_collide_with_paper_fill_ids(paper):
    ex, store = paper
    ioc = ex.open(book(), "buy", 200.0, 1.0)  # a paper IOC fill: oid = tid
    oid = ex.place_quote("buy", Decimal("1"), Decimal("101"), book(), 10.0).oid
    (f,) = ex.match_prints([prt(1, "sell", 99.0)])
    ids = [r["oid"] for r in store.fills()]
    assert oid not in {x["tid"] for x in ioc} and len(set(ids)) == len(ids) == 2
    assert f["tid"] != oid
