"""Tests for static/logic.js (state updates and stream reconnection), run under node.
They need `node` on PATH: a missing node skips locally and fails on CI (node_required.py)."""

import json
import subprocess
from pathlib import Path

import pytest

from node_required import require_node

LOGIC = Path(__file__).parent.parent / "src" / "jevhl" / "static" / "logic.js"
pytestmark = pytest.mark.usefixtures("node")


@pytest.fixture(scope="module")
def node():
    require_node()


PRELUDE = f"""
const L = require({json.dumps(str(LOGIC))});
class FakeSource {{
  constructor() {{ this.handlers = {{}}; this.closed = false; }}
  addEventListener(n, f) {{ this.handlers[n] = f; }}
  close() {{ this.closed = true; }}
  emit(n, data) {{ this.handlers[n]({{ data: typeof data === "string" ? data : JSON.stringify(data) }}); }}
}}
function rig(staleMs) {{
  const r = {{ sources: [], statuses: [], events: [], timers: [], t: 0, cleared: [] }};
  const conn = L.createConnection({{
    open: () => {{ const s = new FakeSource(); r.sources.push(s); return s; }},
    onEvent: (n, d) => r.events.push([n, d]),
    onStatus: (s) => r.statuses.push(s),
    setTimer: (fn, ms) => {{ r.timers.push({{ fn, ms }}); return r.timers.length; }},
    clearTimer: (id) => r.cleared.push(id),
    now: () => r.t,
  }}, staleMs);
  r.conn = conn;
  return r;
}}
"""


def run_js(body: str):
    out = subprocess.run(
        ["node", "-e", PRELUDE + body], capture_output=True, text=True, timeout=20
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_backoff_doubles_and_caps():
    r = run_js("console.log(JSON.stringify([0,1,2,3,4,5,20].map(L.backoffMs)))")
    assert r == [1000, 2000, 4000, 8000, 10000, 10000, 10000]


def test_apply_event_sequence():
    r = run_js("""
      const s = L.emptyState();
      L.applyEvent(s, "snapshot", {summary: {a: 1}, decisions: [{id: 1}, {id: 2}], fills: [{id: 5}]});
      L.applyEvent(s, "decision", {id: 3});
      L.applyEvent(s, "decision", {id: 3});
      L.applyEvent(s, "decision", {id: 2});
      L.applyEvent(s, "fill", {id: 6});
      L.applyEvent(s, "fill", {id: 6});
      L.applyEvent(s, "summary", {a: 2});
      const a = JSON.parse(JSON.stringify(s));
      L.applyEvent(s, "snapshot", {summary: {a: 9}, decisions: [{id: 7}], fills: []});
      console.log(JSON.stringify([a, s]));
    """)
    first, second = r
    assert [d["id"] for d in first["decisions"]] == [1, 2, 3]
    assert [f["id"] for f in first["fills"]] == [5, 6]
    assert first["summary"] == {"a": 2}
    assert second == {"summary": {"a": 9}, "decisions": [{"id": 7}], "fills": []}


def test_history_is_capped():
    r = run_js("""
      const s = L.emptyState();
      L.applyEvent(s, "snapshot", {summary: {}, decisions: [], fills: []});
      for (let i = 1; i <= 350; i++) L.applyEvent(s, "decision", {id: i});
      for (let i = 1; i <= 120; i++) L.applyEvent(s, "fill", {id: i});
      console.log(JSON.stringify([s.decisions.length, s.decisions[0].id, s.fills.length, s.fills[0].id]));
    """)
    assert r == [300, 51, 100, 21]


def test_events_are_parsed_and_dispatched_and_bad_json_ignored():
    r = run_js("""
      const x = rig(15000); x.conn.connect();
      const s = x.sources[0];
      s.onopen();
      s.emit("decision", {id: 1});
      s.emit("summary", "{not json");
      console.log(JSON.stringify([x.events, x.statuses]));
    """)
    assert r == [[["decision", {"id": 1}]], ["connecting", "connected"]]


def test_error_reconnects_with_growing_backoff_and_resets_on_open():
    r = run_js("""
      const x = rig(15000); x.conn.connect();
      x.sources[0].onerror();                     // drop 1
      const ms1 = x.timers[0].ms;
      x.timers[0].fn();                           // reconnect
      x.sources[1].onerror();                     // drop 2, never opened
      const ms2 = x.timers[1].ms;
      x.timers[1].fn();
      x.sources[2].onopen();                      // success resets the backoff
      x.sources[2].onerror();
      const ms3 = x.timers[2].ms;
      console.log(JSON.stringify({ms1, ms2, ms3, n: x.sources.length, closed0: x.sources[0].closed,
        statuses: x.statuses}));
    """)
    assert (r["ms1"], r["ms2"], r["ms3"]) == (1000, 2000, 1000)
    assert r["n"] == 3 and r["closed0"] is True
    assert r["statuses"].count("reconnecting") == 3


def test_silent_stream_is_dropped_and_reconnected():
    r = run_js("""
      const x = rig(15000); x.conn.connect();
      x.sources[0].onopen();
      x.t = 14000; x.conn.check();                // within the limit: still connected
      const before = x.timers.length;
      x.sources[0].emit("summary", {a: 1});       // traffic resets the silence clock at t=14000
      x.t = 28000; x.conn.check();                // 14 s since last message: still fine
      const mid = x.timers.length;
      x.t = 30000; x.conn.check();                // 16 s of silence: drop
      x.timers[0].fn();
      console.log(JSON.stringify({before, mid, after: x.timers.length, n: x.sources.length,
        closed: x.sources[0].closed}));
    """)
    assert r == {"before": 0, "mid": 0, "after": 1, "n": 2, "closed": True}


def test_repeated_errors_schedule_one_reconnect_only():
    r = run_js("""
      const x = rig(15000); x.conn.connect();
      x.sources[0].onerror(); x.sources[0].onerror(); x.conn.check();
      console.log(JSON.stringify(x.timers.length));
    """)
    assert r == 1


def test_stop_cancels_pending_reconnect_and_ignores_old_source():
    r = run_js("""
      const x = rig(15000); x.conn.connect();
      x.sources[0].onerror();
      x.conn.stop();
      x.timers[0].fn();                           // a late timer must not reopen
      console.log(JSON.stringify({n: x.sources.length, cleared: x.cleared}));
    """)
    assert r == {"n": 1, "cleared": [1]}


def test_events_from_a_replaced_source_are_ignored():
    r = run_js("""
      const x = rig(15000); x.conn.connect();
      const old = x.sources[0];
      old.onerror(); x.timers[0].fn();
      old.emit("decision", {id: 99});
      x.sources[1].emit("decision", {id: 1});
      console.log(JSON.stringify(x.events));
    """)
    assert r == [["decision", {"id": 1}]]


def test_maker_snapshot_sets_a_longer_history():
    r = run_js("""
      const s = L.emptyState();
      L.applyEvent(s, "snapshot", {summary: {}, decisions: [], fills: [], history_decisions: 1800});
      for (let i = 1; i <= 1850; i++) L.applyEvent(s, "decision", {id: i});
      const a = s.decisions.length;
      L.applyEvent(s, "snapshot", {summary: {}, decisions: [], fills: []});
      for (let i = 1; i <= 350; i++) L.applyEvent(s, "decision", {id: i});
      console.log(JSON.stringify([a, s.decisions.length, "history" in s]));
    """)
    assert r == [1800, 300, False]
