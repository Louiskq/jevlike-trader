Closes #14

Milestone 7 stops a stalled feed from ending an unattended run. In the 2026-10-09 soak the testnet feed went quiet with no disconnect; the feed only pinged after 20 s, so the 15 s stale-book kill fired first and stopped the bot for good after 2 hours. Now the feed pings after 4 s of silence and drops the connection if no book arrives within 10 s (even if pings are answered), and a stale-book kill cancels, flattens, then pauses and resumes after 60 s of steady books, at most 5 times per run. Daily-loss and order-error kills are unchanged and permanent.

## What's in it
- **Spec amendment (99cb8aa):** `SPEC.md` kill-switch rule.
- **Feed (b8e00f7, 249bde6):** `src/jevhl/feed.py`; `FEED_PING_S` (4), `FEED_DEAD_S` (6), validated so ping + dead < `STALE_S`.
- **Pause and resume (a0b8d29):** `src/jevhl/trader.py`, `src/jevhl/risk.py`; `RESUME_AFTER_S` (60), `RESUME_MAX_GAP_S` (8), `STALE_RESUMES_MAX` (5); events `stale_pause`, `stale_resume`, `stale_resume_blocked`, `stale_gave_up`, each also printed.
- **Dashboard and report (7523feb, 2b47648):** PAUSED shown apart from TRIPPED; pause/resume counts and "(paused at the end)".
- **Tests (176a4bc, f441b58), docstring (54d62d3), dead code (434d193).**

## Decisions
Made at the review gate. Rationale given for each, in the user's words: "no reason".
- **Pause and resume on a stale feed instead of stopping for good:** requested by the user ("can you try fix it then"); no reason.
- **Fix R1, R2 and R4 rather than R1 only:** no reason.

Decided by the builder on evidence, not chosen either way by the user:
- **The resume gap bound is 8 s, not 2 s:** the soak's books arrive up to 6.1 s apart.

## Caught in review
- **Caught (reviewer):** a socket that answered pings but sent no books was never dropped, so the bot would pause forever. Fixed in 249bde6.
- **Caught (reviewer):** "(paused at the end)" could never print. Fixed in 2b47648.
- **Caught (reviewer):** DECISIONS.md still described a permanent stale stop. Fixed in the records commit.
- **Caught (reviewer, break-it):** the raw paused flag on the dashboard and a deleted stop re-check passed every test. Tests added in f441b58.
- **Caught (me):** nothing raised by me at the review gate.

## Break-it checks (builder-run, one mutant at a time)
29 mutants on the first build (feed drop, settling gap, flatten/unresolved/flat checks, resume cap, pause guards in decide/enter/exits, latch clearing, counters), all caught after four new tests; 9 more after review (book bound, timeout cap, bound reset, ended_paused guard, report flag, dashboard flag, stop re-check, both settling restarts), all caught. Not caught by the end-to-end test alone: deleting the single `book_left <= wait` branch (the pong-only socket is then dropped one ping later; the unit tests catch it).

## Deviations from the spec
None (the spec is amended in 99cb8aa). A paused bot during a long outage stays paused until books return or the run ends.

## Not yet verified (needs the real venue)
Whether the 2026-10-09 outage was a pong-answering or a silent socket; the real ping/pong behaviour; whether a reconnect gets a book in the ~5 s left before the 15 s stale bound (if not, the bot pauses and resumes 60 s later); a real stall during live orders.

## Verification
- Reproduce locally: `uv sync --locked && uv run ruff check && uv run pytest` (642 tests, no key or network needed)

https://claude.ai/code/session_01Ua15mU8f7EqK7W91vAtfvo
