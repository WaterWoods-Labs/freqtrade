import hmac
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace

import ccxt
import pytest

from freqtrade.configuration.config_validation import validate_config_schema
from freqtrade.data.dataprovider import DataProvider
from freqtrade.enums import CandleType, RunMode
from freqtrade.exceptions import ConfigurationError, OperationalException
from freqtrade.exchange import UMX, available_exchanges, ccxt_exchanges, list_available_exchanges
from freqtrade.exchange.check_exchange import check_exchange
from freqtrade.exchange.umx_api import UMXSync
from freqtrade.exchange.umx_connector import (
    UMX_DEFAULT_BASE_URL,
    UMXClient,
    ccxt_symbol_to_umx,
    umx_symbol_to_ccxt,
)
from freqtrade.resolvers.exchange_resolver import ExchangeResolver
from freqtrade.wallets import PositionWallet, Wallets


def _umx_config(default_conf: dict, *, dry_run: bool = True) -> dict:
    conf = deepcopy(default_conf)
    conf.update(
        {
            "dry_run": dry_run,
            "stake_currency": "USDT",
            "stake_amount": 25,
            "timeframe": "5m",
            "trading_mode": "spot",
            "margin_mode": "",
        }
    )
    conf["exchange"].update(
        {
            "name": "umx",
            "pair_whitelist": ["BTC/USDT"],
            "pair_blacklist": [],
            "umx_live_trading_enabled": not dry_run,
        }
    )
    return conf


def test_umx_product_rejects_removed_exchange_risk_policy(default_conf) -> None:
    conf = _umx_config(default_conf)
    removed_risk_key = "portfolio_" + "margin_risk"
    conf["exchange"][removed_risk_key] = {"pair": "BTC/USDT"}

    with pytest.raises(
        ConfigurationError,
        match="UMX-only product does not support exchange account-routing extensions",
    ):
        validate_config_schema(conf)


@pytest.mark.parametrize("config_key", ["ccxt_config", "ccxt_async_config"])
def test_umx_product_rejects_removed_account_mode(default_conf, config_key) -> None:
    conf = _umx_config(default_conf)
    removed_account_mode = "portfolio" + "Margin"
    conf["exchange"][config_key] = {"options": {removed_account_mode: True}}

    with pytest.raises(
        ConfigurationError,
        match="UMX-only product does not support exchange account-routing extensions",
    ):
        validate_config_schema(conf)


def test_removed_xcoin_selector_fails_explicitly(default_conf) -> None:
    conf = _umx_config(default_conf)
    conf["exchange"]["name"] = "xcoin"

    with pytest.raises(ConfigurationError, match=r"set `exchange\.name` to `umx`"):
        validate_config_schema(conf)
    with pytest.raises(OperationalException, match=r"set `exchange\.name` to `umx`"):
        ExchangeResolver.load_exchange(conf, validate=False)


@pytest.mark.parametrize(
    "old_key",
    ["xcoin_live_trading_enabled", "xcoin_base_url", "xcoin_timeout", "xcoin_future_option"],
)
def test_removed_xcoin_config_keys_fail_explicitly(default_conf, old_key) -> None:
    conf = _umx_config(default_conf)
    conf["exchange"][old_key] = True

    with pytest.raises(ConfigurationError, match=old_key):
        validate_config_schema(conf)


def _umx_response(method, path, params=None, data=None, private=False):
    if path == "/v2/public/symbols":
        return {
            "code": "0",
            "msg": "Success",
            "data": [
                {
                    "businessType": "spot",
                    "symbol": "BTC-USDT",
                    "symbolFamily": "BTC-USDT",
                    "quoteCurrency": "USDT",
                    "baseCurrency": "BTC",
                    "settleCurrency": "USDT",
                    "ctVal": "0",
                    "tickSize": "0.1",
                    "status": "trading",
                    "pricePrecision": "1",
                    "quantityPrecision": "5",
                    "orderParameters": {
                        "minOrderQty": "0.00001",
                        "minOrderAmt": "5",
                        "maxOrderNum": "500",
                        "maxLmtOrderQty": None,
                        "maxMktOrderQty": None,
                        "maxLmtOrderAmt": "2000000",
                        "maxMktOrderAmt": "100000",
                    },
                    "priceParameters": {
                        "maxLmtPriceUp": "0.03",
                        "minLmtPriceDown": "0.03",
                    },
                    "positionParameters": None,
                    "group": ["0.1", "1", "10", "100"],
                }
            ],
            "ts": "1732193257273",
        }
    if path == "/v1/market/ticker/mini":
        return {
            "code": "0",
            "msg": "Success",
            "data": [
                {
                    "businessType": "spot",
                    "symbol": "BTC-USDT",
                    "priceChange": "10",
                    "priceChangePercent": "0.01",
                    "lastPrice": "80000",
                    "fillQty": "1.2",
                    "fillAmount": "96000",
                    "baseCurrency": "BTC",
                }
            ],
            "ts": "1732193257273",
        }
    if path == "/v1/market/depth":
        return {
            "code": "0",
            "msg": "Success",
            "data": {
                "bids": [["79999.99", "0.5"]],
                "asks": [["80000.01", "0.4"]],
                "lastUpdateId": "5001",
            },
            "ts": "1732193257273",
        }
    if path == "/v1/market/kline":
        return {
            "code": "0",
            "msg": "Success",
            "data": [
                [
                    "5m",
                    "1732193400000",
                    "1732193699999",
                    "80100",
                    "80200",
                    "80300",
                    "80000",
                    "2.0",
                    "160400",
                    "10",
                    "100",
                    "0.001",
                ],
                [
                    "5m",
                    "1732193100000",
                    "1732193399999",
                    "80000",
                    "80100",
                    "80200",
                    "79900",
                    "1.5",
                    "120150",
                    "8",
                    "100",
                    "0.001",
                ],
            ],
            "ts": "1732193700000",
        }

    assert private
    if path == "/v1/account/balance":
        return {
            "code": "0",
            "msg": "Success",
            "data": {
                # Spot responses may expose account-level futures totals too. They must not
                # replace the per-currency spot balances below.
                "totalEquity": "999",
                "totalAvailableBalance": "888",
                "details": [
                    {
                        "currency": "USDT",
                        "equity": "100",
                        "totalBalance": "100",
                        "cashBalance": "80",
                        "savingBalance": "0",
                        "frozen": "20",
                    },
                    {
                        "currency": "BTC",
                        "equity": "0.5",
                        "totalBalance": "0.5",
                        "cashBalance": "0.4",
                        "savingBalance": "0",
                        "frozen": "0.1",
                    },
                ],
            },
            "ts": "1732193257273",
        }
    if path == "/v2/trade/order":
        assert method == "POST"
        assert data["symbol"] == "BTC-USDT"
        assert data["side"] == "buy"
        assert data["marketUnit"] == "baseCoin"
        assert data["isLeverage"] is False
        if data["orderType"] == "limit":
            assert float(data["price"]) == 80000
            assert data["timeInForce"] == "gtc"
        elif data["orderType"] == "market":
            assert "price" not in data
            assert data["timeInForce"] == "ioc"
        else:
            raise AssertionError(f"Unexpected UMX order type: {data['orderType']}")
        return {
            "code": "0",
            "msg": "Success",
            "data": {"orderId": "1322590062927904769", "clientOrderId": "Client1001"},
            "ts": "1732193257273",
        }
    if path == "/v1/trade/cancelOrder":
        assert method == "POST"
        assert data["orderId"] == "1322590062927904769"
        return {
            "code": "0",
            "msg": "Success",
            "data": {"orderId": "1322590062927904769"},
            "ts": "1732193257273",
        }
    if path == "/v2/trade/order/info":
        return {
            "code": "0",
            "msg": "Success",
            "data": {
                "id": "1322590062927904769",
                "businessType": "spot",
                "symbol": "BTC-USDT",
                "orderId": "1322590062927904769",
                "clientOrderId": "Client1001",
                "price": "80000",
                "qty": "0.01",
                "quoteQty": "400",
                "orderType": "limit",
                "side": "buy",
                "totalFillQty": "0.005",
                "avgPrice": "80000",
                "status": "partially_filled",
                "baseFee": "0",
                "quoteFee": "-0.4",
                "timeInForce": "gtc",
                "createTime": "1732193257000",
                "updateTime": "1732193257273",
            },
            "ts": "1732193257273",
        }
    if path == "/v2/trade/openOrders":
        return {
            "code": "0",
            "msg": "Success",
            "data": [
                {
                    "id": "1322590062927904769",
                    "businessType": "spot",
                    "symbol": "BTC-USDT",
                    "orderId": "1322590062927904769",
                    "clientOrderId": "Client1001",
                    "price": "80000",
                    "qty": "0.01",
                    "quoteQty": "400",
                    "orderType": "limit",
                    "side": "buy",
                    "totalFillQty": "0.005",
                    "avgPrice": "80000",
                    "status": "partially_filled",
                    "timeInForce": "gtc",
                    "createTime": "1732193257000",
                    "updateTime": "1732193257273",
                }
            ],
            "ts": "1732193257273",
        }
    raise AssertionError(f"Unhandled UMX mock path: {method} {path}")


