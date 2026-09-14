from copy import deepcopy
from unittest.mock import Mock

import ccxt
import pytest

from freqtrade.exchange.umx import UMX
from freqtrade.exchange.umx_api import UMXSync


PAIR = "SNDK/USDT:USDT"
SYMBOL = "SNDK-USDT-PERP"


@pytest.fixture
def stop_api():
    api = object.__new__(UMXSync)
    api.markets = {PAIR: {"base": "SNDK", "quote": "USDT"}}
    api._is_futures_symbol = Mock(return_value=True)
    api.market_id = Mock(return_value=SYMBOL)
    api._contract_size = Mock(return_value=0.01)
    api._coin_amount_to_contracts = Mock(side_effect=lambda symbol, qty: qty / 0.01)
    api.fetch_positions = Mock(return_value=[{"side": "long", "contracts": 1}])
    api.client = Mock()
    api.client.private_params.side_effect = lambda params: params
    raw = {
        "symbol": SYMBOL,
        "complexOId": "stop1",
        "side": "sell",
        "qty": "0.01",
        "status": "live",
        "createTime": "1000000",
        "tpslOrder": {
            "tpslClOrdId": "ftsltest",
            "tpslMode": "partially_position",
            "stopLoss": "1455",
            "stopLossType": "last_price",
            "slOrderType": "market",
        },
    }
    api._test_rows = []

    def request(method, path, **kwargs):
        if path == "/v2/trade/stopPosition":
            assert method == "POST"
            data = kwargs["data"]
            assert data["positionIdx"] == "net" and data["tpslMode"] == "partially_position"
            assert data["tpslQty"] == "0.01" and data["slOrderType"] == "market"
            created = deepcopy(raw)
            created["tpslOrder"]["tpslClOrdId"] = data["tpslClOrdId"]
            api._test_rows = [created]
            return {"data": {"orderId": "stop1"}, "ts": 1000000}
        if path == "/v2/trade/openOrderComplex":
            return {"data": [r for r in api._test_rows if r["status"] == "live"]}
        if path == "/v2/history/orderComplexs":
            return {"data": [r for r in api._test_rows if r["status"] != "live"]}
        if path == "/v1/trade/cancelComplex":
            assert kwargs["data"]["complexOId"] == "stop1"
            api._test_rows[0]["status"] = "canceled"
            return {"data": {"complexOId": "stop1"}}
        raise AssertionError(path)

    api.client.request.side_effect = request
    return api


def create(api):
    return api.create_order(
        symbol=PAIR,
        type="stop_market",
        side="sell",
        amount=1,
        params={"reduceOnly": True, "stopLossPrice": 1455},
    )


def test_position_bound_stop_transport_and_retry_adoption(stop_api):
    order = create(stop_api)
    assert order["amount"] == 1 and order["filled"] == 0 and order["status"] == "open"
    assert order["stopLossPrice"] == 1455
    # A later call following an uncertain response adopts the accepted stop, with no second POST.
    assert create(stop_api)["id"] == "stop1"
    writes = [c for c in stop_api.client.request.call_args_list if c.args[0] == "POST"]
    assert len(writes) == 1


@pytest.mark.parametrize("side,stop", [("buy", "1455.01"), ("sell", "1545.01")])
def test_entry_transports_attached_stop_in_the_same_request(stop_api, side, stop):
    stop_api.entry_stoploss = -0.03
    stop_api._market = Mock(
        return_value={"precision": {"price": 2}, "info": {"quantityPrecision": 4}}
    )
    stop_api.fetch_leverage = Mock(return_value={"longLeverage": 1})
    stop_api.client.place_order.return_value = {"ts": 1000000, "data": {"orderId": "entry1"}}
    stop_api.create_order(PAIR, "limit", side, 1, 1500.01, {"postOnly": True})
    body = stop_api.client.place_order.call_args.args[0]
    assert body["orderType"] == "post_only" and float(body["qty"]) == 0.01
    assert body["tpslOrder"] == {
        "tpslClOrdId": body["tpslOrder"]["tpslClOrdId"],
        "stopLoss": stop,
        "stopLossType": "last_price",
        "slOrderType": "market",
        "slLimitPrice": None,
    }
    assert body["tpslOrder"]["tpslClOrdId"].startswith("ftsa")
    stop_api.client.request.assert_not_called()
    stop_api.create_order(PAIR, "market", side, 1, params={"reduceOnly": True})
    assert "tpslOrder" not in stop_api.client.place_order.call_args.args[0]


