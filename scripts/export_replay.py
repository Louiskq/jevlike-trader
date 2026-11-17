"""Export three one-hour maker runs as small JSON files for the site's replay page.

    uv run python scripts/export_replay.py --runs runs --out site/data

Reads only the run databases (decisions, outcomes, fills, mainnet_input events). Times are
seconds from the run's first decision. Nothing here touches the exchange or a key.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

RUNS = [
    ("r6", "maker_live2.db", "R6", "Original decider, 1 h"),
    ("r7", "maker_live3.db", "R7", "Original decider, 1 h"),
    ("ft", "maker_ft.db", "FT", "Fine-tuned decider with the mainnet prompt, 1 h"),
]
ACT = {"buy": "b", "sell": "s", "hold": "h"}


def export(db: Path, key: str, name: str, desc: str) -> dict:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        "select id, ts, p_up, action, reason, mid from decisions order by ts"
    ).fetchall()
    t0 = rows[0][1]
    y60 = dict(con.execute("select decision_id, label from outcomes where horizon_s=60"))
    mainnet = {}
    for (detail,) in con.execute("select detail from events where kind='mainnet_input'"):
        d = json.loads(detail)
        mainnet[d["decision_id"]] = (d["mid"], d["gap_bps"])
    last_fill = con.execute("select max(ts) from fills").fetchone()[0] or 0
    out = {
        "key": key, "name": name, "desc": desc,
        "seconds": round(max(rows[-1][1], last_fill) - t0) + 1,
        "t": [], "mid": [], "p": [], "a": [], "why": [], "y": [], "mm": [], "gap": [],
    }
    for did, ts, p, action, reason, mid in rows:
        out["t"].append(round(ts - t0, 1))
        out["mid"].append(round(mid, 1))
        out["p"].append(None if p is None else round(p, 4))
        out["a"].append(ACT[action])
        out["why"].append(reason)
        out["y"].append(y60.get(did))
        m = mainnet.get(did)
        out["mm"].append(None if m is None else round(m[0], 1))
        out["gap"].append(None if m is None else round(m[1], 1))
    out["fills"] = [
        [round(ts - t0, 1), side[0], round(px, 1), sz]
        for ts, side, sz, px in con.execute("select ts, side, sz, px from fills order by ts")
    ]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=Path, default=Path("runs"))
    ap.add_argument("--out", type=Path, default=Path("site/data"))
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    index = []
    for key, fname, name, desc in RUNS:
        data = export(args.runs / fname, key, name, desc)
        (args.out / f"{key}.json").write_text(json.dumps(data, separators=(",", ":")))
        index.append({"key": key, "name": name, "desc": desc})
        print(key, len(data["t"]), "decisions", len(data["fills"]), "fills")


if __name__ == "__main__":
    main()
