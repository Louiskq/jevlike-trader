# Evidence plan (step 6), draft for the user's approval

Source of truth: PROBLEM.md "How I'll know it worked". Everything below is computed from the bot's own SQLite logs; no new trading is needed except the optional run in E7. Output goes to `results/` (tables as Markdown, plots as PNG), produced by one script (`scripts/evidence.py`) so anyone can rerun it on the same databases.

## Runs used (already done)

| Run | Strategy | Model | Where | Length | DB |
|---|---|---|---|---|---|
| R1 hour run | taker | mock | Mac | 1 h | hour.db |
| R2 real-model run | taker | decider | Mac (tunnel) | 2 h, ended by a stale-book kill (fixed in M7) | soak.db |
| R3 unattended run | taker | decider | VM | 2 h | overnight.db (VM) |
| R4 maker paper run | maker | mock | Mac | 30 min | maker30.db |
| R5 maker live run | maker | decider | VM | 14 min, killed by the M9 bug | maker_live.db (VM) |
| R6 maker live run | maker | decider | VM | 1 h | maker_live2.db (VM) |
| Drills | taker | mock | Mac | short | drill.db, short.db |

## Software evidence

- **E1 Latency table** (PROBLEM: "latency numbers in the README come from its own logs"). Per run: model latency p50/p90/p99/max, late-hold rate; maker runs also REST book, order action (modify/place) and loop latency. Expected: VM taker p50 ~41 ms; maker p50 ~159 ms through the Mac relay; modify ~630 ms.
- **E2 Reconciliation table.** Per live run: fills, fees, closedPnl local vs exchange, end position, open orders. Expected: all agree (already checked by `jevhl.reconcile` after each run; the table re-reads the DBs and quotes the saved reconcile output).
- **E3 Safety drills table.** Loss-limit kill, network loss (stale pause, flatten retry), short deadline (late holds), stale feed recovery, maker kill switch cancelling resting orders. Each row: what was done, what the bot did, evidence (DB or log). From PIPELINE.md log and the DBs; no reruns.
- **E4 Unattended run.** RSS over R3 (plot) and hold rate. Expected: flat memory (+0.6 MB over 2 h). Note honestly: 2 h, not overnight (user decision 2026-10-10).

## Model evidence (judged on calibration, not P&L)

- **E5 Calibration.** For the real-model runs (R3 taker, R6 maker, and pooled), at 30/60/300 s: Brier, baseline Brier (running base rate), reliability curve (10 bins, with counts), sample count. Because consecutive decisions overlap, a **block bootstrap** (blocks of one horizon length) gives a 95% interval on Brier minus baseline. Expected from the run reports: model worse than or level with the baseline at all horizons (no skill shown).
- **E6 Baselines and ablations** (offline, from stored `p_up`, mids and outcomes):
  - B1 constant 0.5 (Brier 0.25 by construction, the floor of "no information").
  - B2 running base rate (what the dashboard already shows).
  - B3 momentum: p_up = 0.5 + k * sign(last 30 s mid change), k fitted on the first half and tested on the second. Tests whether the model beats a one-line heuristic from the same mids.
  - A1 shuffled p_up (permutation, 1000 shuffles): is the model's Brier distinguishable from its own outputs in random order? Tests whether ordering carries any information.
  - A2 recalibrated p_up (isotonic fit on the first half, tested on the second): separates "bad calibration" from "no ranking skill" (AUC reported too).
  - A3 gate sweep (taker R3 only): Brier and hit rate of decisions that would trade at gates 0.55/0.60/0.65/0.70. Tests whether the confident tail is better than the rest.
  No seeds: the decider is served deterministically and runs are on live markets, so repeats are new data, not reseeds. Noted as a limitation.
- **E7 (optional, needs your approval and GPU time): one more real-model run** to reach "a few thousand" scored decisions per strategy. Pooled R3+R6 now: about 1,600 taker-equivalent scored decisions at 60 s, heavily overlapping. Suggestion: a 3 h taker run on the VM (~3 GPU-hours, 1 GH200). Without it, the README says the sample is too small to claim anything either way.

## P&L (last, as PROBLEM.md says)

- **E8 P&L table.** Per live run: before fees, fees, after fees, fills, maker/taker split, fee rate. Plus the honest note: short-horizon bots usually lose their fees; maker fees (1.50 bps) are a third of taker fees (4.50 bps) measured on testnet; testnet liquidity is thin and not like mainnet.

## What didn't work (from real evidence only)

The R2 stale-book stop (fixed in M7), the first drill's flatten gap (M6), the R5 side-change kill (M9), the decider's up bias seen on 2026-10-08, and the caught mistakes in DECISIONS.md.

## Compute record

GPU-hours per run, from Slurm (`sacct`), into `results/compute.md` (no usernames or paths).
