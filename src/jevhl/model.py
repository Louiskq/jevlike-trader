"""Model interface and a deterministic mock."""

from __future__ import annotations

import http.client
import json
import math
import random
import socket
import threading
import time
from collections.abc import Callable
from typing import Protocol
from urllib.parse import urlsplit

from .config import Config
from .prompt import OPTIONS, question_text, render_context
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


MAX_RESPONSE_BYTES = 64 * 1024
PROB_SUM_TOLERANCE = 0.01  # the server rounds probabilities to 4 decimals


class DeciderError(RuntimeError):
    """The decider did not give a usable answer. The trader turns this into an invalid hold."""


def _probability(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise DeciderError(f"probability {name!r} is not a number: {value!r}")
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise DeciderError(f"probability {name!r} out of range: {value!r}")
    return float(value)


def parse_decision(body: object, question: str) -> float:
    """p_up from the decider's reply, or DeciderError. Strict: every option present, finite,
    in [0, 1] and summing to about 1; nothing else reaches storage."""
    answer = body.get(question) if isinstance(body, dict) else None
    probs = answer.get("probabilities") if isinstance(answer, dict) else None
    if not isinstance(probs, dict):
        raise DeciderError("reply has no probabilities for the question")
    values = {}
    for opt in OPTIONS:
        if opt not in probs:
            raise DeciderError(f"reply is missing probability for {opt!r}")
        values[opt] = _probability(probs[opt], opt)
    if abs(sum(values.values()) - 1.0) > PROB_SUM_TOLERANCE:
        raise DeciderError(f"probabilities do not sum to 1: {values}")
    return values[OPTIONS[0]]


class DeciderModel:
    """The decider served over HTTP (normally an SSH tunnel to a cluster node).

    predict() runs in a worker thread (see Trader._ask). timeout_s bounds each socket
    operation, so a hung server cannot keep the thread past about the deadline. abort() is
    called when the bot stops: it shuts the open socket so a request in flight ends at once.
    """

    def __init__(self, url: str, timeout_s: float):
        parts = urlsplit(url)
        self._host, self._port = parts.hostname, parts.port or 80
        self._timeout_s = timeout_s
        self._aborted = threading.Event()
        self._lock = threading.Lock()
        self._conn: http.client.HTTPConnection | None = None

    def abort(self) -> None:
        with self._lock:
            self._aborted.set()
            sock = self._conn.sock if self._conn is not None else None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def predict(self, inp: ModelInput) -> ModelOutput:
        question = question_text(inp.horizon_s)
        payload = json.dumps({
            "context": render_context(inp),
            "schema": {question: {"type": "choice", "options": list(OPTIONS)}},
        }).encode()
        conn = http.client.HTTPConnection(self._host, self._port, timeout=self._timeout_s)
        try:
            conn.connect()
            with self._lock:  # registered only once connected, so abort() can reach the socket
                if self._aborted.is_set():
                    raise DeciderError("aborted")
                self._conn = conn
            conn.request("POST", "/decide", payload, {"content-type": "application/json"})
            resp = conn.getresponse()
            data = resp.read(MAX_RESPONSE_BYTES + 1)
            status = resp.status
        except (OSError, http.client.HTTPException) as e:
            why = "aborted" if self._aborted.is_set() else f"{type(e).__name__}: {e}"
            raise DeciderError(f"request failed: {why}") from e
        finally:
            with self._lock:
                self._conn = None
            conn.close()
        if status != 200:
            raise DeciderError(f"HTTP {status}")
        if len(data) > MAX_RESPONSE_BYTES:
            raise DeciderError("reply too large")
        try:
            body = json.loads(data)
        except ValueError as e:  # includes bad UTF-8
            raise DeciderError(f"reply is not JSON: {e}") from e
        return ModelOutput(p_up=parse_decision(body, question))


def build_model(cfg: Config) -> Model:
    if cfg.model == "decider":
        return DeciderModel(cfg.decider_url, cfg.deadline_s)
    return MockModel()
