import json
import os
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from random import randint
from threading import Event
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock
from urllib.parse import parse_qs, urlparse

import ccxt
import pandas as pd
import pytest

from freqtrade.data.converter.trade_converter import trades_dict_to_list
from freqtrade.enums import CandleType, MarginMode, PriceType, RunMode, TradingMode
from freqtrade.exceptions import (
    DependencyException,
    InvalidOrderException,
    OperationalException,
    TemporaryError,
)
from freqtrade.exchange.exchange_utils_timeframe import timeframe_to_seconds
from freqtrade.misc import deep_merge_dicts
from freqtrade.persistence import Trade
from freqtrade.util.datetime_helpers import dt_from_ts, dt_ts, dt_utc
from freqtrade.wallets import PositionWallet
from tests.conftest import EXMS, get_patched_exchange
from tests.exchange.test_exchange import ccxt_exceptionhandlers


def portfolio_margin_conf(default_conf, *, dry_run: bool = True):
    conf = deepcopy(default_conf)
    conf["dry_run"] = dry_run
    conf["trading_mode"] = TradingMode.FUTURES
    conf["margin_mode"] = MarginMode.CROSS
    conf["stake_currency"] = "USDT"
    conf["exchange"]["pair_whitelist"] = ["ETH/USDT:USDT"]
    conf["exchange"]["ccxt_config"] = {
        "options": {
            "defaultType": "swap",
            "portfolioMargin": True,
        }
    }
    return conf


def portfolio_margin_risk_conf(default_conf, *, dry_run: bool = True):
    conf = portfolio_margin_conf(default_conf, dry_run=dry_run)
    pair = conf["exchange"]["pair_whitelist"][0]
    conf["exchange"]["portfolio_margin_risk"] = {
        "pair": pair,
        "side": "long",
        "max_leverage": 1,
        "max_entry_notional": 100,
        "force_entry_order_type": "market",
        "reject_force_entry_price": True,
    }
    return conf


CHAN_PAIR_LIMITS = {
    "BTC/USDT:USDT": 100,
    "ETH/USDT:USDT": 100,
    "BNB/USDT:USDT": 100,
    "SOL/USDT:USDT": 100,
    "SPY/USDT:USDT": 100,
}


def portfolio_margin_chan_risk_conf(default_conf, *, dry_run: bool = True):
    conf = portfolio_margin_conf(default_conf, dry_run=dry_run)
    conf["exchange"]["pair_whitelist"] = list(CHAN_PAIR_LIMITS)
    conf["exchange"]["portfolio_margin_risk"] = {
        "account_namespace": "chan-live-account",
        "policy": "chan_multi_pair",
        "pairs": CHAN_PAIR_LIMITS.copy(),
        "allowed_sides": ["long", "short"],
        "max_leverage": 1,
        "max_total_entry_notional": 500,
        "force_entry_order_type": "disabled",
        "reject_force_entry_price": True,
    }
    return conf


def persistent_portfolio_margin_conf(default_conf, tmp_path):
    conf = portfolio_margin_risk_conf(default_conf, dry_run=False)
    conf["user_data_dir"] = tmp_path
    conf["db_url"] = "sqlite:///portfolio-margin-intent-test.sqlite"
    conf["bot_name"] = "portfolio-margin-intent-test"
    conf["runmode"] = RunMode.LIVE
    return conf


def persistent_portfolio_margin_chan_conf(default_conf, tmp_path):
    conf = portfolio_margin_chan_risk_conf(default_conf, dry_run=False)
    conf["user_data_dir"] = tmp_path
    conf["db_url"] = "sqlite:///portfolio-margin-chan-reservation-test.sqlite"
    conf["bot_name"] = "portfolio-margin-chan-reservation-test"
    conf["runmode"] = RunMode.LIVE
    return conf


def configure_portfolio_algo_api_mock(api_mock):
    api_mock.request.return_value = []
    api_mock.market.side_effect = lambda pair: {
        "id": pair.replace("/", "").split(":")[0],
        "symbol": pair,
    }
    api_mock.parse_order.side_effect = lambda order, market=None: order
    api_mock.parse_orders.side_effect = lambda orders, market=None: orders
    return api_mock


def portfolio_algo_create_request(client_order_id: str) -> dict:
    return {
        "symbol": "ETHUSDT",
        "side": "SELL",
        "clientAlgoId": client_order_id,
        "newOrderRespType": "RESULT",
        "type": "STOP_MARKET",
        "quantity": "1",
        "triggerPrice": "1900",
        "reduceOnly": True,
        "maxRetriesOnFailure": 0,
    }


def portfolio_algo_order(order_id: str, client_order_id: str, status: str = "open") -> dict:
    return {
        "id": order_id,
        "clientOrderId": client_order_id,
        "symbol": "ETH/USDT:USDT",
        "type": "stop_market",
        "amount": 1.0,
        "filled": 0.0,
        "remaining": 1.0,
        "status": status,
        "info": {
            "algoId": order_id,
            "clientAlgoId": client_order_id,
        },
    }


def portfolio_margin_live_api_mock():
    api_mock = MagicMock(
        **{
            "fetch_leverage_tiers.return_value": {},
            "papiGetUmPositionSideDual.return_value": {"dualSidePosition": False},
            "papiGetUmAccountConfig.return_value": {"canTrade": True},
            "fetch_open_orders.return_value": [],
            "papiGetCmPositionRisk.return_value": [],
            "papiGetCmOpenOrders.return_value": [],
            "papiGetCmConditionalOpenOrders.return_value": [],
            "papiGetMarginOpenOrders.return_value": [],
            "papiGetMarginOpenOrderList.return_value": [],
        }
    )
    return configure_portfolio_algo_api_mock(api_mock)


def portfolio_margin_position(
    pair: str = "ETH/USDT:USDT",
    *,
    contracts: float = 0.025,
    side: str = "long",
) -> dict:
    return {
        "symbol": pair,
        "contracts": contracts,
        "side": side,
        "leverage": 1,
        "marginMode": "cross",
        "collateral": 50,
    }


def unknown_entry_containment_exchange(
    default_conf,
    mocker,
    position_snapshots: list[list[dict]],
    *,
    open_orders: list[dict] | None = None,
    open_order_snapshots: list[list[dict]] | None = None,
):
    mocker.patch("freqtrade.exchange.binance.sleep")
    if position_snapshots and len(position_snapshots) < 10:
        position_snapshots = [
            *position_snapshots,
            *([position_snapshots[-1]] * (10 - len(position_snapshots))),
        ]
    if open_order_snapshots and len(open_order_snapshots) < 10:
        open_order_snapshots = [
            *open_order_snapshots,
            *([open_order_snapshots[-1]] * (10 - len(open_order_snapshots))),
        ]
    api_mock = MagicMock()
    api_mock.fetch_leverage_tiers.return_value = {}
    api_mock.papiGetUmPositionSideDual.return_value = {"dualSidePosition": False}
    api_mock.papiGetUmAccountConfig.return_value = {"canTrade": True}
    api_mock.fetch_order.side_effect = ccxt.OrderNotFound("not visible")
    if open_order_snapshots is None:
        api_mock.fetch_open_orders.return_value = list(open_orders or [])
    else:
        api_mock.fetch_open_orders.side_effect = open_order_snapshots
    api_mock.fetch_positions.side_effect = position_snapshots
    api_mock.create_order.side_effect = [
        ccxt.RequestTimeout("entry status unknown"),
        *[
            {
                "id": f"emergency-close-{index}",
                "clientOrderId": f"ftpm-close-{index}",
                "symbol": "ETH/USDT:USDT",
                "status": "closed",
                "info": {},
            }
            for index in range(10)
        ],
    ]
    type(api_mock).has = PropertyMock(return_value={"setLeverage": False})
    mocker.patch(f"{EXMS}.amount_to_precision", lambda s, x, y: y)
    mocker.patch(f"{EXMS}.price_to_precision", lambda s, x, y, **kwargs: y)
    exchange = get_patched_exchange(
        mocker,
        portfolio_margin_risk_conf(default_conf, dry_run=False),
        api_mock,
        exchange="binance",
    )
    exchange._portfolio_order_recovery_attempts = 1
    client_ids = iter(["ftpm-entry", *[f"ftpm-close-{index}" for index in range(10)]])
    mocker.patch.object(
        exchange,
        "_new_portfolio_client_order_id",
        side_effect=lambda: next(client_ids),
    )
    return exchange, api_mock


@pytest.mark.parametrize(
    "side,order_type,time_in_force,expected",
    [
        ("buy", "limit", "gtc", {"timeInForce": "GTC"}),
        ("buy", "limit", "IOC", {"timeInForce": "IOC"}),
        ("buy", "market", "IOC", {}),
        ("buy", "limit", "PO", {"timeInForce": "PO"}),
        ("sell", "limit", "PO", {"timeInForce": "PO"}),
        ("sell", "market", "PO", {}),
    ],
)
def test__get_params_binance(default_conf, mocker, side, order_type, time_in_force, expected):
    exchange = get_patched_exchange(mocker, default_conf, exchange="binance")
    assert exchange._get_params(side, order_type, 1, False, time_in_force) == expected


@pytest.mark.parametrize("trademode", [TradingMode.FUTURES, TradingMode.SPOT])
@pytest.mark.parametrize(
    "limitratio,expected,side",
    [
        (None, 220 * 0.99, "sell"),
        (0.99, 220 * 0.99, "sell"),
        (0.98, 220 * 0.98, "sell"),
        (None, 220 * 1.01, "buy"),
        (0.99, 220 * 1.01, "buy"),
        (0.98, 220 * 1.02, "buy"),
    ],
)
def test_create_stoploss_order_binance(default_conf, mocker, limitratio, expected, side, trademode):
    api_mock = MagicMock()
    order_id = f"test_prod_buy_{randint(0, 10**6)}"
    order_type = "stop_loss_limit" if trademode == TradingMode.SPOT else "stop"

    api_mock.create_order = MagicMock(return_value={"id": order_id, "info": {"foo": "bar"}})
    default_conf["dry_run"] = False
    default_conf["margin_mode"] = MarginMode.ISOLATED
    default_conf["trading_mode"] = trademode
    mocker.patch(f"{EXMS}.amount_to_precision", lambda s, x, y: y)
    mocker.patch(f"{EXMS}.price_to_precision", lambda s, x, y, **kwargs: y)

    exchange = get_patched_exchange(mocker, default_conf, api_mock, "binance")

    with pytest.raises(InvalidOrderException):
        order = exchange.create_stoploss(
            pair="ETH/BTC",
            amount=1,
            stop_price=190,
            side=side,
            order_types={"stoploss": "limit", "stoploss_on_exchange_limit_ratio": 1.05},
            leverage=1.0,
        )

    api_mock.create_order.reset_mock()
    order_types = {"stoploss": "limit", "stoploss_price_type": "mark"}
    if limitratio is not None:
        order_types.update({"stoploss_on_exchange_limit_ratio": limitratio})

    order = exchange.create_stoploss(
        pair="ETH/BTC", amount=1, stop_price=220, order_types=order_types, side=side, leverage=1.0
    )

    assert "id" in order
    assert "info" in order
    assert order["id"] == order_id
    assert api_mock.create_order.call_args_list[0][1]["symbol"] == "ETH/BTC"
    assert api_mock.create_order.call_args_list[0][1]["type"] == order_type
    assert api_mock.create_order.call_args_list[0][1]["side"] == side
    assert api_mock.create_order.call_args_list[0][1]["amount"] == 1
    # Price should be 1% below stopprice
    assert api_mock.create_order.call_args_list[0][1]["price"] == expected
    if trademode == TradingMode.SPOT:
        params_dict = {"stopPrice": 220}
    else:
        params_dict = {"stopPrice": 220, "reduceOnly": True, "workingType": "MARK_PRICE"}
    assert api_mock.create_order.call_args_list[0][1]["params"] == params_dict

    # test exception handling
    with pytest.raises(DependencyException):
        api_mock.create_order = MagicMock(side_effect=ccxt.InsufficientFunds("0 balance"))
        exchange = get_patched_exchange(mocker, default_conf, api_mock, "binance")
        exchange.create_stoploss(
            pair="ETH/BTC", amount=1, stop_price=220, order_types={}, side=side, leverage=1.0
        )

    with pytest.raises(InvalidOrderException):
        api_mock.create_order = MagicMock(
            side_effect=ccxt.InvalidOrder("binance Order would trigger immediately.")
        )
        exchange = get_patched_exchange(mocker, default_conf, api_mock, "binance")
        exchange.create_stoploss(
            pair="ETH/BTC", amount=1, stop_price=220, order_types={}, side=side, leverage=1.0
        )

    ccxt_exceptionhandlers(
        mocker,
        default_conf,
        api_mock,
        "binance",
        "create_stoploss",
        "create_order",
        retries=1,
        pair="ETH/BTC",
        amount=1,
        stop_price=220,
        order_types={},
        side=side,
        leverage=1.0,
    )


def test_create_stoploss_order_dry_run_binance(default_conf, mocker):
    api_mock = MagicMock()
    order_type = "stop_loss_limit"
    default_conf["dry_run"] = True
    mocker.patch(f"{EXMS}.amount_to_precision", lambda s, x, y: y)
    mocker.patch(f"{EXMS}.price_to_precision", lambda s, x, y, **kwargs: y)

    exchange = get_patched_exchange(mocker, default_conf, api_mock, "binance")

    with pytest.raises(InvalidOrderException):
        order = exchange.create_stoploss(
            pair="ETH/BTC",
            amount=1,
            stop_price=190,
            side="sell",
            order_types={"stoploss_on_exchange_limit_ratio": 1.05},
            leverage=1.0,
        )

    api_mock.create_order.reset_mock()

    order = exchange.create_stoploss(
        pair="ETH/BTC", amount=1, stop_price=220, order_types={}, side="sell", leverage=1.0
    )

    assert "id" in order
    assert "info" in order
    assert "type" in order

    assert order["type"] == order_type
    assert order["price"] == 217.8
    assert order["stopPrice"] == 220
    assert order["amount"] == 1


@pytest.mark.parametrize(
    "sl1,sl2,sl3,side", [(1501, 1499, 1501, "sell"), (1499, 1501, 1499, "buy")]
)
def test_stoploss_adjust_binance(mocker, default_conf, sl1, sl2, sl3, side):
    exchange = get_patched_exchange(mocker, default_conf, exchange="binance")
    order = {
        "type": "stop_loss_limit",
        "price": 1500,
        "stopPrice": 1500,
        "info": {"stopPrice": 1500},
    }
    assert exchange.stoploss_adjust(sl1, order, side=side)
    assert not exchange.stoploss_adjust(sl2, order, side=side)


@pytest.mark.parametrize(
    "pair, is_short, trading_mode, margin_mode, wallet_balance, "
    "maintenance_amt, amount, open_rate, open_trades,"
    "mm_ratio, expected",
    [
        (
            "ETH/USDT:USDT",
            False,
            "futures",
            "isolated",
            1535443.01,
            135365.00,
            3683.979,
            1456.84,
            [],
            0.10,
            1114.78,
        ),
        (
            "ETH/USDT:USDT",
            False,
            "futures",
            "isolated",
            1535443.01,
            16300.000,
            109.488,
            32481.980,
            [],
            0.025,
            18778.73,
        ),
        (
            "ETH/USDT:USDT",
            False,
            "futures",
            "cross",
            1535443.01,
            135365.00,
            3683.979,  # amount
            1456.84,  # open_rate
            [
                {
                    # From calc example
                    "pair": "BTC/USDT:USDT",
                    "open_rate": 32481.98,
                    "amount": 109.488,
                    "stake_amount": 3556387.02624,  # open_rate * amount
                    "mark_price": 31967.27,
                    "mm_ratio": 0.025,
                    "maintenance_amt": 16300.0,
                },
                {
                    # From calc example
                    "pair": "ETH/USDT:USDT",
                    "open_rate": 1456.84,
                    "amount": 3683.979,
                    "stake_amount": 5366967.96,
                    "mark_price": 1335.18,
                    "mm_ratio": 0.10,
                    "maintenance_amt": 135365.00,
                },
            ],
            0.10,
            1153.26,
        ),
        (
            "BTC/USDT:USDT",
            False,
            "futures",
            "cross",
            1535443.01,
            16300.0,
            109.488,  # amount
            32481.980,  # open_rate
            [
                {
                    # From calc example
                    "pair": "BTC/USDT:USDT",
                    "open_rate": 32481.98,
                    "amount": 109.488,
                    "stake_amount": 3556387.02624,  # open_rate * amount
                    "mark_price": 31967.27,
                    "mm_ratio": 0.025,
                    "maintenance_amt": 16300.0,
                },
                {
                    # From calc example
                    "pair": "ETH/USDT:USDT",
                    "open_rate": 1456.84,
                    "amount": 3683.979,
                    "stake_amount": 5366967.96,
                    "mark_price": 1335.18,
                    "mm_ratio": 0.10,
                    "maintenance_amt": 135365.00,
                },
            ],
            0.025,
            26316.89,
        ),
    ],
)
def test_liquidation_price_binance(
    mocker,
    default_conf,
    pair,
    is_short,
    trading_mode,
    margin_mode,
    wallet_balance,
    maintenance_amt,
    amount,
    open_rate,
    open_trades,
    mm_ratio,
    expected,
):
    default_conf["trading_mode"] = trading_mode
    default_conf["margin_mode"] = margin_mode
    default_conf["liquidation_buffer"] = 0.0
    mocker.patch(f"{EXMS}.price_to_precision", lambda s, x, y, **kwargs: y)
    exchange = get_patched_exchange(mocker, default_conf, exchange="binance")

    def get_maint_ratio(pair_, stake_amount):
        if pair_ != pair:
            oc = next(c for c in open_trades if c["pair"] == pair_)
            return oc["mm_ratio"], oc["maintenance_amt"]
        return mm_ratio, maintenance_amt

    def fetch_funding_rates(*args, **kwargs):
        return {
            t["pair"]: {
                "symbol": t["pair"],
                "markPrice": t["mark_price"],
            }
            for t in open_trades
        }

    exchange.get_maintenance_ratio_and_amt = get_maint_ratio
    exchange.fetch_funding_rates = fetch_funding_rates

    open_trade_objects = [
        Trade(
            pair=t["pair"],
            open_rate=t["open_rate"],
            amount=t["amount"],
            stake_amount=t["stake_amount"],
            fee_open=0,
        )
        for t in open_trades
    ]

    assert (
        pytest.approx(
            round(
                exchange.get_liquidation_price(
                    pair=pair,
                    open_rate=open_rate,
                    is_short=is_short,
                    wallet_balance=wallet_balance,
                    amount=amount,
                    stake_amount=open_rate * amount,
                    leverage=5,
                    open_trades=open_trade_objects,
                ),
                2,
            )
        )
        == expected
    )


