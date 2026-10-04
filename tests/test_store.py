import pytest

from jevhl.store import Store
from jevhl.types import Action, Decision


def dec(ts=1.0, p=0.7, action=Action.BUY, reason="signal", size=50.0):
    return Decision(ts, p, action, reason, size, 0.6, 12.0)


@pytest.fixture
def store():
    s = Store(":memory:")
    yield s
    s.close()


def test_insert_and_read_decision(store):
    i = store.insert_decision(dec(), mid=100.0)
    rows = store.decisions()
    assert len(rows) == 1 and rows[0]["id"] == i
    r = rows[0]
    assert r["p_up"] == 0.7 and r["action"] == "buy" and r["reason"] == "signal"
    assert r["size_usd"] == 50.0 and r["gate"] == 0.6 and r["latency_ms"] == 12.0
    assert r["mid"] == 100.0 and r["mid_future"] is None and r["label"] is None


def test_resolve_sets_label(store):
    up = store.insert_decision(dec(ts=1), mid=100.0)
    down = store.insert_decision(dec(ts=2), mid=100.0)
    tie = store.insert_decision(dec(ts=3), mid=100.0)
    store.resolve(up, 101.0)
    store.resolve(down, 99.0)
    store.resolve(tie, 100.0)
    by_id = {r["id"]: r for r in store.decisions()}
    assert by_id[up]["label"] == 1 and by_id[down]["label"] == 0
    assert by_id[tie]["label"] is None and by_id[tie]["mid_future"] == 100.0


def test_pending_lists_unresolved_before_cutoff(store):
    a = store.insert_decision(dec(ts=10), mid=1.0)
    store.insert_decision(dec(ts=100), mid=1.0)
    c = store.insert_decision(dec(ts=20), mid=1.0)
    store.resolve(c, 2.0)
    assert [r["id"] for r in store.pending(before_ts=50)] == [a]


def test_scored_includes_holds_excludes_ties_and_missing_p(store):
    h = store.insert_decision(dec(p=0.5, action=Action.HOLD, reason="below_gate", size=0), mid=1)
    t = store.insert_decision(dec(ts=2), mid=1)
    n = store.insert_decision(dec(ts=3, p=None, action=Action.HOLD, reason="late", size=0), mid=1)
    u = store.insert_decision(dec(ts=4), mid=1)  # unresolved
    store.resolve(h, 2)
    store.resolve(t, 1)  # tie
    store.resolve(n, 2)
    assert store.scored() == [(0.5, 1)]
    assert u


def test_scored_ordered_by_ts(store):
    a = store.insert_decision(dec(ts=5, p=0.9), mid=1)
    b = store.insert_decision(dec(ts=1, p=0.2), mid=1)
    store.resolve(a, 2)
    store.resolve(b, 0)
    assert store.scored() == [(0.2, 0), (0.9, 1)]


def fill(**kw):
    base = dict(oid=1, ts=1.0, coin="BTC", side="buy", sz=0.001, px=65000.0, fee=0.02)
    base.update(kw)
    return base


def test_fill_dedup_on_oid_ts_sz(store):
    assert store.insert_fill(**fill()) is True
    assert store.insert_fill(**fill()) is False
    assert store.insert_fill(**fill(sz=0.002)) is True  # same oid, different size
    assert store.insert_fill(**fill(ts=2.0)) is True
    assert len(store.fills()) == 3


def test_fill_fields(store):
    store.insert_fill(**fill())
    r = store.fills()[0]
    assert r["oid"] == 1 and r["side"] == "buy" and r["px"] == 65000.0 and r["fee"] == 0.02


def test_events(store):
    store.log_event(1.0, "kill_switch", "daily_loss")
    store.log_event(2.0, "connect", "")
    ev = store.events()
    assert [e["kind"] for e in ev] == ["kill_switch", "connect"]
    assert ev[0]["detail"] == "daily_loss"


def test_schema_version_and_reopen(tmp_path):
    path = str(tmp_path / "t.db")
    s = Store(path)
    assert s.schema_version() == 1
    s.insert_decision(dec(), mid=1.0)
    s.close()
    s2 = Store(path)  # reopen: data persists, schema creation idempotent
    assert len(s2.decisions()) == 1 and s2.schema_version() == 1
    s2.close()


def test_sql_injection_is_inert(store):
    store.log_event(1.0, "x'); DROP TABLE decisions;--", "d")
    store.insert_decision(dec(), mid=1.0)
    assert len(store.decisions()) == 1
    assert store.events()[0]["kind"] == "x'); DROP TABLE decisions;--"


@pytest.mark.parametrize("bad", [1.5, -0.2, float("inf"), float("-inf"), float("nan")])
@pytest.mark.parametrize("latency", [12.0, 10_000.0])  # on time -> invalid, late
def test_out_of_range_p_never_scored(store, bad, latency):
    from jevhl.config import Config
    from jevhl.policy import decide

    d = decide(bad, 0.0, Config(), ts=1.0, latency_ms=latency)
    i = store.insert_decision(d, mid=1)
    store.resolve(i, 2)
    assert store.decisions()[0]["p_up"] is None
    assert store.scored() == []


def test_late_but_valid_p_still_scored(store):
    d = Decision(1.0, 0.7, Action.HOLD, "late", 0.0, 0.6, 9999.0)
    store.resolve(store.insert_decision(d, mid=1), 2)
    assert store.scored() == [(0.7, 1)]
