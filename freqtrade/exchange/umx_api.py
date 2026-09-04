"""ccxt-like UMX facade backed by the internal REST connector."""

import asyncio
from copy import deepcopy
from typing import Any

import ccxt
from ccxt import DECIMAL_PLACES

from freqtrade.exchange.umx_connector import (
    UMX_BUSINESS_LINEAR_PERPETUAL,
    UMX_BUSINESS_SPOT,
    UMX_DEFAULT_BASE_URL,
    UMX_PERP_SUFFIX,
    UMX_TIMEFRAMES,
    UMXClient,
    ccxt_symbol_to_umx,
    umx_symbol_to_ccxt,
)
from freqtrade.util.ft_precise import FtPrecise


__all__ = [
    "UMX_DEFAULT_BASE_URL",
    "UMX_HAS",
    "UMX_TIMEFRAMES",
    "UMXAsync",
    "UMXSync",
    "ccxt_symbol_to_umx",
    "umx_symbol_to_ccxt",
]


UMX_HAS = {
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
    "fetchMyTrades": True,
    "fetchTrades": False,
    "cancelOrder": True,
    "createOrder": True,
    "createLimitOrder": True,
    "createMarketOrder": True,
    "watchOHLCV": False,
    # Futures-only endpoints are gated through ``_ft_has_futures`` overrides in umx.py
    # so spot mode keeps reporting them as unsupported.
    "fetchMarkOHLCV": False,
    "fetchIndexOHLCV": False,
    "fetchFundingRate": False,
    "fetchFundingRateHistory": False,
    "fetchFundingHistory": False,
    "fetchLeverage": False,
    "fetchPositions": False,
    "setLeverage": False,
    "setMarginMode": False,
}

