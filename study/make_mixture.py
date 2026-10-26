"""Build the decider fine-tuning mixture: the trading rows from study/finetune_data.py (train
split, the prompt with the mainnet block) plus a replay sample of the decider's own general
mixture, so the LoRA does not wipe out its other skills. Runs on the cluster, inside the
decider repository's environment (it imports `decider.data`).

    python make_mixture.py --rows rows.jsonl --general mixture_full.pkl --out mixture_jev.pkl

Writes (examples, evals) in the decider's mixture format. evals keeps a 150-per-task sample
of the general evals (to see whether general skill dropped) and adds `jev_btc_60s`, a sample
of held-out trading rows.
"""

from __future__ import annotations

import argparse
import json
import pickle
import random

REPLAY_SHARE = 0.2  # of the final mixture
TASK = "jev_btc_60s"


def main(argv: list[str] | None = None) -> None:
    from decider import data as D

    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", required=True)
    ap.add_argument("--general", required=True,
                    help="the decider's mixture pickle (examples, evals)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--context", default="context_mainnet", choices=["context", "context_mainnet"])
    ap.add_argument("--eval_rows", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=20261026)
    a = ap.parse_args(argv)
    rng = random.Random(a.seed)

    rows = [json.loads(line) for line in open(a.rows)]

    def ex(r):
        return D.Example(context=r[a.context],
                         qs=[D.Q(text=r["question"], options=list(r["options"]), gold=r["gold"])],
                         task=TASK)

    train = [ex(r) for r in rows if r["split"] == "train"]
    test = [r for r in rows if r["split"] == "test"]
    general, evals = pickle.load(open(a.general, "rb"))
    n_replay = round(len(train) * REPLAY_SHARE / (1 - REPLAY_SHARE))
    replay = rng.sample(general, n_replay)
    mix = train + replay
    rng.shuffle(mix)
    evals = {k: v[:150] for k, v in evals.items()}
    evals[TASK] = [ex(r) for r in rng.sample(test, min(a.eval_rows, len(test)))]
    pickle.dump((mix, evals), open(a.out, "wb"))
    print(json.dumps({"trading": len(train), "replay": len(replay), "total": len(mix),
                      "eval_tasks": len(evals)}))


if __name__ == "__main__":
    main()