def test_attached_stop_is_adopted_without_duplicate_after_better_entry_fill(stop_api):
    create(stop_api)
    row = stop_api._test_rows[0]
    row["tpslOrder"].update(tpslClOrdId="ftsafixture", stopLoss="1456")
    row["tpslOrder"].pop("tpslMode")
    stop_api.client.request.reset_mock()
    assert create(stop_api)["id"] == "stop1"
    assert all(c.args[0] == "GET" for c in stop_api.client.request.call_args_list)
    row["qty"] = "0.02"
    with pytest.raises(ccxt.InvalidOrder, match="Conflicting"):
        create(stop_api)


@pytest.mark.parametrize("params", [{}, {"postOnly": True, "tpslOrder": {}}])
def test_maker_entry_cannot_omit_po_or_override_protection(stop_api, params):
    stop_api.entry_stoploss = -0.03
    with pytest.raises(ccxt.InvalidOrder):
        stop_api.create_order(PAIR, "limit", "buy", 1, 1500, params)
    stop_api.client.place_order.assert_not_called()


@pytest.mark.parametrize(
    "positions", [[], [{"side": "short", "contracts": 1}], [{"side": "long", "contracts": 0.5}]]
)
def test_stop_cannot_open_or_overclose_position(stop_api, positions):
    stop_api.fetch_positions.return_value = positions
    with pytest.raises(ccxt.InvalidOrder):
        create(stop_api)
    stop_api.client.request.assert_not_called()


def test_conflicting_protection_is_not_silently_replaced(stop_api):
    create(stop_api)
    stop_api._test_rows[0]["tpslOrder"]["stopLoss"] = "1400"
    with pytest.raises(ccxt.InvalidOrder):
        create(stop_api)


def test_trigger_resolves_real_execution_order_not_assumed_fill(stop_api):
    create(stop_api)
    stop_api._test_rows[0]["status"] = "slEffective"
    stop_api.client.order_info.return_value = {"data": {"symbol": SYMBOL, "orderId": "execution1"}}
    order = stop_api.fetch_order("stop1", PAIR, {"stop": True})
    assert order["info"]["triggeredOrderId"] == "execution1"
    assert order["filled"] == 0  # Generic Exchange resolves actual fills using triggeredOrderId.
    stop_api.client.order_info.return_value = {"data": {}}
    with pytest.raises(ccxt.ExchangeError):
        stop_api.fetch_order("stop1", PAIR, {"stop": True})


def test_cancel_reads_actual_state_and_restart_needs_no_memory(stop_api):
    create(stop_api)
    assert stop_api.cancel_order("stop1", PAIR, {"stop": True})["status"] == "canceled"
    assert stop_api.fetch_order("stop1", PAIR, {"stop": True})["status"] == "canceled"


def test_stop_rejects_spot_and_nonreduce_orders(stop_api):
    with pytest.raises(ccxt.InvalidOrder):
        stop_api.create_order(PAIR, "stop_market", "sell", 1, params={"stopLossPrice": 1455})
    stop_api._is_futures_symbol.return_value = False
    with pytest.raises(ccxt.InvalidOrder):
        create(stop_api)


