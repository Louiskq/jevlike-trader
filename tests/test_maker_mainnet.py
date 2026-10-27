"""The maker with MAKER_PROMPT=mainnet (milestone 10): the mainnet block in the model's input,
the 310 s warm-up hold, the no_mainnet hold on stale or missing mainnet prices (quote left as
it is, never a kill), the mainnet mid and gap recorded per decision, and the standard prompt
unchanged. Paper and the fake venue; clocks are injected, nothing waits."""

import json

import pytest

from jevhl.config import Config
from jevhl.mainnet import MainnetHistory, mainnet_input
from jevhl.prompt import render_maker_context
from test_maker import (
    MCFG,
    SZ,
    Recorder,
    events,
    make_live,
    make_paper,
    mkbook,
    no_polls,
    quote_actions,
    run,
    tick,
)
from test_trader import T0, StubModel

NCFG = Config(strategy="maker", max_position_usd=15.0, maker_prompt="mainnet")
WARM = T0 + 400.0  # past the warm-up (first book at T0)
MMID = 64990.0  # mainnet mid in these tests; testnet's paper mid is 65001.5


def feed(tr, t_from, t_to, mid=MMID, step=1.0):
    t = t_from
    while t <= t_to + 1e-9:
        tr.mainnet.add(t, mid)
        t += step


def reasons(store):
    return [d["reason"] for d in store.decisions()]


def warmed(cfg=NCFG, model=None, live=False):
    """A trader past its warm-up: first book at T0 (a warmup hold), mainnet fed up to WARM."""
    if live:
        tr, store, clock, fake, model = make_live(cfg=cfg, model=model or Recorder())
    else:
        (tr, store, clock, model), fake = make_paper(cfg=cfg, model=model or Recorder()), None
    feed(tr, T0 - 100, WARM)
    tick(tr, clock, T0)
    if live:
        no_polls(tr, clock)
    return tr, store, clock, fake, model


# ---- warm-up ----


def test_mainnet_prompt_holds_warmup_for_the_first_310_s():
    tr, store, clock, model = make_paper(cfg=NCFG, model=Recorder())
    clock.t = T0 - 50  # the trader exists before the first book: warm-up counts from the book
    feed(tr, T0 - 100, T0 + 361)  # mainnet books long before the first testnet book
    tick(tr, clock, T0 + 50)  # the run's first testnet book
    tick(tr, clock, T0 + 50 + 309.63)
    assert reasons(store) == ["warmup", "warmup"]
    assert model.inputs == [] and tr._quote is None and tr._actions == []
    tick(tr, clock, T0 + 50 + 310.37)
    assert reasons(store)[-1] == "signal" and len(model.inputs) == 1
    assert tr._quote is not None
    assert len(events(store, "mainnet_warmup")) == 1


def test_warmup_ends_at_exactly_310_s_as_in_training():
    # training kept a row when now >= t0 + 310 (`now < t0 + WARMUP_S` was skipped)
    tr, store, clock, model = make_paper(cfg=NCFG, model=Recorder())
    feed(tr, T0 - 100, T0 + 400)
    tick(tr, clock, T0)
    tick(tr, clock, T0 + 310.0)
    assert reasons(store) == ["warmup", "signal"]


def test_standard_prompt_has_no_warmup():
    tr, store, clock, model = make_paper(cfg=MCFG, model=Recorder())
    tick(tr, clock, T0)
    assert reasons(store) == ["signal"] and len(model.inputs) == 1
    assert events(store, "mainnet_warmup") == []


def test_warmup_comes_after_stale_and_rate_pause_and_before_no_mainnet():
    tr, store, clock, model = make_paper(cfg=NCFG, model=Recorder())  # no mainnet book at all
    tick(tr, clock, T0)
    assert reasons(store) == ["warmup"]  # not no_mainnet
    clock.t = T0 + 30
    run(tr.on_book(mkbook(recv_ts=T0 + 12)))  # 18 s old: over stale_s
    assert reasons(store)[-1] == "stale"
    tr.rate_paused_until = T0 + 100
    tick(tr, clock, T0 + 40)
    assert reasons(store)[-1] == "rate_paused"
    assert model.inputs == []


