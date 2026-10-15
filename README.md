# jevlike-trader

A bot that lets a jev-like model trade the BTC perpetual on the **Hyperliquid testnet**. The model reads the live order book and gives a probability that the price rises over the next 60 s. The bot turns that into orders, and a local dashboard streams every decision, fill, fee and calibration statistic. Testnet only: there is no mainnet code path, and no withdrawal or transfer code.

## The problem

In my words, from [PROBLEM.md](PROBLEM.md):

> The problem is that I want to test to see if a jev-like model can trade on some sort of market; here I'm picking Hyperliquid.
>
> It matters because I am interested in jev-like models and want to test their capabilities.

It is judged in this order: first, does the software work (reconciliation with the exchange, safety features, unattended running); second, is the model calibrated and does it have skill (Brier score against a baseline, not P&L); and P&L last.

## What it does

- **Two strategies on one core.**
  - **Taker** (default): about one decision per order-book snapshot, ~5 s apart. It buys if `p_up ≥ 0.60` and sells if `p_up ≤ 0.40`, otherwise it holds. Orders are marketable IOC, each closed 60 s after it fills.
  - **Maker** (`STRATEGY=maker`): about one decision a second. It always quotes one post-only order on the model's side, one tick inside the touch. The quote is replaced only when its side or price changes: a price change is one modify, a side change is a cancel plus a new order, because Hyperliquid refuses a side-changing modify.
- **The model.** `MODEL=mock` (in-process) or `MODEL=decider`, an HTTP model served on a GPU node and reached over SSH ([docs/soak.md](docs/soak.md)). A late or invalid answer becomes a hold and is never traded on.
- **Safety.** A kill switch fires on the daily loss limit, 3 consecutive order errors, or a stale book. It cancels, flattens with reduce-only orders, and stops; a stale book pauses instead, and the bot may resume. A failed flatten keeps retrying for 10 minutes. The maker obeys a 20-actions-a-minute cap.
- **Accounting.** Position and P&L change only on exchange fill confirmations. `jevhl.reconcile` checks fills, fees, position and `closedPnl` against the exchange.
- **Calibration.** Every decision is scored at 30, 60 and 300 s, traded or not, against a running base-rate baseline.

## Results

Full tables and plots are in [results/](results/README.md), regenerated from the bot's own logs by `scripts/evidence.py`. The headline:

| | Result |
|---|---|
| Reconciliation | All 5 live testnet runs agreed with the exchange on fills, fees, position and closedPnl |
| Safety drills | Loss-limit kill, short decider deadline (24/24 late holds, 0 fills) and maker cancel-on-kill worked live. The flatten retry and stale-feed pause/resume were tested in code only, never drilled live |
| Unattended run | 2 h on a VM (not overnight), 0 error events; memory rose +0.45 MB an hour and had not levelled off |
| Latency (taker, VM) | Model p50 41.0 ms, p99 289.4 ms, 0 late of 1,365 decisions |
| Latency (maker, 1 h) | Model p50 159.1 ms (relayed through a laptop), 0 late of 3,507; order modify p50 633 ms |
| Calibration | **No skill shown.** Pooled 60 s Brier 0.2765 vs baseline 0.2494 (2,809 overlapping decisions; 95% interval of the difference [-0.0155, +0.0653]). The model leaned down: mean p_up 0.38 against an up rate of 0.53 |
| Lead, not an edge | In the 1-hour maker run, the model ranked ups above downs better than chance at 30 s (AUC 0.686 [0.572, 0.793]). One run only |
| P&L | -$0.19 before fees, $1.40 fees, **-$1.59 after fees** over 319 minimum-size fills. Maker fee 1.50 bps, taker fee 4.50 bps (measured) |
| Compute | 8.13 GPU-hours on GH200s, all serving the model (no training) |

The sample is too small to claim skill or its absence: at most a few hundred non-overlapping outcome windows per horizon. A short-horizon bot is expected to lose its fees unless the model has a real edge; in the 2-hour taker run the median 60 s move was 0.7 bps against a 9 bps taker round trip. See [results/whats-didnt-work.md](results/whats-didnt-work.md) for the failures along the way.

## Key decisions

> **Draft by the assistant**, written at Louis's request ("do the facts yourself"). It states decisions and facts only, with Louis's own words quoted where Louis gave them; Louis has not yet edited it into their own words.

- **Testnet only, with no mainnet code path at all.** Louis will not trade real money (spec amended 2026-10-04). `NETWORK=mainnet` is rejected and there is no withdrawal or transfer code.
- **Judge the model on calibration, not P&L.** Every decision is scored, traded or not, at 30, 60 and 300 s (Louis: "yes do this"), against a running base-rate baseline. P&L is reported last.
- **Taker first, then a maker strategy alongside it** (M8), after comparing with jarrodwatts/jev-trader. The maker quotes post-only orders about once a second to pay the lower maker fee (1.50 bps against 4.50 bps).
- **Trade as much as the minimum allows.** Orders are rounded up one lot to meet the $10 exchange minimum. Louis: "I want to trade as much as possible to validate the idea."
- **Fix failures found in live runs before continuing.** The flatten gap after a network loss (M6), the stalled feed (M7) and the maker's side-changing modify (M9) were each fixed and reviewed before the next run.
- **Run the bot on a VM rather than the laptop; a 2-hour unattended run instead of overnight.** Louis: "if it works for say 2h its probably good".
- **No extra GPU run for the evidence.** The results say the sample is too small rather than adding a longer run.

