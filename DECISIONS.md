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

- **A feed outage over 15 s trips the stale-book kill switch and stops the bot.** Chosen: keep (alternative: hold and keep reconnecting). Rationale (user): none given. *Superseded in M7 / PR #15: a stale-book kill now flattens, pauses and may resume (see below).*
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

## M6 / PR #13: decisions

Made at the review gate. The user gave no rationale ("no reason").

- **Fix R1-R4 (false GAVE UP on one Ctrl-C, `--seconds` cutting the retry short, a second Ctrl-C taking tens of seconds, nothing printed during a retry); document R5 (the window bounds when an attempt may start, so the end can overrun by about 43 s).** Chosen: fix R1-R4, document R5 (alternatives: fix R1 and R2 only; fix all five). Rationale (user): none given.
- **Accept that a Ctrl-C mid-retry abandons the in-flight network call, which may place a close order the run never books (the next start reads it back from the exchange).** Chosen: accept as is (alternative: an extra position check at exit). Rationale (user): none given.

Decided by the orchestrator when the user said "make the decision for me" (not the user's reasoning): fix the flatten gap before the overnight soak (issue #12) instead of listing it as a limitation, because the soak is unattended.

## M6 / PR #13: caught mistakes

> **M6 / PR #13: one Ctrl-C reported "FLATTEN GAVE UP".** Caught by: reviewer. What: on Python 3.13 the first SIGINT cancels the main task and the cancel becomes a KeyboardInterrupt after a clean shutdown, so every normal stop printed a false give-up, logged a false event and exited 130 without the normal report. Fix: e3e6b22. Prevention: `test_one_ctrl_c_stops_a_live_run_with_the_normal_report_and_no_gave_up`.

> **M6 / PR #13: `--seconds` cut the flatten retry short.** Caught by: reviewer. What: the duration timeout cancelled the watchdog that held the retrying flatten, leaving the position open. Fix: 9a4c307. Prevention: `test_seconds_does_not_cut_a_flatten_retry_short`.

> **M6 / PR #13: a second Ctrl-C waited for a blocked network thread; nothing printed during a retry.** Caught by: reviewer. Fix: c7fbd85 and 70f56d2. Prevention: `test_a_second_ctrl_c_mid_attempt_returns_at_once_and_says_the_position_may_be_open`, `test_each_failed_attempt_and_the_outcome_are_printed_to_stderr`.

> **M6 / PR #13: deleting the KeyboardInterrupt handler passed all 547 tests.** Caught by: reviewer (break-it). Fix: 742df0f (tests only). Prevention: the two named SIGINT tests above.

## M7 / PR #15: decisions

Made at the review gate. The user gave no rationale ("no reason").

- **A stale-book kill cancels, flattens, then pauses; the bot resumes after 60 s of steady books, at most 5 times per run; daily-loss and order-error kills stay permanent.** Requested by the user after the 2026-10-09 soak ("can you try fix it then"). Supersedes the M2 decision above. Rationale (user): none given.
- **Fix R1 (drop a socket that answers pings but sends no book), R2 ("paused at the end" in the report) and R4 (docstring).** Chosen: fix R1, R2, R4 (alternatives: R1 only; all plus an end-to-end test). Rationale (user): none given.

Decided by the builder, on evidence, and not chosen either way by the user: the gap bound before resuming is 8 s, not the brief's 2 s (soak.db shows books 5.2 s apart on average, up to 6.1 s). Each pause gets a fresh flatten retry window; a resume is blocked unless the ledger and the exchange are flat.

## M7 / PR #15: caught mistakes

> **M7 / PR #15: a socket that answered pings but sent no books was never dropped.** Caught by: reviewer. What: any message, a pong included, counted as proof of life, so a stalled subscription would pause the bot forever (one plausible shape of the 2026-10-09 incident); the test `test_any_message_after_a_ping_keeps_the_socket...` locked the bug in. Fix: 249bde6. Prevention: `test_a_pong_only_socket_is_dropped_and_reconnected_before_the_stale_pause` and the pong-only feed tests.

> **M7 / PR #15: "(paused at the end)" could never print.** Caught by: reviewer. Fix: 2b47648. Prevention: `test_report_says_paused_at_the_end_after_a_run_stopped_while_paused`.

> **M7 / PR #15: DECISIONS.md still described a permanent stale stop.** Caught by: reviewer. Fix: this entry and the note on the M2 decision.

> **M7 / PR #15: two deliberate bugs passed every test.** Caught by: reviewer (break-it). What: the dashboard using the raw paused flag; deleting the stop re-check after the resume-time flatten. Fix: f441b58 (tests only). Prevention: `test_trader_live_shows_a_bot_stopped_while_paused_as_stopped_not_paused`, `test_a_stop_during_the_resume_time_flatten_ends_the_resume_before_any_position_read`.

## M8 / PR #17: decisions

Made at the spec, plan and review gates (2026-10-10/11). The user gave no rationale ("no reason").

- **A maker strategy as `STRATEGY=maker`, next to the unchanged taker.** Chosen over replacing the taker. Rationale (user): none given.
- **About one decision a second; the quote is replaced only when its side or price changes.** Chosen after the measurement that testnet's book changes slowly (alternatives: replace every second; back to ~5 s). Rationale (user): none given.
- **A Jarrod-style prompt, drafted by the assistant for the user to edit.** The prompt in `src/jevhl/prompt.py` is still the assistant's draft. Rationale (user): none given.
- **Always quote buy or sell; hold only when the model is late or invalid.** Rationale (user): none given.
- **Plan gate: score a maker decision only on a changed book; paper fills need a print through the quote price; maker decider deadline 1 s; the builder's other plan defaults accepted.** Rationale (user): none given.
- **Fix all five review findings (R1-R5).** Rationale (user): none given.
- **Keep the strict cap: quotes get 18 actions a minute and a stop waits rather than exceeding 20** (replaces the earlier accepted default that a stop may push a rare minute past 20). Rationale (user): none given.
- **Git email for this repo set to the GitHub noreply address; the 9 unpushed commits re-authored** after GitHub blocked a push exposing the private address. Chosen over changing the GitHub setting. Rationale (user): none given.

Decided by the builder, not chosen either way by the user: labels and the model's view use the book with our own quote removed; a book with our orders unknown gives no decision (`decision_skipped`); read rate limits pause quoting but are not order errors; inner retries inside one flatten attempt are not counted against the cap (bounded, documented in `_flatten_once`).

## M8 / PR #17: caught mistakes

> **M8 / PR #17: our own quote leaked into the model's input and labels.** Caught by: reviewer. What: recent mids and returns used the raw mid, and an unknown quote fell back to the raw book, so the model saw a fake move following its own last side and labels were biased against it. Fix: a30df8e. Prevention: `test_live_scoring_keys_on_the_market_book_not_the_raw_one` and the R1 tests.

> **M8 / PR #17: the 20-actions-a-minute hard constraint could be broken.** Caught by: reviewer. What: a stray cancel used the reserved slot; stop cancels and flatten IOCs were not counted (22 actions in 20 s in a probe). Fix: 77eefcf. Prevention: `test_actions_capped_at_20_per_rolling_minute`, `test_halt_cancel_is_never_blocked_by_the_cap`.

> **M8 / PR #17: paper missed adverse fills just before a modify; read rate limits only logged; maker settings could stop the taker.** Caught by: reviewer. Fix: 99ad80a, d17858b, 044713d.

> **M8 / PR #17: three deliberate bugs passed every test** (stop cancel not counted, stray cancel not counted, scoring on the raw book). Caught by: reviewer (break-it). Fix: cbc93d8 (tests only).

> **M8 / PR #17: decision mids included our own quote.** Caught by: builder during its own break-it pass. Fix: df3d294.
