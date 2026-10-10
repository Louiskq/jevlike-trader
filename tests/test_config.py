import pytest

from jevhl.config import Config, ConfigError


def test_defaults():
    c = Config.from_env({})
    assert c.network == "testnet"
    assert c.coin == "BTC"
    assert c.gate == 0.60
    assert c.horizon_s == 60
    assert c.deadline_s == 2.0
    assert c.max_position_usd == 500
    assert c.max_daily_loss_usd == 100
    assert c.min_notional_usd == 10
    assert c.full_size_p == 0.80
    assert c.stale_s == 15
    assert c.max_order_errors == 3
    assert c.api_key is None


@pytest.mark.parametrize("net", ["mainnet", "Mainnet", "prod", ""])
def test_rejects_non_testnet(net):
    with pytest.raises(ConfigError):
        Config.from_env({"NETWORK": net})


def test_explicit_testnet_ok():
    assert Config.from_env({"NETWORK": "testnet"}).network == "testnet"


def test_limits_configurable_without_ceiling():
    c = Config.from_env({"MAX_POSITION_USD": "100000", "MAX_DAILY_LOSS_USD": "5000"})
    assert c.max_position_usd == 100000
    assert c.max_daily_loss_usd == 5000


@pytest.mark.parametrize(
    "env",
    [
        {"MAX_POSITION_USD": "5"},  # below min notional
        {"MAX_POSITION_USD": "abc"},
        {"MAX_DAILY_LOSS_USD": "0"},
        {"MAX_DAILY_LOSS_USD": "-1"},
        {"GATE": "0.5"},
        {"GATE": "1"},
        {"GATE": "0.85"},  # not below full_size_p
        {"HORIZON_S": "0"},
        {"DECIDER_DEADLINE_S": "0"},
        {"MAX_DAILY_LOSS_USD": "nan"},
        {"MAX_DAILY_LOSS_USD": "inf"},
        {"DECIDER_DEADLINE_S": "nan"},
        {"MAX_POSITION_USD": "inf"},
        {"MAX_POSITION_USD": "nan"},
        {"HORIZON_S": "nan"},
        {"HORIZON_S": "-inf"},
        {"GATE": "nan"},
    ],
)
def test_invalid_values_raise(env):
    with pytest.raises(ConfigError):
        Config.from_env(env)


def test_key_from_env_and_never_in_repr():
    c = Config.from_env({"HL_API_KEY": "0xsecretkey"})
    assert c.api_key == "0xsecretkey"
    assert "0xsecretkey" not in repr(c)
    assert "0xsecretkey" not in str(c)


def test_empty_key_is_none():
    assert Config.from_env({"HL_API_KEY": ""}).api_key is None


def test_frozen():
    c = Config.from_env({})
    with pytest.raises(AttributeError):
        c.gate = 0.7  # type: ignore[misc]


ADDR = "0x" + "ab" * 20


def test_account_address_is_read_and_defaults_to_none():
    assert Config.from_env({}).account_address is None
    assert Config.from_env({"HL_ACCOUNT_ADDRESS": ADDR}).account_address == ADDR


@pytest.mark.parametrize("bad", ["0x1234", "ab" * 20, "0x" + "zz" * 20, "0x" + "ab" * 21])
def test_malformed_account_address_is_rejected(bad):
    with pytest.raises(ConfigError):
        Config.from_env({"HL_ACCOUNT_ADDRESS": bad})


def test_secret_key_is_not_in_the_repr():
    c = Config.from_env({"HL_API_KEY": "0x" + "11" * 32})
    assert "1111" not in repr(c)


def test_flatten_retry_window_defaults_to_ten_minutes_and_is_configurable():
    assert Config.from_env({}).flatten_retry_window_s == 600
    assert Config.from_env({"FLATTEN_RETRY_WINDOW_S": "90.5"}).flatten_retry_window_s == 90.5
    assert Config.from_env({"FLATTEN_RETRY_WINDOW_S": "0"}).flatten_retry_window_s == 0


@pytest.mark.parametrize("raw", ["-1", "-0.01", "nan", "inf", "-inf", "abc"])
def test_bad_flatten_retry_window_is_rejected(raw):
    with pytest.raises(ConfigError):
        Config.from_env({"FLATTEN_RETRY_WINDOW_S": raw})


@pytest.mark.parametrize("v", [float("nan"), float("inf"), -1.0])
def test_validate_rejects_bad_flatten_window_set_directly(v):
    with pytest.raises(ConfigError):
        Config(flatten_retry_window_s=v).validate()


# ---- M7: feed liveness and stale-pause recovery ----


