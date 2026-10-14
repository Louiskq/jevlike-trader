Closes #21. Implements the approved evidence plan (.pipeline/evidence-plan.md). `uv run python scripts/evidence.py --runs runs --out results` regenerates `results/` byte for byte from the run databases (not committed). Stacked on PR #20 (records).

## Headline (see results/README.md)
- **Software:** all 5 live runs reconciled with the exchange; loss-limit kill, short deadline and maker cancel-on-kill worked live; the M6 flatten retry and M7 stale pause/resume were never drilled live (tests only). VM taker latency p50 41.0 ms, p99 289.4 ms, 0 late of 1365.
- **Unattended:** 2 h (not overnight), 0 error events; memory +0.45 MB an hour, not levelled off.
- **Model:** no skill shown. Pooled 60 s Brier 0.2765 vs baseline 0.2494 (n=2809 overlapping decisions; 95% interval of the difference [-0.0155, +0.0653]). 2 of 18 out-of-sample intervals exclude zero, about what chance gives. R6's AUC above 0.5 at 30 s and 60 s is a lead from one 1-hour run, not an edge.
- **P&L:** -$0.1864 before fees, $1.3992 fees, -$1.5856 after fees, 319 fills. Maker 1.50 bps, taker 4.50 bps.
- **Compute:** 8.13 GPU-hours (serving only).

## Decisions
- Evidence plan approved as written; no extra GPU run. Rationale: none given.
- Complete copy of drill.db after the first copy missed its WAL. Rationale: none given.
- Fix all four review findings. Rationale: none given.

## Caught
- (builder) drill.db copy incomplete → regenerated
- (reviewer) R1 isotonic ties → 77200b2; R2 momentum caveat → 14fbf7e; R3 hard-coded prose → 3797a0c; R4 copied constant → f5deaf3; weak test → 7de538a
- (me) nothing at this step

https://claude.ai/code/session_01Ua15mU8f7EqK7W91vAtfvo
