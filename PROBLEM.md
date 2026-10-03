1. The problem is that I want to test to see if a jev-like model can trade on some sort of market; here I'm picking Hyperliquid.

2. It matters because I am interested in jev-like models and want to test their capabilities.

3. How I'll know it worked:

**1. The software works when all of these are true:**

Tests pass, and the dry run streams a decision every cycle with no errors.
Orders show up on the exchange. Your testnet orders appear in Hyperliquid's testnet UI, and the bot's fills, position, fees and P&L match the UI exactly. This reconciliation is the most important check; if anything is off, the accounting is wrong.
The safety features fire. Set the daily loss limit very low and watch the kill switch cancel orders, flatten your position and stop. Turn your wifi off mid-run and check that it holds, reconnects and never trades on stale data. Set the decider deadline very short and check it logs late holds instead of trading.
It runs unattended. It survives a night with no crashes and no growing memory use, and the latency numbers in the README come from its own logs.

**2. The model works. This is judged on the calibration panel, not on P&L:**

It answers reliably. The decider responds within its deadline nearly every time, and its hold rate isn't stuck at 0% or 100%.
It's calibrated. Its reliability curve sits near the diagonal: when it says 60%, it's right about 60% of the time.
It has skill. It has a lower Brier score than simply predicting historical frequencies. You need a lot of decisions before that means anything: probably a few thousand. Consecutive decisions overlap, so they aren't independent samples.
P&L comes last. Positive P&L before fees would be a good sign. Positive P&L after fees over weeks is the real bar, and most short-horizon bots never clear it. A negative after-fees line doesn't mean the project failed.