# ---- the block in the model's input ----


def test_mainnet_prompt_sends_the_mainnet_block_to_the_model():
    tr, store, clock, _, model = warmed()
    tick(tr, clock, WARM)
    assert reasons(store)[-1] == "signal"
    inp = model.inputs[-1]
    assert inp.mainnet == mainnet_input(WARM, 65001.5, tr.mainnet.at)
    assert inp.mainnet.mid == MMID
    assert inp.mainnet.gap_bps == pytest.approx((65001.5 / MMID - 1) * 1e4)
    assert "Mainnet (the main Hyperliquid market) BTC mid 64990.00;" in render_maker_context(inp)


def test_gap_uses_the_market_mid_without_our_own_quote():
    tr, store, clock, fake, model = warmed(live=True)
    tick(tr, clock, WARM)  # buy quote at 65001; market mid 65001.5
    tick(tr, clock, WARM + 1, bid=65001.0, bid_sz=SZ)  # our bid is the touch: raw mid 65002.0
    gaps = [m.mainnet.gap_bps for m in model.inputs]
    assert gaps == [pytest.approx((65001.5 / MMID - 1) * 1e4)] * 2


def test_standard_prompt_makes_no_mainnet_lookup():
    tr, store, clock, model = make_paper(cfg=MCFG, model=Recorder())
    assert tr.mainnet is None
    tick(tr, clock, T0)
    assert model.inputs[-1].mainnet is None
    assert "Mainnet" not in render_maker_context(model.inputs[-1])
    assert events(store, "mainnet_input") == []


def test_a_history_passed_with_the_standard_prompt_is_not_used():
    from jevhl.ledger import Ledger
    from jevhl.maker import MakerTrader
    from jevhl.paper import PaperExchange
    from jevhl.risk import KillSwitch
    from jevhl.store import Store
    from test_maker import BTC
    from test_trader import Clock
    store, clock = Store(":memory:"), Clock()
    tr = MakerTrader(MCFG, store, StubModel(), PaperExchange(store, BTC, "BTC"), Ledger(),
                     KillSwitch(MCFG, clock, day_start_pnl=0.0), clock=clock,
                     mainnet=MainnetHistory())
    assert tr.mainnet is None


# ---- stale or missing mainnet ----


def test_stale_mainnet_holds_no_mainnet_without_asking_the_model_and_leaves_the_quote():
    tr, store, clock, fake, model = warmed(live=True)
    tick(tr, clock, WARM)
    quote, sent, asked = tr._quote, len(quote_actions(fake)), len(model.inputs)
    model.p = 0.1  # the model would now want the other side
    tick(tr, clock, WARM + 5.37)  # latest mainnet book (WARM) is 5.37 s old
    assert reasons(store)[-1] == "no_mainnet"
    assert len(model.inputs) == asked and tr._quote is quote
    assert len(quote_actions(fake)) == sent  # no cancel, no modify, no place
    d = store.decisions()[-1]
    assert d["p_up"] is None and d["latency_ms"] is None and d["mid"] == 65001.5
    feed(tr, WARM + 6.0, WARM + 6.0)
    tick(tr, clock, WARM + 10.63)  # 4.63 s old: fresh again
    assert reasons(store)[-1] == "signal" and len(model.inputs) == asked + 1


def test_no_mainnet_book_yet_holds_no_mainnet():
    tr, store, clock, model = make_paper(cfg=NCFG, model=Recorder())
    tick(tr, clock, T0)
    tick(tr, clock, WARM)
    assert reasons(store) == ["warmup", "no_mainnet"] and model.inputs == []
    assert events(store, "mainnet_stale") == ["no mainnet book yet"]


def test_no_mainnet_is_not_a_kill_or_pause_trigger():
    tr, store, clock, _, model = warmed()
    for i in range(40):  # 40 s of testnet books, mainnet stale throughout (> stale_s 15 s)
        tick(tr, clock, WARM + 10 + i)
        run(tr.check_stale())
    assert set(reasons(store)[1:]) == {"no_mainnet"}
    assert tr.kill_reason is None and not tr.paused and not tr.stopped
    assert events(store, "kill_switch") == [] and events(store, "stale_pause") == []