def test_fill_leverage_tiers_binance(default_conf, mocker):
    api_mock = MagicMock()
    api_mock.fetch_leverage_tiers = MagicMock(
        return_value={
            "ADA/BUSD": [
                {
                    "tier": 1,
                    "minNotional": 0,
                    "maxNotional": 100000,
                    "maintenanceMarginRate": 0.025,
                    "maxLeverage": 20,
                    "info": {
                        "bracket": "1",
                        "initialLeverage": "20",
                        "maxNotional": "100000",
                        "minNotional": "0",
                        "maintMarginRatio": "0.025",
                        "cum": "0.0",
                    },
                },
                {
                    "tier": 2,
                    "minNotional": 100000,
                    "maxNotional": 500000,
                    "maintenanceMarginRate": 0.05,
                    "maxLeverage": 10,
                    "info": {
                        "bracket": "2",
                        "initialLeverage": "10",
                        "maxNotional": "500000",
                        "minNotional": "100000",
                        "maintMarginRatio": "0.05",
                        "cum": "2500.0",
                    },
                },
                {
                    "tier": 3,
                    "minNotional": 500000,
                    "maxNotional": 1000000,
                    "maintenanceMarginRate": 0.1,
                    "maxLeverage": 5,
                    "info": {
                        "bracket": "3",
                        "initialLeverage": "5",
                        "maxNotional": "1000000",
                        "minNotional": "500000",
                        "maintMarginRatio": "0.1",
                        "cum": "27500.0",
                    },
                },
                {
                    "tier": 4,
                    "minNotional": 1000000,
                    "maxNotional": 2000000,
                    "maintenanceMarginRate": 0.15,
                    "maxLeverage": 3,
                    "info": {
                        "bracket": "4",
                        "initialLeverage": "3",
                        "maxNotional": "2000000",
                        "minNotional": "1000000",
                        "maintMarginRatio": "0.15",
                        "cum": "77500.0",
                    },
                },
                {
                    "tier": 5,
                    "minNotional": 2000000,
                    "maxNotional": 5000000,
                    "maintenanceMarginRate": 0.25,
                    "maxLeverage": 2,
                    "info": {
                        "bracket": "5",
                        "initialLeverage": "2",
                        "maxNotional": "5000000",
                        "minNotional": "2000000",
                        "maintMarginRatio": "0.25",
                        "cum": "277500.0",
                    },
                },
                {
                    "tier": 6,
                    "minNotional": 5000000,
                    "maxNotional": 30000000,
                    "maintenanceMarginRate": 0.5,
                    "maxLeverage": 1,
                    "info": {
                        "bracket": "6",
                        "initialLeverage": "1",
                        "maxNotional": "30000000",
                        "minNotional": "5000000",
                        "maintMarginRatio": "0.5",
                        "cum": "1527500.0",
                    },
                },
            ],
            "ZEC/USDT": [
                {
                    "tier": 1,
                    "minNotional": 0,
                    "maxNotional": 50000,
                    "maintenanceMarginRate": 0.01,
                    "maxLeverage": 50,
                    "info": {
                        "bracket": "1",
                        "initialLeverage": "50",
                        "maxNotional": "50000",
                        "minNotional": "0",
                        "maintMarginRatio": "0.01",
                        "cum": "0.0",
                    },
                },
                {
                    "tier": 2,
                    "minNotional": 50000,
                    "maxNotional": 150000,
                    "maintenanceMarginRate": 0.025,
                    "maxLeverage": 20,
                    "info": {
                        "bracket": "2",
                        "initialLeverage": "20",
                        "maxNotional": "150000",
                        "minNotional": "50000",
                        "maintMarginRatio": "0.025",
                        "cum": "750.0",
                    },
                },
                {
                    "tier": 3,
                    "minNotional": 150000,
                    "maxNotional": 250000,
                    "maintenanceMarginRate": 0.05,
                    "maxLeverage": 10,
                    "info": {
                        "bracket": "3",
                        "initialLeverage": "10",
                        "maxNotional": "250000",
                        "minNotional": "150000",
                        "maintMarginRatio": "0.05",
                        "cum": "4500.0",
                    },
                },
                {
                    "tier": 4,
                    "minNotional": 250000,
                    "maxNotional": 500000,
                    "maintenanceMarginRate": 0.1,
                    "maxLeverage": 5,
                    "info": {
                        "bracket": "4",
                        "initialLeverage": "5",
                        "maxNotional": "500000",
                        "minNotional": "250000",
                        "maintMarginRatio": "0.1",
                        "cum": "17000.0",
                    },
                },
                {
                    "tier": 5,
                    "minNotional": 500000,
                    "maxNotional": 1000000,
                    "maintenanceMarginRate": 0.125,
                    "maxLeverage": 4,
                    "info": {
                        "bracket": "5",
                        "initialLeverage": "4",
                        "maxNotional": "1000000",
                        "minNotional": "500000",
                        "maintMarginRatio": "0.125",
                        "cum": "29500.0",
                    },
                },
                {
                    "tier": 6,
                    "minNotional": 1000000,
                    "maxNotional": 2000000,
                    "maintenanceMarginRate": 0.25,
                    "maxLeverage": 2,
                    "info": {
                        "bracket": "6",
                        "initialLeverage": "2",
                        "maxNotional": "2000000",
                        "minNotional": "1000000",
                        "maintMarginRatio": "0.25",
                        "cum": "154500.0",
                    },
                },
                {
                    "tier": 7,
                    "minNotional": 2000000,
                    "maxNotional": 30000000,
                    "maintenanceMarginRate": 0.5,
                    "maxLeverage": 1,
                    "info": {
                        "bracket": "7",
                        "initialLeverage": "1",
                        "maxNotional": "30000000",
                        "minNotional": "2000000",
                        "maintMarginRatio": "0.5",
                        "cum": "654500.0",
                    },
                },
            ],
        }
    )
    default_conf["dry_run"] = False
    default_conf["trading_mode"] = TradingMode.FUTURES
    default_conf["margin_mode"] = MarginMode.ISOLATED
    exchange = get_patched_exchange(mocker, default_conf, api_mock, exchange="binance")
    exchange.fill_leverage_tiers()

    assert exchange._leverage_tiers == {
        "ADA/BUSD": [
            {
                "minNotional": 0,
                "maxNotional": 100000,
                "maintenanceMarginRate": 0.025,
                "maxLeverage": 20,
                "maintAmt": 0.0,
            },
            {
                "minNotional": 100000,
                "maxNotional": 500000,
                "maintenanceMarginRate": 0.05,
                "maxLeverage": 10,
                "maintAmt": 2500.0,
            },
            {
                "minNotional": 500000,
                "maxNotional": 1000000,
                "maintenanceMarginRate": 0.1,
                "maxLeverage": 5,
                "maintAmt": 27500.0,
            },
            {
                "minNotional": 1000000,
                "maxNotional": 2000000,
                "maintenanceMarginRate": 0.15,
                "maxLeverage": 3,
                "maintAmt": 77500.0,
            },
            {
                "minNotional": 2000000,
                "maxNotional": 5000000,
                "maintenanceMarginRate": 0.25,
                "maxLeverage": 2,
                "maintAmt": 277500.0,
            },
            {
                "minNotional": 5000000,
                "maxNotional": 30000000,
                "maintenanceMarginRate": 0.5,
                "maxLeverage": 1,
                "maintAmt": 1527500.0,
            },
        ],
        "ZEC/USDT": [
            {
                "minNotional": 0,
                "maxNotional": 50000,
                "maintenanceMarginRate": 0.01,
                "maxLeverage": 50,
                "maintAmt": 0.0,
            },
            {
                "minNotional": 50000,
                "maxNotional": 150000,
                "maintenanceMarginRate": 0.025,
                "maxLeverage": 20,
                "maintAmt": 750.0,
            },
            {
                "minNotional": 150000,
                "maxNotional": 250000,
                "maintenanceMarginRate": 0.05,
                "maxLeverage": 10,
                "maintAmt": 4500.0,
            },
            {
                "minNotional": 250000,
                "maxNotional": 500000,
                "maintenanceMarginRate": 0.1,
                "maxLeverage": 5,
                "maintAmt": 17000.0,
            },
            {
                "minNotional": 500000,
                "maxNotional": 1000000,
                "maintenanceMarginRate": 0.125,
                "maxLeverage": 4,
                "maintAmt": 29500.0,
            },
            {
                "minNotional": 1000000,
                "maxNotional": 2000000,
                "maintenanceMarginRate": 0.25,
                "maxLeverage": 2,
                "maintAmt": 154500.0,
            },
            {
                "minNotional": 2000000,
                "maxNotional": 30000000,
                "maintenanceMarginRate": 0.5,
                "maxLeverage": 1,
                "maintAmt": 654500.0,
            },
        ],
    }

    api_mock = MagicMock()
    api_mock.load_leverage_tiers = MagicMock()
    type(api_mock).has = PropertyMock(return_value={"fetchLeverageTiers": True})

    ccxt_exceptionhandlers(
        mocker,
        default_conf,
        api_mock,
        "binance",
        "fill_leverage_tiers",
        "fetch_leverage_tiers",
    )


def test_fill_leverage_tiers_binance_dryrun(default_conf, mocker, leverage_tiers):
    api_mock = MagicMock()
    default_conf["trading_mode"] = TradingMode.FUTURES
    default_conf["margin_mode"] = MarginMode.ISOLATED
    exchange = get_patched_exchange(mocker, default_conf, api_mock, exchange="binance")
    exchange.fill_leverage_tiers()
    assert len(exchange._leverage_tiers.keys()) > 100
    for key, value in leverage_tiers.items():
        v = exchange._leverage_tiers[key]
        assert isinstance(v, list)
        # Assert if conftest leverage tiers have less or equal tiers than the exchange
        assert len(v) >= len(value)


def test_additional_exchange_init_binance(default_conf, mocker):
    api_mock = MagicMock()
    api_mock.fapiPrivateGetPositionSideDual = MagicMock(return_value={"dualSidePosition": True})
    api_mock.fapiPrivateGetMultiAssetsMargin = MagicMock(return_value={"multiAssetsMargin": True})
    default_conf["dry_run"] = False
    default_conf["trading_mode"] = TradingMode.FUTURES
    default_conf["margin_mode"] = MarginMode.ISOLATED
    with pytest.raises(
        OperationalException,
        match=r"Hedge Mode is not supported.*\nMulti-Asset Mode is not supported.*",
    ):
        get_patched_exchange(mocker, default_conf, exchange="binance", api_mock=api_mock)
    api_mock.fapiPrivateGetPositionSideDual = MagicMock(return_value={"dualSidePosition": False})
    api_mock.fapiPrivateGetMultiAssetsMargin = MagicMock(return_value={"multiAssetsMargin": False})
    exchange = get_patched_exchange(mocker, default_conf, exchange="binance", api_mock=api_mock)
    assert exchange
    assert exchange._portfolio_create_lock is None
    ccxt_exceptionhandlers(
        mocker,
        default_conf,
        api_mock,
        "binance",
        "additional_exchange_init",
        "fapiPrivateGetPositionSideDual",
    )


def test_binance_portfolio_margin_rejects_unsupported_configs(default_conf, mocker):
    conf = portfolio_margin_conf(default_conf)
    conf["margin_mode"] = MarginMode.ISOLATED
    with pytest.raises(OperationalException, match="futures trading with cross margin"):
        get_patched_exchange(mocker, conf, exchange="binance")

    conf = portfolio_margin_conf(default_conf)
    conf["trading_mode"] = TradingMode.SPOT
    conf["margin_mode"] = MarginMode.NONE
    with pytest.raises(OperationalException, match="futures trading with cross margin"):
        get_patched_exchange(mocker, conf, exchange="binance")

    conf = portfolio_margin_conf(default_conf)
    conf["exchange"]["ccxt_config"]["options"]["portfolioMarginPro"] = True
    with pytest.raises(OperationalException, match="Portfolio Margin Pro/PAPI v2 is not supported"):
        get_patched_exchange(mocker, conf, exchange="binance")

    conf = portfolio_margin_conf(default_conf)
    conf["exchange"]["ccxt_config"]["options"]["defaultType"] = "delivery"
    with pytest.raises(OperationalException, match="only supports linear USD-M perpetual"):
        get_patched_exchange(mocker, conf, exchange="binance")

    conf = portfolio_margin_conf(default_conf)
    conf["exchange"]["ccxt_config"]["options"]["papi"] = False
    with pytest.raises(OperationalException, match="cannot disable PAPI"):
        get_patched_exchange(mocker, conf, exchange="binance")

    conf = portfolio_margin_conf(default_conf)
    conf["exchange"]["ccxt_config"]["options"]["loadLeverageBrackets"] = {"papi": False}
    with pytest.raises(OperationalException, match="cannot disable PAPI"):
        get_patched_exchange(mocker, conf, exchange="binance")

    conf = portfolio_margin_conf(default_conf)
    conf["exchange"]["ccxt_sync_config"] = {"options": {"papi": False}}
    with pytest.raises(OperationalException, match="cannot disable PAPI"):
        get_patched_exchange(mocker, conf, exchange="binance")

    conf = portfolio_margin_conf(default_conf)
    conf["exchange"]["ccxt_async_config"] = {"options": {"papi": 0}}
    with pytest.raises(OperationalException, match="cannot disable PAPI"):
        get_patched_exchange(mocker, conf, exchange="binance")

    conf = portfolio_margin_conf(default_conf)
    conf["exchange"]["ccxt_sync_config"] = {"options": {"defaultPapi": False}}
    with pytest.raises(OperationalException, match="cannot disable PAPI"):
        get_patched_exchange(mocker, conf, exchange="binance")

    conf = portfolio_margin_conf(default_conf)
    conf["exchange"]["ccxt_async_config"] = {
        "options": {"fetchPositionsRisk": {"defaultPortfolioMargin": False}}
    }
    with pytest.raises(OperationalException, match="cannot disable PAPI"):
        get_patched_exchange(mocker, conf, exchange="binance")

    conf = portfolio_margin_conf(default_conf)
    conf["exchange"]["ccxt_sync_config"] = {"options": {"papiV2": True}}
    with pytest.raises(OperationalException, match="Pro/PAPI v2"):
        get_patched_exchange(mocker, conf, exchange="binance")

    conf = portfolio_margin_conf(default_conf)
    conf["exchange"]["ccxt_async_config"] = {
        "options": {"fetchPositionsRisk": {"defaultUseV2": True}}
    }
    with pytest.raises(OperationalException, match="Pro/PAPI v2"):
        get_patched_exchange(mocker, conf, exchange="binance")

    conf = portfolio_margin_conf(default_conf)
    conf["exchange"]["ccxt_config"]["options"]["fetchCurrencies"] = True
    with pytest.raises(OperationalException, match="fetchCurrencies=false"):
        get_patched_exchange(mocker, conf, exchange="binance")

    conf = portfolio_margin_conf(default_conf)
    conf["exchange"]["ccxt_config"]["options"]["fetchOpenOrders"] = {"warnWithoutSymbol": True}
    with pytest.raises(OperationalException, match="no-symbol warning"):
        get_patched_exchange(mocker, conf, exchange="binance")

    conf = portfolio_margin_conf(default_conf)
    conf["exchange"]["ccxt_config"]["options"]["fetchPositions"] = {"method": "account"}
    with pytest.raises(OperationalException, match="method=positionRisk"):
        get_patched_exchange(mocker, conf, exchange="binance")

    conf = portfolio_margin_conf(default_conf)
    conf["exchange"]["ccxt_async_config"] = {"options": {"fetchPositions": "account"}}
    with pytest.raises(OperationalException, match="method=positionRisk"):
        get_patched_exchange(mocker, conf, exchange="binance")

    conf = portfolio_margin_conf(default_conf)
    conf["exchange"]["ccxt_async_config"] = {
        "options": {"fetchPositions": {"defaultMethod": "account"}}
    }
    with pytest.raises(OperationalException, match="method=positionRisk"):
        get_patched_exchange(mocker, conf, exchange="binance")

    conf = portfolio_margin_conf(default_conf)
    conf["exchange"]["ccxt_config"]["options"]["fetchOpenOrders"] = {
        "warnWithoutSymbol": False,
        "defaultType": "margin",
    }
    with pytest.raises(OperationalException, match="must remain linear USD-M cross futures"):
        get_patched_exchange(mocker, conf, exchange="binance")

    conf = portfolio_margin_conf(default_conf)
    conf["exchange"]["ccxt_async_config"] = {
        "options": {"loadLeverageBrackets": {"defaultSubType": "inverse"}}
    }
    with pytest.raises(OperationalException, match="must remain linear USD-M cross futures"):
        get_patched_exchange(mocker, conf, exchange="binance")

    conf = portfolio_margin_conf(default_conf)
    conf["exchange"]["ccxt_config"]["options"]["maxRetriesOnFailure"] = 1
    with pytest.raises(OperationalException, match="automatic request retries"):
        get_patched_exchange(mocker, conf, exchange="binance")

    conf = portfolio_margin_conf(default_conf)
    conf["exchange"]["ccxt_async_config"] = {
        "options": {"createOrder": {"defaultMaxRetriesOnFailure": 1}}
    }
    with pytest.raises(OperationalException, match="automatic request retries"):
        get_patched_exchange(mocker, conf, exchange="binance")

    conf = portfolio_margin_conf(default_conf)
    conf["exchange"]["ccxt_async_config"] = {"timeout": 5_001}
    with pytest.raises(OperationalException, match="timeout must be at most 5000 ms"):
        get_patched_exchange(mocker, conf, exchange="binance")

    conf = portfolio_margin_conf(default_conf)
    conf["force_entry_enable"] = True
    with pytest.raises(OperationalException, match="force-entry requires"):
        get_patched_exchange(mocker, conf, exchange="binance")

    conf = portfolio_margin_risk_conf(default_conf)
    conf["exchange"]["portfolio_margin_risk"]["max_entry_notional"] = 101
    with pytest.raises(OperationalException, match="at most 100 USDT"):
        get_patched_exchange(mocker, conf, exchange="binance")

    conf = portfolio_margin_risk_conf(default_conf)
    conf["exchange"]["portfolio_margin_risk"]["max_leverage"] = True
    with pytest.raises(OperationalException, match="1x leverage"):
        get_patched_exchange(mocker, conf, exchange="binance")


def test_binance_validates_portfolio_margin_chan_risk(default_conf, mocker):
    conf = portfolio_margin_chan_risk_conf(default_conf)
    exchange = get_patched_exchange(mocker, conf, exchange="binance")
    assert exchange.portfolio_margin_risk == conf["exchange"]["portfolio_margin_risk"]

    invalid_configs = []
    invalid = portfolio_margin_chan_risk_conf(default_conf)
    invalid["exchange"]["portfolio_margin_risk"]["pairs"].pop("SPY/USDT:USDT")
    invalid_configs.append(invalid)
    invalid = portfolio_margin_chan_risk_conf(default_conf)
    invalid["exchange"]["pair_whitelist"].remove("SPY/USDT:USDT")
    invalid_configs.append(invalid)
    invalid = portfolio_margin_chan_risk_conf(default_conf)
    invalid["exchange"]["portfolio_margin_risk"]["pairs"]["BTC/USDT:USDT"] = 100.01
    invalid_configs.append(invalid)
    invalid = portfolio_margin_chan_risk_conf(default_conf)
    invalid["exchange"]["portfolio_margin_risk"]["max_total_entry_notional"] = 500.01
    invalid_configs.append(invalid)
    invalid = portfolio_margin_chan_risk_conf(default_conf)
    invalid["exchange"]["portfolio_margin_risk"]["allowed_sides"] = ["long"]
    invalid_configs.append(invalid)
    invalid = portfolio_margin_chan_risk_conf(default_conf)
    invalid["exchange"]["portfolio_margin_risk"].pop("allowed_sides")
    invalid_configs.append(invalid)
    invalid = portfolio_margin_chan_risk_conf(default_conf)
    invalid["exchange"]["portfolio_margin_risk"]["account_namespace"] = "../unsafe"
    invalid_configs.append(invalid)
    invalid = portfolio_margin_chan_risk_conf(default_conf)
    invalid["exchange"]["portfolio_margin_risk"]["force_entry_order_type"] = "market"
    invalid_configs.append(invalid)

    for invalid_conf in invalid_configs:
        with pytest.raises(OperationalException, match=r"Chan risk policy|policy schema"):
            get_patched_exchange(mocker, invalid_conf, exchange="binance")


@pytest.mark.parametrize(
    ("config_key", "options"),
    [
        ("ccxt_config", {"papi": True}),
        ("ccxt_config", {"papi": 1}),
        ("ccxt_sync_config", {"fetchBalance": {"defaultPapi": "yes"}}),
        ("ccxt_async_config", {"createOrder": {"portfolioMargin": True}}),
    ],
)
def test_binance_rejects_implicit_portfolio_margin_routing(
    default_conf, mocker, config_key, options
):
    conf = deepcopy(default_conf)
    conf["exchange"][config_key] = {"options": options}

    with pytest.raises(OperationalException, match="must be enabled explicitly"):
        get_patched_exchange(mocker, conf, exchange="binance")


def test_binance_non_portfolio_use_v2_behavior_is_unchanged(default_conf, mocker):
    conf = deepcopy(default_conf)
    conf["exchange"]["ccxt_config"] = {"options": {"useV2": True}}

    exchange = get_patched_exchange(mocker, conf, exchange="binance")

    assert exchange._portfolio_margin is False
    assert exchange._portfolio_create_lock is None


def test_binance_non_portfolio_does_not_expose_portfolio_risk(default_conf, mocker):
    conf = deepcopy(default_conf)
    conf["exchange"]["portfolio_margin_risk"] = {
        "pair": "ETH/USDT:USDT",
        "side": "long",
        "max_leverage": 1,
        "max_entry_notional": 50,
        "force_entry_order_type": "market",
        "reject_force_entry_price": True,
    }

    exchange = get_patched_exchange(mocker, conf, exchange="binance")

    assert exchange.portfolio_margin_enabled is False
    assert exchange.portfolio_margin_risk is None
    assert exchange._portfolio_create_lock is None


def test_binance_portfolio_margin_dry_run_does_not_create_order_lock(default_conf, mocker):
    exchange = get_patched_exchange(
        mocker,
        portfolio_margin_conf(default_conf),
        exchange="binance",
    )

    assert exchange.portfolio_margin_enabled is True
    assert exchange._portfolio_create_lock is None


def test_binance_portfolio_margin_rejects_non_linear_market(default_conf, mocker):
    conf = portfolio_margin_conf(default_conf)
    conf["exchange"]["pair_whitelist"] = ["ETH/BTC"]
    with pytest.raises(OperationalException, match="linear USD-M perpetual markets only"):
        get_patched_exchange(mocker, conf, exchange="binance")


def test_binance_portfolio_margin_additional_exchange_init(default_conf, mocker):
    api_mock = MagicMock()
    api_mock.fetch_leverage_tiers.return_value = {}
    api_mock.papiGetUmPositionSideDual.return_value = {"dualSidePosition": False}
    api_mock.papiGetUmAccountConfig.return_value = {"canTrade": True}
    conf = portfolio_margin_conf(default_conf, dry_run=False)

    exchange = get_patched_exchange(mocker, conf, api_mock, exchange="binance")

    assert exchange
    assert exchange._portfolio_create_lock is not None
    api_mock.papiGetUmPositionSideDual.assert_called_once_with()
    api_mock.papiGetUmAccountConfig.assert_called_once_with()
    api_mock.fapiPrivateGetPositionSideDual.assert_not_called()
    api_mock.fapiPrivateGetMultiAssetsMargin.assert_not_called()

    api_mock.papiGetUmPositionSideDual.return_value = {"dualSidePosition": True}
    with pytest.raises(OperationalException, match="One-way Mode"):
        get_patched_exchange(
            mocker,
            portfolio_margin_conf(default_conf, dry_run=False),
            api_mock,
            exchange="binance",
        )

    api_mock.papiGetUmPositionSideDual.return_value = {"dualSidePosition": False}
    api_mock.papiGetUmAccountConfig.return_value = {"canTrade": False}
    with pytest.raises(OperationalException, match="trading permission is disabled"):
        get_patched_exchange(
            mocker,
            portfolio_margin_conf(default_conf, dry_run=False),
            api_mock,
            exchange="binance",
        )


