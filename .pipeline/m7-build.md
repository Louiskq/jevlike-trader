# M7 build summary (builder subagent, condensed from its report)

Built (issue #14): the feed pings after 4 s of silence (FEED_PING_S) and drops a dead socket after 6 s more (FEED_DEAD_S), through the existing backoff. A stale-book kill still cancels and flattens (with the M6 retry), then PAUSES the bot instead of stopping it: no entries, decisions held as 'paused', feed still read. It resumes after RESUME_AFTER_S (60) of steady books (no gap over RESUME_MAX_GAP_S, 8 s), at most STALE_RESUMES_MAX (5) times per run; the next stale kill is permanent. Daily-loss and order-error kills stay permanent. Events stale_pause, stale_resume, stale_resume_blocked, stale_gave_up plus a terminal line each. Dashboard shows PAUSED apart from TRIPPED; the final report gives pause/resume counts.

Commits: 99cb8aa Spec (SPEC.md only); b8e00f7 Feed + config; a0b8d29 Trader pause/resume (+ KillSwitch.clear_stale, 'paused' hold reason); 7523feb dashboard and report; 176a4bc test gaps. No Co-Authored-By.

Deviations from the brief:
- Gap bound is 8 s, not 2 s: soak.db shows books 5.2 s apart on average, up to 6.1 s, so 2 s would never settle.
- Resume blocked while a flatten is running or unresolved; if unresolved when the settling period ends, one fresh flatten attempt is made first. If the ledger or exchange position is not flat the resume is blocked (stale_resume_blocked), flatten again, restart settling.
- Each pause resets the flatten retry window.
- Config validation requires ping + dead < stale_s and STALE_RESUMES_MAX whole >= 0; only enforced through from_env.
- Eight existing tests assumed a permanent stale kill and now use Config(stale_resumes_max=0); none weakened or deleted.

Unsure about: an entry decided before a pause could finish after a resume if RESUME_AFTER_S=0 (no pause-generation counter); default 8 s gap rests on one soak's cadence; "paused" chip colour/length cosmetic; a bot paused during a long outage stays paused indefinitely (limited only by the resume count); DECISIONS.md, README and docs/soak.md not updated; new env vars only in Config comments.

Verification (builder): 36 feed/config tests and 20 of 22 trader tests failed before implementation (the 2 others are negative cases); 631 pass, ruff clean; smoke on the real testnet feed 20 s: 4 decisions, no errors, "stale pauses 0 resumes 0"; 29 mutants (B1-B30, B29 equivalent and removed), all caught after fixes (survivors B8, B10, B11, B17 and a B6 hang were fixed by new tests). Orchestrator re-ran: 631 pass, ruff clean, no co-author trailers, no env/db/settings files in the diff.

Cannot verify without the real venue: whether the testnet answers {"method":"ping"} with traffic that counts; that a silent socket really happens and the 4 s + 6 s window beats 15 s; real l2Book cadence vs the 8 s gap and 60 s settling; a real stall during live orders (position read on resume, reduce-only closes, M6 retry concurrent with returning books); the dashboard chip in a real browser.
