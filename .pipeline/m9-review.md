# M9 review (reviewer subagent; first two attempts stalled, the second was resumed and finished)

## Findings
R1 [should-fix] src/jevhl/maker.py:406-408 — `refused` restores the old quote (`self._quote = prev`) and counts as success (`_kill.on_order_result(True)` at :396). With side changes no longer sent as modifies, `refused` can only come from a same-side modify (cause unmeasured), and every later decision re-sends the identical modify and is refused again. Nothing escalates: no order error, no fallback to cancel+place, no kill.
   Breaks when: HL refuses a same-side modify persistently → the bot keeps an off-price quote indefinitely, burns one action per decision (up to 18/min of the cap), logs `quote_refused` ~1/s, never stops. test_refused_modify_keeps_the_old_quote_and_is_not_an_order_error asserts this loop (4 refused modifies) as intended. Issue says "the next decision re-quotes"; repeating a refused modify is a weak reading.
   Suggested test: two consecutive refused modifies on the same side → next decision sends cancel+place (or logs a distinct escalation), not a third modify.
R2 [nit] src/jevhl/maker.py:380-381 — a successful side-change cancel never calls `_kill.on_order_result(True)`.
   Breaks when: order error, then a side change whose cancel succeeds but whose place is skipped (position changed), then two more errors → kill on "3 in a row" despite an intervening successful order action.
   Suggested test: errors=2, side-change cancel succeeds + place skipped, then one error → not killed.
R3 [nit] src/jevhl/exchange.py:350-353 (unchanged, new path relies on it) — `cancel_oids` checks only top-level `status`; HL's per-order "already canceled, or filled" counts as success. The side change learns of a filled old quote only via `_sync_locked` (maker.py:382-389); if userFills lags, the place is still sent. Harmless (place is on the reducing side) but the docstring's "the place is left to the next decision" depends on fill timing.

No committed secrets/paths/binaries. SPEC conformance otherwise good.

## Weak tests
- test_refused_modify_keeps_the_old_quote_and_is_not_an_order_error — `_kill._errors` starts at 0; passes even if `refused` skipped `on_order_result(True)`. Also enshrines R1's endless retry.
- test_quote_error_events_say_what_was_sent — prefix checks only; no case for the `filled` branch (maker.py:413-414).
- test_paper_side_change_cancels_then_places — paper `_sync_locked` returns early, so the position-changed skip is live-only.

## Break-it candidates (all planted and restored; all caught)
B1 drop position-changed check (1 test); B2 refused clears quote (1); B3 `_room(1)` for flip (1); B4 no fill sync after cancel (2); B5 paper allows flip (1); B6 refused is an order error (1); B7 failed cancel leaves quote tracked (3); B8 drop blocked re-check (1); B9 `flip = False` (11).
Not planted, likely uncaught: refused skipping the kill reset; `filled` event dropping `sent`.

## Reading list
- maker.py:310-359 same side = one modify; side change = cancel then place, both under cap
- maker.py:361-390 the cancel half (failure, 429, fill booked, halt)
- maker.py:81-84, 392-416 refused keeps old quote; error events record what was sent
- maker.py:441-452 cap arithmetic
- exchange.py:121-155 venue answer parsing (refused at 152-153)
- paper.py:94-109 paper refusal
- tests/test_maker.py:210-410 acceptance tests

## Plumbing
types.py docstring; exchange.py docstrings; tests/fake_hl.py refusal; `flip`→`walk` test rewrites; SPEC rows.

## Could be deleted
Nothing.