def test_binance_portfolio_margin_private_routes(default_conf, mocker):
    api_mock = MagicMock()
    api_mock.fetch_leverage_tiers.return_value = {}
    api_mock.papiGetUmPositionSideDual.return_value = {"dualSidePosition": False}
    api_mock.papiGetUmAccountConfig.return_value = {"canTrade": True}
    api_mock.fetch_balance.return_value = {"info": {}, "free": {}, "used": {}, "total": {}}
    api_mock.fetch_positions.return_value = []
    api_mock.fetch_funding_history.return_value = [{"amount": 0.12}, {"amount": -0.02}]
    type(api_mock).has = PropertyMock(
        return_value={
            "fetchLeverageTiers": True,
            "setLeverage": True,
            "fetchFundingHistory": True,
        }
    )
    exchange = get_patched_exchange(
        mocker,
        portfolio_margin_conf(default_conf, dry_run=False),
        api_mock,
        exchange="binance",
    )
    api_mock.reset_mock()

    assert exchange._get_params("buy", "limit", 1, False) == {
        "papi": True,
        "portfolioMargin": True,
        "maxRetriesOnFailure": 0,
    }
    assert exchange._get_stop_params("sell", "stop", 100) == {
        "stopPrice": 100,
        "papi": True,
        "portfolioMargin": True,
        "maxRetriesOnFailure": 0,
    }
    assert exchange.get_balances() == {}
    api_mock.fetch_balance.assert_called_once_with(
        {"papi": True, "portfolioMargin": True, "maxRetriesOnFailure": 0}
    )

    assert exchange.fetch_positions() == []
    api_mock.fetch_positions.assert_called_once_with(None, params={"maxRetriesOnFailure": 0})
    api_mock.fetch_positions.reset_mock()
    assert (
        exchange.fetch_positions(
            params={
                "subType": "linear",
                "defaultSubType": "linear",
                "defaultType": "swap",
                "marginMode": "cross",
            }
        )
        == []
    )
    api_mock.fetch_positions.assert_called_once_with(None, params={"maxRetriesOnFailure": 0})

    api_mock.fetch_leverage_tiers.return_value = {}
    assert exchange.get_leverage_tiers() == {}
    api_mock.fetch_leverage_tiers.assert_called_once_with(
        params={
            "papi": True,
            "portfolioMargin": True,
            "maxRetriesOnFailure": 0,
        }
    )

    exchange._set_leverage(1.0, "ETH/USDT:USDT")
    api_mock.set_leverage.assert_called_once_with(
        symbol="ETH/USDT:USDT",
        leverage=1,
        params={
            "papi": True,
            "portfolioMargin": True,
            "maxRetriesOnFailure": 0,
        },
    )
    with pytest.raises(OperationalException, match="restricted to 1x leverage"):
        exchange._set_leverage(1.1, "ETH/USDT:USDT")

    assert exchange._get_funding_fees_from_exchange(
        "ETH/USDT:USDT", datetime(2026, 1, 1)
    ) == pytest.approx(0.1)
    assert api_mock.fetch_funding_history.call_args.kwargs["params"] == {
        "papi": True,
        "portfolioMargin": True,
        "maxRetriesOnFailure": 0,
    }

    with pytest.raises(OperationalException, match="cannot disable PAPI"):
        exchange._portfolio_margin_params({"papi": False})
    with pytest.raises(OperationalException, match="cannot disable PAPI"):
        exchange._portfolio_margin_params({"portfolioMargin": False})
    with pytest.raises(OperationalException, match="non-USD-M markets"):
        exchange._portfolio_margin_params({"subType": "inverse"})
    with pytest.raises(OperationalException, match="cannot disable PAPI"):
        exchange._portfolio_margin_params({"papi": 0})
    with pytest.raises(OperationalException, match="cannot disable PAPI"):
        exchange._portfolio_margin_params({"portfolioMargin": 0})
    with pytest.raises(OperationalException, match="cannot disable PAPI"):
        exchange._portfolio_margin_params({"defaultPapi": False})
    with pytest.raises(OperationalException, match="safe retry controls"):
        exchange._portfolio_margin_params({"defaultUseV2": True})
    with pytest.raises(OperationalException, match="safe retry controls"):
        exchange._portfolio_margin_params({"maxRetriesOnFailure": 1})
    with pytest.raises(OperationalException, match="non-USD-M markets"):
        exchange.fetch_positions(params={"callerMethodName": "unsafe"})
    with pytest.raises(OperationalException, match="positionRisk"):
        exchange.fetch_positions(params={"method": "account"})
    with pytest.raises(OperationalException, match="positionRisk"):
        exchange.fetch_positions(params={"defaultMethod": "account"})
    with pytest.raises(OperationalException, match="non-USD-M markets"):
        exchange._portfolio_margin_params({"type": "delivery"})
    assert exchange._portfolio_margin_params({"subType": "linear"}) == {
        "papi": True,
        "portfolioMargin": True,
        "maxRetriesOnFailure": 0,
    }

    exchange.set_margin_mode("ETH/USDT:USDT", MarginMode.CROSS)
    api_mock.set_margin_mode.assert_not_called()
    with pytest.raises(OperationalException, match="isolated mode is not supported"):
        exchange.set_margin_mode("ETH/USDT:USDT", MarginMode.ISOLATED)
    assert exchange.fetch_trading_fees() == {}
    api_mock.fetch_trading_fees.assert_not_called()


def test_binance_portfolio_margin_unified_collateral_balance_mapping(default_conf, mocker):
    api_mock = MagicMock()
    api_mock.fetch_leverage_tiers.return_value = {}
    api_mock.papiGetUmPositionSideDual.return_value = {"dualSidePosition": False}
    api_mock.papiGetUmAccountConfig.return_value = {"canTrade": True}
    api_mock.fetch_positions.return_value = []
    api_mock.fetch_balance.return_value = {"USDT": {"free": 0.0, "used": 0.0}}
    api_mock.papiGetBalance.return_value = [
        {
            "asset": "USDT",
            "totalWalletBalance": "1099.43174479",
            "crossMarginAsset": "1099.43174479",
            "crossMarginFree": "1099.43174479",
            "crossMarginLocked": "0",
            "crossMarginBorrowed": "0",
            "crossMarginInterest": "0",
            "umWalletBalance": "0.0",
            "cmWalletBalance": "0",
            "negativeBalance": "0",
        }
    ]
    exchange = get_patched_exchange(
        mocker,
        portfolio_margin_conf(default_conf, dry_run=False),
        api_mock,
        exchange="binance",
    )

    balances = exchange.get_balances()
    assert balances["USDT"]["free"] == 1099.43174479
    assert balances["USDT"]["used"] == 0.0
    assert balances["USDT"]["total"] == 1099.43174479

    # A funded UM wallet keeps the CCXT mapping untouched and adds no extra request.
    api_mock.fetch_balance.return_value = {"USDT": {"free": 60.0, "used": 0.0}}
    api_mock.papiGetBalance.reset_mock()
    balances = exchange.get_balances()
    assert balances["USDT"]["free"] == 60.0
    assert "total" not in balances["USDT"]
    api_mock.papiGetBalance.assert_not_called()

    # No fallback when the shared pool is empty as well.
    api_mock.fetch_balance.return_value = {"USDT": {"free": 0.0, "used": 0.0}}
    api_mock.papiGetBalance.return_value = [
        {"asset": "USDT", "crossMarginFree": "0", "umWalletBalance": "0"}
    ]
    balances = exchange.get_balances()
    assert balances["USDT"]["free"] == 0.0

    # A malformed raw record fails closed and keeps the zero mapping.
    api_mock.papiGetBalance.return_value = [
        {"asset": "USDT", "crossMarginFree": "lots", "umWalletBalance": "0"}
    ]
    balances = exchange.get_balances()
    assert balances["USDT"]["free"] == 0.0


def test_binance_portfolio_margin_entry_risk_guard(default_conf, mocker):
    exchange = get_patched_exchange(
        mocker,
        portfolio_margin_risk_conf(default_conf),
        exchange="binance",
    )
    pair = "ETH/USDT:USDT"

    exchange._validate_portfolio_margin_entry_order(
        pair=pair,
        side="buy",
        amount=0.05,
        rate=2000,
        leverage=1,
        reduce_only=False,
    )
    for overrides in (
        {"pair": "XRP/USDT:USDT"},
        {"side": "sell"},
        {"amount": 0.0501},
        {"rate": 0},
        {"leverage": 2},
    ):
        values = {
            "pair": pair,
            "side": "buy",
            "amount": 0.05,
            "rate": 2000,
            "leverage": 1,
            "reduce_only": False,
        }
        values.update(overrides)
        with pytest.raises(OperationalException, match="entry blocked"):
            exchange._validate_portfolio_margin_entry_order(**values)

    # Emergency/normal reduce-only exits must remain available even if entry limits differ.
    exchange._validate_portfolio_margin_entry_order(
        pair="XRP/USDT:USDT",
        side="sell",
        amount=100,
        rate=2000,
        leverage=5,
        reduce_only=True,
    )


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_binance_portfolio_margin_chan_entry_risk_guard(default_conf, mocker, side):
    exchange = get_patched_exchange(
        mocker,
        portfolio_margin_chan_risk_conf(default_conf),
        exchange="binance",
    )
    values = {
        "pair": "ETH/USDT:USDT",
        "side": side,
        "amount": 0.05,
        "rate": 2000,
        "leverage": 1,
        "reduce_only": False,
    }

    exchange._validate_portfolio_margin_entry_order(**values)
    for overrides in (
        {"pair": "XRP/USDT:USDT"},
        {"amount": 0.05001},
        {"rate": 0},
        {"leverage": 2},
    ):
        invalid = {**values, **overrides}
        with pytest.raises(OperationalException, match="Chan entry blocked"):
            exchange._validate_portfolio_margin_entry_order(**invalid)

    exchange._portfolio_margin_risk["pairs"]["XRP/USDT:USDT"] = 100
    with pytest.raises(OperationalException, match="Chan entry blocked"):
        exchange._validate_portfolio_margin_entry_order(**values)
    exchange._portfolio_margin_risk["pairs"].pop("XRP/USDT:USDT")

    exchange._validate_portfolio_margin_entry_order(
        pair="XRP/USDT:USDT",
        side="sell",
        amount=100,
        rate=2000,
        leverage=5,
        reduce_only=True,
    )


def test_binance_portfolio_margin_chan_projected_exposure_guard(default_conf, mocker):
    api_mock = portfolio_margin_live_api_mock()
    api_mock.fetch_positions.return_value = [
        {
            "symbol": pair,
            "contracts": 1,
            "notional": -100 if pair == "ETH/USDT:USDT" else 100,
            "leverage": 1,
            "side": "short" if pair == "ETH/USDT:USDT" else "long",
        }
        for pair in (
            "BTC/USDT:USDT",
            "ETH/USDT:USDT",
            "BNB/USDT:USDT",
            "SOL/USDT:USDT",
        )
    ]
    conf = portfolio_margin_chan_risk_conf(default_conf, dry_run=False)
    exchange = get_patched_exchange(mocker, conf, api_mock, exchange="binance")

    exchange._validate_portfolio_margin_chan_projected_exposure(
        "SPY/USDT:USDT", 100, "ftpm-proposed"
    )
    api_mock.fetch_open_orders.assert_called_once_with(
        params={
            "papi": True,
            "portfolioMargin": True,
            "maxRetriesOnFailure": 0,
        }
    )
    api_mock.fetch_positions.assert_called_once_with(None, params={"maxRetriesOnFailure": 0})

    exchange._portfolio_margin_risk["max_total_entry_notional"] = 450
    with pytest.raises(OperationalException, match="projected pair or total"):
        exchange._validate_portfolio_margin_chan_projected_exposure(
            "SPY/USDT:USDT", 51, "ftpm-proposed"
        )


def test_binance_portfolio_margin_chan_projected_exposure_counts_open_entries(default_conf, mocker):
    api_mock = portfolio_margin_live_api_mock()
    api_mock.fetch_positions.return_value = []
    api_mock.fetch_open_orders.return_value = [
        {
            "id": "entry-1",
            "symbol": "ETH/USDT:USDT",
            "side": "buy",
            "remaining": 1,
            "price": 8,
            "reduceOnly": False,
            "info": {},
        }
    ]
    exchange = get_patched_exchange(
        mocker,
        portfolio_margin_chan_risk_conf(default_conf, dry_run=False),
        api_mock,
        exchange="binance",
    )

    with pytest.raises(OperationalException, match="already has exchange exposure"):
        exchange._validate_portfolio_margin_chan_projected_exposure(
            "ETH/USDT:USDT", 21, "ftpm-proposed"
        )

    api_mock.fetch_open_orders.return_value[0]["reduceOnly"] = True
    exchange._validate_portfolio_margin_chan_projected_exposure(
        "ETH/USDT:USDT", 100, "ftpm-proposed"
    )

    api_mock.fetch_open_orders.return_value[0]["reduceOnly"] = False
    api_mock.fetch_open_orders.return_value[0].pop("side")
    with pytest.raises(OperationalException, match="invalid or missing side"):
        exchange._validate_portfolio_margin_chan_projected_exposure(
            "ETH/USDT:USDT", 20, "ftpm-proposed"
        )

    api_mock.fetch_open_orders.return_value[0]["side"] = "buy"
    api_mock.fetch_open_orders.return_value[0].pop("price")
    with pytest.raises(OperationalException, match="invalid amount or price metadata"):
        exchange._validate_portfolio_margin_chan_projected_exposure(
            "ETH/USDT:USDT", 20, "ftpm-proposed"
        )


def test_binance_portfolio_margin_chan_projected_exposure_rejects_duplicate_client_id(
    default_conf, mocker
):
    api_mock = portfolio_margin_live_api_mock()
    api_mock.fetch_positions.return_value = []
    api_mock.fetch_open_orders.return_value = [
        {
            "id": "existing-reduce-only",
            "clientOrderId": "ftpm-duplicate",
            "symbol": "ETH/USDT:USDT",
            "reduceOnly": True,
            "info": {},
        }
    ]
    exchange = get_patched_exchange(
        mocker,
        portfolio_margin_chan_risk_conf(default_conf, dry_run=False),
        api_mock,
        exchange="binance",
    )

    with pytest.raises(OperationalException, match="already belongs to an open order"):
        exchange._validate_portfolio_margin_chan_projected_exposure(
            "ETH/USDT:USDT", 50, "ftpm-duplicate"
        )


