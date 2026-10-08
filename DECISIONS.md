# DECISIONS

Seeded from the spec interview (`.pipeline/spec-interview.md`). Entries are the user's choices; the rejected alternatives and the trade-off stated at the time are recorded so the reasoning is visible.

| Decision | Chosen | Rejected (and why) |
|---|---|---|
| Stack | Python | TypeScript: dashboard-friendly but a second language for the decider and calibration code |
| Cadence | 5 s per `l2Book` snapshot | 1 s on bbo+trades (deeper book stale, heavy overlap); fixed clock (repeats identical data) |
| Market | BTC perp on testnet | ETH (thinner); configurable-first (extra work) |
| Order type | Marketable IOC | Post-only: adverse selection, unfilled orders, harder reconciliation |
| Horizon | 60 s | 30 s (moves rarely beat fees); 5 min (too few independent samples) |
| Model output | `p_up` only | Triple (hold has no ground-truth label); p_up + abstain flag (two things to calibrate) |
| Calibration label | Sign of mid change, ties dropped | Beyond round-trip fee (mixes skill with cost assumptions) |
| Exit policy | Time exit at 60 s | Hold until opposite signal (drift); flat on hold (fee-heavy) |
| Storage | SQLite | JSONL (no indexing, full reload to compute calibration) |
| Gate default | 0.60 | 0.55 (more fee drag); 0.65 (too selective) |
| Decider deadline | 2 s | 1 s (tight); 3 s (staleness approaches 8 s) |
| Network and limits | Testnet only, no mainnet code. Defaults $500 position / $100 daily loss, configurable (amended after interview) | Original: $50/$10 hard caps with a typed-confirmation mainnet mode. Dropped because the user will never trade real money |
| Dashboard | FastAPI + SSE, localhost | Public deployment (exposes trading activity) |

## Evaluation plan
Software: tests pass; dry run decides every cycle; exchange UI reconciliation matches exactly; kill switch, wifi-off and short-deadline drills; overnight run with no crash or memory growth. Model: reliable answers within deadline, hold rate not stuck, reliability curve near the diagonal, Brier below the historical-frequency baseline over thousands of decisions. P&L comes last (see PROBLEM.md).

## Out of scope
Mainnet; withdrawals/transfers; multi-market; maker strategy; public dashboard; backtesting; training the decider.

## M1 / PR #6: decisions

- **Round up one lot to meet the $10 minimum order.** Chosen: keep (alternative: raise an error). Rationale (user): "I want to trade as much as possible to validate the idea."
- **Stale-book timer not armed until the first book arrives.** Chosen: keep for now (alternative: arm at startup and after reset). Rationale (user): "does not matter."
- **Late and invalid answer is recorded as `late`.** Chosen: keep (alternative: `invalid` wins). Rationale (user): "it is late."
- **Spec amended on this branch (e558bd4):** testnet only, no mainnet code; defaults $500 position / $100 daily loss, configurable. Alternative: original $50/$10 hard caps with a typed-confirmation mainnet mode. Rationale (user, in conversation): "i will not be trading real money at any point".

## M1 / PR #6: caught mistakes

> **M1 / PR #6: non-finite env values.** Caught by: reviewer. What: `float()` accepted `nan`/`inf`, so `MAX_DAILY_LOSS_USD=nan` was valid. Why it was wrong: every comparison with NaN is False, so the daily-loss kill switch could never trip. Fix: a7f596e. Prevention: parametrized `test_invalid_values_raise` cases for nan/inf.

> **M1 / PR #6: out-of-range probabilities reached calibration.** Caught by: reviewer. What: a model answer such as 1.5 was stored and scored. Why it was wrong: Brier became infinite and a negative probability wrapped into the top reliability bin. Fix: a987589. Prevention: `test_out_of_range_p_never_scored` and calibration rejection tests.

