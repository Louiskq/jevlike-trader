# M8 plan: maker strategy (`STRATEGY=maker`), issue #16

Plan only. Sources: SPEC.md "Maker strategy (milestone 8)", Hard constraints, "Milestone 8";
`.pipeline/hl-facts.md`; hyperliquid-python-sdk 0.24.0 as installed in `.venv`; reference
jarrodwatts/jev-trader `src/trader.ts`, `src/model.ts`, `src/market.ts`, `src/book.ts`.
Taker code paths and their tests stay as they are. Testnet only.

## 1. Architecture

- **New module `src/jevhl/maker.py`: `class MakerTrader(Trader)`.** Not branches in `trader.py`
  (619 lines, already carries the taker's exits, rate guard and M6/M7 logic). A subclass reuses
  everything shared without copying it: `run()`, `on_book()` housekeeping, `_resolve_outcomes`,
  `_apply` (tid-idempotent booking), `_order_failed`, `_risk`, `check_stale`, `_halt`, `_pause`,
  `_settle`, `_flatten` with M6 retry, `shutdown`, `_ask` (deadline, abandoned thread, abort).
- **The maker overrides only:** `_decide` (maker input, quote), `_poll_fills` (maker fill
  sources, run every tick), `_cover_residue` (no-op: no time exits, spec assumption), `_flatten`
  (clear the paper quote first, reset quote tracking after), `_is_flat` (resume also needs zero
  open orders on the coin).
- **One small refactor in `trader.py`:** the last line of `Trader._ask` (`decide(...)`) moves into
  a hook `_policy(p_up, inp, ts, latency_ms) -> Decision`. The taker's version is that same call,
  so taker behaviour and tests do not change. `MakerTrader._policy` calls `policy.maker_side`.
- **Shared by both strategies:** feed parsing, model/decider HTTP contract, ledger, store,
  kill switch, M6 flatten retry, M7 pause/resume, reconcile, dashboard.
- **Main data flow:** `poll_books()` yields one REST `Book` about every second →
  `Trader.run` → `MakerTrader.on_book` (inherited housekeeping) → `_poll_fills` (fills) →
  `_decide` → `_quote` (keep / modify / place) under `self._lock`.
- **Trades:** a background task fills a `TradeTape` from the WS `trades` subscription. The
  maker reads it for the prompt and for paper fills.
- **Where `STRATEGY` takes effect:** `run.dry_run` builds `MakerTrader` when
  `cfg.strategy == "maker"`. `run.main` then opens `poll_books` plus `stream_trades` in place of
  `stream_books`.

## 2. Data

- **REST book (`feed.py`).**
  - `fetch_l2book(coin) -> dict`: urllib POST `{"type":"l2Book","coin":coin}` to `INFO_URL`,
    timeout 2 s. Blocking, so it is called through `asyncio.to_thread`.
  - `parse_l2book(data, coin, recv_ts, levels=20) -> Book`: the body of `parse_message` after the
    channel check moves into a shared `_parse_book_data(data, coin, recv_ts, levels)`. The WS path
    keeps `TOP_LEVELS=10`, so the taker sees no change. The maker keeps all 20 REST levels; depth
    bands need them (20 BTC levels cover about 18 bps).
  - `Book` gets an optional `read_ms: float | None = None`, used for the latency log.
  - `poll_books(coin, clock, *, every_s=1.0, fetch=fetch_l2book, sleep, on_event)` is an async
    generator on a fixed schedule with at most one request in flight. An overrunning fetch skips
    the missed slot instead of bursting, so it never makes more than one read per second (Hard
    constraint). A failed fetch or parse is logged as `rest_error` and yields nothing.
- **WS trades (`feed.py`).**
  - `parse_trades(raw, coin, recv_ts) -> list[Print] | None`. Channel `trades`; rows
    `{coin, side "B"/"A", px, sz, time, tid, hash, users}`. `B` means the taker bought. Uses the
    same strict `_num` checks (NaN or inf raises `ParseError`); other coins and channels give
    `None`.
  - `Print(ts, recv_ts, side, px, sz, tid)`.
  - `stream_trades(coin, clock, ...)` uses the same connect, ping, backoff and `on_event` pattern
    as `stream_books`, without the "a book must arrive" rule, because prints are bursty (35 in
    40 s). Liveness is judged by ping and pong only. One socket, one subscription.
  - `TradeTape`: a bounded deque (last 600 s, max 5000 prints) deduped on `tid`. Methods
    `add(prints)`, `since(recv_ts)`, `flow(window_s, now)` (count, buy and sell size, net, vwap,
    last px and side), `recent(n)`.
  - Fixtures, captured read-only from the public testnet WS/REST during the build:
    `tests/fixtures/ws_trades_btc.json` and `tests/fixtures/rest_l2book_btc.json`.
- **Staleness in maker mode.**
  - Kill-switch stale means no successful REST snapshot for `stale_s` (15 s, about 15 failed
    polls). This is unchanged code: `on_book` feeds `KillSwitch.on_book`.
  - Per decision, `_decide` already holds `stale` past `stale_s`. Before sending a quote action
    the maker also skips (keeps the current quote, logs `quote_skipped: old book`) if the book is
    older than `MAKER_BOOK_MAX_AGE_S = 3.0` (a constant).
  - A trades-socket outage is logged (`disconnect` events) and shown. Proposed: it is not a kill
    trigger (open question Q6).
- **M7 pause and resume.** These apply as they are. `_pause` calls `_flatten`, and the live
  `Exchange.flatten` already runs `cancel_all()` before closing. The maker's `_flatten` override
  also clears the paper quote (paper `_flatten_once` returns early when the ledger is flat, so it
  would never cancel). `_settle` gaps are now about 1 s, well under `resume_max_gap_s` 8 s.
  `MakerTrader._is_flat` also needs `open_quotes()` to be empty (live) or no paper quote.
  Otherwise it cancels and the resume is blocked, as for a position.
- **M6 flatten retry.** Unchanged. Each attempt cancels all before closing. If a flatten gives
  up, the quote tracking is set to `unknown`, and the next quote first reconciles against open
  orders.

## 3. Quote logic

- **Pricing (`rounding.py`).**
  - `tick_size(px, meta) -> Decimal` uses the same decimals rule as `round_price` (5 sig figs,
    at most `6 - szDecimals` decimals, integers always allowed). BTC's tick is $1.
  - `maker_price(side, best_bid, best_ask, meta) -> Decimal`:
    - Buy: `best_bid + tick(best_bid)`. If that is `>= best_ask`, use `best_bid` (one-tick
      spread). Round down.
    - Sell: mirror image (`best_ask - tick`, falling back to `best_ask`). Round up.
    - Asserts buy `< best_ask` and sell `> best_bid`. Tests cover the decade boundary
      (9.9999/10.001) and a coin with `szDecimals` 4.
- **Own order excluded (non-obvious, from the reference).** Live, our quote is the new touch in
  the next REST book. Read naively, every second would step another tick inside and walk to the
  opposite touch. `without_own(book, quote) -> Book` subtracts our remaining size at our price
  level (it drops the level if the remainder is under a lot). It is used for both pricing and the
  prompt. Paper mode is a no-op.
- **Side (`policy.py`).** `maker_side(p_up, position, mid, size_usd, cfg) -> (side, reason)`.
  - `p_up >= 0.5` means buy, otherwise sell.
  - A side is allowed if `abs(pos_usd ± size_usd) <= max_position_usd + EPS`, or if it reduces
    `abs(pos)`. The second clause matters when the position is already over the cap (a UI trade,
    flooring drift), so the reducing side stays allowed.
  - The wanted side blocked and the other allowed gives `reason="capped"` (new entry in `REASONS`).
    Both blocked cannot happen once config checks `MAKER_SIZE_USD <= MAX_POSITION_USD`.
  - Late or invalid uses the existing `hold("late"|"invalid")`, and the quote stays as it is.
  - Stored `gate` is 0.5 and `size_usd` is `MAKER_SIZE_USD`.
- **Size.** `order_size(MAKER_SIZE_USD, quote_px, meta, min_notional)` uses existing code and
  gives 0.00012 BTC at 85.5k. It is recomputed only when the price or side changes.
- **Replace only on change.** Tracking is `self._quote = Quote(oid, side, px, sz, remaining,
  placed_ts)`, or `None`, or `UNKNOWN`.
  - Same side and same px: no action, so the queue place is kept. This also holds after a
    partial fill: the remainder rests.
  - Price or side differs and an order is tracked: one `batchModify` (1 action). The SDK's
    `modify_order(oid, coin, is_buy, sz, px, {"limit":{"tif":"Alo"}})` calls
    `bulk_modify_orders_new` and can change side and price.
  - Nothing tracked: one `order(..., {"limit":{"tif":"Alo"}}, reduce_only=False)` (1 action).
  - `UNKNOWN`: `open_quotes()` first (an info read, not an action). Our order is adopted, and any
    others are cancelled.
- **Live methods in `exchange.py` (blocking, run in a thread).**
  - `place_quote(side, sz, px) -> QuoteAck`, `modify_quote(oid, side, sz, px) -> QuoteAck`,
    `cancel_quote(oid)`, `open_quotes() -> list[dict]` (parsed `open_orders`: oid, side, px,
    remaining sz) and `rate_budget() -> dict` (`info.user_rate_limit`).
  - `parse_quote_response(resp) -> QuoteAck(status, oid)`:
    - `resting{oid}` gives `placed`.
    - An error containing `"Post only order would have immediately matched"` gives `rejected`.
    - "cannot modify / canceled or filled" gives `gone`.
    - A rate-limit error (HTTP 429 `ClientError`, or a status text containing `"Too many"`)
      raises `RateLimited`.
    - Anything else raises `ExchangeError`.
    - A `filled` status (an Alo should never take) is treated as taken: its fills are confirmed
      through `sync_fills` and booked.
    - Exact texts are unverified until the live run; see section 9.
- **Rejection.** A `rejected` place leaves tracking at `None`. The bot logs `quote_rejected`
  (with the bbo text) and re-prices at the next decision (spec). It is not an order error: it
  does not count towards the 3-error kill. A rejected modify sets `UNKNOWN`, because a failed
  modify may or may not leave the original order.
- **Gone.** On a `gone` modify the bot syncs fills now (tid-deduped), clears tracking and places
  at the next decision.
- **Partial fills.** Each booked fill whose oid matches the tracked quote lowers `remaining`.
  When `remaining` drops under a lot, tracking goes to `None`.
- **Reconcile with open orders.** Every `ORDERS_RECONCILE_S = 15` (live), `open_quotes()`:
  - Tracked but missing: sync fills, then `None`.
  - Untracked orders on the coin: cancel them, logged `stray_order`.
  - `remaining` is set from the exchange's `sz`.
  - Startup (`run.prepare_live`, maker only) calls `cancel_all()` before the position check.
- **Action counter.** `self._actions: list[float]`, a rolling 60 s window. Every place, modify and
  cancel counts 1 (a bulk cancel of under 40 orders is weight 1). Flatten IOCs are counted too.
  `MAKER_ACTIONS_PER_MIN = 20` is a constant, not env: it is a Hard constraint.
  - A quote action is allowed only if `len(window) + 1 <= 20 - 1`. One slot is reserved for an
    emergency cancel. Otherwise the bot logs `action_cap` and keeps the current quote.
  - Cancels from stale, kill, shutdown or a stray order are never blocked, as exits are never
    gated in the taker.
- **Rate-limit error.** On `RateLimited` the bot logs `rate_limited`, cancels the tracked quote
  (`cancel_all`), counts it as an order error (`_kill.on_order_result(False)`) and pauses quoting
  for `RATE_PAUSE_S = 60`. Decisions continue and are recorded as hold `rate_paused` (new
  reason). Three in a row trip the existing `order_errors` kill (permanent, flatten). This is
  open question Q5.
- **Address budget.** `rate_budget()` runs every 60 s (live) and is logged as
  `address_budget used/cap`.
- **Every await:** after acquiring the lock and after each executor call, re-check
  `stopped / paused / kill / rate pause` before acting (M2 pattern).

## 4. Fills

- **Live.**
  - Maker fills arrive asynchronously; an Alo ack never carries a fill. `MakerTrader._poll_fills`
    runs `executor.sync_fills()` every `MAKER_FILL_POLL_S = 5` (instead of the taker's 15 s), plus
    on a `gone` modify and on every flatten (inherited).
  - Booking goes through the inherited `_apply`, which is idempotent on `tid` through
    `store.insert_fill`. The fee comes from the exchange row (the maker rate is whatever the
    exchange charged).
  - Weight budget per minute (per IP 1200): l2Book 60×2 + userFillsByTime 12×20 + openOrders
    4×20 + userRateLimit 1×20 + at most 20 actions ≈ 480.
  - The alternative is the WS `userFills` subscription (push, free), with the poll as a backstop
    (open question Q11).
- **Paper.** `PaperExchange` gains `place_quote`, `modify_quote`, `cancel_quote` and
  `match_prints(prints)`, plus `cancel_all` clearing the quote. Taker calls stay as they are.
  - A buy quote at P fills on a print with `side=="A"` (taker sell) and `px <= P`. A sell quote
    fills on `side=="B"` and `px >= P` ("reaches its price", per the spec).
  - Only prints with `recv_ts > placed_ts` count. A modify resets `placed_ts`, because the queue
    place is lost.
  - Fill size is `min(remaining, print.sz)`, rounded to a lot. The price is our quote price.
  - Fee is `MAKER_FEE_RATE = 0.00015` × notional (spec, "until verified"). Tids are negative,
    counting down, as now.
  - `place_quote` rejects a quote that would cross the book it is given, so paper exercises
    rejection too.

## 5. Prompt (DRAFT for the user)

- **`types.py`.** New `MakerInput`, which keeps `ModelInput`'s fields (`MockModel` still works on
  `bids`/`asks`) and adds:
  - `imbalance`: (bid−ask)/(bid+ask) size within 1% of mid.
  - `depth`: `{bps: (bid, ask)}`.
  - `returns_bps`: `{window_s: bps}`.
  - `sampled_mids`.
  - `flow`: `TradeTape.flow`.
  - `prints`: the last 10 as strings.
- **Building it.** `maker.build_input(book, mids_by_time, tape, cfg)` builds the input from the
  book with our own order removed. Returns are computed by time, not by count, because most
  seconds repeat the book.
- **`prompt.py`.** A second block, `# ---- MAKER DRAFT WORDING: edit freely ----`, holds
  constants for every line:
  - market line, imbalance line, depth line (bands default 5/10/20 bps), top-5 levels each side,
    returns (5/15/60/300 s), recent mids (every 5 s over 60 s), trade flow over 60 s, last 10
    prints, and short instructions adapted from Jarrod's `model.ts` (taker flow first, depth
    asymmetry, path).
  - `render_maker_context(inp)` assembles them. The module docstring states that this block is
    the user's modelling decision. The taker constants and `render_context` stay as they are.
