// Dashboard rendering. All data is written with textContent or canvas drawing, never as HTML.
// Numbers (P&L, fees, Brier, counts) are shown exactly as the server computed them.
(function () {
  "use strict";

  var state = JevLogic.emptyState();
  var $ = function (id) { return document.getElementById(id); };

  function fmt(x, nd) {
    return typeof x === "number" && isFinite(x) ? x.toFixed(nd) : "n/a";
  }
  function signed(x, nd) {
    return typeof x === "number" && isFinite(x) ? (x > 0 ? "+" : "") + x.toFixed(nd) : "n/a";
  }
  function timeOf(ts) { return new Date(ts * 1000).toLocaleTimeString(); }

  function el(tag, text, cls) {
    var e = document.createElement(tag);
    if (text !== undefined && text !== null) e.textContent = String(text);
    if (cls) e.className = cls;
    return e;
  }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function sign(x) { return typeof x !== "number" || x === 0 ? "" : x > 0 ? "good" : "bad"; }

  function fillDl(id, rows) {
    var dl = $(id);
    clear(dl);
    rows.forEach(function (r) {
      dl.appendChild(el("dt", r[0]));
      dl.appendChild(el("dd", r[1], r[2]));
    });
  }

  function chip(id, text, cls) {
    var c = $(id);
    c.textContent = text;
    c.className = "chip " + (cls || "");
  }

  function renderStatus(s) {
    var cls = s === "connected" ? "good" : s === "connecting" ? "warn" : "bad";
    chip("chip-stream", "stream: " + s, cls);
  }

  function renderChips(sm) {
    var feed = sm.feed;
    chip("chip-feed", "feed: " + feed, feed === "live" ? "good" : feed === "waiting" ? "warn" : "bad");
    var k = sm.kill;
    var text = "kill switch: " + (k.state === "tripped" ? "TRIPPED (" + k.reason + ")" : k.state);
    chip("chip-kill", text + (sm.stopped ? ", stopped" : ""),
      k.state === "ok" ? "good" : k.state === "tripped" ? "bad" : "warn");
  }

  function renderSummary(sm) {
    var d = sm.decision;
    fillDl("decision", d ? [
      ["Time", timeOf(d.ts)],
      ["p_up", d.p_up === null ? "n/a" : fmt(d.p_up, 4)],
      ["Action", d.action, d.action === "buy" ? "good" : d.action === "sell" ? "bad" : ""],
      ["Reason", d.reason],
      ["Gate", fmt(d.gate, 2)],
      ["Size $", fmt(d.size_usd, 2)],
      ["Latency ms", d.latency_ms === null ? "n/a" : fmt(d.latency_ms, 1)],
    ] : [["No decisions yet", ""]]);
    fillDl("pnl", [
      ["Mid", fmt(sm.mid, 2)],
      ["Position (base)", fmt(sm.position, 5)],
      ["Position $", fmt(sm.position_usd, 2)],
      ["P&L after fees $", signed(sm.pnl_after_fees, 4), sign(sm.pnl_after_fees)],
      ["P&L before fees $", signed(sm.pnl_before_fees, 4), sign(sm.pnl_before_fees)],
      ["Cumulative fees $", fmt(sm.fees, 4)],
    ]);
    fillDl("counts", [
      ["Decisions", sm.decisions],
      ["Holds", sm.holds],
      ["Late holds", sm.late_holds],
      ["Hold rate", sm.hold_rate === null ? "n/a" : fmt(sm.hold_rate * 100, 1) + "%"],
      ["Fills", sm.n_fills],
    ]);
    renderChips(sm);
    renderBrier(sm.calibration);
    sm.calibration.forEach(drawReliability);
  }

  function renderBrier(cal) {
    var body = $("brier").tBodies[0];
    clear(body);
    cal.forEach(function (c) {
      var tr = el("tr");
      var verdict = "n/a";
      var cls = "";
      if (c.brier !== null && c.baseline_running !== null) {
        if (c.n < 30) { verdict = "too few samples"; cls = "warn"; }
        else if (c.brier < c.baseline_running) { verdict = "beats baseline"; cls = "good"; }
        else { verdict = "no better than baseline"; cls = "bad"; }
      }
      tr.appendChild(el("td", c.horizon_s + " s" + (c.horizon_s === 60 ? " (headline)" : "")));
      tr.appendChild(el("td", c.n));
      tr.appendChild(el("td", fmt(c.brier, 4)));
      tr.appendChild(el("td", fmt(c.baseline_running, 4)));
      tr.appendChild(el("td", verdict, cls));
      body.appendChild(tr);
    });
  }

  function renderTables() {
    var fb = $("fills").tBodies[0];
    clear(fb);
    state.fills.slice(-15).reverse().forEach(function (f) {
      var tr = el("tr");
      [timeOf(f.ts), f.side, fmt(f.sz, 5), fmt(f.px, 2), fmt(f.fee, 4)].forEach(function (v, i) {
        tr.appendChild(el("td", v, i === 1 ? (f.side === "buy" ? "good" : "bad") : ""));
      });
      fb.appendChild(tr);
    });
    var db = $("decisions").tBodies[0];
    clear(db);
    state.decisions.slice(-30).reverse().forEach(function (d) {
      var tr = el("tr");
      [timeOf(d.ts), d.action, d.p_up === null ? "n/a" : fmt(d.p_up, 4), d.reason,
        fmt(d.size_usd, 2), d.latency_ms === null ? "n/a" : fmt(d.latency_ms, 1)
      ].forEach(function (v, i) {
        tr.appendChild(el("td", v, i === 1 ? (d.action === "buy" ? "good" : d.action === "sell" ? "bad" : "") : ""));
      });
      db.appendChild(tr);
    });
  }

  var COLORS = { line: "#7aa2f7", good: "#4cc38a", bad: "#ef6b6b", grid: "#2a313b", text: "#8793a1" };

  function ctxOf(id) {
    var c = $(id);
    var g = c.getContext("2d");
    g.clearRect(0, 0, c.width, c.height);
    return { g: g, w: c.width, h: c.height };
  }

  function tri(g, x, y, up, color) {
    g.fillStyle = color;
    g.beginPath();
    if (up) { g.moveTo(x, y - 7); g.lineTo(x - 5, y + 3); g.lineTo(x + 5, y + 3); }
    else { g.moveTo(x, y + 7); g.lineTo(x - 5, y - 3); g.lineTo(x + 5, y - 3); }
    g.closePath();
    g.fill();
  }

  function drawMid() {
    var o = ctxOf("mid-chart"), g = o.g, ds = state.decisions, fs = state.fills;
    if (ds.length < 2) {
      g.fillStyle = COLORS.text; g.fillText("waiting for decisions", 20, 30);
      return;
    }
    var padL = 64, padR = 10, padT = 10, padB = 20;
    var t0 = ds[0].ts, t1 = ds[ds.length - 1].ts;
    var lo = Infinity, hi = -Infinity;
    ds.forEach(function (d) { lo = Math.min(lo, d.mid); hi = Math.max(hi, d.mid); });
    fs.forEach(function (f) { if (f.ts >= t0) { lo = Math.min(lo, f.px); hi = Math.max(hi, f.px); } });
    if (hi - lo < 1e-9) { hi += 1; lo -= 1; }
    var span = hi - lo; lo -= span * 0.05; hi += span * 0.05;
    var X = function (t) { return padL + (t - t0) / (t1 - t0 || 1) * (o.w - padL - padR); };
    var Y = function (v) { return padT + (hi - v) / (hi - lo) * (o.h - padT - padB); };
    g.strokeStyle = COLORS.grid; g.fillStyle = COLORS.text; g.font = "11px sans-serif"; g.lineWidth = 1;
    for (var i = 0; i <= 4; i++) {
      var v = lo + (hi - lo) * i / 4;
      g.beginPath(); g.moveTo(padL, Y(v)); g.lineTo(o.w - padR, Y(v)); g.stroke();
      g.fillText(v.toFixed(2), 4, Y(v) + 4);
    }
    g.fillText(timeOf(t0), padL, o.h - 4);
    g.fillText(timeOf(t1), o.w - padR - 60, o.h - 4);
    g.strokeStyle = COLORS.line; g.lineWidth = 1.5; g.beginPath();
    ds.forEach(function (d, k) { if (k) g.lineTo(X(d.ts), Y(d.mid)); else g.moveTo(X(d.ts), Y(d.mid)); });
    g.stroke();
    ds.forEach(function (d) {
      if (d.action === "buy") tri(g, X(d.ts), Y(d.mid), true, COLORS.good);
      else if (d.action === "sell") tri(g, X(d.ts), Y(d.mid), false, COLORS.bad);
    });
    g.lineWidth = 2;
    fs.forEach(function (f) {
      if (f.ts < t0) return;
      g.strokeStyle = f.side === "buy" ? COLORS.good : COLORS.bad;
      g.beginPath(); g.arc(X(f.ts), Y(f.px), 5, 0, 2 * Math.PI); g.stroke();
    });
  }

  function drawReliability(c) {
    var o = ctxOf("rel-chart-" + c.horizon_s), g = o.g;
    var pad = 36, size = o.w - pad - 10;
    var X = function (p) { return pad + p * size; }, Y = function (p) { return 10 + (1 - p) * size; };
    g.strokeStyle = COLORS.grid; g.fillStyle = COLORS.text; g.font = "11px sans-serif"; g.lineWidth = 1;
    for (var i = 0; i <= 5; i++) {
      var p = i / 5;
      g.beginPath(); g.moveTo(X(p), Y(0)); g.lineTo(X(p), Y(1)); g.stroke();
      g.beginPath(); g.moveTo(X(0), Y(p)); g.lineTo(X(1), Y(p)); g.stroke();
      g.fillText(p.toFixed(1), X(p) - 8, Y(0) + 14);
      g.fillText(p.toFixed(1), 8, Y(p) + 4);
    }
    g.strokeStyle = COLORS.text; g.setLineDash([4, 4]);
    g.beginPath(); g.moveTo(X(0), Y(0)); g.lineTo(X(1), Y(1)); g.stroke(); g.setLineDash([]);
    g.fillText("mean predicted p_up", X(0.3), Y(0) + 28);
    g.save(); g.translate(10, Y(0.2)); g.rotate(-Math.PI / 2); g.fillText("observed frequency up", 0, 0); g.restore();
    var pts = c.bins.filter(function (b) { return b.count > 0; });
    g.strokeStyle = COLORS.line; g.lineWidth = 1.5; g.beginPath();
    pts.forEach(function (b, k) { if (k) g.lineTo(X(b.mean_p), Y(b.freq)); else g.moveTo(X(b.mean_p), Y(b.freq)); });
    g.stroke();
    var maxN = pts.reduce(function (m, b) { return Math.max(m, b.count); }, 1);
    g.fillStyle = COLORS.line;
    pts.forEach(function (b) {
      g.beginPath(); g.arc(X(b.mean_p), Y(b.freq), 3 + 5 * Math.sqrt(b.count / maxN), 0, 2 * Math.PI); g.fill();
    });
    $("rel-note-" + c.horizon_s).textContent = "n = " + c.n + " scored decisions; dot size = samples in the bin. "
      + "Below the dashed line means the model is overconfident in up.";
  }

  function render() {
    if (state.summary) renderSummary(state.summary);
    renderTables();
    drawMid();
  }

  function onEvent(name, data) {
    JevLogic.applyEvent(state, name, data);
    render();
  }

  var conn = JevLogic.createConnection({
    open: function () { return new EventSource("/events"); },
    onEvent: onEvent,
    onStatus: renderStatus,
    setTimer: function (fn, ms) { return window.setTimeout(fn, ms); },
    clearTimer: function (id) { window.clearTimeout(id); },
    now: function () { return Date.now(); },
  }, 15000);
  window.setInterval(conn.check, 1000);
  conn.connect();
})();
