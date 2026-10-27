"""Feasibility study helpers (study/evaluate.py, study/candles.py)."""

import random

import pytest
from candles import features_and_labels
from evaluate import confident_threshold, evaluate, signed_moves, split_with_gap


def test_split_skips_one_horizon_between_train_and_test():
    train, test = split_with_gap(100, gap=5)
    assert list(train) == list(range(60))
    assert test.start == 65 and test.stop == 100
    assert train.stop + 5 == test.start


def test_split_gap_larger_than_rest_leaves_empty_test():
    _, test = split_with_gap(10, gap=50)
    assert len(test) == 0


def test_confident_threshold_takes_the_top_share():
    ps = [0.5, 0.52, 0.45, 0.6, 0.3, 0.51, 0.49, 0.7, 0.5, 0.55]  # |p-0.5|: .2 .2 .1 .05 ...
    assert confident_threshold(ps, share=0.2) == pytest.approx(0.2)
    assert confident_threshold(ps, share=0.3) == pytest.approx(0.1)


def test_signed_moves_score_the_called_direction():
    ps = [0.8, 0.2, 0.55, 0.5]
    moves = [3.0, 3.0, -1.0, 9.0]
    assert signed_moves(ps, moves, threshold=0.1) == [3.0, -3.0]
    assert signed_moves(ps, moves, threshold=0.0) == [3.0, -3.0, -1.0]  # p == 0.5 is no call


def test_features_use_only_the_past_and_labels_only_the_future():
    closes = [100.0 + i for i in range(200)]
    ts = [60.0 * i for i in range(200)]
    rows = features_and_labels(ts, closes, horizon_bars=5, lags=(1, 5), vol_bars=10)
    first = rows[0]
    i = first["i"]
    assert i >= 10  # needs vol_bars of history
    assert first["x"][0] == pytest.approx((closes[i] / closes[i - 1] - 1) * 1e4)
    assert first["move_bps"] == pytest.approx((closes[i + 5] / closes[i] - 1) * 1e4)
    assert rows[-1]["i"] + 5 <= 199  # every label has its future close


def test_features_change_if_a_past_close_changes_never_a_future_one():
    rng = random.Random(1)
    closes = [100 + rng.gauss(0, 1) for _ in range(120)]
    ts = [60.0 * i for i in range(120)]
    base = features_and_labels(ts, closes, horizon_bars=3, lags=(1, 5), vol_bars=10)
    k = 70
    bumped = closes[:]
    bumped[k + 3] += 5  # the close one horizon after decision k
    after = features_and_labels(ts, bumped, horizon_bars=3, lags=(1, 5), vol_bars=10)
    row = next(r for r in base if r["i"] == k)
    row2 = next(r for r in after if r["i"] == k)
    assert row["x"] == row2["x"]  # the features at k do not see a later close
    assert row["move_bps"] != row2["move_bps"]  # the label does


def test_evaluate_finds_a_planted_edge_and_not_a_fake_one():
    rng = random.Random(7)
    n = 3000
    moves = [rng.gauss(0, 5) for _ in range(n)]
    ys = [1 if m > 0 else 0 for m in moves]
    _, test = split_with_gap(n, gap=1)
    good = [0.5 + (0.3 if moves[i] > 0 else -0.3) * (1 if rng.random() < 0.8 else -1)
            for i in test]
    r = evaluate(good, ys, test, [moves[i] for i in test], threshold=0.1, block=1)
    assert r.auc_lo > 0.5 and r.brier_verdict == "better than baseline"
    assert r.edge_lo > 1.35
    noise = [rng.random() for _ in test]
    r2 = evaluate(noise, ys, test, [moves[i] for i in test], threshold=0.3, block=1)
    assert r2.edge_verdict() == "nothing found"