- **Decider contract unchanged.** `DeciderModel(url, timeout, render=render_context)` gets a
  `render` parameter. `build_model(cfg)` passes `render_maker_context` when maker. The payload is
  the same `{context, schema: {QUESTION: {"type":"choice","options":["up","down"]}}}`, and
  `p_up = probabilities["up"]` through the existing strict `parse_decision`. The question text
  (the reply's key) stays the 60 s question (Q2).

## 6. Calibration, latency, store

- **Every decision with a `p_up` is scored**, as the spec says. At 1 s cadence about 60
  decisions share each 60 s window, so n is inflated, roughly 5× the taker's per hour. The
  `HORIZONS_S` comment and the dashboard get an overlap note (Q1).
- **Outcome resolution** improves: a book every ~1 s.
- **Load.** About 3 small `pending/resolve` queries per second plus 1 decision insert, which is
  fine for SQLite. Dashboard summaries already run off-loop. `HISTORY_DECISIONS=300` becomes
  5 minutes of chart (Q13).
- **Latency.** `decisions.latency_ms` (model) is as now. The maker also keeps in-memory REST
  read ms (`Book.read_ms`), action round-trip ms and loop ms (book `recv_ts` to action sent). It
  logs one `maker_latency` event per 60 s (n, p50, p90, max for each), and `format_report`
  prints them.
- **Store: no schema change (stays v3).** Quote history goes in `events` (`quote_place`,
  `quote_modify`, `quote_cancel`, `quote_rejected`, `quote_gone`, `stray_order`, `action_cap`,
  `rate_limited`, `address_budget`, `maker_latency`). New reasons `capped` and `rate_paused` need
  no DDL. A `strategy` event is written next to `run_start`. A maker/taker fee split on the
  dashboard would need a `fills.crossed` column (a v4 migration); it is not planned (Q8).
- **`soak_report(store, strategy)`.** In maker mode only a 100% hold rate warns; 0% is
  expected.

## 7. Dashboard and reconcile

- **Live view.** `run.trader_live` adds `"maker": trader.maker_view()` when it is a
  `MakerTrader`:
  - `quote {side, px, sz, remaining, oid, age_s}` or `null`/`"unknown"`
  - `actions_last_min` / 20
  - `rate_paused_until`
  - `budget {used, cap}`
  - last `capped` flag
- **Server and page.** `server.build_summary` passes `live.get("maker")` through.
  `static/app.js` renders a "Resting quote" panel and an action counter when it is present, with
  a mid-chart marker at the quote price. Taker pages look the same.
- **Fees.** Cumulative fees are shown as now (maker fees are just smaller rows).
- **Reconcile (`reconcile.py`).** Fills are already compared by tid, including the exact fee.
  New: a maker/taker split from the exchange's own `crossed` field (count, fees, effective bps).
  This verifies the 0.015% assumption. It also prints the open orders on the coin (should be 0
  after a run).

## 8. Tests (written first, each confirmed failing)

- **Quote pricing (`test_rounding.py`, `test_maker.py`):**
  - `test_buy_quote_is_one_tick_above_best_bid`
  - `test_sell_quote_is_one_tick_below_best_ask`
  - `test_one_tick_spread_quotes_at_the_touch[buy|sell]`
  - `test_quote_price_at_decade_boundary_never_crosses`
  - `test_quote_price_for_coin_with_sz_decimals_4`
  - `test_random_books_never_produce_a_crossing_quote`
  - `test_own_resting_order_is_not_treated_as_the_touch`
- **Replace only on change:**
  - `test_unchanged_side_and_price_sends_no_action`
  - `test_price_change_sends_one_modify_not_cancel_and_place`
  - `test_side_change_is_one_modify`
  - `test_no_tracked_order_places_one_alo`
  - `test_order_sent_is_post_only_alo`
- **Cap forcing the other side:**
  - `test_cap_blocks_buy_so_quote_goes_on_sell_logged_capped`
  - `test_reducing_side_allowed_when_position_is_over_cap`
  - `test_boundary_cap_uses_non_float_exact_values` (e.g. $15 cap, $11.37 position)
  - `test_config_rejects_maker_size_above_cap_or_below_minimum`
  - `test_config_strategy_values`
- **Post-only rejection:**
  - `test_parse_post_only_rejection`
  - `test_rejected_quote_is_logged_not_counted_as_order_error`
  - `test_three_rejections_do_not_trip_the_kill_switch`
  - `test_rejected_quote_is_repriced_at_next_decision`
  - `test_rejected_modify_reconciles_open_orders`
  - `test_paper_rejects_a_crossing_quote`
- **Partial fills:**
  - `test_partial_fill_booked_once_and_remaining_reduced`
  - `test_partial_fill_same_price_keeps_remainder_resting`
  - `test_paper_print_smaller_than_quote_fills_partially`
  - `test_fully_filled_quote_is_cleared_and_replaced`
  - `test_modify_of_a_filled_order_syncs_fills_without_double_booking`
  - `test_open_orders_reconcile_clears_vanished_and_cancels_stray`
- **Cancel-all on stale, kill and shutdown:**
  - `test_stale_pause_cancels_resting_quote_before_closing[paper|live]`
  - `test_daily_loss_kill_cancels_quote`
  - `test_order_error_kill_cancels_quote`
  - `test_shutdown_cancels_resting_quote`
  - `test_quote_waiting_for_lock_is_not_sent_after_halt_or_pause`
  - `test_no_quote_while_paused_decisions_held_paused`
  - `test_resume_blocked_while_an_order_still_rests`
  - `test_late_answer_leaves_quote_untouched`
  - `test_invalid_answer_leaves_quote_untouched`
- **Order-action cap:**
  - `test_actions_capped_at_20_per_rolling_minute`
  - `test_action_window_rolls_off_after_60_s` (boundary at 60.37 s)
  - `test_halt_cancel_is_never_blocked_by_the_cap`
  - `test_rate_limit_error_cancels_and_pauses_quoting`
  - `test_quoting_resumes_after_rate_pause`
  - `test_three_rate_limit_errors_trip_order_error_kill`
  - `test_http_429_and_too_many_text_both_detected`
  - `test_address_budget_logged_every_minute`
- **Data:**
  - `test_parse_trades_fixture`
  - `test_parse_trades_rejects_nan_and_bad_side`
  - `test_parse_trades_ignores_other_coins`
  - `test_parse_rest_l2book_fixture_keeps_20_levels`
  - `test_ws_l2book_still_truncates_to_10` (taker unchanged)
  - `test_poll_books_never_two_requests_in_flight_or_faster_than_1_s`
  - `test_rest_error_logged_and_no_book_yielded`
  - `test_stream_trades_reconnects_with_backoff`
  - `test_trade_tape_flow_window_and_dedupe_on_tid`
- **Fills:**
  - `test_buy_quote_fills_on_sell_print_at_or_below_price`
  - `test_same_side_print_does_not_fill`
  - `test_print_before_placement_does_not_fill`
  - `test_paper_maker_fee_rate`
  - `test_live_maker_fills_booked_from_poll_idempotent_on_tid`
  - `test_live_fill_poll_runs_every_5_s_not_every_tick`
- **Prompt:**
  - `test_maker_prompt_has_every_section`
  - `test_maker_prompt_question_is_the_schema_key`
  - `test_imbalance_and_depth_band_maths`
  - `test_returns_windows_by_time_not_count`
  - `test_decider_uses_maker_renderer_when_strategy_maker`
  - The existing taker prompt tests are kept as they are.
- **Run, dashboard, reconcile and soak:**
  - `test_maker_dry_run_end_to_end_with_fake_feeds`
  - `test_taker_is_the_default_and_unchanged`
  - `test_prepare_live_maker_cancels_leftover_orders`
  - `test_summary_carries_quote_and_action_counter`
  - dashboard render test for the quote panel (node harness)
  - `test_reconcile_reports_maker_taker_split`
  - `test_maker_zero_hold_rate_is_not_a_warning`
- **Fakes.** `tests/fake_hl.py`:
  - Alo orders rest (they appear in `open_orders` with `limitPx`, `sz`, `origSz`, side B/A) or
    are rejected with the post-only text if they would cross.
  - `bulk_modify_orders_new`, with a knob for "new oid on modify" and the error for a gone oid.
  - `taker_hits(oid, sz)`, which records a maker fill (`crossed: False`, maker fee).
  - `l2_snapshot`, `user_rate_limit`, `rate_limit_next` (429 `ClientError` or status text).
  - Fake async iterators for REST books and trades.
- **Break-it candidates:**
  - quote at the touch instead of inside (off by one tick)
  - own order not excluded
  - replacing on every decision
  - cancel+place instead of modify
  - cap check without the "reducing is allowed" clause
  - post-only rejection counted as an order error
  - action window never decays, or counts only places
  - halt cancel blocked by the cap
  - paper fill on a same-side print, on a pre-placement print, or above the print's size
  - late answer cancelling the quote
  - fill double-booked across poll and gone-sync
  - quote sent after a pause while waiting on the lock
  - resume with an order resting
  - two REST polls in flight
  - trades parser accepting NaN

## 9. Commits, risks, open questions

**Commit plan (5):**
1. Maker building blocks, with tests:
   - config `STRATEGY` (taker|maker, default taker) and `MAKER_SIZE_USD` (11; checked
     `>= min_notional` and `<= MAX_POSITION_USD`)
   - `rounding.tick_size` and `maker_price`
   - feed: REST l2Book, trades parsing, `TradeTape`, `poll_books`, `stream_trades`
   - fixtures
2. Venue support for post-only quotes, with tests:
   - `Exchange` place/modify/cancel/open_quotes/rate_budget
   - response parsing, `RateLimited`
   - paper maker quotes and print fills
   - `FakeHL` Alo/modify/maker fills
3. Maker prompt draft and decider renderer: `MakerInput`, `render_maker_context`,
   `DeciderModel(render=)`, `build_model`.
4. The maker loop, with tests:
   - `maker.py` `MakerTrader`
   - the `Trader._policy` hook
   - `policy.maker_side`
   - reasons `capped`/`rate_paused`
   - action cap, rate pause, reconcile, cancel on halt
5. Run wiring, dashboard and reconcile:
   - `run.py` strategy branch (feeds, `prepare_live` cancel, report)
   - live view and quote panel
   - reconcile maker/taker split
   - soak warning

**Risks:**
- Modify semantics on Hyperliquid are unverified: the response shape (statuses or `default`),
  whether the oid changes, and whether a failed modify leaves the original. The design survives
  either way (`UNKNOWN` plus open-orders reconcile), but the first live minutes must check.
- Mock-model noise near 0.5 flips the side often. The 20-action cap will bind and leave stale
  quotes on the wrong side for part of each minute.
- At $11 against a $15 cap the position just oscillates 0 ↔ ±$11.
- Paper fills are optimistic when we join the touch (queue ahead ignored), per the spec's
  "reaches its price".
- An address-limited account (10000 buffer, +1 per USDC) cannot be cured by a 60 s pause. The
  3-error kill bounds it.

**Open questions for the user (not decided in this plan):**
- Q1. Score every 1 s decision (spec, heavily overlapping) or only decisions on a changed book /
  one per 5 s? Default in the plan: every decision, with an overlap note.
- Q2. Maker question horizon: keep the 60 s question (same contract and calibration) or ask about
  a shorter horizon? Modelling call. Default: 60 s.
- Q3. Prompt contents: depth bands (5/10/20 bps, given 20 REST levels), return windows
  (5/15/60/300 s), flow window 60 s, 10 prints. Should it show position or which sides are
  allowed (Jarrod shows `allowed`)? Default: no.
- Q4. Decider deadline in maker mode: 2 s means some ticks are skipped and quotes go on books up
  to 2 s old. Use 1 s for maker? Default: unchanged `DECIDER_DEADLINE_S`.
- Q5. Rate-limit handling: 60 s quote pause, counted as an order error (3 trips the permanent
  kill), no flatten. OK?
- Q6. Trades WS down: only log and show (default), or treat it as stale after `stale_s` (pause)?
  Paper fills and flow features are blind while it is down.
- Q7. Paper fills: keep "print reaches our price" for quotes that join the touch, or require a
  print through the price in that case?
- Q8. A maker/taker fee split on the dashboard needs a `fills.crossed` column (v4 migration).
  Default: no; reconcile prints the split from exchange data.
- Q9. Action cap: one slot is reserved for a cancel, and halt cancels may exceed 20 in a rare
  minute. OK?
- Q10. First live maker run at `MAX_POSITION_USD=15`, `MAKER_SIZE_USD=11`: OK?
- Q11. Learn live fills by a 5 s `userFillsByTime` poll (default, 240 weight/min) or by the WS
  `userFills` push with a poll backstop?
- Q12. Should the bot refuse a database used by the other strategy? Default: log the strategy and
  advise a separate `--db`.
- Q13. Dashboard history of 300 decisions is 5 minutes at 1 s. Raise it for maker?

**Only verifiable on live testnet (user runs with `.env`):**
- the Alo rejection text
- the modify response shape and oid behaviour, the gone-order error text, and whether modify can
  change side
- the rate-limit error forms
- the maker fee rate and `crossed:false` on maker fills
- the `userRateLimit` response shape
- that 1 s REST l2Book reads run for an hour without 429
- resting orders visible in the testnet UI
- the kill switch and Ctrl-C cancelling them
- the 1-hour run reconciling (`python -m jevhl.reconcile`)
- 30-minute maker dry run: public data only, no key
