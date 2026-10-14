# Evidence review, PR #22 (reviewer subagent, condensed from its report)

## Findings
R1 [must-fix] scripts/evidence.py:157-164 — `isotonic_fit` does not pool tied p values (docstring says it does). Sorted by (p, y), tied 0s each open a block and merge backwards; the 1s start a fresh block. Monotone but not the least-squares isotonic fit. `isotonic_fit([0.1,0.2,0.2],[1,0,1])` gives [0.5, 1.0], correct is [2/3, 2/3]. Real data is heavily tied (R3 32-35 distinct p_up, R6 66-76). With ties pooled the A2 Brier moves up to 0.006 (R3 30 s 0.2859→0.2800, R6 60 s 0.2360→0.2312, R3 60 s 0.2525→0.2545); verdicts and E6 headline unchanged, but A2 numbers in results/baselines.md are wrong. Test: the 3-point case above → [2/3, 2/3].
R2 [should-fix] evidence.py:1055-1064, 1011 — E6 headline says momentum beat the base rate in R3 30 s without caveat: 1 of 18 intervals at 95% (≈1 expected by chance), upper bound -0.0004, and the split favours fitted rows. Overstated. Add a multiple-comparisons caveat.
R3 [nit] evidence.py:729, 641, 727, 976-977, 1114, 308-314 — prose claims hard-coded rather than computed (E4 "0 error events", E3 "worked live" regardless of loss_in_db, "faster rise than R3's", auc_note recalibration claim, "every run ended flat", OVERLAP's "~5.3 s"/"about 11 neighbours"). True on today's DBs; would print false statements on other data. Test: e4 on a fixture with one error event.
R4 [nit] evidence.py:33 — `GATE_EPS` copies `jevhl.policy.EPS`; import it.

Checked correct: running baseline (no lookahead), per-run pooling, moving-block resample, permutation +1 correction, AUC half ties, momentum lookback, least-squares k, gate semantics incl. EPS, reconcile tolerances, byte-identical regeneration, E5 claims, no secrets (job IDs and dates only), matplotlib dev-only.

## Weak tests
- test_isotonic_ties_in_probability_share_one_value: passes with R1's bug (B6).
- isotonic pool/sort tests pass with `>` for `>=` (B3).
- test_block_len_is_the_horizon_in_decisions: uniform gaps, passes with mean instead of median (B1).
- No test on calib_row/e5/e6 wiring (baseline restart at split, pooled bootstrap, momentum on s.ts).

## Break-it
B1 mean gap for block length: SURVIVES. B2 permutation inequality: caught. B3 `>`→ in PAV: SURVIVES. B4 gate inclusive: caught. B5 pooled bootstrap: caught. B6 ties sorted y descending: SURVIVES. B7 AUC tie averaging: caught.

## Reading list
evidence.py:40-54 (Brier minus baseline), 57-98 + 758-772 (block bootstrap), 207-219 (verdict), 101-118 (permutation), 121-151 + 937-941 (momentum), 154-200 + 942 (isotonic, AUC; R1), 913-963 (split), 222-243 (gate sweep vs policy.py:51-53), 823-829/873-878/973-982/1055-1064 (headline claims), 476-492 (reconcile).

## Could be deleted
evidence.py:33 GATE_EPS (import policy.EPS).
