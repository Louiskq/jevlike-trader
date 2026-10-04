"""Offline end-to-end: MockModel -> policy -> store -> calibration."""

import random

from jevhl.calibration import reliability_bins, summary
from jevhl.config import Config
from jevhl.model import MockModel
from jevhl.policy import decide
from jevhl.store import Store
from jevhl.types import ModelInput


def test_offline_pipeline():
    cfg = Config.from_env({})
    model = MockModel(seed=1)
    rng = random.Random(2)
    store = Store(":memory:")
    mid = 65000.0
    for i in range(300):
        bids = [[mid - j, rng.uniform(0.1, 5)] for j in range(10)]
        asks = [[mid + 1 + j, rng.uniform(0.1, 5)] for j in range(10)]
        out = model.predict(ModelInput(i * 5.0, "BTC", mid, 1.5, bids, asks))
        d = decide(out.p_up, 0.0, cfg, ts=i * 5.0, latency_ms=1.0)
        did = store.insert_decision(d, mid)
        store.resolve(did, 60, mid + rng.choice([-1.0, 0.0, 1.0]))
    scored = store.scored(60)
    ps, ys = [p for p, _ in scored], [y for _, y in scored]
    s = summary(ps, ys)
    assert 0 < len(ps) <= 300 and s["n"] == len(ps)
    assert 0 <= s["brier"] <= 1
    assert sum(b.count for b in reliability_bins(ps, ys)) == len(ps)
