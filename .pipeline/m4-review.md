# M4 review (reviewer subagent, condensed from its report)

419 tests pass. Mutation-tested a scratch copy: 21 meaningful mutants, 5 survived (reconcile P&L line, live flatten when ledger flat, exit error-count reset, stop-before-flatten order in _halt, poll's stop check).

## Findings
R1 [must-fix] .env.example:18-20 — comment says keep size small (cap 15) but the committed value is MAX_POSITION_USD=500; nothing enforces minimum size for --live. `cp .env.example .env` then `run --live` trades ~50x the intended size. Test: .env.example value test or a --live startup check on max_position_usd.
R2 [must-fix] trader.py:256-265 — Hard constraint "at most about 2 orders per minute" has no guard; policy allows a same-side add every snapshot (~5.3 s) until the cap; each add gets its own exit. Default config, p~0.65: ~4 entries in ~20 s then 4 exits 60 s later (4-11 orders/min in bursts). Test: 12 books 5.3 s apart at p=0.65, default Config, assert <= ~2 order calls per 60 s window on FakeHL.
R3 [should-fix] trader.py:258-265 — _enter re-checks stopped/tripped after the lock but not book age; sizes from a book captured before the wait. Waits can reach ~70 s while the WebSocket stays healthy, so the stale watchdog never fires. Entry on a 30-70 s old book contradicts "never trades on stale data". Test: hold tr._lock, start on_book at T0, advance clock past stale_s, release; assert no order and an order_skipped event.
R4 [should-fix] reconcile.py:56,66-70 and exchange.py:101-117 — "exchange P&L" is computed with the same Ledger over the same fills just checked identical, so it can never disagree; parse_fill discards closedPnl. M4 acceptance needs P&L to match the testnet UI. Mutant B2 passes all tests. Test: FakeHL fills with non-zero closedPnl; assert mismatch flagged when sum(closedPnl) differs from ledger realized P&L.
R5 [should-fix] trader.py:153-158,174-176 — a fill booked late by the backstop gets exit due at 0.0, closed at next book (~15 s after fill). SPEC: each entry closed 60 s after it filled. test_trader_live.py:94-110 asserts the early close as intended. Test: late part still open at T0+16, closed at T0+60.
R6 [nit] trader.py:145-151 — FillNotConfirmed counts as a consecutive order error although the order filled; 3 slow-index confirms trip the kill switch on a healthy venue.
R7 [nit] trader.py:174-176 — _cover_residue ignores exits already claimed by another _run_exits task waiting on the lock; may add an extra immediate exit and close a separate entry early (reduce-only prevents a flip).
R8 [nit] trader.py:304-314 with :350 — after a halt, run()'s finally calls shutdown() which flattens a second time; with wifi off the drill takes ~2x as long to stop.

## Weak tests
- No trader-level test: passes if Trader._flatten returned early when ledger flat in live mode (B1).
- test_three_failed_orders_in_a_row_trip_the_kill_switch: passes if a successful exit did not reset the counter (B3).
- test_unconfirmed_part_of_an_entry_is_booked_later_by_the_backstop_and_closed: asserts the out-of-spec early close (R5).
- test_report_ok_when_the_store_matches_the_venue: passes if exchange P&L computed from local fills (B2).
- test_a_halt_while_an_entry_waits_for_the_exchange_sends_no_order: passes if _halt set stopped after flattening.
- test_backstop_poll_runs_on_an_interval_not_every_book: passes if _poll_fills dropped its stop check.

## Break-it candidates
B1 trader.py:287 skip live flatten when ledger flat (survives). B2 reconcile.py:56 remote ledger from local fills (survives). B3 delete trader.py:190 on_order_result(True) (survives). B4 trader.py:259 `if False:` (caught). B5 trader.py:140 `continue` -> `pass` (caught).

## Reading list
exchange.py:30,266-291 and config.py (testnet only, key, no transfers); exchange.py:168-195 and rounding.py:69-86 (IOC/reduce-only/rounding); exchange.py:197-208,256-263 and trader.py:131-151 (confirmations, tid idempotency); trader.py:153-176 (backstop, residue: R5, R7); trader.py:178-196,256-276 (lock, stop recheck: R3); trader.py:280-314 and exchange.py:220-249 (kill switch, flatten: R8); run.py:50-73,224-239 (live startup safety); reconcile.py:25-75 (R4); .env.example:18-21 and policy.py:55-66 (size, rate: R1, R2).

## Plumbing
tests/fake_hl.py; exchange.py:63-98; paper.py refactor; run.py:82-180,187-210; .pipeline/hl-facts.md; pyproject.toml/uv.lock (hyperliquid-python-sdk).

## Could be deleted
exchange.py:253-254 Exchange.position(); trader.py:350 second flatten after halt (make conditional).
