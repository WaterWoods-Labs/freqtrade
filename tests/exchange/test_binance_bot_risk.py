import pytest

from freqtrade.bot_risk import validate_bot_config
from freqtrade.exceptions import InvalidOrderException, OperationalException
from freqtrade.exchange.binance_portfolio_policy import portfolio_policy
from tests.conftest import get_patched_exchange
from tests.exchange.test_binance import portfolio_margin_dca_conf, portfolio_margin_live_api_mock
from tests.test_bot_risk import risk


def configured(default_conf, tmp_path, capital=1234.56):
    config = portfolio_margin_dca_conf(default_conf, tmp_path)
    policy = risk(capital)
    config.update(
        bot_risk=policy, available_capital=capital, tradable_balance_ratio=0.95, max_open_trades=2
    )
    config["exchange"]["pair_whitelist"] = list(policy["pair_limits"])
    config["exchange"]["portfolio_margin_risk"] = portfolio_policy(policy)
    validate_bot_config(config)
    return config


def test_nonpreset_limits_reach_submission_guard(default_conf, tmp_path, mocker):
    config = configured(default_conf, tmp_path)
    api = portfolio_margin_live_api_mock()
    api.fetch_positions.return_value = []
    api.fetch_open_orders.return_value = []
    exchange = get_patched_exchange(mocker, config, api, exchange="binance")
    mocker.patch.object(exchange, "_reconcile_portfolio_entry_reservations")
    cap = config["bot_risk"]["max_initial_notional_usdt"]
    exchange._validate_portfolio_margin_dca_projected_exposure(
        "ETH/USDT:USDT", cap, "ftpm-test", "buy"
    )
    with pytest.raises(InvalidOrderException, match="initial/add entry limit"):
        exchange._validate_portfolio_margin_dca_projected_exposure(
            "ETH/USDT:USDT", cap + 0.01, "ftpm-test", "buy"
        )
    assert not api.create_order.called


def test_derived_policy_cannot_override_canonical_budget(default_conf, tmp_path, mocker):
    config = configured(default_conf, tmp_path)
    config["exchange"]["portfolio_margin_risk"]["max_total_entry_notional"] += 1
    with pytest.raises(OperationalException, match="differs"):
        get_patched_exchange(mocker, config, portfolio_margin_live_api_mock(), exchange="binance")


def test_lower_budget_keeps_namespace_and_unknown_reservations(default_conf, tmp_path, mocker):
    from freqtrade.exchange.binance_order_intent import PortfolioOrderIntent

    config = configured(default_conf, tmp_path)
    api = portfolio_margin_live_api_mock()
    api.fetch_positions.return_value = []
    api.fetch_open_orders.return_value = []
    exchange = get_patched_exchange(mocker, config, api, exchange="binance")
    store = exchange._portfolio_order_intent_store
    store.add(PortfolioOrderIntent("ftpm-unknown", "BTC/USDT:USDT", "regular"))
    lower = configured(default_conf, tmp_path, 500)
    restarted = get_patched_exchange(mocker, lower, api, exchange="binance")
    assert restarted._portfolio_order_intent_store.path == store.path
    assert len(restarted._portfolio_order_intent_store.intents) == 1
    assert restarted.portfolio_margin_unknown_order_latched


def test_dynamic_slots_include_position_exposure(default_conf, tmp_path, mocker):
    config = configured(default_conf, tmp_path)
    config["bot_risk"]["max_occupied_symbols"] = config["max_open_trades"] = 1
    config["exchange"]["portfolio_margin_risk"] = portfolio_policy(config["bot_risk"])
    api = portfolio_margin_live_api_mock()
    api.fetch_positions.return_value = [
        {"symbol": "BTC/USDT:USDT", "contracts": 1, "notional": 10, "side": "long", "leverage": 1}
    ]
    api.fetch_open_orders.return_value = []
    exchange = get_patched_exchange(mocker, config, api, exchange="binance")
    mocker.patch.object(exchange, "_reconcile_portfolio_entry_reservations")
    with pytest.raises(InvalidOrderException, match="slot limit"):
        exchange._validate_portfolio_margin_dca_projected_exposure(
            "ETH/USDT:USDT", 1, "ftpm-next", "buy"
        )
