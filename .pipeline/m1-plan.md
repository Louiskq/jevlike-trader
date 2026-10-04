# M1 plan (builder, plan mode) — saved from the builder's report

Orchestrator notes (added): the builder's "not a git repo" remark is stale; the repo exists with remote, .gitignore and CI already committed (844c412). Rounding rules verified against https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/tick-and-lot-size on 2026-10-03: price <= 5 significant figures and <= (6 - szDecimals) decimals for perps; integer prices always allowed; size rounded to szDecimals. Builder should extend, not recreate, .gitignore and ci.yml.

## Files
- `.env.example` (no secrets); `pyproject.toml` (uv, src layout, hatchling, Python >=3.11, no runtime deps beyond stdlib; dev: pytest, ruff; ruff E,F,I,B,UP, line 100); CI already exists.
- `src/jevhl/{__init__,config,rounding,types,policy,risk,store,calibration,model}.py`
- `tests/{test_config,test_rounding,test_policy,test_risk,test_store,test_calibration,test_model}.py`, `tests/fixtures/meta_btc.json`
- SDK and FastAPI NOT added yet (M2/M3).

## Commits (4)
1. Scaffold + config/mainnet guard, with tests.
2. types + rounding + policy (gate, abstain, sizing, caps), with tests.
3. risk kill switch + MockModel, with tests.
4. store (SQLite) + calibration, with tests.
Tests written first and seen failing each time.

## Choices
Config: frozen dataclass; defaults network=testnet, coin=BTC, gate=0.60, horizon=60, deadline=2.0, min_notional=10, full_size_p=0.80, stale_s=15, max_order_errors=3. Hard caps $50 position / $10 daily loss as module constants, apply on both networks; env may lower but not raise (raise ConfigError, no silent clamp). Mainnet requires NETWORK=mainnet AND injected confirm() returning exactly a fixed phrase ("I understand this uses real funds"). Unknown NETWORK raises. Gate in (0.5,1) and below full_size_p. Key from HL_API_KEY, repr=False, never logged.

Rounding: AssetMeta from meta()["universe"] entry. Size floored to szDecimals. Price per HL rules above. Marketable IOC price: buy rounds up, sell rounds down. Decimal throughout, float out for SDK. Tests: BTC szDecimals=5 65432.123 -> 65432; ETH szDecimals=4 3456.789 -> 3456.8; low-price decimals cap; size floor edges; min notional; buy-up/sell-down; zero/negative raises.

Types: Side, Action, Decision(ts,p_up,action,reason,size_usd,gate,latency_ms) with reason in {signal, below_gate, blocked, late, stale, cap_reached, killed, invalid}; net signed position_usd; ModelInput per contract; ModelOutput p_up only.

Policy (pure): p>=gate buy; p<=1-gate sell; else hold below_gate. Opposite side while position != 0 -> hold blocked. Size linear from min_notional at gate to cap at p=0.80 using conf=p (buy) or 1-p (sell), clamped. Room = cap - |position|; room < min_notional -> hold cap_reached; else size=min(size,room). NaN/out-of-range p -> hold invalid. late_hold()/stale_hold() helpers.

Risk: KillSwitch with injected clock, no I/O. Triggers daily_loss / order_errors / stale_book, latched until reset(). Consecutive errors, success resets, 3rd trips. Daily loss: (realized + unrealized - fees) <= -max_loss trips (== trips); UTC rollover resets baseline to the P&L at rollover. Stale: now - last_book > 15 s trips (exactly 15 does not); armed on first book. Boundary tests for each.

Store: sqlite3, WAL, :memory: for tests. Tables decisions(label, mid_future nullable), fills (UNIQUE(oid,ts,sz)), events, schema_version. Parameterized SQL only.

Calibration: label(mid_t, mid_t60) -> 1|0|None (ties None). brier. Baseline Brier = constant predictor b=mean(ys), b(1-b); also running baseline (expanding mean of prior labels) to avoid lookahead. reliability_bins n=10 equal width, p=1.0 in last bin, empty bins included. All decisions scored. Empty -> None.

MockModel: deterministic seeded; p = 0.5 + 0.3*tanh(k*imbalance over top-10) + seeded noise, clipped [0.01,0.99]; optional injected delay_s for late-hold tests. Model Protocol.

## Builder's risks/ambiguities
1. Rounding from memory (now verified, see top).
2. Same-side adds when room < $10 minimum: hold cap_reached (spec silent).
3. Baseline Brier: in-sample vs running (spec silent); in-sample on dashboard, user to decide.
4. Hard caps apply on testnet too; no override above $50.
5. Mainnet confirmation phrase unspecified; fixed phrase in one constant.
6. Daily-loss across UTC midnight with open position: baseline reset to P&L at rollover.
7. CI untested locally.
8. Check uv/Python version first.

## Amendments after the plan gate (user answers, 2026-10-04)
- Testnet only, no mainnet code path (spec commit e558bd4). Config rejects NETWORK != testnet. No confirmation phrase, no hard-cap constants. Limits default $500 position / $100 daily loss, configurable via env, no ceiling.
- Near-limit: room < $10 minimum -> hold `cap_reached` (kept; user said raise the caps so it rarely occurs).
- Baseline Brier: provide both in-sample and running; headline is running.
- All other plan choices stand (user did not object).
