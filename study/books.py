"""Feasibility study part B: do order-book and trade-flow features predict the BTC mid, on
mainnet and testnet, and does mainnet lead testnet? (.pipeline/feasibility-plan.md)

    uv run --group study python study/books.py --data data/collect --out study/results

Reads the collector's databases (study/collect.py) read-only. All times are the collector's
receive times, so a feature only uses data the collector had at the decision time. One
decision every `--stride` books (about 5 s), from 300 s after the start (the longest feature
window) to one horizon before the end.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from evaluate import COSTS_BPS, confident_threshold, evaluate, split_with_gap  # noqa: E402

from evidence import chance_note  # noqa: E402

HORIZONS_S = (60, 300, 900)
RET_WINDOWS_S = (5, 15, 60, 300)
FLOW_WINDOWS_S = (60, 300)
BANDS_BPS = (5, 10, 20)
MAIN_RET_WINDOWS_S = (5, 15, 60)
MAX_BOOK_AGE_S = 5.0  # a decision or label needs a book this fresh, else it is dropped (gap)
WARMUP_S = 300.0


@dataclass
class Venue:
    t: np.ndarray       # book receive times
    mid: np.ndarray
    spread_bps: np.ndarray
    imb: np.ndarray     # (n, len(BANDS_BPS)) imbalance within each band
    depth: np.ndarray   # (n, 2*len(BANDS_BPS)) log1p bid and ask size within each band
    tr_t: np.ndarray    # trade receive times, sorted
    tr_signed: np.ndarray  # +size for taker buys, -size for taker sells
    tr_size: np.ndarray
    hours: float
    n_trades: int
    gaps: int


def band_sizes(levels: list[list[float]], mid: float, bps: float, bid: bool) -> float:
    lim = mid * (1 - bps / 1e4) if bid else mid * (1 + bps / 1e4)
    return sum(sz for px, sz in levels if (px >= lim if bid else px <= lim))


def load_venue(path: Path) -> Venue:
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    rows = db.execute("SELECT recv_ts, bids, asks FROM books ORDER BY recv_ts").fetchall()
    t, mid, spread, imb, depth = [], [], [], [], []
    for rt, b, a in rows:
        bids, asks = json.loads(b), json.loads(a)
        m = (bids[0][0] + asks[0][0]) / 2
        t.append(rt)
        mid.append(m)
        spread.append((asks[0][0] - bids[0][0]) / m * 1e4)
        bs = [band_sizes(bids, m, x, True) for x in BANDS_BPS]
        as_ = [band_sizes(asks, m, x, False) for x in BANDS_BPS]
        imb.append([(x - y) / (x + y) if x + y > 0 else 0.0 for x, y in zip(bs, as_, strict=True)])
        depth.append([np.log1p(x) for x in bs] + [np.log1p(y) for y in as_])
    tr = db.execute("SELECT recv_ts, side, sz FROM trades ORDER BY recv_ts").fetchall()
    ta = np.array(t)
    return Venue(
        t=ta, mid=np.array(mid), spread_bps=np.array(spread), imb=np.array(imb),
        depth=np.array(depth),
        tr_t=np.array([r[0] for r in tr]),
        tr_signed=np.array([r[2] if r[1] == "buy" else -r[2] for r in tr]),
        tr_size=np.array([r[2] for r in tr]),
        hours=float((ta[-1] - ta[0]) / 3600) if len(ta) > 1 else 0.0, n_trades=len(tr),
        gaps=int((np.diff(ta) > MAX_BOOK_AGE_S).sum()),
    )


def mid_at(v: Venue, times: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Mid of the latest book received at or before each time, and that book's age (inf when
    there is none)."""
    i = np.searchsorted(v.t, times, side="right") - 1
    ok = i >= 0
    ic = np.clip(i, 0, None)
    return np.where(ok, v.mid[ic], np.nan), np.where(ok, times - v.t[ic], np.inf)


def flow(v: Venue, times: np.ndarray, w: float) -> tuple[np.ndarray, ...]:
    """Taker flow in (t - w, t]: net share (buy - sell over total), log1p count, log1p size."""
    cs = np.concatenate([[0.0], np.cumsum(v.tr_signed)])
    cz = np.concatenate([[0.0], np.cumsum(v.tr_size)])
    hi = np.searchsorted(v.tr_t, times, side="right")
    lo = np.searchsorted(v.tr_t, times - w, side="right")
    net, tot = cs[hi] - cs[lo], cz[hi] - cz[lo]
    return (np.where(tot > 0, net / np.where(tot > 0, tot, 1), 0.0), np.log1p(hi - lo),
            np.log1p(tot))