def test_binance_portfolio_margin_chan_serializes_projected_exposure_checks(
    default_conf, mocker, tmp_path
):
    api_mock = portfolio_margin_live_api_mock()
    type(api_mock).has = PropertyMock(return_value={"setLeverage": False})
    mocker.patch(f"{EXMS}.amount_to_precision", lambda s, x, y: y)
    mocker.patch(f"{EXMS}.price_to_precision", lambda s, x, y, **kwargs: y)
    exchange = get_patched_exchange(
        mocker,
        persistent_portfolio_margin_chan_conf(default_conf, tmp_path),
        api_mock,
        exchange="binance",
    )
    first_check_entered = Event()
    release_first_check = Event()
    snapshot_count = 0

    def fetch_open_orders(*args, **kwargs):
        nonlocal snapshot_count
        snapshot_count += 1
        if snapshot_count == 1:
            first_check_entered.set()
            assert release_first_check.wait(timeout=5)
        return []

    api_mock.fetch_open_orders.side_effect = fetch_open_orders
    api_mock.fetch_positions.return_value = []
    api_mock.fetch_order.side_effect = ccxt.OrderNotFound("snapshot still lagging")
    mocker.patch.object(
        exchange,
        "_new_portfolio_client_order_id",
        side_effect=("ftpm-concurrent-1", "ftpm-concurrent-2"),
    )

    def create_response(pair, ordertype, side, amount, rate, params):
        return {
            "id": params["clientOrderId"],
            "clientOrderId": params["clientOrderId"],
            "symbol": pair,
            "type": ordertype,
            "side": side,
            "amount": amount,
            "filled": 0.0,
            "remaining": amount,
            "status": "open",
            "info": {},
        }

    api_mock.create_order.side_effect = create_response

    def submit(side):
        return exchange.create_order(
            pair="ETH/USDT:USDT",
            ordertype="limit",
            side=side,
            amount=0.025,
            rate=2000,
            leverage=1,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_order = executor.submit(submit, "buy")
        assert first_check_entered.wait(timeout=5)
        second_order = executor.submit(submit, "sell")
        assert not second_order.done()
        release_first_check.set()
        assert first_order.result(timeout=5)["id"] == "ftpm-concurrent-1"
        with pytest.raises(OperationalException, match="durable entry reservation"):
            second_order.result(timeout=5)

    assert api_mock.create_order.call_count == 1
    assert api_mock.create_order.call_args.args[-1]["clientOrderId"] == "ftpm-concurrent-1"


def test_binance_portfolio_margin_chan_create_checks_projected_exposure_before_post(
    default_conf, mocker
):
    api_mock = portfolio_margin_live_api_mock()
    api_mock.fetch_positions.return_value = [
        {
            "symbol": "ETH/USDT:USDT",
            "contracts": 0.045,
            "notional": 90,
            "leverage": 1,
            "side": "long",
        }
    ]
    exchange = get_patched_exchange(
        mocker,
        portfolio_margin_chan_risk_conf(default_conf, dry_run=False),
        api_mock,
        exchange="binance",
    )

    with pytest.raises(OperationalException, match="already has exchange exposure"):
        exchange.create_order(
            pair="ETH/USDT:USDT",
            ordertype="limit",
            side="sell",
            amount=0.0055,
            rate=2000,
            leverage=1,
        )
    api_mock.create_order.assert_not_called()

    api_mock.fetch_positions.return_value = []
    api_mock.fetch_open_orders.side_effect = ccxt.RequestTimeout("snapshot unavailable")
    with pytest.raises(OperationalException, match="reconciliation request failed"):
        exchange.create_order(
            pair="ETH/USDT:USDT",
            ordertype="limit",
            side="buy",
            amount=0.025,
            rate=2000,
            leverage=1,
        )
    api_mock.create_order.assert_not_called()


def test_binance_portfolio_margin_chan_projected_exposure_rejects_unknown_position(
    default_conf, mocker
):
    api_mock = portfolio_margin_live_api_mock()
    api_mock.fetch_positions.return_value = [
        {
            "symbol": "XRP/USDT:USDT",
            "contracts": 1,
            "notional": 50,
            "leverage": 1,
            "side": "long",
        }
    ]
    exchange = get_patched_exchange(
        mocker,
        portfolio_margin_chan_risk_conf(default_conf, dry_run=False),
        api_mock,
        exchange="binance",
    )

    with pytest.raises(OperationalException, match="unreviewed or missing pair"):
        exchange._validate_portfolio_margin_chan_projected_exposure(
            "BTC/USDT:USDT", 50, "ftpm-proposed"
        )

    api_mock.fetch_positions.return_value[0]["symbol"] = "BTC/USDT:USDT"
    api_mock.fetch_positions.return_value[0].pop("side")
    with pytest.raises(OperationalException, match="invalid or missing side"):
        exchange._validate_portfolio_margin_chan_projected_exposure(
            "BTC/USDT:USDT", 50, "ftpm-proposed"
        )


@pytest.mark.parametrize("contracts", ["missing", None, "", float("nan"), 0])
def test_binance_portfolio_margin_chan_position_amount_inconsistency_fails_closed(
    default_conf, mocker, contracts
):
    position = {
        "symbol": "ETH/USDT:USDT",
        "contracts": contracts,
        "notional": 50,
        "info": {"positionAmt": "0.025"},
        "side": "long",
    }
    if contracts == "missing":
        position.pop("contracts")
    api_mock = portfolio_margin_live_api_mock()
    api_mock.fetch_positions.return_value = [position]
    exchange = get_patched_exchange(
        mocker,
        portfolio_margin_chan_risk_conf(default_conf, dry_run=False),
        api_mock,
        exchange="binance",
    )

    with pytest.raises(
        OperationalException,
        match=(
            r"missing position amount|non-finite position amount|"
            "non-finite Portfolio Margin position amount|non-zero exposure"
        ),
    ):
        exchange._validate_portfolio_margin_chan_projected_exposure(
            "BTC/USDT:USDT", 50, "ftpm-proposed"
        )


def test_binance_portfolio_margin_chan_market_entry_and_force_entry_fail_closed(
    default_conf, mocker
):
    api_mock = portfolio_margin_live_api_mock()
    type(api_mock).has = PropertyMock(return_value={"setLeverage": False})
    exchange = get_patched_exchange(
        mocker,
        portfolio_margin_chan_risk_conf(default_conf, dry_run=False),
        api_mock,
        exchange="binance",
    )

    with pytest.raises(OperationalException, match="limit-order-only"):
        exchange.create_order(
            pair="ETH/USDT:USDT",
            ordertype="market",
            side="buy",
            amount=0.05,
            rate=2000,
            leverage=1,
        )
    api_mock.create_order.assert_not_called()

    force_conf = portfolio_margin_chan_risk_conf(default_conf, dry_run=False)
    force_conf["force_entry_enable"] = True
    with pytest.raises(OperationalException, match="disable force-entry"):
        get_patched_exchange(
            mocker, force_conf, portfolio_margin_live_api_mock(), exchange="binance"
        )


def test_binance_portfolio_margin_preserves_open_position(default_conf, mocker):
    api_mock = MagicMock()
    api_mock.fetch_leverage_tiers.return_value = {}
    api_mock.papiGetUmPositionSideDual.return_value = {"dualSidePosition": False}
    api_mock.papiGetUmAccountConfig.return_value = {"canTrade": True}
    api_mock.fetch_positions.return_value = [
        {
            "symbol": "ETH/USDT:USDT",
            "contracts": 2.0,
            "notional": 4000.0,
            "leverage": 2.0,
            "initialMargin": 2000.0,
            "collateral": 0.0,
            "side": "long",
            "marginMode": None,
        }
    ]
    exchange = get_patched_exchange(
        mocker,
        portfolio_margin_conf(default_conf, dry_run=False),
        api_mock,
        exchange="binance",
    )

    positions = exchange.fetch_positions()

    assert positions[0]["collateral"] == 2000.0
    assert positions[0]["marginMode"] == "cross"
    assert positions[0]["contracts"] == 2.0
    api_mock.fetch_positions.assert_called_once_with(None, params={"maxRetriesOnFailure": 0})


def test_binance_portfolio_margin_ccxt_raw_routes(default_conf, mocker, markets):
    """Exercise CCXT 4.5.67 and record the raw endpoint requests without network access."""
    assert ccxt.__version__ == "4.5.67"
    conf = portfolio_margin_conf(default_conf)
    exchange = get_patched_exchange(mocker, conf, exchange="binance")
    route_params = exchange._portfolio_margin_params({"subType": "linear"})

    market = deepcopy(markets["ETH/USDT:USDT"])
    market["id"] = "ETHUSDT"
    market["info"] = {
        "orderTypes": [
            "LIMIT",
            "MARKET",
            "STOP",
            "STOP_MARKET",
            "TAKE_PROFIT",
            "TAKE_PROFIT_MARKET",
        ]
    }
    raw_ccxt_config = deep_merge_dicts(
        conf["exchange"]["ccxt_config"], deepcopy(exchange._ccxt_config)
    )
    api = ccxt.binance(raw_ccxt_config)
    assert api.options["fetchCurrencies"] is False
    assert api.options["fetchMarkets"]["types"] == ["linear"]
    assert api.options["fetchOpenOrders"]["warnWithoutSymbol"] is False
    assert api.options["fetchPositions"]["method"] == "positionRisk"
    assert api.options["defaultSubType"] == "linear"
    assert api.options["maxRetriesOnFailure"] == 0
    assert api.options["useV2"] is False
    api.set_markets([market])
    recorded: list[tuple[str, dict]] = []

    regular_order = {
        "symbol": "ETHUSDT",
        "orderId": 42,
        "clientOrderId": "ftpm-order",
        "price": "2000",
        "origQty": "1",
        "executedQty": "0",
        "status": "NEW",
        "timeInForce": "GTC",
        "type": "LIMIT",
        "side": "BUY",
        "updateTime": 1,
    }
    endpoint_responses = {
        "papiGetBalance": [],
        "papiPostUmOrder": regular_order,
        "papiGetUmOrder": regular_order,
        "papiDeleteUmOrder": regular_order,
        "papiGetUmOpenOrders": [regular_order],
        "papiGetUmAllOrders": [regular_order],
        "papiGetUmUserTrades": [],
        "papiPostUmLeverage": {
            "symbol": "ETHUSDT",
            "leverage": 1,
            "maxNotionalValue": "100000",
        },
        "papiGetUmIncome": [],
        "papiGetUmLeverageBracket": [],
        "papiGetUmPositionRisk": [],
    }

    def record_endpoint(name, response):
        def endpoint(params=None):
            recorded.append((name, params or {}))
            return response

        return endpoint

    for endpoint_name, response in endpoint_responses.items():
        setattr(api, endpoint_name, record_endpoint(endpoint_name, response))

    fapi_calls: list[str] = []

    def reject_fapi(*args, **kwargs):
        fapi_calls.append("fapi")
        raise AssertionError("Portfolio Margin route attempted to call FAPI")

    for attribute in dir(api):
        if attribute.startswith("fapi") and callable(getattr(api, attribute)):
            setattr(api, attribute, reject_fapi)

    pair = "ETH/USDT:USDT"
    api.fetch_balance(dict(route_params))
    api.create_order(pair, "limit", "buy", 1, 2000, dict(route_params))
    api.fetch_order("42", pair, dict(route_params))
    api.fetch_order(
        "ftpm-order",
        pair,
        {**route_params, "origClientOrderId": "ftpm-order"},
    )
    api.cancel_order("42", pair, dict(route_params))
    api.fetch_open_orders(params=dict(route_params))
    api.fetch_orders(pair, params=dict(route_params))
    api.fetch_my_trades(pair, params=dict(route_params))
    api.set_leverage(1, pair, dict(route_params))
    api.fetch_funding_history(pair, params=dict(route_params))
    api.fetch_leverage_tiers(params=dict(route_params))
    api.fetch_positions([pair], {})

    assert fapi_calls == []
    expected_endpoints = list(endpoint_responses)
    expected_endpoints.insert(expected_endpoints.index("papiDeleteUmOrder"), "papiGetUmOrder")
    expected_endpoints.insert(
        expected_endpoints.index("papiGetUmPositionRisk"), "papiGetUmLeverageBracket"
    )
    assert [name for name, _ in recorded] == expected_endpoints
    routing_keys = {"papi", "portfolioMargin", "subType"}
    assert all(routing_keys.isdisjoint(request) for _, request in recorded)


def test_binance_portfolio_margin_ccxt_algo_raw_routes(default_conf, mocker, markets):
    """Use CCXT 4.5.67 signing with a fake transport for the full Algo lifecycle."""
    assert ccxt.__version__ == "4.5.67"
    pair = "ETH/USDT:USDT"
    market = deepcopy(markets[pair])
    market["id"] = "ETHUSDT"
    market["contractSize"] = 10
    market["precision"] = {"amount": 0.1, "price": 0.05}
    market["info"] = {
        "orderTypes": [
            "LIMIT",
            "MARKET",
            "STOP",
            "STOP_MARKET",
            "TAKE_PROFIT",
            "TAKE_PROFIT_MARKET",
        ]
    }

    api = ccxt.binance(
        {
            "apiKey": "test-api-key",
            "secret": "test-api-secret",
            "enableRateLimit": False,
            "options": {
                "defaultType": "swap",
                "portfolioMargin": True,
            },
        }
    )
    api.set_markets([market])
    raw_order = {
        "algoId": 73,
        "clientAlgoId": "ftpm-raw-stop",
        "algoType": "CONDITIONAL",
        "orderType": "STOP_MARKET",
        "symbol": "ETHUSDT",
        "side": "SELL",
        "positionSide": "BOTH",
        "timeInForce": "GTC",
        "quantity": "0.1",
        "algoStatus": "NEW",
        "triggerPrice": "1900",
        "price": "0",
        "workingType": "MARK_PRICE",
        "priceProtect": False,
        "reduceOnly": True,
        "createTime": 1,
        "updateTime": 1,
        "actualOrderId": "",
    }
    recorded: list[dict] = []

    def fake_fetch(url, method="GET", headers=None, body=None):
        recorded.append({"url": url, "method": method, "body": body})
        path = urlparse(url).path
        if path == "/papi/v1/um/algo/order" and method == "POST":
            return raw_order
        if path == "/papi/v1/um/algo/order" and method == "DELETE":
            return {"complete": True}
        if path == "/papi/v1/um/algo/algoOrder":
            return raw_order
        if path in (
            "/papi/v1/um/algo/openAlgoOrders",
            "/papi/v1/um/algo/allAlgoOrders",
        ):
            return [raw_order]
        raise AssertionError(f"Unexpected fake transport request: {method} {path}")

    mocker.patch.object(api, "fetch", side_effect=fake_fetch)
    exchange = get_patched_exchange(
        mocker,
        portfolio_margin_conf(default_conf, dry_run=False),
        portfolio_margin_live_api_mock(),
        exchange="binance",
    )
    exchange._api = api
    exchange._markets = {pair: market}
    mocker.patch.object(exchange, "_lev_prep")
    mocker.patch.object(
        exchange,
        "_new_portfolio_client_order_id",
        return_value="ftpm-raw-stop",
    )

    created = exchange.create_stoploss(
        pair=pair,
        amount=1,
        stop_price=1900,
        order_types={
            "stoploss": "market",
            "stoploss_price_type": PriceType.MARK,
        },
        side="sell",
        leverage=1,
    )
    assert created["id"] == "73"
    assert created["amount"] == 1.0
    fetched = exchange.fetch_stoploss_order("73", pair)
    assert fetched["id"] == "73"
    assert fetched["amount"] == 1.0
    assert exchange._fetch_portfolio_algo_open_orders()[0]["id"] == "73"
    assert exchange._fetch_portfolio_algo_order_history(pair, order_id="73")[0]["id"] == "73"
    recovered = exchange._recover_portfolio_order(
        pair,
        "ftpm-raw-stop",
        conditional=True,
    )
    assert recovered is not None
    assert recovered["amount"] == 1.0
    assert exchange.cancel_stoploss_order("73", pair)["status"] == "canceled"

    assert [(item["method"], urlparse(item["url"]).path) for item in recorded] == [
        ("POST", "/papi/v1/um/algo/order"),
        ("GET", "/papi/v1/um/algo/algoOrder"),
        ("GET", "/papi/v1/um/algo/openAlgoOrders"),
        ("GET", "/papi/v1/um/algo/allAlgoOrders"),
        ("GET", "/papi/v1/um/algo/allAlgoOrders"),
        ("DELETE", "/papi/v1/um/algo/order"),
    ]
    assert all("/fapi/" not in item["url"] for item in recorded)
    assert all("/um/conditional/" not in item["url"] for item in recorded)

    post = recorded[0]
    post_body = post["body"].decode() if isinstance(post["body"], bytes) else post["body"]
    post_params = parse_qs(post_body or urlparse(post["url"]).query)
    assert {
        "algoType": ["CONDITIONAL"],
        "symbol": ["ETHUSDT"],
        "side": ["SELL"],
        "type": ["STOP_MARKET"],
        "quantity": ["0.1"],
        "triggerPrice": ["1900"],
        "clientAlgoId": ["ftpm-raw-stop"],
        "reduceOnly": ["true"],
        "workingType": ["MARK_PRICE"],
        "newOrderRespType": ["RESULT"],
    }.items() <= post_params.items()
    assert {
        "strategyType",
        "stopPrice",
        "newClientStrategyId",
        "papi",
        "portfolioMargin",
        "maxRetriesOnFailure",
    }.isdisjoint(post_params)


def test_binance_portfolio_margin_ccxt_disables_transport_retry():
    assert ccxt.__version__ == "4.5.67"
    api = ccxt.binance(
        {
            "enableRateLimit": False,
            "options": {"maxRetriesOnFailure": 5},
        }
    )
    api.sign = MagicMock(
        return_value={
            "url": "https://papi.binance.com/papi/v1/um/order",
            "method": "POST",
            "headers": {},
            "body": "",
        }
    )
    api.fetch = MagicMock(side_effect=ccxt.RequestTimeout("unknown order result"))

    with pytest.raises(ccxt.RequestTimeout):
        api.fetch2(
            "um/order",
            "papi",
            "POST",
            {
                "symbol": "ETHUSDT",
                "maxRetriesOnFailure": 0,
            },
        )

    assert api.fetch.call_count == 1
    signed_params = api.sign.call_args.args[3]
    assert signed_params == {"symbol": "ETHUSDT"}


def test_binance_portfolio_margin_ccxt_market_loading_avoids_signed_sapi(default_conf, mocker):
    """Authenticated market loading must use only public linear-market metadata."""
    assert ccxt.__version__ == "4.5.67"
    conf = portfolio_margin_conf(default_conf)
    exchange = get_patched_exchange(mocker, conf, exchange="binance")
    raw_ccxt_config = deep_merge_dicts(
        conf["exchange"]["ccxt_config"], deepcopy(exchange._ccxt_config)
    )
    raw_ccxt_config.update({"apiKey": "not-a-real-key", "secret": "not-a-real-secret"})
    api = ccxt.binance(raw_ccxt_config)
    recorded: list[tuple[str, str, str]] = []

    def record_request(path, api_name="public", method="GET", *args, **kwargs):
        normalized_api = "/".join(api_name) if isinstance(api_name, list) else str(api_name)
        recorded.append((normalized_api, method, path))
        if "private" in normalized_api.lower() or normalized_api.lower().startswith("sapi"):
            raise AssertionError(f"Signed non-PAPI market-loading request: {normalized_api}")
        return {"symbols": []}

    api.request = record_request

    assert api.load_markets() == {}
    assert recorded
    assert all(api_name == "fapiPublic" for api_name, _, _ in recorded)


def test_binance_portfolio_margin_order_lifecycle_routes(default_conf, mocker):
    api_mock = MagicMock()
    api_mock.fetch_leverage_tiers.return_value = {}
    api_mock.papiGetUmPositionSideDual.return_value = {"dualSidePosition": False}
    api_mock.papiGetUmAccountConfig.return_value = {"canTrade": True}
    order = {
        "id": "42",
        "symbol": "ETH/USDT:USDT",
        "amount": 1.0,
        "filled": 0.0,
        "remaining": 1.0,
        "status": "open",
        "info": {},
    }
    api_mock.fetch_order.return_value = order
    api_mock.cancel_order.return_value = order
    api_mock.fetch_orders.return_value = [order]
    api_mock.fetch_my_trades.return_value = []
    type(api_mock).has = PropertyMock(
        return_value={
            "fetchOrder": True,
            "fetchOrders": True,
            "fetchMyTrades": True,
        }
    )
    exchange = get_patched_exchange(
        mocker,
        portfolio_margin_conf(default_conf, dry_run=False),
        api_mock,
        exchange="binance",
    )
    api_mock.reset_mock()

    exchange.fetch_order("42", "ETH/USDT:USDT")
    assert api_mock.fetch_order.call_args.kwargs["params"] == {
        "papi": True,
        "portfolioMargin": True,
        "maxRetriesOnFailure": 0,
    }
    exchange.cancel_order("42", "ETH/USDT:USDT")
    assert api_mock.cancel_order.call_args.kwargs["params"] == {
        "papi": True,
        "portfolioMargin": True,
        "maxRetriesOnFailure": 0,
    }
    exchange._fetch_orders("ETH/USDT:USDT", datetime(2026, 1, 1))
    assert api_mock.fetch_orders.call_args.kwargs["params"] == {
        "papi": True,
        "portfolioMargin": True,
        "maxRetriesOnFailure": 0,
    }
    exchange.get_trades_for_order("42", "ETH/USDT:USDT", datetime(2026, 1, 1))
    assert api_mock.fetch_my_trades.call_args.kwargs["params"] == {
        "papi": True,
        "portfolioMargin": True,
        "maxRetriesOnFailure": 0,
    }


def test_binance_portfolio_margin_stoploss_query(default_conf, mocker):
    api_mock = configure_portfolio_algo_api_mock(MagicMock())
    api_mock.fetch_leverage_tiers.return_value = {}
    order = {
        "id": "73",
        "symbol": "ETH/USDT:USDT",
        "amount": 1.0,
        "filled": 0.0,
        "remaining": 1.0,
        "status": "open",
        "info": {},
    }
    api_mock.request.return_value = order
    exchange = get_patched_exchange(
        mocker,
        portfolio_margin_conf(default_conf),
        api_mock,
        exchange="binance",
    )

    assert exchange.fetch_stoploss_order("73", "ETH/USDT:USDT")["id"] == "73"
    api_mock.request.assert_called_once_with(
        "um/algo/algoOrder",
        "papi",
        "GET",
        {
            "algoId": "73",
            "maxRetriesOnFailure": 0,
        },
        config={"cost": 1},
    )
    api_mock.fetch_open_order.assert_not_called()
    api_mock.fetch_orders.assert_not_called()

    api_mock.request.reset_mock()
    api_mock.request.side_effect = [ccxt.OrderNotFound("not open"), [order]]
    assert exchange.fetch_stoploss_order("73", "ETH/USDT:USDT")["id"] == "73"
    assert [call.args[:3] for call in api_mock.request.call_args_list] == [
        ("um/algo/algoOrder", "papi", "GET"),
        ("um/algo/allAlgoOrders", "papi", "GET"),
    ]
    assert api_mock.request.call_args_list[1].args[3] == {
        "symbol": "ETHUSDT",
        "algoId": "73",
        "maxRetriesOnFailure": 0,
    }

    api_mock.request.side_effect = ccxt.InvalidOrder("invalid")
    with pytest.raises(InvalidOrderException, match="invalid Portfolio Margin"):
        exchange.fetch_stoploss_order("73", "ETH/USDT:USDT")

    api_mock.request.side_effect = ccxt.RequestTimeout("timeout")
    with pytest.raises(TemporaryError, match="Could not get Portfolio Margin"):
        exchange.fetch_stoploss_order("73", "ETH/USDT:USDT")

    api_mock.request.side_effect = ccxt.AuthenticationError("bad key")
    with pytest.raises(TemporaryError, match="bad key"):
        exchange.fetch_stoploss_order("73", "ETH/USDT:USDT")

    api_mock.request.side_effect = ccxt.BaseError("unexpected")
    with pytest.raises(OperationalException, match="unexpected"):
        exchange.fetch_stoploss_order("73", "ETH/USDT:USDT")


def test_binance_portfolio_margin_stoploss_query_triggered(default_conf, mocker):
    api_mock = portfolio_margin_live_api_mock()
    type(api_mock).has = PropertyMock(return_value={"fetchOrder": True})
    stop_order = portfolio_algo_order("73", "ftpm-stop", status="closed")
    stop_order["stopPrice"] = 1900.0
    stop_order["info"]["actualOrderId"] = "9001"
    api_mock.request.return_value = stop_order
    api_mock.fetch_order.return_value = {
        "id": "9001",
        "symbol": "ETH/USDT:USDT",
        "amount": 1.0,
        "filled": 1.0,
        "remaining": 0.0,
        "status": "closed",
        "info": {},
    }
    exchange = get_patched_exchange(
        mocker,
        portfolio_margin_conf(default_conf, dry_run=False),
        api_mock,
        exchange="binance",
    )

    order = exchange.fetch_stoploss_order("73", "ETH/USDT:USDT")

    assert order["id"] == "73"
    assert order["id_stop"] == "9001"
    assert order["status_stop"] == "triggered"
    assert order["stopPrice"] == 1900.0
    api_mock.fetch_order.assert_called_once_with(
        "9001",
        "ETH/USDT:USDT",
        params={
            "papi": True,
            "portfolioMargin": True,
            "maxRetriesOnFailure": 0,
        },
    )
    api_mock.fetch_open_order.assert_not_called()
    api_mock.fetch_orders.assert_not_called()


def test_binance_portfolio_margin_recovers_unknown_create(default_conf, mocker):
    mocker.patch("freqtrade.exchange.binance.sleep")
    api_mock = MagicMock()
    api_mock.fetch_leverage_tiers.return_value = {}
    api_mock.papiGetUmPositionSideDual.return_value = {"dualSidePosition": False}
    api_mock.papiGetUmAccountConfig.return_value = {"canTrade": True}
    api_mock.create_order.side_effect = ccxt.RequestTimeout("execution status unknown")
    api_mock.fetch_order.return_value = {
        "id": "91",
        "clientOrderId": "ftpm-fixed",
        "symbol": "ETH/USDT:USDT",
        "amount": 1.0,
        "filled": 0.0,
        "remaining": 1.0,
        "status": "open",
        "info": {},
    }
    type(api_mock).has = PropertyMock(return_value={"setLeverage": False})
    mocker.patch(f"{EXMS}.amount_to_precision", lambda s, x, y: y)
    mocker.patch(f"{EXMS}.price_to_precision", lambda s, x, y, **kwargs: y)
    exchange = get_patched_exchange(
        mocker,
        portfolio_margin_conf(default_conf, dry_run=False),
        api_mock,
        exchange="binance",
    )
    mocker.patch.object(exchange, "_new_portfolio_client_order_id", return_value="ftpm-fixed")

    recovered = exchange.create_order(
        pair="ETH/USDT:USDT",
        ordertype="limit",
        side="buy",
        amount=1,
        rate=2000,
        leverage=1,
    )

    assert recovered["id"] == "91"
    assert api_mock.create_order.call_count == 1
    assert api_mock.create_order.call_args.args[-1]["clientOrderId"] == "ftpm-fixed"
    api_mock.fetch_order.assert_called_once_with(
        "ftpm-fixed",
        "ETH/USDT:USDT",
        params={
            "origClientOrderId": "ftpm-fixed",
            "papi": True,
            "portfolioMargin": True,
            "maxRetriesOnFailure": 0,
        },
    )

    api_mock.create_order.reset_mock(side_effect=True)
    api_mock.create_order.side_effect = ccxt.DDoSProtection("rate limited")
    api_mock.fetch_order.reset_mock(side_effect=True)
    recovered = exchange.create_order(
        pair="ETH/USDT:USDT",
        ordertype="limit",
        side="buy",
        amount=1,
        rate=2000,
        leverage=1,
    )
    assert recovered["id"] == "91"
    assert api_mock.create_order.call_count == 1
    assert api_mock.fetch_order.call_count == 1

    api_mock.create_order.reset_mock(side_effect=True)
    api_mock.create_order.side_effect = ccxt.RequestTimeout("execution status unknown")
    api_mock.fetch_order.side_effect = ccxt.OrderNotFound("not visible")
    with pytest.raises(OperationalException, match="automatic retry is disabled"):
        exchange.create_order(
            pair="ETH/USDT:USDT",
            ordertype="limit",
            side="buy",
            amount=1,
            rate=2000,
            leverage=1,
        )
    assert api_mock.create_order.call_count == 1
    assert exchange._portfolio_unknown_order_latched is True
    with pytest.raises(OperationalException, match="latched unknown order"):
        exchange.create_order(
            pair="ETH/USDT:USDT",
            ordertype="limit",
            side="buy",
            amount=1,
            rate=2000,
            leverage=1,
        )

    exchange._portfolio_unknown_order_latched = False
    api_mock.create_order.reset_mock(side_effect=True)
    api_mock.create_order.side_effect = ccxt.DDoSProtection("rate limited")
    api_mock.fetch_order.side_effect = ccxt.RequestTimeout("recovery timeout")
    with pytest.raises(OperationalException, match="reconciliation query failed"):
        exchange.create_order(
            pair="ETH/USDT:USDT",
            ordertype="limit",
            side="buy",
            amount=1,
            rate=2000,
            leverage=1,
        )
    assert api_mock.create_order.call_count == 1


def test_binance_portfolio_margin_recovers_malformed_create_response(default_conf, mocker):
    mocker.patch("freqtrade.exchange.binance.sleep")
    api_mock = MagicMock()
    api_mock.fetch_leverage_tiers.return_value = {}
    api_mock.papiGetUmPositionSideDual.return_value = {"dualSidePosition": False}
    api_mock.papiGetUmAccountConfig.return_value = {"canTrade": True}
    api_mock.create_order.return_value = {
        "id": "0",
        "clientOrderId": "ftpm-malformed",
        "symbol": "ETH/USDT:USDT",
        "type": "limit",
        "amount": 1.0,
        "filled": 0.0,
        "remaining": 1.0,
        "status": "open",
        "info": {},
    }
    api_mock.fetch_order.return_value = {
        "id": "93",
        "clientOrderId": "ftpm-malformed",
        "symbol": "ETH/USDT:USDT",
        "type": "limit",
        "amount": 1.0,
        "filled": 0.0,
        "remaining": 1.0,
        "status": "open",
        "info": {},
    }
    type(api_mock).has = PropertyMock(return_value={"setLeverage": False})
    mocker.patch(f"{EXMS}.amount_to_precision", lambda s, x, y: y)
    mocker.patch(f"{EXMS}.price_to_precision", lambda s, x, y, **kwargs: y)
    exchange = get_patched_exchange(
        mocker,
        portfolio_margin_conf(default_conf, dry_run=False),
        api_mock,
        exchange="binance",
    )
    mocker.patch.object(exchange, "_new_portfolio_client_order_id", return_value="ftpm-malformed")

    recovered = exchange.create_order(
        pair="ETH/USDT:USDT",
        ordertype="limit",
        side="buy",
        amount=1,
        rate=2000,
        leverage=1,
    )

    assert recovered["id"] == "93"
    assert api_mock.create_order.call_count == 1
    assert api_mock.fetch_order.call_count == 1
    assert api_mock.create_order.call_args.args[-1]["maxRetriesOnFailure"] == 0


def test_binance_portfolio_margin_latch_survives_recovered_reduce_only_order(default_conf, mocker):
    mocker.patch("freqtrade.exchange.binance.sleep")
    api_mock = MagicMock()
    api_mock.fetch_leverage_tiers.return_value = {}
    api_mock.papiGetUmPositionSideDual.return_value = {"dualSidePosition": False}
    api_mock.papiGetUmAccountConfig.return_value = {"canTrade": True}
    api_mock.create_order.side_effect = ccxt.RequestTimeout("unknown reduce-only order")
    api_mock.fetch_order.return_value = {
        "id": "94",
        "clientOrderId": "ftpm-reduce",
        "symbol": "ETH/USDT:USDT",
        "type": "market",
        "amount": 1.0,
        "filled": 1.0,
        "remaining": 0.0,
        "status": "closed",
        "info": {},
    }
    type(api_mock).has = PropertyMock(return_value={"setLeverage": False})
    mocker.patch(f"{EXMS}.amount_to_precision", lambda s, x, y: y)
    mocker.patch(f"{EXMS}.price_to_precision", lambda s, x, y, **kwargs: y)
    exchange = get_patched_exchange(
        mocker,
        portfolio_margin_conf(default_conf, dry_run=False),
        api_mock,
        exchange="binance",
    )
    exchange._portfolio_unknown_order_latched = True
    mocker.patch.object(exchange, "_new_portfolio_client_order_id", return_value="ftpm-reduce")

    recovered = exchange.create_order(
        pair="ETH/USDT:USDT",
        ordertype="market",
        side="sell",
        amount=1,
        rate=2000,
        leverage=1,
        reduceOnly=True,
    )

    assert recovered["id"] == "94"
    assert exchange._portfolio_unknown_order_latched is True


def test_binance_portfolio_margin_rechecks_latch_after_order_lock(default_conf, mocker):
    api_mock = MagicMock()
    api_mock.fetch_leverage_tiers.return_value = {}
    api_mock.papiGetUmPositionSideDual.return_value = {"dualSidePosition": False}
    api_mock.papiGetUmAccountConfig.return_value = {"canTrade": True}
    type(api_mock).has = PropertyMock(return_value={"setLeverage": False})
    exchange = get_patched_exchange(
        mocker,
        portfolio_margin_conf(default_conf, dry_run=False),
        api_mock,
        exchange="binance",
    )

    class LatchingLock:
        def __enter__(self):
            exchange._portfolio_unknown_order_latched = True

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    exchange._portfolio_create_lock = LatchingLock()

    with pytest.raises(OperationalException, match="latched unknown order"):
        exchange.create_order(
            pair="ETH/USDT:USDT",
            ordertype="limit",
            side="buy",
            amount=1,
            rate=2000,
            leverage=1,
        )
    api_mock.create_order.assert_not_called()


def test_binance_portfolio_margin_stoploss_rechecks_latch_after_order_lock(default_conf, mocker):
    api_mock = MagicMock()
    api_mock.fetch_leverage_tiers.return_value = {}
    api_mock.papiGetUmPositionSideDual.return_value = {"dualSidePosition": False}
    api_mock.papiGetUmAccountConfig.return_value = {"canTrade": True}
    type(api_mock).has = PropertyMock(return_value={"setLeverage": False})
    exchange = get_patched_exchange(
        mocker,
        portfolio_margin_conf(default_conf, dry_run=False),
        api_mock,
        exchange="binance",
    )

    class LatchingLock:
        def __enter__(self):
            exchange._portfolio_unknown_order_latched = True

        def __exit__(self, exc_type, exc_value, traceback):
            return False

    exchange._portfolio_create_lock = LatchingLock()

    with pytest.raises(InvalidOrderException, match="latched unknown order"):
        exchange.create_stoploss(
            pair="ETH/USDT:USDT",
            amount=1,
            stop_price=1900,
            order_types={"stoploss": "market"},
            side="sell",
            leverage=1,
        )
    api_mock.create_order.assert_not_called()


def test_binance_portfolio_margin_recovers_unknown_stoploss(default_conf, mocker):
    mocker.patch("freqtrade.exchange.binance.sleep")
    api_mock = configure_portfolio_algo_api_mock(MagicMock())
    api_mock.fetch_leverage_tiers.return_value = {}
    api_mock.papiGetUmPositionSideDual.return_value = {"dualSidePosition": False}
    api_mock.papiGetUmAccountConfig.return_value = {"canTrade": True}
    api_mock.create_order_request.return_value = portfolio_algo_create_request("ftpm-stop")
    recovered_order = portfolio_algo_order("92", "ftpm-stop")
    api_mock.request.side_effect = [
        ccxt.RequestTimeout("execution status unknown"),
        [recovered_order],
    ]
    type(api_mock).has = PropertyMock(return_value={"setLeverage": False})
    mocker.patch(f"{EXMS}.amount_to_precision", lambda s, x, y: y)
    mocker.patch(f"{EXMS}.price_to_precision", lambda s, x, y, **kwargs: y)
    exchange = get_patched_exchange(
        mocker,
        portfolio_margin_conf(default_conf, dry_run=False),
        api_mock,
        exchange="binance",
    )
    mocker.patch.object(exchange, "_new_portfolio_client_order_id", return_value="ftpm-stop")

    recovered = exchange.create_stoploss(
        pair="ETH/USDT:USDT",
        amount=1,
        stop_price=1900,
        order_types={"stoploss": "market"},
        side="sell",
        leverage=1,
    )

    assert recovered["id"] == "92"
    assert [call.args[2] for call in api_mock.request.call_args_list] == ["POST", "GET"]
    post_calls = [call for call in api_mock.request.call_args_list if call.args[2] == "POST"]
    assert len(post_calls) == 1
    assert post_calls[0].args[:3] == ("um/algo/order", "papi", "POST")
    assert post_calls[0].args[3]["clientAlgoId"] == "ftpm-stop"
    assert post_calls[0].args[3]["algoType"] == "CONDITIONAL"
    assert {"strategyType", "stopPrice", "newClientStrategyId"}.isdisjoint(post_calls[0].args[3])
    api_mock.create_order.assert_not_called()
    api_mock.fetch_orders.assert_not_called()

    api_mock.request.reset_mock(side_effect=True)
    api_mock.request.side_effect = [
        ccxt.DDoSProtection("rate limited"),
        [recovered_order],
    ]
    recovered = exchange.create_stoploss(
        pair="ETH/USDT:USDT",
        amount=1,
        stop_price=1900,
        order_types={"stoploss": "market"},
        side="sell",
        leverage=1,
    )
    assert recovered["id"] == "92"

    api_mock.request.reset_mock(side_effect=True)
    api_mock.request.side_effect = [
        ccxt.RequestTimeout("execution status unknown"),
        [],
    ]
    with pytest.raises(InvalidOrderException, match="emergency exit"):
        exchange.create_stoploss(
            pair="ETH/USDT:USDT",
            amount=1,
            stop_price=1900,
            order_types={"stoploss": "market"},
            side="sell",
            leverage=1,
        )
    assert [call.args[2] for call in api_mock.request.call_args_list] == [
        "POST",
        "GET",
    ]
    assert len([call for call in api_mock.request.call_args_list if call.args[2] == "POST"]) == 1
    assert exchange.portfolio_margin_unknown_order_latched is True
    assert exchange.portfolio_margin_enabled is True

    request_count = api_mock.request.call_count
    with pytest.raises(InvalidOrderException, match="latched unknown order"):
        exchange.create_stoploss(
            pair="ETH/USDT:USDT",
            amount=1,
            stop_price=1900,
            order_types={"stoploss": "market"},
            side="sell",
            leverage=1,
        )
    assert api_mock.request.call_count == request_count

    exchange._portfolio_unknown_order_latched = False
    api_mock.request.reset_mock(side_effect=True)
    api_mock.request.side_effect = [
        ccxt.RequestTimeout("execution status unknown"),
        ccxt.RequestTimeout("recovery timeout"),
    ]
    with pytest.raises(InvalidOrderException, match="recovery query failed"):
        exchange.create_stoploss(
            pair="ETH/USDT:USDT",
            amount=1,
            stop_price=1900,
            order_types={"stoploss": "market"},
            side="sell",
            leverage=1,
        )
    assert [call.args[2] for call in api_mock.request.call_args_list] == ["POST", "GET"]


def test_binance_portfolio_margin_recovers_malformed_stoploss_response(default_conf, mocker):
    api_mock = configure_portfolio_algo_api_mock(MagicMock())
    api_mock.fetch_leverage_tiers.return_value = {}
    api_mock.papiGetUmPositionSideDual.return_value = {"dualSidePosition": False}
    api_mock.papiGetUmAccountConfig.return_value = {"canTrade": True}
    api_mock.create_order_request.return_value = portfolio_algo_create_request(
        "ftpm-stop-malformed"
    )
    malformed_order = {
        "id": 0,
        "clientOrderId": "ftpm-stop-malformed",
        "symbol": "ETH/USDT:USDT",
        "type": "stop_market",
        "amount": 1.0,
        "filled": 0.0,
        "remaining": 1.0,
        "status": "open",
        "info": {},
    }
    recovered_order = portfolio_algo_order("95", "ftpm-stop-malformed")
    api_mock.request.side_effect = [
        malformed_order,
        [recovered_order],
    ]
    type(api_mock).has = PropertyMock(return_value={"setLeverage": False})
    mocker.patch(f"{EXMS}.amount_to_precision", lambda s, x, y: y)
    mocker.patch(f"{EXMS}.price_to_precision", lambda s, x, y, **kwargs: y)
    exchange = get_patched_exchange(
        mocker,
        portfolio_margin_conf(default_conf, dry_run=False),
        api_mock,
        exchange="binance",
    )
    mocker.patch.object(
        exchange,
        "_new_portfolio_client_order_id",
        return_value="ftpm-stop-malformed",
    )

    recovered = exchange.create_stoploss(
        pair="ETH/USDT:USDT",
        amount=1,
        stop_price=1900,
        order_types={"stoploss": "market"},
        side="sell",
        leverage=1,
    )

    assert recovered["id"] == "95"
    assert [call.args[2] for call in api_mock.request.call_args_list] == ["POST", "GET"]
    assert api_mock.request.call_args_list[0].args[3]["maxRetriesOnFailure"] == 0
    api_mock.create_order.assert_not_called()
    api_mock.fetch_orders.assert_not_called()


def test_binance_portfolio_margin_unknown_entry_flattens_detected_exposure(default_conf, mocker):
    mocker.patch("freqtrade.exchange.binance.sleep")
    api_mock = MagicMock()
    api_mock.fetch_leverage_tiers.return_value = {}
    api_mock.papiGetUmPositionSideDual.return_value = {"dualSidePosition": False}
    api_mock.papiGetUmAccountConfig.return_value = {"canTrade": True}
    api_mock.fetch_order.side_effect = ccxt.OrderNotFound("not visible")
    api_mock.fetch_open_orders.return_value = []
    api_mock.fetch_positions.side_effect = [
        [],
        [
            {
                "symbol": "ETH/USDT:USDT",
                "contracts": 0.025,
                "side": "long",
                "leverage": 1,
                "marginMode": "cross",
                "collateral": 50,
            }
        ],
        [],
        [],
        [],
        [],
        [],
        [],
        [],
        [],
        [],
        [],
        [],
        [],
        [],
    ]
    api_mock.create_order.side_effect = [
        ccxt.RequestTimeout("entry status unknown"),
        {
            "id": "emergency-close",
            "clientOrderId": "ftpm-close",
            "symbol": "ETH/USDT:USDT",
            "status": "closed",
            "info": {},
        },
    ]
    type(api_mock).has = PropertyMock(return_value={"setLeverage": False})
    mocker.patch(f"{EXMS}.amount_to_precision", lambda s, x, y: y)
    mocker.patch(f"{EXMS}.price_to_precision", lambda s, x, y, **kwargs: y)
    exchange = get_patched_exchange(
        mocker,
        portfolio_margin_risk_conf(default_conf, dry_run=False),
        api_mock,
        exchange="binance",
    )
    exchange._portfolio_order_recovery_attempts = 1
    client_ids = iter(("ftpm-entry", "ftpm-close"))
    mocker.patch.object(
        exchange,
        "_new_portfolio_client_order_id",
        side_effect=lambda: next(client_ids),
    )

    with pytest.raises(OperationalException, match="flattened detected exposure"):
        exchange.create_order(
            pair="ETH/USDT:USDT",
            ordertype="market",
            side="buy",
            amount=0.025,
            rate=2000,
            leverage=1,
        )

    assert api_mock.create_order.call_count == 2
    assert api_mock.fetch_open_orders.call_count == 10
    assert api_mock.fetch_positions.call_count == 10
    emergency_call = api_mock.create_order.call_args_list[1]
    assert emergency_call.args[:3] == ("ETH/USDT:USDT", "market", "sell")
    assert emergency_call.args[-1] == {
        "reduceOnly": True,
        "clientOrderId": "ftpm-close",
        "papi": True,
        "portfolioMargin": True,
        "maxRetriesOnFailure": 0,
    }
    assert exchange._portfolio_unknown_order_latched is True


def test_binance_portfolio_margin_unknown_entry_cancel_fill_race_is_flattened(default_conf, mocker):
    mocker.patch("freqtrade.exchange.binance.sleep")
    api_mock = MagicMock()
    api_mock.fetch_leverage_tiers.return_value = {}
    api_mock.papiGetUmPositionSideDual.return_value = {"dualSidePosition": False}
    api_mock.papiGetUmAccountConfig.return_value = {"canTrade": True}
    api_mock.fetch_order.side_effect = ccxt.OrderNotFound("not visible")
    api_mock.fetch_open_orders.side_effect = [
        [
            {
                "id": "late-entry",
                "clientOrderId": "ftpm-entry",
                "symbol": "ETH/USDT:USDT",
                "status": "open",
                "info": {},
            }
        ],
        [],
        [],
        [],
        [],
        [],
        [],
        [],
        [],
        [],
    ]
    api_mock.cancel_order.side_effect = ccxt.OrderNotFound("filled before cancel")
    api_mock.fetch_positions.side_effect = [
        [],
        [
            {
                "symbol": "ETH/USDT:USDT",
                "contracts": 0.025,
                "side": "long",
                "leverage": 1,
                "marginMode": "cross",
                "collateral": 50,
            }
        ],
        [],
        [],
        [],
        [],
        [],
        [],
        [],
        [],
    ]
    api_mock.create_order.side_effect = [
        ccxt.RequestTimeout("entry status unknown"),
        {
            "id": "emergency-close",
            "clientOrderId": "ftpm-close",
            "symbol": "ETH/USDT:USDT",
            "status": "closed",
            "info": {},
        },
    ]
    type(api_mock).has = PropertyMock(return_value={"setLeverage": False})
    mocker.patch(f"{EXMS}.amount_to_precision", lambda s, x, y: y)
    mocker.patch(f"{EXMS}.price_to_precision", lambda s, x, y, **kwargs: y)
    exchange = get_patched_exchange(
        mocker,
        portfolio_margin_risk_conf(default_conf, dry_run=False),
        api_mock,
        exchange="binance",
    )
    exchange._portfolio_order_recovery_attempts = 1
    client_ids = iter(("ftpm-entry", "ftpm-close"))
    mocker.patch.object(
        exchange,
        "_new_portfolio_client_order_id",
        side_effect=lambda: next(client_ids),
    )

    with pytest.raises(OperationalException, match="flattened detected exposure"):
        exchange.create_order(
            pair="ETH/USDT:USDT",
            ordertype="market",
            side="buy",
            amount=0.025,
            rate=2000,
            leverage=1,
        )

    api_mock.cancel_order.assert_called_once_with(
        "late-entry",
        "ETH/USDT:USDT",
        params={
            "papi": True,
            "portfolioMargin": True,
            "maxRetriesOnFailure": 0,
        },
    )
    assert api_mock.fetch_open_orders.call_count == 10
    assert api_mock.fetch_positions.call_count == 10
    assert api_mock.create_order.call_count == 2
    assert api_mock.create_order.call_args_list[1].args[:3] == (
        "ETH/USDT:USDT",
        "market",
        "sell",
    )
    assert exchange.portfolio_margin_unknown_order_latched is True


def test_binance_portfolio_margin_unknown_entry_reflattens_fill_after_first_close(
    default_conf, mocker
):
    late_order = {
        "id": "late-entry",
        "clientOrderId": "ftpm-entry",
        "symbol": "ETH/USDT:USDT",
        "status": "open",
        "info": {},
    }
    exchange, api_mock = unknown_entry_containment_exchange(
        default_conf,
        mocker,
        [
            [portfolio_margin_position()],
            [],
            [portfolio_margin_position()],
            [],
            [],
            [],
        ],
        open_order_snapshots=[[], [late_order], [], [], [], []],
    )

    with pytest.raises(OperationalException, match="flattened detected exposure"):
        exchange.create_order(
            pair="ETH/USDT:USDT",
            ordertype="market",
            side="buy",
            amount=0.025,
            rate=2000,
            leverage=1,
        )

    close_calls = api_mock.create_order.call_args_list[1:]
    assert len(close_calls) == 2
    assert all(call.args[:3] == ("ETH/USDT:USDT", "market", "sell") for call in close_calls)
    assert all(call.args[-1]["reduceOnly"] is True for call in close_calls)
    api_mock.cancel_order.assert_called_once_with(
        "late-entry",
        "ETH/USDT:USDT",
        params={
            "papi": True,
            "portfolioMargin": True,
            "maxRetriesOnFailure": 0,
        },
    )
    assert api_mock.fetch_open_orders.call_count == 10
    assert api_mock.fetch_positions.call_count == 10
    assert exchange.portfolio_margin_unknown_order_latched is True


def test_binance_portfolio_margin_unknown_entry_reflattens_repeated_late_fills(
    default_conf, mocker
):
    exchange, api_mock = unknown_entry_containment_exchange(
        default_conf,
        mocker,
        [
            [portfolio_margin_position()],
            [],
            [portfolio_margin_position()],
            [],
            [portfolio_margin_position()],
            [],
            [],
            [],
        ],
    )

    with pytest.raises(OperationalException, match="flattened detected exposure"):
        exchange.create_order(
            pair="ETH/USDT:USDT",
            ordertype="market",
            side="buy",
            amount=0.025,
            rate=2000,
            leverage=1,
        )

    close_calls = api_mock.create_order.call_args_list[1:]
    assert len(close_calls) == 3
    assert all(call.args[:3] == ("ETH/USDT:USDT", "market", "sell") for call in close_calls)
    assert all(call.args[-1]["reduceOnly"] is True for call in close_calls)
    assert api_mock.fetch_open_orders.call_count == 10
    assert api_mock.fetch_positions.call_count == 10
    assert exchange.portfolio_margin_unknown_order_latched is True


def test_binance_portfolio_margin_unknown_entry_observes_full_window_after_three_clean(
    default_conf, mocker
):
    late_order = {
        "id": "fourth-snapshot-entry",
        "clientOrderId": "ftpm-entry",
        "symbol": "ETH/USDT:USDT",
        "status": "open",
        "info": {},
    }
    exchange, api_mock = unknown_entry_containment_exchange(
        default_conf,
        mocker,
        [
            [],
            [],
            [],
            [],
            [portfolio_margin_position()],
            [],
            [],
            [],
            [],
            [],
        ],
        open_order_snapshots=[
            [],
            [],
            [],
            [late_order],
            [],
            [],
            [],
            [],
            [],
            [],
        ],
    )

    with pytest.raises(OperationalException, match="flattened detected exposure"):
        exchange.create_order(
            pair="ETH/USDT:USDT",
            ordertype="market",
            side="buy",
            amount=0.025,
            rate=2000,
            leverage=1,
        )

    assert api_mock.fetch_open_orders.call_count == 10
    assert api_mock.fetch_positions.call_count == 10
    api_mock.cancel_order.assert_called_once()
    close_calls = api_mock.create_order.call_args_list[1:]
    assert len(close_calls) == 1
    assert close_calls[0].args[-1]["reduceOnly"] is True


def test_binance_portfolio_margin_unknown_entry_never_stable_fails_closed(default_conf, mocker):
    exchange, api_mock = unknown_entry_containment_exchange(
        default_conf,
        mocker,
        [[portfolio_margin_position()], []] * 5,
    )

    with pytest.raises(OperationalException, match="consecutive clean PAPI snapshots"):
        exchange.create_order(
            pair="ETH/USDT:USDT",
            ordertype="market",
            side="buy",
            amount=0.025,
            rate=2000,
            leverage=1,
        )

    assert api_mock.fetch_open_orders.call_count == exchange._portfolio_containment_attempts
    assert api_mock.fetch_positions.call_count == exchange._portfolio_containment_attempts
    assert len(api_mock.create_order.call_args_list[1:]) == 5
    assert exchange.portfolio_margin_unknown_order_latched is True
    previous_create_count = api_mock.create_order.call_count
    with pytest.raises(OperationalException, match="latched unknown order"):
        exchange.create_order(
            pair="ETH/USDT:USDT",
            ordertype="market",
            side="buy",
            amount=0.025,
            rate=2000,
            leverage=1,
        )
    assert api_mock.create_order.call_count == previous_create_count


def test_binance_portfolio_margin_unknown_entry_does_not_flatten_other_pair(default_conf, mocker):
    target_position = portfolio_margin_position()
    other_position = portfolio_margin_position("BTC/USDT:USDT", contracts=0.001)
    unrelated_order = {
        "id": "btc-order",
        "clientOrderId": "another-strategy",
        "symbol": "BTC/USDT:USDT",
        "status": "open",
        "info": {},
    }
    exchange, api_mock = unknown_entry_containment_exchange(
        default_conf,
        mocker,
        [
            [target_position, other_position],
            [other_position],
            [other_position],
            [other_position],
        ],
        open_orders=[unrelated_order],
    )

    with pytest.raises(OperationalException, match="flattened detected exposure"):
        exchange.create_order(
            pair="ETH/USDT:USDT",
            ordertype="market",
            side="buy",
            amount=0.025,
            rate=2000,
            leverage=1,
        )

    close_calls = api_mock.create_order.call_args_list[1:]
    assert len(close_calls) == 1
    assert close_calls[0].args[:4] == ("ETH/USDT:USDT", "market", "sell", 0.025)
    assert close_calls[0].args[-1]["reduceOnly"] is True
    api_mock.cancel_order.assert_not_called()
    assert exchange.portfolio_margin_unknown_order_latched is True


@pytest.mark.parametrize(
    "ccxt_error",
    [
        ccxt.InsufficientFunds("insufficient"),
        ccxt.InvalidOrder("invalid stop"),
        ccxt.BaseError("unexpected"),
    ],
)
def test_binance_portfolio_margin_stoploss_failure_forces_emergency_exit(
    default_conf, mocker, ccxt_error
):
    api_mock = configure_portfolio_algo_api_mock(MagicMock())
    api_mock.fetch_leverage_tiers.return_value = {}
    api_mock.papiGetUmPositionSideDual.return_value = {"dualSidePosition": False}
    api_mock.papiGetUmAccountConfig.return_value = {"canTrade": True}
    api_mock.create_order_request.return_value = portfolio_algo_create_request("ftpm-stop")
    api_mock.request.side_effect = ccxt_error
    type(api_mock).has = PropertyMock(return_value={"setLeverage": False})
    mocker.patch(f"{EXMS}.amount_to_precision", lambda s, x, y: y)
    mocker.patch(f"{EXMS}.price_to_precision", lambda s, x, y, **kwargs: y)
    exchange = get_patched_exchange(
        mocker,
        portfolio_margin_conf(default_conf, dry_run=False),
        api_mock,
        exchange="binance",
    )

    with pytest.raises(InvalidOrderException, match="emergency exit") as exc_info:
        exchange.create_stoploss(
            pair="ETH/USDT:USDT",
            amount=1,
            stop_price=1900,
            order_types={"stoploss": "market"},
            side="sell",
            leverage=1,
        )
    assert type(exc_info.value) is InvalidOrderException
    assert api_mock.request.call_count == 1
    assert api_mock.request.call_args.args[:3] == ("um/algo/order", "papi", "POST")
    api_mock.create_order.assert_not_called()
    api_mock.fetch_orders.assert_not_called()


def test_binance_portfolio_margin_delayed_unknown_stop_is_cancelled_after_emergency_exit(
    default_conf, mocker
):
    sleep_mock = mocker.patch("freqtrade.exchange.binance.sleep")
    api_mock = configure_portfolio_algo_api_mock(MagicMock())
    api_mock.fetch_leverage_tiers.return_value = {}
    api_mock.papiGetUmPositionSideDual.return_value = {"dualSidePosition": False}
    api_mock.papiGetUmAccountConfig.return_value = {"canTrade": True}
    api_mock.create_order_request.return_value = portfolio_algo_create_request("ftpm-delayed-stop")
    delayed_order = portfolio_algo_order("delayed-stop-92", "ftpm-delayed-stop")
    api_mock.request.side_effect = [
        ccxt.RequestTimeout("execution status unknown"),
        [],
        [],
        [delayed_order],
        {"complete": True},
        [],
        [],
        [],
    ]
    type(api_mock).has = PropertyMock(return_value={"setLeverage": False})
    mocker.patch(f"{EXMS}.amount_to_precision", lambda s, x, y: y)
    mocker.patch(f"{EXMS}.price_to_precision", lambda s, x, y, **kwargs: y)
    exchange = get_patched_exchange(
        mocker,
        portfolio_margin_conf(default_conf, dry_run=False),
        api_mock,
        exchange="binance",
    )
    mocker.patch.object(
        exchange,
        "_new_portfolio_client_order_id",
        return_value="ftpm-delayed-stop",
    )

    with pytest.raises(InvalidOrderException, match="emergency exit"):
        exchange.create_stoploss(
            pair="ETH/USDT:USDT",
            amount=1,
            stop_price=1900,
            order_types={"stoploss": "market"},
            side="sell",
            leverage=1,
        )

    # Creation performs only the one configured fast recovery query. The
    # post-emergency cleanup owns all delayed visibility polling.
    assert [call.args[:3] for call in api_mock.request.call_args_list] == [
        ("um/algo/order", "papi", "POST"),
        ("um/algo/allAlgoOrders", "papi", "GET"),
    ]
    sleep_mock.assert_not_called()
    assert exchange.cleanup_portfolio_margin_unknown_conditional_order("ETH/USDT:USDT")

    open_params = {
        "algoType": "CONDITIONAL",
        "symbol": "ETHUSDT",
        "maxRetriesOnFailure": 0,
    }
    cleanup_calls = api_mock.request.call_args_list[2:]
    assert [call.args[:3] for call in cleanup_calls] == [
        ("um/algo/openAlgoOrders", "papi", "GET"),
        ("um/algo/openAlgoOrders", "papi", "GET"),
        ("um/algo/order", "papi", "DELETE"),
        ("um/algo/openAlgoOrders", "papi", "GET"),
        ("um/algo/openAlgoOrders", "papi", "GET"),
        ("um/algo/openAlgoOrders", "papi", "GET"),
    ]
    assert [
        call.args[3]
        for call in cleanup_calls
        if call.args[:3] == ("um/algo/openAlgoOrders", "papi", "GET")
    ] == [open_params] * 5
    delete_call = next(call for call in cleanup_calls if call.args[2] == "DELETE")
    assert delete_call.args[3] == {
        "algoId": "delayed-stop-92",
        "maxRetriesOnFailure": 0,
    }
    api_mock.create_order.assert_not_called()
    api_mock.fetch_orders.assert_not_called()
    api_mock.fetch_open_orders.assert_not_called()
    api_mock.cancel_order.assert_not_called()
    assert exchange.portfolio_margin_unknown_order_latched is True
    assert exchange._portfolio_unknown_conditional_client_order_id is None
    assert exchange._portfolio_unknown_conditional_pair is None


def test_binance_portfolio_margin_unknown_stop_cleanup_fails_closed_if_still_open(
    default_conf, mocker
):
    mocker.patch("freqtrade.exchange.binance.sleep")
    api_mock = portfolio_margin_live_api_mock()
    delayed_order = portfolio_algo_order("delayed-stop-93", "ftpm-persistent-stop")

    def persistent_algo_order(path, api, method, params, **kwargs):
        return {"complete": True} if method == "DELETE" else [delayed_order]

    api_mock.request.side_effect = persistent_algo_order
    exchange = get_patched_exchange(
        mocker,
        portfolio_margin_conf(default_conf, dry_run=False),
        api_mock,
        exchange="binance",
    )
    exchange._portfolio_unknown_order_latched = True
    exchange._portfolio_unknown_conditional_client_order_id = "ftpm-persistent-stop"
    exchange._portfolio_unknown_conditional_pair = "ETH/USDT:USDT"

    with pytest.raises(OperationalException, match="could not confirm"):
        exchange.cleanup_portfolio_margin_unknown_conditional_order("ETH/USDT:USDT")

    get_calls = [call for call in api_mock.request.call_args_list if call.args[2] == "GET"]
    delete_calls = [call for call in api_mock.request.call_args_list if call.args[2] == "DELETE"]
    assert len(get_calls) == exchange._portfolio_conditional_cleanup_attempts
    assert len(delete_calls) == exchange._portfolio_conditional_cleanup_attempts
    assert all(call.args[0] == "um/algo/openAlgoOrders" for call in get_calls)
    assert all(call.args[0] == "um/algo/order" for call in delete_calls)
    api_mock.fetch_open_orders.assert_not_called()
    api_mock.cancel_order.assert_not_called()
    api_mock.create_order.assert_not_called()
    assert exchange._portfolio_unknown_conditional_client_order_id == "ftpm-persistent-stop"


def test_binance_portfolio_margin_persists_regular_intent_before_post(
    default_conf, mocker, tmp_path
):
    api_mock = portfolio_margin_live_api_mock()
    type(api_mock).has = PropertyMock(return_value={"setLeverage": False})
    mocker.patch(f"{EXMS}.amount_to_precision", lambda s, x, y: y)
    mocker.patch(f"{EXMS}.price_to_precision", lambda s, x, y, **kwargs: y)
    exchange = get_patched_exchange(
        mocker,
        persistent_portfolio_margin_conf(default_conf, tmp_path),
        api_mock,
        exchange="binance",
    )
    mocker.patch.object(exchange, "_new_portfolio_client_order_id", return_value="ftpm-regular")
    state_path = exchange._portfolio_order_intent_store.path
    post_snapshots = []

    def create_order(*args, **kwargs):
        post_snapshots.append(json.loads(state_path.read_text(encoding="utf-8")))
        return {
            "id": "regular-1",
            "clientOrderId": "ftpm-regular",
            "symbol": "ETH/USDT:USDT",
            "status": "open",
            "amount": 1.0,
            "filled": 0.0,
            "remaining": 1.0,
            "info": {},
        }

    api_mock.create_order.side_effect = create_order

    exchange.create_order(
        pair="ETH/USDT:USDT",
        ordertype="limit",
        side="buy",
        amount=0.025,
        rate=2000,
        leverage=1,
    )

    assert post_snapshots == [
        {
            "version": 1,
            "intents": [
                {
                    "client_order_id": "ftpm-regular",
                    "pair": "ETH/USDT:USDT",
                    "order_kind": "regular",
                    "purpose": "submission",
                    "parent_client_order_id": None,
                }
            ],
        }
    ]
    assert json.loads(state_path.read_text(encoding="utf-8")) == {
        "version": 1,
        "intents": [],
    }
    if os.name != "nt":
        assert state_path.stat().st_mode & 0o777 == 0o600
        assert state_path.with_name(f"{state_path.name}.lock").stat().st_mode & 0o777 == 0o600


def test_binance_portfolio_margin_persists_conditional_intent_before_post(
    default_conf, mocker, tmp_path
):
    api_mock = portfolio_margin_live_api_mock()
    api_mock.create_order_request.return_value = portfolio_algo_create_request("ftpm-stop")
    type(api_mock).has = PropertyMock(return_value={"setLeverage": False})
    mocker.patch(f"{EXMS}.amount_to_precision", lambda s, x, y: y)
    mocker.patch(f"{EXMS}.price_to_precision", lambda s, x, y, **kwargs: y)
    exchange = get_patched_exchange(
        mocker,
        persistent_portfolio_margin_conf(default_conf, tmp_path),
        api_mock,
        exchange="binance",
    )
    mocker.patch.object(exchange, "_new_portfolio_client_order_id", return_value="ftpm-stop")
    state_path = exchange._portfolio_order_intent_store.path
    post_snapshots = []

    def request(path, api, method, params, **kwargs):
        assert method == "POST"
        post_snapshots.append(json.loads(state_path.read_text(encoding="utf-8")))
        return portfolio_algo_order("stop-1", "ftpm-stop")

    api_mock.request.side_effect = request

    exchange.create_stoploss(
        pair="ETH/USDT:USDT",
        amount=1,
        stop_price=1900,
        order_types={"stoploss": "market"},
        side="sell",
        leverage=1,
    )

    intent = post_snapshots[0]["intents"][0]
    assert intent == {
        "client_order_id": "ftpm-stop",
        "pair": "ETH/USDT:USDT",
        "order_kind": "conditional",
        "purpose": "submission",
        "parent_client_order_id": None,
    }
    assert set(intent) == {
        "client_order_id",
        "pair",
        "order_kind",
        "purpose",
        "parent_client_order_id",
    }
    assert json.loads(state_path.read_text(encoding="utf-8"))["intents"] == []


@pytest.mark.parametrize("conditional", [False, True])
def test_binance_portfolio_margin_blocks_post_when_intent_persistence_fails(
    default_conf, mocker, tmp_path, conditional
):
    api_mock = portfolio_margin_live_api_mock()
    type(api_mock).has = PropertyMock(return_value={"setLeverage": False})
    exchange = get_patched_exchange(
        mocker,
        persistent_portfolio_margin_conf(default_conf, tmp_path),
        api_mock,
        exchange="binance",
    )
    mocker.patch.object(
        exchange._portfolio_order_intent_store,
        "_atomic_write",
        side_effect=OperationalException("simulated persistence kill point"),
    )

    with pytest.raises(OperationalException, match="simulated persistence kill point"):
        if conditional:
            exchange.create_stoploss(
                pair="ETH/USDT:USDT",
                amount=1,
                stop_price=1900,
                order_types={"stoploss": "market"},
                side="sell",
                leverage=1,
            )
        else:
            exchange.create_order(
                pair="ETH/USDT:USDT",
                ordertype="market",
                side="buy",
                amount=0.025,
                rate=2000,
                leverage=1,
            )

    api_mock.create_order.assert_not_called()
    api_mock.request.assert_not_called()
    assert exchange.portfolio_margin_unknown_order_latched is True


@pytest.mark.parametrize("conditional", [False, True])
def test_binance_portfolio_margin_restart_contains_post_cleanup_kill_point(
    default_conf, mocker, tmp_path, conditional
):
    mocker.patch("freqtrade.exchange.binance.sleep")
    conf = persistent_portfolio_margin_conf(default_conf, tmp_path)
    api_mock = portfolio_margin_live_api_mock()
    type(api_mock).has = PropertyMock(return_value={"setLeverage": False})
    mocker.patch(f"{EXMS}.amount_to_precision", lambda s, x, y: y)
    mocker.patch(f"{EXMS}.price_to_precision", lambda s, x, y, **kwargs: y)
    exchange = get_patched_exchange(mocker, conf, api_mock, exchange="binance")
    client_order_id = "ftpm-crashed-stop" if conditional else "ftpm-crashed-regular"
    mocker.patch.object(exchange, "_new_portfolio_client_order_id", return_value=client_order_id)
    if conditional:
        api_mock.create_order_request.return_value = portfolio_algo_create_request(client_order_id)
        api_mock.request.return_value = portfolio_algo_order("stop-2", client_order_id)
    else:
        api_mock.create_order.return_value = {
            "id": "regular-2",
            "clientOrderId": client_order_id,
            "symbol": "ETH/USDT:USDT",
            "status": "open",
            "amount": 0.025,
            "filled": 0.0,
            "remaining": 0.025,
            "info": {},
        }
    mocker.patch.object(
        exchange._portfolio_order_intent_store,
        "remove",
        side_effect=OperationalException("simulated crash before intent cleanup"),
    )

    with pytest.raises(OperationalException, match="simulated crash before intent cleanup"):
        if conditional:
            exchange.create_stoploss(
                pair="ETH/USDT:USDT",
                amount=1,
                stop_price=1900,
                order_types={"stoploss": "market"},
                side="sell",
                leverage=1,
            )
        else:
            exchange.create_order(
                pair="ETH/USDT:USDT",
                ordertype="market",
                side="buy",
                amount=0.025,
                rate=2000,
                leverage=1,
            )

    recovery_api = portfolio_margin_live_api_mock()
    recovery_api.fetch_positions.return_value = []
    type(recovery_api).has = PropertyMock(return_value={"setLeverage": False})
    restarted = get_patched_exchange(mocker, conf, recovery_api, exchange="binance")
    assert restarted.portfolio_margin_unknown_order_latched is True

    state_path = restarted._portfolio_order_intent_store.path
    if conditional:
        with pytest.raises(OperationalException, match="Restart once more"):
            restarted.validate_existing_positions({}, [])
        assert json.loads(state_path.read_text(encoding="utf-8"))["intents"] == []
        assert all(
            call.args[:3] == ("um/algo/openAlgoOrders", "papi", "GET")
            and call.args[3]["symbol"] == "ETHUSDT"
            for call in recovery_api.request.call_args_list
        )
        recovery_api.create_order.assert_not_called()
    else:
        with pytest.raises(OperationalException, match="could not clear every recovered"):
            restarted.validate_existing_positions({}, [])
        persisted = json.loads(state_path.read_text(encoding="utf-8"))["intents"]
        assert [item["client_order_id"] for item in persisted] == [client_order_id]
        assert all(
            call.args[0] == "ETH/USDT:USDT"
            for call in recovery_api.fetch_open_orders.call_args_list
        )


def test_binance_portfolio_margin_conditional_cleanup_kill_point_is_recoverable(
    default_conf, mocker, tmp_path
):
    mocker.patch("freqtrade.exchange.binance.sleep")
    conf = persistent_portfolio_margin_conf(default_conf, tmp_path)
    api_mock = portfolio_margin_live_api_mock()
    type(api_mock).has = PropertyMock(return_value={"setLeverage": False})
    exchange = get_patched_exchange(mocker, conf, api_mock, exchange="binance")
    exchange._record_portfolio_order_intent(
        pair="ETH/USDT:USDT",
        client_order_id="ftpm-cleanup-kill",
        order_kind="conditional",
    )
    exchange._portfolio_unknown_order_latched = True
    exchange._portfolio_unknown_conditional_client_order_id = "ftpm-cleanup-kill"
    exchange._portfolio_unknown_conditional_pair = "ETH/USDT:USDT"
    mocker.patch.object(
        exchange._portfolio_order_intent_store,
        "_atomic_write",
        side_effect=OperationalException("simulated cleanup fsync kill point"),
    )

    with pytest.raises(OperationalException, match="simulated cleanup fsync kill point"):
        exchange.cleanup_portfolio_margin_unknown_conditional_order("ETH/USDT:USDT")

    restarted = get_patched_exchange(
        mocker, conf, portfolio_margin_live_api_mock(), exchange="binance"
    )
    assert restarted.portfolio_margin_unknown_order_latched is True
    assert restarted._portfolio_unknown_conditional_client_order_id == "ftpm-cleanup-kill"
    assert restarted._portfolio_unknown_conditional_pair == "ETH/USDT:USDT"


def test_binance_portfolio_margin_rejects_persisted_intent_for_other_pair(
    default_conf, mocker, tmp_path
):
    conf = persistent_portfolio_margin_conf(default_conf, tmp_path)
    api_mock = portfolio_margin_live_api_mock()
    exchange = get_patched_exchange(mocker, conf, api_mock, exchange="binance")
    state_path = exchange._portfolio_order_intent_store.path
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "intents": [
                    {
                        "client_order_id": "ftpm-other-pair",
                        "pair": "BTC/USDT:USDT",
                        "order_kind": "regular",
                        "purpose": "submission",
                        "parent_client_order_id": None,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    other_api = portfolio_margin_live_api_mock()

    with pytest.raises(OperationalException, match="could not safely load"):
        get_patched_exchange(mocker, conf, other_api, exchange="binance")

    other_api.fetch_open_orders.assert_not_called()
    other_api.cancel_order.assert_not_called()
    other_api.create_order.assert_not_called()


def test_binance_portfolio_margin_corrupt_intent_file_fails_closed(default_conf, mocker, tmp_path):
    conf = persistent_portfolio_margin_conf(default_conf, tmp_path)
    exchange = get_patched_exchange(
        mocker, conf, portfolio_margin_live_api_mock(), exchange="binance"
    )
    state_path = exchange._portfolio_order_intent_store.path
    state_path.write_text('{"version":1,"intents":', encoding="utf-8")
    other_api = portfolio_margin_live_api_mock()

    with pytest.raises(OperationalException, match="could not safely load"):
        get_patched_exchange(mocker, conf, other_api, exchange="binance")

    other_api.fetch_open_orders.assert_not_called()
    other_api.cancel_order.assert_not_called()
    other_api.create_order.assert_not_called()


@pytest.mark.parametrize(
    ("created_at", "exposure_seen"),
    [
        ("2000-01-01T00:00:00+00:00", False),
        (None, "yes"),
    ],
    ids=["expired", "invalid-exposure-evidence"],
)
def test_binance_portfolio_margin_chan_expired_or_corrupt_reservation_fails_closed(
    default_conf, mocker, tmp_path, created_at, exposure_seen
):
    conf = persistent_portfolio_margin_chan_conf(default_conf, tmp_path)
    exchange = get_patched_exchange(
        mocker, conf, portfolio_margin_live_api_mock(), exchange="binance"
    )
    state_path = exchange._portfolio_order_intent_store.path
    state_path.write_text(
        json.dumps(
            {
                "version": 2,
                "intents": [],
                "reservations": [
                    {
                        "client_order_id": "ftpm-invalid-reservation",
                        "pair": "ETH/USDT:USDT",
                        "created_at": created_at or datetime.now(UTC).isoformat(),
                        "exposure_seen": exposure_seen,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    other_api = portfolio_margin_live_api_mock()

    with pytest.raises(OperationalException, match="could not safely load"):
        get_patched_exchange(mocker, conf, other_api, exchange="binance")

    other_api.fetch_open_orders.assert_not_called()
    other_api.fetch_positions.assert_not_called()
    other_api.create_order.assert_not_called()


def test_binance_portfolio_margin_atomic_replace_failure_preserves_prior_intent(
    default_conf, mocker, tmp_path
):
    conf = persistent_portfolio_margin_conf(default_conf, tmp_path)
    exchange = get_patched_exchange(
        mocker, conf, portfolio_margin_live_api_mock(), exchange="binance"
    )
    exchange._record_portfolio_order_intent(
        pair="ETH/USDT:USDT",
        client_order_id="ftpm-atomic-parent",
        order_kind="regular",
    )
    state_path = exchange._portfolio_order_intent_store.path
    original_state = state_path.read_bytes()
    mocker.patch(
        "freqtrade.exchange.binance_order_intent.Path.replace",
        side_effect=OSError("simulated replace kill point"),
    )

    with pytest.raises(OperationalException, match="atomically persist"):
        exchange._record_portfolio_order_intent(
            pair="ETH/USDT:USDT",
            client_order_id="ftpm-atomic-child",
            order_kind="regular",
            purpose="containment",
            parent_client_order_id="ftpm-atomic-parent",
        )

    assert state_path.read_bytes() == original_state
    assert list(state_path.parent.glob(f".{state_path.name}.*.tmp")) == []


def test_binance_portfolio_margin_stale_process_cannot_overwrite_pending_intent(
    default_conf, mocker, tmp_path
):
    conf = persistent_portfolio_margin_conf(default_conf, tmp_path)
    first_api = portfolio_margin_live_api_mock()
    second_api = portfolio_margin_live_api_mock()
    type(second_api).has = PropertyMock(return_value={"setLeverage": False})
    first = get_patched_exchange(mocker, conf, first_api, exchange="binance")
    second = get_patched_exchange(mocker, conf, second_api, exchange="binance")
    first._record_portfolio_order_intent(
        pair="ETH/USDT:USDT",
        client_order_id="ftpm-first-process",
        order_kind="regular",
    )
    mocker.patch.object(second, "_new_portfolio_client_order_id", return_value="ftpm-stale-process")

    with pytest.raises(OperationalException, match="persisted order evidence"):
        second.create_order(
            pair="ETH/USDT:USDT",
            ordertype="market",
            side="buy",
            amount=0.025,
            rate=2000,
            leverage=1,
        )

    second_api.create_order.assert_not_called()
    intents = json.loads(first._portfolio_order_intent_store.path.read_text(encoding="utf-8"))[
        "intents"
    ]
    assert [intent["client_order_id"] for intent in intents] == ["ftpm-first-process"]


def test_binance_portfolio_margin_process_lock_fails_closed(default_conf, mocker, tmp_path):
    conf = persistent_portfolio_margin_conf(default_conf, tmp_path)
    first = get_patched_exchange(mocker, conf, portfolio_margin_live_api_mock(), exchange="binance")
    second = get_patched_exchange(
        mocker, conf, portfolio_margin_live_api_mock(), exchange="binance"
    )

    with first._get_portfolio_create_lock():
        with pytest.raises(OperationalException, match="Another process is updating"):
            with second._get_portfolio_create_lock():
                pass


def test_binance_portfolio_margin_chan_reservations_cover_sequential_snapshot_lag(
    default_conf, mocker, tmp_path
):
    api_mock = portfolio_margin_live_api_mock()
    api_mock.fetch_positions.return_value = []
    api_mock.fetch_open_orders.return_value = []
    api_mock.fetch_order.side_effect = ccxt.OrderNotFound("exchange snapshot still lagging")
    type(api_mock).has = PropertyMock(return_value={"setLeverage": False})
    mocker.patch(f"{EXMS}.amount_to_precision", lambda s, x, y: y)
    mocker.patch(f"{EXMS}.price_to_precision", lambda s, x, y, **kwargs: y)

    def create_response(pair, ordertype, side, amount, rate, params):
        return {
            "id": params["clientOrderId"],
            "clientOrderId": params["clientOrderId"],
            "symbol": pair,
            "type": ordertype,
            "side": side,
            "status": "open",
            "info": {},
        }

    api_mock.create_order.side_effect = create_response
    exchange = get_patched_exchange(
        mocker,
        persistent_portfolio_margin_chan_conf(default_conf, tmp_path),
        api_mock,
        exchange="binance",
    )

    for pair in CHAN_PAIR_LIMITS:
        exchange.create_order(
            pair=pair,
            ordertype="limit",
            side="buy",
            amount=0.05,
            rate=2000,
            leverage=1,
            time_in_force="IOC",
        )

    with pytest.raises(OperationalException, match="durable entry reservation"):
        exchange.create_order(
            pair="BTC/USDT:USDT",
            ordertype="limit",
            side="sell",
            amount=0.05,
            rate=2000,
            leverage=1,
            time_in_force="IOC",
        )

    assert api_mock.create_order.call_count == 5
    state_path = exchange._portfolio_order_intent_store.path
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["version"] == 2
    assert state["intents"] == []
    assert {item["pair"] for item in state["reservations"]} == set(CHAN_PAIR_LIMITS)
    assert all(
        set(item) == {"client_order_id", "pair", "created_at", "exposure_seen"}
        for item in state["reservations"]
    )


def test_binance_portfolio_margin_chan_reservation_survives_restart_and_bot_identity_change(
    default_conf, mocker, tmp_path
):
    conf = persistent_portfolio_margin_chan_conf(default_conf, tmp_path)
    first_api = portfolio_margin_live_api_mock()
    first_api.fetch_positions.return_value = []
    first_api.fetch_open_orders.return_value = []
    type(first_api).has = PropertyMock(return_value={"setLeverage": False})
    mocker.patch(f"{EXMS}.amount_to_precision", lambda s, x, y: y)
    mocker.patch(f"{EXMS}.price_to_precision", lambda s, x, y, **kwargs: y)
    first_api.create_order.return_value = {
        "id": "reserved-entry",
        "clientOrderId": "ftpm-reserved-restart",
        "symbol": "BTC/USDT:USDT",
        "status": "open",
        "info": {},
    }
    first = get_patched_exchange(mocker, conf, first_api, exchange="binance")
    mocker.patch.object(
        first, "_new_portfolio_client_order_id", return_value="ftpm-reserved-restart"
    )
    first.create_order(
        pair="BTC/USDT:USDT",
        ordertype="limit",
        side="buy",
        amount=0.001,
        rate=100_000,
        leverage=1,
        time_in_force="IOC",
    )

    second_conf = deepcopy(conf)
    second_conf["bot_name"] = "different-bot-name"
    second_conf["db_url"] = "sqlite:///different-bot.sqlite"
    second_api = portfolio_margin_live_api_mock()
    second_api.fetch_positions.return_value = []
    second_api.fetch_open_orders.return_value = []
    second_api.fetch_order.side_effect = ccxt.OrderNotFound("lagging after restart")
    type(second_api).has = PropertyMock(return_value={"setLeverage": False})
    second = get_patched_exchange(mocker, second_conf, second_api, exchange="binance")

    assert second._portfolio_order_intent_store.path == first._portfolio_order_intent_store.path
    assert "chan-live-account" not in second._portfolio_order_intent_store.path.name
    with pytest.raises(OperationalException, match="without matching open database trades"):
        second.validate_existing_positions({}, [])
    with pytest.raises(OperationalException, match="durable entry reservation"):
        second.create_order(
            pair="BTC/USDT:USDT",
            ordertype="limit",
            side="sell",
            amount=0.001,
            rate=100_000,
            leverage=1,
            time_in_force="IOC",
        )
    second_api.create_order.assert_not_called()


def test_binance_portfolio_margin_chan_stale_instance_reloads_crashed_intent_before_snapshot(
    default_conf, mocker, tmp_path
):
    conf = persistent_portfolio_margin_chan_conf(default_conf, tmp_path)
    first = get_patched_exchange(mocker, conf, portfolio_margin_live_api_mock(), exchange="binance")
    second_api = portfolio_margin_live_api_mock()
    type(second_api).has = PropertyMock(return_value={"setLeverage": False})
    second = get_patched_exchange(mocker, conf, second_api, exchange="binance")

    with first._get_portfolio_create_lock():
        first._record_portfolio_order_intent(
            pair="ETH/USDT:USDT",
            client_order_id="ftpm-crashed-other-process",
            order_kind="regular",
        )

    with pytest.raises(OperationalException, match="persisted order evidence"):
        second.create_order(
            pair="ETH/USDT:USDT",
            ordertype="limit",
            side="buy",
            amount=0.05,
            rate=2000,
            leverage=1,
            time_in_force="IOC",
        )
    second_api.fetch_open_orders.assert_not_called()
    second_api.fetch_positions.assert_not_called()
    second_api.create_order.assert_not_called()


def test_binance_portfolio_margin_chan_promotion_kill_point_recovers_as_unknown_intent(
    default_conf, mocker, tmp_path
):
    mocker.patch("freqtrade.exchange.binance.sleep")
    conf = persistent_portfolio_margin_chan_conf(default_conf, tmp_path)
    api_mock = portfolio_margin_live_api_mock()
    api_mock.fetch_positions.return_value = []
    api_mock.fetch_open_orders.return_value = []
    type(api_mock).has = PropertyMock(return_value={"setLeverage": False})
    mocker.patch(f"{EXMS}.amount_to_precision", lambda s, x, y: y)
    mocker.patch(f"{EXMS}.price_to_precision", lambda s, x, y, **kwargs: y)
    api_mock.create_order.return_value = {
        "id": "confirmed-before-crash",
        "clientOrderId": "ftpm-promotion-crash",
        "symbol": "ETH/USDT:USDT",
        "status": "open",
        "info": {},
    }
    exchange = get_patched_exchange(mocker, conf, api_mock, exchange="binance")
    mocker.patch.object(
        exchange, "_new_portfolio_client_order_id", return_value="ftpm-promotion-crash"
    )
    store = exchange._portfolio_order_intent_store
    original_atomic_write = store._atomic_write
    write_count = 0

    def fail_promotion_write(intents, reservations):
        nonlocal write_count
        write_count += 1
        if write_count == 2:
            raise OperationalException("simulated promotion fsync kill point")
        return original_atomic_write(intents, reservations)

    mocker.patch.object(store, "_atomic_write", side_effect=fail_promotion_write)

    with pytest.raises(OperationalException, match="promotion fsync kill point"):
        exchange.create_order(
            pair="ETH/USDT:USDT",
            ordertype="limit",
            side="buy",
            amount=0.05,
            rate=2000,
            leverage=1,
            time_in_force="IOC",
        )

    state = json.loads(store.path.read_text(encoding="utf-8"))
    assert [item["client_order_id"] for item in state["intents"]] == ["ftpm-promotion-crash"]
    assert state["reservations"] == []
    assert api_mock.create_order.call_count == 1

    recovery_api = portfolio_margin_live_api_mock()
    recovery_api.fetch_positions.return_value = []
    recovery_api.fetch_open_orders.return_value = []
    recovery_api.fetch_order.side_effect = ccxt.OrderNotFound("still not visible")
    restarted = get_patched_exchange(mocker, conf, recovery_api, exchange="binance")
    assert restarted.portfolio_margin_unknown_order_latched is True
    with pytest.raises(OperationalException, match="could not clear every recovered"):
        restarted.validate_existing_positions({}, [])
    persisted = json.loads(store.path.read_text(encoding="utf-8"))
    assert [item["client_order_id"] for item in persisted["intents"]] == ["ftpm-promotion-crash"]
    recovery_api.create_order.assert_not_called()


def test_binance_portfolio_margin_chan_zero_fill_reservation_uses_full_release_window(
    default_conf, mocker, tmp_path
):
    mocker.patch("freqtrade.exchange.binance.sleep")
    conf = persistent_portfolio_margin_chan_conf(default_conf, tmp_path)
    api_mock = portfolio_margin_live_api_mock()
    api_mock.fetch_positions.return_value = []
    api_mock.fetch_open_orders.return_value = []
    api_mock.fetch_order.return_value = {
        "id": "zero-fill-entry",
        "clientOrderId": "ftpm-zero-fill",
        "symbol": "ETH/USDT:USDT",
        "status": "canceled",
        "filled": 0.0,
        "info": {},
    }
    exchange = get_patched_exchange(mocker, conf, api_mock, exchange="binance")
    exchange._record_portfolio_order_intent(
        pair="ETH/USDT:USDT",
        client_order_id="ftpm-zero-fill",
        order_kind="regular",
    )
    exchange._promote_portfolio_entry_reservation("ftpm-zero-fill")

    with exchange._get_portfolio_create_lock():
        exchange._reconcile_portfolio_entry_reservations()

    assert exchange._portfolio_order_intent_store.reservations == ()
    assert api_mock.fetch_order.call_count == 11
    assert api_mock.fetch_open_orders.call_count == 10
    assert api_mock.fetch_positions.call_count == 11


def test_binance_portfolio_margin_chan_filled_reservation_requires_seen_then_flat(
    default_conf, mocker, tmp_path
):
    mocker.patch("freqtrade.exchange.binance.sleep")
    conf = persistent_portfolio_margin_chan_conf(default_conf, tmp_path)
    api_mock = portfolio_margin_live_api_mock()
    api_mock.fetch_open_orders.return_value = []
    api_mock.fetch_order.return_value = {
        "id": "filled-entry",
        "clientOrderId": "ftpm-filled-entry",
        "symbol": "ETH/USDT:USDT",
        "status": "closed",
        "filled": 0.025,
        "info": {},
    }
    exchange = get_patched_exchange(mocker, conf, api_mock, exchange="binance")
    exchange._record_portfolio_order_intent(
        pair="ETH/USDT:USDT",
        client_order_id="ftpm-filled-entry",
        order_kind="regular",
    )
    exchange._promote_portfolio_entry_reservation("ftpm-filled-entry")

    api_mock.fetch_positions.return_value = []
    with exchange._get_portfolio_create_lock():
        exchange._reconcile_portfolio_entry_reservations()
    reservation = exchange._portfolio_order_intent_store.reservations[0]
    assert reservation.exposure_seen is False

    api_mock.fetch_positions.return_value = [portfolio_margin_position()]
    with exchange._get_portfolio_create_lock():
        exchange._reconcile_portfolio_entry_reservations()
    reservation = exchange._portfolio_order_intent_store.reservations[0]
    assert reservation.exposure_seen is True

    api_mock.fetch_order.reset_mock()
    api_mock.fetch_open_orders.reset_mock()
    api_mock.fetch_positions.reset_mock()
    api_mock.fetch_positions.return_value = []
    with exchange._get_portfolio_create_lock():
        exchange._reconcile_portfolio_entry_reservations()

    assert exchange._portfolio_order_intent_store.reservations == ()
    assert api_mock.fetch_order.call_count == 11
    assert api_mock.fetch_open_orders.call_count == 10
    assert api_mock.fetch_positions.call_count == 11


def test_binance_portfolio_margin_containment_cleanup_kill_point_is_recoverable(
    default_conf, mocker, tmp_path
):
    mocker.patch("freqtrade.exchange.binance.sleep")
    conf = persistent_portfolio_margin_conf(default_conf, tmp_path)
    api_mock = portfolio_margin_live_api_mock()
    api_mock.fetch_positions.return_value = []
    matching_order = {
        "id": "contained-entry",
        "clientOrderId": "ftpm-contained-parent",
        "symbol": "ETH/USDT:USDT",
        "status": "open",
        "info": {},
    }
    api_mock.fetch_open_orders.side_effect = [[matching_order], *([[]] * 9)]
    exchange = get_patched_exchange(mocker, conf, api_mock, exchange="binance")
    exchange._record_portfolio_order_intent(
        pair="ETH/USDT:USDT",
        client_order_id="ftpm-contained-parent",
        order_kind="regular",
    )
    exchange._portfolio_unknown_order_latched = True

    with exchange._get_portfolio_create_lock():
        outcome = exchange._contain_unknown_portfolio_order(
            "ETH/USDT:USDT", "ftpm-contained-parent"
        )
        assert outcome.flattened is False
        assert outcome.exchange_evidence_seen is True
    mocker.patch.object(
        exchange._portfolio_order_intent_store,
        "_atomic_write",
        side_effect=OperationalException("simulated contained cleanup kill point"),
    )
    with pytest.raises(OperationalException, match="contained cleanup kill point"):
        exchange._clear_portfolio_order_intent("ftpm-contained-parent")

    recovery_api = portfolio_margin_live_api_mock()
    recovery_api.fetch_positions.return_value = []
    recovery_api.fetch_open_orders.side_effect = [[matching_order], *([[]] * 9)]
    restarted = get_patched_exchange(mocker, conf, recovery_api, exchange="binance")
    with pytest.raises(OperationalException, match="Restart once more"):
        restarted.validate_existing_positions({}, [])
    assert (
        json.loads(restarted._portfolio_order_intent_store.path.read_text(encoding="utf-8"))[
            "intents"
        ]
        == []
    )
    recovery_api.papiGetCmPositionRisk.assert_not_called()


def test_binance_portfolio_margin_persists_emergency_containment_before_post(
    default_conf, mocker, tmp_path
):
    mocker.patch("freqtrade.exchange.binance.sleep")
    api_mock = portfolio_margin_live_api_mock()
    api_mock.fetch_order.side_effect = ccxt.OrderNotFound("not visible")
    api_mock.fetch_positions.side_effect = [
        [],
        [
            {
                "symbol": "ETH/USDT:USDT",
                "contracts": 0.025,
                "side": "long",
                "leverage": 1,
                "marginMode": "cross",
                "collateral": 50,
            }
        ],
        [],
        [],
        [],
        [],
        [],
        [],
        [],
        [],
    ]
    type(api_mock).has = PropertyMock(return_value={"setLeverage": False})
    mocker.patch(f"{EXMS}.amount_to_precision", lambda s, x, y: y)
    mocker.patch(f"{EXMS}.price_to_precision", lambda s, x, y, **kwargs: y)
    exchange = get_patched_exchange(
        mocker,
        persistent_portfolio_margin_conf(default_conf, tmp_path),
        api_mock,
        exchange="binance",
    )
    exchange._portfolio_order_recovery_attempts = 1
    client_ids = iter(("ftpm-entry-parent", "ftpm-containment-child"))
    mocker.patch.object(
        exchange,
        "_new_portfolio_client_order_id",
        side_effect=lambda: next(client_ids),
    )
    state_path = exchange._portfolio_order_intent_store.path
    containment_snapshots = []
    create_count = 0

    def create_order(*args, **kwargs):
        nonlocal create_count
        create_count += 1
        if create_count == 1:
            raise ccxt.RequestTimeout("entry status unknown")
        containment_snapshots.append(json.loads(state_path.read_text(encoding="utf-8")))
        return {
            "id": "emergency-close",
            "clientOrderId": "ftpm-containment-child",
            "symbol": "ETH/USDT:USDT",
            "status": "closed",
            "info": {},
        }

    api_mock.create_order.side_effect = create_order

    with pytest.raises(OperationalException, match="flattened detected exposure"):
        exchange.create_order(
            pair="ETH/USDT:USDT",
            ordertype="market",
            side="buy",
            amount=0.025,
            rate=2000,
            leverage=1,
        )

    intents = containment_snapshots[0]["intents"]
    assert {(intent["client_order_id"], intent["purpose"]) for intent in intents} == {
        ("ftpm-entry-parent", "submission"),
        ("ftpm-containment-child", "containment"),
    }
    child = next(intent for intent in intents if intent["purpose"] == "containment")
    assert child["parent_client_order_id"] == "ftpm-entry-parent"
    assert json.loads(state_path.read_text(encoding="utf-8"))["intents"] == []


def test_binance_portfolio_margin_position_flat_check_is_single_papi_read(default_conf, mocker):
    api_mock = portfolio_margin_live_api_mock()
    api_mock.fetch_positions.return_value = []
    exchange = get_patched_exchange(
        mocker,
        portfolio_margin_conf(default_conf, dry_run=False),
        api_mock,
        exchange="binance",
    )

    assert exchange.is_portfolio_margin_position_flat("ETH/USDT:USDT") is True
    api_mock.fetch_positions.assert_called_once_with(
        ["ETH/USDT:USDT"],
        params={"maxRetriesOnFailure": 0},
    )

    api_mock.fetch_positions.reset_mock()
    api_mock.fetch_positions.side_effect = ccxt.RequestTimeout("single read timeout")
    with pytest.raises(TemporaryError, match="Portfolio Margin positions"):
        exchange.is_portfolio_margin_position_flat("ETH/USDT:USDT")
    assert api_mock.fetch_positions.call_count == 1


def test_binance_portfolio_margin_reconciliation_and_liquidation(default_conf, mocker):
    mocker.patch("freqtrade.exchange.binance.sleep")
    api_mock = portfolio_margin_live_api_mock()
    exchange = get_patched_exchange(
        mocker,
        portfolio_margin_conf(default_conf, dry_run=False),
        api_mock,
        exchange="binance",
    )
    pair = "ETH/USDT:USDT"
    position = PositionWallet(pair, position=1.0000000001, leverage=1, side="long")
    trade = SimpleNamespace(pair=pair, amount=1.0, trade_direction="long")

    exchange.validate_existing_positions({pair: position}, [trade])
    with pytest.raises(OperationalException, match="exchange leverage is 2"):
        exchange.validate_existing_positions(
            {pair: PositionWallet(pair, position=1.0, leverage=2, side="long")},
            [trade],
        )
    with pytest.raises(OperationalException, match="database has no open trade"):
        exchange.validate_existing_positions({pair: position}, [])
    with pytest.raises(OperationalException, match="exchange side is long"):
        exchange.validate_existing_positions(
            {pair: position}, [SimpleNamespace(pair=pair, amount=1.0, trade_direction="short")]
        )
    with pytest.raises(OperationalException, match="exchange has no matching position"):
        exchange.validate_existing_positions({}, [trade])

    assert (
        exchange.get_liquidation_price(
            pair=pair,
            open_rate=2000,
            is_short=False,
            amount=1,
            stake_amount=2000,
            leverage=1,
            wallet_balance=100,
        )
        is None
    )


@pytest.mark.parametrize(
    ("exchange_orders", "order_kind"),
    [
        (
            [
                [{"id": "regular-unknown", "symbol": "ETH/USDT:USDT", "info": {}}],
                [],
            ],
            "regular",
        ),
        (
            [
                [],
                [{"id": "stop-unknown", "symbol": "ETH/USDT:USDT", "info": {}}],
            ],
            "conditional",
        ),
    ],
)
def test_binance_portfolio_margin_rejects_untracked_open_orders(
    default_conf, mocker, exchange_orders, order_kind
):
    api_mock = portfolio_margin_live_api_mock()
    api_mock.fetch_open_orders.return_value = exchange_orders[0]
    api_mock.request.return_value = exchange_orders[1]
    exchange = get_patched_exchange(
        mocker,
        portfolio_margin_conf(default_conf, dry_run=False),
        api_mock,
        exchange="binance",
    )

    with pytest.raises(OperationalException, match=rf"untracked {order_kind} open order"):
        exchange.validate_existing_positions({}, [])

    api_mock.fetch_open_orders.assert_called_once_with(
        params={
            "papi": True,
            "portfolioMargin": True,
            "maxRetriesOnFailure": 0,
        }
    )
    api_mock.request.assert_called_once_with(
        "um/algo/openAlgoOrders",
        "papi",
        "GET",
        {
            "algoType": "CONDITIONAL",
            "maxRetriesOnFailure": 0,
        },
        config={"cost": 40},
    )


def test_binance_portfolio_margin_accepts_tracked_open_orders(default_conf, mocker):
    mocker.patch("freqtrade.exchange.binance.sleep")
    api_mock = portfolio_margin_live_api_mock()
    api_mock.fetch_open_orders.return_value = [
        {"id": "entry-42", "symbol": "ETH/USDT:USDT", "info": {}}
    ]
    api_mock.request.return_value = [{"id": "stop-73", "symbol": "ETH/USDT:USDT", "info": {}}]
    exchange = get_patched_exchange(
        mocker,
        portfolio_margin_conf(default_conf, dry_run=False),
        api_mock,
        exchange="binance",
    )
    pair = "ETH/USDT:USDT"
    position = PositionWallet(pair, position=1.0, leverage=1, side="long")
    trade = SimpleNamespace(
        pair=pair,
        amount=1.0,
        trade_direction="long",
        orders=[
            SimpleNamespace(order_id="entry-42", ft_is_open=True),
            SimpleNamespace(order_id="stop-73", ft_is_open=True),
        ],
    )

    exchange.validate_existing_positions({pair: position}, [trade])
    api_mock.papiGetCmPositionRisk.assert_called_once_with()
    api_mock.papiGetCmOpenOrders.assert_called_once_with()
    api_mock.papiGetCmConditionalOpenOrders.assert_called_once_with()
    api_mock.papiGetMarginOpenOrders.assert_called_once_with()
    api_mock.papiGetMarginOpenOrderList.assert_called_once_with()
    api_mock.dapiPrivateGetPositionRisk.assert_not_called()
    api_mock.sapiGetMarginOpenOrders.assert_not_called()
    api_mock.fapiPrivateGetOpenOrders.assert_not_called()
    assert api_mock.fetch_open_orders.call_count == 3
    assert api_mock.request.call_count == 3


def test_binance_portfolio_margin_restart_detects_delayed_conditional_order(default_conf, mocker):
    sleep_mock = mocker.patch("freqtrade.exchange.binance.sleep")
    api_mock = portfolio_margin_live_api_mock()
    api_mock.fetch_open_orders.side_effect = [[], []]
    api_mock.request.side_effect = [
        [],
        [
            {
                "id": "late-stop-74",
                "clientOrderId": "ftpm-old-process",
                "symbol": "ETH/USDT:USDT",
                "status": "open",
                "info": {"newClientStrategyId": "ftpm-old-process"},
            }
        ],
    ]
    exchange = get_patched_exchange(
        mocker,
        portfolio_margin_conf(default_conf, dry_run=False),
        api_mock,
        exchange="binance",
    )

    with pytest.raises(OperationalException, match="untracked conditional open order"):
        exchange.validate_existing_positions({}, [])

    assert api_mock.fetch_open_orders.call_count == 2
    assert api_mock.request.call_count == 2
    assert [call.kwargs["params"] for call in api_mock.fetch_open_orders.call_args_list] == [
        {
            "papi": True,
            "portfolioMargin": True,
            "maxRetriesOnFailure": 0,
        },
        {
            "papi": True,
            "portfolioMargin": True,
            "maxRetriesOnFailure": 0,
        },
    ]
    assert [call.args[:3] for call in api_mock.request.call_args_list] == [
        ("um/algo/openAlgoOrders", "papi", "GET"),
        ("um/algo/openAlgoOrders", "papi", "GET"),
    ]
    assert [call.args[3] for call in api_mock.request.call_args_list] == [
        {
            "algoType": "CONDITIONAL",
            "maxRetriesOnFailure": 0,
        },
        {
            "algoType": "CONDITIONAL",
            "maxRetriesOnFailure": 0,
        },
    ]
    sleep_mock.assert_called_once_with(exchange._portfolio_order_recovery_delay)
    api_mock.fapiPrivateGetOpenOrders.assert_not_called()


@pytest.mark.parametrize(
    ("method_name", "response", "message"),
    [
        (
            "papiGetCmPositionRisk",
            [{"symbol": "BTCUSD_PERP", "positionAmt": "1"}],
            "unsupported COIN-M position",
        ),
        (
            "papiGetCmOpenOrders",
            [{"symbol": "BTCUSD_PERP", "orderId": "1"}],
            "unsupported COIN-M open orders",
        ),
        (
            "papiGetCmConditionalOpenOrders",
            [{"symbol": "BTCUSD_PERP", "strategyId": "2"}],
            "unsupported COIN-M conditional orders",
        ),
        (
            "papiGetMarginOpenOrders",
            [{"symbol": "BTCUSDT", "orderId": "3"}],
            "unsupported margin open orders",
        ),
        (
            "papiGetMarginOpenOrderList",
            [{"symbol": "BTCUSDT", "orderListId": "4"}],
            "unsupported margin OCO order lists",
        ),
    ],
)
def test_binance_portfolio_margin_rejects_unsupported_account_exposure(
    default_conf, mocker, method_name, response, message
):
    mocker.patch("freqtrade.exchange.binance.sleep")
    api_mock = portfolio_margin_live_api_mock()
    getattr(api_mock, method_name).return_value = response
    exchange = get_patched_exchange(
        mocker,
        portfolio_margin_conf(default_conf, dry_run=False),
        api_mock,
        exchange="binance",
    )

    with pytest.raises(OperationalException, match=message):
        exchange.validate_existing_positions({}, [])


def test_binance_portfolio_margin_fails_closed_when_open_orders_unavailable(default_conf, mocker):
    api_mock = portfolio_margin_live_api_mock()
    api_mock.fetch_open_orders.side_effect = ccxt.RequestTimeout("timeout")
    exchange = get_patched_exchange(
        mocker,
        portfolio_margin_conf(default_conf, dry_run=False),
        api_mock,
        exchange="binance",
    )

    with pytest.raises(OperationalException, match="open-order reconciliation request failed"):
        exchange.validate_existing_positions({}, [])


def test_binance_portfolio_margin_fails_closed_when_algo_orders_unavailable(default_conf, mocker):
    api_mock = portfolio_margin_live_api_mock()
    api_mock.fetch_open_orders.return_value = []
    api_mock.request.side_effect = ccxt.RequestTimeout("algo timeout")
    exchange = get_patched_exchange(
        mocker,
        portfolio_margin_conf(default_conf, dry_run=False),
        api_mock,
        exchange="binance",
    )

    with pytest.raises(
        OperationalException,
        match="Algo conditional-order reconciliation request failed",
    ):
        exchange.validate_existing_positions({}, [])

    api_mock.fetch_open_orders.assert_called_once()
    api_mock.fetch_orders.assert_not_called()
    api_mock.fetch_open_order.assert_not_called()
    api_mock.cancel_order.assert_not_called()


def test__set_leverage_binance(mocker, default_conf):
    api_mock = MagicMock()
    api_mock.set_leverage = MagicMock()
    type(api_mock).has = PropertyMock(return_value={"setLeverage": True})
    default_conf["dry_run"] = False
    default_conf["trading_mode"] = TradingMode.FUTURES
    default_conf["margin_mode"] = MarginMode.ISOLATED

    exchange = get_patched_exchange(mocker, default_conf, api_mock, exchange="binance")
    exchange._set_leverage(3.2, "BTC/USDT:USDT")
    assert api_mock.set_leverage.call_count == 1
    # Leverage is rounded to 3.
    assert api_mock.set_leverage.call_args_list[0][1]["leverage"] == 3
    assert api_mock.set_leverage.call_args_list[0][1]["symbol"] == "BTC/USDT:USDT"

    ccxt_exceptionhandlers(
        mocker,
        default_conf,
        api_mock,
        "binance",
        "_set_leverage",
        "set_leverage",
        pair="XRP/USDT",
        leverage=5.0,
    )


def patch_binance_vision_ohlcv(mocker, start, archive_end, api_end, timeframe):
    def make_storage(start: datetime, end: datetime, timeframe: str):
        date = pd.date_range(start, end, freq=timeframe.replace("m", "min"))
        df = pd.DataFrame(
            data=dict(date=date, open=1.0, high=1.0, low=1.0, close=1.0),
        )
        return df

    archive_storage = make_storage(start, archive_end, timeframe)
    api_storage = make_storage(start, api_end, timeframe)

    ohlcv = [[dt_ts(start), 1, 1, 1, 1]]
    # (pair, timeframe, candle_type, ohlcv, True)
    candle_history = [None, None, None, ohlcv, None]

    def get_historic_ohlcv(
        # self,
        pair: str,
        timeframe: str,
        since_ms: int,
        candle_type: CandleType,
        is_new_pair: bool = False,
        until_ms: int | None = None,
    ):
        since = dt_from_ts(since_ms)
        until = dt_from_ts(until_ms) if until_ms else api_end + timedelta(seconds=1)
        return api_storage.loc[(api_storage["date"] >= since) & (api_storage["date"] < until)]

    async def download_archive_ohlcv(
        candle_type,
        pair,
        timeframe,
        since_ms,
        until_ms,
        markets=None,
        stop_on_404=False,
    ):
        since = dt_from_ts(since_ms)
        until = dt_from_ts(until_ms) if until_ms else archive_end + timedelta(seconds=1)
        if since < start:
            pass
        return archive_storage.loc[
            (archive_storage["date"] >= since) & (archive_storage["date"] < until)
        ]

    candle_mock = mocker.patch(f"{EXMS}._async_get_candle_history", return_value=candle_history)
    api_mock = mocker.patch(f"{EXMS}.get_historic_ohlcv", side_effect=get_historic_ohlcv)
    archive_mock = mocker.patch(
        "freqtrade.exchange.binance.download_archive_ohlcv", side_effect=download_archive_ohlcv
    )
    return candle_mock, api_mock, archive_mock


@pytest.mark.parametrize(
    "timeframe,is_new_pair,since,until,first_date,last_date,candle_called,archive_called,"
    "api_called",
    [
        (
            "1m",
            True,
            dt_utc(2020, 1, 1),
            dt_utc(2020, 1, 2),
            dt_utc(2020, 1, 1),
            dt_utc(2020, 1, 1, 23, 59),
            True,
            True,
            False,
        ),
        (
            "1m",
            True,
            dt_utc(2020, 1, 1),
            dt_utc(2020, 1, 3),
            dt_utc(2020, 1, 1),
            dt_utc(2020, 1, 2, 23, 59),
            True,
            True,
            True,
        ),
        (
            "1m",
            True,
            dt_utc(2020, 1, 2),
            dt_utc(2020, 1, 2, 1),
            dt_utc(2020, 1, 2),
            dt_utc(2020, 1, 2, 0, 59),
            True,
            False,
            True,
        ),
        (
            "1m",
            False,
            dt_utc(2020, 1, 1),
            dt_utc(2020, 1, 2),
            dt_utc(2020, 1, 1),
            dt_utc(2020, 1, 1, 23, 59),
            False,
            True,
            False,
        ),
        (
            "1m",
            True,
            dt_utc(2019, 1, 1),
            dt_utc(2020, 1, 2),
            dt_utc(2020, 1, 1),
            dt_utc(2020, 1, 1, 23, 59),
            True,
            True,
            False,
        ),
        (
            "1m",
            False,
            dt_utc(2019, 1, 1),
            dt_utc(2020, 1, 2),
            dt_utc(2020, 1, 1),
            dt_utc(2020, 1, 1, 23, 59),
            False,
            True,
            False,
        ),
        (
            "1m",
            False,
            dt_utc(2019, 1, 1),
            dt_utc(2019, 1, 2),
            None,
            None,
            False,
            True,
            True,
        ),
        (
            "1m",
            True,
            dt_utc(2019, 1, 1),
            dt_utc(2019, 1, 2),
            None,
            None,
            True,
            False,
            False,
        ),
        (
            "1m",
            False,
            dt_utc(2021, 1, 1),
            dt_utc(2021, 1, 2),
            None,
            None,
            False,
            False,
            False,
        ),
        (
            "1m",
            True,
            dt_utc(2021, 1, 1),
            dt_utc(2021, 1, 2),
            None,
            None,
            True,
            False,
            False,
        ),
        (
            "1h",
            False,
            dt_utc(2020, 1, 1),
            dt_utc(2020, 1, 2),
            dt_utc(2020, 1, 1),
            dt_utc(2020, 1, 1, 23),
            False,
            False,
            True,
        ),
        (
            "1m",
            False,
            dt_utc(2020, 1, 1),
            dt_utc(2020, 1, 1, 3, 50, 30),
            dt_utc(2020, 1, 1),
            dt_utc(2020, 1, 1, 3, 50),
            False,
            True,
            False,
        ),
    ],
)
def test_get_historic_ohlcv_binance(
    mocker,
    default_conf,
    timeframe,
    is_new_pair,
    since,
    until,
    first_date,
    last_date,
    candle_called,
    archive_called,
    api_called,
):
    exchange = get_patched_exchange(mocker, default_conf, exchange="binance")

    start = dt_utc(2020, 1, 1)
    archive_end = dt_utc(2020, 1, 2)
    api_end = dt_utc(2020, 1, 3)
    candle_mock, api_mock, archive_mock = patch_binance_vision_ohlcv(
        mocker, start=start, archive_end=archive_end, api_end=api_end, timeframe=timeframe
    )

    candle_type = CandleType.SPOT
    pair = "BTC/USDT"

    since_ms = dt_ts(since)
    until_ms = dt_ts(until)

    df = exchange.get_historic_ohlcv(pair, timeframe, since_ms, candle_type, is_new_pair, until_ms)

    if df.empty:
        assert first_date is None
        assert last_date is None
    else:
        assert df["date"].iloc[0] == first_date
        assert df["date"].iloc[-1] == last_date
        assert (
            df["date"].diff().iloc[1:] == timedelta(seconds=timeframe_to_seconds(timeframe))
        ).all()

    if candle_called:
        candle_mock.assert_called_once()
    if archive_called:
        archive_mock.assert_called_once()
    if api_called:
        api_mock.assert_called_once()
    candle_mock.reset_mock()
    api_mock.reset_mock()
    archive_mock.reset_mock()

    # binanceus does not use archive mode!
    exchange._can_use_data_download_fast = False
    df = exchange.get_historic_ohlcv(pair, timeframe, since_ms, candle_type, is_new_pair, until_ms)
    # Never uses archive
    assert archive_mock.call_count == 0
    assert candle_mock.call_count == (0 if not candle_called else 1)
    if api_called:
        assert api_mock.call_count == 1


@pytest.mark.parametrize(
    "pair,notional_value,mm_ratio,amt",
    [
        ("XRP/USDT:USDT", 0.0, 0.025, 0),
        ("BNB/USDT:USDT", 100.0, 0.0065, 0),
        ("BTC/USDT:USDT", 170.30, 0.004, 0),
        ("XRP/USDT:USDT", 999999.9, 0.1, 27500.0),
        ("BNB/USDT:USDT", 5000000.0, 0.15, 233035.0),
        ("BTC/USDT:USDT", 600000000, 0.5, 1.997038e8),
    ],
)
def test_get_maintenance_ratio_and_amt_binance(
    default_conf,
    mocker,
    leverage_tiers,
    pair,
    notional_value,
    mm_ratio,
    amt,
):
    mocker.patch(f"{EXMS}.exchange_has", return_value=True)
    exchange = get_patched_exchange(mocker, default_conf, exchange="binance")
    exchange._leverage_tiers = leverage_tiers
    (result_ratio, result_amt) = exchange.get_maintenance_ratio_and_amt(pair, notional_value)
    assert (round(result_ratio, 8), round(result_amt, 8)) == (mm_ratio, amt)


async def test__async_get_trade_history_id_binance(default_conf_usdt, mocker, fetch_trades_result):
    default_conf_usdt["exchange"]["only_from_ccxt"] = True
    exchange = get_patched_exchange(mocker, default_conf_usdt, exchange="binance")

    async def mock_get_trade_hist(pair, *args, **kwargs):
        if "since" in kwargs:
            # older than initial call
            if kwargs["since"] < 1565798399752:
                return []
            else:
                # Don't expect to get here
                raise ValueError("Unexpected call")
                # return fetch_trades_result[:-2]
        elif kwargs.get("params", {}).get(exchange._ft_has["trades_pagination_arg"]) == "0":
            # Return first 3
            return fetch_trades_result[:-2]
        elif kwargs.get("params", {}).get(exchange._ft_has["trades_pagination_arg"]) in (
            fetch_trades_result[-3]["id"],
            1565798399752,
        ):
            # Return 2
            return fetch_trades_result[-3:-1]
        else:
            # Return last 2
            return fetch_trades_result[-2:]

    exchange._api_async.fetch_trades = MagicMock(side_effect=mock_get_trade_hist)

    pair = "ETH/BTC"
    ret = await exchange._async_get_trade_history_id(
        pair,
        since=fetch_trades_result[0]["timestamp"],
        until=fetch_trades_result[-1]["timestamp"] - 1,
    )
    assert ret[0] == pair
    assert isinstance(ret[1], list)
    assert exchange._api_async.fetch_trades.call_count == 4

    fetch_trades_cal = exchange._api_async.fetch_trades.call_args_list
    # first call (using since, not fromId)
    assert fetch_trades_cal[0][0][0] == pair
    assert fetch_trades_cal[0][1]["since"] == fetch_trades_result[0]["timestamp"]

    # 2nd call
    assert fetch_trades_cal[1][0][0] == pair
    assert "params" in fetch_trades_cal[1][1]
    pagination_arg = exchange._ft_has["trades_pagination_arg"]
    assert pagination_arg in fetch_trades_cal[1][1]["params"]
    # Initial call was with from_id = "0"
    assert fetch_trades_cal[1][1]["params"][pagination_arg] == "0"

    assert fetch_trades_cal[2][1]["params"][pagination_arg] != "0"
    assert fetch_trades_cal[3][1]["params"][pagination_arg] != "0"

    # Clean up event loop to avoid warnings
    exchange.close()


async def test__async_get_trade_history_id_binance_fast(
    default_conf_usdt, mocker, fetch_trades_result
):
    default_conf_usdt["exchange"]["only_from_ccxt"] = False
    exchange = get_patched_exchange(mocker, default_conf_usdt, exchange="binance")

    async def mock_get_trade_hist(pair, *args, **kwargs):
        if "since" in kwargs:
            pass
            # older than initial call
            # if kwargs["since"] < 1565798399752:
            #     return []
            # else:
            #     # Don't expect to get here
            #     raise ValueError("Unexpected call")
            #     # return fetch_trades_result[:-2]
        elif kwargs.get("params", {}).get(exchange._ft_has["trades_pagination_arg"]) == "0":
            # Return first 3
            return fetch_trades_result[:-2]
        # elif kwargs.get("params", {}).get(exchange._ft_has['trades_pagination_arg']) in (
        #     fetch_trades_result[-3]["id"],
        #     1565798399752,
        # ):
        #     # Return 2
        #     return fetch_trades_result[-3:-1]
        # else:
        #     # Return last 2
        #     return fetch_trades_result[-2:]

    pair = "ETH/BTC"
    mocker.patch(
        "freqtrade.exchange.binance.download_archive_trades",
        return_value=(pair, trades_dict_to_list(fetch_trades_result[-2:])),
    )

    exchange._api_async.fetch_trades = MagicMock(side_effect=mock_get_trade_hist)

    ret = await exchange._async_get_trade_history(
        pair,
        since=fetch_trades_result[0]["timestamp"],
        until=fetch_trades_result[-1]["timestamp"] - 1,
    )

    assert ret[0] == pair
    assert isinstance(ret[1], list)

    # Clean up event loop to avoid warnings
    exchange.close()


def test_check_delisting_time_binance(default_conf_usdt, mocker):
    exchange = get_patched_exchange(mocker, default_conf_usdt, exchange="binance")
    exchange._config["runmode"] = RunMode.BACKTEST
    delist_mock = MagicMock(return_value=None)
    delist_fut_mock = MagicMock(return_value=None)
    mocker.patch.object(exchange, "_get_spot_pair_delist_time", delist_mock)
    mocker.patch.object(exchange, "_check_delisting_futures", delist_fut_mock)

    # Invalid run mode
    resp = exchange.check_delisting_time("BTC/USDT")
    assert resp is None
    assert delist_mock.call_count == 0
    assert delist_fut_mock.call_count == 0

    # Delist spot called
    exchange._config["runmode"] = RunMode.DRY_RUN
    resp1 = exchange.check_delisting_time("BTC/USDT")
    assert resp1 is None
    assert delist_mock.call_count == 1
    assert delist_fut_mock.call_count == 0
    delist_mock.reset_mock()

    # Delist futures called
    exchange.trading_mode = TradingMode.FUTURES
    resp1 = exchange.check_delisting_time("BTC/USDT:USDT")
    assert resp1 is None
    assert delist_mock.call_count == 0
    assert delist_fut_mock.call_count == 1


def test__check_delisting_futures_binance(default_conf_usdt, mocker, markets):
    markets["BTC/USDT:USDT"] = deepcopy(markets["SOL/BUSD:BUSD"])
    markets["BTC/USDT:USDT"]["info"]["deliveryDate"] = 4133404800000
    markets["SOL/BUSD:BUSD"]["info"]["deliveryDate"] = 4133404800000
    markets["ADA/USDT:USDT"]["info"]["deliveryDate"] = 1760745600000  # 2025-10-18
    exchange = get_patched_exchange(mocker, default_conf_usdt, exchange="binance")
    mocker.patch(f"{EXMS}.markets", PropertyMock(return_value=markets))

    resp_sol = exchange._check_delisting_futures("SOL/BUSD:BUSD")
    # Delisting is equal to BTC
    assert resp_sol is None
    # Actually has a delisting date
    resp_ada = exchange._check_delisting_futures("ADA/USDT:USDT")
    assert resp_ada == dt_utc(2025, 10, 18)


def test__get_spot_delist_schedule_binance(default_conf_usdt, mocker):
    exchange = get_patched_exchange(mocker, default_conf_usdt, exchange="binance")
    ret_value = [{"delistTime": 1759114800000, "symbols": ["ETCBTC"]}]
    schedule_mock = mocker.patch.object(exchange, "_get_spot_delist_schedule", return_value=None)

    # None - mode is DRY
    assert exchange._get_spot_pair_delist_time("ETC/BTC") is None
    # Switch to live
    exchange._config["runmode"] = RunMode.LIVE
    assert exchange._get_spot_pair_delist_time("ETC/BTC") is None

    mocker.patch.object(exchange, "_get_spot_delist_schedule", return_value=ret_value)
    resp = exchange._get_spot_pair_delist_time("ETC/BTC")
    assert resp == dt_utc(2025, 9, 29, 3, 0)
    assert schedule_mock.call_count == 1
    schedule_mock.reset_mock()

    # Caching - don't refresh.
    assert exchange._get_spot_pair_delist_time("ETC/BTC", refresh=False) == dt_utc(
        2025, 9, 29, 3, 0
    )
    assert schedule_mock.call_count == 0

    api_mock = MagicMock()
    ccxt_exceptionhandlers(
        mocker,
        default_conf_usdt,
        api_mock,
        "binance",
        "_get_spot_delist_schedule",
        "sapi_get_spot_delist_schedule",
        retries=1,
    )
