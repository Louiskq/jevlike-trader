# Milestone loop (pipeline steps 4–5)

One milestone per pass through this file. Sections A–J run in order. There are three user gates: **mode** (A), **review** (F) and **decisions** (H), plus the **merge** click (I).

Throughout, `<N>` is the milestone number, `<issue>` its GitHub issue number, `<PR>` the PR number, and `<owner>/<repo>` comes from `gh repo view --json nameWithOwner -q .nameWithOwner`.

## A. Kickoff

1. Read the milestone's section of SPEC.md, its issue (`gh issue view <issue>`), and "Notes for next build" in PIPELINE.md.
2. **Gate: mode.** Ask with AskUserQuestion, recommending one based on how risky or vague the milestone looks:
   - **Normal**: builder runs to completion.
   - **Plan first**: builder proposes a plan, the user approves, then it builds. Recommend for risky or vague milestones.
   - **Learn**: builder stops after each commit and you explain it. Recommend for milestones the user mainly wants to learn from.
3. Create the branch: `git checkout main && git pull && git checkout -b feat/m<N>-<short-slug>`.

## B. Build

Invoke the **builder** with: the mode, the milestone number and SPEC.md section, the "Notes for next build" from PIPELINE.md, and this instruction (the user's step-4 prompt):

> Implement Milestone <N> from @SPEC.md on this branch. Follow @CLAUDE.md. Tests first; not done until tests, lint and the smoke run pass. Commit in a few coherent units (typically 2–5), with tests in the same commit as the code they cover. End with your structured summary.

Mode handling:
- **Plan first**: invoke the builder in plan mode, save the plan to `.pipeline/m<N>-plan.md` (on disk; it's committed with the records), and gate: "Approve, or tell me what to change." If the user's answer changes SPEC.md, commit that change on its own. Then invoke the builder again in implement mode with the approved plan.
- **Learn**: invoke the builder in one-step mode. After each return, explain the commit in 3–5 lines (what changed, why, how it connects to the previous step) and gate: "Continue, or ask about this step." Repeat until the milestone is done.

After the builder returns, verify independently: run the tests, the linter and the smoke run yourself. If anything fails, send the failure back to the builder. After two failed rounds, stop and gate with the failure.

Save the builder's summary verbatim to `.pipeline/m<N>-build.md`.

## C. Open the PR

```
git push -u origin HEAD
gh pr create --draft --title "M<N>: <milestone title>" --body "Closes #<issue>

Description in progress." --milestone "<milestone title>"
```

Record the PR number in PIPELINE.md (on disk).

## D. Independent review and break-it checks

1. Invoke the **reviewer** with only: the PR number, the issue number, and which SPEC.md section the milestone covers. Save its output verbatim to `.pipeline/m<N>-review.md`.
2. **Break-it checks.** Take the reviewer's break-it candidates (and any items from the builder's "Unsure about" list that can be expressed as a deliberate bug). For each one:
   ```
   git worktree add ../_breakit HEAD
   ```
   Apply the deliberate bug in `../_breakit`, run the test suite there, record exactly which tests fail (names and count, not ranges), then `git worktree remove --force ../_breakit`. Never apply deliberate bugs in the main working tree.
3. A deliberate bug that **no test catches** is itself a must-fix finding: the fix is a new test.
4. Write the results table to `.pipeline/m<N>-breakit.md` (on disk; committed with the records).

## E. Fix must-fix findings

Split must-fix items into two kinds:

- **Behaviour fixes** (the code does the wrong thing): invoke the **builder** in fix mode, one finding at a time. Each is one commit, with a failing test added first where possible:
  ```
  Fix: <what changed>

  Why: <reason the old behaviour was wrong>
  Caught by: reviewer (R<k>)
  Test: <test name that now guards it>
  ```
- **Test-only gaps** (the code is right, but a break-it bug passed every test): invoke the builder once in batch-tests mode with all of them. One commit:
  ```
  Test: cover gaps found by break-it checks

  - B<k>: <bug> → <test name>
  - B<k>: <bug> → <test name>
  Caught by: reviewer (break-it)
  ```

Leave nits alone for now; they go to the user at the review gate. Push once, after all fixes.

If the builder disagrees with a finding, don't force it. Mark the finding "disputed" with the builder's reasoning and take it to the review gate.

## F. Review gate (the main human gate)

Present the gate with:
- links: the PR (`https://github.com/<owner>/<repo>/pull/<PR>`) and its Files changed tab (`.../pull/<PR>/files`)
- the **reading list**: the 30–60 lines that implement spec behaviour, from the reviewer's map, as file:line ranges
- reviewer findings, grouped as fixed (with commit hashes), nits, and disputed
- the builder's "Unsure about" list
- one line pointing to the break-it table in `.pipeline/m<N>-breakit.md`

Ask the user to read the reading list on GitHub and then tell you any of:
- **"Explain X"**: explain neutrally and briefly, in the style of an independent reviewer, not a defence of the code. If the honest explanation is convoluted, say so and propose a simpler rewrite.
- **"Change X because Y"**: implement it (builder in fix mode for anything non-trivial) as its own commit with `Why: <the user's reason, in their words>` and `Caught by: me`.
- **Nit decisions**: fix or leave, each. Nits the user chooses to fix go into **one** commit, `Fix review nits`, with each nit listed in the body.
- **Disputed findings**: the user decides.
- **"Done"**: move on.

Loop until the user says done. Keep a running list of everything changed at this gate; you need it for G and H.

## G. Inline comments

Post one review containing an inline comment for every fixed finding, every user catch and every user-requested change. Prefixes: `Caught (reviewer):`, `Caught (me):`, `Changed:`. Each comment says what was wrong in one line and gives the fix commit's short hash.

Build a JSON file and post it as a single review. Because the user authored the PR, `event` must be `COMMENT`:

```
gh api repos/<owner>/<repo>/pulls/<PR>/reviews --method POST --input .pipeline/m<N>-inline.json
```

```json
{
  "commit_id": "<current head sha>",
  "event": "COMMENT",
  "body": "Review trail for M<N>: reviewer findings and changes made at the review gate.",
  "comments": [
    {"path": "src/pkg/env.py", "line": 42, "side": "RIGHT", "body": "Caught (reviewer): ... Fixed in abc1234."}
  ]
}
```

A comment's line must fall within the diff. If a line isn't in the diff, put that item in the review's `body` instead. Items fixed in a batched commit share that commit's hash.

## H. Decisions gate, DECISIONS.md and the PR description

1. Identify 2–4 decision points from this milestone: places where the spec was silent and the build chose something, deviations from the spec, and nits the user chose to leave.
2. **Gate: decisions.** For each, ask with AskUserQuestion whether to keep or change it, then ask for **one sentence of rationale in the user's own words** (free text). If they choose "change", go back through E's fix process.
3. Append to DECISIONS.md:
   - each decision: what was decided, the alternative, and the user's rationale
   - each caught mistake, in this shape:
     > **M<N> / PR #<PR>: <short name>.** Caught by: <reviewer | me>. What: <...>. Why it was wrong: <...>. Fix: <hash>. Prevention: <test that now guards it>.
4. Fill `templates/pr-description.md`. The "Decisions" section is the user's rationale sentences, transcribed. Everything else (What's in it, Caught in review, Break-it checks with exact test counts, Deviations, Verification) you write from the records.
5. Show the user the full description and ask: "Approve, or tell me what to change." Then `gh pr edit <PR> --body-file .pipeline/m<N>-pr-description.md`.
6. Add any **patterns** from this milestone's caught mistakes to "Notes for next build" in PIPELINE.md (for example "check coordinate convention against SPEC before writing movement code"). This is how one milestone's review improves the next build.
7. Make the milestone's **records commit**: DECISIONS.md, `.pipeline/` and PIPELINE.md together, message `Record M<N>: decisions and pipeline records`. Push. This is the only commit the orchestrator makes for bookkeeping in a milestone.

