# M5 review (reviewer subagent, condensed from its report)

Head 1036fdd. Nothing that breaks the spec. 508 tests pass, ruff clean, no new dependencies, no secrets or cluster paths (soak.md ssh line uses placeholders). PR still a draft; nothing in the repo records that the user confirmed the HTTP contract (spec requires it at this milestone). One worry tested and ruled out: socket timeout equal to the deadline could log a slow reply as invalid instead of late; 20 runs at each of four settings all gave `late`.

## Findings (all nits)
R1 soak.py:62-89 (printed via run.py:245) — soak_report reads every rss event and decision in the database, not just the current run's; start and end RSS can come from different processes and hold rates mix runs. Breaks when `--live --db live.db` is reused or a crashed soak restarts on the same db (a live restart needs it). soak.md says use a fresh --db but code doesn't enforce. Test: two dry_runs on one Store (rss 1000->1010, then 5000->5010); second report says start 5000, end 5010.
R2 prompt.py:14,17,19 — fixed price precision ({px:.1f}, mid .2f); COIN is configurable. With COIN=DOGE (~0.2) every level renders "0.2", the decider sees a flat book and still returns believable probabilities; fails silently. Test: render_context with mid 0.12345 and bids 0.1234/0.1233; assert lines differ.
R3 soak.py:56 (29-37) — rss() runs on the event loop; macOS runs a `ps` subprocess with a 5 s timeout. If ps stalls the trading loop, watchdog and dashboard freeze up to 5 s. Project convention (server.py:145) is asyncio.to_thread. Test: rss=lambda: time.sleep(0.5); assert decisions stay within deadline.
R4 config.py:108-109 — validate() only checks decider_url is set; loopback/http rule lives only in from_env. Config(model="decider", decider_url="http://10.0.0.5:8000") passes validation and build_model would connect off-box. Test: Config(model="decider", decider_url="http://example.com:80").validate() raises ConfigError.

## Weak tests
- test_soak_report_flags_stuck_hold_rate[buy-signal-0%] (test_soak.py:150-157) — passes if the warning printed the wrong percentage ("0%" in "...100%..."). Confirmed.
- Success-path tests in test_decider.py (good_body uses down = 1 - p_up, sums exactly 1.0) — pass even if PROB_SUM_TOLERANCE were 0, which would reject real 4-decimal replies (0.3333/0.6666 sums 0.9999) and give 100% invalid holds overnight. Confirmed. No test sends a rounded pair inside the tolerance or one just outside (sum 1.02).
- test_current_rss_falls_back_when_proc_and_ps_fail — only asserts > 0; passes if macOS // 1024 dropped or applied on Linux.
- test_dry_run_samples_rss_at_start_and_end_and_report_shows_soak — uses rss_every_s=3600 so periodic sampling inside dry_run is never exercised.

## Break-it candidates
B1 model.py:60 PROB_SUM_TOLERANCE = 0.0 (survives). B2 soak.py:79 `{1 - hold_rate:.0%}` (only the 100% case catches it). B3 model.py:89 return values[OPTIONS[1]] (caught). B4 soak.py:46 `return peak` dropping macOS unit conversion (survives). B5 delete model.py:128-129 post-connect abort check (caught).

## Reading list
config.py:34-56,83-87,103-109 (MODEL, loopback DECIDER_URL); model.py:59-89 (contract, strict parsing); model.py:92-155 (HTTP, timeout, abort); trader.py:83-91,264-284 (late/invalid/in-flight -> hold); prompt.py:13-40 (what the decider sees: the user's modelling decision); soak.py:49-89 and run.py:117-130,245,288 (RSS, stuck hold-rate); docs/soak.md:7-22 (contract as documented).

## Plumbing
tests/decider_server.py; run.py format_soak/_pct and rss pass-through; soak.py:20-46 RSS readers; tests/test_prompt.py.

## Could be deleted
run.py:288 duplicates the branch inside build_model (use build_model(cfg)); config.py:108-109 "DECIDER_URL is required" can't fire from from_env (should do the full loopback check instead, see R4).
