# M7 review (reviewer subagent, condensed from its report)

Head 176a4bc (+ build record commit). 631 tests pass, no .db/.env tracked, SPEC amendment in its own commit (99cb8aa).

## Findings
R1 [must-fix] feed.py:142 — `pinged = False` is reset by any message, pongs included. A socket that keeps answering pings but sends no l2Book data is never dropped (one plausible reading of the 2026-10-09 incident). The trader then pauses at 15 s, no book arrives, `_settle` never runs, the bot stays paused for good: as dead as before the PR, shown as PAUSED. Reproduced with a fake socket that pongs every ping (83 pings in 1 s, only a 'connect' event). Test: a socket that pongs every ping and never sends an l2Book must log disconnect and reconnect within ping_s + dead_s of the last book. Fix idea: also drop when no book has arrived for ping_s + dead_s, whatever else arrives.
R2 [nit] run.py:273 — "(paused at the end)" can never print: format_report runs after Trader.run finished, whose finally calls shutdown() and sets stopped, and `paused` is `_paused and not stopped`. A run ending paused looks like one that resumed. Test: format_report after shutdown on a paused trader still says "(paused at the end)".
R3 [must-fix] DECISIONS.md:54 — not updated though issue #14 item 4 requires it; it still says a feed outage over 15 s stops the bot ("Chosen: keep"), contradicting the amended SPEC and code. Add a superseding M7 entry (8 s gap vs 2 s, new retry window per pause, resume blocked unless flat).
R4 [nit] trader.py:407 — `_flatten` docstring still says "One retry sequence per run"; each pause resets `_retry_spent` and the not-flat branch of `_settle` can start a full window too.

## Weak tests
- test_any_message_after_a_ping_keeps_the_socket_and_the_ping_cycle_restarts: locks in the R1 bug; would fail on a fix.
- test_report_counts_stale_pauses_and_resumes: calls format_report on a still-running trader; passes with R2.
- test_trader_live_reports_a_stale_pause_as_paused_not_killed: passes if trader_live used `_paused`; nothing covers trader_live after stop-while-paused.
- No end-to-end test runs stream_books and Trader.run with default timers to show a silent socket is dropped before the 15 s bound; feed tests use 0.01/0.02 s timers; bypassing open_feed in main passes.

## Break-it candidates
B1 `"paused": trader._paused` run.py:159 (nothing catches). B2 reset `pinged` only on a book (close to the R1 fix; shows the test above has the wrong teeth). B3 `<=` on the resume limit trader.py:490 (caught). B4 delete the post-flatten stop/pause re-check trader.py:534-535 (nothing catches; no test stops the bot during the resume-time flatten). B5 delete the settling restart after a failed resume flatten trader.py:537 (check test_a_flatten_that_fails_again_at_resume_time_keeps_the_bot_paused).

## Reading list
feed.py:125-154; trader.py:486-514, 516-567, 125-153/282-286/309-318/332-335/366; risk.py:44-50; config.py:81-90, 151-162; server.py:121-130, run.py:156-159, 272-273. M4 "never trades on stale data" still holds: a decision started before a pause is held as 'paused' or skipped by the stale-book check in `_enter` (trader.py:369).

## Plumbing
run.py:91-93, 190-191; static/app.js:50-51; types.py:26-28; config.py:34-39 (`_whole`); tests switched to stale_resumes_max=0; .pipeline/m7-build.md.

## Could be deleted
trader.py:523 `prev is None` clause is unreachable.
