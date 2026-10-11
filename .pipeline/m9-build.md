## Built
When the quote's side changes, the maker now cancels the resting quote and places a new post-only order instead of sending a modify, which Hyperliquid refuses. The side change counts as two order actions and is sent only if both fit under the cap. A same-side price change is still one modify. A new "refused" venue answer (`Attempted to modify to invalid new order`) leaves the old quote tracked and does not count as an order error. Every quote error, rejection, gone, unknown and rate-limit event now records what was sent. The paper and fake venues also refuse a side-changing modify, so paper runs take the same path.

## Commits
- 4ce087e Spec: a maker side change is a cancel plus a new post-only order
- f862024 Maker side changes cancel and re-place; a refused modify keeps the quote

## Deviations from SPEC.md
- I edited SPEC.md (the Maker order row, the action-cap assumption, and a new assumption for a refused modify). The builder rules say SPEC.md is the orchestrator's, but the orchestrator asked for this edit. It is its own "Spec:" commit so the user can review it separately.
- Existing tests changed (none weakened):
  - `test_side_change_is_one_modify` is replaced by `test_side_change_cancels_then_places_not_modify`, because the behaviour it pinned is the bug.
  - Several tests used a side flip only to trigger a modify (gone, unclear reply, 429, failed modify, order-error kill) or to produce one action per second for the cap tests. They now use a same-side price change (new helper `walk`), so their counts and assertions are unchanged.
  - Two tests that needed an "unknown" quote with our buy still resting now get it from a failed side-change cancel instead of an unclear modify reply.
  - `test_a_book_rate_limit_stops_a_quote_already_decided` now also asserts that no place was sent, not just no modify.

## Unsure about
- **Skipping the place when the position moved.** After a successful cancel I sync fills (live). If the position changed, or the bot was halted, paused or rate-paused meanwhile, I skip the place (`quote_skipped`) and the next decision re-quotes. This is conservative: a fill of the cancelled quote is on the opposite side, so it can only make the new side more allowed by the position cap. The cost is at most one decision (about 1 s) with no quote after a fill. Placing anyway would also have been safe, with one branch less.
- **Cancel replies.** `cancel_oids` checks only the top-level `status: ok`, not the per-order statuses. The fill sync covers an "already canceled, or filled" order. Any order a cancel missed is cancelled as a stray by the 15 s open-orders check.
- **Repeated refusals.** If Hyperliquid refuses a same-side modify (not seen so far), the bot retries it at every decision, bounded by the action cap, with no fallback to cancel and place. It never trips the kill switch.
- **Extra fill reads.** Each side change adds one `userFillsByTime` read: at most about 9 a minute under the cap. That is my estimate of about 180 extra weight a minute, well under the IP limit. Not measured.
- **Error counter.** A successful side-change cancel does not reset the consecutive order-error counter, the same as other cancels. The place that follows does.
- **More capped decisions.** A side change now costs 2 of the 18 quote slots a minute. A model that flips often will hit `action_cap` more than in M8. The rerun of the live hour will show how often.
- **Flaky test.** `tests/test_feed.py::test_the_book_bound_restarts_at_each_book_not_at_connect` failed once in a full run. It passed alone and in three reruns of test_feed.py, and it is untouched by this change. It looks like a timing flake.
- This file is left uncommitted for the orchestrator.

## Spec traceability
- Side change = cancel + new post-only order, not a modify → src/jevhl/maker.py:330-339 (`_quote_to`), 361-390 (`_cancel_for_side_change`)
- Two actions counted, sent only if both fit, else `action_cap` → src/jevhl/maker.py:330-334, 446-449 (`_room(n)`), 378 (cancel counted), 342 (place counted)
- Cancel fails → order error, quote unknown, no place; 429 → cancel and pause → src/jevhl/maker.py:373-381
- Cancelled order had filled → fills synced, place skipped, re-quoted next decision; halt during the cancel → no place → src/jevhl/maker.py:382-390
- Place after cancel rejected → no quote, re-priced next decision → src/jevhl/maker.py:403-405 (prev None → quote None)
- Refused modify keeps the old quote, not an order error → src/jevhl/exchange.py:152-153; src/jevhl/maker.py:406-408; src/jevhl/types.py:80-85
- Error events record action, oid, side, size, price → src/jevhl/maker.py:81-84 (`_sent`), 343-356, 399-416, 465-475
- Paper refuses a side-changing modify → src/jevhl/paper.py:92-103
- Docstrings corrected → src/jevhl/exchange.py:16-21, 328-333; src/jevhl/maker.py:1-31; src/jevhl/paper.py:9-17
- SPEC Maker order row and assumptions → SPEC.md:37, 47-48
- Tests → tests/test_maker.py:210-410 (new), 767-772 (`walk`); tests/test_maker_venue.py:44-49, 121-126, 285-292

