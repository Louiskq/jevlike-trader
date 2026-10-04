import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from jevhl.config import Config
from jevhl.feed import parse_message
from jevhl.latency import latency_report
from jevhl.model import MockModel
from jevhl.rounding import AssetMeta
from jevhl.run import dry_run, format_report
from jevhl.store import Store

SESSION = json.loads((Path(__file__).parent / "fixtures" / "ws_session_btc.json").read_text())
REAL = [parse_message(m, "BTC", 0.0) for m in (SESSION[1], SESSION[3])]
T0 = 10 * 86400.0 + 3600.0


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


def test_dry_run_replays_real_books_one_decision_each_and_reports():
    clock = Clock()
    store = Store(":memory:")

    async def books():
        for i in range(6):
            clock.t = T0 + 5.3 * i
            yield replace(REAL[i % 2], recv_ts=clock.t)
            await asyncio.sleep(0.05)

    tr = asyncio.run(
        dry_run(Config(), store, AssetMeta("BTC", 5), books(), model=MockModel(seed=1), clock=clock)
    )
    assert len(store.decisions()) == 6
    assert tr.error is None
    rep = latency_report(store)
    assert rep["decisions"] == 6 and rep["p50"] is not None
    assert rep["gap_p50_s"] == pytest.approx(5.3)
    text = format_report(store, tr)
    assert "latency" in text and "60 s" in text and "30 s" in text and "300 s" in text


def test_dry_run_duration_stops_and_flattens():
    clock = Clock()
    store = Store(":memory:")

    async def books():
        i = 0
        while True:
            clock.t = T0 + 5.3 * i
            yield replace(REAL[i % 2], recv_ts=clock.t)
            i += 1
            await asyncio.sleep(0.05)

    class Buyer:
        def predict(self, inp):
            from jevhl.types import ModelOutput

            return ModelOutput(0.9)

    tr = asyncio.run(
        dry_run(Config(), store, AssetMeta("BTC", 5), books(), model=Buyer(), clock=clock,
                duration_s=0.3)
    )
    assert store.decisions() and tr.stopped
    assert abs(tr.ledger.position) < 1e-9
