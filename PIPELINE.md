# PIPELINE

- Phase: 4-5 (Milestone loop)
- Repo: https://github.com/Louiskq/jevlike-trader (private), main
- Rough-spec commit: 71822c4
- Final-spec commit: b279a4b
- Current milestone: 1 (plan-first mode, branch feat/m1-offline-core)
- Next action: fix R1-R4 (one commit each), batch-tests for B1, then review gate
- Waiting on: merge gate (PR #6)

## Milestones
| # | Title | Issue | Branch/PR | Status |
|---|---|---|---|---|
| 1 | Foundations and offline core | #1 | feat/m1-offline-core, PR #6 | awaiting merge |
| 2 | Live data and dry run | #2 | | todo |
| 3 | Dashboard | #3 | | todo |
| 4 | Testnet live orders | #4 | | todo |
| 5 | Decider backend and soak | #5 | | todo |

## Notes for next build
- User has no testnet account/API wallet yet; needed before M4 (walk them through, they do it).
- User chose no CLAUDE.md. Safety rules live in SPEC.md.
- User override (2026-10-04): NO Co-Authored-By trailer on new commits or PR bodies (includeCoAuthoredBy=false). Pass this to builder. Earlier commits keep theirs; history not rewritten.
- M2: build KillSwitch with day_start_pnl computed from the store (cumulative realized+unrealized-fees at 00:00 UTC); insert_fill needs exchange tid; insert_decision nulls out-of-range p_up.
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