## Verification
- New tests written first. All 18 failed on the pre-change code: 3 in test_maker_venue.py and 15 in test_maker.py.
- The 17:44 sequence, replayed on the old maker code with the new fake venue, produced three `Attempted to modify to invalid new order` order errors and `kill=order_errors`, matching the live run. On the new code it ends with a buy resting and no order errors.
- `uv run pytest -q` → 880 passed (baseline 863).
- `uv run ruff check` → All checks passed.
- Break-it: 18 planted bugs (with bytecode caching off), all caught by named tests:
  - room 1 instead of 2
  - side change by modify
  - failed cancel leaves the quote tracked
  - failed cancel not an order error
  - 429 on the cancel treated as a plain error
  - no fill sync after the cancel
  - no position check
  - no halt check
  - refused sets the quote unknown
  - parser has no refused
  - paper allows a side change
  - cancelled quote still tracked
  - each of the five event-params changes
  - cancel not counted
- Smoke: 60 s paper maker run (`STRATEGY=maker uv run python -m jevhl.run --seconds 60 --db <scratch>`, no key, empty env) → 60 decisions, gap p50 1.00 s, 0 late. There were 8 side changes, each a `quote_cancel` then a `quote_place`. No errors, and the stop cancel ran.

## Review fixes
The user chose R1 and R2. R3 stays a documented limit. Each fix was tested first and checked by planting its bug. These fixes supersede the "Repeated refusals" and "Error counter" items under Unsure about above. Line numbers in the traceability section above moved by a few lines.

Commits:
- d0a1eb3 Fix: after a refused modify the next re-quote is a cancel and a place (R1)
- 8149333 Fix: a successful side-change cancel counts as a good order action (R2)
- a1504ca Doc: a filled old quote is seen only through the fill read (R3)
- 4277e2c Test: the filled-branch quote event says what was sent

Changes:
- **R1.** `Quote.modifiable` (src/jevhl/maker.py:79) is set False on a refused answer (maker.py:416) and kept through the open-orders check (maker.py:589-591). The next re-quote of that order takes the cancel-and-place path, needing room for 2 actions (maker.py:331). The new order is modifiable again. `_cancel_for_side_change` is renamed `_cancel_to_replace` (maker.py:362), since it now serves both cases. Its `quote_cancel` event says "side change" or "modify refused".
- **R2.** A successful cancel calls `self._kill.on_order_result(True)` (maker.py:384).
- **R3.** The `_cancel_to_replace` docstring now says a filled old quote is seen only through the fill read. If the venue's fills lag, the place still goes out, which is harmless because the new quote is on the reducing side (maker.py:369-371).

Tests:
- `test_refused_modify_keeps_the_old_quote_and_is_not_an_order_error` is rewritten. It starts with `_kill._errors = 2`. After one refusal the count is 0. The next decision at a different price sends a cancel then a place, never a second modify, and the new order is then modified normally.
- New: `test_a_refused_quote_stays_unmodifiable_after_the_open_orders_check`
- New: `test_a_refused_quote_needs_room_for_two_actions`
- New: `test_a_good_side_change_cancel_resets_the_order_error_count` (errors at 2, the place is skipped after the cancel, then one order error: not killed)
- New: a `place_filled` case in `test_quote_error_events_say_what_was_sent`

Verification:
- The new R1 and R2 tests failed before their fixes.
- Planted bugs, all caught:
  - R1a: the refused quote stays modifiable
  - R1b: the replace decision ignores `modifiable`
  - R1c: the open-orders check resets `modifiable`
  - R1d: only one slot needed for a refused quote
  - R2: no success recorded for the cancel
  - the filled event without the sent params
- `uv run pytest -q` → 884 passed.
- `uv run ruff check` → All checks passed.
- No Co-Authored-By trailers. Nothing pushed.
