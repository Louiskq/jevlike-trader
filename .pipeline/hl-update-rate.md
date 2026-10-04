# Hyperliquid update-rate measurement (2026-10-03, from a Mac, public WS, read-only)

Script was run from the session scratchpad (not committed). 30-60 s windows.

| Feed | Venue | Result |
|---|---|---|
| l2Book BTC | testnet | 0.20-0.23 msg/s; gap p50 ~5.3 s, p90 ~5.4-5.5 s (single and concurrent connection both) |
| l2Book BTC | mainnet | 0.20 msg/s; gap p50 5.4 s |
| l2Book ETH | testnet | 0.22 msg/s; gap p50 5.3 s |
| server->client lag (l2Book) | both | p50 ~400-470 ms, p90 ~500-900 ms (includes clock skew; indicative only) |
| bbo BTC | testnet | 0.27 msg/s; only on change; gap p50 ~650 ms but p90 ~14 s (bursty, thin book) |
| trades | testnet BTC | ~35-45 trades per 30-60 s, in 3-14 messages |

Reading: l2Book snapshots arrive roughly every 5 s, not sub-second. bbo is faster but event-driven. A "decide every 300 ms" design would act on the same book ~16 times.
