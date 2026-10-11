# PIPELINE

- Phase: 4-5 (Milestone loop)
- Repo: https://github.com/Louiskq/jevlike-trader (private), main
- Rough-spec commit: 71822c4
- Final-spec commit: b279a4b
- Current milestone: 9 (maker side changes), issue #18, branch fix/m9-side-change
- Next action: M9 build, PR, review; then rerun the 1 h live maker run on the VM; then step 6 evidence and step 7 README
- Waiting on: user merge of PR #19

## Milestones
| # | Title | Issue | Branch/PR | Status |
|---|---|---|---|---|
| 1 | Foundations and offline core | #1 | PR #6 + follow-up PR #7 | merged, v0.1 |
| 2 | Live data and dry run | #2 | PR #8 | merged, v0.2 |
| 3 | Dashboard | #3 | PR #9 | merged, v0.3 |
| 4 | Testnet live orders | #4 | PR #10 | merged, v0.4; live acceptance pending |
| 5 | Decider backend and soak | #5 | PR #11 | merged, v0.5; soak pending |
| 6 | Flatten retry after network loss | #12 | PR #13 | merged, v0.6; live check pending |
| 7 | Recover from a stalled feed | #14 | PR #15 | merged, v0.7; live check pending |
| 8 | Maker strategy | #16 | PR #17 | merged, v0.8; live maker run pending |
| 9 | Maker side changes cancel and re-place | #18 | PR #19 | ready for merge |

## Notes for next build
- User has no testnet account/API wallet yet; needed before M4 (walk them through, they do it).
- User chose no CLAUDE.md. Safety rules live in SPEC.md.
- User override (2026-10-04): NO Co-Authored-By trailer on new commits or PR bodies (includeCoAuthoredBy=false). Pass this to builder. Earlier commits keep theirs; history not rewritten.
- DECIDED by user (2026-10-04, "yes do this"): add multi-horizon labelling to M2. For every decision record the mid at 30 s, 60 s and 300 s and a label per horizon (ties dropped per horizon). Trading, the gate and exits still use HORIZON_S=60; the dashboard headline calibration/Brier is 60 s, with 30 s and 300 s shown alongside. Needed at M2 kickoff:
  - FIRST commit on the M2 branch: amend SPEC.md (Decisions table: horizon row, Milestone 2 and 3 text) and DECISIONS.md, as its own spec-change commit.
  - Store: replace single mid_future/label columns with an `outcomes(decision_id, horizon_s, mid_future, label)` table (schema v3, no data to migrate yet); `scored()` and calibration take a horizon argument.
  - Brief the builder in plan mode; note overlap caveat (longer horizons overlap more) for the README.
- M2: build KillSwitch with day_start_pnl computed from the store (cumulative realized+unrealized-fees at 00:00 UTC); insert_fill needs exchange tid; insert_decision nulls out-of-range p_up.
- Patterns from M2 review (check these in every later build):
  - After any await, re-check stop/kill state before acting on the result; shared state can change while a call is in flight.
  - Compare float positions against a lot-size epsilon, never against 0; snap residue to exactly flat and round to the nearest lot before flooring.
  - A stop signal must wake a loop that is waiting on I/O, not only one that is receiving data.
  - A timed-out worker thread keeps running: track it and block new calls until it finishes.
  - Every retry, reset and backoff branch needs a test that fails when the branch is deleted.
- Patterns from M1 review (check these in every later build):
  - Parse numeric input with isfinite checks; NaN passes every comparison silently (guards, limits, deadlines).
  - Never let an out-of-range model output reach storage or scoring.
  - Key idempotent inserts on the exchange's own unique id, not on field combinations.
  - State that survives restarts (daily-loss baseline, fills) must be rebuilt from the store, and reset must not forgive it.
  - Boundary tests need non-float-exact values (0.67, not 0.6).
  - Every deliberate break-it bug must be caught by a named test; add one if not.
- SPEC assumptions to honour (see spec e558bd4): testnet only, no mainnet code; limits default $500/$100 configurable; opposite signal while open = hold; linear sizing $10 at gate to cap at p=0.80; 3 consecutive errors; 15 s stale; UTC-day loss.

