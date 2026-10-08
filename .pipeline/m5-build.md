# M5 build summary (builder subagent, condensed from its report)

Built: MODEL=decider over HTTP against DECIDER_URL. DeciderModel in model.py (strict validation, abort on stop); DRAFT prompt in prompt.py (assistant-written, for the user to edit); soak.py (RSS sampling every minute into events, soak report); docs/soak.md. Branch existed at main (e2274a0). PIPELINE.md left modified/uncommitted.

Commits: 6ce38ed DeciderModel + draft prompt + config + tests; bddb7b4 MODEL selection + soak support; 1036fdd soak docs. No Co-Authored-By.

Deviations from SPEC: none.

Unsure about:
- Probabilities must sum to 1 within 0.01 (builder's addition); an unnormalised server reply would make every decision an `invalid` hold.
- Trader.stopped setter now calls model.abort() (permanent for that instance); clearing stop later does not undo it.
- No retry/backoff exists; failure = one invalid hold.
- DECIDER_DEADLINE_S applies per socket operation, not as a total timeout; trader still holds as late at 2 s and blocks new calls until the thread ends.
- run.main still picks MockModel directly; build_model used only for decider (live-run tests monkeypatch run.MockModel).
- isfinite in the probability check is redundant (NaN fails range check); no test catches its removal.
- RSS source: /proc on Linux, ps on macOS, else getrusage peak (cannot show recovery).
- Prompt: schema key and reply key are the question text; fewer than 10 levels print only those; "Top 10" header fixed.
- DECIDER_URL must be http:// to 127.0.0.1/localhost/[::1], no path, query or credentials; MODEL outside mock/decider is a ConfigError.

Verification (builder): new tests failed at collection before the code (no per-test red run); 508 tests pass, ruff clean; 10 mutants deleted in turn, all caught except isfinite; smoke run with a local mock decider against the real testnet feed: 6 decisions, 6 requests, no late holds, 2 RSS samples. Orchestrator re-ran: 508 pass, ruff clean, no co-author trailers, no cluster username/paths in src/docs/tests.

Cannot verify without the real model: actual reply shape in practice; tunnel drop/reconnect behaviour; real latency vs 2 s; usefulness of the draft prompt; overnight leak and hold-rate; /proc RSS path on the soak machine.
