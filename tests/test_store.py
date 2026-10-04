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
    assert r["mid"] == 100.0
    assert "mid_future" not in r.keys() and "label" not in r.keys()
    assert store.outcomes(i) == []


def test_resolve_sets_label_per_horizon(store):
    up = store.insert_decision(dec(ts=1), mid=100.0)
    down = store.insert_decision(dec(ts=2), mid=100.0)
    tie = store.insert_decision(dec(ts=3), mid=100.0)
    store.resolve(up, 60, 101.0)
    store.resolve(down, 60, 99.0)
    store.resolve(tie, 60, 100.0)
    store.resolve(up, 30, 99.0)  # same decision, other horizon, other direction
    by = {(o["decision_id"], o["horizon_s"]): o for i in (up, down, tie) for o in store.outcomes(i)}
    assert by[(up, 60)]["label"] == 1 and by[(up, 30)]["label"] == 0
    assert by[(down, 60)]["label"] == 0
    assert by[(tie, 60)]["label"] is None and by[(tie, 60)]["mid_future"] == 100.0


def test_resolve_is_idempotent_first_outcome_stands(store):
    i = store.insert_decision(dec(), mid=100.0)
    store.resolve(i, 60, 101.0)
    store.resolve(i, 60, 99.0)
    (o,) = store.outcomes(i)
    assert o["mid_future"] == 101.0 and o["label"] == 1


def test_resolve_unknown_decision_raises(store):
    with pytest.raises(KeyError):
        store.resolve(999, 60, 1.0)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0.0, -5.0])
def test_resolve_rejects_non_finite_or_non_positive_mid(store, bad):
    i = store.insert_decision(dec(), mid=100.0)
    with pytest.raises(ValueError):
        store.resolve(i, 60, bad)
    assert store.outcomes(i) == []


def test_pending_is_per_horizon_and_windowed(store):
    a = store.insert_decision(dec(ts=10), mid=1.0)
    store.insert_decision(dec(ts=100), mid=1.0)
    c = store.insert_decision(dec(ts=20), mid=1.0)
    store.insert_decision(dec(ts=1), mid=1.0)  # older than the window
    store.resolve(c, 60, 2.0)
    assert [r["id"] for r in store.pending(60, lo_ts=5, hi_ts=50)] == [a]
    # c is resolved at 60 s but not at 30 s
    assert [r["id"] for r in store.pending(30, lo_ts=5, hi_ts=50)] == [a, c]
    # window bounds are inclusive
    assert [r["id"] for r in store.pending(30, lo_ts=10, hi_ts=20)] == [a, c]


def test_scored_includes_holds_excludes_ties_and_missing_p(store):
    h = store.insert_decision(dec(p=0.5, action=Action.HOLD, reason="below_gate", size=0), mid=1)
    t = store.insert_decision(dec(ts=2), mid=1)
    n = store.insert_decision(dec(ts=3, p=None, action=Action.HOLD, reason="late", size=0), mid=1)
    u = store.insert_decision(dec(ts=4), mid=1)  # unresolved
    store.resolve(h, 60, 2)
    store.resolve(t, 60, 1)  # tie
    store.resolve(n, 60, 2)
    assert store.scored(60) == [(0.5, 1)]
    assert u


def test_scored_ties_dropped_per_horizon(store):
    i = store.insert_decision(dec(p=0.7), mid=1)
    store.resolve(i, 30, 1)  # tie at 30 s
    store.resolve(i, 60, 2)  # up at 60 s
    store.resolve(i, 300, 0.5)  # down at 300 s
    assert store.scored(30) == []
    assert store.scored(60) == [(0.7, 1)]
    assert store.scored(300) == [(0.7, 0)]


def test_scored_ordered_by_ts(store):
    a = store.insert_decision(dec(ts=5, p=0.9), mid=1)
    b = store.insert_decision(dec(ts=1, p=0.2), mid=1)
    store.resolve(a, 60, 2)
    store.resolve(b, 60, 0.5)
    assert store.scored(60) == [(0.2, 0), (0.9, 1)]


