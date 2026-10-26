"""Score fine-tuning rows through a running decider server, the same request the bot sends
(`jevhl.model.DeciderModel`: POST /decide with the context and the one-question schema).

    python score.py --url http://127.0.0.1:8000 --rows rows.jsonl --split test --every 2 \
        --context context --out scores_base_test.jsonl

Stdlib only, so it runs in any Python on the node. Writes one line per row: t and p_up. Rows
already in --out are skipped, so a rerun after an interruption continues.
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor


def decide(url: str, context: str, question: str, options: list[str]) -> float:
    body = json.dumps({"context": context,
                       "schema": {question: {"type": "choice", "options": options}}}).encode()
    req = urllib.request.Request(url + "/decide", body, {"content-type": "application/json"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
                out = json.loads(resp.read())
            break
        except OSError:
            if attempt == 4:
                raise
            time.sleep(2 ** attempt)
    answer = out.get("answers", out)[question] if isinstance(out, dict) else out
    return float(answer["probabilities"][options[0]])


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000")
    ap.add_argument("--rows", required=True)
    ap.add_argument("--split", required=True, choices=["train", "test"])
    ap.add_argument("--every", type=int, default=1, help="score every n-th row of the split")
    ap.add_argument("--context", default="context", choices=["context", "context_mainnet"])
    ap.add_argument("--out", required=True)
    ap.add_argument("--threads", type=int, default=8)
    a = ap.parse_args(argv)
    rows = [r for r in map(json.loads, open(a.rows)) if r["split"] == a.split][::a.every]
    try:
        done = {json.loads(line)["t"] for line in open(a.out)}
    except FileNotFoundError:
        done = set()
    todo = [r for r in rows if r["t"] not in done]
    t0 = time.time()
    with open(a.out, "a") as f, ThreadPoolExecutor(a.threads) as pool:
        futs = [(r, pool.submit(decide, a.url, r[a.context], r["question"], r["options"]))
                for r in todo]
        for i, (r, fut) in enumerate(futs, 1):
            f.write(json.dumps({"t": r["t"], "p_up": fut.result()}) + "\n")
            if i % 500 == 0:
                f.flush()
                print(f"[score] {i}/{len(todo)} {time.time() - t0:.0f}s", flush=True)
    print(f"[score] done {len(todo)} rows ({len(done)} already there) in {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
