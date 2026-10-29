from copy import deepcopy

import pytest
from jsonschema import Draft4Validator

from freqtrade.bot_risk import BOT_RISK_SCHEMA, validate_bot_config, validate_bot_risk


def risk(capital=1000):
    return {
        "schema_version": 1,
        "account_namespace": "test-account",
        "capital_usdt": capital,
        "reserve_ratio": 0.05,
        "max_total_notional_usdt": capital * 0.95,
        "pair_limits": {"BTC/USDT:USDT": capital * 0.2, "ETH/USDT:USDT": capital * 0.2},
        "max_initial_notional_usdt": capital * 0.1,
        "max_add_notional_usdt": capital * 0.1,
        "max_occupied_symbols": 2,
    }


@pytest.mark.parametrize("capital", [500, 1000, 1234.56, 2000, 10000])
def test_config_only_capital_and_pair_limits(capital):
    value = risk(capital)
    Draft4Validator.check_schema(BOT_RISK_SCHEMA)
    Draft4Validator(BOT_RISK_SCHEMA).validate(value)
    assert validate_bot_risk(value) == value
    config = {
        "bot_risk": value,
        "available_capital": capital,
        "tradable_balance_ratio": 0.95,
        "max_open_trades": 2,
        "stake_currency": "USDT",
        "trading_mode": "futures",
        "margin_mode": "cross",
        "exchange": {"pair_whitelist": list(value["pair_limits"])},
    }
    assert validate_bot_config(config) == value
    changed = deepcopy(config)
    changed["available_capital"] += 1
    with pytest.raises(ValueError, match="Derived config"):
        validate_bot_config(changed)


@pytest.mark.parametrize(
    "key,value",
    [
        ("capital_usdt", float("nan")),
        ("capital_usdt", True),
        ("max_total_notional_usdt", 1001),
        ("max_add_notional_usdt", 201),
        ("reserve_ratio", 1),
        ("max_occupied_symbols", 3),
        ("schema_version", True),
    ],
)
def test_invalid_and_conflicting_limits_rejected(key, value):
    candidate = risk()
    candidate[key] = value
    with pytest.raises(ValueError):
        validate_bot_risk(candidate)
