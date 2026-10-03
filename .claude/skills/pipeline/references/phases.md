# Phases outside the milestone loop

Contents: Step 0 · Step 1 · Step 2 · Step 3 · Step 6 · Step 7 · Step 8 and the learning loop

---

## Step 0: Own the problem (no AI)

This step is deliberately AI-free. Don't suggest wording, ask leading questions, or critique.

**Gate: problem.** Ask the user to create `PROBLEM.md` themselves with three lines:
1. the problem
2. why it matters to them
3. how they'll know it worked (a metric or a demo)

Wait for them to say it's done. Read it, confirm it has three lines, and move on without commenting on the content unless they ask.

## Step 1: Ideate (outside this terminal)

This happens in Claude desktop: brainstorming, having Claude argue against the idea, and the user writing a rough SPEC.md of a few paragraphs.

If there's no `SPEC.md` in the repo, **gate**: ask the user to do step 1 in Claude desktop and save their rough SPEC.md into the repo root, then come back.

## Step 2: Spec interview

1. If the folder isn't a git repo, `git init`. Commit `PROBLEM.md` and the rough `SPEC.md` first, with message `Rough spec (before interview)`. Record that commit's hash in PIPELINE.md: the diff from it to the final spec shows how the user's thinking developed.
2. Run the interview. Read SPEC.md and interview the user in depth with AskUserQuestion about implementation, trade-offs, evaluation and risks. Argue against their approach where it's weak and suggest alternatives. Skip obvious questions. Keep going until the spec is complete.
   - Append each question and the user's answer to `.pipeline/spec-interview.md` as you go, so the interview survives a `/clear`.
   - The user's answers are their decisions. Don't nudge them toward your preferred option beyond stating the trade-off once.
3. Rewrite SPEC.md from the answers. Split the work into **3–6 milestones**, with tests and evaluation set up in the first milestone. Each milestone gets a short goal, acceptance criteria and its tests.
4. Seed `DECISIONS.md` from the interview answers only: the chosen approach, what was rejected and why, the evaluation plan, and what's out of scope.
5. **Gate: spec approval.** This is the user's single plan-approval checkpoint, so make it count:
   - Show `git diff <rough-spec-commit> -- SPEC.md --stat` and a 5-line summary of what changed.
   - Ask the user to read SPEC.md properly and check three things: it reflects their answers, the milestones are sensibly sized, and nothing important was added or dropped.
   - They either edit SPEC.md themselves or tell you the edits. Repeat until they approve.
6. Commit with message `Final spec (approved)` and record the hash in PIPELINE.md.

## Step 3: Repo setup

Do all of this locally first; the repo is created and pushed at the end in one step.

1. **.gitignore**: language defaults, plus `.env`, `.env.*`, `.claude/settings.local.json`, `wandb/`, checkpoint and weight files (`*.ckpt`, `*.pt`, `*.pth`, `*.safetensors`), and data/output folders. **Gate (quick):** ask whether there are cluster-specific paths or files to exclude.
2. **CLAUDE.md**: **gate**: ask the user to paste their CLAUDE.md rules, or confirm the existing file is current. Don't write the rules yourself.
3. **CI**: add `.github/workflows/ci.yml` that runs lint and tests on every push and pull request, using the project's tooling (for example `uv sync --locked`, `uv run ruff check`, `uv run pytest`).
4. **.claude/ snapshot**: if the pipeline is installed personally (`~/.claude/skills/pipeline`, `~/.claude/agents/builder.md`, `~/.claude/agents/reviewer.md`), copy them into the repo's `.claude/skills/pipeline/` and `.claude/agents/`, so the repo records the exact pipeline that built it and the README's "How this was built" link works. Add a project `.claude/settings.json` only if the project needs settings beyond the personal ones. `settings.local.json` must not be committed.
5. **Secrets check** on everything staged (see the ownership rules in SKILL.md), then make **one** setup commit: `Repo setup: gitignore, CLAUDE.md, CI, pipeline snapshot`.
6. **Gate: repo.** Ask for the repo name and public or private. Then:
   ```
   gh repo create <name> --<public|private> --source=. --push
   ```
7. **Milestones and issues**: for each milestone in SPEC.md:
   ```
   gh api repos/<owner>/<repo>/milestones --method POST -f title="Milestone <N>: <title>" -f description="<goal>"
   gh issue create --title "Milestone <N>: <title>" --body "<goal, acceptance criteria, link to the SPEC.md section>" --milestone "Milestone <N>: <title>"
   ```
   Record issue numbers in PIPELINE.md's milestone table.
8. Tell the user (optional, one line): protecting `main` on GitHub so changes must come through a PR with passing checks makes the merge gate enforced by GitHub too. It's in the repo's Settings under branch rules.

Then start the milestone loop at Milestone 1.

## Step 6: Evidence

1. Draft an experiment plan in `.pipeline/evidence-plan.md` from SPEC.md's evaluation plan: edge cases, baselines, multiple seeds for anything ML, and 2–3 ablations, each with what it tests and the expected outcome.
2. **Gate: evidence plan and compute.** Ask the user to approve or edit the plan, and where to run it (locally or on a cluster). Get explicit approval before submitting any cluster or GPU job. Never write cluster usernames or paths into committed files.
3. Run it. Put plots and tables under `results/`. Record compute as you go: hardware, and GPU-hours per run.
4. Draft a "What didn't work" section from real evidence only: failed runs, rejected approaches in DECISIONS.md, and caught mistakes. Don't invent failures.
5. Ship the evidence through the milestone loop (branch, PR, review, merge), because evidence code deserves review too.

## Step 7: README

1. Draft the mechanical sections: the problem (quoting PROBLEM.md, the user's words), results (from `results/`), how to run it, and a "How this was built" note linking to SPEC.md, DECISIONS.md and `.claude/`.
2. **Gate: README judgement sections.** Ask the user for key decisions, limitations and next steps in their own words (typed, or dictated for you to transcribe). Insert them verbatim apart from typo fixes.
3. Ship through the milestone loop. If every milestone is done, ask whether this release is `v1.0`.

## Step 8: Interview check and learning loop

Here you act as a tutor. The goal is the user's understanding, so do less, not more.

1. **Walkthrough.** Go through `git log --reverse` a few commits at a time. For each: what changed, why, and how it connects to what came before. Pause after each batch: "Continue, or questions?"
2. **Explain it back.** Pick a module. Ask the user to explain it in their own words without looking at the code. Then tell them what they got wrong or left out.
3. **Quiz.** Ask interviewer-style questions one at a time ("Why did you do X instead of Y?", "What happens if Z fails?"). Wait for each answer before giving feedback.
4. **One change by hand.** Propose a small change (a new metric, a changed default). The user implements it **without AI**: don't write code, don't hint at the implementation, don't review until they say it's done. Then run the tests and review it like a PR.

Track progress in PIPELINE.md's interview-check table: each module, whether the user can explain it in a sentence or two, whether they've walked through one PR and one caught mistake out loud, and whether they could rebuild the core logic without AI.

**Gate:** anything that fails the check gets studied until the user understands it, or cut. The user chooses which.
