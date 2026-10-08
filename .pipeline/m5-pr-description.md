Closes #5

Milestone 5 delivers the decider backend and the soak tooling: `MODEL=decider` talks HTTP to a loopback `DECIDER_URL`, the order book is rendered into a prompt, and a soak report (memory, hold rate, late-hold rate) is printed at the end of a run. Nothing has been run against the real model through the bot yet (see "Not yet verified").

## What's in it
- **DeciderModel, draft prompt, config (6ce38ed):** `src/jevhl/model.py`, `src/jevhl/prompt.py`, `src/jevhl/config.py`. Strict reply parsing; any bad reply becomes an invalid or late hold.
- **Model selection and soak support (bddb7b4):** `MODEL=mock|decider`; `src/jevhl/soak.py` samples memory every minute and builds the soak report.
- **Soak docs (1036fdd):** `docs/soak.md` (SSH tunnel with placeholders, laptop must stay awake).
- **Review fixes:** 7c3506b, 7105162, 74aae54, 0b61806, and tests in f0ca131.

## Decisions
Made at the review gate. Rationale given for each, in the user's words: "no reason".
- **The HTTP contract is `POST /decide` with a question schema (options up/down); `p_up` is `probabilities["up"]`:** confirmed by the user; no reason.
- **Fix R1–R4 (soak scoped per run, prompt precision, memory sample off the event loop, URL rule in `validate()`) rather than leave them as nits:** no reason.

Decided by the orchestrator, from the user's instructions (not the user's reasoning):
- **The bot runs on the user's laptop and reaches the model through an SSH tunnel.** The user said "wherever latency is low".
- **The prompt wording in `src/jevhl/prompt.py` is the assistant's draft.** The user asked for a draft; they have not edited it yet.

## Caught in review
- **Caught (reviewer):** the soak report read the whole database, so a reused db could give a wrong memory verdict. Fixed in 7c3506b.
- **Caught (reviewer):** a cheap coin would render as a flat book in the prompt. Fixed in 7105162.
- **Caught (reviewer):** the memory sample could stall the event loop. Fixed in 74aae54.
- **Caught (reviewer):** the loopback-only URL rule was not enforced in `validate()`. Fixed in 0b61806.
- **Caught (reviewer, break-it):** a probability tolerance of 0 passed all tests, though it would reject real replies. Tests added in f0ca131.
- **Caught (me):** nothing raised by me at the review gate.

## Break-it checks
| Deliberate bug | Tests that failed |
|---|---|
| `PROB_SUM_TOLERANCE = 0.0` | none before f0ca131; now 1: test_rounded_probability_pair_within_tolerance_is_accepted |
| `PROB_SUM_TOLERANCE = 0.05` | now 1: test_probabilities_summing_to_1_02_are_rejected |
| wrong percentage in the stuck-hold warning | only the 100% case before; now exact message asserted in both cases |
| RSS unit conversion dropped on macOS | none before; now 1: test_rusage_fallback_converts_units_per_platform |
| periodic RSS sampling hardcoded / not forwarded by the dashboard path | none before; now 2: test_dry_run_samples_rss_periodically, test_dry_run_with_dashboard_forwards_rss_settings |

(Builder-run, one mutant at a time. The 0% stuck case was not run alone.)

## Deviations from the spec
None. The server's reply shape was verified by a 20-request timing test on the cluster, not just from its docs.

## Not yet verified (needs the real model through the bot)
Real latency over the SSH tunnel against the 2 s deadline (server-side latency measured at ~27 ms); tunnel behaviour on drops and reconnects; whether the draft prompt gives useful probabilities (a first test gave a mean `p_up` of 0.63, biased up); the overnight run, memory growth and hold rate; the Linux memory-reading path.

## Verification
- Reproduce locally: `uv sync --locked && uv run ruff check && uv run pytest` (525 tests, no key, network or model needed)
- Soak run instructions: `docs/soak.md`
