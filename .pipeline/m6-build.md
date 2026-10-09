# M6 build summary (builder subagent, condensed from its report)

Built: a failed live flatten is retried (1 s, 2, 4 ... capped 30 s) until the exchange is flat or FLATTEN_RETRY_WINDOW_S (default 600; finite, >= 0; 0 = one attempt) runs out. Each retry re-reads the exchange position, places only reduce-only closes, books partial fills at once. Events: flatten_failed per attempt, then flatten_recovered or flatten_gave_up. Wait is an awaited asyncio.sleep; self._lock free during the wait; a new _flatten_lock serialises whole flattens. One retry window per run: a later flatten (shutdown after a kill) joins a running retry or makes one attempt. run.py: report ends with "FLATTEN GAVE UP: the position may still be open on the exchange, close it by hand"; a second Ctrl-C prints interrupted_report and exits 130. Paper path never retries.

Commits: bf1e1d0 Config: FLATTEN_RETRY_WINDOW_S; 50f5839 Trader retry (includes lot-snap fix); fb64baa run.py last line and second Ctrl-C. No Co-Authored-By.

Deviations from SPEC / extras:
- exchange.py Exchange.flatten: size now round(abs(pos), sz_decimals) before reduce_size (a float position of 9.99e-6 floored to a zero-size order so a retry could never finish; probably latent on the real venue).
- Existing test test_flatten_that_cannot_reach_the_venue... now uses flatten_retry_window_s=0 (asserts old behaviour; assertions unchanged).
- No docs edited; FLATTEN_RETRY_WINDOW_S not in .env.example (agents cannot edit it).

Unsure about:
- First Ctrl-C while a halt-flatten is retrying (no dashboard): cancels the gather and so aborts the retry, logging gave_up; it does not restart a flatten. Making the first Ctrl-C ignored and only the second abort would need asyncio.shield plus signal handling; not built.
- Dashboard mode: uvicorn takes the first SIGINT; the second is believed to reach asyncio.run's handler. Untested with real signals.
- Two mutations (config window ignored; lock held across the wait) were caught only by a pytest timeout (a hang), not a clean named failure.
- A cancel waits for the in-flight executor.flatten thread (3 tries plus HTTP timeouts) because threads cannot be interrupted.

Verification (builder): config tests failed before the change (10), trader tests failed with TypeError before implementation, run.py tests failed on import; 547 pass, ruff clean; 17 mutants, all caught (two by timeout). Not mutation-tested: KeyboardInterrupt handling in main (needs network). Orchestrator re-ran: 547 pass, ruff clean, no co-author trailers, no env/db/settings files in the diff.

Cannot verify without the real venue: a real network drop mid-flatten and recovery; real SIGINT behaviour (first/second Ctrl-C, with and without --dashboard, exit 130); real HTTP timeouts in the SDK; whether a reduce-only IOC fills after a long outage.
