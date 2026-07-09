import hmac
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256

import ccxt
import pytest

from freqtrade.enums import CandleType, RunMode
from freqtrade.exceptions import ExchangeError, OperationalException
from freqtrade.exchange import Xcoin
from freqtrade.exchange.check_exchange import check_exchange
from freqtrade.exchange.xcoin_connector import (
    XCoinClient,
    ccxt_symbol_to_xcoin,
    xcoin_symbol_to_ccxt,
)
from freqtrade.resolvers.exchange_resolver import ExchangeResolver


def _xcoin_config(default_conf: dict, *, dry_run: bool = True) -> dict:
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
            "name": "xcoin",
            "pair_whitelist": ["BTC/USDT"],
            "pair_blacklist": [],
            "xcoin_live_trading_enabled": not dry_run,
        }
    )
    return conf


def _xcoin_response(method, path, params=None, data=None, private=False):
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
                ]
            },
            "ts": "1732193257273",
        }
    if path == "/v2/trade/order":
        assert method == "POST"
        assert data["symbol"] == "BTC-USDT"
        assert data["side"] == "buy"
        assert data["marketUnit"] == "baseCoin"
        if data["orderType"] == "limit":
            assert float(data["price"]) == 80000
            assert data["timeInForce"] == "gtc"
        elif data["orderType"] == "market":
            assert "price" not in data
            assert data["timeInForce"] == "ioc"
        else:
            raise AssertionError(f"Unexpected XCoin order type: {data['orderType']}")
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
    raise AssertionError(f"Unhandled XCoin mock path: {method} {path}")


def _patch_xcoin_request(mocker):
    calls = []

    def fake_request(self, method, path, *, params=None, data=None, private=False):
        calls.append((method, path, params, data, private))
        return _xcoin_response(method, path, params=params, data=data, private=private)

    mocker.patch.object(XCoinClient, "request", fake_request)
    return calls


def test_xcoin_client_signing_uses_document_shape():
    client = XCoinClient({"secret": "test-secret"})
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


@pytest.mark.parametrize(
    ("code", "exception"),
    [
        ("14001", ccxt.DDoSProtection),
        ("10112", ccxt.AuthenticationError),
        ("40013", ccxt.OrderNotFound),
        ("50006", ccxt.InvalidOrder),
        ("60101", ccxt.InsufficientFunds),
        ("60117", ccxt.InvalidOrder),
    ],
)
def test_xcoin_client_error_mapping(code, exception):
    with pytest.raises(exception):
        XCoinClient()._handle_response({"code": code, "msg": "boom"})


def test_check_exchange_accepts_native_xcoin(default_conf):
    conf = _xcoin_config(default_conf)
    conf["runmode"] = RunMode.DRY_RUN
    assert check_exchange(conf)


