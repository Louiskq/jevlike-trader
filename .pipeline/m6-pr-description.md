Closes #12

Milestone 6 makes the kill switch's flatten survive a network loss. Before, a failed flatten was tried a few times and the bot exited, leaving the position open (this happened in the first loss-limit drill: a 0.00017 BTC short stayed open until closed by hand). Now a failed live flatten is retried with a growing delay (1 s, 2, 4 ... capped at 30 s) for up to `FLATTEN_RETRY_WINDOW_S` (default 600; 0 = one attempt), re-reading the exchange's position each time and only placing reduce-only closes.

## What's in it
- **Config (bf1e1d0):** `FLATTEN_RETRY_WINDOW_S`, validated finite and >= 0.
- **Retry loop (50f5839):** `src/jevhl/trader.py`; events `flatten_failed` (per attempt), `flatten_recovered`, `flatten_gave_up`; one retry window per run; the lot-size snap in `src/jevhl/exchange.py`.
- **Last line and Ctrl-C (fb64baa):** `src/jevhl/run.py`; the report ends with `FLATTEN GAVE UP ... close it by hand` when unresolved.
- **Review fixes:** e3e6b22 (one Ctrl-C is a normal stop), 9a4c307 (`--seconds` lets a retry finish), c7fbd85 (a cancel returns at once; blocked thread tracked), 70f56d2 (attempts and outcome printed to stderr), 742df0f (test gaps and the window docs).

## Decisions
Made at the review gate. Rationale given for each, in the user's words: "no reason".
- **Fix R1-R4 and document R5 rather than fix only R1 and R2, or all five:** no reason.
- **Accept that a Ctrl-C mid-retry abandons the in-flight network call (it may place a close order this run never books; the next start reads it back):** no reason.

Decided by the orchestrator when the user said "make the decision for me" (not the user's reasoning):
- **Fix the flatten gap before the overnight soak instead of listing it as a limitation.**

## Caught in review
- **Caught (reviewer):** one Ctrl-C printed a false "FLATTEN GAVE UP" and logged a false event. Fixed in e3e6b22.
- **Caught (reviewer):** `--seconds` could cut a retrying flatten short and leave the position open. Fixed in 9a4c307.
- **Caught (reviewer):** a second Ctrl-C waited 20-50 s for a blocked network thread. Fixed in c7fbd85.
- **Caught (reviewer):** nothing printed to the terminal during a retry of up to 10 minutes. Fixed in 70f56d2.
- **Caught (reviewer, break-it):** deleting the KeyboardInterrupt handler passed all 547 tests. Tests added in 742df0f.
- **Caught (me):** nothing raised by me at the review gate.

## Break-it checks (builder-run, one mutant at a time; all caught, two by a pytest timeout)
Retry deleted, delay not doubling, no cap, no window trim, retry-spent flag, paper retried, cancel not logged, flatten lock removed, failed-attempt fills dropped, lot snap removed, gave-up line removed, config validation removed; after review: handler deleted, report line removed, exit code, `--seconds` cancel, executor use, abandoned-thread tracking, each stderr line. The R2 "outside cancel no longer cancels the run" mutation is caught only by the pytest session aborting with a KeyboardInterrupt, not a clean named failure.

## Deviations from the spec
None. Two behaviours to know: `--seconds` can now overrun by up to the retry window while a flatten finishes; and `FLATTEN_RETRY_WINDOW_S` bounds when an attempt may start, so the end can overrun by one attempt (about 43 s, the reviewer's figure, not re-measured). `FLATTEN_RETRY_WINDOW_S` is not in `.env.example` (agents cannot edit it).

## Not yet verified (needs the real venue)
A real network drop mid-flatten and recovery on testnet; real Ctrl-C behaviour (first and second, and with `--dashboard --live`); real HTTP timeouts; whether an abandoned call's order lands on the exchange. All simulated with the fake venue.

## Verification
- Reproduce locally: `uv sync --locked && uv run ruff check && uv run pytest` (557 tests, no key or network needed)
