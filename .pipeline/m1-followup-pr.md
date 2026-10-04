Follow-up to #6 (Milestone 1, issue #1 already closed).

PR #6 was merged at `3e63a40`, before the review fixes reached `main`. This PR brings them in, plus the pipeline records. It contains no new features.

## What's in it
- **a7f596e Fix:** reject non-finite numeric env values (`MAX_DAILY_LOSS_USD=nan` silently disabled the daily-loss kill switch).
- **a987589 Fix:** out-of-range model probabilities are stored as NULL and rejected by calibration (they produced an infinite Brier score and a wrong reliability bin).
- **491f527 Fix:** fills are deduplicated on the exchange trade id (`tid`), so a sweep of two equal-size resting orders keeps both fills.
- **222f9ad Fix:** the daily-loss baseline can be supplied by the caller and survives `reset()`, so a restart cannot grant a fresh daily allowance.
- **615d979 Test:** covers the gate's float tolerance (`EPS = 0.0` previously passed all 107 tests; with the bug planted the new test fails 3 times).
- **5da48ec Records:** DECISIONS.md, `.pipeline/` (plan, build, review, break-it, PR description) and PIPELINE.md for M1.

Full review trail, decisions and break-it table: `.pipeline/m1-pr-description.md`, `.pipeline/m1-review.md`, `.pipeline/m1-breakit.md`, DECISIONS.md ("M1 / PR #6").

## Deviations from the spec
None.

## Verification
- CI: lint and tests pass
- Reproduce locally: `uv sync --locked && uv run ruff check && uv run pytest` (137 tests)
