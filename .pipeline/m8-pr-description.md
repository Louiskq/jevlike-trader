Closes #16

Milestone 8 adds a maker strategy in the style of jarrodwatts/jev-trader, on Hyperliquid testnet. With `STRATEGY=maker` the bot decides about once a second on a REST book plus the trades feed, always picks a side, and keeps one post-only (`Alo`) quote one tick inside the touch, replaced only when its side or price changes. The taker strategy is unchanged and stays the default. It has passed a 30-minute paper dry run; it has not yet placed a live maker order (see "Not yet verified").

## What's in it
- **Building blocks (b676468):** config (`STRATEGY`, `MAKER_SIZE_USD` $11, `MAKER_DECIDER_DEADLINE_S` 1 s), quote pricing in `rounding.py`, the 1/s REST book poll and the WS trades feed in `feed.py`.
- **Venue (3595458):** post-only quotes, modify and cancel live (`exchange.py`) and in paper (`paper.py`, fills only on a print through the price, 0.015% fee).
- **Prompt (f8106ff):** `src/jevhl/prompt.py`, the assistant's DRAFT for the user to edit.
- **Maker loop (db695cc):** `src/jevhl/maker.py` (`MakerTrader`, built on `Trader`, so the kill switch, flatten retry and stall recovery carry over).
- **Run, dashboard, reconcile, soak (f7799f6).**
- **Fixes:** df3d294 (builder), a30df8e, 77eefcf, 99ad80a, d17858b, 044713d (review); tests 38c2c79, cbc93d8.

## Decisions
Rationale for each, in the user's words: "no reason".
- **A maker strategy as `STRATEGY=maker`, next to the taker:** no reason.
- **About one decision a second, quote replaced only on change:** no reason.
- **A Jarrod-style prompt drafted for the user to edit (still the draft):** no reason.
- **Always quote buy or sell:** no reason.
- **Score only on a changed book; paper fills only through the price; 1 s maker deadline; other plan defaults accepted:** no reason.
- **Fix all five review findings:** no reason.
- **Keep the strict cap (18 quote actions a minute; a stop waits rather than exceeding 20):** no reason.
- **Use the GitHub noreply address for this repo's commits:** no reason.

Decided by the builder, not chosen either way by the user: the model's view and labels exclude our own quote; a book with our orders unknown gives no decision; read rate limits pause quoting but are not order errors; inner flatten retries are not counted against the cap (bounded, documented).

## Caught in review
- **Caught (reviewer):** our own quote leaked into the model's recent mids, returns and, when unknown, labels. Fixed in a30df8e.
- **Caught (reviewer):** the 20-a-minute cap could be broken by a stray cancel, stop cancels and flatten orders. Fixed in 77eefcf.
- **Caught (reviewer):** paper missed fills just before a modify. Fixed in 99ad80a.
- **Caught (reviewer):** read rate limits were only logged. Fixed in d17858b.
- **Caught (reviewer):** maker settings could stop the taker starting. Fixed in 044713d.
- **Caught (reviewer, break-it):** three deliberate bugs passed every test. Tests added in cbc93d8.
- **Caught (builder):** decision mids included our own quote. Fixed in df3d294.
- **Caught (me):** nothing raised by me at the review gate.

## Break-it checks
Builder: 74 planted bugs in the first build, all caught by named tests; 37 more on the review fixes, all caught. Reviewer: B1-B3 survived the first build, now caught.

## Deviations from the spec
None in behaviour. A stop now waits for a free slot rather than exceeding 20 actions a minute (the user chose this at the review gate).

## Evidence so far
30-minute paper dry run (mock model, before the review fixes): 1800 decisions one a second, 0 late, no errors, 442 quote actions (~15 a minute), 50 paper fills, fees $0.074, REST book read p50 228 ms, clean stop.

## Not yet verified (needs live testnet)
Resting maker orders in the testnet UI; the real `Alo` rejection and batchModify replies; real 429 shapes; maker fee and `crossed: false` on maker fills; an hour of 1/s REST reads; the kill switch and Ctrl-C cancelling resting orders; the 1-hour live maker run reconciling.

## Verification
- Reproduce locally: `uv sync --locked && uv run ruff check && uv run pytest` (863 tests, no key or network needed)

https://claude.ai/code/session_01Ua15mU8f7EqK7W91vAtfvo
