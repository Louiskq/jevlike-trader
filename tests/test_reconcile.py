"""Reconciliation: local fills vs the exchange's, fill by fill, plus position and fees."""

import time

import pytest

from fake_hl import FakeHL
from jevhl import reconcile as rc
from jevhl.exchange import Exchange
from jevhl.rounding import AssetMeta
from jevhl.store import Store

BTC = AssetMeta("BTC", 5)
ADDR = "0x" + "ab" * 20
DUST = 5e-6


def fill(tid=1, oid=10, side="buy", sz=0.001, px=65000.0, fee=0.0292, ts=1000.0):
    return {"tid": tid, "oid": oid, "side": side, "sz": sz, "px": px, "fee": fee, "ts": ts}


def cmp(local, remote, lp=0.0, ep=0.0):
    return rc.compare(local, remote, lp, ep, DUST)


def test_identical_books_have_no_problems():
    f = [fill(1), fill(2, side="sell", ts=1060.0)]
    assert cmp(f, [dict(x) for x in f]) == []


def test_a_fill_missing_on_the_exchange_is_reported():
    (p,) = cmp([fill(1), fill(2)], [fill(1)])
    assert "tid 2" in p and "exchange" in p


def test_a_fill_missing_locally_is_reported():
    (p,) = cmp([fill(1)], [fill(1), fill(2)])
    assert "tid 2" in p and "local" in p


@pytest.mark.parametrize("field,value", [("px", 65000.5), ("sz", 0.0011), ("side", "sell"),
                                         ("oid", 11), ("fee", 0.03)])
def test_a_field_mismatch_is_reported(field, value):
    (p,) = cmp([fill(1)], [fill(1, **{field: value})])
    assert "tid 1" in p and field in p


def test_fee_tolerance_is_float_noise_only():
    assert cmp([fill(1, fee=0.0292)], [fill(1, fee=0.0292 + 1e-12)]) == []
    assert cmp([fill(1, fee=0.0292)], [fill(1, fee=0.0292 + 1e-6)]) != []


def test_paper_fills_are_not_compared():
    assert cmp([fill(-1), fill(1)], [fill(1)]) == []


def test_position_must_match_within_half_a_lot():
    assert cmp([], [], lp=0.002, ep=0.002 + 4.9e-6) == []
    (p,) = cmp([], [], lp=0.002, ep=0.002 + 5.1e-6)
    assert "position" in p


# ---- the CLI path against the fake venue ----


def _store_with(fake, n=2):
    store = Store(":memory:")
    ex = Exchange(fake, fake, ADDR, BTC, "BTC", since_ms=fake.now_ms - 60_000, sleep=lambda s: None)
    fills = ex.open(_book(), "buy", 100.0, 1.0)
    fills += ex.close(_book(), "sell", fills[0]["sz"], fills[0]["sz"], 2.0)
    for f in fills[:n]:
        store.insert_fill(tid=f["tid"], oid=f["oid"], ts=f["ts"], coin="BTC", side=f["side"],
                          sz=f["sz"], px=f["px"], fee=f["fee"])
    return store


def _book():
    from jevhl.feed import Book

    return Book(ts=1.0, recv_ts=1.0, coin="BTC", bids=[[64999.0, 5.0]], asks=[[65001.0, 5.0]])


def test_report_ok_when_the_store_matches_the_venue():
    fake = FakeHL()
    store = _store_with(fake)
    text, ok = rc.run_reconcile(store, fake, ADDR, "BTC", BTC)
    assert ok and "OK" in text and "fills 2" in text and "fees" in text
    assert "P&L" in text and "position" in text


def test_report_flags_a_fill_the_store_never_saw():
    fake = FakeHL()
    store = _store_with(fake, n=1)  # the closing fill was never stored
    text, ok = rc.run_reconcile(store, fake, ADDR, "BTC", BTC)
    assert not ok and "MISMATCH" in text and "tid 5001" in text


def test_exchange_fills_from_before_the_stored_history_are_not_flagged():
    fake = FakeHL()
    store = _store_with(fake)
    old = dict(tid=1, oid=1, coin="BTC", px="1.0", sz="1.0", side="B", fee="0", feeToken="USDC",
               time=fake.now_ms - 3_600_000)
    fake.raw_fills.insert(0, old)  # an earlier session's fill, before our first stored one
    text, ok = rc.run_reconcile(store, fake, ADDR, "BTC", BTC)
    assert ok, text


def test_empty_store_with_flat_account_is_ok_and_says_so():
    text, ok = rc.run_reconcile(Store(":memory:"), FakeHL(), ADDR, "BTC", BTC)
    assert ok and "no live fills" in text


def test_main_needs_the_account_address(monkeypatch, tmp_path):
    monkeypatch.delenv("HL_ACCOUNT_ADDRESS", raising=False)
    with pytest.raises(SystemExit, match="HL_ACCOUNT_ADDRESS"):
        rc.main(["--db", str(tmp_path / "x.db")])


def test_main_exits_nonzero_on_a_mismatch(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HL_ACCOUNT_ADDRESS", ADDR)
    fake = FakeHL(t0_ms=int(time.time() * 1000))
    store = _store_with(fake, n=1)
    db = tmp_path / "r.db"
    disk = Store(str(db))
    for r in store.fills():
        disk.insert_fill(tid=r["tid"], oid=r["oid"], ts=r["ts"], coin="BTC", side=r["side"],
                         sz=r["sz"], px=r["px"], fee=r["fee"])
    monkeypatch.setattr(rc, "_connect", lambda coin: (fake, BTC))
    with pytest.raises(SystemExit) as e:
        rc.main(["--db", str(db)])
    assert e.value.code == 1
    assert "MISMATCH" in capsys.readouterr().out
