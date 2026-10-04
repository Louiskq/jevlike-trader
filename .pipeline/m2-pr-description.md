Closes #2

Milestone 2 delivers the dry run: a reconnecting testnet l2Book feed, the trader loop with simulated fills, a ledger, per-horizon outcomes (30/60/300 s) and latency percentiles from the bot's own logs. No API key and no real orders.

## What's in it
- **Spec change (own commit 6a52082):** outcomes recorded at 30 s, 60 s and 300 s; trading, gate and exits stay at 60 s.
- **Store, ledger, latency (e8243e9):** `src/jevhl/store.py` schema v3 with an `outcomes(decision_id, horizon_s, mid_future, label)` table, `ledger.py`, `latency.py`, per-horizon calibration in `calibration.py`.
- **Feed (5e92a37):** `src/jevhl/feed.py` parses l2Book messages and streams them with reconnect and backoff. Adds the `websockets` dependency.
- **Paper fills and trader loop (6c57648):** `paper.py`, `trader.py`: one decision per snapshot, 2 s deadline, stale-book and late holds, 60 s time exit, kill switch.
- **Dry-run entry point (0eb472d):** `python -m jevhl.run` prints latency and calibration reports.
- **Review fixes:** f341991, 2fd8b9f, d42e440, 243845b, a48d328, 6754608, and tests in 859e87f.

## Decisions
The spec was silent on these four. All were kept. Rationale given for each, in the user's words: "no reason".
- **A feed outage over 15 s stops the bot:** no reason.
- **An outcome is scored only if a book arrives within 15 s of the horizon mark:** no reason.
- **A leftover position at startup is closed at the first book:** no reason.
- **Paper fee 4.5 bps, 50 bps band, reduce-only skips the $10 minimum (kept until M4 verifies them):** no reason.

## Caught in review
- **Caught (reviewer):** a decision in flight could still trade after the kill switch tripped. Fixed in f341991.
- **Caught (reviewer):** float residue in the position blocked every sell signal. Fixed in 2fd8b9f.
- **Caught (reviewer):** `run()` hung after the stale watchdog tripped with the feed down. Fixed in d42e440.
- **Caught (reviewer):** a hung model got a new call on every snapshot. Fixed in 243845b.
- **Caught (reviewer):** double `kill_switch` event, and SQLite WAL/SHM files not gitignored. Fixed in 6754608.
- **Caught (builder, investigating):** a reduce-only close left one lot open from float error. Fixed in a48d328.
- **Caught (reviewer, break-it):** four deliberate bugs passed all 240 tests. Tests added in 859e87f.
- **Caught (me):** nothing raised by me at the review gate.

Left as-is: the duplicate `self.meta` name in `paper.py` and the duplicate `SECONDS_PER_DAY` in `ledger.py`; the weaker `format_report` horizon assertion.

## Break-it checks
Each deliberate bug was applied in a scratch worktree and the suite was run (baseline 240 passed). Counts are exact.

| Deliberate bug | Tests that failed |
|----------------|-------------------|
| partial-exit retry replaced with `pass` | none (0) before 859e87f; with the new test, 1: test_partial_exit_is_retried_at_the_next_book |
| feed backoff never reset | none (0) before 859e87f; now 2: test_stream_reconnects_and_resubscribes_after_drop, test_stream_backoff_doubles_on_failures_and_resets_after_a_book |
| order-error counter never reset on success | none (0) before 859e87f; now 1: test_order_error_counter_resets_after_a_successful_order |
| staleness judged on exchange `ts` not `recv_ts` | none (0) before 859e87f; now 3: test_stale_book_is_a_hold_and_model_is_not_called, test_book_just_inside_staleness_bound_is_decided, test_book_exactly_at_staleness_bound_is_still_fresh |
| timeout latency logged as 0 | 1: test_hung_model_times_out_to_late_hold_without_p |

## Deviations from the spec
1. Adds the `websockets` dependency (pure Python, no system packages).

## Verification
- CI: lint and tests pass
- Reproduce locally: `uv sync --locked && uv run ruff check && uv run pytest` (249 tests)
- Smoke run: `uv run python -m jevhl.run --seconds 130 --db <scratch>/smoke.db` (25 decisions, 0 late holds, no errors)

## 30-minute dry run (acceptance)
`uv run python -m jevhl.run --seconds 1800` against live testnet public data, BTC, mock model, simulated fills, no key. Exit 0, kill switch not tripped. Full output: `.pipeline/dryrun/m2-30min.txt`.

- 340 decisions in 1800 s (one per snapshot), 0 late holds, 0 stale holds, no parse or order errors.
- Decision gap p50 5.33 s, p90 5.52 s. Model latency ms: p50 0.956, p90 2.079, p99 12.461, max 40.521.
- 69 simulated fills, fees $1.6986.
- Two disconnects, at about 10 and 20 minutes: the server closed the socket cleanly (code 1000, "Expired"). The feed reconnected within about 1.6 s each time with no missed decisions or stale holds.
- Calibration (mock model, so near chance is expected): Brier vs running baseline, 30 s n=226 0.2472 vs 0.2533; 60 s n=260 0.2502 vs 0.2526; 300 s n=283 0.2524 vs 0.2529. These samples are small and the 300 s ones overlap about 98%, so none of this shows the model has edge.
