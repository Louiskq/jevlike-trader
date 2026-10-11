# M8 review (reviewer subagent, condensed from its report)

Head 2c1e076. 836 tests pass, ruff clean. Mutations run in a scratch copy. Taker unchanged apart from an extra `strategy` event and a `"maker": None` summary key.

## Findings
R1 [must-fix] maker.py:170 (trader.py:141, maker.py:220, 227) — our own quote still leaks into the model input and labels: `_mids` and `_recent` store the raw mid, so `returns_bps`, `sampled_mids`, `recent_mids` include our order; and when `_quote == UNKNOWN`, `_seen()` returns the raw book, so `_decide`, `_mark_scored` and `_resolve_outcomes` use a mid that includes our order. Probe: market 65000/65003, our buy at 65001: model sees mid 65001.5 but recent mids 65002.0 and a fake return whose sign follows its own previous side; after a rejected/failed/unclear modify the stored mid is 65002.0 and a static book becomes a "down" label against our side (if the real batchModify reply is `{"type":"default"}`, after every modify). Test: static market with our quote as the touch: model input shows no change; quote UNKNOWN: stored mid still 65001.5.
R2 [must-fix] maker.py:421-423 (and 358, uncounted flatten IOC) — the 20-actions/min hard constraint can be broken: the periodic stray cancel calls `_note_action()` without `_room()`, using the reserved slot; the stop cancel is never blocked; the flatten IOC is not counted. Probe: 22 venue order actions in 20 s, counter 21. Test: window at 19, stray order, 15 s reconcile, then shutdown: at most 20 order/modify/bulk_cancel in 60 s (or defer the stray cancel when full).
R3 [nit] paper.py:122 (maker.py:375, 277-280) — paper misses fills from prints that went through the old quote just before a modify (tape matched only at the start of on_book; prints between match and modify are compared only with the new quote and skipped). Those are adverse-selection fills, so paper looks better than it is. Test: print through the old price after on_book starts, model flips side, print fills the old quote.
R4 [nit] feed.py:226-228, maker.py:394-399, 409-411 — rate-limit handling covers only quote actions; a 429 on the l2Book poll, open_orders, userFills or userRateLimit is only logged. Test: poll_books fetch raising HTTP 429 -> maker cancels and holds rate_paused.
R5 [nit] config.py:179-182 — MAKER_DECIDER_DEADLINE_S and MAKER_SIZE_USD are validated even for STRATEGY=taker, so a leftover bad value stops the taker starting. Test: Config.from_env({"MAKER_DECIDER_DEADLINE_S": "0"}) succeeds for the taker.

## Weak tests
- test_decision_and_outcome_mids_exclude_our_own_quote: passes with R1 (only checks stored mids while the quote is known).
- test_scores_only_decisions_on_a_changed_book, test_held_decisions_without_p_up_do_not_reset_the_scored_book: paper only (seen is book); pass if scoring keyed on the raw book (B3 survives).
- test_actions_capped_at_20_per_rolling_minute, test_halt_cancel_is_never_blocked_by_the_cap: pass if cancels were not counted (B1, B2 survive); use no_polls so the stray cancel never runs in a full window.
- test_paper_print_through_the_quote_is_booked_by_the_trader: passes with R3.
- test_taker_is_the_default_and_unchanged: shallow; the real guarantee is the unchanged taker suite.

## Break-it candidates
B1 remove `_note_action()` maker.py:358 (survives). B2 remove `_note_action()` maker.py:423 (survives). B3 `_mark_scored(..., book)` maker.py:234 (survives). B4 `mid = book.mid` maker.py:184 (caught). B5 `<=` at maker.py:341 (not run; expected caught).

## Reading list
policy.py:72-106; rounding.py:89-110; maker.py:247-289, 291-312 + exchange.py:119-160; maker.py:113-131, 168-176, 209-238 (own order removal, R1); maker.py:181-201 (scoring); maker.py:337-366, 403-440 (cap, rate pause); maker.py:444-466 (cancel before flatten); paper.py:83-143; feed.py:209-236; config.py:139-142 + trader.py:352-378.

## Plumbing
feed.py:248-377 (trades WS, tape); prompt.py:46-123 (maker prompt draft, the user's decision); run.py wiring; server.py, static/*; reconcile.py; soak.py; tests/fake_hl.py, fixtures (placeholders; public trade hashes).
