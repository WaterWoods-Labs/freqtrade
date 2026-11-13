from copy import deepcopy

import ccxt
import pytest

from freqtrade.exceptions import TemporaryError
from tests.conftest import get_patched_exchange
from tests.exchange.test_binance import (
    dca_reservation_retry_exchange,
    portfolio_margin_conf,
    portfolio_margin_dca_conf,
    portfolio_margin_live_api_mock,
    portfolio_margin_position,
)


@pytest.mark.parametrize("stop", [False, True])
@pytest.mark.parametrize("error", [ccxt.OperationRejected("-2011"), ccxt.RequestTimeout("lost")])
@pytest.mark.parametrize("status,filled", [("closed", 1.0), ("canceled", 0.4), ("canceled", 0.0)])
def test_ambiguous_cancel_reads_actual_fill_once(default_conf, mocker, stop, error, status, filled):
    api = portfolio_margin_live_api_mock()
    exchange = get_patched_exchange(
        mocker, portfolio_margin_conf(default_conf, dry_run=False), api, exchange="binance"
    )
    mocker.patch.object(exchange, "get_contract_size", return_value=1.0)
    order = {
        "id": "exact",
        "symbol": "ETH/USDT:USDT",
        "status": status,
        "amount": 1.0,
        "filled": filled,
        "fee": None,
    }
    if stop:
        cancel = mocker.patch.object(exchange, "_cancel_portfolio_algo_order", side_effect=error)
        fetch = mocker.patch.object(exchange, "fetch_stoploss_order", return_value=order)
        method = exchange.cancel_stoploss_order_with_result
    else:
        cancel = api.cancel_order
        cancel.side_effect = error
        fetch = api.fetch_order
        fetch.return_value = order
        method = exchange.cancel_order_with_result
    sleep = mocker.patch("freqtrade.exchange.common.time.sleep")
    result = method("exact", "ETH/USDT:USDT", 1.0)
    assert result["status"] == status
    assert result["filled"] == filled
    cancel.assert_called_once()
    fetch.assert_called_once()
    sleep.assert_not_called()


@pytest.mark.parametrize("stop", [False, True])
@pytest.mark.parametrize(
    "changes",
    [
        {"id": "other"},
        {"symbol": "BTC/USDT:USDT"},
        {"status": "open"},
        {"filled": None},
        {"filled": float("nan")},
        {"filled": -1},
        {"filled": 2},
        {"amount": None},
    ],
)
def test_ambiguous_cancel_never_fabricates_terminal_zero(default_conf, mocker, stop, changes):
    api = portfolio_margin_live_api_mock()
    exchange = get_patched_exchange(
        mocker, portfolio_margin_conf(default_conf, dry_run=False), api, exchange="binance"
    )
    order = {
        "id": "exact",
        "symbol": "ETH/USDT:USDT",
        "status": "canceled",
        "amount": 1.0,
        "filled": 0,
        "fee": None,
        **changes,
    }
    if stop:
        mocker.patch.object(
            exchange, "_cancel_portfolio_algo_order", side_effect=ccxt.OrderNotFound()
        )
        mocker.patch.object(exchange, "fetch_stoploss_order", return_value=order)
        method = exchange.cancel_stoploss_order_with_result
    else:
        api.cancel_order.side_effect = ccxt.OrderNotFound()
        api.fetch_order.return_value = order
        method = exchange.cancel_order_with_result
    with pytest.raises(TemporaryError, match="terminal fill evidence"):
        method("exact", "ETH/USDT:USDT", 1.0)


def test_missing_cancel_result_defers_without_repeated_delete(default_conf, mocker):
    api = portfolio_margin_live_api_mock()
    exchange = get_patched_exchange(
        mocker, portfolio_margin_conf(default_conf, dry_run=False), api, exchange="binance"
    )
    api.cancel_order.side_effect = ccxt.OperationRejected("-2011")
    api.fetch_order.side_effect = ccxt.OrderNotFound()
    with pytest.raises(TemporaryError, match="unconfirmed"):
        exchange.cancel_order_with_result("exact", "ETH/USDT:USDT", 1.0)
    api.cancel_order.assert_called_once()
    api.fetch_order.assert_called_once()


def test_admission_shares_terminal_reads_but_rechecks_next_attempt(default_conf, mocker, tmp_path):
    _, exchange, api, _orders = dca_reservation_retry_exchange(default_conf, mocker, tmp_path)
    store = exchange._portfolio_order_intent_store
    store.remove_reservation("ftpm-canceled-add")
    api.fetch_order.reset_mock()
    api.fetch_positions.reset_mock()
    exchange._validate_portfolio_margin_dca_projected_exposure(
        "ETH/USDT:USDT", 20, "ftpm-next", "buy"
    )
    assert api.fetch_order.call_count == 1
    # One reconciliation snapshot plus the fresh final admission snapshot.
    assert api.fetch_positions.call_count == 2
    exchange._validate_portfolio_margin_dca_projected_exposure(
        "ETH/USDT:USDT", 20, "ftpm-next", "buy"
    )
    assert api.fetch_order.call_count == 2
    assert api.fetch_positions.call_count == 4
    api.create_order.assert_not_called()


def test_nonterminal_reservation_is_not_reused(default_conf, mocker, tmp_path):
    _, exchange, api, orders = dca_reservation_retry_exchange(default_conf, mocker, tmp_path)
    exchange._portfolio_order_intent_store.remove_reservation("ftpm-canceled-add")
    first = orders["ftpm-first"]
    api.fetch_order.side_effect = [dict(first, status="open"), deepcopy(first)]
    exchange._validate_portfolio_margin_dca_projected_exposure(
        "ETH/USDT:USDT", 20, "ftpm-next", "buy"
    )
    assert api.fetch_order.call_count == 2


def test_fourteen_reservations_share_one_reconciliation_snapshot(default_conf, mocker, tmp_path):
    from freqtrade.exchange.binance_order_intent import PortfolioOrderIntent

    conf = portfolio_margin_dca_conf(default_conf, tmp_path, exposure_limit=2000)
    api = portfolio_margin_live_api_mock()
    exchange = get_patched_exchange(mocker, conf, api, exchange="binance")
    mocker.patch.object(exchange, "get_contract_size", return_value=1.0)
    pairs = sorted(conf["exchange"]["portfolio_margin_risk"]["pairs"])
    store = exchange._portfolio_order_intent_store
    orders = {}
    for index, pair in enumerate(pairs[:14]):
        cid = f"ftpm-existing-{index}"
        store.add(PortfolioOrderIntent(cid, pair, "regular"))
        store.promote_to_entry_reservation(cid)
        orders[cid] = {
            "id": cid,
            "clientOrderId": cid,
            "symbol": pair,
            "side": "buy",
            "status": "closed",
            "filled": 0.02,
            "amount": 0.02,
            "price": 2000.0,
        }
    api.fetch_order.side_effect = lambda cid, *args, **kwargs: deepcopy(orders[cid])
    api.fetch_positions.return_value = [
        {**portfolio_margin_position(pair, contracts=0.02), "notional": 40.0} for pair in pairs[:14]
    ]
    exchange._validate_portfolio_margin_dca_projected_exposure(pairs[14], 20, "ftpm-next", "buy")
    assert api.fetch_order.call_count == 14
    assert api.fetch_positions.call_count == 2
    assert all(r.exposure_seen for r in store.reservations)
    api.create_order.assert_not_called()