> **M1 / PR #6: fills deduplicated on (oid, ts, sz).** Caught by: reviewer. What: one order sweeping two resting orders of equal size lost its second fill. Why it was wrong: position, fees and P&L would be understated and the testnet-UI reconciliation would fail. Fix: 491f527. Prevention: `test_fills_same_oid_ts_sz_different_tid_both_stored`.

> **M1 / PR #6: daily-loss baseline reset on restart.** Caught by: reviewer. What: the baseline was the first P&L the object saw, and `reset()` cleared it. Why it was wrong: after a restart or reset, another full daily limit could be lost the same UTC day. Fix: 222f9ad. Prevention: `test_day_start_pnl_survives_restart`, `test_reset_keeps_day_baseline`.

> **M1 / PR #6: float tolerance untested.** Caught by: reviewer (break-it). What: setting `EPS = 0.0` in policy.py left all 107 tests passing. Why it was wrong: gates like 0.67 would silently fail to sell at the boundary. Fix: 615d979 (test only). Prevention: `test_boundary_trades_for_non_float_exact_gates`.

## M2: spec change (multi-horizon labelling)

- **Record outcomes at 30 s, 60 s and 300 s for every decision.** Trading, the gate and exits stay at 60 s; the dashboard headline Brier and reliability curve are 60 s, with 30 s and 300 s alongside. Alternative: 60 s only. Rationale (user): "yes do this". Caveat for the README: longer horizons overlap more, so their samples are less independent.

## M2 / PR #8: decisions

The user kept all four choices and gave no rationale ("no reason").

- **A feed outage over 15 s trips the stale-book kill switch and stops the bot.** Chosen: keep (alternative: hold and keep reconnecting). Rationale (user): none given.
- **An outcome at 30/60/300 s is recorded only if a book arrives within 15 s of the mark; otherwise the decision stays unscored.** Chosen: keep (alternative: use the first later book however late). Rationale (user): none given.
- **A position left over at startup is rebuilt from the store and closed at the first book.** Chosen: keep (alternative: restore the original 60 s timers). Rationale (user): none given.
- **Paper fills use a 4.5 bps taker fee and a 50 bps price band; reduce-only exits skip the $10 minimum.** Chosen: keep until M4 verifies them on testnet (alternative: different numbers or rules). Rationale (user): none given.

## M2 / PR #8: caught mistakes

> **M2 / PR #8: trade after the kill switch tripped mid-decision.** Caught by: reviewer. What: a model call in flight when the kill switch tripped still returned and bought. Why it was wrong: an extra round trip with fees after the stop (from M4 a real order after a kill). Fix: f341991. Prevention: `test_kill_switch_tripping_mid_decision_does_not_trade_and_holds_killed`.

> **M2 / PR #8: float residue blocked one side.** Caught by: reviewer. What: closing two same-side entries left a position of about 4e-19, which the policy read as long. Why it was wrong: every sell signal became hold/blocked, silently. Fix: 2fd8b9f. Prevention: `test_float_residue_after_closing_two_entries_does_not_block_opposite_signal`, `test_float_residue_snaps_to_exactly_flat`.

> **M2 / PR #8: run() hung after the stale watchdog tripped.** Caught by: reviewer. What: with the feed down, run() waited for a book that never came. Why it was wrong: the spec says stale book means flatten and stop. Fix: d42e440. Prevention: `test_run_returns_promptly_after_watchdog_trips_while_feed_is_down`.

> **M2 / PR #8: hung model got a new call every snapshot.** Caught by: reviewer. What: a timed-out model thread kept running while each snapshot started another. Why it was wrong: it broke "one decision in flight". Fix: 243845b. Prevention: `test_hung_model_thread_gets_no_second_predict_until_it_finishes`.

> **M2 / PR #8: a close left one lot open.** Caught by: builder (investigation). What: the float position 0.00015999... was floored to 0.00015 on a reduce-only close. Why it was wrong: one lot stayed open with no exit scheduled. Fix: a48d328. Prevention: `test_exit_closes_the_whole_lot_despite_float_error_in_position`.

