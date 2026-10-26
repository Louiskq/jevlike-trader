# Our maker (M8/M9) vs jarrodwatts/jev-trader, and can either actually trade?

Written 2026-10-23 by the assistant at the user's request. Jarrod's repo was read at d6254bf
(2026-10-13). Our numbers come from `runs/maker_live.db` (R5) and `runs/maker_live2.db` (R6).
Jarrod's public dry-run deployment (in his README) returned 404 on 2026-10-23, so his side uses
his code plus a live read of Monad's gas price (102 gwei, chain id 143).

## Side by side

| | Ours (`STRATEGY=maker`) | Jarrod's |
|---|---|---|
| Venue | Hyperliquid **testnet**, BTC perp | Kuru MON-USDC spot on **Monad mainnet** (chain 143), real funds |
| Cadence | 1 decision a second (REST book) | 1 decision every block (~300 ms) |
| Model question | up/down over 60 s (`p_up`) | buy/sell over 100 blocks (~30 s) |
| Prompt inputs | imbalance, depth bands, top levels, returns, sampled mids, taker flow, last prints (adapted from his) | the same family, plus `allowed` sides |
| Order | 1 post-only quote one tick inside the touch | the same |
| Replace rule | only when the side or price changes: a modify for price, cancel and place for side (M9) | **cancel and re-place every block**, changed or not (one `batchUpdate` tx) |
| Queue priority | kept while the quote is unchanged | lost every block |
| Rate limit | 20 actions a minute, enforced; one slot kept for an emergency cancel | none needed by the chain, but every block costs gas |
| Cost per order action | none (Hyperliquid actions are free) | gas on the **limit**, ~0.0357 MON a block, paid even on reverts |
| Trading fee | maker 1.50 bps (measured) | Kuru fee not modelled in the code |
| Position limit | `MAX_POSITION_USD`, reducing side always allowed | `MAX_POSITION_MON`, counts resting and in-flight orders too |
| Kill switch, loss limit | yes: daily loss, 3 order errors, stale book; cancels and flattens | **none** |
| On Ctrl-C | cancels the quote and flattens | nothing: orders stay resting, inventory stays |
| Fill truth | exchange fills by tid; `jevhl.reconcile` checks against the exchange | Trade logs from `eth_getLogs`; no reconcile |
| Calibration | every decision scored at 30/60/300 s vs a baseline | not scored |
| Paper fills | print reaches our price after placement (ignores queue) | the same rule, order lives one block |
| Side hysteresis | none | added, then removed (645a641, b587759) |

