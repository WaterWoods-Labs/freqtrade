from copy import deepcopy

import pytest

from freqtrade.binance_portfolio_usdt235 import PAIRS
from freqtrade.configuration.config_validation import validate_config_schema
from freqtrade.exceptions import ConfigurationError
from freqtrade.exchange.binance_portfolio_policy import _portfolio_margin_chan_risk_policy_valid


def risk():
    return {
        "account_namespace": "existing-account-namespace",
        "policy": "chan_dca4_235_2000_v1",
        "pairs": dict.fromkeys(PAIRS, 200),
        "allowed_sides": ["long", "short"],
        "max_leverage": 1,
        "max_total_entry_notional": 2000,
        "force_entry_order_type": "disabled",
        "reject_force_entry_price": True,
    }


def test_catalog_includes_short_history_usdt_symbols_only(default_conf):
    assert len(PAIRS) == 235
    assert all(pair.endswith("/USDT:USDT") for pair in PAIRS)
    assert {f"{s}/USDT:USDT" for s in ("ABBV", "EIDO", "EQT", "STM", "SPCX")} <= PAIRS
    conf = deepcopy(default_conf)
    conf["exchange"]["portfolio_margin_risk"] = risk()
    assert _portfolio_margin_chan_risk_policy_valid(risk())
    validate_config_schema(conf)


@pytest.mark.parametrize(
    "change",
    [
        {"max_total_entry_notional": 2000.01},
        {"max_leverage": 2},
        {"force_entry_order_type": "market"},
        {"policy": "chan_dca4_56_2000_v1"},
        {"pairs": {**dict.fromkeys(PAIRS, 200), "SPCX/USD1:USD1": 200}},
        {"pairs": {**dict.fromkeys(PAIRS, 200), "NEW/USDT:USDT": 200}},
        {"pairs": {p: 200 for p in PAIRS if p != "ABBV/USDT:USDT"}},
        {"pairs": {**dict.fromkeys(PAIRS, 200), "ABBV/USDT:USDT": 200.01}},
    ],
)
def test_expansion_never_relaxes_risk_or_drops_unready_members(default_conf, change):
    selected = {**risk(), **change}
    assert not _portfolio_margin_chan_risk_policy_valid(selected)
    conf = deepcopy(default_conf)
    conf["exchange"]["portfolio_margin_risk"] = selected
    with pytest.raises(ConfigurationError):
        validate_config_schema(conf)