## Limitations

> **Draft by the assistant**, written at Louis's request ("do the facts yourself"). It states decisions and facts only, with Louis's own words quoted where Louis gave them; Louis has not yet edited it into their own words.

- **Too few samples to judge the model.** There are at most a few hundred non-overlapping outcome windows per horizon, where PROBLEM.md expected a few thousand decisions to be needed. "No skill shown" is not "no skill".
- **Testnet is not mainnet.** Testnet's book is thin and updates slowly (WebSocket books about every 5.25 s), so fills, queue position and P&L say little about a real market.
- **Short runs.** The longest unattended run was 2 h, not overnight, and memory had not levelled off by its end (+0.45 MB an hour).
- **Recovery paths never tested live.** The flatten retry after a network loss (M6) and the stale-feed pause and resume (M7) are covered by tests only.
- **The prompt is the assistant's draft** (`src/jevhl/prompt.py`), and the model leaned down: mean p_up 0.38 against an up rate of 0.53.
- **The model depends on a GPU job and an SSH link.** The cluster login lasts about 12 hours, and the 1-hour maker run relayed the model through the laptop because the VM's login had expired.
- **Fees.** A 60 s taker round trip costs 9 bps against a median 60 s move of 0.7 bps, so the taker cannot profit without a large edge.

## Next steps

> **Draft by the assistant**, written at Louis's request ("do the facts yourself"). It states decisions and facts only, with Louis's own words quoted where Louis gave them; Louis has not yet edited it into their own words.

- A longer real-model run (the plan's E7, about 3 GPU-hours) to reach a few thousand scored decisions per strategy.
- Edit the decider prompt into Louis's own design, and check whether the downward lean comes from the prompt or the model.
- Follow up the maker run's ranking lead (AUC 0.686 at 30 s) with more maker runs before reading anything into it.
- Drill the flatten retry and the stale-feed pause and resume live.
- An overnight unattended run.
- Run the model server where the bot can reach it without a laptop relay or a 12-hour login.

## How to run it

Needs Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```
uv sync
uv run pytest -q                      # tests (no network)
uv run python -m jevhl.run --seconds 600 --dashboard
```

The default is a **dry run**: live testnet market data, the mock model and simulated fills, so no key is needed. The dashboard is at http://127.0.0.1:8765 (bound to loopback only).

**Live testnet orders** need a Hyperliquid testnet account and an API (agent) wallet. Put `HL_API_KEY` (the API wallet's key) and `HL_ACCOUNT_ADDRESS` (your main address) in `.env`, which is gitignored and never committed. Then:

```
uv run --env-file .env python -m jevhl.run --live --dashboard --db live.db
uv run --env-file .env python -m jevhl.reconcile --db live.db   # check against the exchange
```

Main settings (environment variables, defaults in brackets):

| Setting | Meaning |
|---|---|
| `STRATEGY` [taker] | `taker` or `maker` |
| `MODEL` [mock] | `mock` or `decider`; `decider` needs `DECIDER_URL` (loopback http only) |
| `GATE` [0.60], `HORIZON_S` [60] | taker gate and horizon |
| `DECIDER_DEADLINE_S` [2], `MAKER_DECIDER_DEADLINE_S` [1] | model deadlines; a late answer means hold |
| `MAX_POSITION_USD` [500], `MAX_DAILY_LOSS_USD` [100] | limits |
| `MAKER_SIZE_USD` [11] | maker quote size |
| `COIN` [BTC] | market |

`NETWORK=mainnet` is rejected. To reproduce the evidence: `uv run python scripts/evidence.py --runs runs --out results`, with the run databases in `runs/`.

## How this was built

Built with Claude Code through a staged pipeline:
- I wrote the problem and made the decisions at each gate; Claude wrote the code and the mechanical text.
- [SPEC.md](SPEC.md) records the spec decisions.
- [DECISIONS.md](DECISIONS.md) records every later decision and every caught mistake, with who caught it.
- Each of the 9 milestones went through a PR with an independent reviewer subagent, break-it checks (planted bugs the tests must catch) and a review trail on GitHub. Only I merged PRs (one records commit was pushed to main by the assistant by mistake; see PIPELINE.md, 2026-10-04).
- The builder and reviewer agents are in [.claude/agents/](.claude/agents/), and the per-milestone build and review records are in [.pipeline/](.pipeline/).
- The decider prompt in `src/jevhl/prompt.py` is still the assistant's draft.
