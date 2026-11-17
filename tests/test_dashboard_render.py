"""Render tests for static/app.js: the real page script runs under node against a minimal fake
DOM built from the real index.html ids, fed one snapshot event. Needs `node` (see
node_required.py for the skip/fail policy)."""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from node_required import require_node

pytestmark = pytest.mark.usefixtures("node")


@pytest.fixture(scope="module")
def node():
    require_node()

STATIC = Path(__file__).parent.parent / "src" / "jevhl" / "static"
IDS = re.findall(r'id="([^"]+)"', (STATIC / "index.html").read_text())

HARNESS = (Path(__file__).parent / "render_harness.js").read_text()


def render(snapshot: dict, extra_js: str = "") -> dict:
    """Send one snapshot to the page script; return what it rendered."""
    js = HARNESS + f"""
send("snapshot", {json.dumps(snapshot)});
{extra_js}
console.log(JSON.stringify(collect()));
"""
    env = {**os.environ, "IDS": json.dumps(IDS), "DIR": str(STATIC)}
    res = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=20, env=env)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def bins(*pts):
    """10 reliability bins; pts are (index, count, mean_p, freq) for the non-empty ones."""
    out = [{"lo": i / 10, "hi": (i + 1) / 10, "count": 0, "mean_p": None, "freq": None}
           for i in range(10)]
    for i, count, mean_p, freq in pts:
        out[i].update(count=count, mean_p=mean_p, freq=freq)
    return out


def cal(h, n, brier, base, b):
    return {"horizon_s": h, "n": n, "brier": brier, "baseline_running": base,
            "baseline_insample": 0.111, "bins": b}


def summary(**kw):
    s = {
        "mid": 100.0, "position": 0.25, "position_usd": 25.0, "pnl_after_fees": 1.5,
        "pnl_before_fees": 1.75, "fees": 0.25, "n_fills": 2, "decisions": 3, "holds": 2,
        "late_holds": 1, "hold_rate": 2 / 3, "decision": None, "feed": "live", "stopped": False,
        "kill": {"state": "ok", "reason": None},
        "calibration": [
            cal(60, 40, 0.20, 0.25, bins((9, 5, 0.9, 1.0))),
            cal(30, 40, 0.30, 0.25, bins((2, 5, 0.2, 0.0), (8, 5, 0.8, 1.0))),
            cal(300, 40, 0.25, 0.30, bins((1, 5, 0.1, 0.0), (5, 5, 0.5, 0.5), (9, 5, 0.9, 1.0))),
        ],
    }
    s.update(kw)
    return s


def snap(sm):
    return {"summary": sm, "decisions": [], "fills": [], "last_decision_id": 0, "last_fill_id": 0}


def test_reliability_curve_drawn_for_each_horizon_with_60s_headline():
    out = render(snap(summary()))
    # one canvas per horizon, each with its own points (dots = non-empty bins)
    assert out["arcs"] == {"rel-chart-60": 1, "rel-chart-30": 2, "rel-chart-300": 3}
    assert all(f"n = {40}" in out["notes"][f"rel-note-{h}"] for h in (60, 30, 300))
    page = (STATIC / "index.html").read_text()
    assert page.index('id="rel-chart-60"') < page.index('id="rel-chart-30"')
    assert "60 s (headline)" in page



def test_pnl_panel_labels_match_the_server_numbers():
    out = render(snap(summary()))
    assert dict(out["pnl"]) == {
        "Mid": "100.00", "Position (base)": "0.25000", "Position $": "25.00",
        "P&L after fees $": "+1.5000", "P&L before fees $": "+1.7500",
        "Cumulative fees $": "0.2500",
    }


def test_counts_panel_shows_server_counts():
    out = render(snap(summary()))
    assert dict(out["counts"]) == {
        "Decisions": "3", "Holds": "2", "Late holds": "1", "Hold rate": "66.7%", "Fills": "2",
    }


def test_brier_table_compares_against_the_running_baseline():
    """baseline_insample (0.111) is deliberately far from baseline_running: showing or
    comparing against the in-sample one would change the numbers and the verdicts."""
    out = render(snap(summary()))
    assert out["brier"] == [
        ["60 s (headline)", "40", "0.2000", "0.2500", "beats baseline"],
        ["30 s", "40", "0.3000", "0.2500", "no better than baseline"],
        ["300 s", "40", "0.2500", "0.3000", "beats baseline"],
    ]


def test_brier_verdict_needs_enough_samples():
    sm = summary()
    sm["calibration"][0]["n"] = 29
    assert render(snap(sm))["brier"][0][4] == "too few samples"