def test_numpy_auc_and_base_rate_match_the_evidence_script():
    import numpy as np
    from evaluate import auc_np, running_base

    from evidence import auc, running_baseline_probs
    rng = random.Random(3)
    ps = [round(rng.random(), 1) for _ in range(500)]  # rounded, so many ties
    ys = [int(rng.random() < 0.3 + 0.4 * p) for p in ps]
    assert auc_np(np.array(ps), np.array(ys)) == pytest.approx(auc(ps, ys))
    assert list(running_base(np.array(ys, dtype=float))) == pytest.approx(
        running_baseline_probs(ys))
    assert auc_np(np.array([0.2, 0.8]), np.array([1, 1])) is None


def test_block_indices_are_runs_of_consecutive_points():
    import numpy as np
    from evaluate import block_indices
    ix = block_indices(103, 10, np.random.default_rng(0))
    assert len(ix) == 103 and ix.min() >= 0 and ix.max() <= 102
    assert all(ix[k + 1] == ix[k] + 1 for k in range(9))  # first block is consecutive


def _venue(n=2000, seed=0):
    import numpy as np
    from books import Venue
    rng = np.random.default_rng(seed)
    t = np.arange(n, dtype=float)
    mid = 80000 + np.cumsum(rng.normal(0, 2, n))
    tr_t = np.sort(rng.uniform(0, n, 4 * n))
    sz = rng.uniform(0.01, 1, len(tr_t))
    return Venue(t=t, mid=mid, spread_bps=np.full(n, 0.12), imb=rng.uniform(-1, 1, (n, 3)),
                 depth=rng.uniform(0, 3, (n, 6)), tr_t=tr_t,
                 tr_signed=sz * rng.choice([-1, 1], len(sz)), tr_size=sz,
                 hours=n / 3600, n_trades=len(sz), gaps=0)


def test_book_features_never_see_the_future():
    from books import build
    v = _venue()
    base = build(v, 60, stride=5, main=_venue(seed=1))
    k = len(base["t"]) // 2
    t_k = base["t"][k]
    v2 = _venue()
    after = v2.t > t_k
    v2.mid[after] += 500  # every later book changes
    v2.tr_signed[v2.tr_t > t_k] *= -1  # and every later trade
    changed = build(v2, 60, stride=5, main=_venue(seed=1))
    j = list(changed["t"]).index(t_k)
    assert (changed["x"][j] == base["x"][k]).all()
    assert changed["move"][j] != base["move"][k]


def test_label_is_dropped_when_the_future_book_is_stale():
    import numpy as np
    from books import build
    v = _venue()
    keep = (v.t < 1000) | (v.t > 1100)  # a 100 s hole in the books
    v.t, v.mid = v.t[keep], v.mid[keep]
    v.spread_bps, v.imb, v.depth = v.spread_bps[keep], v.imb[keep], v.depth[keep]
    d = build(v, 60, stride=1)
    target = d["t"] + 60
    assert not np.any((target > 1005) & (target <= 1100))


def _replay_inputs(n=900, seed=0):
    import numpy as np
    from finetune_data import Series

    from jevhl.feed import Book, Print
    rng = np.random.default_rng(seed)
    mids = 80000 + np.cumsum(rng.normal(0, 1, n)).round()
    books = [Book(ts=float(i), recv_ts=float(i) + 0.3, coin="BTC",
                  bids=[[m - 0.5 - k, 0.3] for k in range(20)],
                  asks=[[m + 0.5 + k, 0.3] for k in range(20)]) for i, m in enumerate(mids)]
    prints = [Print(float(i), float(i) + 0.2, "buy" if i % 3 else "sell", float(mids[i]), 0.01, i)
              for i in range(0, n, 2)]
    main = Series([float(i) for i in range(n)], list(mids + 40))
    return books, prints, main


def test_finetune_prompt_is_the_bots_prompt_plus_a_mainnet_block_before_the_question():
    from finetune_data import build_rows
    books, prints, main = _replay_inputs()
    r = build_rows(books, prints, main)[0]
    assert r["context_mainnet"].startswith(r["context"].rsplit("\nQuestion: ", 1)[0])
    assert r["context_mainnet"].endswith("\nQuestion: Will the mid price be higher in 60 seconds?")
    assert "Mainnet (the main Hyperliquid market) BTC mid" in r["context_mainnet"]
    assert "Mainnet" not in r["context"]
    assert r["question"] == "Will the mid price be higher in 60 seconds?"
    assert r["options"] == ["up", "down"]


