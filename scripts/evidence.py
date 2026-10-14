"""Regenerate the step-6 evidence in results/ from the bot's own SQLite logs.

    uv run python scripts/evidence.py --runs runs --out results

Every database is opened read-only. Brier, the running baseline, reliability bins and latency
percentiles come from jevhl's own functions, so the numbers match the dashboard and the run
reports. The bootstrap and the permutation test use fixed seeds, so a rerun on the same
databases writes the same files.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
import random
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path

from jevhl.calibration import baseline_brier_running, brier, reliability_bins
from jevhl.latency import latency_report, percentile
from jevhl.ledger import Ledger
from jevhl.soak import soak_report
from jevhl.store import SCHEMA_VERSION, Store
from jevhl.types import HORIZONS_S

# ---------------------------------------------------------------------------------------------
# Statistics (pure functions, tested in tests/test_evidence.py)
# ---------------------------------------------------------------------------------------------

GATE_EPS = 1e-12  # the same float tolerance as jevhl.policy.EPS


def mean(xs: Sequence[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def running_baseline_probs(ys: Sequence[int]) -> list[float]:
    """At each step, the mean of the labels before it (0.5 first): the forecast whose Brier is
    jevhl.calibration.baseline_brier_running."""
    out, wins = [], 0
    for i, y in enumerate(ys):
        out.append(wins / i if i else 0.5)
        wins += y
    return out


def brier_diffs(ps: Sequence[float], ys: Sequence[int]) -> list[float]:
    """Per-decision (p - y)^2 - (b - y)^2 with b the running baseline. The mean is Brier minus
    the running baseline Brier; negative means the model beat the baseline."""
    bs = running_baseline_probs(ys)
    return [(p - y) ** 2 - (b - y) ** 2 for p, y, b in zip(ps, ys, bs, strict=True)]


def block_len(ts: Sequence[float], horizon_s: float) -> int:
    """The horizon in decisions: horizon / median gap between decisions, rounded up, within
    [1, n]. Decisions closer than one horizon share part of their outcome window."""
    n = len(ts)
    if n < 2:
        return 1
    gap = percentile([b - a for a, b in zip(ts, ts[1:], strict=False)], 50)
    if not gap or gap <= 0:
        return n
    return max(1, min(n, math.ceil(horizon_s / gap - 1e-9)))


def block_resample(n: int, length: int, rng: random.Random) -> list[int]:
    """One moving-block bootstrap resample of range(n): blocks of `length` consecutive indices
    with uniform random starts, concatenated and cut to n."""
    length = max(1, min(length, n))
    idx: list[int] = []
    while len(idx) < n:
        start = rng.randint(0, n - length)
        idx.extend(range(start, start + length))
    return idx[:n]


def block_bootstrap(
    stat: Callable[[list], float | None],
    groups: Sequence[Sequence],
    block_lens: Sequence[int],
    n_boot: int,
    seed: int,
) -> tuple[float | None, float | None]:
    """95% percentile interval of stat over block-bootstrap resamples. Each group (one run) is
    resampled on its own with its own block length, and the resamples are pooled."""
    rng = random.Random(seed)
    vals = []
    for _ in range(n_boot):
        items: list = []
        for g, length in zip(groups, block_lens, strict=True):
            items.extend(g[i] for i in block_resample(len(g), length, rng))
        v = stat(items)
        if v is not None:
            vals.append(v)
    return percentile(vals, 2.5), percentile(vals, 97.5)


def permutation_pvalue(
    ps: Sequence[float], ys: Sequence[int], n_perm: int, seed: int
) -> tuple[float, float]:
    """(one-sided p-value, mean shuffled Brier): how often the model's own forecasts in random
    order score at least as well as in their real order. Shuffling ignores overlap between
    decisions, so the null is too narrow and a small p-value is weaker evidence than it looks."""
    observed = brier(ps, ys)
    rng = random.Random(seed)
    shuffled = list(ps)
    as_good = 0
    total = 0.0
    for _ in range(n_perm):
        rng.shuffle(shuffled)
        b = brier(shuffled, ys)
        total += b
        if b <= observed + 1e-12:
            as_good += 1
    return (as_good + 1) / (n_perm + 1), total / n_perm


def momentum_signs(
    ts_all: Sequence[float],
    mids_all: Sequence[float],
    ts: Sequence[float],
    mids: Sequence[float],
    lookback_s: float = 30.0,
) -> list[int]:
    """For each decision (ts, mid): the sign of mid minus the latest decision mid at least
    lookback_s older, from the run's decisions (ts_all sorted). 0 if none or unchanged."""
    out = []
    for t, m in zip(ts, mids, strict=True):
        j = bisect.bisect_right(ts_all, t - lookback_s) - 1
        if j < 0:
            out.append(0)
        else:
            d = m - mids_all[j]
            out.append((d > 0) - (d < 0))
    return out


def fit_momentum_k(signs: Sequence[int], ys: Sequence[int]) -> float:
    """Least-squares k for p = 0.5 + k * sign (minimum Brier), clipped to [-0.5, 0.5]."""
    ss = sum(s * s for s in signs)
    if ss == 0:
        return 0.0
    k = sum(s * (y - 0.5) for s, y in zip(signs, ys, strict=True)) / ss
    return max(-0.5, min(0.5, k))


def momentum_probs(signs: Sequence[int], k: float) -> list[float]:
    return [0.5 + k * s for s in signs]


def isotonic_fit(ps: Sequence[float], ys: Sequence[int]) -> list[tuple[float, float]]:
    """Least-squares isotonic fit of y on p (pool adjacent violators). Points with the same p
    are pooled first, weighted by count, so ties always share one value. Returns maximal
    blocks as (lowest p of the block, block value) in increasing p."""
    by_p: dict[float, list[float]] = {}  # p -> [sum y, count]
    for p, y in zip(ps, ys, strict=True):
        acc = by_p.setdefault(p, [0.0, 0])
        acc[0] += y
        acc[1] += 1
    blocks: list[list[float]] = []  # [start p, sum y, count]
    for p in sorted(by_p):
        blocks.append([p, *by_p[p]])
        while len(blocks) > 1 and blocks[-2][1] / blocks[-2][2] >= blocks[-1][1] / blocks[-1][2]:
            _, s, c = blocks.pop()
            blocks[-1][1] += s
            blocks[-1][2] += c
    return [(b[0], b[1] / b[2]) for b in blocks]


def isotonic_predict(model: list[tuple[float, float]], ps: Sequence[float]) -> list[float]:
    """Step function: the value of the last block starting at or below p (the first block's
    value below the fitted range)."""
    starts = [s for s, _ in model]
    out = []
    for p in ps:
        j = max(0, bisect.bisect_right(starts, p) - 1)
        out.append(model[j][1])
    return out


def auc(ps: Sequence[float], ys: Sequence[int]) -> float | None:
    """Area under the ROC curve (Mann-Whitney, ties count half); None with one class only.
    0.5 means the forecasts do not rank ups above downs at all."""
    n1 = sum(ys)
    n0 = len(ys) - n1
    if n1 == 0 or n0 == 0:
        return None
    order = sorted(range(len(ps)), key=lambda i: ps[i])
    rank_sum = 0.0
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and ps[order[j + 1]] == ps[order[i]]:
            j += 1
        avg_rank = (i + j) / 2 + 1
        rank_sum += avg_rank * sum(ys[order[k]] for k in range(i, j + 1))
        i = j + 1
    return (rank_sum - n1 * (n1 + 1) / 2) / (n1 * n0)


def auc_of_pairs(pairs: list[tuple[float, int]]) -> float | None:
    return auc([p for p, _ in pairs], [y for _, y in pairs])


MIN_BLOCKS = 10  # fewer bootstrap blocks than this and the interval is not read