def test_latencies_skips_missing(store):
    store.insert_decision(Decision(1.0, 0.5, Action.HOLD, "stale", 0.0, 0.6, None), mid=1)
    store.insert_decision(dec(ts=2), mid=1)
    assert store.latencies() == [12.0]


def test_decision_mid_before(store):
    store.insert_decision(dec(ts=10), mid=5.0)
    store.insert_decision(dec(ts=20), mid=7.0)
    assert store.last_mid_before(15) == 5.0
    assert store.last_mid_before(20) == 5.0  # strictly before
    assert store.last_mid_before(10) is None


def fill(**kw):
    base = dict(tid=1, oid=1, ts=1.0, coin="BTC", side="buy", sz=0.001, px=65000.0, fee=0.02)
    base.update(kw)
    return base


def test_fill_idempotent_on_tid(store):
    assert store.insert_fill(**fill()) is True
    assert store.insert_fill(**fill()) is False  # same tid again: one row
    assert len(store.fills()) == 1


def test_fills_same_oid_ts_sz_different_tid_both_stored(store):
    # One IOC sweeping two makers: same oid, time and size, different price and tid.
    assert store.insert_fill(**fill(tid=1, px=65000.0)) is True
    assert store.insert_fill(**fill(tid=2, px=65001.0)) is True
    rows = store.fills()
    assert [r["px"] for r in rows] == [65000.0, 65001.0]
    assert [r["tid"] for r in rows] == [1, 2]


def test_fill_fields(store):
    store.insert_fill(**fill())
    r = store.fills()[0]
    assert r["oid"] == 1 and r["side"] == "buy" and r["px"] == 65000.0 and r["fee"] == 0.02


@pytest.mark.parametrize(
    "kw",
    [
        dict(px=float("nan")), dict(sz=float("inf")), dict(fee=float("nan")),
        dict(px=0.0), dict(sz=0.0), dict(sz=-1.0),
    ],
)
def test_fill_rejects_non_finite_or_non_positive(store, kw):
    with pytest.raises(ValueError):
        store.insert_fill(**fill(**kw))
    assert store.fills() == []


def test_fills_before_is_strict_and_ordered(store):
    store.insert_fill(**fill(tid=1, ts=5.0))
    store.insert_fill(**fill(tid=2, ts=10.0))
    assert [r["tid"] for r in store.fills_before(10.0)] == [1]


def test_events(store):
    store.log_event(1.0, "kill_switch", "daily_loss")
    store.log_event(2.0, "connect", "")
    ev = store.events()
    assert [e["kind"] for e in ev] == ["kill_switch", "connect"]
    assert ev[0]["detail"] == "daily_loss"


def test_schema_version_and_reopen(tmp_path):
    path = str(tmp_path / "t.db")
    s = Store(path)
    assert s.schema_version() == 3
    s.insert_decision(dec(), mid=1.0)
    s.close()
    s2 = Store(path)  # reopen: data persists, schema creation idempotent
    assert len(s2.decisions()) == 1 and s2.schema_version() == 3
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
    store.resolve(i, 60, 2)
    assert store.decisions()[0]["p_up"] is None
    assert store.scored(60) == []


def test_late_but_valid_p_still_scored(store):
    d = Decision(1.0, 0.7, Action.HOLD, "late", 0.0, 0.6, 9999.0)
    store.resolve(store.insert_decision(d, mid=1), 60, 2)
    assert store.scored(60) == [(0.7, 1)]


def test_old_schema_version_refused(tmp_path):
    import sqlite3

    path = str(tmp_path / "old.db")
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE schema_version (version INTEGER NOT NULL);"
                     "INSERT INTO schema_version VALUES (2);")
    db.commit()
    db.close()
    with pytest.raises(RuntimeError, match="schema"):
        Store(path)