def test_kill_chip_shows_paused_apart_from_tripped_and_ok():
    ok = render(snap(summary()))["kill"]
    paused = render(snap(summary(kill={"state": "paused", "reason": "stale_book"})))["kill"]
    tripped = render(snap(summary(kill={"state": "tripped", "reason": "stale_book"},
                                  stopped=True)))["kill"]
    assert ok == ["kill switch: ok", "chip good"]
    assert paused[0].startswith("kill switch: PAUSED (stale_book") and "TRIPPED" not in paused[0]
    assert paused[1] == "chip warn"
    assert tripped == ["kill switch: TRIPPED (stale_book), stopped", "chip bad"]


# ---- maker panel (milestone 8) ----


def maker(**kw):
    m = {"quote": {"side": "buy", "px": 65001.0, "remaining": 0.00016, "oid": 1000,
                   "age_s": 12.34},
         "actions_last_min": 7, "actions_cap": 20, "rate_paused_until": None,
         "budget": {"used": 321, "cap": 11234}, "capped": False}
    m.update(kw)
    return m


def test_maker_panel_shows_the_resting_quote_and_action_counter():
    out = render(snap(summary(maker=maker())))
    assert dict(out["maker"]) == {
        "Quote": "buy @ 65001.00", "Remaining": "0.00016", "Resting for": "12.3 s",
        "Order actions (last 60 s)": "7 / 20", "Rate-limit pause": "no",
        "Address budget": "321 / 11234", "Capped": "no",
    }
    assert out["makerHidden"] is False


def test_maker_panel_is_hidden_for_the_taker():
    assert render(snap(summary()))["makerHidden"] is True


def test_maker_panel_states_without_a_known_quote():
    unknown = dict(render(snap(summary(maker=maker(quote="unknown", capped=True,
                                                   rate_paused_until=1.0, budget=None)))))
    assert unknown["maker"][0] == ["Quote", "unknown (re-reading open orders)"]
    rows = dict(unknown["maker"])
    assert rows["Capped"].startswith("yes") and rows["Rate-limit pause"].startswith("until ")
    assert rows["Address budget"] == "n/a"
    assert dict(render(snap(summary(maker=maker(quote=None))))["maker"])["Quote"] == "none"


def test_mid_chart_marks_the_resting_quote():
    decisions = [{"id": i, "ts": float(i), "mid": 65000.0 + i, "action": "buy", "p_up": 0.6,
                  "reason": "signal", "size_usd": 11.0, "latency_ms": 5.0} for i in (1, 2)]
    s = {**snap(summary(maker=maker())), "decisions": decisions}
    assert "quote buy 65001.00" in render(s)["midTexts"]
    s = {**snap(summary()), "decisions": decisions}
    assert not any(t.startswith("quote") for t in render(s)["midTexts"])


# ---- mainnet prompt (milestone 10) ----


def mainnet(**kw):
    m = {"state": "fresh", "mid": 64990.0, "age_s": 0.73, "gap_bps": 1.7695,
         "warmup_until": None}
    m.update(kw)
    return m


def test_maker_panel_shows_mainnet_mid_gap_and_age():
    out = render(snap(summary(maker=maker(mainnet=mainnet()))))
    rows = dict(out["maker"])
    assert rows["Mainnet"] == "fresh"
    assert rows["Mainnet mid"] == "64990.00"
    assert rows["Gap to mainnet"] == "+1.77 bps"
    assert rows["Mainnet age"] == "0.7 s"
    assert out["makerCls"]["Mainnet"] == "" and out["makerCls"]["Mainnet age"] == ""
    neg = dict(render(snap(summary(maker=maker(mainnet=mainnet(gap_bps=-3.2)))))["maker"])
    assert neg["Gap to mainnet"] == "-3.20 bps"


def test_maker_panel_has_no_mainnet_rows_with_the_standard_prompt():
    for m in (maker(), maker(mainnet=None)):
        labels = [k for k, _ in render(snap(summary(maker=m)))["maker"]]
        assert not [k for k in labels if "ainnet" in k], labels


def test_maker_panel_marks_stale_mainnet_and_warmup():
    out = render(snap(summary(maker=maker(mainnet=mainnet(state="stale", age_s=7.3)))))
    rows = dict(out["maker"])
    assert rows["Mainnet"].startswith("stale") and "no_mainnet" in rows["Mainnet"]
    assert out["makerCls"]["Mainnet"] == "warn" and out["makerCls"]["Mainnet age"] == "warn"
    out = render(snap(summary(maker=maker(mainnet=mainnet(warmup_until=1.0)))))
    assert dict(out["maker"])["Mainnet"].startswith("warm-up until ")
    assert out["makerCls"]["Mainnet"] == "warn"
    out = render(snap(summary(maker=maker(mainnet=mainnet(state="waiting", mid=None,
                                                          age_s=None, gap_bps=None)))))
    rows = dict(out["maker"])
    assert rows["Mainnet"].startswith("waiting") and rows["Mainnet mid"] == "n/a"
    assert rows["Gap to mainnet"] == "n/a" and rows["Mainnet age"] == "n/a"