## Log
- 2026-10-03: problem, spec interview, spec approved, repo setup done; repo created (private); 5 milestone issues created.
- 2026-10-03: M1 mode = Plan first; branch feat/m1-offline-core created.
- 2026-10-03: M1 plan saved (.pipeline/m1-plan.md); HL rounding rules verified in docs. At plan gate.
- 2026-10-04: user: will never trade real money -> spec amended (e558bd4). Plan approved with amendments (.pipeline/m1-plan.md). Starting M1 build.
- 2026-10-04: M1 built (4 commits, 107 tests); verified by orchestrator.
- 2026-10-04: pushed branch, draft PR #6.
- 2026-10-04: reviewer + break-it done (.pipeline/m1-review.md, m1-breakit.md): 4 must-fix (R1-R4), B1 uncaught.
- 2026-10-04: R1 fixed a7f596e.
- 2026-10-04: R2 fixed a987589 (builder did not separately confirm calibration-reject tests failed pre-fix).
- 2026-10-04: R3 fixed 491f527.
- 2026-10-04: R4 fixed 222f9ad. M2 note: pass day_start_pnl (cumulative P&L at 00:00 UTC) to KillSwitch at startup.
- 2026-10-04: B1 test gap closed 615d979 (3 failures with bug planted); pushed. At review gate.
- 2026-10-04: user asked includeCoAuthoredBy=false; set in .claude/settings.local.json (gitignored); no co-author lines from now on (overrides pipeline default).
- 2026-10-04: review gate done (nits left, no user catches); inline review posted; user decisions recorded in DECISIONS.md; PR description drafted.
- 2026-10-04: PR description approved and applied; patterns added; records commit next.
- 2026-10-04: user merged PR #6 at 15:17 UTC with head 3e63a40, before fixes reached main. Fixes + records are only on feat/m1-offline-core (6 commits ahead). Not merged by orchestrator.
- 2026-10-04: follow-up PR #7 opened, ready, CI checked.
- 2026-10-04: user approved multi-horizon labelling (30/60/300 s) for M2; recorded in notes, spec edit to be first commit on M2 branch.
- 2026-10-04: PR #7 merged (0164fad); main verified (137 tests); v0.1 released; M1 done.
- 2026-10-04: orchestrator pushed 9995d6d (M1 records) directly to main by mistake; disclosed to user, not reverted.
- 2026-10-04: M2 mode = Normal; branch feat/m2-live-dry-run; spec-change commit 6a52082 (multi-horizon); builder launched.
- 2026-10-04: M2 built (4 commits, 240 tests); verified by orchestrator (tests, ruff check, 40 s live smoke). Pushed, draft PR opened.
- 2026-10-04: M2 reviewer + break-it done (.pipeline/m2-review.md, m2-breakit.md): 3 must-fix (R1-R3), 3 nits (R4-R6), B1-B4 uncaught.
- 2026-10-04: R1 f341991, R2 2fd8b9f, R3 d42e440, B1-B4 tests 859e87f (all mutants now caught); 247 tests; pushed. At review gate. Builder flagged an unexplained 1e-5 (one-lot) leftover position case (entries p=0.65 then 0.6, both exited).
- 2026-10-04: user chose to fix R4-R6 and investigate residue. R4 243845b; residue was a real bug (paper close floored 0.00015999.. to 0.00015) fixed a48d328; R5+R6 6754608. 249 tests; pushed. Still at review gate (user has not yet said done).
- 2026-10-04: review gate done (user: 'seems good to me', no catches); inline review posted (10 comments); decisions gate: all 4 kept, user rationale 'no reason'; DECISIONS.md and PR description drafted.
- 2026-10-04: PR description approved and applied.
- 2026-10-04: 30-min dry run done (340 decisions, 0 late, 2 clean server-side reconnects, kill none); result added to PR body, .pipeline/dryrun/m2-30min.txt.
- 2026-10-04: PR #8 merged (085432e); main verified (249 tests); v0.2 released; M2 done.
- 2026-10-04: M3 mode = Normal; branch feat/m3-dashboard; builder launched.
- 2026-10-04: M3 built (3 commits, 298 tests, live smoke ok); pushed, draft PR opened. Builder returned no summary file.
- 2026-10-04: M3 reviewer done (.pipeline/m3-review.md): 1 must-fix (R1 loop blocking), 3 nits (R2-R4), B1-B5 uncaught. At review gate.
- 2026-10-04: M3 fixes R1-R4 + B1-B5 tests done (6 commits, 309 tests, verified by orchestrator, pushed). Builder note: R1 builds per client in a thread (no shared cache). User accepted node as test-only dep; R2-R4 fix chosen. Next: inline review, decisions gate.
- 2026-10-04: user: 'no reason' for decisions (node accepted, 3 curves, fix R3/R4). DECISIONS.md, m3-inline.json, m3-pr-description.md drafted. At PR-description approval gate.
- 2026-10-04: PR description approved and applied; inline review posted (5 comments). Records commit next.
- 2026-10-04: records pushed (4944e55), CI green, PR #9 ready for review.
- 2026-10-04: PR #9 merged (2956f6f); main verified (309 tests, ruff clean); v0.3 released; M3 done.
- 2026-10-04: M4 mode = Normal; branch feat/m4-testnet-orders; builder launched (user has no testnet account yet; live acceptance deferred).
- 2026-10-04: M4 built (3 commits, 419 tests, verified by orchestrator); pushed, draft PR #10. Live acceptance blocked on user testnet account. New dep hyperliquid-python-sdk.
- 2026-10-05: M4 reviewer done after 2 failed attempts (network/stall): 2 must-fix, 3 should-fix, 3 nits, B1-B3 uncaught (.pipeline/m4-review.md). User chose R3-R5 fixes and keep SDK dep ('no reason'); orchestrator decided warning-not-refusal and 60 s exit for UI fills.
- 2026-10-05: R1-R5 + B tests done (6 commits, 433 tests, verified, pushed). Manual TODO for user: set MAX_POSITION_USD=15 in .env.example (permission-denied for agents).
- 2026-10-05: PR description approved and applied; inline review posted (5 comments). Records commit next.
- 2026-10-05: records pushed (a7c7a47), CI green, PR #10 ready.
- 2026-10-05: PR #10 merged (e2274a0); main verified (433 tests, ruff clean); v0.4 released; M4 code done, live acceptance pending.
- 2026-10-07: user has testnet funds, .env set (key 66 chars, address ok, MAX_POSITION_USD=15 in .env; .env.example edit unconfirmed). First live run NOT yet done. User paused work until the cluster is back; M5 needs the user's real model contract.
- 2026-10-08: checked jev-class-9b on Isambard (job 7144550, 1 GH200, 2m13s): decider9b_full model COMPLETE, /decide returns probabilities; server-side latency p50 27 ms, max 28.6 ms over 17 requests; p_up mean 0.626 (18/20 above 0.5) with placeholder prompt, i.e. biased up. M5 needs: prompt wording (user's call), tunnel vs run-on-cluster, DeciderModel adapter. Test files in cluster ~/jevtest.
- 2026-10-08: M5 mode = Normal; branch feat/m5-decider-soak; user: draft prompt (labelled DRAFT) and bot runs on laptop via SSH tunnel; builder launched.
- 2026-10-08: M5 built (3 commits, 508 tests, verified); pushed, draft PR opened. Prompt in src/jevhl/prompt.py is a DRAFT for the user to edit.
- 2026-10-08: M5 reviewer done (.pipeline/m5-review.md): 0 must-fix, 4 nits (R1-R4), 4 weak tests, B1/B2(partial)/B4 uncaught. At review gate.
- 2026-10-08: R1-R4 + break-it tests done (5 commits, 525 tests, verified, pushed f0ca131). User confirmed HTTP contract and chose R1-R4. DECISIONS.md, m5-inline.json, m5-pr-description.md drafted. At PR-description gate.
- 2026-10-08: PR description approved and applied; inline review posted (5 comments). User: 'no reason' for decisions. Records commit next.
- 2026-10-08: records pushed (b7a5d1e), CI green, PR #11 ready.
- 2026-10-08: PR #11 merged (a74244a); main verified (525 tests, ruff clean); v0.5 released; M5 code done, live acceptance pending.
- 2026-10-08: UI check of 4 live fills passed (sizes, prices, fees match; UI closed P&L is net of fees). 1 h live run (mock model, $15 cap, hour.db): 695 decisions, 0 late, 48 fills, fees $0.223041, reconcile OK (ledger and exchange closedPnl both -$0.0536, position 0). 5 websocket 'Expired' reconnects, ~1.5 s each; 48 skips all rate limit; RSS 62->38 MB.
- 2026-10-09: loss-limit drill passed (drill.db, MAX_DAILY_LOSS_USD=0.02): kill=daily_loss, flat, reconcile OK (9 fills, fees $0.043794, closedPnl $0.0214 both sides).
- 2026-10-09: accidental network loss in the first drill (DNS failed ~2 min in): stale_book kill fired, no stale trades, but flatten failed twice and left a 0.00017 BTC short open; bot reported 'position UNKNOWN'. User closed it by hand in the UI. KNOWN LIMIT: no flatten retry after the network returns; note in README, ask user whether to fix. Fresh --db after a manual UI trade makes startup refuse (position differs from ledger): by design, reuse the db.
- 2026-10-09: M6 (issue #12) built, reviewed (R1-R5), fixed R1-R4, R5 documented; PR #13 ready; decisions recorded (user: no reason; orchestrator decided fix-before-soak at user's request). Waiting for merge.
- 2026-10-09: PR #13 merged (6506406); main verified (557 tests, ruff clean); v0.6 released.
- 2026-10-09: short-deadline check passed (24/24 late holds, 0 fills). Real-model run (soak.db, job 7192991, tunnel to node address): 2 h of trading, 1423 decisions, latency p50 137 ms p90 407 ms p99 1241 ms, 11 late holds, Brier worse than baseline at all horizons, reconcile OK (114 fills); ended by a stale-book kill at 16:18 UTC (feed went quiet, no disconnect; ping after 20 s > stale 15 s).
- 2026-10-10: M7 (issue #14) built, reviewed (R1-R4), fixed R1, R2, R4; R3 (DECISIONS.md) fixed by orchestrator; PR #15 ready; user: no reason. User wants the bot on their VM, not the MacBook (no reason given).
- 2026-10-10: PR #15 merged (dfce6d5); main verified (642 tests, ruff clean); v0.7 released.
- 2026-10-10: bot moved to the user's VM (Oracle, Ubuntu 24.04 aarch64): 627 tests pass (15 node-only skipped), Linux RSS path works. .env copied by orchestrator at user's request (not opened). VM clifton cert valid to 03:12 UTC. Model job 7232911 (11 h, node address bind). VM smoke: latency p50 41 ms p90 59 ms, 0 late. Overnight live run started 15:19 UTC, 10 h, real model, $15 cap.
- 2026-10-10: USER DECISION: no overnight run for now; a 2 h unattended run is enough ("its fine to not have an overnight run for now. if it works for say 2h its probably good"). Changes the SPEC M5 acceptance (overnight soak) and the PROBLEM.md 'unattended overnight run' criterion: amend SPEC and record in DECISIONS.md on the next branch; README must say the unattended run was 2 h.
- 2026-10-10: 2 h unattended VM run DONE (overnight.db on VM, 15:19-17:21 UTC): 1365 decisions, 0 late, latency p50 41 ms p90 65 ms p99 289 ms max 956 ms; Brier 30/60/300 s 0.268/0.274/0.291 vs baseline 0.250/0.248/0.231 (model worse at all horizons); hold rate 64.9%; 114 fills, fees $0.550461, reconcile OK (closedPnl -$0.0478 both, flat); RSS quarters 70.0/70.3/70.5/70.6 MB (+0.6 MB over 2 h, flattening); 10 routine 'Expired' reconnects, 0 stale pauses; clean stop exit 130; GPU job cancelled, tunnel closed. M5 acceptance (2 h version) met.
- 2026-10-10: M8 spec (f245791 -> rewritten), plan approved with user answers (scoring per changed book, paper fills through price, 1 s maker deadline; other defaults accepted). Built (836 tests, 74 break-it mutants caught); builder hit a usage limit once and was resumed. GitHub GH007 blocked the push (private email); USER chose the noreply address: repo-local user.email set to 102479386+Louiskq@users.noreply.github.com and the 9 unpushed commits re-authored (new hashes: spec 39cf663, fix df3d294, tests 38c2c79, build record 2c1e076). Draft PR #17; reviewer started.
- 2026-10-10: M8 30-min maker paper dry run (pre-review code, mock model) PASSED: 1800 decisions, gap p50 1.00 s, 0 late, 0 errors, 442 quote actions (~14.7/min) with 318 action_cap events (mock flips near 0.5), 50 paper fills, fees $0.0740, REST book p50 228 ms, loop p50 3.5 ms, 2 trades-socket reconnects, clean stop. Review R1-R5: user chose fix all five; builder working.
- 2026-10-11: M8 review fixes (a30df8e..cbc93d8) verified (863 tests, ruff clean), pushed; user: no reason; kept strict cap (18 quotes/min, stop waits); PR #17 ready, review trail posted.
- 2026-10-11: PR #17 merged (e0c79cf); main verified (863 tests, ruff clean); v0.8 released.
- 2026-10-11: VM clifton cert renewed (two device codes expired first; third approved), valid to 05:22 UTC. Stale port-8000 forward from 10-10 lived on in the VM's ssh ControlMaster (pid 729790); replaced with ssh -O cancel/forward. Maker paper smoke with the model: 60 decisions, latency p50 86 ms, 1 late (1 s deadline), 3 quote actions. Live 1 h maker run started 17:30 UTC.
- 2026-10-11: live maker run (maker_live.db on VM) STOPPED at 17:44 UTC after 14 min: kill=order_errors after 3x 'Attempted to modify to invalid new order' on adopted sell oid 62440039299 (after buy fill 17:44:05 at 82947 and a quote_gone). Ended safe: flat, 0 open orders, reconcile OK (6 fills, all maker, fees $0.009694 = 1.50 bps, closedPnl -$0.0101 both). Verified live: Alo resting quotes, post-only rejection text ('Post only order would have immediately matched, bbo was ...'), maker fee 0.015%, kill switch cancels resting orders. Modify latency p50 628 ms. Cause of the invalid modify not yet known (bot does not log the attempted params on error). GPU job cancelled, forward closed.
- 2026-10-11: cause found: all 3 failed modifies flipped sell->buy; no side-changing modify succeeded in the run. Testnet probe (scratch probe_modify.py, one Alo 2% from mid, cancelled): same-side modify ok, side-flip modify -> 'Attempted to modify to invalid new order', original order left resting. User: go ahead with the fix. M9 issue #18, branch fix/m9-side-change, mode Normal; builder launched.
- 2026-10-11: M9 built (4ce087e spec, f862024 fix; 880 tests, 18 break-it mutants caught; verified by orchestrator: 880 pass, ruff clean). Pushed, draft PR #19; reviewer started.
- 2026-10-11: M9 reviewer: first two attempts stalled (watchdog), second resumed and finished (.pipeline/m9-review.md): R1 should-fix (refused same-side modify retried forever), R2/R3 nits; B1-B9 all caught. At review gate.
- 2026-10-11: review gate: user chose fix R1 and R2 (R3 documented as a limit). Builder resumed for the fixes.
- 2026-10-11: R1 d0a1eb3, R2 8149333, R3 doc a1504ca, test 4277e2c; verified (884 tests, ruff clean). Decisions gate: user 'no reason'. DECISIONS.md, PR description, inline review written; records commit next.
