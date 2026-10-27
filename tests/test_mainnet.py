"""The read-only mainnet reader (milestone 10): the only mainnet URL in the bot, the public
info endpoint, `l2Book` of BTC only, no key, no signing, no exchange or config code. These
tests are the SPEC hard constraint's enforcement: keep them strict."""

import ast
import asyncio
import inspect
import json
import random
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest
from finetune_data import Series

from jevhl import mainnet
from jevhl.mainnet import KEEP_S, MAINNET_INFO_URL, MainnetHistory, poll_mainnet
from jevhl.prompt import MAINNET_MAX_AGE_S, MAINNET_WINDOWS_S

SRC = Path(mainnet.__file__)
REST = json.loads((Path(__file__).parent / "fixtures" / "rest_l2book_btc.json").read_text())


class _Resp:
    def __init__(self, body: bytes):
        self.body = body

    def read(self, n=-1):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_mainnet_fetch_posts_only_l2book_btc_to_the_public_info_url(monkeypatch):
    seen = []

    def urlopen(req, timeout=None):
        seen.append((req, timeout))
        return _Resp(json.dumps(REST).encode())

    monkeypatch.setattr(mainnet.urllib.request, "urlopen", urlopen)
    assert mainnet.fetch_mainnet_l2book("BTC") == REST
    (req, timeout), = seen
    assert MAINNET_INFO_URL == "https://api.hyperliquid.xyz/info"
    assert req.full_url == MAINNET_INFO_URL
    assert req.get_method() == "POST"
    assert json.loads(req.data) == {"type": "l2Book", "coin": "BTC"}
    assert req.header_items() == [("Content-type", "application/json")]  # no auth of any kind
    assert timeout is not None and timeout <= 5
    with pytest.raises(ValueError):
        mainnet.fetch_mainnet_l2book("ETH")  # BTC only: what the prompt says and was trained on
    assert len(seen) == 1


def _imports():
    out = []
    for node in ast.walk(ast.parse(SRC.read_text())):
        if isinstance(node, ast.Import):
            out += [(0, a.name) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            out.append((node.level, node.module or ""))
    return out


def test_mainnet_reader_imports_only_stdlib_feed_prompt_and_types():
    for level, name in _imports():
        if level:
            assert name in ("feed", "prompt", "types"), name
        else:
            assert name == "__future__" or name.split(".")[0] in sys.stdlib_module_names, name


def test_importing_the_mainnet_reader_loads_no_exchange_config_or_signing_code():
    code = "import sys, jevhl.mainnet; print('\\n'.join(sorted(sys.modules)))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True, cwd=Path(__file__).parent.parent).stdout.split()
    ours = {m for m in out if m.split(".")[0] == "jevhl"}
    assert ours == {"jevhl", "jevhl.mainnet", "jevhl.feed", "jevhl.prompt", "jevhl.types"}
    assert not [m for m in out if m.split(".")[0] in ("hyperliquid", "eth_account", "eth_keys")]


def test_mainnet_reader_has_one_url_and_never_reads_the_environment():
    text = SRC.read_text()
    urls = {n.value for n in ast.walk(ast.parse(text))
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and "://" in n.value}
    assert urls == {"https://api.hyperliquid.xyz/info"}
    assert text.count("hyperliquid.xyz") == 1
    for w in ("environ", "getenv", "dotenv", ".env", "api_key", "secret", "/exchange"):
        assert w not in text, w


def test_mainnet_reader_takes_nothing_key_like():
    bad = ("key", "secret", "address", "account", "wallet", "cfg", "config", "private", "sign")
    funcs = [f for _, f in inspect.getmembers(mainnet, inspect.isfunction)
             if f.__module__ == mainnet.__name__]
    for _, cls in inspect.getmembers(mainnet, inspect.isclass):
        if cls.__module__ == mainnet.__name__:
            funcs += [f for _, f in inspect.getmembers(cls, inspect.isfunction)]
    assert funcs
    for f in funcs:
        for p in inspect.signature(f).parameters:
            assert not any(w in p.lower() for w in bad), (f.__name__, p)


def test_mainnet_history_answers_by_receive_time():
    h = MainnetHistory()
    assert h.at(100.0) == (None, float("inf")) and h.latest() is None
    h.add(100.0, 80000.0)
    h.add(101.3, 80001.0)
    assert h.at(99.9) == (None, float("inf"))
    assert h.at(100.0) == (80000.0, 0.0)
    mid, age = h.at(101.0)
    assert mid == 80000.0 and age == pytest.approx(1.0)
    mid, age = h.at(104.63)
    assert mid == 80001.0 and age == pytest.approx(3.33)
    assert h.latest() == (101.3, 80001.0)


def test_mainnet_history_matches_the_training_series():
    rng = random.Random(4)
    ts, t = [], 1000.0
    for _ in range(300):
        t += rng.choice([0.2, 0.9, 1.0, 1.1, 3.0, 7.5])
        ts.append(t)
    mids = [80000 + rng.gauss(0, 20) for _ in ts]
    h = MainnetHistory()
    for x, m in zip(ts, mids, strict=True):
        h.add(x, m)
    full = Series(ts, mids)
    for _ in range(500):
        q = rng.uniform(ts[-1] - KEEP_S + 5, ts[-1] + 3)
        assert h.at(q) == full.at(q), q


def test_mainnet_history_prunes_only_what_no_lookup_can_use():
    # a decision may run a few seconds after the latest mainnet read: its 60 s lookup and that
    # lookup's 5 s freshness must still see everything the training series would have seen
    assert KEEP_S >= max(MAINNET_WINDOWS_S) + MAINNET_MAX_AGE_S + 30
    h = MainnetHistory()
    ts = [1000.0 + 0.97 * i for i in range(400)]
    for x in ts:
        h.add(x, 80000.0 + x)
    full = Series(ts, [80000.0 + x for x in ts])
    assert len(h) <= KEEP_S / 0.97 + 2  # bounded
    for q in (ts[-1] - 60 - 5.37, ts[-1] - 60 - 4.63, ts[-1] - 90.0):
        assert h.at(q) == full.at(q)


def test_mainnet_poller_events_are_prefixed_and_it_reads_btc_only():
    calls, events = [], []
    replies = [urllib.error.HTTPError(MAINNET_INFO_URL, 429, "Too Many Requests", {}, None),
               ValueError("boom"), REST]

    def fetch(coin):
        calls.append(coin)
        r = replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    async def nosleep(d):
        pass

    async def first_book():
        stream = poll_mainnet(lambda: 5000.0, on_event=lambda k, d: events.append((k, d)),
                              fetch=fetch, sleep=nosleep)
        return await anext(stream)

    book = asyncio.run(first_book())
    assert book.coin == "BTC" and book.recv_ts == 5000.0 and book.mid > 0
    assert calls == ["BTC", "BTC", "BTC"]
    assert [k for k, _ in events] == ["mainnet_rest_error", "mainnet_rest_error"]
    assert "429" in events[0][1]