## I. CI and the merge gate

```
gh pr ready <PR>
gh pr checks <PR> --watch
```

If checks fail, fix them through the builder (fix mode, `Caught by: CI`) and repeat.

When green, **gate: merge**:

```
━━ GATE: merge M<N> ━━
Needed from you: merge the PR on GitHub
Why it's yours: merging is your approval of this milestone
Look at: https://github.com/<owner>/<repo>/pull/<PR>
Use the arrow next to "Merge pull request" → "Create a merge commit" (keeps the fix commits).
Then type "merged".
```

## J. Ship

1. Verify: `gh pr view <PR> --json state,mergeCommit`. If not merged, re-present the merge gate.
2. `git checkout main && git pull && git branch -d feat/m<N>-<slug>`
3. Version: `v0.<N>`. If this was the final milestone, ask whether the project is finished before using `v1.0`.
4. Release:
   ```
   gh release create v0.<N> --title "v0.<N>: <milestone title>" --notes "<what it does in one line>. Review caught: <one line, or 'nothing'>. See DECISIONS.md."
   ```
5. Update PIPELINE.md (milestone row: merged, release tag; log line) but **don't commit it on main**. Everything reaches main through PRs, so the uncommitted update carries over when the next milestone's branch is created and gets committed there.
6. Tell the user the milestone is done, name the next one, and suggest `/clear` then `/pipeline` to start it with a clean context.