UMX_STATUS_MAP = {
    "untrigger": "open",
    # Compatibility is deliberately limited to parsing legacy wire responses.
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


def _str_num(value: float | str | None) -> str | None:
    if value is None:
        return None
    return str(value)


def _str_positive_int(value: float | str | None, name: str) -> str:
    if value is None:
        raise ccxt.BadRequest(f"{name} is required")
    parsed = float(value)
    if parsed <= 0 or not parsed.is_integer():
        raise ccxt.BadRequest(f"{name} must be a positive integer")
    return str(int(parsed))


def _first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


class UMXSync:
    """Small ccxt-compatible subset used by Freqtrade's Exchange base class."""

    id = "umx"
    name = "UMX"
    precisionMode = DECIMAL_PLACES
    timeframes = UMX_TIMEFRAMES
    features = {
        "spot": {"fetchOHLCV": {"limit": 1000}},
        "swap": {"linear": {"fetchOHLCV": {"limit": 1000}}},
    }
    has = UMX_HAS
    session = None

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        config = config or {}
        self.client = UMXClient(config)
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
        self.default_business_type = config.get("default_business_type") or UMX_BUSINESS_SPOT
        self.funding_fee_timeframe = str(config.get("funding_fee_timeframe") or "1h")
        try:
            self.funding_fee_timeframe_ms = (
                ccxt.Exchange.parse_timeframe(self.funding_fee_timeframe) * 1000
            )
        except (TypeError, ValueError) as exc:
            raise ccxt.BadRequest(
                f"Invalid UMX funding history shard timeframe: {self.funding_fee_timeframe!r}"
            ) from exc
        if self.funding_fee_timeframe_ms <= 0:
            raise ccxt.BadRequest(
                f"Invalid UMX funding history shard timeframe: {self.funding_fee_timeframe!r}"
            )
        self.markets: dict[str, dict[str, Any]] = {}
        self.markets_by_id: dict[str, dict[str, Any]] = {}
        self.options = {
            "createMarketBuyOrderRequiresPrice": False,
            "timeframes": {"spot": UMX_TIMEFRAMES, "swap": UMX_TIMEFRAMES},
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
        return ccxt_symbol_to_umx(symbol)

    def _market(self, symbol: str) -> dict[str, Any]:
        return self.markets.get(umx_symbol_to_ccxt(symbol)) or {}

    def _is_futures_symbol(self, symbol: str) -> bool:
        """Decide whether a ccxt symbol refers to a linear-perpetual contract."""
        market = self._market(symbol)
        if market:
            return market.get("swap", False) is True
        return ":" in symbol or symbol.endswith(f"-{UMX_PERP_SUFFIX}")

    def _business_type(self, symbol: str) -> str:
        return (
            UMX_BUSINESS_LINEAR_PERPETUAL if self._is_futures_symbol(symbol) else UMX_BUSINESS_SPOT
        )

    def _contract_size(self, symbol: str) -> float:
        size = self._market(symbol).get("contractSize")
        return float(size) if size else 1.0

    def _coin_amount_to_contracts(self, symbol: str, amount: float | None) -> float | None:
        """Convert UMX futures coin-denominated quantities to ccxt contracts."""
        if amount is None or not self._is_futures_symbol(symbol):
            return amount
        contract_size = self._contract_size(symbol)
        return float(FtPrecise(amount) / FtPrecise(contract_size)) if contract_size else amount

    def _symbol_family(self, symbol: str) -> str:
        family = self._market(symbol).get("info", {}).get("symbolFamily")
        if family:
            return family
        market_id = self.market_id(symbol)
        return (
            market_id[: -(len(UMX_PERP_SUFFIX) + 1)]
            if market_id.endswith(f"-{UMX_PERP_SUFFIX}")
            else market_id
        )

    def _base_currency(self, symbol: str) -> str:
        market = self._market(symbol)
        if base := market.get("base"):
            return str(base)
        market_id = self.market_id(symbol)
        return market_id.split("-")[0]

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
        is_perp = business_type == UMX_BUSINESS_LINEAR_PERPETUAL
        symbol = umx_symbol_to_ccxt(raw["symbol"])
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

        # ccxt expresses contract amounts in *contracts*; convert UMX's coin-denominated
        # order limits (and amount precision) to whole contracts when ``ctVal`` is known.
        if is_perp and contract_size:
            min_amount = _clean_float(order_params.get("minOrderQty"))
            min_amount = min_amount / contract_size if min_amount is not None else None
            max_amount_value = _clean_float(max_amount)
            max_amount_value = (
                max_amount_value / contract_size if max_amount_value is not None else None
            )
            # UMX trades whole contracts, so contract amounts have no decimal places.
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
            "spot": business_type == UMX_BUSINESS_SPOT,
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
            # UMX perpetual trading fees are account-tier specific and not exposed by the
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
        symbol = umx_symbol_to_ccxt(raw["symbol"])
        return {
            "symbol": symbol,
            "timestamp": timestamp,
            "datetime": self.iso8601(timestamp),
            "high": None,
            "low": None,
            "bid": bid,
            "bidVolume": None,
            "ask": ask,
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
            raise ccxt.BadSymbol(f"UMX ticker not found for {symbol}")
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
                self._parse_ticker(item, timestamp=timestamp) for item in payload.get("data", [])
            )
        }
        if symbols:
            symbols = [umx_symbol_to_ccxt(s) for s in symbols]
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
            "symbol": umx_symbol_to_ccxt(symbol),
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
            raise ccxt.BadRequest(f"Unsupported UMX timeframe: {timeframe}")
        params = dict(params or {})
        # Freqtrade routes mark/index candles through params["price"] = str(candle_type).
        price = params.pop("price", None)
        period = self.timeframes[timeframe]
        market_id = self.market_id(symbol)
        business_type = self._business_type(symbol)

        if price == "mark":
            payload = self.client.mark_price_klines(
                market_id,
                period,
                since=since,
                limit=limit,
                params=params,
                business_type=business_type,
            )
            return self._parse_priced_klines(payload)
        if price == "index":
            payload = self.client.index_price_klines(
                self._symbol_family(symbol), period, since=since, limit=limit, params=params
            )
            return self._parse_priced_klines(payload)

        payload = self.client.klines(
            market_id,
            period,
            since=since,
            limit=limit,
            params=params,
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
        if self.default_business_type == UMX_BUSINESS_LINEAR_PERPETUAL and total_equity is not None:
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
        symbol = umx_symbol_to_ccxt(raw.get("symbol") or symbol or "")
        timestamp = int(raw.get("createTime") or timestamp or raw.get("ts") or self.milliseconds())
        raw_amount = _clean_float(raw.get("qty")) if raw.get("qty") is not None else None
        price = _clean_float(raw.get("price")) if raw.get("price") is not None else price
        raw_filled = _num(raw.get("totalFillQty"), 0.0)
        average = _clean_float(raw.get("avgPrice"))
        cost = _clean_float(raw.get("quoteQty"))
        if cost is None and raw_filled and average:
            cost = raw_filled * average
        if raw_amount is not None:
            amount = self._coin_amount_to_contracts(symbol, raw_amount)
        filled = self._coin_amount_to_contracts(symbol, raw_filled) or 0.0
        fee = self._parse_order_fee(raw, symbol)
        raw_status = raw.get("status")
        parsed_status = UMX_STATUS_MAP.get(str(raw_status).lower(), raw_status or "open")
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
        if quote_fee is not None:
            # UMX reports deductions as negative and rebates as positive. CCXT's fee
            # cost uses the opposite convention: costs are positive, rebates negative.
            return {"currency": quote, "cost": -quote_fee, "rate": None}
        base_fee = _clean_float(raw.get("baseFee"))
        if base_fee is not None:
            return {"currency": base, "cost": -base_fee, "rate": None}
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
            raise ccxt.InvalidOrder("UMX adapter only supports limit/market/post_only orders")
        if order_type in {"limit", "post_only"} and price is None:
            raise ccxt.InvalidOrder("UMX limit orders require a price")
        params = params or {}
        is_futures = self._is_futures_symbol(symbol)
        if order_type == "market":
            time_in_force = "ioc"
        else:
            time_in_force = (
                params.get("timeInForce") or params.get("time_in_force") or "gtc"
            ).lower()
        if order_type == "post_only" and time_in_force != "gtc":
            raise ccxt.InvalidOrder("UMX post_only orders only support gtc timeInForce")

        # ``amount`` arrives in contracts for futures (Freqtrade._amount_to_contracts);
        # UMX expects the order quantity denominated in coin: coin_qty = contracts * ctVal.
        if is_futures:
            contract_size = self._contract_size(symbol)
            decimals = int(self._market(symbol).get("info", {}).get("quantityPrecision") or 8)
            qty_value: float = round(amount * contract_size, decimals)
            market_unit = "baseCoin"
        else:
            qty_value = amount
            market_unit = params.get("marketUnit") or params.get("market_unit") or "baseCoin"

        # UMX defaults isLeverage to true, which means leveraged spot. Freqtrade's spot
        # adapter always opts out. Contract routing uses the perpetual symbol, so this
        # spot-only switch is deliberately absent from futures requests.
        spot_leverage_opt_out = {"isLeverage": False} if not is_futures else {}
        body: dict[str, Any] = {
            **spot_leverage_opt_out,
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

        if is_futures:
            leverage = self.fetch_leverage(symbol)
            actual_leverage = _first_present(
                leverage.get("longLeverage"), leverage.get("shortLeverage")
            )
            if actual_leverage is None or float(actual_leverage) != 1.0:
                raise ccxt.InvalidOrder(
                    f"UMX order blocked for {symbol}: symbol-level leverage readback must be 1x; "
                    f"received {actual_leverage!r}."
                )

        payload = self.client.place_order(body)
        raw = {
            **(payload.get("data") or {}),
            "symbol": self.market_id(symbol),
            "side": side.lower(),
            "orderType": order_type,
            "qty": _str_num(qty_value),
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
            timestamp=int(payload.get("ts") or self.milliseconds()),
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

    def _parse_trade(self, raw: dict[str, Any]) -> dict[str, Any]:
        symbol = umx_symbol_to_ccxt(raw.get("symbol") or "")
        raw_amount = _num(raw.get("fillQty"), 0.0)
        amount = self._coin_amount_to_contracts(symbol, raw_amount) or 0.0
        price = _clean_float(raw.get("fillPrice"))
        timestamp = int(_num(raw.get("fillTime"), 0.0))
        raw_fee = _clean_float(raw.get("fee"))
        fee = (
            {
                "currency": raw.get("feeCurrency") or None,
                # UMX: negative means charged, positive means rebate. CCXT is opposite.
                "cost": -raw_fee,
                "rate": None,
            }
            if raw_fee is not None
            else None
        )
        role = str(raw.get("role") or "").lower()
        taker_or_maker = role if role in {"maker", "taker"} else None
        return {
            "id": str(raw.get("tradeId") or raw.get("id") or "") or None,
            "order": str(raw.get("orderId") or "") or None,
            "timestamp": timestamp,
            "datetime": self.iso8601(timestamp),
            "symbol": symbol,
            "type": str(raw.get("orderType") or "").lower() or None,
            "side": str(raw.get("side") or "").lower() or None,
            "takerOrMaker": taker_or_maker,
            "price": price,
            "amount": amount,
            "cost": raw_amount * price if price is not None else None,
            "fee": fee,
            "fees": [fee] if fee else [],
            "leverage": _clean_float(raw.get("lever")),
            "info": raw,
        }

    def fetch_my_trades(
        self,
        symbol: str | None = None,
        since: int | None = None,
        limit: int | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        business_type = self._business_type(symbol) if symbol else self.default_business_type
        page_size = min(limit or 100, 100)
        page_params = dict(params or {})
        rows: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        previous_cursor: str | None = None
        while True:
            payload = self.client.trade_history(
                symbol=self.market_id(symbol) if symbol else None,
                business_type=business_type,
                begin_time=since,
                limit=page_size,
                params=page_params,
            )
            page = payload.get("data", [])
            for raw in page:
                row_id = str(raw.get("id") or raw.get("tradeId") or "")
                if row_id and row_id in seen_ids:
                    continue
                if row_id:
                    seen_ids.add(row_id)
                rows.append(raw)
            if len(page) < page_size or (limit is not None and len(rows) >= limit):
                break
            cursor = str(page[-1].get("id") or "")
            if not cursor or cursor == previous_cursor:
                raise ccxt.ExchangeError("UMX trade-history pagination did not advance")
            previous_cursor = cursor
            page_params["endId"] = cursor

        trades = [self._parse_trade(raw) for raw in rows]
        if limit is not None:
            trades = trades[:limit]
        return sorted(trades, key=lambda trade: trade["timestamp"])

    def _parse_position(self, raw: dict[str, Any]) -> dict[str, Any] | None:
        position_qty = _clean_float(raw.get("positionQty"))
        if position_qty is None:
            return None
        symbol = umx_symbol_to_ccxt(raw.get("symbol") or "")
        # UMX reports the position size in coin; express it in contracts for ccxt.
        contracts = self._coin_amount_to_contracts(symbol, abs(position_qty))
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
            symbol=symbol_id, params=params, business_type=UMX_BUSINESS_LINEAR_PERPETUAL
        )
        positions = []
        for raw in payload.get("data", []):
            parsed = self._parse_position(raw)
            if parsed is not None:
                positions.append(parsed)
        if symbols:
            wanted = {umx_symbol_to_ccxt(s) for s in symbols}
            positions = [p for p in positions if p["symbol"] in wanted]
        return positions

    def set_leverage(
        self,
        leverage: float,
        symbol: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if symbol is None:
            raise ccxt.BadRequest("UMX leverage changes require a symbol")
        if "currency" in (params or {}):
            raise ccxt.BadRequest("UMX leverage changes must use symbol scope, not currency scope")
        if float(leverage) != 1.0:
            raise ccxt.InvalidOrder(
                f"UMX permits only 1x leverage through this adapter; received {leverage!r}."
            )
        body: dict[str, Any] = {
            **(params or {}),
            "lever": _str_positive_int(leverage, "lever"),
            "symbol": self.market_id(symbol),
        }
        return self.client.set_leverage(body)

    def fetch_leverage(
        self, symbol: str | None = None, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if symbol is None:
            raise ccxt.BadRequest("UMX leverage readback requires a symbol")
        if "currency" in (params or {}):
            raise ccxt.BadRequest("UMX leverage readback must use symbol scope, not currency scope")
        payload = self.client.leverage(self.market_id(symbol), params)
        raw = payload.get("data") or {}
        leverage = _clean_float(raw.get("lever"))
        return {
            "info": raw,
            "symbol": umx_symbol_to_ccxt(raw.get("symbol") or self.market_id(symbol)),
            "marginMode": "cross",
            "longLeverage": leverage,
            "shortLeverage": leverage,
        }

    def fetch_funding_rate(
        self, symbol: str | None = None, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if symbol is None:
            raise ccxt.BadRequest("UMX current funding rate requires a symbol")
        market_id = self.market_id(symbol)
        payload = self.client.funding_rate(market_id, params)
        data = payload.get("data") or []
        rows = [data] if isinstance(data, dict) else data
        raw = next((item for item in rows if item.get("symbol") == market_id), None)
        if raw is None:
            raise ccxt.BadSymbol(f"UMX funding rate not found for {symbol}")

        response_timestamp_value = int(_num(payload.get("ts"), 0))
        response_timestamp = response_timestamp_value or None
        funding_timestamp_value = int(_num(raw.get("fundingTime"), 0))
        funding_timestamp = funding_timestamp_value or None
        interval_hours = _clean_float(raw.get("fundingInterval"))
        interval = f"{interval_hours:g}h" if interval_hours is not None else None
        return {
            "info": raw,
            "symbol": umx_symbol_to_ccxt(raw.get("symbol") or market_id),
            "markPrice": None,
            "indexPrice": None,
            "interestRate": None,
            "estimatedSettlePrice": None,
            "timestamp": response_timestamp,
            "datetime": self.iso8601(response_timestamp) if response_timestamp else None,
            "fundingRate": _clean_float(raw.get("fundingRate")),
            "fundingTimestamp": funding_timestamp,
            "fundingDatetime": self.iso8601(funding_timestamp) if funding_timestamp else None,
            "nextFundingRate": None,
            "nextFundingTimestamp": None,
            "nextFundingDatetime": None,
            "previousFundingRate": None,
            "previousFundingTimestamp": None,
            "previousFundingDatetime": None,
            # UMX documents fundingInterval in hours and can vary it by symbol.
            "interval": interval,
        }

    def fetch_funding_rate_history(
        self,
        symbol: str | None = None,
        since: int | None = None,
        limit: int | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if symbol is None:
            raise ccxt.BadRequest("UMX funding rate history requires a symbol")
        request_params = dict(params or {})
        explicit_end = request_params.pop("endTime", None)
        end_time: int | None = None
        if explicit_end is not None:
            try:
                end_time = int(explicit_end)
            except (TypeError, ValueError) as exc:
                raise ccxt.BadRequest(
                    "UMX funding history endTime must be integer milliseconds"
                ) from exc
            if end_time <= 0:
                raise ccxt.BadRequest("UMX funding history endTime must be positive")
        if since is not None:
            since = int(since)
            if end_time is not None and end_time <= since:
                raise ccxt.BadRequest("UMX funding history endTime must be greater than since")
            if end_time is None:
                # Bound this request to the same logical shard that Freqtrade generated.
                # The 1h default is a download-grid boundary only; actual UMX settlements
                # can occur on other intervals and are parsed from their returned timestamps.
                end_time = since + (limit or 1000) * self.funding_fee_timeframe_ms
        payload = self.client.funding_rate_history(
            self.market_id(symbol),
            begin_time=since,
            end_time=end_time,
            limit=limit,
            params=request_params,
        )
        history = []
        for raw in payload.get("data", []):
            timestamp = int(_num(raw.get("fundingTime"), 0))
            history.append(
                {
                    "symbol": umx_symbol_to_ccxt(raw.get("symbol") or symbol),
                    "fundingRate": _clean_float(raw.get("fundingRate")),
                    "timestamp": timestamp,
                    "datetime": self.iso8601(timestamp),
                    "info": raw,
                }
            )
        return sorted(history, key=lambda item: item["timestamp"])

    @staticmethod
    def _funding_bill_windows(
        begin_time: int | None, end_time: int, explicit_end: int | None
    ) -> list[tuple[int | None, int | None]]:
        if begin_time is None:
            return [(None, explicit_end)]
        thirty_days_ms = 30 * 24 * 60 * 60 * 1000
        windows: list[tuple[int | None, int | None]] = []
        window_start = begin_time
        while window_start <= end_time:
            window_end = min(window_start + thirty_days_ms, end_time)
            windows.append((window_start, window_end))
            window_start = window_end + 1
        return windows

    def _fetch_funding_bill_rows(
        self,
        windows: list[tuple[int | None, int | None]],
        page_size: int,
        request_params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for window_start, window_end in windows:
            page_params = dict(request_params)
            previous_cursor: str | None = None
            while True:
                payload = self.client.bills(
                    business_type=UMX_BUSINESS_LINEAR_PERPETUAL,
                    action_type="18",
                    begin_time=window_start,
                    end_time=window_end,
                    limit=page_size,
                    params=page_params,
                )
                page = payload.get("data", [])
                for raw in page:
                    row_id = str(raw.get("id") or raw.get("actionId") or "")
                    if row_id and row_id in seen_ids:
                        continue
                    if row_id:
                        seen_ids.add(row_id)
                    rows.append(raw)
                if len(page) < page_size:
                    break
                cursor = str(page[-1].get("id") or "")
                if not cursor or cursor == previous_cursor:
                    raise ccxt.ExchangeError("UMX funding bill pagination did not advance")
                previous_cursor = cursor
                page_params["endId"] = cursor
        return rows

    def _parse_funding_bill(
        self,
        raw: dict[str, Any],
        symbol: str | None,
        begin_time: int | None,
        end_time: int,
    ) -> dict[str, Any] | None:
        if str(raw.get("actionType")) != "18":
            return None
        if symbol and raw.get("symbol") != self.market_id(symbol):
            return None
        timestamp = int(_num(raw.get("createTime"), 0.0))
        if (begin_time is not None and timestamp < begin_time) or timestamp > end_time:
            return None
        return {
            "info": raw,
            "symbol": umx_symbol_to_ccxt(raw.get("symbol") or symbol or ""),
            "code": raw.get("currency") or None,
            "timestamp": timestamp,
            "datetime": self.iso8601(timestamp),
            "id": str(raw.get("id") or raw.get("actionId") or "") or None,
            # UMX qty already uses account perspective: negative paid, positive received.
            "amount": _num(raw.get("qty"), 0.0),
        }

    def fetch_funding_history(
        self,
        symbol: str | None = None,
        since: int | None = None,
        limit: int | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        request_params = dict(params or {})
        request_params.pop("actionType", None)
        explicit_begin = request_params.pop("beginTime", None)
        explicit_end = request_params.pop("endTime", None)
        begin_time = since if since is not None else explicit_begin
        end_time = int(explicit_end) if explicit_end is not None else self.milliseconds()
        if begin_time is not None:
            ninety_days_ms = 90 * 24 * 60 * 60 * 1000
            begin_time = max(int(begin_time), end_time - ninety_days_ms)

        # The private bill API accepts windows of at most 30 days and exposes only the
        # most recent 90 days. Page each window backwards by endId so 4h settlements and
        # multi-symbol accounts are not silently truncated at the endpoint's 100-row cap.
        windows = self._funding_bill_windows(
            begin_time, end_time, int(explicit_end) if explicit_end is not None else None
        )
        rows = self._fetch_funding_bill_rows(windows, min(limit or 100, 100), request_params)
        history = [
            parsed
            for raw in rows
            if (parsed := self._parse_funding_bill(raw, symbol, begin_time, end_time)) is not None
        ]
        history = sorted(history, key=lambda item: item["timestamp"])
        return history[:limit] if limit is not None else history

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


class UMXAsync(UMXSync):
    """Async facade used by Freqtrade candle and market refresh paths."""

    async def load_markets(  # type: ignore[override]
        self, reload: bool = False, params: dict[str, Any] | None = None
    ) -> dict[str, dict[str, Any]]:
        return await asyncio.to_thread(super().load_markets, reload, params)

    async def fetch_ohlcv(  # type: ignore[override]
        self,
        symbol: str,
        timeframe: str = "1m",
        since: int | None = None,
        limit: int | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[list[float]]:
        return await asyncio.to_thread(super().fetch_ohlcv, symbol, timeframe, since, limit, params)

    async def fetch_funding_rate_history(  # type: ignore[override]
        self,
        symbol: str | None = None,
        since: int | None = None,
        limit: int | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        return await asyncio.to_thread(
            super().fetch_funding_rate_history, symbol, since, limit, params
        )

    async def close(self) -> None:  # type: ignore[override]
        super().close()
