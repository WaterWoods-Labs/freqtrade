from decimal import Decimal
from unittest.mock import Mock

import ccxt
import pytest

from freqtrade.enums import RunMode
from freqtrade.exceptions import (
    CancelReconciliationPending,
    CancelReconciliationRequired,
    OperationalException,
    TemporaryError,
)
from freqtrade.exchange.binance_order_intent import PortfolioOrderIntent
from tests.conftest import get_patched_exchange
from tests.exchange.test_binance import portfolio_margin_dca_conf, portfolio_margin_live_api_mock


PAIR = "ETH/USDT:USDT"
PARENT = "ftpm-initial"
CHILD = "ftpm-protect"


@pytest.fixture
def live_exchange(default_conf, tmp_path, mocker):
    conf = portfolio_margin_dca_conf(default_conf, tmp_path)
    conf["exchange"]["papi_auto_reconcile_cancels"] = True
    exchange = get_patched_exchange(
        mocker, conf, portfolio_margin_live_api_mock(), exchange="binance"
    )
    mocker.patch("freqtrade.exchange.binance.sleep")
    mocker.patch.object(exchange, "get_contract_size", return_value=1.0)
    return exchange


def observed(exchange):
    store = exchange._portfolio_order_intent_store
    store.add(PortfolioOrderIntent(PARENT, PAIR, "regular"))
    store.promote_to_entry_reservation(PARENT)
    return {
        "id": "123",
        "clientOrderId": PARENT,
        "symbol": PAIR,
        "status": "open",
        "side": "sell",
        "amount": 2.0,
        "filled": 1.0,
        "info": {
            "orderId": 123,
            "clientOrderId": PARENT,
            "symbol": exchange.markets[PAIR]["id"],
            "side": "SELL",
            "origQty": "2",
            "executedQty": "1",
            "status": "PARTIALLY_FILLED",
        },
    }


@pytest.mark.parametrize("lost_ack", [False, True])
def test_partial_incident_owns_exact_containment_children(live_exchange, mocker, lost_ack):
    exchange = live_exchange
    order = observed(exchange)
    store = exchange._portfolio_order_intent_store
    mocker.patch.object(exchange, "_resolve_portfolio_partial_cancel", return_value=None)
    mocker.patch.object(exchange, "_new_portfolio_client_order_id", return_value=CHILD)
    mocker.patch.object(exchange, "amount_to_precision", return_value=1.0)
    mocker.patch.object(exchange, "_recover_portfolio_order", return_value=None)

    def close(*args):
        assert sorted(i.client_order_id for i in store.intents) == sorted([PARENT, CHILD])
        assert args[-1]["clientOrderId"] == CHILD and args[-1]["reduceOnly"]
        if lost_ack:
            raise ccxt.RequestTimeout()
        return {"id": "456", "clientOrderId": CHILD, "symbol": PAIR, "side": "buy"}

    exchange._api.create_order.side_effect = close
    positions = [({"symbol": PAIR, "side": "short"}, 1.0)]

    def contain(pair, parent, children):
        exchange._flatten_portfolio_position(pair, positions, parent, children)

    mocker.patch.object(exchange, "_contain_unknown_portfolio_order", side_effect=contain)
    with pytest.raises(CancelReconciliationRequired) as error:
        exchange.settle_portfolio_dca_partial_entry(order, PAIR)
    incident = error.value
    assert (incident.kind, incident.order_id, incident.client_id, incident.side) == (
        "partial_entry",
        "123",
        PARENT,
        "sell",
    )
    assert incident.containment_orders == ((CHILD, None if lost_ack else "456"),)
    assert incident.observed_amount == incident.observed_base_amount == 2
    assert incident.observed_fill == incident.observed_base_fill == 1
    assert exchange.portfolio_margin_unknown_order_latched
    exchange._api.create_order.assert_called_once()
    with pytest.raises(OperationalException, match="outside"):
        exchange.clear_reconciled_cancel(incident)
    with pytest.raises(OperationalException, match="originating"):
        exchange.reconcile_cancel(
            Mock(), CancelReconciliationRequired("partial_entry", PAIR, "123", PARENT)
        )

    def after_proof(bot, current):
        with exchange._get_portfolio_create_lock():
            exchange.clear_reconciled_cancel(current)

    mocker.patch(
        "freqtrade.exchange.binance_cancel_reconcile.reconcile_cancel", side_effect=after_proof
    )
    exchange.reconcile_cancel(Mock(exchange=exchange), incident)
    assert not store.intents and not exchange.portfolio_margin_unknown_order_latched
    with pytest.raises(OperationalException, match="originating"):
        exchange.reconcile_cancel(Mock(), incident)


def test_conflicting_partial_receipt_blocks_reactivation(live_exchange, mocker):
    exchange = live_exchange
    order = observed(exchange)
    mocker.patch.object(
        exchange, "_resolve_portfolio_partial_cancel", side_effect=OperationalException("conflict")
    )
    mocker.patch.object(exchange, "_contain_unknown_portfolio_order")
    with pytest.raises(CancelReconciliationRequired) as error:
        exchange.settle_portfolio_dca_partial_entry(order, PAIR)
    with pytest.raises(OperationalException, match="contradictory"):
        exchange.reconcile_cancel(Mock(), error.value)
    assert (
        exchange.portfolio_margin_unknown_order_latched
        and exchange._portfolio_order_intent_store.intents
    )


