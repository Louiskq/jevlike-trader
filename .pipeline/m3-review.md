# M3 review (reviewer subagent, verbatim summary of findings)

## Findings
R1 [must-fix] server.py:149,:162 — `event_stream` calls `build_summary` synchronously on the event loop, which with `--dashboard` is also the trader's loop (run.py:116,:131). Each call reads every fill twice and every scored row three times, once per second per open tab; cost grows with the never-reset DB. At ~16k decisions / 3k fills: ~73 ms per call. 15 s dry run with 300 ms model: latency_ms max 310.7 -> 511.9 ms with 3 SSE clients (p50 unchanged). Dashboard inflates stored model latencies (the latency-percentile deliverable) and delays book.recv_ts stamping; worse with more tabs, longer runs (M5 soak), slower decider. Fix: build summary once per poll for all clients and/or asyncio.to_thread, or keep running totals. Test: N rows, K SSE clients, sleeping model, assert max(latency_ms) - sleep under a bound, or that build_summary never runs on the loop thread.
R2 [nit] app.js:83, index.html:45 — only the 60 s reliability curve is drawn; spec says 30 s and 300 s shown alongside. Bins are sent (server.py:98-101) but not shown; only Brier has all three. Ask user whether "alongside" covers curves.
R3 [nit] run.py:123-126 — startup-failure branch never runs on bind failure: uvicorn calls sys.exit(3) inside serve(); SystemExit escapes asyncio.run with "Task exception was never retrieved"; main() already printed the URL. No trading happens. Test: occupy a port, assert clean specific error and no decisions written.
R4 [nit] tests/test_dashboard_js.py:12 — JS tests need `node` (system package) and silently skip without it. SPEC: ask before adding system-package dependencies; no record of consent. CI has node and ran all 9 tests.

## Weak tests
- test_server.py::test_summary_calibration_three_horizons_with_sample_counts — passes if baseline_running held the in-sample baseline or labels were misordered (only `is not None`).
- test_run.py::test_stopping_the_server_stops_the_trader — tr.stopped always True because dry_run's finally sets it; missing callback only caught by 15 s timeout; position never checked.
- test_server.py::test_page_and_assets_served_with_csp_and_no_external_urls — only test touching app.js; passes if P&L labels swapped, 30 s curve headline, or Brier vs baseline_insample. Rendering has no tests.
- test_run.py::test_trader_live_reports_waiting_live_stale_and_kill — probes stale_s +/- 0.5; passes if run.py:81 used `<` not `<=`.
- test_run.py::test_dry_run_with_dashboard_serves_page_and_stream_then_shuts_down — reads only first 4 SSE lines; passes if real server never sent decision/fill/summary events after snapshot.

## Break-it candidates
- B1: swap sm.pnl_after_fees / sm.pnl_before_fees in app.js:70-71. Expected caught by: nothing.
- B2: app.js:93,95,101 use c.baseline_insample instead of baseline_running. Nothing.
- B3: calibration.py summary() sets baseline_running to baseline_brier_insample(ys). Not by any server test.
- B4: delete run.py:122 add_done_callback(server_done). Caught only via 15 s TimeoutError.
- B5: run.py:81 `<=` -> `<`. Nothing.
- Control: server.py:36 HEADLINE_FIRST = (30, 60, 300). Should be caught (is).

## Reading list
- Loopback only: server.py:167-183 (Host guard), :229-233 (make_server refuses non-loopback)
- Numbers from SQLite, P&L after/before fees etc.: server.py:68-111
- Brier vs baseline, 60 s headline, 30/300 alongside: server.py:36, :98-101; app.js:86-105
- Reliability curve (60 s only, R2): app.js:83, :186-213
- History reload / reconnect snapshot: server.py:114-123, :137-164; store.py read helpers
- Reconnect: logic.js:24-43, :49-109
- Lifecycle, kill linger, Ctrl-C: run.py:73-140
- Feed and kill state: run.py:73-86; app.js:41-53

## Plumbing (skim)
style.css, index.html, app.js:107-184, server.py:49-65, :126-134, trader.py diff, pyproject/uv.lock (fastapi, uvicorn, httpx dev, ruff ignore).

## Could be deleted
store.py:234-237 last_event (+ test_store.py:246-251); server.py:180-181 websocket branch; server.py:85 "now"; server.py:35,:148 retry hint; run.py:123-126; dead `tmp = Store(":memory:")` in test_run.py.

Local: 298 tests pass, ruff clean, CI green. No secrets in diff.
