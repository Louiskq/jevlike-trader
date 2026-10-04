// Pure dashboard logic, no DOM: state updates and the reconnecting stream. Loaded by the page
// as a classic script (global JevLogic) and by node for tests (module.exports).
(function (root) {
  "use strict";

  var MAX_DECISIONS = 300;
  var MAX_FILLS = 100;
  var EVENTS = ["snapshot", "decision", "fill", "summary"];

  function backoffMs(attempt) {
    return Math.min(1000 * Math.pow(2, attempt), 10000);
  }

  function emptyState() {
    return { summary: null, decisions: [], fills: [] };
  }

  function lastId(rows) {
    return rows.length ? rows[rows.length - 1].id : 0;
  }

  // A snapshot replaces everything (it is the full history from SQLite). Later rows are
  // appended only if newer than what we hold, so a repeated event is harmless.
  function applyEvent(state, name, data) {
    if (name === "snapshot") {
      state.summary = data.summary;
      state.decisions = data.decisions.slice(-MAX_DECISIONS);
      state.fills = data.fills.slice(-MAX_FILLS);
    } else if (name === "decision") {
      if (data.id > lastId(state.decisions)) {
        state.decisions.push(data);
        if (state.decisions.length > MAX_DECISIONS) state.decisions.shift();
      }
    } else if (name === "fill") {
      if (data.id > lastId(state.fills)) {
        state.fills.push(data);
        if (state.fills.length > MAX_FILLS) state.fills.shift();
      }
    } else if (name === "summary") {
      state.summary = data;
    }
    return state;
  }

  // Keeps one EventSource-like stream alive. A drop (error, or no message for staleMs)
  // closes it and reconnects with exponential backoff; the new stream starts with a snapshot.
  // deps: open() -> source, onEvent(name, data), onStatus(s), setTimer(fn, ms) -> id,
  // clearTimer(id), now() -> ms.
  function createConnection(deps, staleMs) {
    var source = null;
    var timer = null;
    var attempt = 0;
    var lastMsg = 0;
    var connected = false;
    var closed = false;

    function drop() {
      if (source) {
        try { source.close(); } catch (e) { /* already closed */ }
        source = null;
      }
      connected = false;
      if (closed || timer !== null) return;
      deps.onStatus("reconnecting");
      timer = deps.setTimer(function () { timer = null; connect(); }, backoffMs(attempt));
      attempt += 1;
    }

    function connect() {
      if (closed) return;
      deps.onStatus("connecting");
      var src = deps.open();
      source = src;
      src.onopen = function () {
        if (src !== source) return;
        attempt = 0;
        connected = true;
        lastMsg = deps.now();
        deps.onStatus("connected");
      };
      src.onerror = function () {
        if (src === source) drop();
      };
      EVENTS.forEach(function (name) {
        src.addEventListener(name, function (ev) {
          if (src !== source) return;
          lastMsg = deps.now();
          var data;
          try { data = JSON.parse(ev.data); } catch (e) { return; }
          deps.onEvent(name, data);
        });
      });
    }

    // Call about once a second: a stream that went silent is treated as dropped.
    function check() {
      if (connected && deps.now() - lastMsg > staleMs) drop();
    }

    function stop() {
      closed = true;
      if (timer !== null) deps.clearTimer(timer);
      timer = null;
      if (source) source.close();
      source = null;
    }

    return { connect: connect, check: check, stop: stop };
  }

  var api = {
    backoffMs: backoffMs, emptyState: emptyState, applyEvent: applyEvent,
    createConnection: createConnection,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.JevLogic = api;
})(typeof window !== "undefined" ? window : globalThis);
