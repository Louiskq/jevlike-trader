# M4 build summary (builder subagent, condensed from its report)

Built: exchange.py (SDK wrapper: IOC entry, reduce-only close, cancel-all, flatten, fills with tid and fee), trader driven by exchange confirmations only (calls in a worker thread under one lock, stop re-checked after every await), kill switch flattens, `run --live` with startup checks, `python -m jevhl.reconcile`. Dry run unchanged, no key needed. Nothing run against a real testnet account.

Commits: 59d2544 exchange wrapper + fake venue; cfda52b trader from confirmations; 3abe9ab --live, startup checks, reconcile. No Co-Authored-By. Not pushed.

Deviations from SPEC:
- No user-event WebSocket; only userFillsByTime read after each order and every 15 s.
- Wrapper blocking, trader async (to_thread under one lock); check_stale/_halt/shutdown/_flatten now async; 6 existing test lines edited (await, tr._paper -> tr._executor); no assertions weakened.
- PaperExchange gained cancel_all and flatten; SLIPPAGE_BPS, reduce_size, aggressive_price moved to rounding.py.
- Live entries sized at the limit price, aimed at ~$10.50 (5% above $10 minimum).
- New dependency hyperliquid-python-sdk==0.24.0 (24 packages incl. eth-account, ckzg, cytoolz, bitarray, pycryptodome; prebuilt wheels, no system packages); touches uv.lock.
- Exits count toward the consecutive-order-error counter (also paper).
- HL_ACCOUNT_ADDRESS added to Config (0x + 40 hex, not secret). Needed: API wallet only signs; fills/positions read with main account address.
- .env not auto-loaded: `uv run --env-file .env python -m jevhl.run --live ...`.
- Edited .pipeline/hl-facts.md with verified formats and unverified items.

Unsure / unverified without an account:
- Error text when an IOC matches nothing (code looks for "immediately match"; otherwise counts as order error, fails safe but noisy).
- Whether reduce-only closes below $10 are exempt from the minimum.
- Whether the UI's closed P&L is net of fees (reconcile computes from fills, ignores closedPnl).
- Real SDK Exchange(...) constructor with timeout/account_address and no meta (tested only with a fake class).
- Orphaned threads: never abandoned; bounded by 10 s HTTP timeout and ~2.5 s confirmation polling; a stop waits up to a few seconds for an in-flight call.
- Flatten with wifi off retries 3 times, logs flatten_failed, still stops; position stays open; report prints "exchange position UNKNOWN".
- Live and paper need separate DBs (--live refuses a DB with paper fills).
- First live hour: set MAX_POSITION_USD=15 for minimum size; documented in .env.example, not enforced in code.

Cannot verify without a testnet account: an hour of live trading; fills/position/fees/P&L matching the UI; daily-loss limit firing and flattening on testnet; wifi-off drill on hardware; short decider deadline late holds on a live run; the signed-order path (no real order ever sent).

Verification (builder): 419 tests pass (309 before), ruff clean, 30 s live-data dry run, dashboard smoke, --live without key exits cleanly. ~60 hand mutants run; 5 survived initially, tests added. Orchestrator re-ran tests (419), ruff, --live without key (clean error), found no co-author trailers and no mainnet URLs.
