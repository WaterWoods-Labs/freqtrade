from unittest.mock import Mock

import ccxt
import pytest

from freqtrade.enums import CandleType, MarginMode, TradingMode
from freqtrade.exceptions import OperationalException
from freqtrade.exchange.umx import UMX
from freqtrade.exchange.umx_api import UMXSync
from freqtrade.exchange.umx_connector.client import UMXClient


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_po_transport_and_no_market_fallback(side):
    exchange = object.__new__(UMX)
    exchange.close = Mock()
    exchange._params = {}
    params = exchange._get_params(side, "limit", 1, False, "PO")
    api = object.__new__(UMXSync)
    api._is_futures_symbol = Mock(return_value=True)
    api._contract_size = Mock(return_value=0.001)
    api._market = Mock(return_value={"info": {"quantityPrecision": 6}})
    api.market_id = Mock(return_value="ETH-USDT-PERP")
    api.fetch_leverage = Mock(return_value={"longLeverage": 1})
    api._parse_order = Mock(return_value={})
    api.client = Mock()
    api.client.place_order.return_value = {"ts": 1, "data": {}}
    api.create_order("ETH/USDT:USDT", "limit", side, 5, 2000, params)
    body = api.client.place_order.call_args.args[0]
    assert body["orderType"] == "post_only"
    assert body["timeInForce"] == "gtc"
    assert float(body["qty"]) == 0.005
    api.client.place_order.side_effect = ccxt.InvalidOrder("post-only would cross")
    with pytest.raises(ccxt.InvalidOrder):
        api.create_order("ETH/USDT:USDT", "limit", side, 5, 2000, params)
    assert api.client.place_order.call_count == 2
    for kind, extra in [("market", params), ("limit", {**params, "timeInForce": "IOC"})]:
        with pytest.raises(ccxt.InvalidOrder):
            api.create_order("ETH/USDT:USDT", kind, side, 5, 2000, extra)
    assert api.client.place_order.call_count == 2
    api.client.place_order.side_effect = None
    api.create_order("ETH/USDT:USDT", "market", side, 5, None, {"reduceOnly": True})
    assert api.client.place_order.call_args.args[0]["reduceOnly"] is True
    assert api.client.place_order.call_args.args[0]["orderType"] == "market"


@pytest.mark.parametrize("actual", [1, 2, None])
def test_readonly_leverage_never_sets(actual):
    exchange = object.__new__(UMX)
    exchange.close = Mock()
    exchange._config = {"dry_run": False, "exchange": {"umx_leverage_readonly": True}}
    exchange._api = Mock()
    exchange._api.fetch_leverage.return_value = {"longLeverage": actual, "shortLeverage": actual}
    exchange._set_leverage = Mock(side_effect=AssertionError("no leverage writes"))
    if actual == 1:
        exchange._lev_prep("ETH/USDT:USDT", 1, "buy")
    else:
        with pytest.raises(OperationalException):
            exchange._lev_prep("ETH/USDT:USDT", 1, "buy")
    exchange._set_leverage.assert_not_called()


def test_strict_ohlcv_retains_closed_last_and_does_not_fill_gaps(mocker):
    exchange = object.__new__(UMX)
    exchange.close = Mock()
    exchange._config = {"exchange": {"umx_strict_ohlcv": True}}
    exchange._klines = {}
    exchange._pairs_last_refresh_time = {}
    exchange._ft_has = {"ohlcv_candle_limit": 1000}
    exchange.ohlcv_candle_limit = Mock(return_value=1000)
    mocker.patch("freqtrade.exchange.exchange.dt_ts", return_value=900000)
    ticks = [[0, 1, 1, 1, 1, 1], [600000, 1, 1, 1, 1, 1]]
    frame = exchange._process_ohlcv_df(
        "ETH/USDT:USDT", "5m", CandleType.FUTURES, ticks, False, True
    )
    assert len(frame) == 2
    assert frame.date.iloc[-1].value // 1000000 == 600000


def test_public_pacing_shared_and_private_not_queued(mocker):
    client = UMXClient({"umx_public_request_interval": 0.2})
    client._http.request = Mock(return_value=Mock(status_code=200))
    client._http.request.return_value.json.return_value = {"code": "0", "data": []}
    clock = mocker.patch("freqtrade.exchange.umx_connector.client.time.monotonic", return_value=10)
    sleep = mocker.patch("freqtrade.exchange.umx_connector.client.time.sleep")
    mocker.patch.object(UMXClient, "_public_next_request", 10.1)
    client.request("GET", "/v1/market/kline")
    assert sleep.call_count == 1
    client.api_key, client.api_secret = "test", "test"
    sleep.reset_mock()
    clock.reset_mock()
    client.request("GET", "/v2/trade/order/info", private=True)
    sleep.assert_not_called()
    clock.assert_not_called()


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_readonly_deployment_blocks_writes_before_http(method):
    client = UMXClient({"umx_order_read_only": True})
    client._http.request = Mock(side_effect=AssertionError("Must never send a write"))
    with pytest.raises(ccxt.PermissionDenied, match="read-only"):
        client.request(method, "/v2/trade/order", private=True, data={})
    client._http.request.assert_not_called()


def test_readonly_deployment_allows_authenticated_reads():
    client = UMXClient({"umx_order_read_only": True})
    client.api_key, client.api_secret = "fixture", "fixture"
    client._http.request = Mock(return_value=Mock(status_code=200))
    client._http.request.return_value.json.return_value = {"code": "0", "data": []}
    assert client.request("GET", "/v2/trade/order/info", private=True)["data"] == []
    client._http.request.assert_called_once()


@pytest.mark.parametrize("sync", [True, False])
def test_unapproved_maker_forces_readonly_after_generic_client_overrides(sync):
    exchange = object.__new__(UMX)
    exchange.close = Mock()
    exchange.trading_mode = TradingMode.FUTURES
    exchange.margin_mode = MarginMode.CROSS
    exchange._ft_has = {"funding_fee_timeframe": "8h"}
    exchange._config = {"strategy": "UMXChanB1S1Maker", "dry_run": True}
    api = exchange._init_ccxt({"umx_order_read_only": False}, sync, {"umx_order_read_only": False})
    assert api.client.order_read_only is True
    api.client.close()