def test_incident_keeps_highest_exact_fill_in_contract_and_trade_units(live_exchange, mocker):
    exchange = live_exchange
    order = observed(exchange)
    order.update(amount=0.2, filled=0.1)
    mocker.patch.object(exchange, "get_contract_size", return_value=0.1)
    mocker.patch.object(exchange, "_contain_unknown_portfolio_order")

    # A subsequent exact GET saw more executed contracts before the cancel became ambiguous.
    def observe_more(evidence):
        evidence.observe(
            {
                **order,
                "amount": 2.0,
                "filled": 1.4,
                "info": {**order["info"], "executedQty": "1.4"},
            },
            "orderId",
        )

    mocker.patch.object(exchange, "_resolve_portfolio_partial_cancel", side_effect=observe_more)
    with pytest.raises(CancelReconciliationRequired) as error:
        exchange.settle_portfolio_dca_partial_entry(order, PAIR)
    event = error.value
    assert event.blocker is None
    assert (event.observed_amount, event.observed_fill) == (Decimal(2), Decimal("1.4"))
    assert (event.observed_base_amount, event.observed_base_fill) == (Decimal(".2"), Decimal(".14"))


@pytest.mark.parametrize("stop", [False, True])
def test_deferred_cancel_remains_temporary_until_caller_identifies_a_fault(
    live_exchange, mocker, stop
):
    exchange = live_exchange
    if stop:
        mocker.patch.object(
            exchange, "fetch_stoploss_order", side_effect=TemporaryError("deferred")
        )
    else:
        exchange._api.fetch_order.side_effect = ccxt.RequestTimeout()
    with pytest.raises(TemporaryError) as error:
        exchange._reconcile_portfolio_cancel("123", PAIR, stop=stop)
    assert isinstance(error.value, CancelReconciliationPending)
    assert error.value.incident.kind == ("stop" if stop else "regular")
    assert not exchange.portfolio_margin_unknown_order_latched
    assert exchange._active_cancel_incident is None


def test_wrong_original_ack_blocks_recovery_after_deferred_get(live_exchange):
    exchange = live_exchange
    exchange._api.fetch_order.side_effect = ccxt.RequestTimeout()
    with pytest.raises(CancelReconciliationPending) as error:
        exchange._portfolio_cancel_receipt_or_query({"id": "WRONG"}, "123", PAIR, stop=False)
    with pytest.raises(OperationalException, match="identified another order") as conflict:
        exchange.reconcile_cancel(Mock(), error.value.incident)
    assert conflict.value.safe_to_notify and "WRONG" not in str(conflict.value)
    exchange._api.fetch_order.assert_not_called()


def test_unacknowledged_native_stop_delete_keeps_its_stopping_semantics(live_exchange, mocker):
    exchange = live_exchange
    delete = mocker.patch.object(exchange, "_portfolio_algo_request", return_value={})
    mocker.patch.object(exchange, "fetch_stoploss_order", side_effect=TemporaryError("not visible"))
    with pytest.raises(CancelReconciliationRequired) as error:
        exchange.cancel_stoploss_order("123", PAIR)
    assert error.value.kind == "stop" and error.value.order_id == "123"
    delete.assert_called_once_with("um/algo/order", "DELETE", {"algoId": "123"})


def test_only_installed_wrapper_and_direct_current_cause_are_recoverable(live_exchange):
    exchange = live_exchange
    pending = exchange._pending_cancel("unconfirmed", "123", PAIR, True)
    wrapper = OperationalException("Trailing market exit needs order reconciliation")
    wrapper.__cause__ = pending
    assert exchange.recoverable_cancel_cause(wrapper) is pending.incident
    other = OperationalException("unrelated fault")
    other.__cause__ = pending
    assert exchange.recoverable_cancel_cause(other) is None
    wrapper.__cause__ = other
    assert exchange.recoverable_cancel_cause(wrapper) is None
    wrapper.__cause__ = pending
    exchange._pending_cancel("next operation", "456", PAIR, False)
    assert exchange.recoverable_cancel_cause(wrapper) is None


@pytest.mark.parametrize("enabled", [False, "true", None])
def test_non_opted_in_dca_returns_only_normal_temporary_error(
    default_conf, tmp_path, mocker, enabled
):
    conf = portfolio_margin_dca_conf(default_conf, tmp_path)
    conf["exchange"]["papi_auto_reconcile_cancels"] = enabled
    exchange = get_patched_exchange(
        mocker, conf, portfolio_margin_live_api_mock(), exchange="binance"
    )
    assert not exchange._papi_cancel_recovery_enabled
    assert type(exchange._pending_cancel("unconfirmed", "123", PAIR, False)) is TemporaryError
    assert exchange._last_cancel_incident is None


def test_verified_clear_is_atomic_and_retains_only_exact_reservation(live_exchange):
    store = live_exchange._portfolio_order_intent_store
    store.add(PortfolioOrderIntent(PARENT, PAIR, "regular"))
    store.add(PortfolioOrderIntent(CHILD, PAIR, "regular", "containment", PARENT))
    before = store.path.read_bytes()
    with pytest.raises(OperationalException, match="unrelated"):
        store.resolve_verified_partial_cancel(PAIR, PARENT, set(), retain_entry=False)
    assert store.path.read_bytes() == before
    store.resolve_verified_partial_cancel(PAIR, PARENT, {CHILD}, retain_entry=True)
    (reservation,) = store.reservations
    assert (reservation.client_order_id, reservation.pair, reservation.exposure_seen) == (
        PARENT,
        PAIR,
        True,
    )
    assert not store.intents


def test_non_trading_runmode_never_enables_recovery(default_conf, tmp_path, mocker):
    conf = portfolio_margin_dca_conf(default_conf, tmp_path)
    conf["runmode"] = RunMode.BACKTEST
    conf["exchange"]["papi_auto_reconcile_cancels"] = True
    exchange = get_patched_exchange(
        mocker, conf, portfolio_margin_live_api_mock(), exchange="binance"
    )
    assert conf["dry_run"] is False
    assert not exchange._papi_cancel_recovery_enabled
