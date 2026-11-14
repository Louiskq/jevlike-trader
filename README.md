# jevlike-trader

A bot that lets a jev-like model trade the BTC on the **Hyperliquid testnet**, and the evidence for whether it can.

**Short answer: the software works, the model has not been shown to have an edge, and no run made money after fees.**

Results write-up: https://jevlike-trader.vercel.app

Trading is testnet only

The model reads the live order book and gives a probability the price rises over the next 60 s. The bot trades on it as a **taker** (marketable orders, closed after 60 s) or a **maker** (one post-only quote a second). It has a kill switch (loss limit, order errors, stale book), books P&L only from exchange fills, and scores every decision against a base rate. A local dashboard shows it live.

## Results

| | Result |
|---|---|
| Software | Every live run reconciled with the exchange on fills, fees and position. Loss-limit kill, short deadline and cancel-on-kill drilled live. 2 h unattended, 0 errors. 1,007 tests |
| Model | **No skill shown.** 60 s Brier is worse than the base rate in every live run, with every interval including zero. Pooled R3+R6: +0.0271 [-0.0155, +0.0653] |
| P&L | -$1.59 after fees over 319 fills (R1 to R6), -$0.10 (R7), -$0.26 (fine-tuned run). A taker round trip costs 9 bps against a median 60 s move of 0.7 bps |

Details: [results/](results/README.md) and [results/whats-didnt-work.md](results/whats-didnt-work.md).

### The fine-tune

I fine-tuned the decider (LoRA) with mainnet prices in its prompt, after a 67-hour study showed testnet's 60 s move is learnable because it follows mainnet ([study/results/](study/results/)).

| 8,782 held-out decisions | Brier | AUC |
|---|---|---|
| Current decider | 0.2800 | 0.557 |
| Current, recalibrated | 0.2486 | 0.550 |
| **Fine-tuned** | **0.2243** | **0.677** |

Clearly better offline. **Live for one hour it was not** (60 s Brier 0.2858, +0.038 worse than the base rate, interval [-0.031, +0.108]). The model link dropped 45 minutes in, and that hour testnet traded about 42 bps below mainnet where the training data had it about 40 bps above. One hour cannot show that was the cause. See [.pipeline/m10-live.md](.pipeline/m10-live.md).

## Limitations

- Too few samples: a few hundred non-overlapping outcome windows per run, where a few thousand were needed. "No skill shown" is not "no skill".
- The fine-tune's edge is mostly mainnet prices that testnet follows, so it may be a testnet artefact. It was not reproduced live.
- Testnet is thin and slow, so fills and P&L say little about a real market.

## How to run it

Needs Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```
uv sync
uv run pytest -q
uv run python -m jevhl.run --seconds 600 --dashboard   # dry run, no key needed
```

Dashboard at http://127.0.0.1:8765. Live testnet orders need `HL_API_KEY` and `HL_ACCOUNT_ADDRESS` in `.env`, then `uv run --env-file .env python -m jevhl.run --live --dashboard --db live.db`, and `python -m jevhl.reconcile --db live.db` to check against the exchange. Settings (`STRATEGY`, `MAKER_PROMPT`, `MODEL`, limits) are environment variables, defined in [src/jevhl/config.py](src/jevhl/config.py). The model server setup is in [docs/soak.md](docs/soak.md).


