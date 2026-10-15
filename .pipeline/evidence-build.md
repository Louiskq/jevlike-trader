## Built
`scripts/evidence.py` regenerates `results/` from the run databases in `runs/`, opened read-only. Brier, baseline, reliability, latency, soak and ledger numbers all come from jevhl's own functions. The statistics are pure functions: block bootstrap, permutation test, momentum baseline, isotonic recalibration, AUC, gate sweep and an interval-reading rule. 30 synthetic-data tests in `tests/test_evidence.py` cover them. The results are E1 to E6, E8, the compute record and an index README, plus a hand-written `whats-didnt-work.md`. Seeds are fixed (bootstrap 2000 resamples, permutation 1000, seed 20261014), and a rerun reproduces the committed files byte for byte.

Headline numbers (exactly as computed):
- **E1 latency:** R3 taker on the VM: model p50 41.0 ms, p90 64.6, p99 289.4, max 955.9 ms, 0 late of 1365. R6 maker through the Mac relay: p50 159.1 ms, 0 late of 3507. R2 through the tunnel: p50 137.3 ms, 11 late (0.77%). Maker order action p50 633 ms.
- **E2 reconcile:** all 5 live runs (R1, R2, R3, R5, R6) agree with every recorded exchange figure: fills, fees, closedPnl to 4 dp, end position. R2's record gives only the fill count.
- **E3 drills:**
  - Done live: short deadline (24/24 late holds, 0 fills); maker kill cancelling its resting order (R5, the M9 bug); R6's clean shutdown.
  - The loss-limit drill is **log only** (see Unsure).
  - Network loss: the only evidence is the accidental pre-M6 DNS failure (drill.db).
  - **The M6 flatten retry and the M7 stale pause/resume were never drilled live** (0 stale pauses in R3, R5, R6).
- **E4 unattended (R3, 2 h, not overnight):** 0 error/kill/stale/flatten events; hold rate 64.9%. RSS quarter means 70.0/70.3/70.5/70.6 MB. From the 1-minute sample to the last in-run sample: **+0.88 MB (+0.45 MB an hour), not levelled off**. The log's "flattening" is not clearly supported, and the file says so. R6 rose +2.82 MB over 1 h.
- **E5 calibration:** no skill shown at any horizon.
  - Pooled R3+R6 at 60 s: Brier 0.2765 vs baseline 0.2494, n=2809 (about 177 non-overlapping windows). 95% block-bootstrap interval of the difference [-0.0155, +0.0653].
  - Every main cell: "no difference shown", except R6 300 s, which is "too few samples" (7 blocks).
  - The model leaned down: mean p_up 0.378 against an up share of 0.534 (pooled, 60 s).
  - R3 and R6 figures match the run reports exactly.
- **E6 baselines:**
  - Model or recalibrated model beat the base rate out of sample in **no cell**.
  - The momentum heuristic did in one cell, R3 30 s, only just: -0.0172 [-0.0334, -0.0004].
  - **R6's AUC at 30 s and 60 s is above 0.5:** 0.686 [0.572, 0.793] and 0.647 [0.509, 0.776]. The files call this a lead from one 1-hour run, chosen after seeing the run-level results, not an edge.
  - Permutation p-values: R6 30/60 s 0.001; R3 30 s 0.025. Shuffling ignores overlap, so these overstate the evidence, as the file says.
  - R3's confident tail is worse, not better: Brier 0.2792 at gate 0.60 vs 0.2737 for all decisions.
- **E7:** not done; the README says the sample is too small to claim anything either way.
- **E8 P&L:** all live runs together: -$0.1864 before fees, $1.3992 fees, -$1.5856 after fees, 319 fills.
  - Every run lost after fees; in R1, R2 and R3 the fees exceeded the before-fee loss.
  - Measured fee rates: maker 1.50 bps, taker 4.50 bps.
  - In R3 the median 60 s mid move was 0.7 bps, against a 9 bps taker round trip.
- **Compute:** 8.13 GPU-hours (6 jobs, 1 GH200 each, serving only).

## Commits
- 233fdcd Add the evidence script and its statistics, with tests
- dd10c0f Add the step-6 evidence results (E1-E6, E8, compute)

