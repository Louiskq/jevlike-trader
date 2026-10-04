## Built
Milestone 2 is built on feat/m2-live-dry-run. The store is now schema v3 with a per-horizon `outcomes` table. There is a validated l2Book feed with reconnect, a ledger, a paper-fill simulator, the `Trader` loop and a runnable dry run (`python -m jevhl.run`). Latency percentiles are computed from the decisions table. The 30-minute dry run is not done (you said I can't do it). I ran a 130 s smoke run against live testnet public data instead, and it was clean.

## Commits
- e8243e9 Store v3 per-horizon outcomes, ledger, latency report, with tests
- 5e92a37 Feed: l2Book parsing and reconnecting WebSocket stream, with tests
- 6c57648 Paper fills and the dry-run trader loop, with tests
- 0eb472d Dry-run entry point (python -m jevhl.run) with latency and calibration report

No Co-Authored-By trailers. PIPELINE.md was already modified in the working tree before I started (orchestrator's edit). I left it unstaged and uncommitted.

## Deviations from SPEC.md
- Added `websockets` 17.2 as a runtime dependency, which is pure Python with no system packages. SPEC says to ask before adding dependencies that need system packages, and this doesn't. It is the first runtime dependency; `uv.lock` changed. The SDK is not used yet; that is M4.
- Paper fills use negative tids and oid = tid, counting down from the lowest stored tid, so they can never collide with real exchange tids. Paper fee is 4.5 bps taker, and the limit band is 50 bps in the aggressive direction, rounded to exchange rules. These are my assumptions, not exchange facts. They are documented in `src/jevhl/paper.py`.
- Reduce-only exits skip the $10 minimum-notional check. I believe Hyperliquid exempts reduce-only orders, but I have not verified that on testnet.
- Opening an old-schema DB raises `RuntimeError` instead of silently creating a mixed schema. This follows from "nothing to migrate".
- `Store.pending(horizon_s, lo_ts, hi_ts)` takes a window rather than only a cutoff. An outcome is recorded only if the first book at or after t+h arrives within `stale_s` (15 s) of that mark. After an outage or restart the decision stays unscored rather than getting a much later mid. This is my design choice because SPEC is silent on it.
- Exits and outcomes are handled when the next snapshot arrives, so an exit happens 60 to about 65 s after the fill and the "mid at t+h" is the first book at or after t+h (up to about 5 s late). This follows from the snapshot cadence.
- Leftover position at startup: the position is rebuilt from the store and closed at the first book, because the exit timers are lost on restart. SPEC is silent on this.
- Staleness is judged on local receive time (`Book.recv_ts`), not the exchange timestamp. This avoids false stale holds from clock skew.

## Unsure about
- The 2 s deadline is enforced with `asyncio.wait_for` around `to_thread`. A hung model thread can't be killed; it keeps running, and the next snapshot starts a new decision. That is fine for the mock, but M5's HTTP decider should use its own request timeout.
- If the feed is down for more than 15 s, the stale-book kill switch trips and the bot stops. That follows SPEC, but it means a brief wifi drop ends the dry run. Reconnect and backoff exist in the feed, but the kill switch wins after 15 s.
- The kill-switch day baseline marks the position at the last decision mid before 00:00 UTC, falling back to the last fill price. Decision mids are the only stored price series, so this is my choice.
- `ruff format --check` reports 6 files that would be reformatted. I did not run the formatter. `ruff check` is clean, and I believe CI only runs `check`; please confirm.
- Overlap caveat for the README (also written in the `types.py` comment above `HORIZONS_S` and in `calibration.horizon_report`): consecutive decisions are about 5 s apart, so 300 s samples overlap by about 98% and are far less independent than 30 s or 60 s ones.

## Spec traceability
- Store v3 `outcomes(decision_id, horizon_s, mid_future, label)`, ties dropped per horizon -> src/jevhl/store.py
- `scored()` and calibration take a horizon -> store.py `scored`; calibration.py `horizon_report`, `all_horizon_reports`
- Horizons 30/60/300 -> src/jevhl/types.py `HORIZONS_S`; trader still uses `cfg.horizon_s` = 60
- Daily-loss baseline from the store -> ledger.py `day_start_pnl`; trader.py `kill_switch_from_store`
- feed -> feed.py; paper -> paper.py; ledger -> ledger.py; trader -> trader.py; latency -> latency.py; entry point -> run.py
- Late hold: `test_late_hold_with_mocked_clock_valid_p_is_kept_but_not_traded`, `test_just_inside_deadline_trades_non_float_exact`
- Hung model: `test_hung_model_times_out_to_late_hold_without_p`
- Stale book: `test_stale_book_is_a_hold_and_model_is_not_called`, `test_book_exactly_at_staleness_bound_is_still_fresh`, `test_stale_watchdog_trips_kill_switch_and_flattens`
- Realistic fixture: tests/fixtures/ws_session_btc.json (real public testnet capture)

## Verification
- Tests-first; 240 passed; ruff check clean.
- Mutation checks on trader.py each made a named test fail.
- Smoke: 130 s against testnet public data, exit 0; 25 decisions, 0 late holds, gap p50 5.32 s, model latency p50 0.83 ms, 3 simulated fills, fees $0.0959, kill switch not tripped.
- Not run: the 30-minute dry run (`uv run python -m jevhl.run --seconds 1800`).