def build(v: Venue, horizon_s: float, stride: int, main: Venue | None = None) -> dict:
    """Features and labels at every stride-th book. Rows are dropped where the decision book,
    a feature's past book or the label's future book is older than MAX_BOOK_AGE_S."""
    idx = np.arange(0, len(v.t), stride)
    t = v.t[idx]
    keep = (t >= v.t[0] + WARMUP_S) & (t + horizon_s <= v.t[-1])
    m0 = v.mid[idx]
    cols, names = [v.spread_bps[idx]], ["spread_bps"]
    for k, b in enumerate(BANDS_BPS):
        cols.append(v.imb[idx, k])
        names.append(f"imb_{b}bps")
    for k in range(v.depth.shape[1]):
        cols.append(v.depth[idx, k])
        names.append(f"depth_{k}")
    for w in RET_WINDOWS_S:
        past, age = mid_at(v, t - w)
        keep &= age <= MAX_BOOK_AGE_S
        cols.append((m0 / past - 1) * 1e4)
        names.append(f"ret_{w}s")
    for w in FLOW_WINDOWS_S:
        for name, col in zip(("net", "count", "size"), flow(v, t, w), strict=True):
            cols.append(col)
            names.append(f"flow_{name}_{w}s")
    if main is not None:
        mnow, age = mid_at(main, t)
        keep &= age <= MAX_BOOK_AGE_S
        cols.append((m0 / mnow - 1) * 1e4)
        names.append("gap_vs_mainnet_bps")
        for w in MAIN_RET_WINDOWS_S:
            past, agep = mid_at(main, t - w)
            keep &= agep <= MAX_BOOK_AGE_S
            cols.append((mnow / past - 1) * 1e4)
            names.append(f"mainnet_ret_{w}s")
    fut, fage = mid_at(v, t + horizon_s)
    keep &= fage <= MAX_BOOK_AGE_S
    move = (fut / m0 - 1) * 1e4
    keep &= np.isfinite(move) & (move != 0)
    x = np.column_stack(cols)
    keep &= np.isfinite(x).all(axis=1)
    return {"t": t[keep], "x": x[keep], "y": (move[keep] > 0).astype(int),
            "move": move[keep], "names": names}


def models():
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    return {
        "logistic": lambda: make_pipeline(StandardScaler(), LogisticRegression(max_iter=2000)),
        "boosted trees": lambda: HistGradientBoostingClassifier(
            max_iter=100, learning_rate=0.05, max_depth=3, min_samples_leaf=200,
            l2_regularization=1.0, early_stopping=False, random_state=0),
    }


def run(data: Path, stride: int) -> tuple[list[dict], dict]:
    venues = {name: load_venue(data / f"{name}.db") for name in ("mainnet", "testnet")}
    specs = [("mainnet", "mainnet", None), ("testnet", "testnet", None),
             ("testnet + mainnet features", "testnet", "mainnet")]
    out = []
    for label, vname, mname in specs:
        for h in HORIZONS_S:
            d = build(venues[vname], h, stride, venues[mname] if mname else None)
            n = len(d["y"])
            if n < 50:
                continue
            gap_pts = int(np.ceil(h / max(np.median(np.diff(d["t"])), 1e-9)))
            train, test = split_with_gap(n, gap=gap_pts)
            if len(test) < 20:
                continue
            for mlabel, make in models().items():
                m = make()
                m.fit(d["x"][train.start:train.stop], d["y"][train.start:train.stop])
                p_tr = m.predict_proba(d["x"][train.start:train.stop])[:, 1]
                p_te = m.predict_proba(d["x"][test.start:test.stop])[:, 1]
                res = evaluate(list(p_te), list(d["y"]), test,
                               list(d["move"][test.start:test.stop]),
                               confident_threshold(list(p_tr)), block=gap_pts)
                out.append({"data": label, "model": mlabel, "h": h, "n": n, "res": res})
    return out, venues


def fmt(x, nd=2, sign=False):
    if x is None:
        return "n/a"
    return f"{x:+.{nd}f}" if sign else f"{x:.{nd}f}"


def report(rows: list[dict], venues: dict, stride: int) -> str:
    lines = [
        "<!-- Generated by study/books.py. Do not edit. -->",
        "# Feasibility B: order book and trade flow",
        "",
        "Plan: `.pipeline/feasibility-plan.md` (with amendment 1). Features: spread, imbalance and",
        "depth within 5/10/20 bps, returns over 5/15/60/300 s, taker flow over 60/300 s; the",
        "testnet + mainnet rows add mainnet's returns over 5/15/60 s and the testnet-minus-mainnet",
        "price gap. Fitted on the first 60% in time, one horizon skipped, scored on the rest.",
        f"One decision every {stride} books. 95% moving-block bootstrap, blocks one horizon long.",
        f"Costs: maker {COSTS_BPS['maker']} bps, taker {COSTS_BPS['taker']} bps.",
        "",
        "| Venue | Hours | Books | Trades | Gaps over 5 s |",
        "|---|---|---|---|---|",
    ]
    for name, v in venues.items():
        lines.append(f"| {name} | {v.hours:.1f} | {len(v.t)} | {v.n_trades} | {v.gaps} |")
    lines += [
        "",
        "| Data | Model | Horizon | Test n (blocks) | Up rate | Mean abs move bps "
        "| Brier - base rate [95%] | AUC [95%] | Confident calls | Mean move called bps [95%] "
        "| Reading |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        s = r["res"]
        lines.append(
            f"| {r['data']} | {r['model']} | {r['h']} s | {s.n_test} ({s.n_blocks:.0f}) "
            f"| {s.up_rate:.3f} | {s.abs_move_mean:.2f} "
            f"| {fmt(s.brier_diff, 4, True)} [{fmt(s.brier_lo, 4, True)}, "
            f"{fmt(s.brier_hi, 4, True)}] "
            f"| {fmt(s.auc, 3)} [{fmt(s.auc_lo, 3)}, {fmt(s.auc_hi, 3)}] | {s.n_confident} "
            f"| {fmt(s.edge_mean, 2, True)} [{fmt(s.edge_lo, 2, True)}, "
            f"{fmt(s.edge_hi, 2, True)}] | {s.edge_verdict()} |")
    lines += ["", "Multiple comparisons (Brier intervals): "
              + chance_note([r["res"].brier_verdict for r in rows]) + "."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", default="data/collect")
    ap.add_argument("--out", default="study/results")
    ap.add_argument("--stride", type=int, default=5)
    args = ap.parse_args(argv)
    rows, venues = run(Path(args.data), args.stride)
    text = report(rows, venues, args.stride)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "books.md").write_text(text)
    print(text)


if __name__ == "__main__":
    main()
