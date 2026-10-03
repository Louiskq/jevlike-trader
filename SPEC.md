# Build: an AI decision trader on Hyperliquid (inspired by jarrodwatts/jev-trader)

## Goal

My own project, inspired by https://github.com/jarrodwatts/jev-trader but not a port. Same core idea: a decision model reads a live order book and decides on a fixed cadence, the bot places real orders, and a dashboard streams every decision and fill live. Different venue, my own model, my own design.

Read his repo first (README, SPEC.md, CLAUDE.md, src/) to understand what works, then design this one fresh. Don't mirror his file layout, endpoints or event schema.

## Ideas worth borrowing from his repo
- Dry run with no key: real market data, real decisions, simulated fills.
- One decision in flight; if the model misses its deadline, hold. Never act on stale state.
- Fire-and-forget orders: show the intent immediately, and update position and P&L only when the exchange confirms the fill.
- A lean hot path, with measured and published latency numbers.
- A pluggable model with a mock stand-in, so everything works before the real model exists.

## Where this one should differ
- Venue: Hyperliquid, testnet by default.
- Model: my fine-tuned decider over HTTP (MODEL=decider), plus mock for development. No TypeSafe/Jev API.
- The model can abstain. His bot always trades; this one asks buy / sell / hold, and hold is a real decision, not just a late answer.
- Size follows confidence. Trade only when the chosen side clears a configurable probability gate, and scale size with the model's probability, capped.
- Show calibration live. Alongside P&L, the dashboard shows whether the model's stated probabilities match what happened (a simple reliability view and a running Brier score), since that decides whether the size-by-confidence idea is justified.
- Costs are front and centre. No gas on Hyperliquid, but fees are real. Show P&L after fees and what it would have been before them.

## Hard constraints
- Testnet by default. Mainnet only if NETWORK=mainnet AND typed confirmation at startup, with hard caps: $50 max position, $10 max daily loss.
- Use an API (agent) wallet key that can trade but not withdraw. Never my main wallet key. Never log or commit it; .env goes in .gitignore before it's created. No withdrawal or transfer code.
- Kill switch on max daily loss, consecutive order errors, or a stale book: cancel, flatten with reduce-only, stop, and show it on the dashboard. Flatten on clean shutdown too.
- Use a well-maintained Hyperliquid SDK for signing and orders; don't hand-roll signing. Round price and size to the exchange's rules, and test the rounding.
- Stay well inside Hyperliquid's rate limits (look them up).
- Ask before adding dependencies that need system packages.

## Ask me first
- Stack: Python or TypeScript? My recommendation is Python, since my decider and the planned calibration benchmark are Python, but give me the trade-off in two lines and let me choose.
- Do I have a testnet account and an API wallet? If not, walk me through creating them in the Hyperliquid UI. I do those steps myself.
- DECIDER_URL and its input/output format, only when we reach that backend.

## Design it yourself, then show me

Before writing code, propose in under a page: the module layout, the decision cadence and horizon (measure Hyperliquid's real update rate first), the order type (IOC vs post-only) with your reasoning, the model input/output contract, and what the dashboard shows. Wait for my OK.

## Tests

Rounding, the hold-when-late rule, abstention and gating, confidence-scaled sizing with caps, the kill switch, dry-run fill simulation, parsing of realistic Hyperliquid messages (mocked), and the decider backend against a mocked server (success and timeout).

## Order of work
1. Design proposal → my OK.
2. Dry run with mock on real testnet data; tests pass; dashboard works.
3. Testnet live orders with mock at minimum size for an hour. Check fills, position and P&L against the Hyperliquid testnet UI.
4. Switch to decider.
5. Stop. Mainnet is my decision, not part of this task.

## README

What it is and how it differs from jev-trader (credit his repo as the inspiration), how to run each mode, the safety model, measured latency, and an honest note: a short-horizon bot is expected to lose its fees unless the model has real edge. The calibration panel is how you'd find out.
