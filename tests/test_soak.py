import asyncio
from dataclasses import replace

import pytest

from jevhl import soak
from jevhl.config import Config
from jevhl.model import MockModel
from jevhl.rounding import AssetMeta
from jevhl.run import dry_run, format_report
from jevhl.soak import MIN_DECISIONS_FOR_STUCK, current_rss_kb, rss_sampler, soak_report
from jevhl.store import Store
from jevhl.types import Action, Decision
from test_run import REAL, T0, Clock


def test_current_rss_is_a_plausible_positive_number():
    kb = current_rss_kb()
    assert 5_000 < kb < 50_000_000


def test_current_rss_falls_back_when_proc_and_ps_fail(monkeypatch):
    monkeypatch.setattr(soak, "_rss_proc", lambda: None)
    monkeypatch.setattr(soak, "_rss_ps", lambda: None)
    assert current_rss_kb() > 0  # peak RSS from getrusage


def test_sampler_logs_immediately_then_every_interval():
    store = Store(":memory:")
    values = iter([100, 150, 120, 999])
    clock = Clock()

    async def go():
        task = asyncio.create_task(rss_sampler(store, clock, 0.05, lambda: next(values)))
        await asyncio.sleep(0.18)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())
    rows = [e for e in store.events() if e["kind"] == "rss"]
    assert [e["detail"] for e in rows][:3] == ["100", "150", "120"]
    assert 3 <= len(rows) <= 4
    assert all(e["ts"] == T0 for e in rows)


def test_sampler_survives_a_failing_read():
    store = Store(":memory:")
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("no /proc")
        return 200

    async def go():
        task = asyncio.create_task(rss_sampler(store, Clock(), 0.03, flaky))
        await asyncio.sleep(0.12)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())
    kinds = [e["kind"] for e in store.events()]
    assert "rss_error" in kinds and "rss" in kinds


def _decisions(store, spec):
    """spec: list of (action, reason) pairs."""
    for i, (action, reason) in enumerate(spec):
        p = None if reason in ("late", "invalid") else 0.7
        store.insert_decision(
            Decision(T0 + i, p, Action(action), reason, 0.0, 0.6, 1.0), 100.0
        )


def test_soak_report_rss_and_rates():
    store = Store(":memory:")
    for ts, kb in [(1, 1000), (2, 4000), (3, 2500)]:
        store.log_event(ts, "rss", str(kb))
    _decisions(store, [("buy", "signal"), ("hold", "late"), ("hold", "below_gate"),
                       ("sell", "signal")])
    r = soak_report(store)
    assert (r["rss_start_kb"], r["rss_end_kb"], r["rss_max_kb"]) == (1000, 2500, 4000)
    assert r["decisions"] == 4
    assert r["late_rate"] == pytest.approx(0.25)
    assert r["hold_rate"] == pytest.approx(0.5)
    assert r["warnings"] == []


def test_soak_report_without_data():
    r = soak_report(Store(":memory:"))
    assert r["rss_start_kb"] is None and r["hold_rate"] is None and r["late_rate"] is None


@pytest.mark.parametrize("action,reason,word", [
    ("hold", "late", "100%"), ("buy", "signal", "0%"),
])
def test_soak_report_flags_stuck_hold_rate(action, reason, word):
    store = Store(":memory:")
    _decisions(store, [(action, reason)] * MIN_DECISIONS_FOR_STUCK)
    (w,) = soak_report(store)["warnings"]
    assert "stuck" in w and word in w


def test_soak_report_no_stuck_warning_on_small_sample():
    store = Store(":memory:")
    _decisions(store, [("hold", "late")] * (MIN_DECISIONS_FOR_STUCK - 1))
    assert soak_report(store)["warnings"] == []


def test_soak_report_ignores_unparseable_rss_rows():
    store = Store(":memory:")
    store.log_event(1, "rss", "junk")
    store.log_event(2, "rss", "nan")
    store.log_event(3, "rss", "500")
    assert soak_report(store)["rss_start_kb"] == 500


def test_dry_run_samples_rss_at_start_and_end_and_report_shows_soak():
    clock = Clock()
    store = Store(":memory:")
    values = iter(range(1000, 2000, 10))

    async def books():
        for i in range(3):
            clock.t = T0 + 5.3 * i
            yield replace(REAL[i % 2], recv_ts=clock.t)
            await asyncio.sleep(0.05)

    tr = asyncio.run(dry_run(
        Config(), store, AssetMeta("BTC", 5), books(), model=MockModel(seed=1), clock=clock,
        rss_every_s=3600, rss=lambda: next(values),
    ))
    rows = [e["detail"] for e in store.events() if e["kind"] == "rss"]
    assert rows == ["1000", "1010"]  # one at start, one at the end
    text = format_report(store, tr)
    assert "soak: rss start 1000 kB  end 1010 kB  max 1010 kB" in text
    assert "hold rate" in text and "late-hold rate" in text


def _run_once(store, clock, values, n_books=3):
    async def books():
        for i in range(n_books):
            clock.t = T0 + 5.3 * i
            yield replace(REAL[i % 2], recv_ts=clock.t)
            await asyncio.sleep(0.01)

    return asyncio.run(dry_run(
        Config(), store, AssetMeta("BTC", 5), books(), model=MockModel(seed=1), clock=clock,
        rss_every_s=3600, rss=lambda: next(values),
    ))


def test_soak_report_covers_only_the_current_run_on_a_reused_db():
    store = Store(":memory:")
    _run_once(store, Clock(), iter([1000, 1010]), n_books=3)
    first = soak_report(store)
    _run_once(store, Clock(), iter([5000, 5010]), n_books=2)
    r = soak_report(store)
    assert (r["rss_start_kb"], r["rss_end_kb"], r["rss_max_kb"]) == (5000, 5010, 5010)
    assert r["rss_samples"] == 2
    assert r["decisions"] == 2 and first["decisions"] == 3
    assert len(store.decisions()) == 5  # earlier run is still in the db, just not in the report
