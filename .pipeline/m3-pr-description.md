Closes #3

Milestone 3 delivers the dashboard: a loopback-only server with an SSE stream, a static page, store read helpers, and a `--dashboard` flag on the dry run. Everything shown is read from SQLite.

## What's in it
- **Store read helpers (29f420d), server, SSE and page (0891d60).**
- **Dashboard wired into the dry run (`--dashboard`), reconnect fix.**
- **Review fixes:** 04d4244 (summary off the event loop), c31f3b9 (curves for all three horizons), ff3d502 (port-in-use error), a017ac6 (node required on CI), 5384c2d (tests only), 05cd2f0 (dead code removed).

## Decisions
Made at the review gate. Rationale given for each, in the user's words: "no reason".
- **Accept `node` as a test-only dependency; a missing node fails CI:** no reason.
- **Reliability curves for 30 s, 60 s and 300 s, not Brier alone:** no reason.
- **Fix the port-in-use error and the silent node skip rather than leave them:** no reason.

## Caught in review
- **Caught (reviewer):** the dashboard built its summary on the trader's event loop, inflating stored model latency. Fixed in 04d4244.
- **Caught (reviewer):** port in use gave a traceback and a misleading URL. Fixed in ff3d502.
- **Caught (reviewer):** JS tests skipped silently without node. Fixed in a017ac6.
- **Caught (reviewer):** only the 60 s curve was drawn. Fixed in c31f3b9.
- **Caught (reviewer, break-it):** five deliberate bugs passed all tests. Tests added in 5384c2d.
- **Caught (me):** nothing raised by me at the review gate.

Left as-is: each open tab builds its own summary (in a thread), so many tabs multiply database reads; the websocket guard and SSE `retry:` hint stay.

## Break-it checks
| Deliberate bug | Tests that failed |
|---|---|
| P&L after/before-fee labels swapped | none before 5384c2d; now 1: test_pnl_panel_labels_match_the_server_numbers |
| app.js uses in-sample baseline | none before; now 1: test_brier_table_compares_against_the_running_baseline |
| summary `baseline_running` set to in-sample | 2: test_summary, test_summary_calibration_three_horizons_with_sample_counts |
| stop callback deleted | caught only by a 15 s timeout before; now fails within 5 s: test_stopping_the_server_stops_the_trader |
| stale bound `<=` -> `<` | none before; now 1: test_trader_live_reports_waiting_live_stale_and_kill |

(Builder-run counts; full-suite run per mutant.)

## Deviations from the spec
1. Adds `fastapi` and `uvicorn` (pure Python) and `httpx` (dev). Tests also require `node` (accepted by the user).

## Verification
- Reproduce locally: `uv sync --locked && uv run ruff check && uv run pytest` (309 tests)
- Smoke: `uv run python -m jevhl.run --dashboard --seconds 20`, page served on 127.0.0.1:8765.