def _patch_umx_request(mocker):
    calls = []

    def fake_request(self, method, path, *, params=None, data=None, private=False):
        calls.append((method, path, params, data, private))
        return _umx_response(method, path, params=params, data=data, private=private)

    mocker.patch.object(UMXClient, "request", fake_request)
    return calls


def test_umx_client_signing_uses_document_shape():
    client = UMXClient({"secret": "test-secret"})
    timestamp = "1756377216596"
    path = "/v2/trade/order"
    query = client._query({"symbol": "BTC-USDT", "businessType": "spot"})
    body = client._body(
        {
            "symbol": "BTC-USDT",
            "side": "buy",
            "orderType": "limit",
            "qty": "0.02",
            "price": "90000",
            "marketUnit": "baseCoin",
            "timeInForce": "gtc",
            "ignored": None,
        }
    )

    assert query == "?businessType=spot&symbol=BTC-USDT"
    assert body == (
        '{"symbol":"BTC-USDT","side":"buy","orderType":"limit","qty":"0.02",'
        '"price":"90000","marketUnit":"baseCoin","timeInForce":"gtc"}'
    )
    expected = hmac.new(
        b"test-secret",
        f"{timestamp}POST{path}{query}{body}".encode(),
        sha256,
    ).hexdigest()

    assert client._sign(timestamp, "post", path, query, body) == expected


def test_umx_client_uses_only_canonical_host():
    assert UMXClient().base_url == UMX_DEFAULT_BASE_URL
    with pytest.raises(ccxt.BadRequest, match="custom base URLs are disabled"):
        UMXClient({"base_url": "https://api.xcoin.com/api"})


@pytest.mark.parametrize(
    ("code", "exception"),
    [
        ("10008", ccxt.DDoSProtection),
        ("14001", ccxt.DDoSProtection),
        ("10112", ccxt.AuthenticationError),
        ("40013", ccxt.OrderNotFound),
        ("50006", ccxt.InvalidOrder),
        ("60101", ccxt.InsufficientFunds),
        ("20001", ccxt.ExchangeNotAvailable),
        ("60117", ccxt.InvalidOrder),
    ],
)
def test_umx_client_error_mapping(code, exception):
    with pytest.raises(exception):
        UMXClient()._handle_response({"code": code, "msg": "boom"})


@pytest.mark.parametrize(
    ("status", "content_type", "body", "exception"),
    [
        (429, "application/json", '{"code":"10008"}', ccxt.DDoSProtection),
        (403, "text/html", "<html>WAF access too frequent</html>", ccxt.DDoSProtection),
        (403, "application/json", '{"code":"10114"}', ccxt.AuthenticationError),
        (401, "application/json", '{"code":"10101"}', ccxt.AuthenticationError),
    ],
)
def test_umx_http_protection_mapping(mocker, status, content_type, body, exception):
    client = UMXClient({"apiKey": "key", "secret": "secret"})
    response = SimpleNamespace(
        status_code=status,
        text=body,
        headers={"Content-Type": content_type},
        json=lambda: {"code": "0"},
    )
    request = mocker.patch.object(client._http, "request", return_value=response)

    with pytest.raises(exception):
        client.request("GET", "/v1/account/balance", private=True)
    request.assert_called_once()


def test_umx_place_order_is_not_retried_after_throttling(mocker):
    client = UMXClient({"apiKey": "key", "secret": "secret"})
    response = SimpleNamespace(
        status_code=429,
        text="too many requests",
        headers={"Content-Type": "text/plain"},
    )
    request = mocker.patch.object(client._http, "request", return_value=response)

    with pytest.raises(ccxt.DDoSProtection):
        client.place_order({"symbol": "BTC-USDT", "qty": "1"})
    request.assert_called_once()


def test_check_exchange_accepts_native_umx(default_conf):
    conf = _umx_config(default_conf)
    conf["runmode"] = RunMode.DRY_RUN
    assert check_exchange(conf)


