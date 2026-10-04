# Hyperliquid decision trader — SPEC

A model reads a live Hyperliquid order book on a fixed cadence and outputs a probability the price rises over the next 60 s. The bot turns that into buy / sell / hold, places real orders, and a local dashboard streams every decision, fill, cost and calibration statistic. Why and how success is judged: see PROBLEM.md. Interview record: `.pipeline/spec-interview.md`.

## Decisions (from the interview)

| Topic | Decision |
|---|---|
| Stack | Python (SDK: `hyperliquid-python-sdk`, 0.24.0, maintained) |
| Venue / market | Hyperliquid testnet by default, BTC perp (`COIN` configurable, BTC default) |
| Cadence | One decision per fresh `l2Book` snapshot (~5.3 s apart, measured: `.pipeline/hl-update-rate.md`) |
| Horizon | 60 s for trading, the gate and exits. Outcomes are also recorded at 30 s and 300 s (measurement only) |
| Model output | `p_up` only: P(mid at t+60 s > mid at t) |
| Action | buy if `p_up >= GATE`, sell if `p_up <= 1-GATE`, else hold. `GATE` default 0.60 |
| Order type | Marketable IOC limit (taker). Exits: reduce-only IOC at horizon |
| Exit policy | Time exit: each entry is closed 60 s after it filled |
| Decider deadline | 2 s; late means hold, logged as a late hold |
| Calibration label | Sign of mid change over each horizon (30 s, 60 s, 300 s); ties dropped per horizon. Every decision is scored, traded or not. The headline Brier and reliability curve use 60 s; 30 s and 300 s are shown alongside |
| Storage | SQLite (stdlib) |
| Dashboard | FastAPI + SSE + static page, bound to 127.0.0.1 only |
| Network | Testnet only. There is no mainnet code path: `NETWORK=mainnet` is rejected (user will not trade real money) |
| Limits | Defaults $500 max position, $100 max daily loss; both configurable via env, no hard ceiling |

**Assumptions I added (not your decisions; check them):**
- While a position is open, a signal on the opposite side is treated as hold (logged as `blocked`). A same-side signal may add up to the position cap. Each entry keeps its own 60 s exit timer.
- Size: notional scales linearly from the $10 exchange minimum at the gate to the position cap at `p = 0.80`, never above the cap (all configurable).
- Consecutive order errors threshold: 3. Stale book threshold: no `l2Book` for 15 s (about 3 missed snapshots).
- Daily loss is measured per UTC day on realized plus unrealized P&L after fees.

## Design proposal

Modules (`src/jevhl/`): `config` (env; rejects mainnet) · `rounding` (price and size to exchange rules) · `feed` (WS client, message parsing, staleness) · `model` (interface, `MockModel`, `DeciderModel` over HTTP) · `policy` (gate, abstain, confidence sizing, caps; pure functions) · `risk` (kill switch state machine) · `exchange` (SDK wrapper: place, close, cancel, flatten, fills; fee capture) · `paper` (dry-run fill simulation against the book) · `ledger` (positions, fees, P&L before and after fees, confirmed by exchange fills only) · `store` (SQLite) · `calibration` (labelling, Brier, baseline Brier, reliability bins) · `trader` (the loop: one decision in flight, hold when late) · `server` (FastAPI, SSE, static dashboard).

Loop: new `l2Book` snapshot arrives → if a decision is already in flight or the book is older than the staleness bound, skip (hold) → ask model with a 2 s deadline → policy → fire order, show intent immediately → position and P&L change only on exchange fill confirmation (user-event WS and an `info` poll as a backstop).

Model contract (mock and decider): input `{ts, coin, mid, spread_bps, bids[[px,sz]...10], asks[[px,sz]...10], recent_mids[last N], horizon_s}`; output `{p_up: float in [0,1]}`. The decider's real input and output format is decided at milestone 5 (asked then).

Dashboard shows: live mid and markers, current decision (`p_up`, action, gate, size, latency), position, P&L after fees and before fees, cumulative fees, decision and late-hold counts, hold rate, reliability curve, running Brier vs baseline Brier with sample count, kill-switch state, connection state.

