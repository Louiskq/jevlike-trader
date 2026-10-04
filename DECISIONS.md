# DECISIONS

Seeded from the spec interview (`.pipeline/spec-interview.md`). Entries are the user's choices; the rejected alternatives and the trade-off stated at the time are recorded so the reasoning is visible.

| Decision | Chosen | Rejected (and why) |
|---|---|---|
| Stack | Python | TypeScript: dashboard-friendly but a second language for the decider and calibration code |
| Cadence | 5 s per `l2Book` snapshot | 1 s on bbo+trades (deeper book stale, heavy overlap); fixed clock (repeats identical data) |
| Market | BTC perp on testnet | ETH (thinner); configurable-first (extra work) |
| Order type | Marketable IOC | Post-only: adverse selection, unfilled orders, harder reconciliation |
| Horizon | 60 s | 30 s (moves rarely beat fees); 5 min (too few independent samples) |
| Model output | `p_up` only | Triple (hold has no ground-truth label); p_up + abstain flag (two things to calibrate) |
| Calibration label | Sign of mid change, ties dropped | Beyond round-trip fee (mixes skill with cost assumptions) |
| Exit policy | Time exit at 60 s | Hold until opposite signal (drift); flat on hold (fee-heavy) |
| Storage | SQLite | JSONL (no indexing, full reload to compute calibration) |
| Gate default | 0.60 | 0.55 (more fee drag); 0.65 (too selective) |
| Decider deadline | 2 s | 1 s (tight); 3 s (staleness approaches 8 s) |
| Network and limits | Testnet only, no mainnet code. Defaults $500 position / $100 daily loss, configurable (amended after interview) | Original: $50/$10 hard caps with a typed-confirmation mainnet mode. Dropped because the user will never trade real money |
| Dashboard | FastAPI + SSE, localhost | Public deployment (exposes trading activity) |

## Evaluation plan
Software: tests pass; dry run decides every cycle; exchange UI reconciliation matches exactly; kill switch, wifi-off and short-deadline drills; overnight run with no crash or memory growth. Model: reliable answers within deadline, hold rate not stuck, reliability curve near the diagonal, Brier below the historical-frequency baseline over thousands of decisions. P&L comes last (see PROBLEM.md).

## Out of scope
Mainnet; withdrawals/transfers; multi-market; maker strategy; public dashboard; backtesting; training the decider.

## M1 / PR #6: decisions

- **Round up one lot to meet the $10 minimum order.** Chosen: keep (alternative: raise an error). Rationale (user): "I want to trade as much as possible to validate the idea."
- **Stale-book timer not armed until the first book arrives.** Chosen: keep for now (alternative: arm at startup and after reset). Rationale (user): "does not matter."
- **Late and invalid answer is recorded as `late`.** Chosen: keep (alternative: `invalid` wins). Rationale (user): "it is late."
- **Spec amended on this branch (e558bd4):** testnet only, no mainnet code; defaults $500 position / $100 daily loss, configurable. Alternative: original $50/$10 hard caps with a typed-confirmation mainnet mode. Rationale (user, in conversation): "i will not be trading real money at any point".

## M1 / PR #6: caught mistakes

> **M1 / PR #6: non-finite env values.** Caught by: reviewer. What: `float()` accepted `nan`/`inf`, so `MAX_DAILY_LOSS_USD=nan` was valid. Why it was wrong: every comparison with NaN is False, so the daily-loss kill switch could never trip. Fix: a7f596e. Prevention: parametrized `test_invalid_values_raise` cases for nan/inf.

> **M1 / PR #6: out-of-range probabilities reached calibration.** Caught by: reviewer. What: a model answer such as 1.5 was stored and scored. Why it was wrong: Brier became infinite and a negative probability wrapped into the top reliability bin. Fix: a987589. Prevention: `test_out_of_range_p_never_scored` and calibration rejection tests.

> **M1 / PR #6: fills deduplicated on (oid, ts, sz).** Caught by: reviewer. What: one order sweeping two resting orders of equal size lost its second fill. Why it was wrong: position, fees and P&L would be understated and the testnet-UI reconciliation would fail. Fix: 491f527. Prevention: `test_fills_same_oid_ts_sz_different_tid_both_stored`.

> **M1 / PR #6: daily-loss baseline reset on restart.** Caught by: reviewer. What: the baseline was the first P&L the object saw, and `reset()` cleared it. Why it was wrong: after a restart or reset, another full daily limit could be lost the same UTC day. Fix: 222f9ad. Prevention: `test_day_start_pnl_survives_restart`, `test_reset_keeps_day_baseline`.

> **M1 / PR #6: float tolerance untested.** Caught by: reviewer (break-it). What: setting `EPS = 0.0` in policy.py left all 107 tests passing. Why it was wrong: gates like 0.67 would silently fail to sell at the boundary. Fix: 615d979 (test only). Prevention: `test_boundary_trades_for_non_float_exact_gates`.

## M2: spec change (multi-horizon labelling)

- **Record outcomes at 30 s, 60 s and 300 s for every decision.** Trading, the gate and exits stay at 60 s; the dashboard headline Brier and reliability curve are 60 s, with 30 s and 300 s alongside. Alternative: 60 s only. Rationale (user): "yes do this". Caveat for the README: longer horizons overlap more, so their samples are less independent.
