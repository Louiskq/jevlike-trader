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
| Network | Testnet only for trading. There is no mainnet trading path: `NETWORK=mainnet` is rejected (user will not trade real money). One read-only exception from milestone 10 (user decision 2026-10-26): with `MAKER_PROMPT=mainnet` the maker reads mainnet's public BTC order book, because the fine-tuned decider was trained with mainnet prices in its prompt |
| Limits | Defaults $500 max position, $100 max daily loss; both configurable via env, no hard ceiling |

**Assumptions I added (not your decisions; check them):**
- While a position is open, a signal on the opposite side is treated as hold (logged as `blocked`). A same-side signal may add up to the position cap. Each entry keeps its own 60 s exit timer.
- Size: notional scales linearly from the $10 exchange minimum at the gate to the position cap at `p = 0.80`, never above the cap (all configurable).
- Consecutive order errors threshold: 3. Stale book threshold: no `l2Book` for 15 s (about 3 missed snapshots).
- Daily loss is measured per UTC day on realized plus unrealized P&L after fees.

### Maker strategy (milestone 8, decided 2026-10-10)

| Topic | Decision |
|---|---|
| Strategy | `STRATEGY=taker` (default: everything above, unchanged) or `STRATEGY=maker`, sharing the feed, model, ledger, kill switch, reconcile and dashboard |
| Maker cadence | About one decision per second, on a REST `l2Book` snapshot plus the trades received since the last decision (WS `trades` subscription). Measured 2026-10-10: testnet's WS book updates only every ~5.25 s and the REST top of book changed twice in 15 s, so most seconds repeat the same book |
| Maker action | Always buy or sell: buy if `p_up >= 0.5`, else sell. Hold only when the model is late or its answer invalid. When the position cap blocks a side, the quote goes on the other side (logged as capped) |
| Maker order | One post-only (Hyperliquid `Alo`) limit order on the model's side, one tick inside the touch (at the touch when the spread is one tick). It is replaced only when the side or the quote price changes, so an unchanged quote keeps its queue place. A price change on the same side is one modify; a side change is a cancel plus a new post-only order (milestone 9: Hyperliquid refuses a side-changing modify, measured 2026-10-11) |
| Maker prompt | Jarrod-style state: book imbalance, depth at several distances from mid, top levels, returns over several windows, recent mids, taker trades (buy and sell volume, net flow, last prints), plus short instructions. Drafted by the assistant for the user to edit (a modelling decision). The taker strategy keeps its current prompt |
| Maker scoring | Calibration scores a maker decision only when the book it saw differs from the last scored one, so ~60 overlapping decisions per window do not inflate the sample count (user decision 2026-10-10) |
| Maker deadline | `MAKER_DECIDER_DEADLINE_S` default 1 s (user decision 2026-10-10); taker keeps 2 s |

**Assumptions I added for milestone 8 (not your decisions; check them):**
- Quote size fixed at `MAKER_SIZE_USD` (default $11, just above the $10 minimum), position cap as configured.
- No time exit in maker mode: the position is reduced by quoting the other side, as in Jarrod's bot; kill switch and shutdown still cancel all and flatten.
- On a late or invalid answer the resting quote is left as it is; on a stale book, a kill or shutdown, every resting order is cancelled first.
- A rejected post-only order (it would have crossed) is logged and re-priced at the next decision.
- Order actions capped at 20 per minute (modify when possible; a side change counts as two actions, its cancel and its place, and is sent only when both fit); on a rate-limit error the bot cancels and pauses quoting. Per-address request budget logged.
- If the venue still refuses a modify (`Attempted to modify to invalid new order`), the old order is still resting (measured 2026-10-11): it stays tracked, the answer is logged and is not an order error, and the next decision re-quotes.
- Paper fills: the resting quote fills only when a real trade print goes through its price (others are queued ahead at that price; user decision 2026-10-10, stricter than the first draft); maker fee 0.015% until verified on testnet.

## Design proposal

Modules (`src/jevhl/`): `config` (env; rejects mainnet) · `rounding` (price and size to exchange rules) · `feed` (WS client, message parsing, staleness) · `model` (interface, `MockModel`, `DeciderModel` over HTTP) · `policy` (gate, abstain, confidence sizing, caps; pure functions) · `risk` (kill switch state machine) · `exchange` (SDK wrapper: place, close, cancel, flatten, fills; fee capture) · `paper` (dry-run fill simulation against the book) · `ledger` (positions, fees, P&L before and after fees, confirmed by exchange fills only) · `store` (SQLite) · `calibration` (labelling, Brier, baseline Brier, reliability bins) · `trader` (the loop: one decision in flight, hold when late) · `server` (FastAPI, SSE, static dashboard).

Loop: new `l2Book` snapshot arrives → if a decision is already in flight or the book is older than the staleness bound, skip (hold) → ask model with a 2 s deadline → policy → fire order, show intent immediately → position and P&L change only on exchange fill confirmation (user-event WS and an `info` poll as a backstop).

Model contract (mock and decider): input `{ts, coin, mid, spread_bps, bids[[px,sz]...10], asks[[px,sz]...10], recent_mids[last N], horizon_s}`; output `{p_up: float in [0,1]}`. The decider's real input and output format is decided at milestone 5 (asked then).

Dashboard shows: live mid and markers, current decision (`p_up`, action, gate, size, latency), position, P&L after fees and before fees, cumulative fees, decision and late-hold counts, hold rate, reliability curve, running Brier vs baseline Brier with sample count, kill-switch state, connection state.

## Hard constraints