def test_mainnet_stale_and_fresh_are_logged_once_per_spell():
    tr, store, clock, _, model = warmed()
    for i in range(5):
        tick(tr, clock, WARM + 6 + i)  # stale: 6..10 s old
    assert events(store, "mainnet_stale") == ["latest mainnet book 6.0 s old"]
    feed(tr, WARM + 11, WARM + 11)
    tick(tr, clock, WARM + 11.5)
    (fresh,) = events(store, "mainnet_fresh")
    assert "5 no_mainnet holds" in fresh and "5.5 s" in fresh
    tick(tr, clock, WARM + 17)
    assert len(events(store, "mainnet_stale")) == 2 and len(events(store, "mainnet_fresh")) == 1


# ---- recording ----


def test_each_mainnet_decision_records_mid_and_gap_in_an_event():
    tr, store, clock, _, model = warmed()
    tick(tr, clock, WARM)
    tick(tr, clock, WARM + 0.7)
    rows = [json.loads(e) for e in events(store, "mainnet_input")]
    ids = [d["id"] for d in store.decisions() if d["reason"] == "signal"]
    assert [r["decision_id"] for r in rows] == ids and len(ids) == 2
    r = rows[-1]
    assert r["mid"] == MMID and r["gap_bps"] == pytest.approx((65001.5 / MMID - 1) * 1e4)
    assert r["age_s"] == pytest.approx(0.7)
    assert r["returns_bps"] == {"5": 0.0, "15": 0.0, "60": 0.0}
    assert tr.last_mainnet == model.inputs[-1].mainnet


def test_no_mainnet_and_warmup_holds_record_no_mainnet_input_event():
    tr, store, clock, _, model = warmed()
    tick(tr, clock, WARM + 7)  # stale
    assert reasons(store) == ["warmup", "no_mainnet"]
    assert events(store, "mainnet_input") == []


def test_kill_during_the_model_call_still_holds_killed_with_mainnet_on():
    tr, store, clock, fake, _ = warmed(live=True, model=StubModel(0.9))

    class Tripping(StubModel):
        def predict(self, inp):
            tr._kill.on_order_result(False)
            tr._kill.on_order_result(False)
            tr._kill.on_order_result(False)
            return super().predict(inp)

    tr._model = Tripping(0.9)
    tick(tr, clock, WARM)
    assert reasons(store)[-1] == "killed"
    assert [c for c in fake.calls if c[0] == "order"] == []  # never quoted
    assert len(events(store, "mainnet_input")) == 1  # the prompt did carry the block


# ---- the dashboard's view ----


def test_maker_view_shows_the_mainnet_state():
    tr, store, clock, model = make_paper(cfg=NCFG, model=Recorder())
    v = tr.maker_view()["mainnet"]
    assert v["state"] == "waiting" and v["mid"] is None and v["age_s"] is None
    feed(tr, T0 - 100, T0)
    tick(tr, clock, T0)
    v = tr.maker_view()["mainnet"]
    assert v["warmup_until"] == pytest.approx(T0 + 310) and v["gap_bps"] is None
    feed(tr, T0 + 1, WARM)
    tick(tr, clock, WARM)
    v = tr.maker_view()["mainnet"]
    assert v["warmup_until"] is None and v["state"] == "fresh" and v["mid"] == MMID
    assert v["gap_bps"] == pytest.approx((65001.5 / MMID - 1) * 1e4) and v["age_s"] == 0.0
    clock.t = WARM + 4.63
    assert tr.maker_view()["mainnet"]["state"] == "fresh"
    clock.t = WARM + 5.37
    v = tr.maker_view()["mainnet"]
    assert v["state"] == "stale" and v["age_s"] == pytest.approx(5.37)


def test_maker_view_has_no_mainnet_with_the_standard_prompt():
    tr, store, clock, model = make_paper(cfg=MCFG)
    tick(tr, clock, T0)
    assert tr.maker_view()["mainnet"] is None
