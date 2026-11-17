"""Fine-tuning data for the decider: the bot's exact maker prompt, rendered offline from the
collector's testnet books and trades, labelled with what the testnet mid did one horizon later.

    uv run python study/finetune_data.py --data data/collect_snap --out data/finetune

User decisions (2026-10-26, PIPELINE.md): fine-tune for better calibration and ranking, not
P&L; the prompt adds mainnet prices; LoRA on decider9b_full. Each row carries both prompts:
`context` (what the bot sends today, for scoring the current decider) and `context_mainnet`
(today's prompt plus the mainnet block, for training and scoring the fine-tuned model).

Replay matches the bot: books in receive order, every trade received up to the book's
receive time on the tape, mids kept by receive time, `jevhl.maker.build_input` and
`jevhl.prompt.render_maker_context` unchanged. The split is by time, as in study/books.py:
the first 60% of rows train, one horizon is skipped, the rest is held out.

The mainnet block's wording and maths live in the bot (milestone 10: `jevhl.prompt`'s
mainnet wording, `jevhl.mainnet.mainnet_input`), so the fine-tuned model's training prompt and
the live prompt come from the same code. Moving them there left the rows byte-identical.
"""

from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
from collections import deque
from dataclasses import replace
from pathlib import Path

from jevhl.feed import Book, Print, TradeTape
from jevhl.mainnet import mainnet_input
from jevhl.maker import build_input
from jevhl.prompt import OPTIONS, question_text, render_maker_context

HORIZON_S = 60.0
STRIDE = 5
WARMUP_S = 310.0  # the bot holds as long with MAKER_PROMPT=mainnet (jevhl.mainnet.WARMUP_S)
MAX_BOOK_AGE_S = 5.0  # label freshness (the mainnet block's own bound is MAINNET_MAX_AGE_S)
TRAIN_SHARE = 0.6
KEEP_MIDS_S = 310.0  # the bot keeps max(RETURN_WINDOWS_S) + 10 s of mids


class Series:
    """(time, value) pairs sorted by time: the latest value at or before t, with its age."""

    def __init__(self, ts: list[float], vs: list[float]):
        self.ts, self.vs = ts, vs

    def at(self, t: float) -> tuple[float | None, float]:
        i = bisect.bisect_right(self.ts, t) - 1
        return (None, float("inf")) if i < 0 else (self.vs[i], t - self.ts[i])


def load_books(db: sqlite3.Connection, coin: str = "BTC") -> list[Book]:
    rows = db.execute("SELECT ts, recv_ts, bids, asks FROM books ORDER BY recv_ts").fetchall()
    return [Book(ts=ts, recv_ts=rt, coin=coin, bids=json.loads(b), asks=json.loads(a))
            for ts, rt, b, a in rows]


def load_prints(db: sqlite3.Connection) -> list[Print]:
    q = "SELECT ts, recv_ts, side, px, sz, tid FROM trades ORDER BY recv_ts"
    rows = db.execute(q).fetchall()
    return [Print(ts, rt, side, px, sz, tid) for ts, rt, side, px, sz, tid in rows]


def build_rows(books: list[Book], prints: list[Print], main: Series,
               horizon_s: float = HORIZON_S, stride: int = STRIDE) -> list[dict]:
    mids_series = Series([b.recv_ts for b in books], [b.mid for b in books])
    tape, mids = TradeTape(), deque()
    j, rows = 0, []
    t0, t_end = books[0].recv_ts, books[-1].recv_ts
    for k, book in enumerate(books):
        now = book.recv_ts
        batch = []
        while j < len(prints) and prints[j].recv_ts <= now:
            batch.append(prints[j])
            j += 1
        if batch:
            tape.add(batch, now)
        mids.append((now, book.mid))
        while mids and mids[0][0] < now - KEEP_MIDS_S:
            mids.popleft()
        if k % stride or now < t0 + WARMUP_S or now + horizon_s > t_end:
            continue
        fut, fage = mids_series.at(now + horizon_s)
        mi = mainnet_input(now, book.mid, main.at)
        if fut is None or fage > MAX_BOOK_AGE_S or mi is None:
            continue
        move = (fut / book.mid - 1) * 1e4
        if move == 0:
            continue
        inp = build_input(book, list(mids), tape, [], horizon_s)
        rows.append({
            "t": now, "mid": book.mid, "move_bps": move,
            "gold": 0 if move > 0 else 1,  # index into OPTIONS: 0 "up", 1 "down"
            "question": question_text(horizon_s), "options": list(OPTIONS),
            "context": render_maker_context(inp),
            "context_mainnet": render_maker_context(replace(inp, mainnet=mi)),
        })
    return rows


def split(rows: list[dict], horizon_s: float = HORIZON_S, share: float = TRAIN_SHARE) -> None:
    """Mark rows train / gap / test by time: first `share` of rows, one horizon skipped."""
    cut = rows[int(len(rows) * share)]["t"]
    for r in rows:
        r["split"] = "train" if r["t"] < cut else "test" if r["t"] >= cut + horizon_s else "gap"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", default="data/collect_snap")
    ap.add_argument("--out", default="data/finetune")
    ap.add_argument("--stride", type=int, default=STRIDE)
    args = ap.parse_args(argv)
    data = Path(args.data)
    tdb = sqlite3.connect(f"file:{data / 'testnet.db'}?mode=ro", uri=True)
    mdb = sqlite3.connect(f"file:{data / 'mainnet.db'}?mode=ro", uri=True)
    m = mdb.execute("SELECT recv_ts, bids, asks FROM books ORDER BY recv_ts").fetchall()
    main_series = Series([r[0] for r in m],
                         [(json.loads(b)[0][0] + json.loads(a)[0][0]) / 2 for _, b, a in m])
    rows = build_rows(load_books(tdb), load_prints(tdb), main_series, stride=args.stride)
    split(rows)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "rows.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    n = {s: sum(r["split"] == s for r in rows) for s in ("train", "gap", "test")}
    up = {s: sum(r["gold"] == 0 for r in rows if r["split"] == s) / max(n[s], 1) for s in n}
    print(json.dumps({"rows": len(rows), "splits": n, "up_rate": up}))


if __name__ == "__main__":
    main()
