# M2 break-it results (orchestrator, throwaway worktree, base 0eb472d)

| # | Deliberate bug | Tests failing | Verdict |
|---|---|---|---|
| B1 | trader.py:109 partial-exit retry replaced with `pass` | none (240 passed) | uncaught: needs test |
| B2 | feed.py:139 backoff never reset | none (240 passed) | uncaught: needs test |
| B3 | trader.py:175 order-error counter never reset on success | none (240 passed) | uncaught: needs test |
| B4 | trader.py:138 staleness judged on `book.ts` not `book.recv_ts` | none (240 passed) | uncaught: needs test |
| B5 | trader.py:158 timeout latency logged as 0 | caught by reviewer: test_hung_model_times_out_to_late_hold_without_p | caught |
