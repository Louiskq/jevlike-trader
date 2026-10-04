Closes #1

Milestone 1 delivers the offline core: config, rounding, decision policy, kill switch, mock model, SQLite store and calibration maths, with tests and CI. No network code yet.

## What's in it
- **Scaffold:** `pyproject.toml`, `uv.lock`, `.env.example`; CI already existed (runs `uv sync --locked`, `ruff check`, `pytest`).
- **Config:** `src/jevhl/config.py`. Testnet only; any other network is rejected. Limits default to $500 position and $100 daily loss, configurable via env.
- **Rounding:** `src/jevhl/rounding.py`. Hyperliquid perp price rules (5 significant figures, at most 6 - szDecimals decimals, integers always allowed) and size flooring.
- **Policy:** `src/jevhl/policy.py`. Gate, abstain, blocked opposite side, confidence-scaled sizing, cap room, late and invalid holds.
- **Kill switch:** `src/jevhl/risk.py`. Daily loss per UTC day, 3 consecutive order errors, stale book.
- **Mock model:** `src/jevhl/model.py`. Seeded, deterministic, order-book-imbalance based.
- **Store:** `src/jevhl/store.py`. SQLite for decisions, fills and events.
- **Calibration:** `src/jevhl/calibration.py`. Labels (ties dropped), Brier, running and in-sample baselines, reliability bins.
- **Types:** `src/jevhl/types.py`.
- **Spec amendment (own commit e558bd4):** testnet only, no mainnet code, defaults $500 / $100.

## Decisions
- **Round up one lot to meet the $10 minimum order:** "I want to trade as much as possible to validate the idea."
- **Stale-book timer not armed until the first book arrives:** "does not matter."
- **Late and invalid answer is recorded as `late`:** "it is late."

## Caught in review
- **Caught (reviewer):** non-finite env values (`nan`) were accepted, which silently disabled the daily-loss kill switch. Fixed in a7f596e.
- **Caught (reviewer):** out-of-range model probabilities reached the calibration maths (infinite Brier, wrong bin). Fixed in a987589.
- **Caught (reviewer):** fills were deduplicated on (oid, ts, sz), so a sweep of two equal-size resting orders lost a fill. Fixed in 491f527.
- **Caught (reviewer):** the daily-loss baseline reset on restart or `reset()`, allowing another full limit the same day. Fixed in 222f9ad.
- **Caught (reviewer, break-it):** removing the gate's float tolerance passed all tests. Test added in 615d979.
- **Caught (me):** nothing raised by me at the review gate.

Left as-is by the user: stale timer not armed before the first book, four settings not settable from env, `.gitignore` lacks SQLite `-wal`/`-shm`, unused `Side` enum and `stale_hold` helper.

## Break-it checks
Each deliberate bug was applied in a scratch worktree and the suite was run (baseline 107 passed). Counts are exact.

| Deliberate bug | Tests that failed |
|----------------|-------------------|
| `EPS = 0.0` in policy.py (no float tolerance on gate) | none (0): fixed by new test in 615d979, which fails 3 times with the bug planted |
| calibration `label()` returns 0 for "up" | test_label_up_down_tie, test_resolve_sets_label, test_scored_includes_holds_excludes_ties_and_missing_p, test_scored_ordered_by_ts (4) |
| `scored()` drops the `p_up IS NOT NULL` filter | test_scored_includes_holds_excludes_ties_and_missing_p (1) |
| running baseline includes the current label (lookahead) | test_baseline_running_uses_only_prior_labels, test_running_baseline_not_affected_by_future_labels (2) |
| stale check `>` changed to `>=` | test_stale_book_boundary (1) |

## Deviations from the spec
1. None from the amended spec. The amendment itself (testnet only, $500 / $100) is commit e558bd4.

## Verification
- CI: lint and tests pass
- Reproduce locally: `uv sync --locked && uv run ruff check && uv run pytest` (137 tests)
