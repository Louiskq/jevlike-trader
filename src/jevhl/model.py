"""Model interface and a deterministic mock."""

from __future__ import annotations

import math
import random
import time
from collections.abc import Callable
from typing import Protocol

from .types import ModelInput, ModelOutput

TOP_LEVELS = 10


class Model(Protocol):
    def predict(self, inp: ModelInput) -> ModelOutput: ...


class MockModel:
    """p_up = 0.5 + 0.3*tanh(k*imbalance) + seeded noise, clipped to [0.01, 0.99].

    imbalance is (bid - ask) / (bid + ask) size over the top 10 levels. delay_s, if set,
    is waited via the injected sleep before answering (to exercise late holds).
    """

    def __init__(
        self,
        seed: int = 0,
        k: float = 2.0,
        noise: float = 0.05,
        delay_s: float = 0.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._rng = random.Random(seed)
        self._k = k
        self._noise = noise
        self._delay_s = delay_s
        self._sleep = sleep

    def predict(self, inp: ModelInput) -> ModelOutput:
        if self._delay_s > 0:
            self._sleep(self._delay_s)
        bid = sum(sz for _, sz in inp.bids[:TOP_LEVELS])
        ask = sum(sz for _, sz in inp.asks[:TOP_LEVELS])
        total = bid + ask
        imbalance = (bid - ask) / total if total > 0 else 0.0
        p = 0.5 + 0.3 * math.tanh(self._k * imbalance) + self._rng.gauss(0.0, self._noise)
        return ModelOutput(p_up=min(max(p, 0.01), 0.99))