def test_umx_resolver_loads_native_exchange(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    _patch_umx_request(mocker)

    exchange = ExchangeResolver.load_exchange(_umx_config(default_conf))

    assert isinstance(exchange, UMX)
    assert exchange.name == "UMX"
    assert exchange.markets["BTC/USDT"]["id"] == "BTC-USDT"
    assert exchange.markets["BTC/USDT"]["limits"]["amount"]["min"] == 0.00001


def test_umx_credentials_are_read_from_environment(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    _patch_umx_request(mocker)
    conf = _umx_config(default_conf)
    conf["exchange"]["key"] = "config-key"
    conf["exchange"]["secret"] = "config-secret"
    conf["exchange"]["ccxt_config"] = {"apiKey": "ccxt-key", "secret": "ccxt-secret"}

    exchange = ExchangeResolver.load_exchange(conf)

    assert exchange._api.apiKey == "env-key"
    assert exchange._api.secret == "env-secret"


def test_removed_xcoin_environment_credentials_are_not_aliased(default_conf, mocker, monkeypatch):
    monkeypatch.delenv("FREQTRADE__EXCHANGE__KEY", raising=False)
    monkeypatch.delenv("FREQTRADE__EXCHANGE__SECRET", raising=False)
    monkeypatch.delenv("UMX_API_KEY", raising=False)
    monkeypatch.delenv("UMX_API_SECRET", raising=False)
    monkeypatch.setenv("XCOIN_API_KEY", "legacy-key")
    monkeypatch.setenv("XCOIN_API_SECRET", "legacy-secret")
    _patch_umx_request(mocker)

    with pytest.raises(OperationalException, match="UMX live trading requires API credentials"):
        ExchangeResolver.load_exchange(_umx_config(default_conf, dry_run=False))


def test_umx_public_market_data(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    _patch_umx_request(mocker)
    exchange = ExchangeResolver.load_exchange(_umx_config(default_conf))

    ticker = exchange.fetch_ticker("BTC/USDT")
    assert ticker["last"] == 80000
    assert ticker["bid"] == 79999.99
    assert ticker["ask"] == 80000.01

    # /ticker/mini has no best bid/ask. Batch results keep them missing instead of
    # fabricating a zero spread from lastPrice, so SpreadFilter is disabled for UMX.
    tickers = exchange.get_tickers()
    assert exchange.get_option("tickers_have_bid_ask") is False
    assert tickers["BTC/USDT"]["last"] == 80000
    assert tickers["BTC/USDT"]["bid"] is None
    assert tickers["BTC/USDT"]["ask"] is None

    candles = exchange.refresh_latest_ohlcv(
        [("BTC/USDT", "5m", CandleType.SPOT)], cache=False, drop_incomplete=False
    )
    candle_df = candles[("BTC/USDT", "5m", CandleType.SPOT)]
    assert len(candle_df) == 2
    assert candle_df.iloc[0]["open"] == 80000
    assert candle_df.iloc[1]["close"] == 80200


def test_umx_private_account_and_order_methods(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    _patch_umx_request(mocker)
    exchange = ExchangeResolver.load_exchange(_umx_config(default_conf, dry_run=False))

    balances = exchange.get_balances()
    assert balances["USDT"] == {"free": 80.0, "used": 20.0, "total": 100.0}

    order = exchange.create_order(
        pair="BTC/USDT",
        ordertype="limit",
        side="buy",
        amount=0.01,
        rate=80000,
        leverage=1,
    )
    assert order["id"] == "1322590062927904769"
    assert order["status"] == "open"
    assert order["type"] == "limit"

    fetched = exchange.fetch_order("1322590062927904769", "BTC/USDT")
    assert fetched["status"] == "open"
    assert fetched["filled"] == 0.005
    assert fetched["remaining"] == 0.005
    assert fetched["fee"] == {"currency": "USDT", "cost": 0.4, "rate": None}

    rebate = exchange._api._parse_order_fee({"quoteFee": "0.4"}, "BTC/USDT")
    assert rebate == {"currency": "USDT", "cost": -0.4, "rate": None}

    canceled = exchange.cancel_order("1322590062927904769", "BTC/USDT")
    assert canceled["status"] == "canceled"

    open_orders = exchange._api.fetch_open_orders("BTC/USDT")
    assert len(open_orders) == 1
    assert open_orders[0]["status"] == "open"


def test_umx_cancel_order_uses_local_timestamp_when_response_omits_ts(
    default_conf, mocker, monkeypatch
):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    _patch_umx_request(mocker)
    exchange = ExchangeResolver.load_exchange(_umx_config(default_conf, dry_run=False))
    mocker.patch.object(
        exchange._api.client,
        "cancel_order",
        return_value={"code": "0", "msg": "Success", "data": {"orderId": "42"}},
    )
    mocker.patch.object(exchange._api, "milliseconds", return_value=1769133528123)

    canceled = exchange._api.cancel_order("42", "BTC/USDT")

    assert canceled["timestamp"] == 1769133528123
    assert canceled["datetime"] == "2026-01-23T01:58:48.123Z"


def test_umx_spot_balance_does_not_replace_details_with_total_equity(
    default_conf, mocker, monkeypatch
):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    _patch_umx_request(mocker)
    exchange = ExchangeResolver.load_exchange(_umx_config(default_conf, dry_run=False))

    balances = exchange.get_balances()

    assert balances["USDT"] == {"free": 80.0, "used": 20.0, "total": 100.0}


@pytest.mark.parametrize("wire_status", ["untrigger", "untriggered"])
def test_umx_parses_current_and_legacy_untrigger_status(wire_status):
    order = UMXSync()._parse_order(
        {"orderId": "42", "symbol": "BTC-USDT", "status": wire_status, "createTime": "1"}
    )
    assert order["status"] == "open"


def test_umx_private_market_order(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    calls = _patch_umx_request(mocker)
    exchange = ExchangeResolver.load_exchange(_umx_config(default_conf, dry_run=False))

    order = exchange.create_order(
        pair="BTC/USDT",
        ordertype="market",
        side="buy",
        amount=0.01,
        rate=80000,
        leverage=1,
    )

    assert order["id"] == "1322590062927904769"
    assert order["status"] == "open"
    assert order["type"] == "market"
    live_order_calls = [call for call in calls if call[1] == "/v2/trade/order"]
    assert live_order_calls[-1][3]["orderType"] == "market"
    assert live_order_calls[-1][3]["timeInForce"] == "ioc"


def test_umx_dry_run_order_does_not_call_live_order(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    calls = _patch_umx_request(mocker)
    exchange = ExchangeResolver.load_exchange(_umx_config(default_conf))

    order = exchange.create_order(
        pair="BTC/USDT",
        ordertype="limit",
        side="buy",
        amount=0.01,
        rate=70000,
        leverage=1,
    )

    assert order["id"].startswith("dry_run_buy_BTC/USDT")
    assert all(call[1] != "/v2/trade/order" for call in calls)


def test_umx_live_requires_explicit_enable(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    _patch_umx_request(mocker)
    conf = _umx_config(default_conf, dry_run=False)
    conf["exchange"]["umx_live_trading_enabled"] = False

    with pytest.raises(Exception, match="UMX live trading is disabled"):
        ExchangeResolver.load_exchange(conf)


# ---------------------------------------------------------------------------
# Linear perpetual (U-margined futures) support
# ---------------------------------------------------------------------------


def _umx_futures_config(default_conf: dict, *, dry_run: bool = True) -> dict:
    conf = deepcopy(default_conf)
    conf.update(
        {
            "dry_run": dry_run,
            "stake_currency": "USDT",
            "stake_amount": 25,
            "timeframe": "5m",
            "trading_mode": "futures",
            "margin_mode": "cross",
        }
    )
    conf["exchange"].update(
        {
            "name": "umx",
            "pair_whitelist": ["BTC/USDT:USDT"],
            "pair_blacklist": [],
            "umx_live_trading_enabled": not dry_run,
        }
    )
    return conf


def _umx_futures_response(method, path, params=None, data=None, private=False):  # noqa: C901
    params = params or {}
    if path == "/v2/public/symbols":
        assert params.get("businessType") == "linear_perpetual"
        return {
            "code": "0",
            "msg": "Success",
            "data": [
                {
                    "businessType": "linear_perpetual",
                    "symbol": "BTC-USDT-PERP",
                    "symbolFamily": "BTC-USDT",
                    "quoteCurrency": "USDT",
                    "baseCurrency": "BTC",
                    "settleCurrency": "USDT",
                    "ctVal": "0.0001",
                    "tickSize": "0.1",
                    "status": "trading",
                    "pricePrecision": "1",
                    "quantityPrecision": "4",
                    "riskEngineRate": "0.02",
                    "maxLeverage": "75",
                    "orderParameters": {
                        "minOrderQty": "0.0001",
                        "minOrderAmt": None,
                        "maxLmtOrderQty": "50",
                        "maxMktOrderQty": "1",
                        "maxLmtOrderAmt": None,
                        "maxMktOrderAmt": None,
                    },
                }
            ],
            "ts": "1769133527828",
        }
    if path == "/v1/market/ticker/mini":
        assert params.get("businessType") == "linear_perpetual"
        return {
            "code": "0",
            "msg": "Success",
            "data": [
                {
                    "businessType": "linear_perpetual",
                    "symbol": "BTC-USDT-PERP",
                    "lastPrice": "90000",
                    "priceChange": "10",
                    "priceChangePercent": "0.01",
                    "fillQty": "1.2",
                    "fillAmount": "108000",
                    "baseCurrency": "BTC",
                }
            ],
            "ts": "1769133527828",
        }
    if path == "/v1/market/depth":
        assert params.get("businessType") == "linear_perpetual"
        return {
            "code": "0",
            "msg": "Success",
            "data": {
                "bids": [["89999.9", "0.5"]],
                "asks": [["90000.1", "0.4"]],
                "lastUpdateId": "5001",
            },
            "ts": "1769133527828",
        }
    if path == "/v1/market/kline":
        assert params.get("businessType") == "linear_perpetual"
        return {
            "code": "0",
            "msg": "Success",
            "data": [
                [
                    "5m",
                    "1769131800000",
                    "1769132099999",
                    "89600",
                    "89700",
                    "89800",
                    "89500",
                    "444.6",
                    "39895537",
                    "238",
                    "100",
                    "0.001",
                ],
            ],
            "ts": "1769133527828",
        }
    if path == "/v1/market/markPriceKline":
        assert params.get("businessType") == "linear_perpetual"
        assert params.get("symbol") == "BTC-USDT-PERP"
        return {
            "code": "0",
            "msg": "Success",
            "data": [
                ["5m", "1769131800000", "1769132099999", "89610", "89720", "89810", "89510"],
            ],
            "ts": "1769133527828",
        }
    if path == "/v1/market/indexPriceKline":
        assert params.get("symbolFamily") == "BTC-USDT"
        return {
            "code": "0",
            "msg": "Success",
            "data": [
                ["5m", "1769131800000", "1769132099999", "89620", "89730", "89820", "89520"],
            ],
            "ts": "1769133527828",
        }
    if path == "/v1/market/fundingRate/history":
        assert params.get("symbol") == "BTC-USDT-PERP"
        return {
            "code": "0",
            "msg": "Success",
            "data": [
                {
                    "symbol": "BTC-USDT-PERP",
                    "fundingRate": "0.0001",
                    "fundingTime": "1769040000000",
                    "markPrice": "90000",
                },
                {
                    "symbol": "BTC-USDT-PERP",
                    "fundingRate": "-0.0002",
                    "fundingTime": "1769011200000",
                    "markPrice": "89500",
                },
            ],
            "ts": "1769133527828",
        }
    if path == "/v1/market/fundingRate":
        assert params.get("symbol") == "BTC-USDT-PERP"
        return {
            "code": "0",
            "msg": "Success",
            "data": [
                {
                    "symbol": "BTC-USDT-PERP",
                    "fundingRate": "0.0005",
                    "fundingTime": "1769040000000",
                    "fundingInterval": "4",
                    "upperFundingRate": "0.00375",
                    "lowerFundingRate": "-0.00375",
                }
            ],
            "ts": "1769039900000",
        }

    assert private
    if path == "/v1/account/balance":
        return {
            "code": "0",
            "msg": "Success",
            "data": {
                # UMX equity includes the open position's 0.662 USDT unrealized PnL.
                "totalEquity": "1000.662",
                "totalMarginBalance": "1000.662",
                "totalAvailableBalance": "800",
                "details": [
                    {
                        "currency": "USDT",
                        "equity": "1000",
                        "totalBalance": "0",
                        "cashBalance": "0",
                        "frozen": "0",
                    },
                ],
            },
            "ts": "1769133527828",
        }
    if path == "/v2/trade/order":
        assert method == "POST"
        assert data["symbol"] == "BTC-USDT-PERP"
        # isLeverage is UMX's leveraged-spot switch. Perpetual routing is carried by
        # the contract symbol and must not inherit the spot-only request field.
        assert "isLeverage" not in data
        return {
            "code": "0",
            "msg": "Success",
            "data": {"orderId": "1322590060595871744", "clientOrderId": "ftperp1"},
            "ts": "1769133527828",
        }
    if path == "/v2/trade/order/info":
        return {
            "code": "0",
            "msg": "Success",
            "data": {
                "id": "1322590060595871744",
                "businessType": "linear_perpetual",
                "symbol": "BTC-USDT-PERP",
                "orderId": "1322590060595871744",
                "clientOrderId": "ftperp1",
                "price": "90000",
                "qty": "0.001",
                "quoteQty": "90",
                "orderType": "limit",
                "side": "buy",
                "totalFillQty": "0.001",
                "avgPrice": "90000",
                "status": "filled",
                "baseFee": "0",
                "quoteFee": "0",
                "createTime": "1769133527000",
                "updateTime": "1769133528000",
            },
            "ts": "1769133529000",
        }
    if path == "/v1/trade/lever" and method == "GET":
        assert params["symbol"] == "BTC-USDT-PERP"
        assert "currency" not in params
        return {
            "code": "0",
            "msg": "Success",
            "data": {"symbol": params["symbol"], "currency": "", "lever": "1"},
            "ts": "1769133527828",
        }
    if path == "/v1/trade/lever" and method == "POST":
        return {
            "code": "0",
            "msg": "Success",
            "data": {"symbol": data.get("symbol"), "lever": data.get("lever")},
            "ts": "1769133527828",
        }
    if path == "/v2/history/trades":
        assert method == "GET"
        assert params["businessType"] == "linear_perpetual"
        assert params["symbol"] == "BTC-USDT-PERP"
        return {
            "code": "0",
            "msg": "Success",
            "data": [
                {
                    "id": "186",
                    "orderId": "1322590060595871744",
                    "businessType": "linear_perpetual",
                    "symbol": "BTC-USDT-PERP",
                    "orderType": "limit",
                    "side": "buy",
                    "fillPrice": "90000",
                    "tradeId": "186",
                    "role": "maker",
                    "fillQty": "0.001",
                    "fillTime": "1769133528000",
                    "lever": "1",
                    "feeCurrency": "USDT",
                    "fee": "0.009",
                }
            ],
            "ts": "1769133529000",
        }
    if path == "/v1/history/bill":
        assert method == "GET"
        assert params["businessType"] == "linear_perpetual"
        assert params["actionType"] == "18"
        return {
            "code": "0",
            "msg": "Success",
            "data": [
                {
                    "id": "7002",
                    "businessType": "linear_perpetual",
                    "symbol": "BTC-USDT-PERP",
                    "actionType": "18",
                    "currency": "USDT",
                    "qty": "0.25",
                    "actionId": "7002",
                    "lever": "1",
                    "createTime": "1769133528000",
                },
                {
                    "id": "7001",
                    "businessType": "linear_perpetual",
                    "symbol": "BTC-USDT-PERP",
                    "actionType": "18",
                    "currency": "USDT",
                    "qty": "-0.4",
                    "actionId": "7001",
                    "lever": "1",
                    "createTime": "1769011200000",
                },
            ],
            "ts": "1769133529000",
        }
    if path == "/v2/trade/positions":
        return {
            "code": "0",
            "msg": "Success",
            "data": [
                {
                    "positionId": "15762598695810131",
                    "businessType": "linear_perpetual",
                    "symbol": "BTC-USDT-PERP",
                    "positionQty": "-2",
                    "avgPrice": "93921.6",
                    "upl": "0.662",
                    "lever": 2,
                    "liquidationPrice": "140063.17",
                    "markPrice": "93888.5",
                    "im": "1877.77",
                    "mm": "0.02",
                    "indexPrice": "93877.2",
                }
            ],
            "ts": "1769133527828",
        }
    raise AssertionError(f"Unhandled UMX futures mock path: {method} {path}")


def _patch_umx_futures_request(mocker):
    calls = []

    def fake_request(self, method, path, *, params=None, data=None, private=False):
        calls.append((method, path, params, data, private))
        return _umx_futures_response(method, path, params=params, data=data, private=private)

    mocker.patch.object(UMXClient, "request", fake_request)
    return calls


def test_umx_symbol_conversion_handles_perp():
    assert umx_symbol_to_ccxt("BTC-USDT-PERP") == "BTC/USDT:USDT"
    assert ccxt_symbol_to_umx("BTC/USDT:USDT") == "BTC-USDT-PERP"
    # Spot conversions stay intact.
    assert umx_symbol_to_ccxt("BTC-USDT") == "BTC/USDT"
    assert ccxt_symbol_to_umx("BTC/USDT") == "BTC-USDT"


def test_umx_native_exchange_is_discoverable_without_changing_ccxt_inventory():
    ccxt_only = SimpleNamespace(exchanges=["binance"])

    assert ccxt_exchanges(ccxt_only) == ["binance"]
    assert available_exchanges(ccxt_only) == ["binance", "umx"]

    entries = list_available_exchanges(False)
    umx_entries = [entry for entry in entries if entry["classname"] == "umx"]
    assert len(umx_entries) == 1
    assert umx_entries[0] == {
        "name": "UMX",
        "classname": "umx",
        "valid": True,
        "supported": False,
        "comment": "Native Freqtrade adapter (not provided by ccxt).",
        "comment_futures": "",
        "dex": False,
        "is_alias": False,
        "alias_for": None,
        "trade_modes": [
            {"trading_mode": "spot", "margin_mode": ""},
            {"trading_mode": "futures", "margin_mode": "cross"},
        ],
    }
    # Idempotency / round-trip.
    assert umx_symbol_to_ccxt(ccxt_symbol_to_umx("ETH/USDT:USDT")) == "ETH/USDT:USDT"


def test_umx_parses_perpetual_market(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    _patch_umx_futures_request(mocker)
    exchange = ExchangeResolver.load_exchange(_umx_futures_config(default_conf))

    market = exchange.markets["BTC/USDT:USDT"]
    assert market["id"] == "BTC-USDT-PERP"
    assert market["type"] == "swap"
    assert market["swap"] is True
    assert market["linear"] is True
    assert market["spot"] is False
    assert market["contract"] is True
    assert market["contractSize"] == 0.0001
    assert market["settle"] == "USDT"
    # Core market_is_future must accept this market.
    assert exchange.market_is_future(market) is True
    # ctVal=0.0001 => contract amount precision is whole contracts, min 1 contract.
    assert market["precision"]["amount"] == 0
    assert market["limits"]["amount"]["min"] == 1.0
    # get_contract_size reads contractSize.
    assert exchange.get_contract_size("BTC/USDT:USDT") == 0.0001
    # Perpetual trading fees configured as 0 (current account tier).
    assert market["maker"] == 0.0
    assert market["taker"] == 0.0


def test_umx_fetch_ohlcv_routes_mark_and_index(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    calls = _patch_umx_futures_request(mocker)
    exchange = ExchangeResolver.load_exchange(_umx_futures_config(default_conf))

    mark = exchange._api.fetch_ohlcv("BTC/USDT:USDT", "5m", params={"price": "mark"})
    assert mark[0][1] == 89610  # open from markPriceKline
    assert mark[0][5] == 0.0  # mark candles carry no volume
    assert any(call[1] == "/v1/market/markPriceKline" for call in calls)

    index = exchange._api.fetch_ohlcv("BTC/USDT:USDT", "5m", params={"price": "index"})
    assert index[0][1] == 89620
    assert index[0][5] == 0.0
    assert any(call[1] == "/v1/market/indexPriceKline" for call in calls)

    regular = exchange._api.fetch_ohlcv("BTC/USDT:USDT", "5m")
    assert regular[0][5] == 444.6  # regular klines keep traded volume


def test_umx_futures_open_order_converts_contracts(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    calls = _patch_umx_futures_request(mocker)
    exchange = ExchangeResolver.load_exchange(_umx_futures_config(default_conf, dry_run=False))

    order = exchange.create_order(
        pair="BTC/USDT:USDT",
        ordertype="limit",
        side="buy",
        amount=0.001,  # base BTC -> 10 contracts (ctVal 0.0001)
        rate=90000,
        leverage=1,
    )
    assert order["id"] == "1322590060595871744"

    order_calls = [c for c in calls if c[1] == "/v2/trade/order"]
    body = order_calls[-1][3]
    # 10 contracts * ctVal(0.0001) == 0.001 coin
    assert float(body["qty"]) == 0.001
    assert body["marketUnit"] == "baseCoin"
    assert "reduceOnly" not in body
    assert "isLeverage" not in body
    assert order["amount"] == pytest.approx(0.001)
    # Every futures order verifies symbol-level 1x leverage immediately before submission.
    readback_calls = [c for c in calls if c[1] == "/v1/trade/lever" and c[0] == "GET"]
    assert len(readback_calls) == 1
    assert readback_calls[0][2]["symbol"] == "BTC-USDT-PERP"
    assert "currency" not in readback_calls[0][2]
    set_calls = [c for c in calls if c[1] == "/v1/trade/lever" and c[0] == "POST"]
    assert len(set_calls) == 1
    assert set_calls[0][3]["symbol"] == "BTC-USDT-PERP"
    assert set_calls[0][3]["lever"] == "1"


def test_umx_futures_fetch_order_converts_coin_qty(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    _patch_umx_futures_request(mocker)
    exchange = ExchangeResolver.load_exchange(_umx_futures_config(default_conf, dry_run=False))

    order = exchange.fetch_order("1322590060595871744", "BTC/USDT:USDT")

    assert order["status"] == "closed"
    assert order["amount"] == pytest.approx(0.001)
    assert order["filled"] == pytest.approx(0.001)
    assert order["remaining"] == pytest.approx(0.0)
    assert order["cost"] == pytest.approx(90.0)
    assert order["fee"] == {"currency": "USDT", "cost": -0.0, "rate": None}


def test_umx_futures_reduce_only_order(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    calls = _patch_umx_futures_request(mocker)
    exchange = ExchangeResolver.load_exchange(_umx_futures_config(default_conf, dry_run=False))

    exchange.create_order(
        pair="BTC/USDT:USDT",
        ordertype="market",
        side="sell",
        amount=0.001,
        rate=90000,
        leverage=1,
        reduceOnly=True,
    )
    order_calls = [c for c in calls if c[1] == "/v2/trade/order"]
    body = order_calls[-1][3]
    assert body["reduceOnly"] is True
    assert body["orderType"] == "market"
    # reduceOnly orders skip leverage mutation but still pass the 1x readback gate.
    assert all(not (c[1] == "/v1/trade/lever" and c[0] == "POST") for c in calls)
    assert any(c[1] == "/v1/trade/lever" and c[0] == "GET" for c in calls)


def test_umx_fetch_positions_mapping(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    _patch_umx_futures_request(mocker)
    exchange = ExchangeResolver.load_exchange(_umx_futures_config(default_conf, dry_run=False))

    positions = exchange.fetch_positions("BTC/USDT:USDT")
    assert len(positions) == 1
    pos = positions[0]
    assert pos["symbol"] == "BTC/USDT:USDT"
    assert pos["side"] == "short"
    # positionQty -2 coin / ctVal 0.0001 => 20000 contracts
    assert pos["contracts"] == 20000.0
    assert pos["leverage"] == 2
    assert pos["entryPrice"] == 93921.6
    assert pos["liquidationPrice"] == 140063.17
    assert pos["marginMode"] == "cross"


@pytest.mark.parametrize(
    ("symbol", "market_id", "position_qty", "contract_size", "expected_contracts"),
    [
        ("HYPE/USDT:USDT", "HYPE-USDT-PERP", "-1.4", 0.1, 14.0),
        ("NVDA/USDT:USDT", "NVDA-USDT-PERP", "-0.47", 0.01, 47.0),
    ],
)
def test_umx_position_contract_conversion_avoids_float_truncation(
    default_conf,
    mocker,
    monkeypatch,
    symbol,
    market_id,
    position_qty,
    contract_size,
    expected_contracts,
):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    _patch_umx_futures_request(mocker)
    exchange = ExchangeResolver.load_exchange(_umx_futures_config(default_conf, dry_run=False))

    market = deepcopy(exchange._api.markets["BTC/USDT:USDT"])
    market.update(
        {
            "id": market_id,
            "symbol": symbol,
            "base": symbol.split("/")[0],
            "contractSize": contract_size,
        }
    )
    exchange._api.markets[symbol] = market
    exchange.markets[symbol] = market

    position = exchange._api._parse_position(
        {"symbol": market_id, "positionQty": position_qty, "im": "1"}
    )

    assert position is not None
    assert position["contracts"] == expected_contracts
    assert exchange._contracts_to_amount(symbol, position["contracts"]) == abs(float(position_qty))


def test_umx_rejects_non_one_x_leverage_without_mutation(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    calls = _patch_umx_futures_request(mocker)
    exchange = ExchangeResolver.load_exchange(_umx_futures_config(default_conf, dry_run=False))

    with pytest.raises(ccxt.InvalidOrder, match="only 1x leverage"):
        exchange._api.set_leverage(5.0, "BTC/USDT:USDT")

    assert all(not (c[1] == "/v1/trade/lever" and c[0] == "POST") for c in calls)


def test_umx_set_leverage_formats_float_as_integer(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    calls = _patch_umx_futures_request(mocker)
    exchange = ExchangeResolver.load_exchange(_umx_futures_config(default_conf, dry_run=False))

    exchange._set_leverage(1.0, "BTC/USDT:USDT")
    lever_calls = [c for c in calls if c[1] == "/v1/trade/lever"]
    assert lever_calls[-1][3]["symbol"] == "BTC-USDT-PERP"
    assert lever_calls[-1][3]["lever"] == "1"


def test_umx_fetch_leverage_uses_symbol_scope(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    calls = _patch_umx_futures_request(mocker)
    exchange = ExchangeResolver.load_exchange(_umx_futures_config(default_conf, dry_run=False))

    leverage = exchange._api.fetch_leverage("BTC/USDT:USDT")

    assert leverage["longLeverage"] == 1.0
    assert leverage["shortLeverage"] == 1.0
    readback = [c for c in calls if c[1] == "/v1/trade/lever" and c[0] == "GET"][-1]
    assert readback[2]["symbol"] == "BTC-USDT-PERP"
    assert "currency" not in readback[2]


def test_umx_futures_order_gate_blocks_non_one_x(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    calls = _patch_umx_futures_request(mocker)
    exchange = ExchangeResolver.load_exchange(_umx_futures_config(default_conf, dry_run=False))
    mocker.patch.object(
        exchange._api.client,
        "leverage",
        return_value={"code": "0", "data": {"symbol": "BTC-USDT-PERP", "lever": "2"}},
    )

    with pytest.raises(ccxt.InvalidOrder, match="symbol-level leverage readback must be 1x"):
        exchange._api.create_order("BTC/USDT:USDT", "limit", "buy", 10, 90000, {"reduceOnly": True})

    assert all(call[1] != "/v2/trade/order" for call in calls)


def test_umx_futures_balance_exposes_cross_equity(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    _patch_umx_futures_request(mocker)
    exchange = ExchangeResolver.load_exchange(_umx_futures_config(default_conf, dry_run=False))

    balances = exchange.get_balances()
    assert exchange.balance_includes_unrealized_pnl() is True
    assert balances["USDT"]["total"] == 1000.662
    assert balances["USDT"]["free"] == 800.0
    assert balances["USDT"]["used"] == pytest.approx(200.662)


def test_umx_wallets_strips_upl_from_cross_equity(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    _patch_umx_futures_request(mocker)
    conf = _umx_futures_config(default_conf, dry_run=False)
    exchange = ExchangeResolver.load_exchange(conf)

    wallets = Wallets(conf, exchange)

    assert wallets.get_all_positions()["BTC/USDT:USDT"].unrealized_pnl == 0.662
    # API totalEquity is 1000.662 and already contains the position UPL. Wallets restores
    # the plain wallet-balance meaning of total, so adding position UPL later is not doubled.
    assert wallets.get_total("USDT") == pytest.approx(1000.0)
    assert wallets.get_free("USDT") == 800.0


def test_umx_dry_run_liquidation_and_maintenance(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    _patch_umx_futures_request(mocker)
    exchange = ExchangeResolver.load_exchange(_umx_futures_config(default_conf))

    mm_ratio, maint_amt = exchange.get_maintenance_ratio_and_amt("BTC/USDT:USDT", 900.0)
    assert mm_ratio == 0.02
    assert maint_amt is None

    long_liq = exchange.dry_run_liquidation_price(
        pair="BTC/USDT:USDT",
        open_rate=90000.0,
        is_short=False,
        amount=0.01,
        stake_amount=90.0,
        leverage=10.0,
        wallet_balance=90.0,
        open_trades=[],
    )
    assert long_liq is not None and long_liq < 90000.0

    short_liq = exchange.dry_run_liquidation_price(
        pair="BTC/USDT:USDT",
        open_rate=90000.0,
        is_short=True,
        amount=0.01,
        stake_amount=90.0,
        leverage=10.0,
        wallet_balance=90.0,
        open_trades=[],
    )
    assert short_liq is not None and short_liq > 90000.0

    # No position size -> cannot compute -> None (core tolerates None).
    assert (
        exchange.dry_run_liquidation_price(
            pair="BTC/USDT:USDT",
            open_rate=90000.0,
            is_short=False,
            amount=0.0,
            stake_amount=90.0,
            leverage=10.0,
            wallet_balance=90.0,
            open_trades=[],
        )
        is None
    )


def test_umx_funding_rate_history_parsed(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    _patch_umx_futures_request(mocker)
    exchange = ExchangeResolver.load_exchange(_umx_futures_config(default_conf))

    # Real funding-rate history is wired up (not modelled as zero).
    assert exchange.exchange_has("fetchFundingRateHistory") is True

    history = exchange._api.fetch_funding_rate_history("BTC/USDT:USDT")
    assert len(history) == 2
    # Returned ascending by timestamp.
    assert history[0]["timestamp"] == 1769011200000
    assert history[0]["fundingRate"] == -0.0002
    assert history[1]["fundingRate"] == 0.0001
    assert history[1]["symbol"] == "BTC/USDT:USDT"


def test_umx_data_provider_fetches_current_funding_rate(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    calls = _patch_umx_futures_request(mocker)
    conf = _umx_futures_config(default_conf)
    exchange = ExchangeResolver.load_exchange(conf)
    data_provider = DataProvider(conf, exchange)

    funding = data_provider.funding_rate("BTC/USDT:USDT")

    assert exchange.exchange_has("fetchFundingRate") is True
    assert funding["symbol"] == "BTC/USDT:USDT"
    assert funding["fundingRate"] == 0.0005
    assert funding["fundingTimestamp"] == 1769040000000
    assert funding["fundingDatetime"] == "2026-01-22T00:00:00.000Z"
    assert funding["interval"] == "4h"
    assert funding["timestamp"] == 1769039900000
    request = [call for call in calls if call[1] == "/v1/market/fundingRate"][-1]
    assert request[2]["symbol"] == "BTC-USDT-PERP"


def test_umx_funding_rate_history_uses_download_shard_window(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    calls = _patch_umx_futures_request(mocker)
    exchange = ExchangeResolver.load_exchange(_umx_futures_config(default_conf))
    start = 1769011200000
    limit = 12

    exchange._api.fetch_funding_rate_history("BTC/USDT:USDT", since=start, limit=limit)

    request = [c for c in calls if c[1] == "/v1/market/fundingRate/history"][-1]
    assert request[2]["beginTime"] == start
    assert request[2]["endTime"] == start + limit * 60 * 60 * 1000
    assert request[2]["limit"] == limit


def test_umx_funding_rate_history_respects_and_validates_explicit_end(
    default_conf, mocker, monkeypatch
):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    calls = _patch_umx_futures_request(mocker)
    exchange = ExchangeResolver.load_exchange(_umx_futures_config(default_conf))
    start = 1769011200000
    explicit_end = start + 123456

    exchange._api.fetch_funding_rate_history(
        "BTC/USDT:USDT", since=start, limit=12, params={"endTime": str(explicit_end)}
    )
    request = [c for c in calls if c[1] == "/v1/market/fundingRate/history"][-1]
    assert request[2]["beginTime"] == start
    assert request[2]["endTime"] == explicit_end

    call_count = len(calls)
    with pytest.raises(ccxt.BadRequest, match="greater than since"):
        exchange._api.fetch_funding_rate_history(
            "BTC/USDT:USDT", since=start, params={"endTime": start}
        )
    with pytest.raises(ccxt.BadRequest, match="integer milliseconds"):
        exchange._api.fetch_funding_rate_history(
            "BTC/USDT:USDT", since=start, params={"endTime": "not-a-timestamp"}
        )
    assert len(calls) == call_count


def test_umx_funding_rate_history_concurrent_shards_use_distinct_windows(
    default_conf, mocker, monkeypatch
):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    calls = _patch_umx_futures_request(mocker)
    exchange = ExchangeResolver.load_exchange(_umx_futures_config(default_conf))
    start = 1760000000000
    limit = exchange.ohlcv_candle_limit("1h", CandleType.FUNDING_RATE)
    shard_ms = limit * 60 * 60 * 1000

    exchange.loop.run_until_complete(
        exchange._async_get_historic_ohlcv(
            "BTC/USDT:USDT",
            "1h",
            start,
            CandleType.FUNDING_RATE,
            raise_=True,
            until_ms=start + 2 * shard_ms,
        )
    )

    requests = sorted(
        (call[2] for call in calls if call[1] == "/v1/market/fundingRate/history"),
        key=lambda params: params["beginTime"],
    )
    assert [request["beginTime"] for request in requests] == [start, start + shard_ms]
    assert [request["endTime"] for request in requests] == [
        start + shard_ms,
        start + 2 * shard_ms,
    ]
    assert requests[0]["endTime"] == requests[1]["beginTime"]


def test_umx_fetch_my_trades_preserves_role_leverage_and_fee(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    calls = _patch_umx_futures_request(mocker)
    exchange = ExchangeResolver.load_exchange(_umx_futures_config(default_conf, dry_run=False))

    trades = exchange._api.fetch_my_trades("BTC/USDT:USDT", since=1769011200000, limit=50)

    assert exchange.exchange_has("fetchMyTrades") is True
    assert trades[0]["order"] == "1322590060595871744"
    assert trades[0]["takerOrMaker"] == "maker"
    assert trades[0]["leverage"] == 1.0
    assert trades[0]["fee"] == {"currency": "USDT", "cost": -0.009, "rate": None}
    assert trades[0]["amount"] == 10.0
    request = [c for c in calls if c[1] == "/v2/history/trades"][-1]
    assert request[2]["beginTime"] == 1769011200000
    assert request[2]["limit"] == 50


def test_umx_fetch_my_trades_pages_past_endpoint_limit(mocker):
    api = UMXSync(
        {
            "apiKey": "key",
            "secret": "secret",
            "default_business_type": "linear_perpetual",
        }
    )
    first_page = [
        {
            "id": str(200 - index),
            "tradeId": str(200 - index),
            "symbol": "BTC-USDT-PERP",
            "fillQty": "0.001",
            "fillPrice": "90000",
            "fillTime": str(1769133528000 - index),
        }
        for index in range(100)
    ]
    final_page = [
        {
            "id": "100",
            "tradeId": "100",
            "symbol": "BTC-USDT-PERP",
            "fillQty": "0.001",
            "fillPrice": "90000",
            "fillTime": "1769133527900",
        }
    ]
    history = mocker.patch.object(
        api.client,
        "trade_history",
        side_effect=[
            {"code": "0", "data": first_page},
            {"code": "0", "data": final_page},
        ],
    )

    trades = api.fetch_my_trades("BTC/USDT:USDT", since=1769011200000)

    assert len(trades) == 101
    assert history.call_count == 2
    assert history.call_args_list[1].kwargs["params"]["endId"] == "101"
    api.close()


def test_umx_get_funding_fees_uses_settled_bills(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    _patch_umx_futures_request(mocker)
    exchange = ExchangeResolver.load_exchange(_umx_futures_config(default_conf, dry_run=False))
    mocker.patch.object(exchange._api, "milliseconds", return_value=1769133529000)
    open_date = datetime(2026, 1, 22, tzinfo=UTC)

    assert exchange.exchange_has("fetchFundingHistory") is True
    history = exchange._api.fetch_funding_history("BTC/USDT:USDT", since=1768953600000)
    assert [entry["amount"] for entry in history] == [-0.4, 0.25]
    assert all(entry["code"] == "USDT" for entry in history)
    assert exchange.get_funding_fees("BTC/USDT:USDT", 0.1, True, open_date) == pytest.approx(0.25)


def test_umx_funding_history_pages_past_endpoint_limit(mocker):
    api = UMXSync(
        {
            "apiKey": "key",
            "secret": "secret",
            "default_business_type": "linear_perpetual",
        }
    )
    first_page = [
        {
            "id": str(200 - index),
            "actionType": "18",
            "businessType": "linear_perpetual",
            "symbol": "BTC-USDT-PERP",
            "currency": "USDT",
            "qty": "0.01",
            "createTime": str(1769133528000 - index),
        }
        for index in range(100)
    ]
    final_page = [
        {
            "id": "100",
            "actionType": "18",
            "businessType": "linear_perpetual",
            "symbol": "BTC-USDT-PERP",
            "currency": "USDT",
            "qty": "0.01",
            "createTime": "1769133527900",
        }
    ]
    history = mocker.patch.object(
        api.client,
        "bills",
        side_effect=[
            {"code": "0", "data": first_page},
            {"code": "0", "data": final_page},
        ],
    )
    mocker.patch.object(api, "milliseconds", return_value=1769133529000)

    funding = api.fetch_funding_history("BTC/USDT:USDT", since=1769011200000)

    assert len(funding) == 101
    assert history.call_count == 2
    assert history.call_args_list[1].kwargs["params"]["endId"] == "101"
    api.close()


def test_umx_futures_rejects_isolated_margin(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    _patch_umx_futures_request(mocker)
    conf = _umx_futures_config(default_conf)
    conf["margin_mode"] = "isolated"

    with pytest.raises(OperationalException):
        ExchangeResolver.load_exchange(conf)


def test_umx_live_futures_validates_existing_positions(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    _patch_umx_futures_request(mocker)
    exchange = ExchangeResolver.load_exchange(_umx_futures_config(default_conf, dry_run=False))

    pair = "BTC/USDT:USDT"
    matching_position = PositionWallet(
        pair,
        position=0.0010000000001,
        leverage=2.0,
        collateral=90.0,
        side="long",
    )
    matching_trade = SimpleNamespace(pair=pair, amount=0.001, trade_direction="long")
    exchange.validate_existing_positions({pair: matching_position}, [matching_trade])

    conflicts = [
        ({pair: matching_position}, [], "database has no open trade"),
        (
            {pair: matching_position},
            [SimpleNamespace(pair=pair, amount=0.001, trade_direction="short")],
            "exchange side is long, database side is short",
        ),
        (
            {pair: matching_position._replace(position=0.0009)},
            [matching_trade],
            "exchange amount is 0.0009, database amount is 0.001",
        ),
        ({}, [matching_trade], "exchange has no matching position"),
    ]
    for positions, trades, message in conflicts:
        with pytest.raises(OperationalException, match=message):
            exchange.validate_existing_positions(positions, trades)


def test_umx_dry_run_ignores_existing_positions(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    _patch_umx_futures_request(mocker)
    exchange = ExchangeResolver.load_exchange(_umx_futures_config(default_conf))

    position = PositionWallet(
        "BTC/USDT:USDT",
        position=0.001,
        leverage=2.0,
        collateral=90.0,
        side="long",
    )
    exchange.validate_existing_positions({position.symbol: position}, [])
