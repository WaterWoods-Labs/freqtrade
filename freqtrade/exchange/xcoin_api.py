"""ccxt-like XCoin facade backed by the internal REST connector."""

import asyncio
from copy import deepcopy
from typing import Any

import ccxt
from ccxt import DECIMAL_PLACES

from freqtrade.exchange.xcoin_connector import (
    XCOIN_BUSINESS_LINEAR_PERPETUAL,
    XCOIN_BUSINESS_SPOT,
    XCOIN_DEFAULT_BASE_URL,
    XCOIN_PERP_SUFFIX,
    XCOIN_TIMEFRAMES,
    XCoinClient,
    ccxt_symbol_to_xcoin,
    xcoin_symbol_to_ccxt,
)


__all__ = [
    "XCOIN_DEFAULT_BASE_URL",
    "XCOIN_HAS",
    "XCOIN_TIMEFRAMES",
    "XCoinAsync",
    "XCoinSync",
    "ccxt_symbol_to_xcoin",
    "xcoin_symbol_to_ccxt",
]


XCOIN_HAS = {
    "fetchOrder": True,
    "fetchOpenOrder": False,
    "fetchClosedOrder": False,
    "fetchOpenOrders": True,
    "fetchOrders": False,
    "fetchBalance": True,
    "fetchOHLCV": True,
    "fetchTicker": True,
    "fetchTickers": True,
    "fetchL2OrderBook": True,
    "fetchOrderBook": True,
    "fetchMyTrades": False,
    "fetchTrades": False,
    "cancelOrder": True,
    "createOrder": True,
    "createLimitOrder": True,
    "createMarketOrder": True,
    "watchOHLCV": False,
    # Futures-only endpoints are gated through ``_ft_has_futures`` overrides in xcoin.py
    # so spot mode keeps reporting them as unsupported.
    "fetchMarkOHLCV": False,
    "fetchIndexOHLCV": False,
    "fetchFundingRateHistory": False,
    "fetchPositions": False,
    "setLeverage": False,
    "setMarginMode": False,
}

XCOIN_STATUS_MAP = {
    "untriggered": "open",
    "new": "open",
    "partially_filled": "open",
    "filled": "closed",
    "canceled": "canceled",
    "partially_canceled": "canceled",
}


def _clean_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _num(value: Any, fallback: float = 0.0) -> float:
    result = _clean_float(value)
    return fallback if result is None else result


def _str_num(value: float | int | str | None) -> str | None:
    if value is None:
        return None
    return str(value)


def _first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