> **M2 / PR #8: double kill-switch event, WAL files not ignored.** Caught by: reviewer. What: a mid-decision trip logged two events; SQLite WAL/SHM files were not gitignored. Fix: 6754608. Prevention: exactly-one assertion in the R1 test; `.gitignore` patterns.

> **M2 / PR #8: four deliberate bugs passed every test.** Caught by: reviewer (break-it). What: partial-exit retry removed, feed backoff never reset, order-error counter never reset, staleness judged on exchange time. Fix: 859e87f (tests only). Prevention: `test_partial_exit_is_retried_at_the_next_book`, `test_stream_backoff_doubles_on_failures_and_resets_after_a_book`, `test_order_error_counter_resets_after_a_successful_order`, and the three stale-book tests now using ts != recv_ts.

## M3 / PR #9: decisions

The user made these calls at the review gate and gave no rationale ("no reason").

- **Accept `node` as a test-only system dependency (the spec says to ask first).** Chosen: accept; a missing node fails the JS tests on CI and skips them locally (alternative: rewrite the JS tests without node). Rationale (user): none given.
- **Show reliability curves for all three horizons (60 s headline, 30 s and 300 s alongside), not Brier alone.** Chosen: all three curves (alternative: curve at 60 s only). Rationale (user): none given.
- **Fix the port-in-use error (R3) and make CI fail without node (R4) rather than leave them as nits.** Chosen: fix both. Rationale (user): none given.

## M3 / PR #9: caught mistakes

> **M3 / PR #9: the dashboard stalled the trader's event loop.** Caught by: reviewer. What: the summary was built on the loop shared with the trader, once per second per open tab (about 73 ms per call at a day of data). Why it was wrong: with three tabs and a 300 ms model, max stored model latency went from 311 ms to 512 ms, which would distort the latency table. Fix: 04d4244 (each client's summary now built in a worker thread; no shared cache). Prevention: `test_stream_never_builds_a_summary_on_the_event_loop_thread`.

> **M3 / PR #9: port in use gave a traceback and a false URL.** Caught by: reviewer. What: uvicorn exited the process on a bind failure after the URL had been printed. Fix: ff3d502. Prevention: `test_port_in_use_is_a_clean_specific_error_and_nothing_is_traded`.

> **M3 / PR #9: five deliberate bugs passed every test.** Caught by: reviewer (break-it). What: swapped after/before-fee P&L labels, in-sample instead of running baseline (in app.js and in the summary), stop-callback deleted, stale bound `<=` changed to `<`. Fix: 5384c2d (tests only). Prevention: `test_pnl_panel_labels_match_the_server_numbers`, `test_brier_table_compares_against_the_running_baseline`, `test_stopping_the_server_stops_the_trader`, `test_trader_live_reports_waiting_live_stale_and_kill`.

## M4 / PR #10: decisions

Made at the review gate. The user gave no rationale ("no reason").

- **Keep the new `hyperliquid-python-sdk` dependency (24 pure-Python or prebuilt-wheel packages, no system packages).** Chosen: keep (alternative: hand-write signing and REST calls). Rationale (user): none given.
- **Fix R3 (stale book), R4 (real reconcile) and R5 (60 s exit); leave R6-R8 as nits.** Rationale (user): none given.

Decided by the orchestrator when the user said "decide yourself" (not the user's reasoning): the first-hour size check is a startup warning, not a refusal; fills made by hand in the UI are closed 60 s after they fill.

## M5 / PR #11: decisions

Made at the review gate. The user gave no rationale ("no reason").

- **The HTTP contract: `POST /decide` with a question schema (options up/down); `p_up` is `probabilities["up"]`.** Chosen: confirmed by the user (alternative: a different format, to be described by the user). Rationale (user): none given.
- **Fix R1-R4 rather than leave them as nits.** Rationale (user): none given.

Decided by the orchestrator from the user's instructions (not the user's reasoning): the bot runs on the laptop and reaches the model through an SSH tunnel ("wherever latency is low"); the prompt in `src/jevhl/prompt.py` is the assistant's draft and the user has not edited it yet.
