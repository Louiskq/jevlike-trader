Closes #4

Milestone 4 delivers live testnet orders: an exchange wrapper over the official SDK, a trader driven by exchange confirmations, a kill switch that flattens, `--live` mode with startup checks, and a reconciliation script. Nothing has been run against a real testnet account yet (see "Not yet verified").

## What's in it
- **Exchange wrapper (59d2544):** `src/jevhl/exchange.py`: IOC entry, reduce-only close, cancel-all, flatten, fills with tid and fee; `tests/fake_hl.py` fake venue.
- **Trader from confirmations (cfda52b):** accounting follows exchange fills only; stop state re-checked after every await.
- **`--live`, startup checks, reconcile (3abe9ab).**
- **Review fixes:** ea488d4 (size warning), 63d9da7 (rate guard), d31833e (stale book), bccafcf (reconcile vs closedPnl), 5b79a00 (60 s exit), bc65c8b (tests only).

## Decisions
Made at the review gate. Rationale given for each, in the user's words: "no reason".
- **Keep the new `hyperliquid-python-sdk` dependency (24 pure-Python or prebuilt-wheel packages, no system packages):** no reason.
- **Fix R3 (stale book), R4 (real reconcile) and R5 (60 s exit) rather than leave them as nits:** no reason.

Decided by the orchestrator, because the user said "decide yourself" (not the user's reasoning):
- **The first-hour size check is a startup warning, not a refusal.**
- **Fills made by hand in the UI are closed 60 s after they fill, like the bot's own.**

## Caught in review
- **Caught (reviewer):** `.env.example` shipped a $500 cap against a "keep it small" comment. Fixed in ea488d4 (warning); the file itself still needs a manual edit.
- **Caught (reviewer):** no limit on order rate. Fixed in 63d9da7.
- **Caught (reviewer):** an entry could be sent on a book up to ~70 s old. Fixed in d31833e.
- **Caught (reviewer):** reconcile's "exchange P&L" could never disagree with the local P&L. Fixed in bccafcf.
- **Caught (reviewer):** late-booked fills closed after ~15 s, not 60 s. Fixed in 5b79a00.
- **Caught (reviewer, break-it):** three deliberate bugs passed all tests. Tests added in bc65c8b.
- **Caught (me):** nothing raised by me at the review gate.

Left as-is: R6 (a confirm lag counts as an order error), R7 (residue exit vs exits already claimed), R8 (second flatten after a halt).

## Break-it checks
| Deliberate bug | Tests that failed |
|---|---|
| live flatten skipped when ledger is flat | none before bc65c8b; now 1: test_shutdown_closes_the_exchange_position_even_when_the_ledger_is_flat |
| reconcile's exchange P&L taken from local fills | none before; now 1: test_report_compares_exchange_closed_pnl_with_the_ledger_realized_pnl |
| exit success no longer resets the error count | none before; now 1: test_a_successful_exit_resets_the_consecutive_order_error_count |
| stop set after the flatten in `_halt` | none before; now 1: test_the_stop_flag_is_set_before_the_flatten_starts |
| `_poll_fills` without its stop check | none before; now 1: test_a_poll_that_waited_for_the_lock_does_nothing_if_the_bot_stopped_meanwhile |

(Builder-run, one mutant at a time. The R2 guard-deletion mutant was not run separately.)

## Deviations from the spec
1. Adds `hyperliquid-python-sdk==0.24.0`.
2. No user-event WebSocket for fills; `userFillsByTime` is read after each order and every 15 s.
3. Needs `HL_ACCOUNT_ADDRESS` (main account, not secret) to read fills and positions; the agent wallet only signs.
4. Live entries are sized at about $10.50 to stay above the $10 minimum.
5. The rate guard allows one entry/exit pair per minute (my reading of "about 2 orders per minute").

## Not yet verified (needs a testnet account)
An hour of live trading; fills, position, fees and P&L matching the testnet UI; the daily-loss limit firing and flattening; the wifi-off drill; the signed-order path (no real order has ever been sent); the error text for an unmatched IOC; whether reduce-only closes under $10 are exempt from the minimum; whether the UI's closed P&L is net of fees.

## Verification
- Reproduce locally: `uv sync --locked && uv run ruff check && uv run pytest` (433 tests, no key or network)
- `uv run python -m jevhl.run --live` with no key exits with a clear error and no network call.
