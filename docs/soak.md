# Running the decider soak

The bot talks to the decider over HTTP. The model runs on a cluster GPU node, which you set up
and serve yourself; the bot reaches it through an SSH tunnel to `127.0.0.1`. `DECIDER_URL` must
be plain `http://` to loopback (`127.0.0.1`, `localhost` or `[::1]`); anything else is refused.

## The contract

- `POST {DECIDER_URL}/decide` with `{"context": "<text>", "schema": {"<question>": {"type": "choice", "options": ["up", "down"]}}}`
- reply `{"<question>": {"choice": ..., "confidence": ..., "probabilities": {"up": 0.38, "down": 0.62}}}`; `p_up = probabilities["up"]`
- `GET /health` returns `{"ok": true, ...}` (handy for checking the tunnel by hand).

A reply is accepted only if both probabilities are present, finite, in [0, 1] and sum to about
1. Anything else (bad JSON, HTTP error, timeout, connection refused) becomes an `invalid` or
`late` hold, is logged as a `model_error` event where relevant, and never reaches the database
as a probability. The decider deadline is `DECIDER_DEADLINE_S` (default 2 s).

## The prompt is a draft

`src/jevhl/prompt.py` holds the wording sent as `context` and the question text. It was written
by the assistant as a first draft. What the decider sees is your modelling decision: edit the
constants at the top of that file. The reply is keyed by the question text, so changing the
question needs no other change.

## Steps

1. On the cluster, start the model server on a compute node (your own procedure), listening on
   port 8000.
2. On the laptop, open the tunnel (leave it running):

   ```
   ssh -N -L 8000:<compute-node>:8000 <user>@<login-host>
   ```

   Check it: `curl -s http://127.0.0.1:8000/health`.
3. Keep the laptop awake for the whole run, and run the bot under `caffeinate`:

   ```
   MODEL=decider DECIDER_URL=http://127.0.0.1:8000 \
     caffeinate -i uv run python -m jevhl.run --db soak.db --seconds 43200
   ```

   Add `--live` (with the testnet credentials in `.env`, via `uv run --env-file .env`) for a
   testnet-order soak, and `--dashboard` to watch it. Use a fresh `--db` per soak.
4. If the tunnel drops, requests fail fast (connection refused) and the bot holds with reason
   `invalid`; it keeps running and resumes when the tunnel returns. Do not expect trades during
   an outage.

## Reading the result

The end-of-run report ends with a soak section:

```
soak: rss start 61234 kB  end 61890 kB  max 62010 kB  (721 samples)
soak: decisions 8120  hold rate 71.3%  late-hold rate 0.4%
```

Process memory (RSS) is sampled once at start, then every minute, then once at the end, into
the `events` table (`kind = 'rss'`, detail in kB), so the series survives a crash:
`SELECT ts, detail FROM events WHERE kind = 'rss'`. Steady growth from start to end is a leak.
A hold rate of exactly 0% or 100% over 50 or more decisions prints a `WARNING: hold rate
stuck` line; either means the model or the gate is not doing what you think (for example a
dead tunnel gives 100% holds).

## The fine-tuned decider (milestone 10)

The fine-tuned decider was trained on the maker prompt plus a mainnet block (mainnet BTC mid,
this market's gap to it, mainnet mid change over 5, 15 and 60 s). Run it with the maker and
`MAKER_PROMPT=mainnet`:

```
STRATEGY=maker MAKER_PROMPT=mainnet MODEL=decider DECIDER_URL=http://127.0.0.1:8000 \
  uv run python -m jevhl.run --db maker_ft.db --seconds 3600 --dashboard
```

- Serve the fine-tuned model at temperature 1.0 (it was trained with cross-entropy at T = 1).
  That is a server setting; the bot's `/decide` contract is unchanged.
- The bot then also reads mainnet's public BTC order book about once a second, read-only (no
  key, no signing; `src/jevhl/mainnet.py`). `MAKER_PROMPT=mainnet` needs `COIN=BTC`. The taker
  ignores the setting.
- The first 310 s of a run are `warmup` holds: training skipped each recording's first 310 s,
  so the model never saw a prompt with less history.
- A decision without a mainnet book under 5 s old is a `no_mainnet` hold: no model call, the
  resting quote is left as it is, and it is not a kill. `mainnet_stale` and `mainnet_fresh`
  events mark each spell; the dashboard's maker card shows mainnet's mid, the gap and its age.
- Each decision whose prompt carried the block has a `mainnet_input` event:
  `SELECT json_extract(detail, '$.decision_id'), json_extract(detail, '$.gap_bps')
  FROM events WHERE kind = 'mainnet_input'`.
- The mainnet wording in `src/jevhl/prompt.py` is what the model was trained on: change it, or
  the maker wording, only together with a new fine-tune (`tests/test_mainnet_prompt.py` fails
  when the prompt drifts).
