# M8 build summary (builder subagent, condensed from its report)

Built (issue #16, plan .pipeline/m8-plan.md): STRATEGY=maker next to the unchanged taker (default). REST l2Book about once a second (20 levels, never two in flight, missed slots skipped) plus the WS trades channel on its own socket, kept in a tid-deduped tape. Richer prompt in prompt.py, marked as the assistant's DRAFT for the user to edit. MakerTrader (Trader subclass, src/jevhl/maker.py) keeps one post-only (Alo) quote one tick inside the touch, our own order removed from the book first; replaced only when side or price changes (one modify, or one place if nothing rests). Every stop (stale pause, kill, shutdown) cancels resting orders first and is never blocked by the 20/min action cap. Paper quotes fill only on prints through the price (0.015% maker fee). Dashboard, reconcile (maker/taker split from `crossed`, open orders) and soak report handle the maker. The builder was cut off once by a usage limit and resumed.

Commits: 701bce5 building blocks (config, pricing, REST book, trades feed); fcc523c venue support (Alo, live and paper); 86c7161 prompt DRAFT and renderer; d53862a maker loop; b74a940 run/dashboard/reconcile/soak; 5c0f14c Fix: decision and outcome mids exclude our own quote; 1a39889 test gaps from break-it. No Co-Authored-By.

Deviations from SPEC/plan:
- Labels use the mid without our own quote (5c0f14c; spec silent): live, our quote one tick inside moves the raw mid toward our side, which follows p_up, and would bias calibration.
- Flatten IOCs are not counted in the action window (plan said they would be).
- "Changed book" for scoring = any of the 20 levels (price or size) differs, on the book the model saw; holds without p_up never become the last scored book; scoring state is in memory only (no schema change).
- A gone or rejected modify waits for the next decision instead of re-placing at once.
- While rate-paused the model is not asked (hold `rate_paused`, unscored); budget and open-orders reads skipped while paused.
- Trades-socket events named trades_connect / trades_disconnect / trades_parse_error.
- Paper quote oids share the paper tid counter (oid -2 named two orders in a smoke run).
- Trades fixture has public addresses replaced with placeholders.

Unsure about: partial fill then a modify can briefly exceed the cap by the partial (up to 5 s until the fill poll); with exactly one unknown open order the bot adopts it as its quote (could be a hand-placed UI order); paper fill timing depends on local vs exchange clock; the mock flips side near 0.5 so the cap binds (19 actions, 10 action_cap events in 60 s smoke); prompt wording and feature settings are the user's to edit.

Verification (builder): tests failed before each implementation, except 10 error-branch tests in test_maker.py written after the code and proven by break-it instead; 836 pass (baseline 642), no existing test changed; paper smoke 60 s: 60 decisions, gap p50 1.00 s, 0 late, quote_place 1, quote_modify 18, action_cap 10, cancel at stop, 0 fills, maker REST p50 229 ms; an earlier smoke had a paper sell fill at the maker fee and a cap-forced buy, and shutdown cancelled and flattened; dashboard smoke shows the maker panel; 74 planted bugs, all caught by named tests. Orchestrator re-ran: 836 pass, ruff clean, no co-author trailers, no env/db/settings files, fixture addresses are placeholders.

Cannot verify without live testnet: exact Alo rejection text; batchModify reply shape, oid change, whether a failed/crossing modify leaves the original, the "cannot modify canceled or filled" text; rate-limit error forms and userRateLimit reply shape; maker fee rate and crossed:false on maker fills; an hour of 1/s REST reads without 429; resting orders in the testnet UI and the kill switch / Ctrl-C cancelling them; startup cancel of leftovers; the 1-hour live maker reconcile. The 30-minute maker dry run needs no key.
