# M1 break-it results (run in a throwaway worktree on 2026-10-04; baseline 107 passed)

| # | Deliberate bug | Result | Tests that failed |
|---|---|---|---|
| B1 | policy.py:10 `EPS = 1e-12` -> `0.0` (no float tolerance on gate) | NOT CAUGHT, 107 passed | none -> must-fix: new test (gate 0.67 with p=0.33) |
| B2 | calibration.py:12 label returns 0 for "up" | caught, 4 failed | test_calibration.py::test_label_up_down_tie; test_store.py::test_resolve_sets_label, ::test_scored_includes_holds_excludes_ties_and_missing_p, ::test_scored_ordered_by_ts |
| B3 | store.py:100 drop `AND p_up IS NOT NULL` from scored() | caught, 1 failed | test_store.py::test_scored_includes_holds_excludes_ties_and_missing_p |
| B4 | calibration.py:43 running baseline includes current label (lookahead) | caught, 2 failed | test_calibration.py::test_baseline_running_uses_only_prior_labels, ::test_running_baseline_not_affected_by_future_labels |
| B5 | risk.py:64 stale `>` -> `>=` | caught, 1 failed | test_risk.py::test_stale_book_boundary |

Note: reviewer expected the smoke test not to catch B2; confirmed, the unit tests do.