## Deviations from SPEC.md
- **`whats-didnt-work.md` is hand-written, not generated.** It is prose from the log and DECISIONS.md, with no computed numbers of its own. The README marks it as not generated.
- **E5 adds a table for R2 and R5** (the other real-model runs), outside the plan's R3/R6/pooled set. Leaving them out could look like cherry-picking. They are not pooled.
- **E4's plot and table also show R6's memory**, labelled as not an unattended-run test.
- **E6 adds intervals the plan didn't list:** block-bootstrap intervals on the test-half differences (model, B3, A2 against B2) and on the AUC. Without them the point values invited over-reading: the recalibrated R6 Brier looked better than the base rate, and its interval crosses zero.
- **A "too few samples" rule:** an interval from fewer than 10 bootstrap blocks is not read. Without it, R5 at 300 s gave a zero-width interval that read as "worse".
- **The GPU job table is copied into the script** as a constant from `.pipeline/compute-raw.md`, which is untracked, so reruns don't depend on it.

## Unsure about
- **`runs/drill.db` is incomplete.** It was copied without its WAL file. The original `drill.db` in the repo root (with a 3 MB `drill.db-wal`) also holds the loss-limit drill session. I checked it read-only: `kill_switch daily_loss` at 2026-10-08 23:07:29 UTC and 9 fills with fees $0.043794, matching the log. I tried to replace `runs/drill.db` with a full backup and the permission system denied it, so the copy is unchanged. drills.md and reconcile.md therefore mark the loss-limit drill as "log only".
  - The fix: run `sqlite3 drill.db ".backup runs/drill.db"`, then rerun the script. The script already picks up a daily_loss session and reports its database facts if one is present.
  - The other copies look complete (their counts match the log).
- **Read-only opens created sidecar files in `runs/`:** `*.db-shm` and empty `*.db-wal` (the databases are in WAL mode). The main `.db` files are unchanged in size and mtime. They are gitignored; delete them if unwanted.
- **Block length** is the horizon divided by the median gap between scored decisions, rounded up. For the maker, whose scoring comes in bursts (median gap 1 s, mean about 2 to 3 s), this gives blocks about twice the horizon in time. That widens the intervals (conservative). A mean-gap definition would give R6 shorter blocks and narrower intervals.
- **short.db timestamps** (14:04 UTC) come 4 minutes before the start that compute-raw.md gives for job 7192991 (14:08), although that job is labelled "short-deadline check + R2". The database doesn't record which model answered, and latency.md says so.
- **Some log dates are local, not UTC:** the PIPELINE.md log dates the drills 2026-10-09, but drill.db's timestamps are 2026-10-08 UTC. drills.md notes this.
- **R2 reconcile:** the log recorded only "reconcile OK (114 fills)". The fee, closedPnl and position checks show "not recorded" rather than being assumed.

## Spec traceability
- E1 latency → scripts/evidence.py:413-490 (`e1_latency`, uses `jevhl.latency.latency_report` and the `maker_latency` events)
- E2 reconcile → scripts/evidence.py:476-550 (`_ledger_row`, `e2_reconcile`; `RECONCILE` and `LOSS_DRILL` constants near line 260)
- E3 drills → scripts/evidence.py:555-640
- E4 unattended and rss.png → scripts/evidence.py:650-755 (uses `jevhl.soak.soak_report`)
- E5 calibration, reliability PNGs and block bootstrap → scripts/evidence.py:758-910; `block_len`/`block_resample`/`block_bootstrap` at 57-98; `verdict` at 210
- E6 B1-B3, A1-A3, AUC → scripts/evidence.py:913-1070; pure functions at 101-245
- E8 P&L → scripts/evidence.py:1073-1148 (`fill_split`, `jevhl.ledger.Ledger`)
- Compute → scripts/evidence.py:1152-1175
- README index → scripts/evidence.py:1177-1209; whats-didnt-work → results/whats-didnt-work.md
- Tests → tests/test_evidence.py:1-229

