# M1 review (reviewer subagent, PR #6)

R1 [must-fix] config.py:19,58-70: float() accepts nan/inf; NaN passes validate(). MAX_DAILY_LOSS_USD=nan silently disables the daily-loss kill switch; DECIDER_DEADLINE_S=nan never returns late; MAX_POSITION_USD=nan/inf gives size_usd=nan. Fix: reject non-finite in _num. Test: parametrize test_invalid_values_raise with nan/inf cases.
R2 [must-fix] store.py:100 (fed by policy.py:41): invalid/late holds still store raw p_up; scored() filters only p_up IS NOT NULL, so out-of-range p reaches calibration (Brier inf, reliability_bins OverflowError, -0.1 wraps into last bin via int(-1.0)=-1). Test: decide(1.5), resolve, assert scored()==[]; reliability_bins([-0.1],[0]) raises.
R3 [must-fix] store.py:37: fills deduped on UNIQUE(oid,ts,sz); one IOC sweeping two makers can give two fills with same oid/ts/sz at different px, second dropped -> position/fees/P&L understated, breaks M4 UI match. HL fills carry unique tid; key on it. test_fill_dedup_on_oid_ts_sz asserts the buggy behaviour.
R4 [must-fix] risk.py:51-53,22-28: daily-loss baseline is first P&L the object saw, not P&L at UTC midnight; reset() clears it. After restart or manual reset, another full limit can be lost the same day. Test: KillSwitch(cfg, clock, day_start_pnl=-90), update_pnl(-90), update_pnl(-190) -> tripped.
R5 [nit] risk.py:64,25: staleness only counts after first on_book(); reset() clears _last_book, so a dead feed never trips stale_book.
R6 [nit] config.py:50-57: min_notional_usd, full_size_p, stale_s, max_order_errors not settable from env, though spec says all configurable.
R7 [nit] .gitignore lacks *.db-wal and *.db-shm (WAL mode files).
R8 [nit] SPEC.md/DECISIONS.md amendment (limits $50/$10 -> $500/$100, mainnet dropped) rides on an implementation PR; confirm user approval or split.

Weak tests: test_smoke::test_offline_pipeline passes with label() inverted; test_policy sell-boundary tests pass with EPS=0.0 (need gate such as 0.67 with p=0.33); test_calibration::test_summary passes if in-sample baseline put under running key; test_store::test_fill_dedup_on_oid_ts_sz encodes R3; test_config::test_invalid_values_raise has no nan/inf; test_bin_edges lacks p<0 case; test_risk daily-loss tests lack restart/reset mid-day.

Break-it candidates:
B1 policy.py:10 EPS = 0.0 (expected: nothing catches it)
B2 calibration.py:12 label returns 0 for up (expected: test_label_up_down_tie, test_resolve_sets_label; not smoke)
B3 store.py:100 scored WHERE label IS NOT NULL (expected: test_scored_includes_holds_excludes_ties_and_missing_p)
B4 calibration.py:43 running baseline with lookahead (expected: two baseline tests)
B5 risk.py:64 stale > to >= (expected: test_stale_book_boundary)

Reading list: config.py:14-70; policy.py:30-66; risk.py:16-66; rounding.py:35-65; calibration.py:9-74; store.py:28-38,66-115.
Plumbing: pyproject, uv.lock, types.py, model.py, tests/fixtures/meta_btc.json, .env.example. CI green on the PR.
Could be deleted: types.py:9-11 Side enum unused; policy.py:26-27 stale_hold unused/untested.