What we took from him: the post-only one-tick-inside quote, the prompt contents, removing our own
order from the book before pricing, and reducing a position by quoting the other side. Where we
differ on purpose: replace only on change (forced by Hyperliquid's action limit), the full M2-M7
safety layer, and reconciliation.

## Can either actually trade?

Two questions: can it place orders and get filled (mechanics), and can it make money (economics).

### Mechanics

- **Ours: yes, shown live.** R6 (1 h, testnet, real decider): 66 places, 136 modifies, 26
  side-change cancels, 0 order errors, 37 fills (36 maker), flat at the end, reconcile OK
  against the exchange. Costs seen: the 20-action cap bound 66 times in the hour (quote left
  where it was), 12 post-only rejections, and a 633 ms p50 order round trip.
- **Jarrod's: the code is complete for live trading, but nothing here shows it filled live.**
  His deployment was a dry run with the mock model, and it is now offline. Live use would need a
  funded mainnet wallet. We will not run that: this project is testnet only.

### Economics: the spread is smaller than the fee (ours)

On testnet BTC (~$83k, $1 tick = 0.12 bps), the mid sat on a half dollar in 87% of R6's
decisions, so the spread was usually one tick. A quote "one tick inside" then just joins the
touch. Measured over our maker fills (edge = how far the fill price was on our good side of the
mid; positive is good for us):

| Run | Maker fills | Edge at the fill | 5 s later | 30 s later | 60 s later | Fee |
|---|---|---|---|---|---|---|
| R5 | 6 | +0.15 bps | +0.13 | +0.82 | +0.19 | 1.50 bps |
| R6 | 36 | +0.14 bps | -0.10 | -0.33 | -0.77 | 1.50 bps |

- The maker earns about 0.14 bps of spread and pays 1.50 bps of fee, so it starts each fill
  about 1.4 bps down.
- After the fill the mid drifted against us on average (adverse selection: we get filled when
  the price is about to move through us). In R6 that cost a further ~0.8 bps by 60 s.
- To break even the model needs to be right by more than ~1.4 bps per fill on top of
  overcoming adverse selection. The calibration showed no skill (pooled 60 s Brier 0.2765 vs
  0.2494). R6's 30 s AUC of 0.686 did not show up in the fills (30 s markout -0.33 bps).
- 42 fills is a tiny sample; the sign of the markouts could flip with more data. The fee gap
  does not depend on the sample: it is the tick size against the fee rate.
- Mainnet BTC is likely the same story: also a $1 tick, also usually one tick wide, and
  Hyperliquid's base maker fee is the same 1.50 bps (lower only at high volume tiers).

### Economics: gas is larger than the edge (Jarrod's)

From his own code and README example (MON ~$0.0226, spread 7.07 bps, tick 0.44 bps):

- Gas: 350k gas limit × 102 gwei = 0.0357 MON a block. At ~12,000 blocks an hour that is
  ~428 MON (~$9.70) an hour, whether or not anything fills. His startup log prints this figure.
- Edge per fill: one tick inside a 7 bps spread is ~3.1 bps from mid. A 200 MON order is ~$4.52,
  so ~$0.0014 per fill before adverse selection and any Kuru fee.
- Break-even: ~$9.70 / $0.0014 ≈ 6,900 fills an hour, about one fill every 1.7 blocks, on a
  book where his own prompt says "most blocks do not fill". So as written it pays its gas
  regardless of the model.
- The wider spread is the one thing in its favour: 3 bps of edge per fill against our 0.14.
  Without the every-block re-place (quote only on change), gas would drop by the share of
  blocks with no change, and the spread edge could matter.

### Verdict

- **Ours trades** (shown live on testnet, reconciled) **but cannot make money on BTC as
  designed**: the spread it captures (~0.14 bps) is a tenth of its maker fee (1.50 bps), so
  profit would need a directional edge the model has not shown.
- **Jarrod's can place and fill orders in principle, unproven live here, and cannot make money
  as written**: re-placing every block costs ~$9.70 an hour in gas against ~$0.0014 per fill.
  It also has no kill switch, no loss limit and no cancel on shutdown, so a live run is riskier
  than ours.
- The levers that could change either answer: a market whose spread is wider than the fee (for
  us, a less liquid Hyperliquid coin), fewer order actions (for him), and a model that shows
  skill on the calibration test before any of it matters.

## Test run R7 (2026-10-23, the user's choice: 1 h live, real model)

Same setup as R6: VM, testnet, BTC, `STRATEGY=maker`, $15 cap, the decider on one GH200
(job 7666824, 1 h 04 min) relayed through the Mac. Record: `.pipeline/runs/maker-live-r7-1h.txt`.

- **Mechanics: passed.** Full 3600 s, exit 0, kill none. 3201 decisions, 52 late holds (1.6%;
  model p99 1001 ms against the 1 s deadline, worse than R6's 0). 95 places, 316 modifies,
  37 cancels, 22 post-only rejections, 14 gone, 54 action-cap hits, 0 order errors.
- **Reconcile: OK.** 57 fills on both sides (55 maker at 1.50 bps, 2 taker = the shutdown flatten
  split in two), fees $0.088759 both, closedPnl -$0.0151 both, flat, 0 open orders.
  After fees -$0.1038.
- **Fills, same measure as above:** edge at the fill +0.17 bps; 5 s -0.05, 30 s -0.51,
  60 s -0.56 bps; fee 1.50 bps. The same picture as R6: about 1.3 bps down at the fill, then
  adverse selection.
- **Calibration** (same block bootstrap as `results/`, seed 20261014):

| Horizon | Run | n | Brier - baseline [95%] | Result | AUC [95%] |
|---|---|---|---|---|---|
| 30 s | R6 | 1228 | -0.0006 [-0.0525, +0.0487] | no difference shown | 0.686 [0.572, 0.793] |
| 30 s | R7 | 1371 | +0.0080 [-0.0294, +0.0387] | no difference shown | 0.601 [0.508, 0.698] |
| 30 s | R6+R7 | 2599 | +0.0039 [-0.0264, +0.0322] | no difference shown | 0.630 [0.558, 0.702] |
| 60 s | R6 | 1600 | +0.0278 [-0.0441, +0.0934] | no difference shown | 0.647 [0.509, 0.776] |
| 60 s | R7 | 1491 | +0.0168 [-0.0329, +0.0512] | no difference shown | 0.568 [0.450, 0.683] |
| 60 s | R6+R7 | 3091 | +0.0225 [-0.0248, +0.0589] | no difference shown | 0.595 [0.517, 0.689] |
| 300 s | R7 | 1545 | +0.0668 [-0.0202, +0.1013] | too few samples | 0.439 [0.270, 0.651] |

- **What R7 adds.** The README's next steps named R6's 30 s ranking lead as the thing to follow
  up, so R7's 30 s AUC is the one pre-stated test here: 0.601, interval just above 0.5. The
  lead held in direction and weakened. Pooled over both maker runs, the 30 s AUC is 0.630
  [0.558, 0.702]. The probabilities are still not better than the base rate (Brier), so the
  model ranks somewhat but is miscalibrated; recalibration (E6's isotonic test) is the next
  check. Two one-hour runs on one market are still a small sample.
- **It did not reach the P&L.** Fills were adversely selected in both runs, and the 1.50 bps
  fee against ~0.15 bps of spread is unchanged. The verdict above stands: it trades, it does
  not make money on BTC.