## Verification
- `uv run pytest -q tests/test_evidence.py` before the implementation → 28 failed (stub module). Later, the 2 verdict tests failed before `verdict` existed.
- `uv run pytest -q` → 914 passed, 1 warning (the existing Starlette/httpx deprecation, unrelated)
- `uv run ruff check` → All checks passed
- `uv run python scripts/evidence.py --runs runs --out results` → wrote 9 .md files and 4 PNGs in about 13 s. A second run matched the first (identical shasums), and a rerun after commit leaves `git status results` clean.
- Scan of results/, scripts/ and tests/test_evidence.py for addresses, user paths, cluster names, IPs and keys → no matches

## Review fixes (R1-R4 and the weak test; .pipeline/evidence-review.md)

### Commits
- 77200b2 Fix: isotonic recalibration pools tied probabilities before PAV (R1)
- 14fbf7e Fix: E6 says how many out-of-sample intervals chance alone would exclude (R2)
- 3797a0c Fix: claims in the results prose are computed from the data (R3)
- f5deaf3 Fix: the gate sweep uses jevhl.policy.EPS instead of a copy (R4)
- 7de538a Test: block length from the median gap, with uneven gaps (weak test B1)
- 28e63b8 Results: regenerated after the review fixes

### Changed numbers (old → new)
A2 recalibrated Brier, second halves (baselines.md):
- R3 30 s 0.2859 → 0.2800; R3 60 s 0.2525 → 0.2545; R3 300 s 0.2954 → 0.2948
- R6 30 s 0.2358 → 0.2383; R6 60 s 0.2360 → 0.2312; R6 300 s 0.2728 → 0.2701

A2 minus B2, with 95% intervals:
- R3 30 s +0.0236 [+0.0032, +0.0439] → +0.0177 [+0.0033, +0.0321]; still "worse than baseline"
- R3 60 s -0.0042 → -0.0022; R3 300 s +0.0232 → +0.0226
- R6 30 s -0.0145 → -0.0119; R6 60 s -0.0190 [-0.0570, +0.0370] → -0.0239 [-0.0578, +0.0252]; R6 300 s +0.0198 → +0.0171
- No verdict changed.

New statements, same data:
- **E6 headline and table note:** "2 of 18 intervals at 95% exclude zero (1 below, 1 above; 3 too few samples to read); about 0.9 would by chance alone, and 2 or more would 23% of the time, so the cells below zero are not evidence of an edge". The momentum cell now shows its upper bound, -0.0004.
- **Computed prose, which says the same as before on these databases:**
  - The overlap note now uses measured spacing: 5.3 s median taker gap, about 11 decisions per 60 s window, maker 1.0 s median and 2.2 s mean.
  - "A few hundred windows" becomes "at most 356".
  - E4 says 2.0 h, "0 error, kill, stale or flatten events" and "still rising in the last quarter".
  - The E3 headline lists what the databases show working: loss-limit kill, short deadline, maker cancel-on-kill.
  - The AUC note gives per-cell figures and "R6 (1.0 h)".

Unchanged: everything in E1, E2, E3's table, E5's numbers, E8 and compute.

### Verification
- Tests first; each new test failed before its fix:
  - R1: 2 tie tests failed. The maximal-blocks test passes on correct code and fails with `>` planted for `>=`.
  - R2: 4 chance-note and binomial tests failed (the helper did not exist).
  - R3: both E4 fixture tests failed for the right reason ("0 error" and "not levelled off" were hard-coded).
  - R4: the policy-agreement test failed while the script kept its own copy of EPS (it patches `policy.EPS`).
  - B1: planting the mean gap fails `test_block_len_uses_the_median_gap_not_the_mean`.
  - Mutation runs used `PYTHONDONTWRITEBYTECODE=1 python -B`, and each file was restored with no diff.
- `uv run pytest -q` → 925 passed, 1 warning (the existing Starlette/httpx deprecation)
- `uv run ruff check` → All checks passed
- `uv run python scripts/evidence.py --runs runs --out results` twice → identical shasums. Committed results match.

### Unsure about
- R2's wiring into the E6 headline is not covered by a test; only `chance_note` is. An e6 test would need fixture stores for two runs at three horizons. The R3 tests cover E4 end to end, but the other computed sentences (E3, E5, E6, E8) are checked only by reading the regenerated output.
- R4 reads `policy.EPS` through the module at call time rather than `from jevhl.policy import EPS`. That way a test can show the sweep follows the policy; an import by name would bind a copy at import time.
