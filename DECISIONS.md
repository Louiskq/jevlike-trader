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
