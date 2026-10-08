# PIPELINE

- Phase: 4-5 (Milestone loop)
- Repo: https://github.com/Louiskq/jevlike-trader (private), main
- Rough-spec commit: 71822c4
- Final-spec commit: b279a4b
- Current milestone: 5 (Normal mode, branch feat/m5-decider-soak)
- Next action: reviewer on M5 (draft PR)
- Waiting on: nothing

## Milestones
| # | Title | Issue | Branch/PR | Status |
|---|---|---|---|---|
| 1 | Foundations and offline core | #1 | PR #6 + follow-up PR #7 | merged, v0.1 |
| 2 | Live data and dry run | #2 | PR #8 | merged, v0.2 |
| 3 | Dashboard | #3 | PR #9 | merged, v0.3 |
| 4 | Testnet live orders | #4 | PR #10 | merged, v0.4; live acceptance pending |
| 5 | Decider backend and soak | #5 | feat/m5-decider-soak | building |

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
