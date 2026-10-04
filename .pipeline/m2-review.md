## Findings
R1 [must-fix] src/jevhl/trader.py:144-148 (with :86-89): after the model call returns, `_decide` goes straight to `insert_decision` and `_enter` without checking `self.stopped` or `self._kill.tripped`. A kill switch that trips while a decision is in flight does not stop that decision from trading.
   Breaks when: the bot is long and the model call is in flight. Meanwhile a concurrent `on_book` trips daily_loss (or the watchdog trips stale_book), so `_halt` flattens and sets `stopped`. The model then returns p=0.9. `decide` now sees position 0 and logs a "buy signal", and `_enter` buys a fresh $500. The `_risk` call at :89 then flattens it again. The result is an extra round trip, with fees, after the kill switch. Probe output: decisions `[buy signal, buy signal]`, fills `[buy, sell, buy, sell]`. From M4 on this is a real order sent after a kill. The "killed" reason in REASONS is never used.
   Suggested test: trip `check_stale()` while a `real_sleep` model call is in flight, then assert no fills and the decision is held as "killed".
R2 [must-fix] src/jevhl/trader.py:164 (and ledger.py:24-26): the raw float `ledger.position` is passed to `decide`. Float residue from opening and closing several entries blocks one side indefinitely, and nothing logs it.
   Breaks when: two same-side entries are opened and both exit, e.g. buy 0.00384 + buy 0.00385, then sell both. Position is 4.3e-19, not 0. That is below `_dust`, so no exit is scheduled, but `decide` sees `position_usd > 0` and every sell signal becomes `hold/blocked`. In 39 of 56 size pairs tried, the residue was non-zero. A trader-level probe ended with position 2.17e-19 and the next p=0.1 decision was `('hold', 'blocked')`.
   Suggested test: open two same-side entries of different lot sizes, let both exit, send an opposite signal, and assert the action is a sell, not blocked.
R3 [must-fix] src/jevhl/trader.py:217-220 with run.py:38-41: after the watchdog trips stale_book and sets `stopped`, `run()` only notices at its next `if self.stopped: break`. That check runs only when the next book arrives. While the feed is down, `stream_books` keeps reconnecting and never yields, so the process never stops and never prints the report. Spec: "stale book: ... flatten, stop".
   Breaks when: the network is off for longer than 15 s and the bot runs without `--seconds`. Probe: `wait_for(tr.run(...), 1.0)` timed out with `stopped=True`.
   Suggested test: feed one book, then await forever, run the watchdog, and assert `run()` returns within about 1 s of the trip.
R4 [nit] src/jevhl/trader.py:154-158: on a timeout, the `asyncio.to_thread` worker keeps running, and `_inflight` is cleared at :88. A hung model therefore gets a fresh `predict` call on every snapshot, all running at the same time. That contradicts "one decision in flight", and `asyncio.run` waits for those threads at exit. Harmless with MockModel but has to be fixed before the M5 decider.
   Suggested test: a model that blocks on an Event, three books, then assert `predict` was entered once.
R5 [nit] src/jevhl/trader.py:89 + :196: when the kill switch trips during an in-flight decision, `_halt` runs twice and logs two `kill_switch` events. M3 dashboard counts will double.
   Suggested test: assert exactly one kill_switch event in the R1 scenario.
R6 [nit] .gitignore: Store uses WAL mode, and `jevhl.db-wal` / `jevhl.db-shm` match none of `*.db`, `*.db-journal` or `*.sqlite*`. A dry run killed with SIGKILL leaves files holding decision data that `git add .` would pick up. Add the patterns.

## Weak tests
- tests/test_feed.py::test_stream_reconnects_and_resubscribes_after_drop: `sleeps[1] >= sleeps[0]` passes with sleeps == [1.0, 1.0]. Passes if the backoff never doubles (feed.py:144) or never resets (feed.py:139).
- tests/test_paper.py::test_tids_continue_below_existing_after_restart: would still pass if `min_tid` lost its `min(0, ...)` clamp (store.py:192).
- tests/test_trader.py stale tests: `mkbook` sets `ts == recv_ts`, so they pass even if staleness were judged on exchange time `book.ts`.
- tests/test_trader.py::test_daily_loss_trips_flattens_and_stops: passes without the post-decision `_risk` call at trader.py:89.
- No trader test covers order-error wiring: deleting `self._kill.on_order_result(True)` at trader.py:175 leaves all 240 tests passing.
- No test covers the partial-exit retry: replacing trader.py:109 with `pass` leaves all 240 tests passing.
- tests/test_run.py::test_dry_run_replays_real_books...: would still pass if `format_report` printed the wrong horizon's Brier on every line.

## Break-it candidates
- B1: no retry of partial exits. Edit src/jevhl/trader.py:109 to `pass`. Expected to be caught by: nothing (verified: 240 pass).
- B2: backoff never resets. Edit src/jevhl/feed.py:139 to delete `backoff = 1.0`. Expected to be caught by: nothing (verified).
- B3: order-error counter never resets on success. Edit src/jevhl/trader.py:175 to `pass`. Expected to be caught by: nothing (verified).
- B4: staleness judged on exchange time. Edit src/jevhl/trader.py:138 so `book.recv_ts` becomes `book.ts`. Expected to be caught by: nothing (verified).
- B5: timeout latency logged as 0. Edit src/jevhl/trader.py:158 to `ms = 0.0`. Expected to be caught by: test_hung_model_times_out_to_late_hold_without_p (verified: caught).

## Reading list
- Loop: one decision per snapshot, one in flight, hold when stale -> src/jevhl/trader.py:71-89, 135-148
- 2 s deadline, late means hold -> src/jevhl/trader.py:150-165 (plus policy.py `decide` late check)
- Time exit 60 s after fill, reduce-only -> src/jevhl/trader.py:99-110, 167-183; src/jevhl/paper.py:49-57
- Kill switch: flatten and stop; stale watchdog -> src/jevhl/trader.py:116-131, 187-229; src/jevhl/run.py:38-66
- Outcomes at 30/60/300 s, ties dropped per horizon (schema v3) -> src/jevhl/trader.py:91-97; src/jevhl/store.py:14-35, 99-141
- Calibration takes a horizon -> src/jevhl/calibration.py:98-114
- Daily loss per UTC day survives restart -> src/jevhl/ledger.py:46-57; trader.py:34-37
- Parsing realistic messages -> src/jevhl/feed.py:74-104
- Latency percentiles from own logs -> src/jevhl/latency.py:19-36

## Plumbing (safe to skim)
- src/jevhl/feed.py:107-144; src/jevhl/paper.py:22-28, 59-83; src/jevhl/run.py:29-35, 69-117; src/jevhl/latency.py:8-16; tests/fixtures/ws_session_btc.json (no secrets or paths found); SPEC.md / DECISIONS.md consistent; uv.lock, pyproject.toml adds `websockets`.

## Could be deleted
- src/jevhl/paper.py:37: `self.meta = self._meta = meta` keeps two names for the same object.
- src/jevhl/ledger.py:13: `SECONDS_PER_DAY` duplicates risk.SECONDS_PER_DAY.

Notes: 240 tests pass and `ruff check` is clean. The 30-minute live acceptance run has no evidence in the PR yet.