def verdict(lo: float | None, hi: float | None, n_blocks: float) -> str:
    """Read a 95% interval of (forecast Brier - baseline Brier) against zero. With fewer than
    MIN_BLOCKS blocks the resamples barely vary, so the interval says nothing."""
    if lo is None or hi is None or n_blocks < MIN_BLOCKS:
        return "too few samples"
    if hi < 0:
        return "better than baseline"
    if lo > 0:
        return "worse than baseline"
    return "no difference shown"


def gate_sweep(ps: Sequence[float], ys: Sequence[int], gates: Sequence[float]) -> list[dict]:
    """For each gate, the decisions the taker would trade (p >= gate buys, p <= 1 - gate
    sells): count, Brier, hit rate (the side matched the label) and share of up labels."""
    rows = []
    for g in gates:
        sel = []
        for p, y in zip(ps, ys, strict=True):
            if p >= g - GATE_EPS:
                sel.append((p, y, 1))
            elif 1.0 - p >= g - GATE_EPS:
                sel.append((p, y, 0))
        n = len(sel)
        rows.append({
            "gate": g,
            "n": n,
            "n_buy": sum(side for _, _, side in sel),
            "n_sell": sum(1 - side for _, _, side in sel),
            "brier": brier([p for p, _, _ in sel], [y for _, y, _ in sel]),
            "hit_rate": sum(y == side for _, y, side in sel) / n if n else None,
            "up_rate": sum(y for _, y, _ in sel) / n if n else None,
        })
    return rows



# ---------------------------------------------------------------------------------------------
# The runs, and facts recorded outside the databases
# ---------------------------------------------------------------------------------------------

SEED = 20261014  # fixed: bootstrap and permutation results are reproducible
N_BOOT = 2000
N_PERM = 1000
GATES = (0.50, 0.55, 0.60, 0.65, 0.70)
MAKER_TAKER_SPLIT_BPS = 3.0  # fee / notional: maker fills pay 1.50 bps, taker fills 4.50 bps

RUNS = {
    "R1": {"db": "hour.db", "what": "hour run", "strategy": "taker", "model": "mock",
           "where": "Mac"},
    "R2": {"db": "soak.db", "what": "real-model run", "strategy": "taker", "model": "decider",
           "where": "Mac, SSH tunnel to the GPU node"},
    "R3": {"db": "overnight.db", "what": "unattended run", "strategy": "taker",
           "model": "decider", "where": "VM"},
    "R4": {"db": "maker30.db", "what": "maker paper run", "strategy": "maker", "model": "mock",
           "where": "Mac"},
    "R5": {"db": "maker_live.db", "what": "maker live run, killed by the M9 bug",
           "strategy": "maker", "model": "decider", "where": "VM"},
    "R6": {"db": "maker_live2.db", "what": "maker live run", "strategy": "maker",
           "model": "decider", "where": "VM, model relayed through the Mac"},
}
LIVE = ("R1", "R2", "R3", "R5", "R6")  # real testnet orders (R4 is paper)
CALIBRATION = ("R3", "R6")  # the plan's calibration runs; "pooled" is both together
EXTRA_CALIBRATION = ("R2", "R5")  # the other real-model runs, shown for completeness only

# The exchange side of each reconcile, as recorded at the time (PIPELINE.md log, or the saved
# run report). None: not recorded.
RECONCILE = {
    "R1": {"fills": 48, "fees": 0.223041, "closed_pnl": -0.0536, "position": 0.0,
           "open_orders": None, "source": "PIPELINE.md log, 2026-10-08"},
    "R2": {"fills": 114, "fees": None, "closed_pnl": None, "position": None,
           "open_orders": None,
           "source": "PIPELINE.md log, 2026-10-09 (only \"reconcile OK (114 fills)\" was noted)"},
    "R3": {"fills": 114, "fees": 0.550461, "closed_pnl": -0.0478, "position": 0.0,
           "open_orders": None, "source": "PIPELINE.md log, 2026-10-10"},
    "R5": {"fills": 6, "fees": 0.009694, "closed_pnl": -0.0101, "position": 0.0,
           "open_orders": 0, "source": "PIPELINE.md log, 2026-10-11"},
    "R6": {"fills": 37, "fees": 0.062239, "closed_pnl": -0.0287, "position": 0.0,
           "open_orders": 0, "source": ".pipeline/runs/m9-live-maker-1h.txt (saved output)"},
}
LOSS_DRILL = {"fills": 9, "fees": 0.043794, "closed_pnl": 0.0214, "position": 0.0,
              "open_orders": None, "source": "PIPELINE.md log, 2026-10-09"}

# Slurm sacct for the GPU jobs (1 GH200 each), copied from .pipeline/compute-raw.md.
GPU_JOBS = [
    ("7144550", "2026-10-08", "00:02:13", "completed", "first check of the decider (17 requests)"),
    ("7192935", "2026-10-09 14:07", "00:01:30", "cancelled", "false start"),
    ("7192991", "2026-10-09 14:08", "04:30:02", "timeout",
     "short-deadline check + R2 real-model taker run (soak.db)"),
    ("7232911", "2026-10-10 15:13", "02:07:40", "cancelled at end",
     "VM smoke + R3 2 h unattended taker run (overnight.db)"),
    ("7253212", "2026-10-11 17:22", "00:22:24", "cancelled at end",
     "maker paper smoke + R5 live maker run (maker_live.db)"),
    ("7388978", "2026-10-13 22:20", "01:03:42", "cancelled at end",
     "R6 1 h live maker run (maker_live2.db)"),
]

GENERATED = "<!-- Generated by scripts/evidence.py from the run databases. Do not edit. -->\n"
OVERLAP = (
    "Overlap: consecutive decisions share most of their outcome window (a taker decision "
    "every ~5.3 s against a 60 s horizon shares it with about 11 neighbours; the maker scores "
    "one decision per changed book, about one every 1-2 s). Sample counts below are decisions, "
    "not independent samples; the \"windows\" column (scored time span / horizon) is a rough "
    "count of non-overlapping outcome windows. Longer horizons overlap more."
)


# ---------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------


def num(x: float | None, nd: int = 4) -> str:
    return "n/a" if x is None else f"{x:.{nd}f}"


def usd(x: float | None, nd: int = 4) -> str:
    if x is None:
        return "n/a"
    return f"-${-x:.{nd}f}" if x < 0 else f"${x:.{nd}f}"


def pct(x: float | None, nd: int = 1) -> str:
    return "n/a" if x is None else f"{100 * x:.{nd}f}%"


