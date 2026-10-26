# Fine-tuning feasibility study: plan (written before any results)

Asked by the user on 2026-10-23 ("what if we finetune a model with the data it needs", then
"ok do this"; data source: "Both, compare"). A check, before any GPU time, of whether the
data contain anything a fine-tuned model could learn that would clear the trading costs.

## Question

Does a simple model, on the features the decider's maker prompt shows, predict the BTC mid
direction out of sample, at which horizons, and is the edge on its confident calls larger
than the costs? If a simple model finds nothing at any horizon, fine-tuning a 9B LLM on text
renderings of the same numbers is unlikely to; if it finds something, it is the bar a
fine-tuned decider has to beat.

## Data (read-only, no orders; trading stays testnet-only)

- **A. Candles, now.** Hyperliquid `candleSnapshot` for BTC on mainnet and testnet: 1m (about
  3.5 days), 5m, 15m and 1h (the most each interval returns, 5000 candles). Price only.
- **B. Books and trades, collecting from 2026-10-23 20:09 UTC.** `study/collect.py` on the VM:
  REST l2Book (20 levels) about every second and every public trade, both venues side by side.
  First readout after about 24 h, the main one after about 72 h.

## Horizons and costs

- Horizons: A: 1, 5, 15, 60, 240 min. B: 60 s, 300 s, 900 s.
- Costs per trade, from our measurements: maker 1.35 bps (1.50 bps fee less ~0.15 bps spread
  captured), taker 9 bps round trip (4.50 bps each way). Mainnet base tier fees are the same.

## Models

- Baselines: running base rate, and constant 0.5.
- A: logistic regression on past returns (1, 5, 15, 60 bars) and recent volatility.
- B: logistic regression and gradient-boosted trees on: imbalance (within 5/10/20 bps), depth
  bands, spread, returns (5/15/60/300 s), taker flow (60/300 s: net size, count), sampled-mid
  slope. The same family as `src/jevhl/prompt.py`'s maker block.
- Labels: sign of the mid change over the horizon, ties dropped. Features use only data at or
  before the decision time.

## Split and statistics

- Time-ordered: train on the first 60%, skip one horizon (so no outcome window crosses the
  split), test on the last 40%. Nothing is tuned on the test set.
- 95% intervals: the moving-block bootstrap from `scripts/evidence.py` (blocks one horizon
  long, 2000 resamples, seed 20261014).
- Several horizons, venues and models are tested, so some intervals will exclude zero by
  chance; the readout counts how many and compares with chance, as in `results/`.

## Pass criteria (fixed now)

At a horizon, on one venue, a model **finds an edge** if both hold on the test set:
1. AUC interval lies above 0.5, and Brier is below the base rate's (interval of the
   difference below zero).
2. On its confident calls (the 20% of test points with the largest |p - 0.5|, threshold set on
   the training set), the mean move in the called direction has a 95% interval whose lower end
   is above the cost: 1.35 bps (maker) or 9 bps (taker).

1 alone means "learnable, not tradable at these costs". Neither means "nothing found".

## What the result decides (for the user)

- Edge found at some horizon: propose fine-tuning the decider at that horizon (data, budget
  and spec changes go through the user's gates).
- Learnable but not tradable: fine-tuning could improve calibration (the project's main
  criterion) but not P&L; the user decides if that is worth GPU time.
- Nothing found: do not fine-tune on these inputs.

## Amendment 1 (2026-10-23, after part A's results, before any part B data was analysed)

Part A found nothing at any horizon on either venue (`study/results/candles.md`). One added
test for part B, because the collector records both venues side by side: **does mainnet lead
testnet?** Testnet BTC tracks the mainnet price, so mainnet's recent returns (5/15/60 s) and
the testnet-minus-mainnet price gap (bps) are added as features for testnet labels, as a separate model ("testnet + mainnet features"). If it
passes, that is an edge on testnet that comes from testnet following mainnet, not from reading
a real market: it would say nothing about trading mainnet, and the readout must say so. Same
split, statistics and pass criteria as above. (The price gap was added to this amendment before
any part B data had been analysed.)

## Amendment 2 (2026-10-23, method check on synthetic data, before any real part B analysis)

`study/books.py` was run on 6 h of synthetic books with a planted signal (testnet copies mainnet
10 s late) and none elsewhere. The logistic model with mainnet features found it at 60 s
(AUC 0.686, edge +3.28 bps [+2.71, +3.81]) and nothing was found where nothing was planted.
Two fixes came out of it:
- The boosted trees overfit noise (Brier up to +0.2 worse than the base rate), so they are now
  regularised harder (depth 3, at least 200 points a leaf, 100 rounds); after that they found
  the planted signal too (AUC 0.673) and stopped overfitting.
- An edge reading now needs confident calls spanning at least 10 bootstrap blocks; with one or
  two calls the interval collapsed to a single value.
