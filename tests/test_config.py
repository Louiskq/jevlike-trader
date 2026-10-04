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