## Hard constraints

- Testnet only. Config rejects any network other than testnet; no mainnet code, URLs or confirmation flow exist. Position and daily-loss limits default to $500 / $100 and are configurable.
- API (agent) wallet key only. Key only from `.env`, which is in `.gitignore` before it exists. Never logged, never committed. No withdrawal or transfer code anywhere.
- Kill switch on max daily loss, 3 consecutive order errors, or stale book: cancel all, flatten with reduce-only, stop, show on dashboard. Also flatten on clean shutdown.
- Signing and orders only through the SDK. Price and size rounded to exchange rules (tick, lot, significant figures), and tested.
- Well inside rate limits (`.pipeline/hl-facts.md`): one WS, few subscriptions, at most about 2 orders per minute plus light polling.
- Ask before adding any dependency that needs system packages.
- Pipeline rule: only the user merges.

## Tests (pytest, in the first milestone where each unit exists)

Rounding; hold-when-late; abstention and gating; confidence-scaled sizing with caps; kill switch (each trigger); dry-run fill simulation; parsing of realistic Hyperliquid messages (mocked fixtures); decider backend against a mocked server (success and timeout); calibration maths (Brier, baseline, reliability bins, tie dropping); config rejects mainnet.

## Milestones

### Milestone 1: Foundations and offline core
Goal: tooling, tests and the pure logic, with no network. Scaffold (`uv`, ruff, pytest, CI), `config` (rejects mainnet), `rounding`, `policy`, `risk`, `store`, `calibration`, `MockModel`.
Acceptance: `uv run pytest` and `ruff check` green in CI. Tests cover rounding, gating and abstention, sizing and caps, each kill-switch trigger, calibration maths, config rejecting mainnet.

### Milestone 2: Live data and dry run
Goal: real testnet market data, real mock decisions, simulated fills, no key needed. `feed`, `paper`, `ledger`, `trader` loop, latency logging to SQLite. Each decision also stores the mid at 30 s, 60 s and 300 s and a label per horizon (`outcomes` table, schema v3); `scored()` and calibration take a horizon argument.
Acceptance: dry run streams one decision per snapshot with no errors for 30 minutes; late-hold and stale-book behaviour proven in tests with mocked clocks and messages; message-parsing tests use realistic fixtures; latency percentiles computed from its own logs.

### Milestone 3: Dashboard
Goal: everything in the dashboard list above, live from the dry run. `server` plus static page.
Acceptance: dashboard loads from 127.0.0.1 only, streams decisions and fills, shows after-fee and before-fee P&L, reliability curve and Brier vs baseline at 60 s, with 30 s and 300 s alongside; reconnects after a dropped stream; history reloads from SQLite after restart.

### Milestone 4: Testnet live orders
Goal: real testnet orders with the mock at minimum size. `exchange` wrapper (place IOC, reduce-only close, cancel-all, flatten, fills and fees), `ledger` driven by confirmations, kill switch wired to flatten. User creates testnet account and API wallet themselves (guided).
Acceptance: an hour of live testnet trading; fills, position, fees and P&L match the Hyperliquid testnet UI exactly (reconciliation script plus user's check); low daily-loss limit fires kill switch and flattens; wifi-off drill holds and never trades on stale data; very short decider deadline logs late holds.

### Milestone 5: Decider backend and soak
Goal: `MODEL=decider` over HTTP against `DECIDER_URL`; contract confirmed with the user then. Overnight unattended run.
Acceptance: backend tested against a mocked server (success and timeout); overnight run with no crash and no growing memory; hold rate not stuck at 0% or 100%. Stop here.

(Evidence and README follow as pipeline steps 6 and 7: latency table from the bot's own logs, the calibration result, and an honest note that a short-horizon bot is expected to lose its fees unless the model has real edge.)

## Out of scope

Mainnet (any use); withdrawals or transfers; multiple markets at once; post-only/maker strategy; public deployment of the dashboard; backtesting; training the decider.
