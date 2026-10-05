from datetime import timedelta
from unittest.mock import Mock

import ccxt
import pytest

from freqtrade.enums import CandleType, MarginMode, TradingMode
from freqtrade.exceptions import OperationalException
from freqtrade.exchange.umx import UMX
from freqtrade.exchange.umx_api import UMXSync
from freqtrade.exchange.umx_connector.client import UMXClient
from freqtrade.util import dt_ts, dt_utc


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


@pytest.fixture
def strict_ohlcv_exchange():
    exchange = object.__new__(UMX)
    exchange.close = Mock()
    exchange._config = {
        "exchange": {"umx_strict_ohlcv": True},
        "candle_type_def": CandleType.FUTURES,
    }
    exchange._klines = {}
    exchange._pairs_last_refresh_time = {}
    exchange._pairs_last_poll_time = {}
    exchange._ohlcv_partial_candle = True
    exchange._ohlcv_late_candle_grace_ms = 15000
    exchange._ohlcv_max_poll_interval_ms = 1800000
    exchange._startup_candle_count = 0
    exchange._ft_has = {"ohlcv_candle_limit": 1000}
    exchange.ohlcv_candle_limit = Mock(return_value=1000)
    return exchange


@pytest.mark.parametrize("cache", [False, True])
def test_strict_ohlcv_retains_closed_last_and_does_not_fill_gaps(strict_ohlcv_exchange, cache):
    exchange = strict_ohlcv_exchange
    start = dt_utc(2026, 1, 1)
    start_ms = dt_ts(start)
    ticks = [[start_ms, 1, 1, 1, 1, 1], [start_ms + 600000, 1, 1, 1, 1, 1]]
    frame = exchange._process_ohlcv_df(
        "ETH/USDT:USDT", "5m", CandleType.FUTURES, ticks, cache, True, start_ms + 916000
    )
    assert frame.date.tolist() == [start, start + timedelta(minutes=10)]

    if cache:
        # The cache merge must also preserve gaps instead of inventing zero-volume candles.
        frame = exchange._process_ohlcv_df(
            "ETH/USDT:USDT",
            "5m",
            CandleType.FUTURES,
            [[start_ms + 1200000, 1, 1, 1, 1, 1]],
            True,
            True,
            start_ms + 1516000,
        )
        assert frame.date.tolist() == [start + timedelta(minutes=m) for m in (0, 10, 20)]
        key = ("ETH/USDT:USDT", "5m", CandleType.FUTURES)
        assert exchange._pairs_last_refresh_time[key] == start_ms + 1200000
        assert exchange._pairs_last_poll_time[key] == start_ms + 1516000


@pytest.mark.parametrize("drop_incomplete", [True, False])
def test_strict_ohlcv_judges_completeness_at_fetch_start(
    strict_ohlcv_exchange, time_machine, drop_incomplete
):
    exchange = strict_ohlcv_exchange
    start = dt_utc(2026, 1, 1)
    start_ms = dt_ts(start)
    # Processing happens after the close and grace period, but the fetched snapshot is partial.
    time_machine.move_to(start + timedelta(minutes=15, seconds=20), tick=False)
    ticks = [[start_ms, 1, 1, 1, 1, 1], [start_ms + 600000, 1, 1, 1, 1, 1]]
    frame = exchange._process_ohlcv_df(
        "ETH/USDT:USDT",
        "5m",
        CandleType.FUTURES,
        ticks,
        False,
        drop_incomplete,
        start_ms + 899000,
    )
    # Strict mode must still reject the incomplete candle when the caller disables dropping.
    assert frame.date.tolist() == [start]


@pytest.mark.parametrize(
    "after_close_ms,expected_rows", [(0, 1), (14999, 1), (15000, 2), (16000, 2)]
)
def test_strict_ohlcv_withholds_just_closed_candle_until_final(
    strict_ohlcv_exchange, after_close_ms, expected_rows
):
    exchange = strict_ohlcv_exchange
    start = dt_utc(2026, 1, 1)
    start_ms = dt_ts(start)
    ticks = [[start_ms, 1, 1, 1, 1, 1], [start_ms + 600000, 1, 1, 1, 1, 1]]
    frame = exchange._process_ohlcv_df(
        "ETH/USDT:USDT",
        "5m",
        CandleType.FUTURES,
        ticks,
        False,
        True,
        start_ms + 900000 + after_close_ms,
    )
    assert frame.date.tolist() == [start, start + timedelta(minutes=10)][:expected_rows]


@pytest.mark.parametrize("drop_incomplete", [None, True, False])
@pytest.mark.parametrize("cache", [False, True])
def test_strict_ohlcv_never_drops_funding_rates(strict_ohlcv_exchange, drop_incomplete, cache):
    exchange = strict_ohlcv_exchange
    start = dt_utc(2026, 1, 1)
    start_ms = dt_ts(start)
    candle_type = CandleType.FUNDING_RATE
    drop_hint = exchange._drop_incomplete_candle(candle_type, drop_incomplete)
    assert drop_hint is False
    # A settled rate is final even though its timestamp is in the currently forming interval.
    frame = exchange._process_ohlcv_df(
        "ETH/USDT:USDT",
        "1h",
        candle_type,
        [[start_ms, 0.0001], [start_ms + 3600000, -0.0002]],
        cache,
        drop_hint,
        start_ms + 5400000,
    )
    assert frame.date.tolist() == [start, start + timedelta(hours=1)]
    assert frame.funding_rate.tolist() == pytest.approx([0.0001, -0.0002])
    assert frame.open.equals(frame.funding_rate)


def test_strict_ohlcv_single_forming_candle_records_poll_without_caching_it(
    strict_ohlcv_exchange, time_machine
):
    exchange = strict_ohlcv_exchange
    start = dt_utc(2026, 1, 1)
    start_ms = dt_ts(start)
    fetch_start_ms = start_ms + 610000
    time_machine.move_to(start + timedelta(minutes=10, seconds=10), tick=False)
    key = ("ETH/USDT:USDT", "5m", CandleType.FUTURES)
    frame = exchange._process_ohlcv_df(
        *key, [[start_ms + 600000, 1, 1, 1, 1, 1]], True, True, fetch_start_ms
    )
    assert frame.empty
    assert exchange._klines[key].empty
    assert exchange._pairs_last_poll_time[key] == fetch_start_ms
    assert exchange._pairs_last_refresh_time[key] == start_ms + 300000
    assert exchange._now_is_time_to_refresh(*key) is False

    time_machine.move_to(start + timedelta(minutes=15), tick=False)
    assert exchange._now_is_time_to_refresh(*key) is True


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