def test_recovery_defaults_and_env():
    c = Config.from_env({})
    assert (c.feed_ping_s, c.feed_dead_s) == (4.0, 6.0)
    assert (c.resume_after_s, c.stale_resumes_max, c.resume_max_gap_s) == (60.0, 5, 8.0)
    c = Config.from_env({"FEED_PING_S": "2.5", "FEED_DEAD_S": "3", "RESUME_AFTER_S": "0",
                         "STALE_RESUMES_MAX": "0", "RESUME_MAX_GAP_S": "7.5"})
    assert (c.feed_ping_s, c.feed_dead_s, c.resume_after_s) == (2.5, 3.0, 0.0)
    assert (c.stale_resumes_max, c.resume_max_gap_s) == (0, 7.5)


@pytest.mark.parametrize("name", ["FEED_PING_S", "FEED_DEAD_S", "RESUME_MAX_GAP_S"])
@pytest.mark.parametrize("raw", ["0", "-1", "nan", "inf", "abc"])
def test_bad_positive_recovery_settings_are_rejected(name, raw):
    with pytest.raises(ConfigError):
        Config.from_env({name: raw})


@pytest.mark.parametrize("raw", ["-1", "-0.01", "nan", "inf", "abc"])
def test_bad_resume_after_is_rejected(raw):
    with pytest.raises(ConfigError):
        Config.from_env({"RESUME_AFTER_S": raw})


@pytest.mark.parametrize("raw", ["-1", "1.5", "nan", "inf", "abc"])
def test_bad_stale_resumes_max_is_rejected(raw):
    with pytest.raises(ConfigError):
        Config.from_env({"STALE_RESUMES_MAX": raw})


def test_ping_plus_dead_must_be_below_stale_s():
    with pytest.raises(ConfigError):
        Config.from_env({"FEED_PING_S": "9", "FEED_DEAD_S": "6"})  # 15 is not below 15
    Config.from_env({"FEED_PING_S": "8.9", "FEED_DEAD_S": "6"})


def test_resume_gap_must_be_below_stale_s():
    with pytest.raises(ConfigError):
        Config.from_env({"RESUME_MAX_GAP_S": "15"})
    Config.from_env({"RESUME_MAX_GAP_S": "14.9"})


@pytest.mark.parametrize(
    "field", ["feed_ping_s", "feed_dead_s", "resume_max_gap_s", "resume_after_s"]
)
def test_validate_rejects_nonfinite_recovery_settings_set_directly(field):
    for v in (float("nan"), float("inf")):
        with pytest.raises(ConfigError):
            Config(**{field: v}).validate()


# ---- maker strategy (milestone 8) ----


def test_strategy_defaults_to_taker_and_maker_settings_have_defaults():
    c = Config.from_env({})
    assert c.strategy == "taker"
    assert c.maker_size_usd == 11 and c.maker_deadline_s == 1.0
    assert c.decision_deadline_s == 2.0  # the taker keeps its own deadline


def test_config_strategy_values():
    c = Config.from_env({"STRATEGY": "maker", "MAX_POSITION_USD": "15", "MAKER_SIZE_USD": "11.37",
                         "MAKER_DECIDER_DEADLINE_S": "0.67"})
    assert (c.strategy, c.maker_size_usd, c.maker_deadline_s) == ("maker", 11.37, 0.67)
    assert c.decision_deadline_s == 0.67  # the maker's deadline replaces the taker's
    for bad in ("Maker", "both", "mm"):
        with pytest.raises(ConfigError):
            Config.from_env({"STRATEGY": bad})


@pytest.mark.parametrize("env", [
    {"MAKER_SIZE_USD": "15.01", "MAX_POSITION_USD": "15"},  # above the cap
    {"MAKER_SIZE_USD": "9.99"},  # below the $10 minimum
    {"MAKER_SIZE_USD": "nan"},
    {"MAKER_SIZE_USD": "inf"},
    {"MAKER_DECIDER_DEADLINE_S": "0"},
    {"MAKER_DECIDER_DEADLINE_S": "nan"},
    {"MAKER_DECIDER_DEADLINE_S": "-1"},
])
def test_config_rejects_maker_size_above_cap_or_below_minimum(env):
    with pytest.raises(ConfigError):
        Config.from_env({"STRATEGY": "maker", **env})


def test_maker_size_at_the_cap_is_allowed_and_only_checked_for_the_maker():
    assert Config.from_env({"STRATEGY": "maker", "MAKER_SIZE_USD": "15",
                            "MAX_POSITION_USD": "15"}).maker_size_usd == 15
    # a taker with a $10 cap is not refused because of the unused maker size default ($11)
    assert Config.from_env({"MAX_POSITION_USD": "10"}).strategy == "taker"


@pytest.mark.parametrize("field", ["maker_size_usd", "maker_deadline_s"])
def test_validate_rejects_nonfinite_maker_settings_set_directly(field):
    with pytest.raises(ConfigError):
        Config(strategy="maker", **{field: float("nan")}).validate()
