# M6 review (reviewer subagent, condensed from its report)

Head fb64baa. 547 tests pass, ruff clean. Reviewer reproduced R1-R3 with scripts in the scratchpad.

## Findings
R1 [must-fix] run.py:326-329 — `except KeyboardInterrupt` assumes a second Ctrl-C, but one Ctrl-C reaches it (Python 3.13 asyncio.Runner cancels the main task on first SIGINT; shutdown() still runs to the end, then the cancel becomes KeyboardInterrupt; dashboard mode: uvicorn re-raises the captured SIGINT). Every routine stop with one Ctrl-C prints "interrupted: flatten cut short ... FLATTEN GAVE UP", logs a false `flatten_gave_up`, exits 130 and prints no normal report. Test: send one SIGINT through the real wiring; assert no GAVE UP line, no gave_up event, normal report.
R2 [must-fix] run.py:123,128 — `--seconds` cuts the flatten retry short: wait_for cancels trader.run, then dry_run's finally cancels the watchdog that holds the retrying `_flatten`, so it gives up as "interrupted" with the position open. Reproduced: stale_s=0.5, window 600, duration_s=4. Test: dry_run with venue down, window large, duration just after the kill; the retry must run past duration_s.
R3 [should-fix] trader.py:346-347 (exchange.py:228-250) — a second Ctrl-C does not end the retry at once while an attempt runs: Exchange.flatten is in a worker thread (10 s HTTP timeouts, sleeps, 3 tries) and asyncio.run waits for it. Measured 19.3 s with flatten_tries=1; ~40-50 s with defaults. Test: venue read blocks N s, cancel during the attempt, control returns well under N.
R4 [should-fix] trader.py:400,359,383 — issue says attempts and outcome are "logged as events and printed"; they are only written to SQLite. During a retry up to 600 s the terminal shows nothing. Test: capsys, one printed line per attempt and a final recovered line.
R5 [nit] trader.py:351-354 — the window bounds when an attempt may start, not when the retry ends; an attempt just before the deadline can overrun by ~43 s. Document or pass remaining time down.

## Weak tests
- test_a_second_ctrl_c_report_says_the_position_may_be_open (test_run.py:442): checks only the string; passes with R1 and with the KeyboardInterrupt handler deleted (B5 survives).
- test_an_interrupt_during_the_retry_wait_ends_it_promptly_and_says_so (test_trader_live.py:519): fake venue fails instantly, cancel always lands in the asyncio sleep; calls task.cancel() directly, not real SIGINT/asyncio.run.
- test_flatten_retries_with_growing_delay_until_the_network_returns (test_trader_live.py:425): venue down before the flatten starts; no test drops the network after the close order is placed (FakeHL can only fail user_state).
- test_the_delay_doubles_to_a_cap_and_the_window_bounds_the_total_time: fake clock never advances inside an attempt (R5).
- No test covers dry_run's watchdog/duration interaction (R2).

## Break-it candidates
B1 drop lot snap exchange.py:240 (caught indirectly, no direct snap test). B2 delete `self._retry_spent = True` trader.py:381 (caught). B3 `fills = []` trader.py:398 (caught). B4 `_flatten_lock` -> `if True:` trader.py:338 (caught). B5 delete `except KeyboardInterrupt` run.py:326-329 (SURVIVES all 547 tests).

## Reading list
trader.py:33-34,338-355 (retry loop, backoff, window); trader.py:340,356-364,380-386 (outcome events, once-per-run); trader.py:373-378 + run.py:262-270,323-329 (interrupt semantics, R1, R3); trader.py:388-402 (one attempt, fills booked); exchange.py:221-251 (reduce-only close of the exchange's own position); run.py:89-131 + trader.py:404-414,444-450 (how kill/shutdown flatten is reached and cut short, R2); run.py:253-258 (report line); config.py:70,98,124-125.

## Plumbing
config.py field/parse/validation; tests/test_config.py:101-116; tests/test_trader_live.py:36-51 (make() with injectable async sleep); trader.py:19,61,66-69.

## Could be deleted
run.py:268 `flatten_gave_up` event in interrupted_report duplicates the one the CancelledError handler already writes (trader.py:375); when no retry is running it is false (R1).