def test_finetune_prompt_never_sees_the_future_and_label_is_the_60s_move():
    from dataclasses import replace

    from finetune_data import build_rows
    books, prints, main = _replay_inputs()
    rows = build_rows(books, prints, main)
    r = rows[len(rows) // 2]
    later = [replace(b, bids=[[px + 7, sz] for px, sz in b.bids],
                     asks=[[px + 7, sz] for px, sz in b.asks]) if b.recv_ts > r["t"] else b
             for b in books]
    later_prints = [replace(p, side="sell") if p.recv_ts > r["t"] else p for p in prints]
    r2 = next(x for x in build_rows(later, later_prints, main) if x["t"] == r["t"])
    assert r2["context_mainnet"] == r["context_mainnet"]
    fut = next(b for b in reversed(books) if b.recv_ts <= r["t"] + 60).mid
    assert r["move_bps"] == pytest.approx((fut / r["mid"] - 1) * 1e4)
    assert r["gold"] == (0 if r["move_bps"] > 0 else 1)
    assert r2["move_bps"] != r["move_bps"]


def test_finetune_rows_use_the_bots_mainnet_renderer():
    """The training prompt comes from the bot's own functions (single source of truth)."""
    from collections import deque
    from dataclasses import replace

    from finetune_data import KEEP_MIDS_S, build_rows

    from jevhl.feed import TradeTape
    from jevhl.mainnet import mainnet_input
    from jevhl.maker import build_input
    from jevhl.prompt import render_maker_context
    books, prints, main = _replay_inputs()
    r = build_rows(books, prints, main)[3]
    tape, mids, j = TradeTape(), deque(), 0
    for b in books:
        batch = []
        while j < len(prints) and prints[j].recv_ts <= b.recv_ts:
            batch.append(prints[j])
            j += 1
        if batch:
            tape.add(batch, b.recv_ts)
        mids.append((b.recv_ts, b.mid))
        while mids and mids[0][0] < b.recv_ts - KEEP_MIDS_S:
            mids.popleft()
        if b.recv_ts == r["t"]:
            break
    inp = build_input(b, list(mids), tape, [], 60.0)
    mi = mainnet_input(b.recv_ts, b.mid, main.at)
    assert r["context_mainnet"] == render_maker_context(replace(inp, mainnet=mi))
    assert r["context"] == render_maker_context(inp)


def test_finetune_split_leaves_one_horizon_between_train_and_test():
    from finetune_data import build_rows, split
    books, prints, main = _replay_inputs(n=2000)
    rows = build_rows(books, prints, main)
    split(rows)
    last_train = max(r["t"] for r in rows if r["split"] == "train")
    first_test = min(r["t"] for r in rows if r["split"] == "test")
    assert first_test - last_train >= 60


# ---- frozen training-time code (study/finetune_data.py at fc94a57; do not edit: the
# fine-tune was trained on rows built by exactly this) ----
_T_HORIZON_S = 60.0
_T_STRIDE = 5
_T_WARMUP_S = 310.0
_T_MAX_BOOK_AGE_S = 5.0
_T_KEEP_MIDS_S = 310.0
_T_MAINNET_WINDOWS_S = (5, 15, 60)
_T_MAINNET_LINE = ("Mainnet (the main Hyperliquid market) BTC mid {mid}; this market's mid is "
                   "{gap:+.1f} bps from it.")
_T_MAINNET_RETURNS_LINE = "Mainnet mid change: {parts}"
_T_MAINNET_RETURN_PART = "{w:g} s {r}"


def _t_mainnet_block(m_mid, gap_bps, rets):
    from jevhl.prompt import MISSING, price
    parts = [_T_MAINNET_RETURN_PART.format(w=w, r=MISSING if r is None else f"{r:+.1f} bps")
             for w, r in rets.items()]
    return "\n".join([_T_MAINNET_LINE.format(mid=price(m_mid, 1), gap=gap_bps),
                      _T_MAINNET_RETURNS_LINE.format(parts=", ".join(parts))])


def _t_with_mainnet(context, block):
    from jevhl.prompt import QUESTION_PREFIX
    head, sep, q = context.rpartition("\n" + QUESTION_PREFIX)
    assert sep, "prompt has no question line"
    return f"{head}\n{block}{sep}{q}"


class _TSeries:
    def __init__(self, ts, vs):
        self.ts, self.vs = ts, vs

    def at(self, t):
        import bisect
        i = bisect.bisect_right(self.ts, t) - 1
        return (None, float("inf")) if i < 0 else (self.vs[i], t - self.ts[i])


def _t_build_rows(books, prints, main, horizon_s=_T_HORIZON_S, stride=_T_STRIDE):
    from collections import deque

    from jevhl.feed import TradeTape
    from jevhl.maker import build_input
    from jevhl.prompt import OPTIONS, question_text, render_maker_context
    mids_series = _TSeries([b.recv_ts for b in books], [b.mid for b in books])
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
        while mids and mids[0][0] < now - _T_KEEP_MIDS_S:
            mids.popleft()
        if k % stride or now < t0 + _T_WARMUP_S or now + horizon_s > t_end:
            continue
        fut, fage = mids_series.at(now + horizon_s)
        m_now, mage = main.at(now)
        if fut is None or fage > _T_MAX_BOOK_AGE_S or m_now is None or mage > _T_MAX_BOOK_AGE_S:
            continue
        move = (fut / book.mid - 1) * 1e4
        if move == 0:
            continue
        rets = {}
        for w in _T_MAINNET_WINDOWS_S:
            past, page = main.at(now - w)
            rets[w] = (None if past is None or page > _T_MAX_BOOK_AGE_S
                       else (m_now / past - 1) * 1e4)
        ctx = render_maker_context(build_input(book, list(mids), tape, [], horizon_s))
        rows.append({
            "t": now, "mid": book.mid, "move_bps": move,
            "gold": 0 if move > 0 else 1,
            "question": question_text(horizon_s), "options": list(OPTIONS),
            "context": ctx,
            "context_mainnet": _t_with_mainnet(
                ctx, _t_mainnet_block(m_now, (book.mid / m_now - 1) * 1e4, rets)),
        })
    return rows
# ---- end of frozen training-time code ----


def test_finetune_rows_equal_the_training_time_rows():
    """build_rows now uses the bot's mainnet_input and renderer; its rows must equal what the
    training-time code built, over mainnet gaps (stale latest mid: row skipped; stale past
    mid: n/a) and gaps of both signs."""
    from finetune_data import Series, build_rows
    books, prints, _ = _replay_inputs(n=1200)
    ts, vs = [], []
    for i, b in enumerate(books):
        if 600 < i < 612 or 800 < i < 806:  # mainnet holes of 11 s and 5 s
            continue
        ts.append(float(i))
        vs.append(b.mid + (40.0 if i < 700 else -30.0))  # testnet below, then above mainnet
    rows = build_rows(books, prints, Series(ts, vs), stride=1)
    want = _t_build_rows(books, prints, _TSeries(ts, vs), stride=1)
    assert rows == want
    by_t = {round(r["t"], 1): r["context_mainnet"] for r in rows}
    assert 604.3 in by_t and 605.3 not in by_t  # latest mainnet mid 4.3 s vs 5.3 s old
    assert 804.3 in by_t and 805.3 not in by_t
    for part in ("change: 5 s n/a", ", 15 s n/a", ", 60 s n/a"):  # past mids in the holes
        assert any(part in c for c in by_t.values()), part
    gaps = [float(c.split("this market's mid is ")[1].split(" bps")[0]) for c in by_t.values()]
    assert min(gaps) < 0 < max(gaps)