- Testnet only for trading. Config rejects any network other than testnet; no mainnet trading code or confirmation flow exists. The only mainnet URL is the public info endpoint, used read-only (unauthenticated `l2Book` requests) by one module that imports nothing from the exchange code and never sees a key (milestone 10); a test enforces this. Position and daily-loss limits default to $500 / $100 and are configurable.
- API (agent) wallet key only. Key only from `.env`, which is in `.gitignore` before it exists. Never logged, never committed. No withdrawal or transfer code anywhere.
- Kill switch on max daily loss, 3 consecutive order errors, or stale book: cancel all, flatten with reduce-only, show on dashboard. Daily-loss and order-error kills stop the bot for good. A stale-book kill instead pauses it (no new orders, feed still read) and it may resume once books have arrived continuously for a settling period (default 60 s) and no flatten is unresolved; at most 5 such resumes per run, after which the next stale kill is permanent. Every pause, resume and give-up is logged and shown. Also flatten on clean shutdown.
- Signing and orders only through the SDK. Price and size rounded to exchange rules (tick, lot, significant figures), and tested.
- Well inside rate limits (`.pipeline/hl-facts.md`): one WS, few subscriptions, at most about 2 orders per minute plus light polling. Maker mode (milestone 8): at most 20 order actions per minute and one REST book read per second.
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

### Fine-tuned decider (milestone 10, decided 2026-10-26)

| Topic | Decision |
|---|---|
| Goal | A decider that is better than the current one at the bot's question: better calibration and ranking, not P&L (user: "i dont want making money after costs, i just want it to be better than it currenty is") |
| Model | decider9b_full plus a LoRA fine-tune (rank 64) on 26,364 testnet rows rendered with the bot's own maker prompt (`study/`); held-out result in `study/results/finetune.md`. Served with the same `/decide` contract |
| Prompt | The maker prompt plus a mainnet block just before the question: mainnet BTC mid, this market's gap to it in bps, mainnet mid change over 5, 15 and 60 s. The wording is exactly as in training (`study/finetune_data.py`); it is the assistant's draft, like the rest of the maker prompt |
| Mainnet data | Read-only: mainnet's public BTC `l2Book` (info endpoint, no key, no signing) about once a second, only when the mainnet prompt is on |
| Live use | "use it live" (user, 2026-10-26): a live testnet maker run with the fine-tuned model |

**Assumptions I added for milestone 10 (not your decisions; check them):**
- `MAKER_PROMPT=mainnet` turns the block and the mainnet reader on; the default (`MAKER_PROMPT=standard`) is today's prompt with no mainnet requests, so the current decider keeps working unchanged. The taker is unaffected.
- When the latest mainnet book is older than 5 s (or missing), the decision is a hold (new reason `no_mainnet`) and the resting quote is left as it is, as for a late answer: the model never saw a prompt without fresh mainnet prices.
- A mainnet outage is not a kill-switch trigger (it never touches orders); it is logged and shown.
- Warm-up (user decision 2026-10-26): with `MAKER_PROMPT=mainnet`, the first 310 s after the run's first testnet book are holds (reason `warmup`, no model call, no quote), because training skipped each recording's first 310 s and the model never saw a prompt without 5 minutes of history. The standard prompt is unaffected.
- The fine-tuned model is served at temperature 1.0 (it was trained with cross-entropy at T = 1); serving is outside this repository.

### Milestone 5: Decider backend and soak
Goal: `MODEL=decider` over HTTP against `DECIDER_URL`; contract confirmed with the user then. Unattended run of at least 2 hours (user decision 2026-10-10: an overnight run is not needed for now).
Acceptance: backend tested against a mocked server (success and timeout); a 2-hour unattended run with no crash and no growing memory; hold rate not stuck at 0% or 100%.

(Evidence and README follow as pipeline steps 6 and 7: latency table from the bot's own logs, the calibration result, and an honest note that a short-horizon bot is expected to lose its fees unless the model has real edge.)

### Milestone 8: Maker strategy
Goal: `STRATEGY=maker` as decided above: REST book poll and trades feed, the richer prompt (draft for the user), post-only quotes one tick inside the touch replaced only on change, paper maker fills from real prints, dashboard and reconcile working for resting orders and maker fees.
Acceptance: tests for quote pricing (inside the touch, one-tick spread), replace-only-on-change, cap forcing the other side, post-only rejection, partial fills, cancel-all on stale, kill and shutdown, the order-action cap; a 30-minute maker dry run with no errors; a 1-hour live testnet maker run at minimum size whose fills reconcile with the exchange, with resting orders visible in the testnet UI and the kill switch cancelling them.

### Milestone 10: Fine-tuned decider live
Goal: as decided above: the read-only mainnet reader, the mainnet block in the maker prompt behind `MAKER_PROMPT=mainnet`, holds on stale mainnet data, and the mainnet mid and gap recorded and shown.
Acceptance: tests that the bot's prompt equals the training renderer's output for the same inputs; that the mainnet reader is read-only and isolated (no imports of exchange code or keys, info endpoint only, `l2Book` only); that `MAKER_PROMPT=standard` sends today's prompt and makes no mainnet request; stale or missing mainnet data gives a `no_mainnet` hold that leaves the quote alone; the taker unchanged. A 1-hour live testnet maker run with the fine-tuned model whose fills reconcile with the exchange, with calibration reported against R6 and R7.

## Out of scope

Mainnet trading, or any mainnet request that needs a key (reading mainnet's public order book is allowed from milestone 10); withdrawals or transfers; multiple markets at once; public deployment of the dashboard; backtesting; training the decider inside the bot (the fine-tune of 2026-10-26 was done separately, in `study/`).