def test_xcoin_resolver_loads_native_exchange(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    _patch_xcoin_request(mocker)

    exchange = ExchangeResolver.load_exchange(_xcoin_config(default_conf))

    assert isinstance(exchange, Xcoin)
    assert exchange.name == "XCoin"
    assert exchange.markets["BTC/USDT"]["id"] == "BTC-USDT"
    assert exchange.markets["BTC/USDT"]["limits"]["amount"]["min"] == 0.00001


def test_xcoin_credentials_are_read_from_environment(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    _patch_xcoin_request(mocker)
    conf = _xcoin_config(default_conf)
    conf["exchange"]["key"] = "config-key"
    conf["exchange"]["secret"] = "config-secret"
    conf["exchange"]["ccxt_config"] = {"apiKey": "ccxt-key", "secret": "ccxt-secret"}

    exchange = ExchangeResolver.load_exchange(conf)

    assert exchange._api.apiKey == "env-key"
    assert exchange._api.secret == "env-secret"


def test_xcoin_public_market_data(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    _patch_xcoin_request(mocker)
    exchange = ExchangeResolver.load_exchange(_xcoin_config(default_conf))

    ticker = exchange.fetch_ticker("BTC/USDT")
    assert ticker["last"] == 80000
    assert ticker["bid"] == 79999.99
    assert ticker["ask"] == 80000.01

    candles = exchange.refresh_latest_ohlcv(
        [("BTC/USDT", "5m", CandleType.SPOT)], cache=False, drop_incomplete=False
    )
    candle_df = candles[("BTC/USDT", "5m", CandleType.SPOT)]
    assert len(candle_df) == 2
    assert candle_df.iloc[0]["open"] == 80000
    assert candle_df.iloc[1]["close"] == 80200


def test_xcoin_private_account_and_order_methods(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    _patch_xcoin_request(mocker)
    exchange = ExchangeResolver.load_exchange(_xcoin_config(default_conf, dry_run=False))

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

    canceled = exchange.cancel_order("1322590062927904769", "BTC/USDT")
    assert canceled["status"] == "canceled"

    open_orders = exchange._api.fetch_open_orders("BTC/USDT")
    assert len(open_orders) == 1
    assert open_orders[0]["status"] == "open"


def test_xcoin_private_market_order(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    calls = _patch_xcoin_request(mocker)
    exchange = ExchangeResolver.load_exchange(_xcoin_config(default_conf, dry_run=False))

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


def test_xcoin_dry_run_order_does_not_call_live_order(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    calls = _patch_xcoin_request(mocker)
    exchange = ExchangeResolver.load_exchange(_xcoin_config(default_conf))

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


def test_xcoin_live_requires_explicit_enable(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    _patch_xcoin_request(mocker)
    conf = _xcoin_config(default_conf, dry_run=False)
    conf["exchange"]["xcoin_live_trading_enabled"] = False

    with pytest.raises(Exception, match="XCoin live trading is disabled"):
        ExchangeResolver.load_exchange(conf)


# ---------------------------------------------------------------------------
# Linear perpetual (U-margined futures) support
# ---------------------------------------------------------------------------


def _xcoin_futures_config(default_conf: dict, *, dry_run: bool = True) -> dict:
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
            "name": "xcoin",
            "pair_whitelist": ["BTC/USDT:USDT"],
            "pair_blacklist": [],
            "xcoin_live_trading_enabled": not dry_run,
        }
    )
    return conf


def _xcoin_futures_response(method, path, params=None, data=None, private=False):
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
                ["5m", "1769131800000", "1769132099999", "89600", "89700",
                 "89800", "89500", "444.6", "39895537", "238", "100", "0.001"],
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
                {"symbol": "BTC-USDT-PERP", "fundingRate": "0.0001",
                 "fundingTime": "1769040000000", "markPrice": "90000"},
                {"symbol": "BTC-USDT-PERP", "fundingRate": "-0.0002",
                 "fundingTime": "1769011200000", "markPrice": "89500"},
            ],
            "ts": "1769133527828",
        }

    assert private
    if path == "/v1/account/balance":
        return {
            "code": "0",
            "msg": "Success",
            "data": {
                "totalEquity": "1000",
                "totalMarginBalance": "1000",
                "totalAvailableBalance": "800",
                "details": [
                    {"currency": "USDT", "equity": "1000", "totalBalance": "0",
                     "cashBalance": "0", "frozen": "0"},
                ],
            },
            "ts": "1769133527828",
        }
    if path == "/v2/trade/order":
        assert method == "POST"
        assert data["symbol"] == "BTC-USDT-PERP"
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
    if path == "/v1/trade/lever":
        assert method == "POST"
        return {
            "code": "0",
            "msg": "Success",
            "data": {"symbol": data.get("symbol"), "lever": data.get("lever")},
            "ts": "1769133527828",
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
    raise AssertionError(f"Unhandled XCoin futures mock path: {method} {path}")


def _patch_xcoin_futures_request(mocker):
    calls = []

    def fake_request(self, method, path, *, params=None, data=None, private=False):
        calls.append((method, path, params, data, private))
        return _xcoin_futures_response(method, path, params=params, data=data, private=private)

    mocker.patch.object(XCoinClient, "request", fake_request)
    return calls


def test_xcoin_symbol_conversion_handles_perp():
    assert xcoin_symbol_to_ccxt("BTC-USDT-PERP") == "BTC/USDT:USDT"
    assert ccxt_symbol_to_xcoin("BTC/USDT:USDT") == "BTC-USDT-PERP"
    # Spot conversions stay intact.
    assert xcoin_symbol_to_ccxt("BTC-USDT") == "BTC/USDT"
    assert ccxt_symbol_to_xcoin("BTC/USDT") == "BTC-USDT"
    # Idempotency / round-trip.
    assert xcoin_symbol_to_ccxt(ccxt_symbol_to_xcoin("ETH/USDT:USDT")) == "ETH/USDT:USDT"


def test_xcoin_parses_perpetual_market(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    _patch_xcoin_futures_request(mocker)
    exchange = ExchangeResolver.load_exchange(_xcoin_futures_config(default_conf))

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


def test_xcoin_fetch_ohlcv_routes_mark_and_index(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    calls = _patch_xcoin_futures_request(mocker)
    exchange = ExchangeResolver.load_exchange(_xcoin_futures_config(default_conf))

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


def test_xcoin_futures_open_order_converts_contracts(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    calls = _patch_xcoin_futures_request(mocker)
    exchange = ExchangeResolver.load_exchange(_xcoin_futures_config(default_conf, dry_run=False))

    order = exchange.create_order(
        pair="BTC/USDT:USDT",
        ordertype="limit",
        side="buy",
        amount=0.001,  # base BTC -> 10 contracts (ctVal 0.0001)
        rate=90000,
        leverage=3,
    )
    assert order["id"] == "1322590060595871744"

    order_calls = [c for c in calls if c[1] == "/v2/trade/order"]
    body = order_calls[-1][3]
    # 10 contracts * ctVal(0.0001) == 0.001 coin
    assert float(body["qty"]) == 0.001
    assert body["marketUnit"] == "baseCoin"
    assert "reduceOnly" not in body
    assert order["amount"] == pytest.approx(0.001)
    # Opening a position sets XCoin's coin-level cross leverage first.
    lever_calls = [c for c in calls if c[1] == "/v1/trade/lever"]
    assert lever_calls and lever_calls[-1][3]["currency"] == "BTC"
    assert "symbol" not in lever_calls[-1][3]
    assert float(lever_calls[-1][3]["lever"]) == 3


def test_xcoin_futures_fetch_order_converts_coin_qty(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    _patch_xcoin_futures_request(mocker)
    exchange = ExchangeResolver.load_exchange(_xcoin_futures_config(default_conf, dry_run=False))

    order = exchange.fetch_order("1322590060595871744", "BTC/USDT:USDT")

    assert order["status"] == "closed"
    assert order["amount"] == pytest.approx(0.001)
    assert order["filled"] == pytest.approx(0.001)
    assert order["remaining"] == pytest.approx(0.0)
    assert order["cost"] == pytest.approx(90.0)


def test_xcoin_futures_reduce_only_order(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    calls = _patch_xcoin_futures_request(mocker)
    exchange = ExchangeResolver.load_exchange(_xcoin_futures_config(default_conf, dry_run=False))

    exchange.create_order(
        pair="BTC/USDT:USDT",
        ordertype="market",
        side="sell",
        amount=0.001,
        rate=90000,
        leverage=3,
        reduceOnly=True,
    )
    order_calls = [c for c in calls if c[1] == "/v2/trade/order"]
    body = order_calls[-1][3]
    assert body["reduceOnly"] is True
    assert body["orderType"] == "market"
    # reduceOnly orders must not re-prepare leverage.
    assert all(c[1] != "/v1/trade/lever" for c in calls)


def test_xcoin_fetch_positions_mapping(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    _patch_xcoin_futures_request(mocker)
    exchange = ExchangeResolver.load_exchange(_xcoin_futures_config(default_conf, dry_run=False))

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


def test_xcoin_set_leverage_calls_lever_endpoint(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    calls = _patch_xcoin_futures_request(mocker)
    exchange = ExchangeResolver.load_exchange(_xcoin_futures_config(default_conf, dry_run=False))

    exchange._set_leverage(5.0, "BTC/USDT:USDT")
    lever_calls = [c for c in calls if c[1] == "/v1/trade/lever"]
    assert lever_calls
    assert lever_calls[-1][3]["currency"] == "BTC"
    assert "symbol" not in lever_calls[-1][3]
    assert lever_calls[-1][3]["lever"] == "5"


def test_xcoin_set_leverage_formats_float_as_integer(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    calls = _patch_xcoin_futures_request(mocker)
    exchange = ExchangeResolver.load_exchange(_xcoin_futures_config(default_conf, dry_run=False))

    exchange._set_leverage(1.0, "BTC/USDT:USDT")
    lever_calls = [c for c in calls if c[1] == "/v1/trade/lever"]
    assert lever_calls[-1][3]["currency"] == "BTC"
    assert lever_calls[-1][3]["lever"] == "1"


def test_xcoin_futures_balance_exposes_cross_equity(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    _patch_xcoin_futures_request(mocker)
    exchange = ExchangeResolver.load_exchange(_xcoin_futures_config(default_conf, dry_run=False))

    balances = exchange.get_balances()
    assert balances["USDT"]["total"] == 1000.0
    assert balances["USDT"]["free"] == 800.0
    assert balances["USDT"]["used"] == 200.0


def test_xcoin_dry_run_liquidation_and_maintenance(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    _patch_xcoin_futures_request(mocker)
    exchange = ExchangeResolver.load_exchange(_xcoin_futures_config(default_conf))

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


def test_xcoin_funding_rate_history_parsed(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    _patch_xcoin_futures_request(mocker)
    exchange = ExchangeResolver.load_exchange(_xcoin_futures_config(default_conf))

    # Real funding-rate history is wired up (not modelled as zero).
    assert exchange.exchange_has("fetchFundingRateHistory") is True

    history = exchange._api.fetch_funding_rate_history("BTC/USDT:USDT")
    assert len(history) == 2
    # Returned ascending by timestamp.
    assert history[0]["timestamp"] == 1769011200000
    assert history[0]["fundingRate"] == -0.0002
    assert history[1]["fundingRate"] == 0.0001
    assert history[1]["symbol"] == "BTC/USDT:USDT"


def test_xcoin_get_funding_fees_uses_rate_history(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    _patch_xcoin_futures_request(mocker)
    exchange = ExchangeResolver.load_exchange(_xcoin_futures_config(default_conf, dry_run=False))
    helper = mocker.patch.object(exchange, "_fetch_and_calculate_funding_fees", return_value=1.23)

    open_date = datetime(2026, 1, 22, tzinfo=timezone.utc)

    assert exchange.get_funding_fees("BTC/USDT:USDT", 0.1, True, open_date) == 1.23
    helper.assert_called_once_with("BTC/USDT:USDT", 0.1, True, open_date)

    helper.reset_mock(side_effect=True)
    helper.side_effect = ExchangeError("funding unavailable")

    assert exchange.get_funding_fees("BTC/USDT:USDT", 0.1, True, open_date) == 0.0


def test_xcoin_futures_rejects_isolated_margin(default_conf, mocker, monkeypatch):
    monkeypatch.setenv("FREQTRADE__EXCHANGE__KEY", "env-key")
    monkeypatch.setenv("FREQTRADE__EXCHANGE__SECRET", "env-secret")
    _patch_xcoin_futures_request(mocker)
    conf = _xcoin_futures_config(default_conf)
    conf["margin_mode"] = "isolated"

    with pytest.raises(OperationalException):
        ExchangeResolver.load_exchange(conf)