def utc(ts: float) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def table(headers: Sequence[str], rows: Sequence[Sequence]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def open_ro(path: Path) -> Store:
    """A Store over a read-only connection. Store() itself would write (schema, WAL pragma)."""
    if not path.exists():
        raise SystemExit(f"error: {path} not found")
    st = Store.__new__(Store)
    st._db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    st._db.row_factory = sqlite3.Row
    if st.schema_version() != SCHEMA_VERSION:
        raise SystemExit(f"error: {path} is schema v{st.schema_version()}, not v{SCHEMA_VERSION}")
    return st


def events(store: Store, kind: str) -> list[sqlite3.Row]:
    return [e for e in store.events() if e["kind"] == kind]


class Scored:
    """Scored decisions at one horizon (the rows of Store.scored, with their ts and mid)."""

    def __init__(self, store: Store, horizon_s: int):
        rows = store._db.execute(
            "SELECT d.ts, d.mid, d.p_up, o.label FROM decisions d"
            " JOIN outcomes o ON o.decision_id = d.id"
            " WHERE o.horizon_s = ? AND o.label IS NOT NULL AND d.p_up IS NOT NULL"
            " ORDER BY d.ts, d.id",
            (horizon_s,),
        ).fetchall()
        self.ts = [r["ts"] for r in rows]
        self.mids = [r["mid"] for r in rows]
        self.ps = [r["p_up"] for r in rows]
        self.ys = [r["label"] for r in rows]
        if list(zip(self.ps, self.ys, strict=True)) != store.scored(horizon_s):
            raise RuntimeError("scored rows differ from Store.scored")
        self.horizon_s = horizon_s
        self.block = block_len(self.ts, horizon_s)
        self.diffs = brier_diffs(self.ps, self.ys)

    def windows(self) -> float:
        return (self.ts[-1] - self.ts[0]) / self.horizon_s if len(self.ts) > 1 else 0.0


def fill_split(fills: Sequence) -> dict:
    """Count, fees, notional and fee rate of maker and taker fills, told apart by fee rate."""
    out = {}
    for name in ("maker", "taker"):
        fs = [f for f in fills
              if (f["fee"] / (f["sz"] * f["px"]) * 1e4 < MAKER_TAKER_SPLIT_BPS)
              == (name == "maker")]
        notional = sum(f["sz"] * f["px"] for f in fs)
        fees = sum(f["fee"] for f in fs)
        out[name] = {"n": len(fs), "fees": fees, "notional": notional,
                     "bps": fees / notional * 1e4 if notional else None}
    return out


def last_mid(store: Store) -> float | None:
    rows = store.recent_decisions(1)
    return rows[0]["mid"] if rows else None


# ---------------------------------------------------------------------------------------------
# E1 latency
# ---------------------------------------------------------------------------------------------


def e1_latency(stores: dict[str, Store]) -> tuple[str, str]:
    rows = []
    for key in list(RUNS) + ["short"]:
        r = latency_report(stores[key])
        info = RUNS.get(key, {"what": "deadline drill (short.db)", "strategy": "taker",
                              "model": "see note", "where": "Mac"})
        late = f"{r['late_holds']} ({pct(r['late_holds'] / r['decisions'], 2)})"
        rows.append([f"{key} {info['what']}", info["strategy"], info["model"], info["where"],
                     r["decisions"], r["n"], num(r["p50"], 1), num(r["p90"], 1),
                     num(r["p99"], 1), num(r["max"], 1), late, num(r["gap_p50_s"], 2)])
    maker_rows = []
    for key in ("R4", "R5", "R6"):
        ev_rows = events(stores[key], "maker_latency")
        last = ev_rows[-1]
        s = json.loads(last["detail"])
        end = stores[key].decisions()[-1]["ts"]

        def cell(d: dict) -> str:
            return f"{num(d['p50'], 0)} / {num(d['p90'], 0)} / {num(d['max'], 0)} (n={d['n']})"

        maker_rows.append([key, cell(s["rest"]), cell(s["action"]), cell(s["loop"]),
                           f"{end - last['ts']:.0f} s"])
    r3, r6 = latency_report(stores["R3"]), latency_report(stores["R6"])
    md = GENERATED + f"""# E1 Latency, from the bot's own logs

Model latency is the time the bot measured around each model call (for the decider: the HTTP
round trip, including the SSH tunnel or relay). A late hold is a decision whose answer missed
the deadline (taker 2 s, maker 1 s); the bot held instead of trading. Gap is the time between
consecutive decisions.

{table(["Run", "Strategy", "Model", "Where", "Decisions", "Model calls", "p50 ms", "p90 ms",
        "p99 ms", "max ms", "Late holds", "Gap p50 s"], rows)}

Maker order path (ms, p50 / p90 / max over the last up to 3600 samples, from the last
`maker_latency` event the run logged; that event is logged once a minute, so it stops short
of the run's end by the time in the last column):

{table(["Run", "REST book read", "Order action (place/modify/cancel)", "Book to action",
        "Before end"], maker_rows)}

Notes:
- R1, R4: the mock model runs in-process, so their "model latency" is the mock's compute
  time, not a network round trip. The short-deadline drill's database does not record which
  model answered (the plan lists the drills as mock runs); every call was cut at the very
  short deadline, so its latencies say nothing about the model.
- R4 is a paper run: its order actions are simulated in-process, not exchange round trips.
- R2 reached the GPU node from the Mac through an SSH tunnel; R3 and R5 from the VM; R6 from
  the VM through a relay on the Mac (the VM could not authenticate to the cluster that day),
  which adds a hop.
- R6's end-of-run report (`.pipeline/runs/m9-live-maker-1h.txt`) covers the final seconds as
  well: REST p50 227.1 ms (n=3600), action p50 633.1 ms (n=224), loop p50 199.7 ms (n=227).
"""
    headline = (f"Taker on the VM (R3): model p50 {r3['p50']:.1f} ms, p99 {r3['p99']:.1f} ms, "
                f"{r3['late_holds']} late of {r3['decisions']}; maker R6 via the Mac relay: "
                f"p50 {r6['p50']:.1f} ms, {r6['late_holds']} late of {r6['decisions']}.")
    return md, headline


# ---------------------------------------------------------------------------------------------
# E2 reconciliation
# ---------------------------------------------------------------------------------------------


def _agree(db: float | int, rec: float | int | None, tol: float) -> str:
    if rec is None:
        return "not recorded"
    return "yes" if abs(db - rec) <= tol else "NO"


def _ledger_row(name: str, fills: Sequence, rec: dict) -> list:
    lg = Ledger.from_fills(fills)
    checks = [
        _agree(len(fills), rec["fills"], 0),
        _agree(lg.fees, rec["fees"], 5e-7),
        _agree(round(lg.realized, 4), rec["closed_pnl"], 5e-5),
        _agree(lg.position, rec["position"], 1e-9),
    ]
    oo = "not recorded" if rec["open_orders"] is None else str(rec["open_orders"])
    return [name, len(fills), f"${lg.fees:.6f}", usd(lg.realized), f"{lg.position:g}",
            " / ".join(checks), oo, rec["source"]]


def e2_reconcile(stores: dict[str, Store]) -> tuple[str, str]:
    rows = []
    for key in LIVE:
        rows.append(_ledger_row(f"{key} {RUNS[key]['what']}", stores[key].fills(), RECONCILE[key]))
    drill = stores["drill"]
    kills = events(drill, "kill_switch")
    first_kill = kills[0]["ts"]
    net_fills = [f for f in drill.fills() if f["ts"] < first_kill]
    net_lg = Ledger.from_fills(net_fills)
    loss_in_db = any(k["detail"] == "daily_loss" for k in kills)
    if loss_in_db:
        rows.append(_ledger_row("Loss-limit drill (drill.db, whole file)", drill.fills(),
                                LOSS_DRILL))
    else:
        rows.append(["Loss-limit drill", "not in runs/drill.db", f"${LOSS_DRILL['fees']:.6f}*",
                     usd(LOSS_DRILL["closed_pnl"]) + "*", "0*", "log only", "not recorded",
                     LOSS_DRILL["source"]])
    rows.append(["Network-loss drill (drill.db, before the kill)", len(net_fills),
                 f"${net_lg.fees:.6f}", usd(net_lg.realized), f"{net_lg.position:g}",
                 "not reconciled", "n/a",
                 "flatten failed with DNS down; the short was closed by hand in the UI (log)"])
    bad = [r for r in rows if "NO" in str(r[5])]
    md = GENERATED + f"""# E2 Reconciliation: local ledger against the exchange

After each live run `jevhl.reconcile` compared every stored fill with the exchange's own
`userFills` by trade id, the ledger's position with the exchange's, and the ledger's realized
P&L (before fees) with the sum of the exchange's `closedPnl`. The reconcile output was not
saved to the databases, so the exchange figures here are the ones recorded at the time
(source column). The local columns are recomputed now from the fills in each database; the
"agrees" column checks them against the recorded figures (fills / fees / realized P&L /
position; `not recorded` where the record does not give that figure).

{table(["Run", "Fills (DB)", "Fees (DB)", "Realized before fees (DB)", "End position BTC (DB)",
        "Agrees with record", "Open orders after", "Exchange figures from"], rows)}

Every recorded reconcile said `OK: local and exchange agree`. Taker runs use IOC orders only,
so nothing rests on the book; their open-order count was not written down.
"""
    if not loss_in_db:
        md += """
\\* The loss-limit drill's rows are not in `runs/drill.db`: that copy holds only the first
(network-loss) session. The starred figures are the PIPELINE.md log's, not recomputed.
"""
    md += """
The network-loss drill (2026-10-08, before M6) is the one run that did not end flat: the
stale-book kill fired but both flatten attempts failed while DNS was down, leaving the short
above open. It is the reason M6 (flatten retry) exists; see E3.
"""
    agree = ("the databases agree with every recorded figure" if not bad else
             f"{len(bad)} row(s) DISAGREE with the recorded figures")
    headline = (f"All {len(LIVE)} live runs reconciled OK when they were run (fills, fees, "
                f"position and closedPnl matched the exchange); {agree}.")
    return md, headline


# ---------------------------------------------------------------------------------------------
# E3 safety drills
# ---------------------------------------------------------------------------------------------


def e3_drills(stores: dict[str, Store]) -> tuple[str, str]:
    drill, short, r5, r6 = stores["drill"], stores["short"], stores["R5"], stores["R6"]
    kills = events(drill, "kill_switch")
    stale_kill = next(k for k in kills if k["detail"] == "stale_book")
    loss_kill = next((k for k in kills if k["detail"] == "daily_loss"), None)
    before = [d for d in drill.decisions() if d["ts"] <= stale_kill["ts"]]
    last_book = before[-1]["ts"]
    fills_before = [f for f in drill.fills() if f["ts"] < stale_kill["ts"]]
    late_fills = [f for f in fills_before if f["ts"] > last_book]
    pos = Ledger.from_fills(fills_before).position
    n_flat_fail = sum(1 for e in events(drill, "flatten_failed") if e["ts"] < (
        loss_kill["ts"] if loss_kill else float("inf")))

    if loss_kill:
        loss_fills = [f for f in drill.fills() if f["ts"] >= loss_kill["ts"]]
        loss_evidence = (f"DB (drill.db): `kill_switch daily_loss` at {utc(loss_kill['ts'])}, "
                         f"{len(loss_fills)} closing fill(s) after it, "
                         f"end position {Ledger.from_fills(drill.fills()).position:g}")
    else:
        loss_evidence = ("**Log only** (PIPELINE.md 2026-10-09). This session is not in "
                         "`runs/drill.db`, which holds only the first session")

    sd = short.decisions()
    sd_late = sum(1 for d in sd if d["reason"] == "late")
    sd_p = sum(1 for d in sd if d["p_up"] is not None)

    r5_kill = events(r5, "kill_switch")[-1]
    r5_cancels = [e for e in events(r5, "quote_cancel") if e["ts"] >= r5_kill["ts"]]
    r5_pos = Ledger.from_fills(r5.fills()).position
    r6_cancels = [e for e in events(r6, "quote_cancel") if e["detail"].startswith("stop")]
    r6_split = fill_split(r6.fills())

    soak = stores["R2"]
    soak_kill = events(soak, "kill_switch")[-1]
    soak_last = soak.decisions()[-1]["ts"]
    pauses = {k: len(events(stores[k], "stale_pause")) for k in ("R3", "R5", "R6")}

    rows = [
        ["Loss-limit kill", "Live testnet, mock model, `MAX_DAILY_LOSS_USD=0.02`",
         "Kill `daily_loss`, cancelled and flattened, stopped; reconcile OK (9 fills, fees "
         "$0.043794, closedPnl $0.0214 both sides)", loss_evidence],
        ["Network loss (accidental, before M6)",
         "DNS failed about 2 min into the first loss-limit drill attempt (not planned)",
         f"No decision after the last book ({utc(last_book)}); stale-book kill at "
         f"{utc(stale_kill['ts'])}; {len(late_fills)} fills after the last book; "
         f"{n_flat_fail} flatten attempts failed (DNS), leaving {pos:g} BTC open; the bot "
         "reported the position as unknown",
         "DB (drill.db); the hand close in the UI is from the log"],
        ["Flatten retry after the network returns (M6)", "**Not drilled live**",
         "Covered by tests only (e.g. `test_seconds_does_not_cut_a_flatten_retry_short`); "
         "the milestone table still says \"live check pending\"", "Tests; PIPELINE.md"],
        ["Stale feed pause and resume (M7)", "**Not drilled live**",
         f"The incident behind it: R2 stopped on `kill_switch stale_book` at "
         f"{utc(soak_kill['ts'])} (last decision {utc(soak_last)}), before M7. After M7, "
         f"stale pauses logged: R3 {pauses['R3']}, R5 {pauses['R5']}, R6 {pauses['R6']}, so "
         "the pause/resume path never ran live",
         "DB (soak.db, overnight.db, maker_live*.db); tests"],
        ["Short decider deadline", "Live, deadline set very short",
         f"{sd_late} of {len(sd)} decisions were late holds, {len(short.fills())} fills; "
         f"{sd_p} late answers stored as probabilities (late answers are never scored)",
         "DB (short.db)"],
        ["Maker kill switch cancels resting orders", "Not planned: R5's kill was the M9 bug",
         f"`kill_switch {r5_kill['detail']}` at {utc(r5_kill['ts'])}; then "
         + "; ".join(f"`{e['detail']}`" for e in r5_cancels)
         + f"; end position {r5_pos:g}; 0 open orders after (reconcile, log)",
         "DB (maker_live.db) and log"],
        ["Maker clean shutdown", "R6 stopped at the end of its hour",
         "; ".join(f"`{e['detail']}` at {utc(e['ts'])}" for e in r6_cancels)
         + f"; {r6_split['taker']['n']} taker fill (the shutdown flatten); 0 open orders and "
         "flat (saved reconcile)", "DB (maker_live2.db) and saved report"],
    ]
    md = GENERATED + f"""# E3 Safety drills

What was done, what the bot did, and where the evidence is. "DB" means the run's database in
`runs/` shows it; "log" means only the PIPELINE.md log (written at the time) does.

{table(["Drill", "What was done", "What the bot did", "Evidence"], rows)}

Gaps, said plainly:
- The wifi-off drill in PROBLEM.md was never done on purpose. The only network-loss evidence
  is the accidental DNS failure above, which happened before the M6 flatten retry and the M7
  stale pause/resume existed. Neither fix has been exercised live.
- The maker kill-switch evidence comes from a real bug (R5), not a planned drill.
- Database times are UTC; the PIPELINE.md log dates some evenings by local date (the drill
  sessions on 2026-10-08 UTC are logged under 2026-10-09).
"""
    headline = ("Loss-limit kill, short deadline and maker cancel-on-kill worked live; the "
                "network-loss evidence is one accidental pre-M6 outage, and the M6 retry and "
                "M7 pause/resume were never drilled live.")
    return md, headline


# ---------------------------------------------------------------------------------------------
# E4 unattended run
# ---------------------------------------------------------------------------------------------


def _rss_series(store: Store) -> list[tuple[float, float]]:
    """(minutes since the first sample, MB = 1000 kB, as in the run reports) for the run's rss
    events: one at start, one a minute, one at the end."""
    pts = []
    for e in events(store, "rss"):
        try:
            kb = float(e["detail"])
        except ValueError:
            continue
        pts.append((e["ts"], kb / 1000))
    t0 = pts[0][0]
    return [((t - t0) / 60, mb) for t, mb in pts]


def _quarter_means(xs: Sequence[float]) -> list[float]:
    q = len(xs) / 4
    return [mean(xs[round(i * q):round((i + 1) * q)]) for i in range(4)]


def _in_run_growth(series: list[tuple[float, float]]) -> tuple[float, float]:
    """(MB, MB per hour) from the first per-minute sample to the last one before shutdown,
    leaving out the start sample (before warm-up) and the end sample (during shutdown)."""
    (t1, m1), (t2, m2) = series[1], series[-2]
    return m2 - m1, (m2 - m1) / ((t2 - t1) / 60)


def e4_unattended(stores: dict[str, Store], out: Path) -> tuple[str, str]:
    r3 = stores["R3"]
    s = soak_report(r3)
    series = _rss_series(r3)
    mbs = [mb for _, mb in series]
    quarters = _quarter_means(mbs)
    grow, rate = _in_run_growth(series)
    kinds: dict[str, int] = {}
    for e in r3.events():
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    bad_kinds = [k for k in kinds if any(w in k for w in ("error", "kill", "stale", "flatten"))]
    decs = r3.decisions()
    stuck = s["hold_rate"] in (0.0, 1.0)
    rss_points = " / ".join(f"{mbs[i]:.1f}" for i in (0, 1, -2, -1))
    r6_series = _rss_series(stores["R6"])
    r6_grow, r6_rate = _in_run_growth(r6_series)
    _plot_rss({"R3 unattended taker run (VM, 2 h)": series,
               "R6 live maker run (VM, 1 h), for comparison": r6_series},
              out / "rss.png")
    md = GENERATED + f"""# E4 Unattended run (R3)

R3: taker, real model, live testnet, on the VM, {utc(decs[0]['ts'])} to
{utc(decs[-1]['ts'])}. It was a 2-hour run, not overnight: the user decided on 2026-10-10 that
2 hours unattended was enough for now.

| Measure | R3 |
|---|---|
| Decisions | {s['decisions']} |
| Hold rate | {pct(s['hold_rate'])} ({"stuck" if stuck else "not stuck at 0% or 100%"}) |
| Late-hold rate | {pct(s['late_rate'], 2)} |
| RSS samples | {len(mbs)} (at start, once a minute, at the end) |
| RSS at start / 1 min / last in-run / at shutdown | {rss_points} MB |
| RSS mean by quarter of the run | {" / ".join(f"{q:.1f}" for q in quarters)} MB |
| RSS growth, 1 min to last in-run sample | {grow:+.2f} MB ({rate:+.2f} MB an hour) |
| Error, kill, stale or flatten events | {sum(kinds[k] for k in bad_kinds)} |
| Websocket reconnects | {kinds.get('disconnect', 0)} (server-side "Expired" closes) |
| Orders skipped by the bot's own 2-a-minute limit | {kinds.get('order_skipped', 0)} |

MB here is 1000 kB, as in the run reports. Memory rose in small steps through the whole run
and had not visibly levelled off by the end; two hours cannot tell a slow leak of this size
from warm-up. It is small: at this rate a 10-hour run would add about {10 * rate:.0f} MB.

The process stopped cleanly on Ctrl-C at the end (exit 130); that is from the PIPELINE.md log,
since the database cannot record its own exit.

![RSS over time](rss.png)

For comparison, R6 (maker, 1 h, not an unattended-run test) went from {r6_series[1][1]:.1f} MB
at 1 minute to {r6_series[-2][1]:.1f} MB before shutdown ({r6_grow:+.2f} MB, {r6_rate:+.2f} MB
an hour), a faster rise than R3's.
"""
    headline = (f"R3 ran 2 h unattended (not overnight) with 0 error events; RSS rose "
                f"{grow:+.1f} MB ({rate:+.2f} MB an hour, not levelled off); hold rate "
                f"{pct(s['hold_rate'])}.")
    return md, headline


def _plot_rss(series: dict[str, list[tuple[float, float]]], path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(len(series), 1, figsize=(7, 2.6 * len(series)), squeeze=False)
    for ax, (title, pts) in zip(axes[:, 0], series.items(), strict=True):
        ax.plot([t for t, _ in pts], [mb for _, mb in pts], marker=".", lw=1)
        ax.set_title(title, fontsize=10)
        ax.set_xlabel("minutes since the first sample")
        ax.set_ylabel("RSS (MB)")
        ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=100, metadata={"Software": None})
    plt.close(fig)


# ---------------------------------------------------------------------------------------------
# E5 calibration
# ---------------------------------------------------------------------------------------------


def calib_row(name: str, parts: Sequence[Scored]) -> dict:
    """Brier, running baseline (per run, each starting at 0.5) and the block-bootstrap 95%
    interval of Brier minus baseline, for one run or several pooled."""
    ps = [p for s in parts for p in s.ps]
    ys = [y for s in parts for y in s.ys]
    diffs = [d for s in parts for d in s.diffs]
    base = sum(baseline_brier_running(s.ys) * len(s.ys) for s in parts if s.ys) / len(ys)
    lo, hi = block_bootstrap(mean, [s.diffs for s in parts], [s.block for s in parts],
                             N_BOOT, SEED)
    n_blocks = sum(len(s.ys) / s.block for s in parts)
    return {"name": name, "n": len(ys), "windows": sum(s.windows() for s in parts),
            "blocks": "+".join(str(s.block) for s in parts), "n_blocks": n_blocks,
            "mean_p": mean(ps), "up_rate": mean(ys), "brier": brier(ps, ys), "baseline": base,
            "diff": mean(diffs), "lo": lo, "hi": hi, "verdict": verdict(lo, hi, n_blocks),
            "bins": reliability_bins(ps, ys)}


def e5_calibration(stores: dict[str, Store], out: Path) -> tuple[str, str, dict]:
    sc = {(k, h): Scored(stores[k], h) for k in CALIBRATION + EXTRA_CALIBRATION
          for h in HORIZONS_S}
    rows, extra, results = [], [], {}
    for h in HORIZONS_S:
        for name, keys in (("R3", ("R3",)), ("R6", ("R6",)), ("pooled R3+R6", CALIBRATION)):
            results[(name, h)] = calib_row(name, [sc[(k, h)] for k in keys])
        for k in EXTRA_CALIBRATION:
            results[(k, h)] = calib_row(k, [sc[(k, h)]])

    def line(r: dict, h: int) -> list:
        return [r["name"], f"{h} s", r["n"], f"{r['windows']:.0f}",
                f"{r['blocks']} ({r['n_blocks']:.0f})",
                num(r["mean_p"], 3), num(r["up_rate"], 3), num(r["brier"]), num(r["baseline"]),
                f"{r['diff']:+.4f}", f"[{r['lo']:+.4f}, {r['hi']:+.4f}]", r["verdict"]]

    heads = ["Run", "Horizon", "n scored", "Windows", "Block length (blocks)", "Mean p_up",
             "Up rate", "Brier", "Baseline", "Brier - baseline", "95% block bootstrap",
             "Result"]
    for h in HORIZONS_S:
        rows += [line(results[(n, h)], h) for n in ("R3", "R6", "pooled R3+R6")]
        extra += [line(results[(k, h)], h) for k in EXTRA_CALIBRATION]

    bin_sections = []
    for name in ("R3", "R6", "pooled R3+R6"):
        slug = name.split()[0]
        _plot_reliability(name, {h: results[(name, h)]["bins"] for h in HORIZONS_S},
                          out / f"reliability_{slug}.png")
        bin_rows = []
        for i in range(10):
            cells = [f"{i / 10:.1f}-{(i + 1) / 10:.1f}"]
            for h in HORIZONS_S:
                b = results[(name, h)]["bins"][i]
                cells += [b.count, num(b.mean_p, 3), num(b.freq, 3)]
            bin_rows.append(cells)
        heads_b = ["p_up bin"] + [f"{c} {h} s" for h in HORIZONS_S
                                  for c in ("n", "mean p", "freq up")]
        bin_sections.append(f"### {name}\n\n![reliability {name}](reliability_{slug}.png)\n\n"
                            + table(heads_b, bin_rows))
    main_rows = {(n, h): r for (n, h), r in results.items()
                 if n in ("R3", "R6", "pooled R3+R6")}
    verdicts = {r["verdict"] for r in main_rows.values()}

    def which(v: str, rs: dict) -> str:
        cells = [f"{n} {h} s" for (n, h), r in rs.items() if r["verdict"] == v]
        return ", ".join(cells) if cells else "none"

    extra_rows = {(n, h): r for (n, h), r in results.items() if n in EXTRA_CALIBRATION}
    better = which("better than baseline", results)
    summary = (
        f"the model was better than the baseline in: {better}. Worse in: "
        f"{which('worse than baseline', main_rows)} (calibration set) and "
        f"{which('worse than baseline', extra_rows)} (other runs). Too few samples to read: "
        f"{which('too few samples', results)}. Everywhere else: no difference shown."
    )
    p60 = results[("pooled R3+R6", 60)]
    md = GENERATED + f"""# E5 Calibration (real-model runs)

Every decision is scored, traded or not: label 1 if the mid rose over the horizon, 0 if it
fell, ties dropped. Brier and the baseline are computed with `jevhl.calibration`, as on the
dashboard. The baseline is the running base rate: at each decision, the share of up labels
seen so far in that run (0.5 at the start), so it never sees the future. A model with skill
has a lower Brier than the baseline.

{OVERLAP}

The interval is a moving-block bootstrap ({N_BOOT} resamples, seed {SEED}) of the
per-decision Brier minus baseline, with blocks one horizon long in decisions (horizon /
median gap between scored decisions, rounded up); pooled resamples each run on its own.
"Result" reads the interval: below zero is better than the baseline, above zero worse, across
zero no difference shown. With fewer than {MIN_BLOCKS} blocks (the number in brackets:
decisions / block length) the resamples barely vary and the row says too few samples. The
pooled baseline runs within each run, so pooled Brier minus baseline is the decision-weighted
mean of the two runs.

{table(heads, rows)}

Other real-model runs, not part of the plan's calibration set (R2 ended early on the stale-
book stop; R5 lasted 14 minutes), for completeness:

{table(heads, extra)}

Read plainly: {summary} With these sample sizes (a few hundred non-overlapping windows at
most) a small real edge could not be seen either way; PROBLEM.md expected a few thousand
decisions to be needed, and the extra run that would have added more (plan item E7) was not
done.

The model leaned down: pooled at 60 s its mean `p_up` was {p60['mean_p']:.3f} while
{p60['up_rate']:.3f} of the labels were up (per run in the table). On 2026-10-08, with a
placeholder prompt, the decider had leaned up instead (PIPELINE.md); E6 tests whether
recalibration removes the lean.

## Reliability (10 equal bins; n = decisions in the bin)

A calibrated model's points sit on the diagonal. Bins with few decisions are noise.

{chr(10).join(bin_sections)}
"""
    lead = ("No skill shown on Brier" if "better than baseline" not in verdicts
            else "Mixed")
    headline = (f"{lead}: pooled 60 s Brier {p60['brier']:.4f} vs baseline "
                f"{p60['baseline']:.4f} (n={p60['n']}, overlapping; 95% interval of the "
                f"difference [{p60['lo']:+.4f}, {p60['hi']:+.4f}]); results across runs and "
                f"horizons: {', '.join(sorted(verdicts))}.")
    return md, headline, sc


def _plot_reliability(name: str, bins_by_h: dict, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, len(bins_by_h), figsize=(4 * len(bins_by_h), 4.2))
    for ax, (h, bins) in zip(axes, bins_by_h.items(), strict=True):
        ax.plot([0, 1], [0, 1], color="grey", lw=1, ls="--")
        full = [b for b in bins if b.count]
        ax.plot([b.mean_p for b in full], [b.freq for b in full], marker="o", lw=1)
        for b in full:
            ax.annotate(str(b.count), (b.mean_p, b.freq), textcoords="offset points",
                        xytext=(4, 4), fontsize=7)
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.set_aspect("equal")
        ax.set_title(f"{name}, {h} s (n={sum(b.count for b in bins)})", fontsize=10)
        ax.set_xlabel("mean p_up in bin")
        ax.set_ylabel("observed frequency of up")
        ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(path, dpi=100, metadata={"Software": None})
    plt.close(fig)


# ---------------------------------------------------------------------------------------------
# E6 baselines and ablations
# ---------------------------------------------------------------------------------------------


def _vs_base(fc: Sequence[float], base: Sequence[float], ys: Sequence[int], block: int) -> dict:
    """Brier of a forecast minus the base-rate forecast's, on the same decisions, with its
    block-bootstrap interval and verdict."""
    d = [(f - y) ** 2 - (b - y) ** 2 for f, b, y in zip(fc, base, ys, strict=True)]
    lo, hi = block_bootstrap(mean, [d], [block], N_BOOT, SEED)
    return {"diff": mean(d), "lo": lo, "hi": hi, "verdict": verdict(lo, hi, len(d) / block)}


def _diff_cell(r: dict) -> str:
    return f"{r['diff']:+.4f} [{r['lo']:+.4f}, {r['hi']:+.4f}] {r['verdict']}"


def e6_baselines(stores: dict[str, Store], sc: dict) -> tuple[str, str]:
    test_rows, diff_rows, perm_rows, auc_rows = [], [], [], []
    beat_base: list[str] = []  # forecast and cell where the interval is below zero
    auc_above: list[str] = []
    for k in CALIBRATION:
        decs = stores[k].decisions()
        ts_all = [d["ts"] for d in decs]
        mids_all = [d["mid"] for d in decs]
        for h in HORIZONS_S:
            s = sc[(k, h)]
            half = len(s.ps) // 2
            tr, te = slice(0, half), slice(half, None)
            ys_te = s.ys[te]
            base_te = running_baseline_probs(s.ys)[te]
            signs = momentum_signs(ts_all, mids_all, s.ts, s.mids)
            kk = fit_momentum_k(signs[tr], s.ys[tr])
            mom_te = momentum_probs(signs[te], kk)
            iso_te = isotonic_predict(isotonic_fit(s.ps[tr], s.ys[tr]), s.ps[te])
            test_rows.append([k, f"{h} s", half, len(ys_te), num(brier(s.ps[te], ys_te)),
                              "0.2500", num(brier(base_te, ys_te)),
                              f"{num(brier(mom_te, ys_te))} (k={kk:+.3f})",
                              num(brier(iso_te, ys_te))])
            cmp = {name: _vs_base(fc, base_te, ys_te, s.block) for name, fc in
                   (("model", s.ps[te]), ("B3 momentum", mom_te), ("A2 recalibrated", iso_te))}
            beat_base += [f"{name} {k} {h} s" for name, r in cmp.items()
                          if r["verdict"] == "better than baseline"]
            diff_rows.append([k, f"{h} s", f"{len(ys_te) / s.block:.0f}"]
                             + [_diff_cell(r) for r in cmp.values()])
            p_val, null = permutation_pvalue(s.ps, s.ys, N_PERM, SEED)
            perm_rows.append([k, f"{h} s", len(s.ps), num(brier(s.ps, s.ys)), num(null),
                              f"{p_val:.3f}"])
            a = auc(s.ps, s.ys)
            lo, hi = block_bootstrap(auc_of_pairs, [list(zip(s.ps, s.ys, strict=True))],
                                     [s.block], N_BOOT, SEED)
            n_blocks = len(s.ps) / s.block
            if lo is not None and lo > 0.5 and n_blocks >= MIN_BLOCKS:
                auc_above.append(f"{k} {h} s ({a:.3f})")
            auc_rows.append([k, f"{h} s", len(s.ps), f"{n_blocks:.0f}", num(a, 3),
                             f"[{lo:.3f}, {hi:.3f}]", num(auc(s.ps[te], ys_te), 3)])
    s3 = {h: sc[("R3", h)] for h in HORIZONS_S}
    gate_rows = []
    for h in HORIZONS_S:
        for g in gate_sweep(s3[h].ps, s3[h].ys, GATES):
            gate_rows.append([f"{h} s", f"{g['gate']:.2f}", g["n"], g["n_buy"], g["n_sell"],
                              num(g["brier"]), pct(g["hit_rate"]), pct(g["up_rate"])])
    g60 = {g["gate"]: g for g in gate_sweep(s3[60].ps, s3[60].ys, GATES)}
    tail_better = g60[0.60]["brier"] < g60[0.50]["brier"]
    r6w = sc[("R6", 60)].windows()
    auc_note = "" if not auc_above else (
        "What this does and does not show: where the AUC interval sits above 0.5, the model's "
        "forecasts ordered ups above downs better than chance in that run, even though its "
        "Brier was no better than the base rate (its probabilities leaned down; see E5), and "
        "recalibrating them on the first half did not give an interval below the base rate on "
        f"the second. R6 is one hour (about {r6w:.0f} non-overlapping 60 s windows) on testnet, "
        "and this analysis was chosen after seeing the run-level Brier results. It is a lead "
        "to test on new runs, not a demonstrated edge. It does not say where the ordering "
        "comes from; short-lived effects of a thin testnet book are one possibility not "
        "examined here.")
    md = GENERATED + f"""# E6 Baselines and ablations (offline, from the stored forecasts)

Computed from the stored `p_up`, decision mids and outcomes of the two calibration runs (R3
taker, R6 maker). {OVERLAP}

## Out-of-sample comparison (fit on the first half of each run, scored on the second)

- **Model**: the decider's stored `p_up`.
- **B1 constant 0.5**: Brier 0.25 by construction (no information).
- **B2 running base rate**: the dashboard baseline, running from the start of the run.
- **B3 momentum**: `p_up = 0.5 + k * sign(mid now - mid at least 30 s earlier)`, using the
  run's own decision mids; k fitted by least squares on the first half (clipped to +-0.5).
- **A2 isotonic recalibration** of the model's `p_up`, fitted on the first half. If it fixes
  the score, the problem was calibration; if not, there is no ranking to recalibrate.

Lower is better. The first-half labels at the split overlap the second half by up to one
horizon, a small leak in favour of the fitted rows (B3, A2).

{table(["Run", "Horizon", "n fit", "n test", "Model", "B1 0.5", "B2 base rate", "B3 momentum",
        "A2 recalibrated model"], test_rows)}

The same second halves as differences from B2 (Brier minus B2's Brier; below zero is better),
with a 95% moving-block bootstrap interval (blocks one horizon long, {N_BOOT} resamples, seed
{SEED}) and how it reads (the rule in E5; fewer than {MIN_BLOCKS} blocks is too few samples):

{table(["Run", "Horizon", "Blocks", "Model - B2", "B3 momentum - B2", "A2 recalibrated - B2"],
       diff_rows)}

Intervals below zero (beat the base rate out of sample): {", ".join(beat_base) or "none"}.

## A1 permutation: does the order of the model's forecasts carry information?

The model's own `p_up` values shuffled against the labels {N_PERM} times (seed {SEED}). The
p-value is the share of shuffles that score at least as well as the real order (one-sided).
Shuffling ignores overlap between decisions, so the shuffled spread is too narrow and a small
p-value here overstates the evidence; a large one means the real order scores no better than
random order.

{table(["Run", "Horizon", "n", "Model Brier", "Mean shuffled Brier", "p-value"], perm_rows)}

## AUC: does the model rank ups above downs at all?

0.5 means no ranking skill. The interval is the same moving-block bootstrap as in E5
({N_BOOT} resamples, seed {SEED}); the blocks column is decisions / block length.

{table(["Run", "Horizon", "n", "Blocks", "AUC", "95% block bootstrap", "AUC second half"],
       auc_rows)}

AUC interval above 0.5 (with at least {MIN_BLOCKS} blocks): {", ".join(auc_above) or "none"}.

{auc_note}

## A3 gate sweep (R3 taker only)

For each gate: the decisions that would have traded (`p_up >= gate` buys, `p_up <= 1 - gate`
sells), their Brier, hit rate (the side matched the label) and share of up labels. Gate 0.50
is every decision (side by `p_up >= 0.5`). The bot traded at 0.60, with exits at 60 s; the
hit rate needs reading against the up-label share, since a run of mostly sells in a falling
market hits often without skill.

{table(["Horizon", "Gate", "n", "Buys", "Sells", "Brier", "Hit rate", "Up share"], gate_rows)}

At the trading horizon (60 s) the would-be trades are {"better" if tail_better else "not better"}
than the rest: Brier
{g60[0.50]['brier']:.4f} for every decision, {g60[0.60]['brier']:.4f} at the bot's gate 0.60
(hit rate {pct(g60[0.60]['hit_rate'])}, n={g60[0.60]['n']}) and {g60[0.70]['brier']:.4f} at
0.70 (n={g60[0.70]['n']}).

Limitation: there are no seeds to repeat. The decider is served deterministically and the
runs are on live markets, so a repeat is new data, not a reseed; each run is one sample of
market conditions.
"""
    model_beat = [c for c in beat_base if not c.startswith("B3")]
    mom_beat = [c.removeprefix("B3 momentum ") for c in beat_base if c.startswith("B3")]
    headline = (
        f"Model or recalibrated model beat the base rate out of sample in: "
        f"{', '.join(model_beat) or 'no cell'}; the one-line momentum heuristic in: "
        f"{', '.join(mom_beat) or 'no cell'}. AUC interval above 0.5: "
        f"{', '.join(auc_above) or 'none'}"
        + (" (a lead from one 1-hour run, not an edge)" if auc_above else "")
        + f". R3's would-be trades at gate 0.60 were {'better' if tail_better else 'no better'}"
        " than all decisions.")
    return md, headline


# ---------------------------------------------------------------------------------------------
# E8 P&L
# ---------------------------------------------------------------------------------------------


def e8_pnl(stores: dict[str, Store]) -> tuple[str, str]:
    rows, tot = [], {"fills": 0, "notional": 0.0, "before": 0.0, "fees": 0.0}
    per_run: dict[str, tuple[float, float, float]] = {}  # before, fees, after
    for key in LIVE:
        st = stores[key]
        fills = st.fills()
        lg = Ledger.from_fills(fills)
        mark = last_mid(st)
        before, after = lg.pnl_before_fees(mark), lg.pnl(mark)
        sp = fill_split(fills)
        notional = sp["maker"]["notional"] + sp["taker"]["notional"]
        tot["fills"] += len(fills)
        tot["notional"] += notional
        tot["before"] += before
        tot["fees"] += lg.fees
        per_run[key] = (before, lg.fees, after)

        def side(d: dict) -> str:
            return "0" if not d["n"] else f"{d['n']} (${d['fees']:.6f}, {d['bps']:.2f} bps)"

        rows.append([f"{key} {RUNS[key]['what']}", RUNS[key]["strategy"], RUNS[key]["model"],
                     len(fills), f"${notional:,.2f}", usd(before), f"${lg.fees:.6f}",
                     usd(after), side(sp["maker"]), side(sp["taker"]),
                     f"{lg.position:g}"])
    after_total = tot["before"] - tot["fees"]
    losers = [k for k, (_, _, a) in per_run.items() if a < 0]
    lost = ("Every run here lost money after fees" if len(losers) == len(per_run)
            else f"{len(losers)} of {len(per_run)} runs lost money after fees")
    fee_heavy = [k for k in per_run if RUNS[k]["strategy"] == "taker"
                 and per_run[k][1] > abs(per_run[k][0])]
    r6_before = per_run["R6"][0]
    moves = [abs(r["mid_future"] / r["mid"] - 1) * 1e4 for r in stores["R3"]._db.execute(
        "SELECT d.mid, o.mid_future FROM decisions d JOIN outcomes o ON o.decision_id = d.id"
        " WHERE o.horizon_s = 60")]
    move_p50, move_p90 = percentile(moves, 50), percentile(moves, 90)
    rows.append(["Total", "", "", tot["fills"], f"${tot['notional']:,.2f}", usd(tot["before"]),
                 f"${tot['fees']:.6f}", usd(after_total), "", "", ""])
    md = GENERATED + f"""# E8 P&L (last, as PROBLEM.md says)

Per live testnet run, from the fills in each database (`jevhl.ledger`, the same accounting
the reconcile checked against the exchange; see E2). Before fees is realized plus unrealized
P&L; every run ended flat, so it is the realized P&L. Maker and taker fills are told apart by
their fee rate (under {MAKER_TAKER_SPLIT_BPS:.0f} bps of notional is maker); for R5 and R6
this matches the exchange's own maker/taker flag in the saved reconcile output.

{table(["Run", "Strategy", "Model", "Fills", "Notional traded", "Before fees", "Fees",
        "After fees", "Maker fills (fees, rate)", "Taker fills (fees, rate)",
        "End position BTC"], rows)}

Honest notes:
- These are minimum-size runs ($10-15 positions) of one or two hours on testnet. The amounts
  are cents and say nothing about whether the strategy makes money.
- Short-horizon bots usually lose their fees: a taker round trip costs 9 bps (4.50 bps each
  way, measured on testnet), while in R3 the mid moved a median {move_p50:.1f} bps over 60 s
  (90th percentile {move_p90:.1f} bps, ties included). {lost}; in
  {", ".join(fee_heavy) or "no taker run"} the fees were larger than the before-fee loss.
- The maker pays 1.50 bps per fill (measured on testnet), a third of the taker's 4.50 bps,
  but a resting quote is filled most often when the price is about to move against it
  (adverse selection). R6's before-fee P&L was {usd(r6_before)}.
- Testnet liquidity is thin and its order flow is not like mainnet's (its book changed only
  every few seconds; see the M8 notes in SPEC.md), so neither fills nor P&L here transfer to
  mainnet. The project never trades real money.
"""
    headline = (f"All live runs together: {usd(tot['before'])} before fees, "
                f"${tot['fees']:.4f} fees, {usd(after_total)} after fees over "
                f"{tot['fills']} fills (cents at minimum size; testnet).")
    return md, headline


# ---------------------------------------------------------------------------------------------
# Compute record
# ---------------------------------------------------------------------------------------------


def _hours(hms: str) -> float:
    h, m, s = (int(x) for x in hms.split(":"))
    return h + m / 60 + s / 3600


def compute_md() -> tuple[str, str]:
    total = sum(_hours(j[2]) for j in GPU_JOBS)
    rows = [[j[0], j[1], j[2], f"{_hours(j[2]):.2f}", j[3], j[4]] for j in GPU_JOBS]
    rows.append(["Total", "", "", f"{total:.2f}", "", ""])
    md = GENERATED + f"""# Compute record

GPU jobs on the cluster that served the decider (Slurm `sacct`, one GH200 each, so elapsed
hours are GPU-hours). The model only answered requests: there was no training. A job's
time includes idle time while the bot was being set up or stopped. The bot itself ran on a
laptop or a small cloud VM (CPU only, not counted).

{table(["Job", "Start", "Elapsed", "GPU-hours", "State", "Used for"], rows)}
"""
    return md, f"{total:.2f} GPU-hours on GH200s in total (6 jobs; serving only, no training)."


# ---------------------------------------------------------------------------------------------
# README and main
# ---------------------------------------------------------------------------------------------


E7 = "Not done (user's choice): the sample is too small to claim skill or its absence."
WDW = "Written by hand from PIPELINE.md and DECISIONS.md (not generated by the script)."


def readme_md(heads: dict[str, str]) -> str:
    return GENERATED + f"""# Results (step 6 evidence)

Everything here is computed from the bot's own SQLite logs by
`uv run python scripts/evidence.py --runs runs --out results` (the databases are not
committed). Where a fact comes from the PIPELINE.md log rather than a database, the file
says so. Numbers are stated as computed; consecutive decisions overlap, so decision counts
overstate the number of independent samples.

| Run | Strategy | Model | Where | Database |
|---|---|---|---|---|
{chr(10).join(f"| {k} {v['what']} | {v['strategy']} | {v['model']} | {v['where']} | {v['db']} |"
              for k, v in RUNS.items())}
| Drills | taker | mock | Mac | drill.db, short.db |

| File | What | Headline |
|---|---|---|
| [latency.md](latency.md) | E1 latency from the logs | {heads['E1']} |
| [reconcile.md](reconcile.md) | E2 ledger vs exchange | {heads['E2']} |
| [drills.md](drills.md) | E3 safety drills | {heads['E3']} |
| [unattended.md](unattended.md) | E4 unattended run, memory | {heads['E4']} |
| [calibration.md](calibration.md) | E5 Brier, baseline, reliability | {heads['E5']} |
| [baselines.md](baselines.md) | E6 baselines and ablations | {heads['E6']} |
| E7 | one more real-model run | {E7} |
| [pnl.md](pnl.md) | E8 P&L | {heads['E8']} |
| [compute.md](compute.md) | GPU-hours | {heads['compute']} |
| [whats-didnt-work.md](whats-didnt-work.md) | failures and caught mistakes | {WDW} |

The sample is too small to claim anything about the model either way: at most a few hundred
non-overlapping outcome windows per horizon, where PROBLEM.md expected a few thousand
decisions to be needed.
"""


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runs", default="runs", help="directory with the run databases")
    ap.add_argument("--out", default="results", help="output directory")
    args = ap.parse_args(argv)
    runs, out = Path(args.runs), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stores = {k: open_ro(runs / v["db"]) for k, v in RUNS.items()}
    stores["drill"] = open_ro(runs / "drill.db")
    stores["short"] = open_ro(runs / "short.db")
    heads: dict[str, str] = {}
    files: dict[str, str] = {}
    files["latency.md"], heads["E1"] = e1_latency(stores)
    files["reconcile.md"], heads["E2"] = e2_reconcile(stores)
    files["drills.md"], heads["E3"] = e3_drills(stores)
    files["unattended.md"], heads["E4"] = e4_unattended(stores, out)
    files["calibration.md"], heads["E5"], sc = e5_calibration(stores, out)
    files["baselines.md"], heads["E6"] = e6_baselines(stores, sc)
    files["pnl.md"], heads["E8"] = e8_pnl(stores)
    files["compute.md"], heads["compute"] = compute_md()
    files["README.md"] = readme_md(heads)
    for name, text in files.items():
        (out / name).write_text(text)
        print(f"wrote {out / name}")
    for st in stores.values():
        st.close()


if __name__ == "__main__":
    main()