class XCoinSync:
    """Small ccxt-compatible subset used by Freqtrade's Exchange base class."""

    id = "xcoin"
    name = "XCoin"
    precisionMode = DECIMAL_PLACES
    timeframes = XCOIN_TIMEFRAMES
    features = {
        "spot": {"fetchOHLCV": {"limit": 1000}},
        "swap": {"linear": {"fetchOHLCV": {"limit": 1000}}},
    }
    has = XCOIN_HAS
    session = None

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        self.client = XCoinClient(config)
        self.apiKey = self.client.api_key
        self.secret = self.client.api_secret
        self.password = self.client.password
        self.uid = self.client.uid
        self.account_name = self.client.account_name
        self.base_url = self.client.base_url
        self.timeout = self.client.timeout
        self.session = self.client._http
        # Market scope for whole-market calls (load_markets / fetch_tickers without a symbol).
        # Per-symbol calls always derive the business type from the symbol itself.
        self.default_business_type = (
            config.get("default_business_type") or XCOIN_BUSINESS_SPOT
        )
        self.markets: dict[str, dict[str, Any]] = {}
        self.markets_by_id: dict[str, dict[str, Any]] = {}
        self.options = {
            "createMarketBuyOrderRequiresPrice": False,
            "timeframes": {"spot": XCOIN_TIMEFRAMES, "swap": XCOIN_TIMEFRAMES},
        }

    def milliseconds(self) -> int:
        return self.client.milliseconds()

    def iso8601(self, timestamp: int | None) -> str | None:
        return ccxt.Exchange.iso8601(timestamp) if timestamp is not None else None

    def set_markets_from_exchange(self, exchange: Any) -> None:
        self.markets = deepcopy(exchange.markets)
        self.markets_by_id = {
            market["id"]: market for market in self.markets.values() if market.get("id")
        }

    def market_id(self, symbol: str) -> str:
        if symbol in self.markets:
            return self.markets[symbol]["id"]
        return ccxt_symbol_to_xcoin(symbol)

    def _market(self, symbol: str) -> dict[str, Any]:
        return self.markets.get(xcoin_symbol_to_ccxt(symbol)) or {}

    def _is_futures_symbol(self, symbol: str) -> bool:
        """Decide whether a ccxt symbol refers to a linear-perpetual contract."""
        market = self._market(symbol)
        if market:
            return market.get("swap", False) is True
        return ":" in symbol or symbol.endswith(f"-{XCOIN_PERP_SUFFIX}")

    def _business_type(self, symbol: str) -> str:
        return (
            XCOIN_BUSINESS_LINEAR_PERPETUAL
            if self._is_futures_symbol(symbol)
            else XCOIN_BUSINESS_SPOT
        )

    def _contract_size(self, symbol: str) -> float:
        size = self._market(symbol).get("contractSize")
        return float(size) if size else 1.0

    def _symbol_family(self, symbol: str) -> str:
        family = self._market(symbol).get("info", {}).get("symbolFamily")
        if family:
            return family
        market_id = self.market_id(symbol)
        return market_id[: -(len(XCOIN_PERP_SUFFIX) + 1)] if market_id.endswith(
            f"-{XCOIN_PERP_SUFFIX}"
        ) else market_id

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        private: bool = False,
    ) -> dict[str, Any]:
        return self.client.request(method, path, params=params, data=data, private=private)

    def _private_params(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.client.private_params(params)

    def _parse_market(self, raw: dict[str, Any]) -> dict[str, Any]:
        business_type = raw.get("businessType")
        is_perp = business_type == XCOIN_BUSINESS_LINEAR_PERPETUAL
        symbol = xcoin_symbol_to_ccxt(raw["symbol"])
        order_params = raw.get("orderParameters") or {}
        max_amount = _first_present(
            order_params.get("maxLmtOrderQty"), order_params.get("maxMktOrderQty")
        )
        max_cost = _first_present(
            order_params.get("maxLmtOrderAmt"), order_params.get("maxMktOrderAmt")
        )
        contract_size = _clean_float(raw.get("ctVal")) if is_perp else None
        if not contract_size:
            contract_size = None

        # ccxt expresses contract amounts in *contracts*; convert XCoin's coin-denominated
        # order limits (and amount precision) to whole contracts when ``ctVal`` is known.
        if is_perp and contract_size:
            min_amount = _clean_float(order_params.get("minOrderQty"))
            min_amount = min_amount / contract_size if min_amount is not None else None
            max_amount_value = _clean_float(max_amount)
            max_amount_value = (
                max_amount_value / contract_size if max_amount_value is not None else None
            )
            # XCoin trades whole contracts, so contract amounts have no decimal places.
            amount_precision = 0
        else:
            min_amount = _clean_float(order_params.get("minOrderQty"))
            max_amount_value = _clean_float(max_amount)
            amount_precision = int(raw.get("quantityPrecision") or 8)

        market: dict[str, Any] = {
            "id": raw["symbol"],
            "symbol": symbol,
            "base": raw.get("baseCurrency"),
            "quote": raw.get("quoteCurrency"),
            "settle": raw.get("settleCurrency") or ("USDT" if is_perp else None),
            "type": "swap" if is_perp else "spot",
            "spot": business_type == XCOIN_BUSINESS_SPOT,
            "margin": False,
            "swap": is_perp,
            "future": False,
            "linear": is_perp,
            "inverse": False if is_perp else None,
            "contract": is_perp,
            "contractSize": contract_size,
            "active": raw.get("status") == "trading",
            "precision": {
                "amount": amount_precision,
                "price": int(raw.get("pricePrecision") or 8),
            },
            "limits": {
                "amount": {
                    "min": min_amount,
                    "max": max_amount_value,
                },
                "price": {"min": None, "max": None},
                "cost": {
                    "min": _clean_float(order_params.get("minOrderAmt")),
                    "max": _clean_float(max_cost),
                },
                "leverage": {
                    "min": 1.0,
                    "max": _clean_float(raw.get("maxLeverage")) if is_perp else None,
                },
            },
            # XCoin perpetual trading fees are account-tier specific and not exposed by the
            # public API; configured here as 0 (current account tier). Override per-account
            # via the config ``fee`` key if your tier differs.
            "maker": 0.0 if is_perp else 0.001,
            "taker": 0.0 if is_perp else 0.001,
            "info": raw,
        }
        return market

    def load_markets(
        self, reload: bool = False, params: dict[str, Any] | None = None
    ) -> dict[str, dict[str, Any]]:
        if self.markets and not reload:
            return self.markets
        payload = self.client.public_symbols(params, business_type=self.default_business_type)
        markets = {
            market["symbol"]: market
            for market in (self._parse_market(item) for item in payload.get("data", []))
        }
        self.markets = markets
        self.markets_by_id = {market["id"]: market for market in markets.values()}
        return markets

    def _parse_ticker(
        self,
        raw: dict[str, Any],
        *,
        bid: float | None = None,
        ask: float | None = None,
        timestamp: int | None = None,
    ) -> dict[str, Any]:
        last = _clean_float(raw.get("lastPrice"))
        symbol = xcoin_symbol_to_ccxt(raw["symbol"])
        return {
            "symbol": symbol,
            "timestamp": timestamp,
            "datetime": self.iso8601(timestamp),
            "high": None,
            "low": None,
            "bid": bid if bid is not None else last,
            "bidVolume": None,
            "ask": ask if ask is not None else last,
            "askVolume": None,
            "vwap": None,
            "open": None,
            "close": last,
            "last": last,
            "previousClose": None,
            "change": _clean_float(raw.get("priceChange")),
            "percentage": _clean_float(raw.get("priceChangePercent")),
            "average": None,
            "baseVolume": _clean_float(raw.get("fillQty")),
            "quoteVolume": _clean_float(raw.get("fillAmount")),
            "info": raw,
        }

    def fetch_ticker(self, symbol: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        market_id = self.market_id(symbol)
        payload = self.client.ticker_mini(
            market_id, params, business_type=self._business_type(symbol)
        )
        data = payload.get("data") or []
        if not data:
            raise ccxt.BadSymbol(f"XCoin ticker not found for {symbol}")
        orderbook = self.fetch_l2_order_book(symbol, 1)
        bid = orderbook["bids"][0][0] if orderbook["bids"] else None
        ask = orderbook["asks"][0][0] if orderbook["asks"] else None
        return self._parse_ticker(
            data[0], bid=bid, ask=ask, timestamp=int(_num(payload.get("ts"), 0))
        )

    def fetch_tickers(
        self, symbols: list[str] | None = None, params: dict[str, Any] | None = None
    ) -> dict[str, dict[str, Any]]:
        single = symbols[0] if symbols and len(symbols) == 1 else None
        market_id = self.market_id(single) if single else None
        business_type = self._business_type(single) if single else self.default_business_type
        payload = self.client.ticker_mini(market_id, params, business_type=business_type)
        timestamp = int(_num(payload.get("ts"), 0))
        tickers = {
            ticker["symbol"]: ticker
            for ticker in (
                self._parse_ticker(item, timestamp=timestamp)
                for item in payload.get("data", [])
            )
        }
        if symbols:
            symbols = [xcoin_symbol_to_ccxt(s) for s in symbols]
            tickers = {symbol: ticker for symbol, ticker in tickers.items() if symbol in symbols}
        return tickers

    def _parse_depth_side(self, values: list[Any]) -> list[list[float]]:
        if (
            values
            and isinstance(values[0], list)
            and len(values) == 1
            and isinstance(values[0][0], list)
        ):
            values = values[0]
        return [[float(price), float(amount)] for price, amount in values]

    def fetch_l2_order_book(
        self, symbol: str, limit: int | None = 100, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload = self.client.depth(
            self.market_id(symbol), limit, params, business_type=self._business_type(symbol)
        )
        data = payload.get("data") or {}
        timestamp = int(_num(payload.get("ts"), 0))
        return {
            "symbol": xcoin_symbol_to_ccxt(symbol),
            "bids": self._parse_depth_side(data.get("bids") or []),
            "asks": self._parse_depth_side(data.get("asks") or []),
            "timestamp": timestamp,
            "datetime": self.iso8601(timestamp),
            "nonce": int(_num(data.get("lastUpdateId"), 0)) if data.get("lastUpdateId") else None,
        }

    def fetch_order_book(
        self, symbol: str, limit: int | None = 100, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return self.fetch_l2_order_book(symbol, limit, params)

    def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1m",
        since: int | None = None,
        limit: int | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[list[float]]:
        if timeframe not in self.timeframes:
            raise ccxt.BadRequest(f"Unsupported XCoin timeframe: {timeframe}")
        params = dict(params or {})
        # Freqtrade routes mark/index candles through params["price"] = str(candle_type).
        price = params.pop("price", None)
        period = self.timeframes[timeframe]
        market_id = self.market_id(symbol)
        business_type = self._business_type(symbol)

        if price == "mark":
            payload = self.client.mark_price_klines(
                market_id, period, since=since, limit=limit, params=params,
                business_type=business_type,
            )
            return self._parse_priced_klines(payload)
        if price == "index":
            payload = self.client.index_price_klines(
                self._symbol_family(symbol), period, since=since, limit=limit, params=params
            )
            return self._parse_priced_klines(payload)

        payload = self.client.klines(
            market_id, period, since=since, limit=limit, params=params,
            business_type=business_type,
        )
        candles = [
            [
                int(row[1]),
                float(row[3]),
                float(row[5]),
                float(row[6]),
                float(row[4]),
                float(row[7]),
            ]
            for row in payload.get("data", [])
        ]
        return sorted(candles, key=lambda candle: candle[0])

    @staticmethod
    def _parse_priced_klines(payload: dict[str, Any]) -> list[list[float]]:
        """Parse mark/index klines: [period, startTime, closeTime, open, close, high, low].

        These series carry no traded volume, so the OHLCV volume column is filled with 0.
        """
        candles = [
            [
                int(row[1]),
                float(row[3]),
                float(row[5]),
                float(row[6]),
                float(row[4]),
                0.0,
            ]
            for row in payload.get("data", [])
        ]
        return sorted(candles, key=lambda candle: candle[0])

    def fetch_balance(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = self.client.balance(params)
        data = payload.get("data") or {}
        balances: dict[str, Any] = {"info": payload, "free": {}, "used": {}, "total": {}}
        for item in data.get("details", []):
            currency = item["currency"]
            free = _num(item.get("cashBalance"), 0.0)
            used = _num(item.get("frozen"), 0.0)
            total = _num(item.get("totalBalance"), free + used)
            balances[currency] = {"free": free, "used": used, "total": total}
            balances["free"][currency] = free
            balances["used"][currency] = used
            balances["total"][currency] = total

        # Futures cross account: expose the USDT settle collateral from the account-level totals
        # so Freqtrade sees the cross-margin equity rather than only the spot cash balance.
        total_equity = _clean_float(data.get("totalEquity"))
        if total_equity is not None:
            settle = "USDT"
            free = _num(data.get("totalAvailableBalance"), 0.0)
            used = max(total_equity - free, 0.0)
            balances[settle] = {"free": free, "used": used, "total": total_equity}
            balances["free"][settle] = free
            balances["used"][settle] = used
            balances["total"][settle] = total_equity
        return balances

    def _parse_order(
        self,
        raw: dict[str, Any],
        *,
        symbol: str | None = None,
        amount: float | None = None,
        price: float | None = None,
        order_type: str | None = None,
        side: str | None = None,
        status: str | None = None,
        timestamp: int | None = None,
    ) -> dict[str, Any]:
        symbol = xcoin_symbol_to_ccxt(raw.get("symbol") or symbol or "")
        timestamp = int(raw.get("createTime") or timestamp or raw.get("ts") or self.milliseconds())
        amount = _clean_float(raw.get("qty")) if raw.get("qty") is not None else amount
        price = _clean_float(raw.get("price")) if raw.get("price") is not None else price
        filled = _num(raw.get("totalFillQty"), 0.0)
        average = _clean_float(raw.get("avgPrice"))
        cost = _clean_float(raw.get("quoteQty"))
        if cost is None and filled and average:
            cost = filled * average
        fee = self._parse_order_fee(raw, symbol)
        raw_status = raw.get("status")
        parsed_status = XCOIN_STATUS_MAP.get(
            str(raw_status).lower(), raw_status or "open"
        )
        order_side = raw.get("side") or side
        return {
            "id": raw.get("orderId"),
            "clientOrderId": raw.get("clientOrderId") or None,
            "timestamp": timestamp,
            "datetime": self.iso8601(timestamp),
            "lastTradeTimestamp": int(raw["updateTime"]) if raw.get("updateTime") else None,
            "symbol": symbol,
            "type": (raw.get("orderType") or order_type or "limit").lower(),
            "timeInForce": (raw.get("timeInForce") or "").upper() or None,
            "side": order_side.lower() if isinstance(order_side, str) else order_side,
            "price": price,
            "average": average,
            "amount": amount,
            "filled": filled,
            "remaining": max((amount or 0.0) - filled, 0.0) if amount is not None else None,
            "cost": cost,
            "status": status or parsed_status,
            "fee": fee,
            "trades": [],
            "info": raw,
        }

    def _parse_order_fee(self, raw: dict[str, Any], symbol: str) -> dict[str, Any] | None:
        market = self.markets.get(symbol, {})
        base = market.get("base")
        quote = market.get("quote")
        quote_fee = _clean_float(raw.get("quoteFee"))
        if quote_fee:
            return {"currency": quote, "cost": abs(quote_fee), "rate": None}
        base_fee = _clean_float(raw.get("baseFee"))
        if base_fee:
            return {"currency": base, "cost": abs(base_fee), "rate": None}
        return None

    def create_order(
        self,
        symbol: str,
        order_type: str,
        side: str,
        amount: float,
        price: float | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        order_type = order_type.lower()
        if order_type not in {"limit", "market", "post_only"}:
            raise ccxt.InvalidOrder(
                "XCoin adapter only supports limit/market/post_only orders"
            )
        if order_type in {"limit", "post_only"} and price is None:
            raise ccxt.InvalidOrder("XCoin limit orders require a price")
        params = params or {}
        is_futures = self._is_futures_symbol(symbol)
        if order_type == "market":
            time_in_force = "ioc"
        else:
            time_in_force = (
                params.get("timeInForce") or params.get("time_in_force") or "gtc"
            ).lower()
        if order_type == "post_only" and time_in_force != "gtc":
            raise ccxt.InvalidOrder("XCoin post_only orders only support gtc timeInForce")

        # ``amount`` arrives in contracts for futures (Freqtrade._amount_to_contracts);
        # XCoin expects the order quantity denominated in coin: coin_qty = contracts * ctVal.
        if is_futures:
            contract_size = self._contract_size(symbol)
            decimals = int(self._market(symbol).get("info", {}).get("quantityPrecision") or 8)
            qty_value: float = round(amount * contract_size, decimals)
            market_unit = "baseCoin"
        else:
            qty_value = amount
            market_unit = params.get("marketUnit") or params.get("market_unit") or "baseCoin"

        body: dict[str, Any] = {
            "symbol": self.market_id(symbol),
            "side": side.lower(),
            "orderType": order_type,
            "qty": _str_num(qty_value),
            "marketUnit": market_unit,
            "timeInForce": time_in_force,
            "clientOrderId": params.get("clientOrderId"),
        }
        if order_type in {"limit", "post_only"}:
            body["price"] = _str_num(price)
        if is_futures:
            reduce_only = params.get("reduceOnly")
            if reduce_only is not None:
                body["reduceOnly"] = bool(reduce_only)
            if params.get("tpslOrder") is not None:
                body["tpslOrder"] = params.get("tpslOrder")

        payload = self.client.place_order(body)
        raw = {
            **(payload.get("data") or {}),
            "symbol": self.market_id(symbol),
            "side": side.lower(),
            "orderType": order_type,
            # Report amounts back in contracts so Freqtrade._order_contracts_to_amount
            # can convert them to base units with ctVal.
            "qty": _str_num(amount),
            "price": _str_num(price),
            "status": "new",
            "ts": payload.get("ts"),
        }
        return self._parse_order(
            raw,
            symbol=symbol,
            amount=amount,
            price=price,
            order_type=order_type,
            side=side.lower(),
            status="open",
            timestamp=int(payload.get("ts") or self.milliseconds()),
        )

    def cancel_order(
        self, order_id: str, symbol: str | None = None, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        request = {
            "orderFilter": "order",
            "orderId": order_id,
            **(params or {}),
        }
        if symbol:
            request["symbol"] = self.market_id(symbol)
        payload = self.client.cancel_order(request)
        raw = {
            **(payload.get("data") or {}),
            "symbol": self.market_id(symbol) if symbol else None,
            "status": "canceled",
            "ts": payload.get("ts"),
        }
        return self._parse_order(
            raw,
            symbol=symbol,
            status="canceled",
            timestamp=int(payload["ts"]),
        )

    def fetch_order(
        self, order_id: str, symbol: str | None = None, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        payload = self.client.order_info(
            {"orderId": order_id, "orderFilter": "order", **(params or {})}
        )
        return self._parse_order(payload.get("data") or {}, symbol=symbol)

    def fetch_open_orders(
        self,
        symbol: str | None = None,
        since: int | None = None,
        limit: int | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        business_type = self._business_type(symbol) if symbol else self.default_business_type
        payload = self.client.open_orders(
            {
                "businessType": business_type,
                "symbol": self.market_id(symbol) if symbol else None,
                "orderFilter": "order",
                **(params or {}),
            }
        )
        orders = [self._parse_order(item) for item in payload.get("data", [])]
        return orders[:limit] if limit else orders

    def _parse_position(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        position_qty = _clean_float(raw.get("positionQty"))
        if position_qty is None:
            return None
        symbol = xcoin_symbol_to_ccxt(raw.get("symbol") or "")
        # XCoin reports the position size in coin; express it in contracts for ccxt.
        contract_size = self._contract_size(symbol)
        contracts = abs(position_qty) / contract_size if contract_size else abs(position_qty)
        if position_qty > 0:
            side: str | None = "long"
        elif position_qty < 0:
            side = "short"
        else:
            side = None
        initial_margin = _clean_float(raw.get("im"))
        return {
            "symbol": symbol,
            "side": side,
            "contracts": contracts,
            "leverage": _num(raw.get("lever"), 1.0),
            "collateral": initial_margin,
            "initialMargin": initial_margin,
            "maintenanceMargin": _clean_float(raw.get("mm")),
            "liquidationPrice": _clean_float(raw.get("liquidationPrice")),
            "entryPrice": _clean_float(raw.get("avgPrice")),
            "markPrice": _clean_float(raw.get("markPrice")),
            "unrealizedPnl": _clean_float(raw.get("upl")),
            "marginMode": "cross",
            "info": raw,
        }

    def fetch_positions(
        self, symbols: list[str] | None = None, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        symbol_id = None
        if symbols and len(symbols) == 1:
            symbol_id = self.market_id(symbols[0])
        payload = self.client.positions(
            symbol=symbol_id, params=params, business_type=XCOIN_BUSINESS_LINEAR_PERPETUAL
        )
        positions = []
        for raw in payload.get("data", []):
            parsed = self._parse_position(raw)
            if parsed is not None:
                positions.append(parsed)
        if symbols:
            wanted = {xcoin_symbol_to_ccxt(s) for s in symbols}
            positions = [p for p in positions if p["symbol"] in wanted]
        return positions

    def set_leverage(
        self,
        leverage: float,
        symbol: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"lever": _str_num(leverage)}
        if symbol:
            body["symbol"] = self.market_id(symbol)
        body.update(params or {})
        return self.client.set_leverage(body)

    def fetch_funding_rate_history(
        self,
        symbol: str | None = None,
        since: int | None = None,
        limit: int | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if symbol is None:
            raise ccxt.BadRequest("XCoin funding rate history requires a symbol")
        # Freqtrade pages forward from ``since``; XCoin's endpoint is anchored on a
        # window (beginTime alone errors), so derive a forward window from ``since``.
        # Funding settles on a fixed interval (8h is the common default).
        begin_time = since
        end_time = None
        if since is not None:
            funding_interval_ms = 8 * 60 * 60 * 1000
            end_time = since + (limit or 1000) * funding_interval_ms
        payload = self.client.funding_rate_history(
            self.market_id(symbol),
            begin_time=begin_time,
            end_time=end_time,
            limit=limit,
            params=params,
        )
        history = []
        for raw in payload.get("data", []):
            timestamp = int(_num(raw.get("fundingTime"), 0))
            history.append(
                {
                    "symbol": xcoin_symbol_to_ccxt(raw.get("symbol") or symbol),
                    "fundingRate": _clean_float(raw.get("fundingRate")),
                    "timestamp": timestamp,
                    "datetime": self.iso8601(timestamp),
                    "info": raw,
                }
            )
        return sorted(history, key=lambda item: item["timestamp"])

    def calculate_fee(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: float,
        takerOrMaker: str = "maker",
        params: dict[str, Any] | None = None,
        **kwargs,
    ) -> dict[str, Any]:
        market = self.markets.get(symbol, {})
        rate = _num(market.get(takerOrMaker), 0.001)
        return {
            "type": takerOrMaker,
            "currency": market.get("quote"),
            "rate": rate,
            "cost": amount * price * rate,
        }

    def close(self) -> None:
        self.client.close()
        self.session = None


class XCoinAsync(XCoinSync):
    """Async facade used by Freqtrade candle and market refresh paths."""

    async def load_markets(
        self, reload: bool = False, params: dict[str, Any] | None = None
    ) -> dict[str, dict[str, Any]]:
        return await asyncio.to_thread(super().load_markets, reload, params)

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str = "1m",
        since: int | None = None,
        limit: int | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[list[float]]:
        return await asyncio.to_thread(super().fetch_ohlcv, symbol, timeframe, since, limit, params)

    async def fetch_funding_rate_history(
        self,
        symbol: str | None = None,
        since: int | None = None,
        limit: int | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            super().fetch_funding_rate_history, symbol, since, limit, params
        )

    async def close(self) -> None:
        super().close()