def test_timeout_after_placement_does_not_duplicate_protection(stop_api):
    request = stop_api.client.request.side_effect

    def timeout_after_post(method, path, **kwargs):
        result = request(method, path, **kwargs)
        if method == "POST":
            raise ccxt.RequestTimeout("Response lost after venue acceptance")
        return result

    stop_api.client.request.side_effect = timeout_after_post
    with pytest.raises(ccxt.RequestTimeout):
        create(stop_api)
    stop_api.client.request.side_effect = request
    assert create(stop_api)["id"] == "stop1"
    writes = [c for c in stop_api.client.request.call_args_list if c.args[0] == "POST"]
    assert len(writes) == 1


def test_wrong_trigger_basis_is_not_adopted(stop_api):
    create(stop_api)
    stop_api._test_rows[0]["tpslOrder"]["stopLossType"] = "mark_price"
    with pytest.raises(ccxt.InvalidOrder):
        create(stop_api)


def test_stop_quantity_does_not_send_binary_float_dust(stop_api):
    create(stop_api)
    template = deepcopy(stop_api._test_rows[0])
    stop_api._test_rows = []
    stop_api.fetch_positions.return_value = [{"side": "long", "contracts": 35}]
    request = stop_api.client.request.side_effect

    def place(method, path, **kwargs):
        if path == "/v2/trade/stopPosition":
            # 35 * 0.01 as a binary float can stringify to 0.35000000000000003.
            assert kwargs["data"]["tpslQty"] == "0.35"
            template["qty"] = "0.35"
            template["tpslOrder"]["tpslClOrdId"] = kwargs["data"]["tpslClOrdId"]
            stop_api._test_rows = [template]
            return {"data": {"orderId": "stop1"}}
        return request(method, path, **kwargs)

    stop_api.client.request.side_effect = place
    order = stop_api.create_order(
        PAIR, "stop_market", "sell", 35, params={"reduceOnly": True, "stopLossPrice": 1455}
    )
    assert order["amount"] == 35


@pytest.mark.parametrize("status,filled", [("open", 0), ("open", 0.5), ("closed", 1)])
def test_native_lookup_uses_execution_status_and_fills(stop_api, status, filled):
    create(stop_api)
    stop_api._test_rows[0]["status"] = "slEffective"
    stop_api.client.order_info.return_value = {"data": {"symbol": SYMBOL, "orderId": "execution1"}}
    exchange = object.__new__(UMX)
    exchange.close = Mock()
    options = {
        "stoploss_query_requires_stop_flag": True,
        "stoploss_algo_order_info_id": "triggeredOrderId",
    }
    exchange.get_option = lambda key: options.get(key)
    execution = {
        "id": "execution1",
        "status": status,
        "filled": filled,
        "amount": 1,
        "remaining": 1 - filled,
    }
    exchange.fetch_order = Mock(
        side_effect=lambda order_id, pair, params: (
            stop_api.fetch_order(order_id, pair, params) if params else deepcopy(execution)
        )
    )
    order = exchange.fetch_stoploss_order("stop1", PAIR)
    assert order["id"] == "stop1" and order["id_stop"] == "execution1"
    assert order["status"] == status and order["filled"] == filled
    assert order["status_stop"] == "triggered"


def test_trigger_racing_with_cancel_is_reconciled(stop_api):
    create(stop_api)
    request = stop_api.client.request.side_effect
    stop_api.client.order_info.return_value = {"data": {"symbol": SYMBOL, "orderId": "execution1"}}

    def trigger_during_cancel(method, path, **kwargs):
        if path == "/v1/trade/cancelComplex":
            stop_api._test_rows[0]["status"] = "slEffective"
            return {"data": {"complexOId": "stop1"}}
        return request(method, path, **kwargs)

    stop_api.client.request.side_effect = trigger_during_cancel
    order = stop_api.cancel_order("stop1", PAIR, {"stop": True})
    assert order["status"] == "closed" and order["filled"] == 0
    assert order["info"]["triggeredOrderId"] == "execution1"
