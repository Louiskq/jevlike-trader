# Hyperliquid facts looked up 2026-10-03

Source: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits

Per IP: REST aggregate weight 1200/min. Exchange actions weight 1 + floor(batch/40). Info l2Book/allMids/clearinghouseState/orderStatus weight 2; most other info weight 20.
WebSocket: max 10 connections, 30 new connections/min, 1000 subscriptions, 2000 messages sent/min, 100 inflight posts.
Per address: 1 request per 1 USDC traded cumulatively, initial buffer 10000 requests; when limited, 1 request per 10 s (cancels get a higher allowance).

Our budget (5 s cadence, 1 WS, ~3 subscriptions): <= ~1 order + ~1 close per 60 s, plus periodic reconciliation polls. Far under limits. The address-based limit is irrelevant at this volume (10000-request buffer).

SDK: hyperliquid-python-sdk 0.24.0, released 2026-06-04, repo pushed 2026-06-04, not archived. Maintained enough; used for signing and orders.
Minimum order value: $10 (to be verified against testnet in milestone 1).

## Order and fill formats (verified 2026-10-04 against the docs, for milestone 4)

Source: hyperliquid-docs for-developers/api/exchange-endpoint and info-endpoint, plus SDK 0.24.0 source.
- Order response: `{"status":"ok","response":{"type":"order","data":{"statuses":[...]}}}`; each status is `{"filled":{"totalSz","avgPx","oid"}}`, `{"resting":{"oid"}}` or `{"error":"..."}` (example: "Order must have minimum value of $10."). Cancel: statuses are "success" or `{"error":...}`.
- userFills / userFillsByTime rows: string numbers; `side` "B"/"A"; `time` ms; `fee`, `feeToken` ("USDC"), `tid`, `oid`, `crossed`, `closedPnl`, `startPosition`. At most 2000 fills per response, only the 10000 most recent are available.
- Info queries need the MAIN account address. The API wallet only signs; querying its address returns an empty result (docs, nonces-and-api-wallets).
- The SDK's Exchange(...) constructor makes info requests (spot meta), so it needs the network; it takes `timeout`, `account_address`, `base_url`.
- NOT verified (needs a testnet account): the exact error text for an IOC that matched nothing (the code matches "immediately match"; the docs list the status `iocCancelRejected`); whether reduce-only orders below $10 notional are exempt from the minimum; whether the UI's closed P&L is net of fees.
