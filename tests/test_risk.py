from jevhl.config import Config
from jevhl.risk import KillSwitch

DAY = 86400.0
CFG = Config.from_env({})  # loss 100, errors 3, stale 15


class Clock:
    def __init__(self, t=10 * DAY + 100.0):
        self.t = t

    def __call__(self):
        return self.t


def make(cfg=CFG):
    c = Clock()
    return KillSwitch(cfg, c), c


def test_starts_untripped():
    ks, _ = make()
    assert not ks.tripped and ks.reason is None


def test_daily_loss_trips_at_limit_inclusive():
    ks, _ = make()
    ks.update_pnl(0.0)
    ks.update_pnl(-99.99)
    assert not ks.tripped
    ks.update_pnl(-100.0)
    assert ks.tripped and ks.reason == "daily_loss"


def test_daily_loss_relative_to_first_pnl():
    ks, _ = make()
    ks.update_pnl(50.0)  # baseline
    ks.update_pnl(-49.0)  # -99 vs baseline
    assert not ks.tripped
    ks.update_pnl(-50.0)  # -100
    assert ks.tripped


def test_gains_do_not_trip_and_raise_nothing():
    ks, _ = make()
    ks.update_pnl(0.0)
    ks.update_pnl(1000.0)
    assert not ks.tripped


def test_daily_loss_resets_at_utc_rollover():
    ks, c = make()
    ks.update_pnl(0.0)
    ks.update_pnl(-90.0)
    c.t = 11 * DAY + 1  # next UTC day
    ks.update_pnl(-150.0)  # -60 vs rollover baseline (-90): fine
    assert not ks.tripped
    ks.update_pnl(-190.0)  # -100 vs baseline
    assert ks.tripped and ks.reason == "daily_loss"


def test_no_rollover_within_same_day():
    ks, c = make()
    ks.update_pnl(0.0)
    c.t = 11 * DAY - 1
    ks.update_pnl(-100.0)
    assert ks.tripped


def test_order_errors_trip_on_third_consecutive():
    ks, _ = make()
    ks.on_order_result(False)
    ks.on_order_result(False)
    assert not ks.tripped
    ks.on_order_result(False)
    assert ks.tripped and ks.reason == "order_errors"


def test_success_resets_error_count():
    ks, _ = make()
    ks.on_order_result(False)
    ks.on_order_result(False)
    ks.on_order_result(True)
    ks.on_order_result(False)
    ks.on_order_result(False)
    assert not ks.tripped


def test_stale_book_boundary():
    ks, c = make()
    ks.on_book()
    c.t += 15.0
    assert ks.check() is None  # exactly 15 s does not trip
    c.t += 0.001
    assert ks.check() == "stale_book"
    assert ks.tripped and ks.reason == "stale_book"


def test_fresh_book_resets_staleness():
    ks, c = make()
    ks.on_book()
    c.t += 10
    ks.on_book()
    c.t += 10
    assert ks.check() is None


def test_stale_not_armed_before_first_book():
    ks, c = make()
    c.t += 1000
    assert ks.check() is None


def test_latches_until_reset_and_keeps_first_reason():
    ks, c = make()
    ks.on_book()
    ks.update_pnl(0.0)
    ks.update_pnl(-100.0)
    assert ks.reason == "daily_loss"
    for _ in range(3):
        ks.on_order_result(False)
    c.t += 100
    ks.check()
    assert ks.reason == "daily_loss"
    ks.update_pnl(0.0)  # recovery does not unlatch
    assert ks.tripped


def test_reset_clears_state():
    ks, c = make()
    for _ in range(3):
        ks.on_order_result(False)
    assert ks.tripped
    ks.reset()
    assert not ks.tripped and ks.reason is None
    ks.on_order_result(False)  # error count restarted
    assert not ks.tripped


def test_configurable_thresholds():
    cfg = Config.from_env({"MAX_DAILY_LOSS_USD": "5"})
    ks, _ = make(cfg)
    ks.update_pnl(0)
    ks.update_pnl(-5)
    assert ks.tripped
