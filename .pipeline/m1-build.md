# M1 build summary (builder, verbatim in substance)

Commits: b839408 scaffold+testnet-only config; 16c6b2a types, rounding, policy; c056e91 kill switch + MockModel; 3e63a40 SQLite store + calibration.
Verification (by builder): 107 tests pass, ruff clean, uv sync --locked ok, new test modules seen failing (import error) before implementation; test_smoke.py added after implementation so not seen failing. Orchestrator re-ran tests, lint and smoke import: same result.
Deviations from SPEC: none.

Unsure about (builder):
- order_size adds one lot when floored size < $10 minimum; can exceed cap by < one lot (~$0.65 BTC). Alternative: raise.
- round_price takes up=True/False, no slippage; M4 must choose the marketable reference price.
- Late: latency_ms > deadline*1000 (exactly 2000 ms not late); late takes priority over invalid.
- Gate comparison uses 1e-12 tolerance for float noise in 1-gate.
- Running baseline uses 0.5 as first prediction.
- scored() includes held decisions, excludes ties and decisions with no p_up (late/stale).
- Daily-loss baseline = first P&L seen; on UTC rollover becomes last P&L seen before the update; restart mid-day re-baselines (M2/M4 may need to persist).
- KillSwitch.check() must be polled by caller for staleness.
- MockModel is synchronous with injected sleep; M2 may want async.
- uv.lock committed (CI uses --locked); CI not run remotely.
- requires-python>=3.11, developed on 3.13/3.14.
