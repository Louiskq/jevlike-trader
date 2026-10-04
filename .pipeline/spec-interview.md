# Spec interview log

Q: Stack: Python or TypeScript?
A: Python (Recommended option chosen).

Q: Do you have a Hyperliquid testnet account and an API (agent) wallet?
A: Neither yet.

(Before these answers, I read jev-trader's README/SPEC/CLAUDE.md and measured Hyperliquid's WS update rate: .pipeline/hl-update-rate.md)

Q: Decision cadence (l2Book snapshots arrive ~5.3 s apart; bbo is event-driven).
A: 5 s on l2Book (Recommended option chosen).

Q: Which market on testnet?
A: BTC perp (Recommended option chosen).

Q: Order type.
A: IOC limit, marketable (Recommended option chosen).

Q: Prediction horizon for calibration label.
A: 60 s.

Q: Model output contract.
A: p_up only (Recommended). Bot derives buy/sell/hold from the gate.

Q: Calibration label.
A: Sign of mid change over 60 s, ties dropped (Recommended).

Q: Exit policy.
A: Time exit at horizon (Recommended): reduce-only IOC close at 60 s.

Q: Storage.
A: SQLite (Recommended).

Q: Default probability gate.
A: 0.60 (Recommended).

Q: Decider deadline.
A: 2 s (Recommended).

Q: Testnet risk caps.
A: Same as mainnet: $50 max position, $10 max daily loss (Recommended).

Q: Dashboard serving.
A: FastAPI + SSE + static page, localhost only (Recommended).
