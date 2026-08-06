"""Binance exchange subclass"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from math import floor, isclose, isfinite
from pathlib import Path
from threading import Lock
from time import sleep
from typing import Any
from uuid import uuid4

import ccxt
from pandas import DataFrame

from freqtrade.constants import DEFAULT_DATAFRAME_COLUMNS, BuySell
from freqtrade.enums import TRADE_MODES, CandleType, MarginMode, PriceType, RunMode, TradingMode
from freqtrade.exceptions import (
    DDosProtection,
    FreqtradeException,
    InsufficientFundsError,
    InvalidOrderException,
    OperationalException,
    RetryableOrderError,
    TemporaryError,
)
from freqtrade.exchange import Exchange
from freqtrade.exchange.binance_public_data import (
    concat_safe,
    download_archive_ohlcv,
    download_archive_trades,
)
from freqtrade.exchange.common import retrier
from freqtrade.exchange.exchange_types import CcxtOrder, FtHas, Tickers
from freqtrade.exchange.exchange_utils import ROUND_DOWN, ROUND_UP
from freqtrade.exchange.exchange_utils_timeframe import timeframe_to_msecs
from freqtrade.misc import deep_merge_dicts, json_load
from freqtrade.util import FtTTLCache
from freqtrade.util.datetime_helpers import dt_from_ts, dt_ts


logger = logging.getLogger(__name__)


class Binance(Exchange):
    _portfolio_order_recovery_attempts = 3
    _portfolio_order_recovery_delay = 1.0
    _portfolio_stop_order_recovery_attempts = 1
    _portfolio_containment_attempts = 10
    _portfolio_containment_stable_snapshots = 3
    _portfolio_conditional_cleanup_attempts = 6
    _portfolio_conditional_cleanup_stable_snapshots = 3
    _portfolio_startup_snapshot_attempts = 3
    _portfolio_request_timeout_ms = 5_000

    """Binance exchange class.
    Contains adjustments needed for Freqtrade to work with this exchange.
    """

    _ft_has: FtHas = {
        "stoploss_on_exchange": True,
        "stop_price_param": "stopPrice",
        "stop_price_prop": "stopPrice",
        "stoploss_order_types": {"limit": "stop_loss_limit"},
        "stoploss_blocks_assets": True,  # By default stoploss orders block assets
        "order_time_in_force": ["GTC", "FOK", "IOC", "PO"],
        "trades_pagination": "id",
        "trades_pagination_arg": "fromId",
        "trades_has_history": True,
        "fetch_orders_limit_minutes": None,
        "l2_limit_range": [5, 10, 20, 50, 100, 500, 1000],
        "ws_enabled": True,
        "has_delisting": True,
        # Demo trading
        # https://www.binance.com/en/support/faq/detail/9be58f73e5e14338809e3b705b9687dd
        # Intentionally Disabled as it's a separate market - not a simulated live market.
        "supports_demo_trading": False,
    }
    _ft_has_futures: FtHas = {
        "ohlcv_candle_limit": 499,
        "funding_fee_candle_limit": 1000,
        "stoploss_order_types": {"limit": "stop", "market": "stop_market"},
        "stoploss_blocks_assets": False,  # Stoploss orders do not block assets
        "stoploss_query_requires_stop_flag": True,
        "stoploss_algo_order_info_id": "actualOrderId",
        "tickers_have_price": False,
        "floor_leverage": True,
        "fetch_orders_limit_minutes": 7 * 1440,  # "fetch_orders" is limited to 7 days
        "stop_price_type_field": "workingType",
        "order_props_in_contracts": ["amount", "cost", "filled", "remaining"],
        "stop_price_type_value_mapping": {
            PriceType.LAST: "CONTRACT_PRICE",
            PriceType.MARK: "MARK_PRICE",
        },
        "ws_enabled": False,
        # ccxt maps "total" to assets[].marginBalance (= walletBalance + unrealizedProfit)
        "balance_includes_unrealized_pnl": True,
        "proxy_coin_mapping": {
            "BNFCR": "USDC",
            "BFUSD": "USDT",
        },
    }
    _can_use_data_download_fast = True

    _supported_trading_mode_margin_pairs: list[tuple[TradingMode, MarginMode]] = [
        (TradingMode.SPOT, MarginMode.NONE),
        # (TradingMode.MARGIN, MarginMode.CROSS),
        (TradingMode.FUTURES, MarginMode.CROSS),
        (TradingMode.FUTURES, MarginMode.ISOLATED),
    ]

    def __init__(self, *args, **kwargs) -> None:
        # Keep destruction safe if explicit Portfolio Margin validation fails before
        # Exchange.__init__ has initialized these lifecycle attributes.
        self._exchange_ws = None
        self._ws_async = None
        self.loop = None  # type: ignore[assignment]
        config = args[0] if args else kwargs["config"]
        exchange_config = config.get("exchange", {})
        options = exchange_config.get("ccxt_config", {}).get("options", {})
        self._portfolio_margin = (
            isinstance(options, dict) and options.get("portfolioMargin") is True
        )
        risk_config = exchange_config.get("portfolio_margin_risk")
        self._portfolio_margin_risk = risk_config if isinstance(risk_config, dict) else None
        self._portfolio_create_lock = (
            Lock() if self._portfolio_margin and not config.get("dry_run", True) else None
        )
        self._portfolio_active_client_order_id: str | None = None
        self._portfolio_unknown_conditional_client_order_id: str | None = None
        self._portfolio_unknown_conditional_pair: str | None = None
        self._portfolio_unknown_order_latched = False
        if self._portfolio_margin:
            self._validate_portfolio_margin_config(config, options)
        elif self._has_implicit_portfolio_margin_routing(exchange_config):
            raise OperationalException(
                "Binance Portfolio Margin must be enabled explicitly with "
                "exchange.ccxt_config.options.portfolioMargin=true. PAPI routing "
                "overrides cannot be used with the ordinary Binance adapter."
            )
        super().__init__(*args, **kwargs)
        self._spot_delist_schedule_cache: FtTTLCache = FtTTLCache(maxsize=100, ttl=300)

    @property
    def portfolio_margin_unknown_order_latched(self) -> bool:
        """Expose the fail-closed latch without leaking exchange or order details."""
        return self._portfolio_margin and self._portfolio_unknown_order_latched

    @property
    def portfolio_margin_enabled(self) -> bool:
        """Expose whether this adapter instance explicitly uses Portfolio Margin."""
        return self._portfolio_margin

    def _get_portfolio_create_lock(self) -> Any:
        """Return the live PAPI order lock, failing closed if it was not initialized."""
        portfolio_create_lock = self._portfolio_create_lock
        if portfolio_create_lock is None:
            raise OperationalException(
                "Binance Portfolio Margin live order serialization is unavailable. "
                "Trading remains stopped."
            )
        return portfolio_create_lock

    @staticmethod
    def _nested_option_dicts(options: dict) -> list[dict]:
        """Return option dictionaries, including method-specific CCXT overrides."""
        result = [options]
        for value in options.values():
            if isinstance(value, dict):
                result.extend(Binance._nested_option_dicts(value))
        return result

    @staticmethod
    def _has_implicit_portfolio_margin_routing(exchange_config: dict) -> bool:
        """Reject mixed FAPI/PAPI mode unless the dedicated main switch is enabled."""
        for config_key in ("ccxt_config", "ccxt_sync_config", "ccxt_async_config"):
            options = exchange_config.get(config_key, {}).get("options", {})
            if not isinstance(options, dict):
                continue
            for option_set in Binance._nested_option_dicts(options):
                if any(
                    flag in option_set and option_set[flag] not in (False, None)
                    for flag in (
                        "papi",
                        "defaultPapi",
                        "portfolioMargin",
                        "defaultPortfolioMargin",
                        "portfolioMarginPro",
                        "defaultPortfolioMarginPro",
                        "papiV2",
                        "defaultPapiV2",
                    )
                ):
                    return True
        return False

    @property
    def _ccxt_config(self) -> dict:
        config = super()._ccxt_config
        if not self._portfolio_margin:
            return config
        # With credentials present, Binance fetchCurrencies uses signed SAPI. Portfolio
        # Margin startup must not leave PAPI, and account-wide reconciliation deliberately
        # acknowledges CCXT's stricter no-symbol open-order rate limit.
        portfolio_config = {
            "timeout": self._portfolio_request_timeout_ms,
            "options": {
                "defaultSubType": "linear",
                "fetchCurrencies": False,
                "fetchMarkets": {"types": ["linear"]},
                "fetchOpenOrders": {"warnWithoutSymbol": False},
                "fetchPositions": {"method": "positionRisk"},
                "maxRetriesOnFailure": 0,
                "useV2": False,
                "warnOnFetchOpenOrdersWithoutSymbol": False,
            },
        }
        return deep_merge_dicts(portfolio_config, config)

    @staticmethod
    def _validate_portfolio_margin_config(config: dict, options: dict) -> None:  # noqa: C901
        """Validate the deliberately narrow Portfolio Margin v1 feature set."""
        trading_mode = TradingMode(config.get("trading_mode", TradingMode.SPOT))
        margin_mode = MarginMode(config.get("margin_mode") or MarginMode.NONE)
        if trading_mode != TradingMode.FUTURES or margin_mode != MarginMode.CROSS:
            raise OperationalException(
                "Binance Portfolio Margin only supports futures trading with cross margin. "
                "Set `trading_mode: futures` and `margin_mode: cross`."
            )
        exchange_config = config.get("exchange", {})
        risk_config = exchange_config.get("portfolio_margin_risk")
        if config.get("force_entry_enable") is True and not isinstance(risk_config, dict):
            raise OperationalException(
                "Binance Portfolio Margin force-entry requires an explicit "
                "exchange.portfolio_margin_risk policy."
            )
        if risk_config is not None:
            if not isinstance(risk_config, dict):
                raise OperationalException("exchange.portfolio_margin_risk must be an object.")
            allowed_risk_keys = {
                "pair",
                "side",
                "max_leverage",
                "max_entry_notional",
                "force_entry_order_type",
                "reject_force_entry_price",
            }
            if set(risk_config) != allowed_risk_keys:
                raise OperationalException(
                    "Binance Portfolio Margin risk policy must define exactly pair, side, "
                    "max_leverage, max_entry_notional, force_entry_order_type, and "
                    "reject_force_entry_price."
                )
            risk_notional = risk_config.get("max_entry_notional")
            if (
                risk_config.get("pair") not in exchange_config.get("pair_whitelist", [])
                or risk_config.get("side") != "long"
                or isinstance(risk_config.get("max_leverage"), bool)
                or risk_config.get("max_leverage") != 1
                or isinstance(risk_notional, bool)
                or not isinstance(risk_notional, (int, float))
                or not isfinite(risk_notional)
                or not 0 < risk_notional <= 50
                or risk_config.get("force_entry_order_type") != "market"
                or risk_config.get("reject_force_entry_price") is not True
            ):
                raise OperationalException(
                    "Binance Portfolio Margin risk policy must select a whitelisted long-only "
                    "pair, 1x leverage, at most 50 USDT entry notional, market force-entry, "
                    "and explicit-price rejection."
                )
        option_sets = [options]
        for config_key in ("ccxt_sync_config", "ccxt_async_config"):
            ccxt_override = exchange_config.get(config_key, {})
            if not isinstance(ccxt_override, dict):
                raise OperationalException(
                    f"Binance Portfolio Margin exchange.{config_key} must be an object."
                )
            override_options = ccxt_override.get("options", {})
            if not isinstance(override_options, dict):
                raise OperationalException(
                    f"Binance Portfolio Margin {config_key}.options must be an object."
                )
            option_sets.append(override_options)
        for config_key in ("ccxt_config", "ccxt_sync_config", "ccxt_async_config"):
            ccxt_override = exchange_config.get(config_key, {})
            if not isinstance(ccxt_override, dict):
                raise OperationalException(
                    f"Binance Portfolio Margin exchange.{config_key} must be an object."
                )
            timeout = ccxt_override.get("timeout")
            if timeout is not None and (
                isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not 0 < timeout <= Binance._portfolio_request_timeout_ms
            ):
                raise OperationalException(
                    "Binance Portfolio Margin CCXT timeout must be at most 5000 ms "
                    "so missing stop-loss protection can trigger emergency exit "
                    "within 30 seconds."
                )

        route_methods = (
            "fetchPositions",
            "fetchPositionsRisk",
            "loadLeverageBrackets",
            "fetchBalance",
            "createOrder",
            "fetchOrder",
            "fetchOpenOrder",
            "cancelOrder",
            "fetchOpenOrders",
            "fetchOrders",
            "fetchMyTrades",
            "fetchLeverageTiers",
            "setLeverage",
            "fetchFundingHistory",
        )
        for option_set in option_sets:
            route_disabled = any(
                key in option_set and option_set[key] is not True
                for key in (
                    "papi",
                    "defaultPapi",
                    "portfolioMargin",
                    "defaultPortfolioMargin",
                )
            )
            if any(
                key in option_set and option_set[key] not in (False, None)
                for key in (
                    "portfolioMarginPro",
                    "defaultPortfolioMarginPro",
                    "papiV2",
                    "defaultPapiV2",
                    "useV2",
                    "defaultUseV2",
                )
            ):
                raise OperationalException(
                    "Binance Portfolio Margin Pro/PAPI v2 is not supported by this adapter."
                )
            if any(
                key in nested and nested[key] not in (None, 0)
                for nested in Binance._nested_option_dicts(option_set)
                for key in ("maxRetriesOnFailure", "defaultMaxRetriesOnFailure")
            ):
                raise OperationalException(
                    "Binance Portfolio Margin disables CCXT automatic request retries "
                    "to prevent duplicate order submission."
                )
            if "fetchCurrencies" in option_set and option_set["fetchCurrencies"] is not False:
                raise OperationalException(
                    "Binance Portfolio Margin must set CCXT fetchCurrencies=false "
                    "to prevent signed SAPI requests."
                )
            if (
                "warnOnFetchOpenOrdersWithoutSymbol" in option_set
                and option_set["warnOnFetchOpenOrdersWithoutSymbol"] is not False
            ) or (
                isinstance(option_set.get("fetchOpenOrders"), dict)
                and "warnWithoutSymbol" in option_set["fetchOpenOrders"]
                and option_set["fetchOpenOrders"]["warnWithoutSymbol"] is not False
            ):
                raise OperationalException(
                    "Binance Portfolio Margin requires account-wide PAPI open-order "
                    "reconciliation and cannot enable CCXT's no-symbol warning."
                )
            fetch_market_types = option_set.get("fetchMarkets")
            if isinstance(fetch_market_types, dict):
                fetch_market_types = fetch_market_types.get("types")
            if fetch_market_types is not None and fetch_market_types != ["linear"]:
                raise OperationalException(
                    "Binance Portfolio Margin CCXT market loading is restricted to "
                    "linear USD-M markets."
                )
            for method in route_methods:
                method_options = option_set.get(method)
                if (
                    method == "fetchPositions"
                    and method_options is not None
                    and not isinstance(method_options, dict)
                    and method_options != "positionRisk"
                ):
                    raise OperationalException(
                        "Binance Portfolio Margin fetchPositions must use CCXT method=positionRisk."
                    )
                if method_options is not None and not isinstance(method_options, dict):
                    if method == "fetchPositions" and method_options == "positionRisk":
                        continue
                    raise OperationalException(
                        "Binance Portfolio Margin private CCXT method overrides must be objects."
                    )
                if isinstance(method_options, dict):
                    if any(
                        key in method_options and method_options[key] is not True
                        for key in (
                            "papi",
                            "defaultPapi",
                            "portfolioMargin",
                            "defaultPortfolioMargin",
                        )
                    ):
                        route_disabled = True
                    if any(
                        key in method_options and method_options[key] not in (False, None)
                        for key in (
                            "portfolioMarginPro",
                            "defaultPortfolioMarginPro",
                            "papiV2",
                            "defaultPapiV2",
                            "useV2",
                            "defaultUseV2",
                        )
                    ):
                        raise OperationalException(
                            "Binance Portfolio Margin Pro/PAPI v2 method overrides "
                            "are not supported."
                        )
                    if method == "fetchPositions" and (
                        method_options.get("method", "positionRisk") != "positionRisk"
                        or method_options.get("defaultMethod", "positionRisk") != "positionRisk"
                    ):
                        raise OperationalException(
                            "Binance Portfolio Margin fetchPositions must use "
                            "CCXT method=positionRisk."
                        )
                    if (
                        method_options.get("type") not in (None, "swap", "future")
                        or method_options.get("defaultType") not in (None, "swap", "future")
                        or ("subType" in method_options and method_options["subType"] != "linear")
                        or (
                            "defaultSubType" in method_options
                            and method_options["defaultSubType"] != "linear"
                        )
                        or (
                            "marginMode" in method_options
                            and method_options["marginMode"] != "cross"
                        )
                        or (
                            "defaultMarginMode" in method_options
                            and method_options["defaultMarginMode"] != "cross"
                        )
                    ):
                        raise OperationalException(
                            "Binance Portfolio Margin private CCXT method routing "
                            "must remain linear USD-M cross futures."
                        )
            if route_disabled:
                raise OperationalException(
                    "Binance Portfolio Margin configuration cannot disable "
                    "PAPI/portfolioMargin globally or for a private CCXT method."
                )
            if (
                option_set.get("type") not in (None, "swap", "future")
                or option_set.get("defaultType") not in (None, "swap", "future")
                or option_set.get("method") not in (None, "positionRisk")
                or option_set.get("defaultMethod") not in (None, "positionRisk")
                or ("defaultSubType" in option_set and option_set["defaultSubType"] != "linear")
                or ("subType" in option_set and option_set["subType"] != "linear")
                or (
                    "marginMode" in option_set
                    and option_set["marginMode"] != MarginMode.CROSS.value
                )
                or (
                    "defaultMarginMode" in option_set
                    and option_set["defaultMarginMode"] != MarginMode.CROSS.value
                )
            ):
                raise OperationalException(
                    "Binance Portfolio Margin only supports linear USD-M perpetual markets."
                )

    def _portfolio_margin_params(self, params: dict | None = None) -> dict:
        """Force private unified CCXT methods onto linear Portfolio Margin endpoints."""
        result = dict(params or {})
        if not self._portfolio_margin:
            return result
        sub_type = result.pop("subType", None)
        default_sub_type = result.pop("defaultSubType", None)
        default_type = result.pop("defaultType", None)
        margin_mode = result.pop("marginMode", None)
        default_margin_mode = result.pop("defaultMarginMode", None)
        caller_method_name = result.pop("callerMethodName", None)
        request_type = result.get("type")
        if (
            ("portfolioMargin" in result and result["portfolioMargin"] is not True)
            or ("defaultPortfolioMargin" in result and result["defaultPortfolioMargin"] is not True)
            or ("papi" in result and result["papi"] is not True)
            or ("defaultPapi" in result and result["defaultPapi"] is not True)
            or any(
                key in result and result[key] not in (False, None)
                for key in (
                    "portfolioMarginPro",
                    "defaultPortfolioMarginPro",
                    "papiV2",
                    "defaultPapiV2",
                    "useV2",
                    "defaultUseV2",
                )
            )
            or any(
                key in result and result[key] not in (None, 0)
                for key in ("maxRetriesOnFailure", "defaultMaxRetriesOnFailure")
            )
            or sub_type not in (None, "linear")
            or default_sub_type not in (None, "linear")
            or default_type not in (None, "swap", "future")
            or request_type not in (None, "swap", "future")
            or margin_mode not in (None, "cross")
            or default_margin_mode not in (None, "cross")
            or caller_method_name is not None
        ):
            raise OperationalException(
                "Binance Portfolio Margin requests cannot disable PAPI/portfolioMargin "
                "or safe retry controls, or select non-USD-M markets."
            )
        # `papi` is CCXT's preferred alias and takes precedence over
        # `portfolioMargin`. Set both so neither call-level nor method-level
        # configuration can silently route a private request back to FAPI.
        for key in (
            "defaultPapi",
            "defaultPortfolioMargin",
            "portfolioMarginPro",
            "defaultPortfolioMarginPro",
            "papiV2",
            "defaultPapiV2",
            "defaultUseV2",
            "defaultMaxRetriesOnFailure",
        ):
            result.pop(key, None)
        result["papi"] = True
        result["portfolioMargin"] = True
        result["maxRetriesOnFailure"] = 0
        return result

    def _portfolio_algo_request(
        self,
        path: str,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Call one explicitly supported USD-M Algo endpoint through CCXT signing.

        CCXT 4.5.67 parses Binance Algo orders but does not yet expose the
        Portfolio Margin ``um/algo`` routes. Keep this shim deliberately thin:
        CCXT still owns signing, time synchronization, HTTP handling, error
        mapping, and rate limiting, while this adapter fixes the private API
        namespace and disables transport retries for order-safety.
        """
        if not self._portfolio_margin:
            raise OperationalException(
                "Binance Portfolio Margin Algo routes require portfolioMargin=true."
            )

        method = method.upper()
        route = (path, method)
        allowed_params: dict[tuple[str, str], set[str]] = {
            (
                "um/algo/order",
                "POST",
            ): {
                "algoType",
                "symbol",
                "side",
                "type",
                "quantity",
                "positionSide",
                "timeInForce",
                "price",
                "triggerPrice",
                "workingType",
                "priceMatch",
                "priceProtect",
                "reduceOnly",
                "activatePrice",
                "callbackRate",
                "clientAlgoId",
                "newOrderRespType",
                "selfTradePreventionMode",
                "goodTillDate",
                "recvWindow",
                "maxRetriesOnFailure",
            },
            (
                "um/algo/order",
                "DELETE",
            ): {
                "algoId",
                "clientAlgoId",
                "recvWindow",
                "maxRetriesOnFailure",
            },
            (
                "um/algo/algoOrder",
                "GET",
            ): {
                "algoId",
                "clientAlgoId",
                "recvWindow",
                "maxRetriesOnFailure",
            },
            (
                "um/algo/openAlgoOrders",
                "GET",
            ): {
                "algoType",
                "symbol",
                "algoId",
                "recvWindow",
                "maxRetriesOnFailure",
            },
            (
                "um/algo/allAlgoOrders",
                "GET",
            ): {
                "symbol",
                "algoId",
                "startTime",
                "endTime",
                "limit",
                "recvWindow",
                "maxRetriesOnFailure",
            },
        }
        if route not in allowed_params:
            raise OperationalException(
                f"Unsupported Binance Portfolio Margin Algo route: {method} {path}."
            )

        request_params = dict(params or {})
        if request_params.get("maxRetriesOnFailure", 0) not in (None, 0):
            raise OperationalException(
                "Binance Portfolio Margin Algo requests cannot enable transport retries."
            )
        unexpected = set(request_params).difference(allowed_params[route])
        if unexpected:
            raise OperationalException(
                "Binance Portfolio Margin Algo request contained unsupported parameters: "
                f"{', '.join(sorted(unexpected))}."
            )
        request_params["maxRetriesOnFailure"] = 0

        # The account-wide open-order snapshot has a documented request weight
        # of 40. History has weight 5; all other supported routes have weight 1.
        cost = 1
        if path == "um/algo/openAlgoOrders" and "symbol" not in request_params:
            cost = 40
        elif path == "um/algo/allAlgoOrders":
            cost = 5
        return self._api.request(
            path,
            "papi",
            method,
            request_params,
            config={"cost": cost},
        )

    def _parse_portfolio_algo_order(self, response: Any, pair: str) -> CcxtOrder:
        if not isinstance(response, dict):
            raise OperationalException(
                "Binance Portfolio Margin returned an unexpected Algo order response."
            )
        order = self._api.parse_order(response, self._api.market(pair))
        if not isinstance(order, dict):
            raise OperationalException(
                "Binance Portfolio Margin returned an unparsable Algo order response."
            )
        return self._order_contracts_to_amount(order)

    def _parse_portfolio_algo_orders(
        self, response: Any, pair: str | None = None
    ) -> list[CcxtOrder]:
        if not isinstance(response, list) or any(not isinstance(item, dict) for item in response):
            raise OperationalException(
                "Binance Portfolio Margin returned an unexpected Algo order-list response."
            )
        market = self._api.market(pair) if pair is not None else None
        orders = self._api.parse_orders(response, market)
        if not isinstance(orders, list) or any(not isinstance(item, dict) for item in orders):
            raise OperationalException(
                "Binance Portfolio Margin returned an unparsable Algo order-list response."
            )
        return [self._order_contracts_to_amount(order) for order in orders]

    def _fetch_portfolio_algo_order(
        self,
        pair: str,
        *,
        order_id: str | None = None,
        client_order_id: str | None = None,
    ) -> CcxtOrder:
        if (order_id is None) == (client_order_id is None):
            raise OperationalException(
                "Exactly one Binance Portfolio Margin Algo order identifier is required."
            )
        query = {"algoId": order_id} if order_id is not None else {"clientAlgoId": client_order_id}
        response = self._portfolio_algo_request("um/algo/algoOrder", "GET", query)
        return self._parse_portfolio_algo_order(response, pair)

    def _fetch_portfolio_algo_open_orders(self, pair: str | None = None) -> list[CcxtOrder]:
        query: dict[str, Any] = {"algoType": "CONDITIONAL"}
        if pair is not None:
            query["symbol"] = self._api.market(pair)["id"]
        response = self._portfolio_algo_request("um/algo/openAlgoOrders", "GET", query)
        return self._parse_portfolio_algo_orders(response, pair)

    def _fetch_portfolio_algo_order_history(
        self, pair: str, *, order_id: str | None = None
    ) -> list[CcxtOrder]:
        query: dict[str, Any] = {"symbol": self._api.market(pair)["id"]}
        if order_id is not None:
            query["algoId"] = order_id
        response = self._portfolio_algo_request("um/algo/allAlgoOrders", "GET", query)
        return self._parse_portfolio_algo_orders(response, pair)

    def _cancel_portfolio_algo_order(self, order_id: str) -> dict[str, Any]:
        response = self._portfolio_algo_request(
            "um/algo/order",
            "DELETE",
            {"algoId": order_id},
        )
        if not isinstance(response, dict) or response.get("complete") is not True:
            raise OperationalException(
                "Binance Portfolio Margin did not confirm the Algo order cancellation."
            )
        return {
            "id": str(order_id),
            "status": "canceled",
            "info": response,
        }

    @property
    def portfolio_margin_risk(self) -> dict[str, Any] | None:
        """Return the optional fail-closed runtime entry policy for RPC and order guards."""
        return self._portfolio_margin_risk if self._portfolio_margin else None

    def _validate_portfolio_margin_entry_order(
        self,
        *,
        pair: str,
        side: BuySell,
        amount: float,
        rate: float,
        leverage: float,
        reduce_only: bool,
    ) -> None:
        risk = self._portfolio_margin_risk
        if not self._portfolio_margin or risk is None or reduce_only:
            return
        expected_side: BuySell = "buy" if risk["side"] == "long" else "sell"
        max_notional = float(risk["max_entry_notional"])
        if (
            pair != risk["pair"]
            or side != expected_side
            or not isfinite(amount)
            or amount <= 0
            or not isfinite(rate)
            or rate <= 0
            or not isfinite(leverage)
            or not isclose(
                leverage,
                float(risk["max_leverage"]),
                rel_tol=0.0,
                abs_tol=1e-9,
            )
            or amount * rate > max_notional + 1e-9
        ):
            raise OperationalException(
                "Binance Portfolio Margin entry blocked by the configured pair, long-only, "
                "1x leverage, or maximum-notional risk policy."
            )

    def validate_config(self, config) -> None:
        super().validate_config(config)
        if not self._portfolio_margin:
            return

        invalid_pairs = []
        for pair in config.get("exchange", {}).get("pair_whitelist", []):
            market = self.markets.get(pair)
            if market and not (
                market.get("contract")
                and market.get("swap")
                and market.get("linear")
                and not market.get("inverse")
            ):
                invalid_pairs.append(pair)
        if invalid_pairs:
            raise OperationalException(
                "Binance Portfolio Margin supports linear USD-M perpetual markets only. "
                f"Unsupported pairs: {', '.join(invalid_pairs)}."
            )

    def get_proxy_coin(self) -> str:
        """
        Get the proxy coin for the given coin
        Falls back to the stake currency if no proxy coin is found
        :return: Proxy coin or stake currency
        """
        if self.margin_mode == MarginMode.CROSS:
            return self._config.get(
                "proxy_coin",
                self._config["stake_currency"],
            )  # type: ignore[return-value]
        return self._config["stake_currency"]

    def get_tickers(
        self,
        symbols: list[str] | None = None,
        *,
        cached: bool = False,
        market_type: TradingMode | None = None,
    ) -> Tickers:
        tickers = super().get_tickers(symbols=symbols, cached=cached, market_type=market_type)
        if self.trading_mode == TradingMode.FUTURES:
            # Binance's future result has no bid/ask values.
            # Therefore we must fetch that from fetch_bids_asks and combine the two results.
            bidsasks = self.fetch_bids_asks(symbols, cached=cached)
            tickers = deep_merge_dicts(bidsasks, tickers, allow_null_overrides=False)
        return tickers

    def _validate_portfolio_margin_account(self) -> None:
        position_side = self._api.papiGetUmPositionSideDual()
        self._log_exchange_response("portfolio_margin_position_mode", position_side)
        account_config = self._api.papiGetUmAccountConfig()
        self._log_exchange_response("portfolio_margin_account_config", account_config)
        if not isinstance(position_side, dict) or not isinstance(account_config, dict):
            raise OperationalException(
                "Binance Portfolio Margin PAPI capability check returned an "
                "unexpected response. Trading is blocked."
            )
        dual_side_position = position_side.get("dualSidePosition")
        if not isinstance(dual_side_position, bool):
            raise OperationalException(
                "Binance Portfolio Margin PAPI capability check returned an "
                "unexpected response. Trading is blocked."
            )
        if dual_side_position:
            raise OperationalException(
                "Hedge Mode is not supported by freqtrade. Please change the "
                "Portfolio Margin USD-M account to One-way Mode."
            )
        if account_config.get("canTrade") is not True:
            raise OperationalException(
                "Binance Portfolio Margin PAPI reports that trading permission "
                "is disabled. Trading is blocked."
            )

    @retrier
    def additional_exchange_init(self) -> None:
        """
        Additional exchange initialization logic.
        .api will be available at this point.
        Must be overridden in child methods if required.
        """
        try:
            if self.trading_mode == TradingMode.FUTURES and not self._config["dry_run"]:
                if self._portfolio_margin:
                    self._validate_portfolio_margin_account()
                    return

                position_side = self._api.fapiPrivateGetPositionSideDual()
                self._log_exchange_response("position_side_setting", position_side)
                assets_margin = self._api.fapiPrivateGetMultiAssetsMargin()
                self._log_exchange_response("multi_asset_margin", assets_margin)
                msg = ""
                if position_side.get("dualSidePosition") is True:
                    msg += (
                        "\nHedge Mode is not supported by freqtrade. "
                        "Please change 'Position Mode' on your binance futures account."
                    )
                if (
                    assets_margin.get("multiAssetsMargin") is True
                    and self.margin_mode != MarginMode.CROSS
                ):
                    msg += (
                        "\nMulti-Asset Mode is not supported by freqtrade. "
                        "Please change 'Asset Mode' on your binance futures account."
                    )
                if msg:
                    raise OperationalException(msg)
        except ccxt.DDoSProtection as e:
            raise DDosProtection(e) from e
        except (ccxt.OperationFailed, ccxt.ExchangeError) as e:
            raise TemporaryError(
                f"Error in additional_exchange_init due to {e.__class__.__name__}. Message: {e}"
            ) from e

        except ccxt.BaseError as e:
            raise OperationalException(e) from e

    def _get_params(
        self,
        side,
        ordertype: str,
        leverage: float,
        reduceOnly: bool,
        time_in_force: str = "GTC",
    ) -> dict:
        params = self._portfolio_margin_params(
            super()._get_params(side, ordertype, leverage, reduceOnly, time_in_force)
        )
        if self._portfolio_margin and self._portfolio_active_client_order_id:
            params["clientOrderId"] = self._portfolio_active_client_order_id
        return params

    def _get_stop_params(self, side, ordertype: str, stop_price: float) -> dict:
        params = self._portfolio_margin_params(
            super()._get_stop_params(side, ordertype, stop_price)
        )
        if self._portfolio_margin and self._portfolio_active_client_order_id:
            params["clientOrderId"] = self._portfolio_active_client_order_id
        return params

    @staticmethod
    def _new_portfolio_client_order_id() -> str:
        # Binance permits up to 36 characters. Generate once before submission and
        # reuse it for status reconciliation if the create response is unknown.
        return f"ftpm-{uuid4().hex[:24]}"

    @staticmethod
    def _portfolio_order_matches_client_id(order: Any, client_order_id: str) -> bool:
        if not isinstance(order, dict):
            return False
        info = order.get("info")
        strategy_client_id = (
            info.get("newClientStrategyId") or info.get("clientAlgoId")
            if isinstance(info, dict)
            else None
        )
        return (
            order.get("clientOrderId") == client_order_id or strategy_client_id == client_order_id
        )

    @staticmethod
    def _portfolio_order_response_confirmed(order: Any) -> bool:
        """Require an exchange order id before treating a create response as final."""
        return isinstance(order, dict) and order.get("id") not in (None, "", 0, "0")

    def _recover_portfolio_order(
        self,
        pair: str,
        client_order_id: str,
        *,
        conditional: bool,
    ) -> CcxtOrder | None:
        order = None
        recovery_attempts = (
            self._portfolio_stop_order_recovery_attempts
            if conditional
            else self._portfolio_order_recovery_attempts
        )
        for attempt in range(recovery_attempts):
            try:
                if conditional:
                    # History includes ACTIVE, CANCELED, TRIGGERED, and FINISHED
                    # orders, so a stop that triggered immediately after an
                    # unknown POST response cannot disappear from reconciliation.
                    orders = self._fetch_portfolio_algo_order_history(pair)
                    order = next(
                        (
                            item
                            for item in orders
                            if self._portfolio_order_matches_client_id(item, client_order_id)
                        ),
                        None,
                    )
                else:
                    params = self._portfolio_margin_params({"origClientOrderId": client_order_id})
                    order = self._api.fetch_order(client_order_id, pair, params=params)
                if order is not None:
                    break
            except ccxt.OrderNotFound:
                pass
            except ccxt.BaseError as e:
                raise OperationalException(
                    "Binance Portfolio Margin order submission status is unknown and the "
                    f"PAPI reconciliation query failed for client order {client_order_id}. "
                    "Automatic retry is disabled; inspect the account before restarting."
                ) from e
            if attempt + 1 < recovery_attempts:
                sleep(self._portfolio_order_recovery_delay)

        if order is None:
            return None
        if not self._portfolio_order_response_confirmed(order):
            raise OperationalException(
                "Binance Portfolio Margin order submission status is unknown because "
                "PAPI returned a reconciliation response without an exchange order id."
            )
        self._log_exchange_response("recovered_portfolio_margin_order", order)
        # Algo helpers normalize contracts while parsing. Ordinary CCXT order
        # recovery still needs the standard Freqtrade conversion exactly once.
        return order if conditional else self._order_contracts_to_amount(order)

    def _portfolio_active_position_snapshot(self, pair: str) -> list[tuple[dict[str, Any], float]]:
        """Return validated active PAPI positions for one pair's safety reconciliation."""
        positions = self.fetch_positions(pair)
        if not isinstance(positions, list) or any(
            not isinstance(position, dict) for position in positions
        ):
            raise OperationalException("PAPI returned an unexpected position containment response.")

        active_positions = []
        for position in positions:
            position_symbol = position.get("symbol")
            if position_symbol not in (None, "", pair):
                # Some PAPI/CCXT combinations can return an account-wide snapshot
                # even when one symbol was requested. Containment owns only the
                # uncertain order's pair and must never flatten another pair.
                continue
            try:
                contracts = abs(float(position.get("contracts") or 0.0))
            except (TypeError, ValueError) as e:
                raise OperationalException(
                    "Binance Portfolio Margin containment received an invalid position amount."
                ) from e
            if not isfinite(contracts):
                raise OperationalException(
                    "Binance Portfolio Margin containment received a non-finite position amount."
                )
            if contracts and position_symbol != pair:
                raise OperationalException(
                    "Binance Portfolio Margin containment received an active position "
                    "without the requested pair symbol."
                )
            if contracts:
                active_positions.append((position, contracts))
        return active_positions

    def _flatten_portfolio_position(
        self, pair: str, active_positions: list[tuple[dict[str, Any], float]]
    ) -> None:
        """Submit one evidence-based reduce-only close for a reconciled PAPI position."""
        if len(active_positions) != 1:
            raise OperationalException(
                "Binance Portfolio Margin containment found multiple active positions; "
                "automatic flattening is unsafe."
            )

        position, contracts = active_positions[0]
        position_symbol = position.get("symbol")
        position_side = position.get("side")
        if position_symbol != pair or position_side not in ("long", "short"):
            raise OperationalException(
                "Binance Portfolio Margin containment found unexpected position state; "
                "automatic flattening is unsafe."
            )

        close_client_id = self._new_portfolio_client_order_id()
        close_side = "sell" if position_side == "long" else "buy"
        base_amount = self._contracts_to_amount(pair, contracts)
        order_amount = self.amount_to_precision(pair, self._amount_to_contracts(pair, base_amount))
        close_params = self._portfolio_margin_params(
            {
                "reduceOnly": True,
                "clientOrderId": close_client_id,
            }
        )
        create_failed = False
        try:
            close_order = self._api.create_order(
                pair,
                "market",
                close_side,
                order_amount,
                None,
                close_params,
            )
        except Exception:
            create_failed = True
            close_order = None
        if create_failed or not self._portfolio_order_response_confirmed(close_order):
            close_order = self._recover_portfolio_order(pair, close_client_id, conditional=False)
            if close_order is None:
                raise OperationalException(
                    "Binance Portfolio Margin emergency reduce-only containment order "
                    "could not be confirmed. Trading remains stopped."
                )
        if not self._portfolio_order_response_confirmed(close_order):
            raise OperationalException(
                "Binance Portfolio Margin emergency containment returned an "
                "order response without an exchange order id."
            )
        self._log_exchange_response("portfolio_margin_emergency_flatten", close_order)

    def _contain_unknown_portfolio_order(  # noqa: C901
        self, pair: str, client_order_id: str
    ) -> bool:
        """Cancel an unknown order and repeatedly contain late fills until stably clean."""
        if self._portfolio_margin_risk is None:
            return False

        flattened = False
        stable_clean_snapshots = 0
        last_error: Exception | None = None
        matching_order_visible = False
        active_position_visible = False

        for attempt in range(self._portfolio_containment_attempts):
            snapshot_action = False
            matching_orders: list[dict[str, Any]] | None = None
            try:
                open_orders = self._api.fetch_open_orders(
                    pair, params=self._portfolio_margin_params()
                )
                if not isinstance(open_orders, list) or any(
                    not isinstance(order, dict) for order in open_orders
                ):
                    raise OperationalException(
                        "PAPI returned an unexpected open-order containment response."
                    )
                matching_orders = [
                    order
                    for order in open_orders
                    if self._portfolio_order_matches_client_id(order, client_order_id)
                ]
                matching_order_visible = bool(matching_orders)
                last_error = None
                for order in matching_orders:
                    snapshot_action = True
                    order_id = order.get("id")
                    if not order_id:
                        last_error = OperationalException(
                            "A matching unknown PAPI order had no exchange order id."
                        )
                        continue
                    try:
                        self._api.cancel_order(
                            str(order_id),
                            pair,
                            params=self._portfolio_margin_params(),
                        )
                    except ccxt.OrderNotFound:
                        # A fill may win the race. A later combined order/position
                        # snapshot must still prove the account is clean.
                        pass
                    except (FreqtradeException, ccxt.BaseError) as e:
                        last_error = e
            except (FreqtradeException, ccxt.BaseError) as e:
                last_error = e

            active_positions: list[tuple[dict[str, Any], float]] | None = None
            try:
                active_positions = self._portfolio_active_position_snapshot(pair)
                active_position_visible = bool(active_positions)
            except (FreqtradeException, ccxt.BaseError) as e:
                last_error = e

            order_snapshot_clean = matching_orders == [] and last_error is None
            if active_positions and order_snapshot_clean:
                # Only close after a fresh PAPI open-order snapshot proves that the
                # potentially filling entry is no longer open. Every later active
                # position snapshot is fresh evidence of exposure: the original
                # entry may have filled after an earlier emergency close. A repeated
                # reduce-only market close cannot reverse or open a position.
                self._flatten_portfolio_position(pair, active_positions)
                flattened = True
                snapshot_action = True

            clean_snapshot = order_snapshot_clean and active_positions == [] and not snapshot_action
            if clean_snapshot:
                stable_clean_snapshots += 1
                if stable_clean_snapshots >= self._portfolio_containment_stable_snapshots:
                    return flattened
            else:
                stable_clean_snapshots = 0

            if attempt + 1 < self._portfolio_containment_attempts:
                sleep(self._portfolio_order_recovery_delay)

        if last_error is not None:
            raise OperationalException(
                "Binance Portfolio Margin unknown-order containment could not obtain "
                "stable PAPI open-order and position snapshots. Trading remains stopped."
            ) from last_error
        if matching_order_visible:
            raise OperationalException(
                "Binance Portfolio Margin unknown-order containment could not confirm "
                "that the PAPI order was cancelled. Trading remains stopped."
            )
        if active_position_visible:
            raise OperationalException(
                "Binance Portfolio Margin emergency containment order did not flatten "
                "the position within the bounded verification window. Trading remains stopped."
            )
        raise OperationalException(
            "Binance Portfolio Margin unknown-order containment could not obtain "
            "consecutive clean PAPI snapshots. Trading remains stopped."
        )

    def create_order(  # noqa: C901
        self,
        *,
        pair: str,
        ordertype: str,
        side: BuySell,
        amount: float,
        rate: float,
        leverage: float,
        time_in_force: str = "GTC",
        reduceOnly: bool = False,
        initial_order: bool = True,
    ) -> CcxtOrder:
        if self._portfolio_unknown_order_latched and not reduceOnly:
            raise OperationalException(
                "Binance Portfolio Margin has a latched unknown order state. "
                "Reconcile the account before restarting."
            )
        self._validate_portfolio_margin_entry_order(
            pair=pair,
            side=side,
            amount=amount,
            rate=rate,
            leverage=leverage,
            reduce_only=reduceOnly,
        )
        if not self._portfolio_margin or self._config["dry_run"]:
            return super().create_order(
                pair=pair,
                ordertype=ordertype,
                side=side,
                amount=amount,
                rate=rate,
                leverage=leverage,
                time_in_force=time_in_force,
                reduceOnly=reduceOnly,
                initial_order=initial_order,
            )

        with self._get_portfolio_create_lock():
            if self._portfolio_unknown_order_latched and not reduceOnly:
                raise OperationalException(
                    "Binance Portfolio Margin has a latched unknown order state. "
                    "Reconcile the account before restarting."
                )
            client_order_id = self._new_portfolio_client_order_id()
            self._portfolio_active_client_order_id = client_order_id
            was_unknown_latched = self._portfolio_unknown_order_latched
            try:
                submission_error: Exception | None = None
                try:
                    order = super().create_order(
                        pair=pair,
                        ordertype=ordertype,
                        side=side,
                        amount=amount,
                        rate=rate,
                        leverage=leverage,
                        time_in_force=time_in_force,
                        reduceOnly=reduceOnly,
                        initial_order=initial_order,
                    )
                except TemporaryError as e:
                    submission_error = e
                except (AttributeError, KeyError, TypeError, ValueError) as e:
                    submission_error = e
                except FreqtradeException:
                    raise
                except Exception as e:
                    submission_error = e
                else:
                    if self._portfolio_order_response_confirmed(order):
                        return order
                    submission_error = OperationalException(
                        "Binance Portfolio Margin create response did not contain "
                        "an exchange order id."
                    )

                self._portfolio_unknown_order_latched = True
                try:
                    recovered = self._recover_portfolio_order(
                        pair, client_order_id, conditional=False
                    )
                except OperationalException as reconciliation_error:
                    try:
                        flattened = self._contain_unknown_portfolio_order(pair, client_order_id)
                    except OperationalException as containment_error:
                        raise OperationalException(
                            "Binance Portfolio Margin order submission status is unknown; "
                            "the PAPI reconciliation query failed and emergency containment "
                            "could not complete. Trading remains stopped."
                        ) from containment_error
                    raise OperationalException(
                        "Binance Portfolio Margin order submission status is unknown and "
                        "the PAPI reconciliation query failed. "
                        + (
                            "Emergency containment flattened detected exposure. "
                            if flattened
                            else "No exposure was visible during bounded containment. "
                        )
                        + "Trading remains stopped."
                    ) from reconciliation_error
                if recovered is not None:
                    self._portfolio_unknown_order_latched = was_unknown_latched
                    return recovered
                flattened = self._contain_unknown_portfolio_order(pair, client_order_id)
                raise OperationalException(
                    "Binance Portfolio Margin order submission status is unknown. "
                    f"No PAPI order was visible for client order {client_order_id}; "
                    "automatic retry is disabled. "
                    + (
                        "Emergency containment flattened detected exposure. "
                        if flattened
                        else "No exposure was visible during bounded containment. "
                    )
                    + "Inspect positions and open orders before restarting."
                ) from submission_error
            finally:
                self._portfolio_active_client_order_id = None

    def _create_portfolio_algo_stoploss(
        self,
        pair: str,
        amount: float,
        stop_price: float,
        order_types: dict,
        side: BuySell,
        leverage: float,
    ) -> CcxtOrder:
        """Create a Portfolio Margin stop through the current PAPI Algo service."""
        if not self._ft_has["stoploss_on_exchange"]:
            raise OperationalException(f"stoploss is not implemented for {self.name}.")

        user_order_type = order_types.get("stoploss", "market")
        ordertype, user_order_type = self._get_stop_order_type(user_order_type)
        round_mode = ROUND_DOWN if side == "buy" else ROUND_UP
        stop_price_norm = self.price_to_precision(pair, stop_price, rounding_mode=round_mode)
        limit_rate = None
        if user_order_type == "limit":
            limit_rate = self._get_stop_limit_rate(stop_price, order_types, side)
            limit_rate = self.price_to_precision(pair, limit_rate, rounding_mode=round_mode)

        params = self._get_stop_params(
            side=side,
            ordertype=ordertype,
            stop_price=stop_price_norm,
        )
        params["reduceOnly"] = True
        if "stoploss_price_type" in order_types and "stop_price_type_field" in self._ft_has:
            price_type = self._ft_has["stop_price_type_value_mapping"][
                order_types.get("stoploss_price_type", PriceType.LAST)
            ]
            params[self._ft_has["stop_price_type_field"]] = price_type

        amount = self.amount_to_precision(pair, self._amount_to_contracts(pair, amount))
        self._lev_prep(pair, leverage, side, accept_fail=True)

        # CCXT 4.5.67 already knows the current Algo field schema for linear
        # futures, but its Portfolio Margin route still builds the retired
        # ``strategy*`` schema. The call-local false values affect request
        # construction only; no request is sent until the fixed PAPI call below.
        builder_params = dict(params)
        builder_params["papi"] = False
        builder_params["portfolioMargin"] = False
        try:
            request = self._api.create_order_request(
                pair,
                ordertype,
                side,
                amount,
                limit_rate,
                builder_params,
            )
            request["algoType"] = "CONDITIONAL"
            response = self._portfolio_algo_request("um/algo/order", "POST", request)
        except ccxt.InsufficientFunds as e:
            raise InsufficientFundsError(
                f"Insufficient funds to create {ordertype} {side} order on market {pair}. "
                f"Tried to {side} amount {amount} at rate {limit_rate} with "
                f"stop-price {stop_price_norm}. Message: {e}"
            ) from e
        except (ccxt.InvalidOrder, ccxt.BadRequest, ccxt.OperationRejected) as e:
            raise InvalidOrderException(
                f"Could not create {ordertype} {side} order on market {pair}. "
                f"Tried to {side} amount {amount} at rate {limit_rate} with "
                f"stop-price {stop_price_norm}. Message: {e}"
            ) from e
        except ccxt.DDoSProtection as e:
            raise DDosProtection(e) from e
        except (ccxt.OperationFailed, ccxt.ExchangeError) as e:
            raise TemporaryError(
                f"Could not place stoploss order due to {e.__class__.__name__}. Message: {e}"
            ) from e
        except ccxt.BaseError as e:
            raise OperationalException(e) from e

        # A malformed or incomplete response has an unknown execution result.
        # Raise a built-in parsing error so the outer client-id reconciliation
        # path runs exactly once and never blindly submits the order again.
        if not isinstance(response, dict):
            raise TypeError("PAPI Algo create returned a non-object response.")
        order = self._api.parse_order(response, self._api.market(pair))
        if not isinstance(order, dict):
            raise TypeError("CCXT could not parse the PAPI Algo create response.")

        self._log_exchange_response("create_stoploss_order", order)
        order = self._order_contracts_to_amount(order)
        logger.info(
            f"stoploss {user_order_type} order added for {pair}. "
            f"stop price: {stop_price}. limit: {limit_rate}"
        )
        return order

    def create_stoploss(
        self,
        pair: str,
        amount: float,
        stop_price: float,
        order_types: dict,
        side: BuySell,
        leverage: float,
    ) -> CcxtOrder:
        if self.portfolio_margin_unknown_order_latched:
            raise InvalidOrderException(
                "Binance Portfolio Margin has a latched unknown order state. "
                "A new conditional stop order cannot be submitted until the account "
                "has been reconciled and the bot restarted."
            )
        if not self._portfolio_margin or self._config["dry_run"]:
            return super().create_stoploss(pair, amount, stop_price, order_types, side, leverage)

        with self._get_portfolio_create_lock():
            if self._portfolio_unknown_order_latched:
                raise InvalidOrderException(
                    "Binance Portfolio Margin has a latched unknown order state. "
                    "A new conditional stop order cannot be submitted until the account "
                    "has been reconciled and the bot restarted."
                )
            client_order_id = self._new_portfolio_client_order_id()
            self._portfolio_active_client_order_id = client_order_id
            was_unknown_latched = self._portfolio_unknown_order_latched
            try:
                submission_error: Exception | None = None
                try:
                    order = self._create_portfolio_algo_stoploss(
                        pair, amount, stop_price, order_types, side, leverage
                    )
                except TemporaryError as e:
                    submission_error = e
                except (AttributeError, KeyError, TypeError, ValueError) as e:
                    submission_error = e
                except FreqtradeException as e:
                    raise InvalidOrderException(
                        "Binance Portfolio Margin exchange stop-loss protection could "
                        "not be confirmed. Freqtrade must perform its configured emergency "
                        "exit."
                    ) from e
                except Exception as e:
                    submission_error = e
                else:
                    if self._portfolio_order_response_confirmed(order):
                        return order
                    submission_error = OperationalException(
                        "Binance Portfolio Margin conditional create response did not "
                        "contain an exchange order id."
                    )

                self._portfolio_unknown_order_latched = True
                self._portfolio_unknown_conditional_client_order_id = client_order_id
                self._portfolio_unknown_conditional_pair = pair
                try:
                    recovered = self._recover_portfolio_order(
                        pair, client_order_id, conditional=True
                    )
                except OperationalException as reconciliation_error:
                    raise InvalidOrderException(
                        "Binance Portfolio Margin conditional-order submission status "
                        "is unknown and the PAPI recovery query failed. Freqtrade must "
                        "perform its configured emergency exit because exchange "
                        "stop-loss protection could not be confirmed."
                    ) from reconciliation_error
                if recovered is not None:
                    self._portfolio_unknown_order_latched = was_unknown_latched
                    self._portfolio_unknown_conditional_client_order_id = None
                    self._portfolio_unknown_conditional_pair = None
                    return recovered
                raise InvalidOrderException(
                    "Binance Portfolio Margin conditional-order submission status is "
                    f"unknown. No PAPI order was visible for client order "
                    f"{client_order_id}; automatic retry is disabled. Freqtrade must "
                    "perform its configured emergency exit because exchange stop-loss "
                    "protection could not be confirmed."
                ) from submission_error
            finally:
                self._portfolio_active_client_order_id = None

    def cleanup_portfolio_margin_unknown_conditional_order(  # noqa: C901
        self, pair: str
    ) -> bool:
        """Cancel a delayed unknown PAPI stop after emergency exit and prove it absent.

        This method only performs PAPI reads and, when the exact client order id
        becomes visible, a cancellation. It never creates an order and intentionally
        leaves the fail-closed unknown-order latch set.
        """
        if not self._portfolio_margin:
            return True
        client_order_id = self._portfolio_unknown_conditional_client_order_id
        if client_order_id is None:
            return True
        if self._portfolio_unknown_conditional_pair != pair:
            raise OperationalException(
                "Binance Portfolio Margin conditional-order cleanup pair does not "
                "match the pending safety record. Trading remains stopped."
            )

        with self._get_portfolio_create_lock():
            stable_absent_snapshots = 0
            cancellation_error: Exception | None = None
            for attempt in range(self._portfolio_conditional_cleanup_attempts):
                try:
                    orders = self._fetch_portfolio_algo_open_orders(pair)
                except ccxt.BaseError as e:
                    raise OperationalException(
                        "Binance Portfolio Margin could not query PAPI Algo conditional "
                        "orders after the emergency exit. Trading remains stopped."
                    ) from e
                if not isinstance(orders, list) or any(
                    not isinstance(order, dict) for order in orders
                ):
                    raise OperationalException(
                        "Binance Portfolio Margin received an unexpected PAPI "
                        "conditional-order cleanup response. Trading remains stopped."
                    )

                matching_orders = [
                    order
                    for order in orders
                    if self._portfolio_order_matches_client_id(order, client_order_id)
                ]
                if matching_orders:
                    stable_absent_snapshots = 0
                    for order in matching_orders:
                        order_id = order.get("id")
                        if not order_id:
                            cancellation_error = OperationalException(
                                "A delayed PAPI conditional order had no exchange order id."
                            )
                            continue
                        try:
                            self._cancel_portfolio_algo_order(str(order_id))
                        except ccxt.OrderNotFound:
                            # The Algo order may have completed between the
                            # snapshot and DELETE. Later snapshots still have to
                            # confirm that it is not open.
                            pass
                        except ccxt.BaseError as e:
                            cancellation_error = e
                else:
                    cancellation_error = None
                    stable_absent_snapshots += 1
                    if (
                        stable_absent_snapshots
                        >= self._portfolio_conditional_cleanup_stable_snapshots
                    ):
                        self._portfolio_unknown_conditional_client_order_id = None
                        self._portfolio_unknown_conditional_pair = None
                        return True

                if attempt + 1 < self._portfolio_conditional_cleanup_attempts:
                    sleep(self._portfolio_order_recovery_delay)

            raise OperationalException(
                "Binance Portfolio Margin could not confirm that the delayed PAPI "
                "conditional order was cancelled after the emergency exit. "
                "Trading remains stopped."
            ) from cancellation_error

    def get_balances(self, params: dict | None = None):
        return super().get_balances(self._portfolio_margin_params(params))

    def _portfolio_position_params(self, params: dict | None = None) -> dict:
        """Validate and reduce PAPI positionRisk parameters to CCXT-safe values."""
        position_params = dict(params or {})
        if (
            position_params.pop("method", "positionRisk") != "positionRisk"
            or position_params.pop("defaultMethod", "positionRisk") != "positionRisk"
        ):
            raise OperationalException(
                "Binance Portfolio Margin fetch_positions must use PAPI positionRisk."
            )
        # Validate caller overrides, but do not forward adapter-routing keys here.
        # CCXT's Binance fetch_positions_risk() loads leverage brackets before it
        # consumes portfolioMargin/papi/subType. Forwarding those keys would leak
        # them into the raw PAPI leverage-bracket request. The exchange-wide CCXT
        # options already fix this instance to linear Portfolio Margin.
        safe_params = self._portfolio_margin_params(position_params)
        for key in (
            "papi",
            "defaultPapi",
            "portfolioMargin",
            "defaultPortfolioMargin",
            "portfolioMarginPro",
            "defaultPortfolioMarginPro",
            "papiV2",
            "defaultPapiV2",
            "useV2",
            "defaultUseV2",
            "subType",
            "defaultSubType",
        ):
            safe_params.pop(key, None)
        return safe_params

    def _fetch_portfolio_positions_once(  # noqa: C901
        self, pair: str | None = None, params: dict | None = None
    ) -> list[dict[str, Any]]:
        """Perform one PAPI positionRisk read without Freqtrade's retry decorator."""
        if self._config["dry_run"]:
            return []
        safe_params = self._portfolio_position_params(params)
        symbols = [pair] if pair else None
        try:
            positions = self._api.fetch_positions(symbols, params=safe_params)
            self._log_exchange_response("fetch_portfolio_margin_positions", positions)
        except ccxt.DDoSProtection as e:
            raise DDosProtection(e) from e
        except (ccxt.OperationFailed, ccxt.ExchangeError) as e:
            raise TemporaryError(
                "Could not get Portfolio Margin positions due to "
                f"{e.__class__.__name__}. Message: {e}"
            ) from e
        except ccxt.BaseError as e:
            raise OperationalException(e) from e

        if not isinstance(positions, list) or any(
            not isinstance(position, dict) for position in positions
        ):
            raise OperationalException(
                "PAPI returned an unexpected Portfolio Margin positionRisk response."
            )

        normalized_positions = []
        for position in positions:
            normalized = dict(position)
            try:
                contracts_value: Any = normalized.get("contracts")
                contracts = abs(float(contracts_value or 0.0))
            except (TypeError, ValueError) as e:
                raise OperationalException(
                    "PAPI returned an invalid Portfolio Margin position amount."
                ) from e
            if not isfinite(contracts):
                raise OperationalException(
                    "PAPI returned a non-finite Portfolio Margin position amount."
                )
            if contracts:
                normalized["marginMode"] = MarginMode.CROSS.value
                normalized["marginType"] = MarginMode.CROSS.value
                if not normalized.get("collateral"):
                    # PAPI position-risk has no per-position collateral. Wallets uses a
                    # nonzero value as its open-position marker, so retain CCXT's initial
                    # margin estimate without treating it as liquidation collateral.
                    marker = normalized.get("initialMargin")
                    if not marker:
                        try:
                            notional_value: Any = normalized.get("notional")
                            leverage_value: Any = normalized.get("leverage")
                            notional = abs(float(notional_value or 0.0))
                            leverage = float(leverage_value or 1.0)
                        except (TypeError, ValueError) as e:
                            raise OperationalException(
                                "PAPI returned invalid Portfolio Margin position metadata."
                            ) from e
                        if not isfinite(notional) or not isfinite(leverage) or leverage <= 0:
                            raise OperationalException(
                                "PAPI returned invalid Portfolio Margin position metadata."
                            )
                        marker = notional / leverage if notional else contracts
                    normalized["collateral"] = marker
            normalized_positions.append(normalized)
        return normalized_positions

    def fetch_positions(self, pair: str | None = None, params: dict | None = None):
        if not self._portfolio_margin:
            return super().fetch_positions(pair, params)
        return self._fetch_portfolio_positions_once(pair, params)

    def is_portfolio_margin_position_flat(self, pair: str) -> bool:
        """Read PAPI positionRisk once and report whether the requested pair is flat."""
        if not self._portfolio_margin:
            return False
        return not self._portfolio_active_position_snapshot(pair)

    def fetch_order(self, order_id: str, pair: str, params: dict | None = None):
        return super().fetch_order(order_id, pair, self._portfolio_margin_params(params))

    def cancel_order(self, order_id: str, pair: str, params: dict | None = None):
        return super().cancel_order(order_id, pair, self._portfolio_margin_params(params))

    def cancel_stoploss_order(self, order_id: str, pair: str, params: dict | None = None) -> dict:
        if not self._portfolio_margin or self._config["dry_run"]:
            return super().cancel_stoploss_order(order_id, pair, params)
        if params:
            unsupported = set(params).difference({"stop", "trigger"})
            if unsupported:
                raise OperationalException(
                    "Binance Portfolio Margin Algo cancellation does not support "
                    f"parameters: {', '.join(sorted(unsupported))}."
                )
        try:
            order = self._cancel_portfolio_algo_order(order_id)
            self._log_exchange_response("cancel_stoploss_order", order)
            return order
        except ccxt.InvalidOrder as e:
            raise InvalidOrderException(f"Could not cancel stoploss order. Message: {e}") from e
        except ccxt.DDoSProtection as e:
            raise DDosProtection(e) from e
        except (ccxt.OperationFailed, ccxt.ExchangeError) as e:
            raise TemporaryError(
                f"Could not cancel stoploss order due to {e.__class__.__name__}. Message: {e}"
            ) from e
        except ccxt.BaseError as e:
            raise OperationalException(e) from e

    def _fetch_orders(self, pair: str, since: datetime, params: dict | None = None):
        return super()._fetch_orders(pair, since, self._portfolio_margin_params(params))

    def get_trades_for_order(
        self,
        order_id: str,
        pair: str,
        since: datetime,
        params: dict | None = None,
    ) -> list:
        return super().get_trades_for_order(
            order_id, pair, since, self._portfolio_margin_params(params)
        )

    def _fetch_portfolio_conditional_order_history(self, order_id: str, pair: str) -> CcxtOrder:
        try:
            orders = self._fetch_portfolio_algo_order_history(pair, order_id=order_id)
            order = next(
                (
                    item
                    for item in orders
                    if isinstance(item, dict) and str(item.get("id")) == str(order_id)
                ),
                None,
            )
            if order is None:
                raise RetryableOrderError(
                    f"Portfolio Margin conditional order not found (pair: {pair} id: {order_id})."
                )
            return order
        except ccxt.OrderNotFound as e:
            raise RetryableOrderError(
                f"Portfolio Margin conditional order not found (pair: {pair} id: {order_id})."
            ) from e
        except ccxt.InvalidOrder as e:
            raise InvalidOrderException(
                "Tried to get an invalid Portfolio Margin conditional order "
                f"(pair: {pair} id: {order_id}). Message: {e}"
            ) from e
        except ccxt.DDoSProtection as e:
            raise DDosProtection(e) from e
        except (ccxt.OperationFailed, ccxt.ExchangeError) as e:
            raise TemporaryError(
                "Could not get Portfolio Margin conditional order due to "
                f"{e.__class__.__name__}. Message: {e}"
            ) from e
        except ccxt.BaseError as e:
            raise OperationalException(e) from e

    def fetch_stoploss_order(self, order_id: str, pair: str, params: dict | None = None):
        if not self._portfolio_margin:
            return super().fetch_stoploss_order(order_id, pair, params)

        if params:
            unsupported = set(params).difference({"stop", "trigger"})
            if unsupported:
                raise OperationalException(
                    "Binance Portfolio Margin Algo query does not support "
                    f"parameters: {', '.join(sorted(unsupported))}."
                )
        try:
            order = self._fetch_portfolio_algo_order(pair, order_id=order_id)
        except ccxt.OrderNotFound:
            order = self._fetch_portfolio_conditional_order_history(order_id, pair)
        except ccxt.InvalidOrder as e:
            raise InvalidOrderException(
                "Tried to get an invalid Portfolio Margin conditional order "
                f"(pair: {pair} id: {order_id}). Message: {e}"
            ) from e
        except ccxt.DDoSProtection as e:
            raise DDosProtection(e) from e
        except (ccxt.OperationFailed, ccxt.ExchangeError) as e:
            raise TemporaryError(
                "Could not get Portfolio Margin conditional order due to "
                f"{e.__class__.__name__}. Message: {e}"
            ) from e
        except ccxt.BaseError as e:
            raise OperationalException(e) from e

        self._log_exchange_response("fetch_stoploss_order", order)
        val = self.get_option("stoploss_algo_order_info_id")
        if order.get("status", "open") in ("closed", "triggered"):
            info = order.get("info", {})
            new_orderid = info.get(val) if val else None
            new_orderid = new_orderid or info.get("orderId")
            if new_orderid and str(new_orderid) != "0":
                actual_order = self.fetch_order(order_id=new_orderid, pair=pair)
                actual_order["id_stop"] = actual_order["id"]
                actual_order["id"] = order_id
                actual_order["type"] = "stoploss"
                actual_order["stopPrice"] = order.get("stopPrice")
                actual_order["status_stop"] = "triggered"
                return actual_order
        return order

    def fetch_trading_fees(self) -> dict[str, Any]:
        if self._portfolio_margin:
            # CCXT's Binance fetch_trading_fees() plural method uses a standard
            # futures account-config endpoint. Binance market fees remain available
            # from public market metadata, so skip this optional private call.
            return {}
        return super().fetch_trading_fees()

    def get_leverage_tiers(self) -> dict[str, list[dict]]:
        if not self._portfolio_margin:
            return super().get_leverage_tiers()
        try:
            return self._api.fetch_leverage_tiers(params=self._portfolio_margin_params())
        except ccxt.DDoSProtection as e:
            raise DDosProtection(e) from e
        except (ccxt.OperationFailed, ccxt.ExchangeError) as e:
            raise TemporaryError(
                f"Could not load Portfolio Margin leverage tiers due to "
                f"{e.__class__.__name__}. Message: {e}"
            ) from e
        except ccxt.BaseError as e:
            raise OperationalException(e) from e

    def _set_leverage(
        self,
        leverage: float,
        pair: str | None = None,
        accept_fail: bool = False,
    ):
        if not self._portfolio_margin:
            return super()._set_leverage(leverage, pair, accept_fail)
        if self._config["dry_run"] or not self.exchange_has("setLeverage"):
            return
        if not isclose(leverage, 1.0, rel_tol=0.0, abs_tol=1e-9):
            raise OperationalException(
                "Binance Portfolio Margin live trading is restricted to 1x leverage "
                "for this initial adapter release."
            )
        leverage = floor(leverage)
        try:
            res = self._api.set_leverage(
                symbol=pair,
                leverage=leverage,
                params=self._portfolio_margin_params(),
            )
            self._log_exchange_response("set_portfolio_margin_leverage", res)
        except ccxt.DDoSProtection as e:
            raise DDosProtection(e) from e
        except (ccxt.BadRequest, ccxt.OperationRejected, ccxt.InsufficientFunds) as e:
            if not accept_fail:
                raise TemporaryError(
                    f"Could not set Portfolio Margin leverage due to "
                    f"{e.__class__.__name__}. Message: {e}"
                ) from e
        except (ccxt.OperationFailed, ccxt.ExchangeError) as e:
            raise TemporaryError(
                f"Could not set Portfolio Margin leverage due to "
                f"{e.__class__.__name__}. Message: {e}"
            ) from e
        except ccxt.BaseError as e:
            raise OperationalException(e) from e

    def set_margin_mode(
        self,
        pair: str,
        margin_mode: MarginMode,
        accept_fail: bool = False,
        params: dict | None = None,
    ):
        if not self._portfolio_margin:
            return super().set_margin_mode(pair, margin_mode, accept_fail, params)
        if margin_mode != MarginMode.CROSS:
            raise OperationalException(
                "Binance Portfolio Margin is account-level cross margin; isolated mode "
                "is not supported."
            )
        # CCXT set_margin_mode uses FAPI. Portfolio Margin is always cross, so no API call
        # is required or permitted.
        return None

    def _get_funding_fees_from_exchange(self, pair: str, since: datetime | int) -> float:
        if not self._portfolio_margin:
            return super()._get_funding_fees_from_exchange(pair, since)
        if not self.exchange_has("fetchFundingHistory"):
            raise OperationalException(
                f"fetch_funding_history() is not available using {self.name}"
            )
        if type(since) is datetime:
            since = dt_ts(since)
        try:
            funding_history = self._api.fetch_funding_history(
                symbol=pair,
                since=since,
                params=self._portfolio_margin_params(),
            )
            self._log_exchange_response(
                "portfolio_margin_funding_history",
                funding_history,
                add_info=f"pair: {pair}, since: {since}",
            )
            return sum(fee["amount"] for fee in funding_history)
        except ccxt.DDoSProtection as e:
            raise DDosProtection(e) from e
        except (ccxt.OperationFailed, ccxt.ExchangeError) as e:
            raise TemporaryError(
                f"Could not get Portfolio Margin funding fees due to "
                f"{e.__class__.__name__}. Message: {e}"
            ) from e
        except ccxt.BaseError as e:
            raise OperationalException(e) from e

    def get_liquidation_price(self, *args, **kwargs) -> float | None:
        if self._portfolio_margin:
            return None
        return super().get_liquidation_price(*args, **kwargs)

    def _portfolio_margin_list_response(
        self, label: str, request: Callable[[], Any]
    ) -> list[dict[str, Any]]:
        try:
            response = request()
        except ccxt.BaseError as e:
            raise OperationalException(
                "Binance Portfolio Margin trading blocked because the PAPI "
                f"{label} reconciliation request failed. No new orders will be "
                "submitted until the account can be reconciled."
            ) from e
        if not isinstance(response, list) or any(not isinstance(item, dict) for item in response):
            raise OperationalException(
                "Binance Portfolio Margin trading blocked because PAPI returned an "
                f"unexpected {label} response."
            )
        return response

    def _portfolio_margin_open_order_conflicts(self, open_trades: list[Any]) -> list[str]:
        known_open_orders = {
            (trade.pair, str(order.order_id))
            for trade in open_trades
            for order in getattr(trade, "orders", [])
            if getattr(order, "ft_is_open", False) and getattr(order, "order_id", None)
        }
        for attempt in range(self._portfolio_startup_snapshot_attempts):
            normal_open_orders = self._portfolio_margin_list_response(
                "USD-M open-order",
                lambda: self._api.fetch_open_orders(params=self._portfolio_margin_params()),
            )
            conditional_open_orders = self._portfolio_margin_list_response(
                "USD-M Algo conditional-order",
                self._fetch_portfolio_algo_open_orders,
            )
            conflicts = []
            for order_kind, orders in (
                ("regular", normal_open_orders),
                ("conditional", conditional_open_orders),
            ):
                for order in orders:
                    order_id = str(order.get("id") or "")
                    info = order.get("info")
                    info_symbol = info.get("symbol") if isinstance(info, dict) else None
                    symbol = order.get("symbol") or info_symbol or "unknown"
                    if not order_id or (symbol, order_id) not in known_open_orders:
                        conflicts.append(
                            f"{symbol}: exchange has untracked {order_kind} open order "
                            f"{order_id or '<missing id>'}"
                        )
            if conflicts:
                return conflicts
            if attempt + 1 < self._portfolio_startup_snapshot_attempts:
                sleep(self._portfolio_order_recovery_delay)
        return []

    def _portfolio_margin_unsupported_exposure_conflicts(self) -> list[str]:
        cm_positions = self._portfolio_margin_list_response(
            "COIN-M position", self._api.papiGetCmPositionRisk
        )
        cm_orders = self._portfolio_margin_list_response(
            "COIN-M open-order", self._api.papiGetCmOpenOrders
        )
        cm_conditional_orders = self._portfolio_margin_list_response(
            "COIN-M conditional-order", self._api.papiGetCmConditionalOpenOrders
        )
        margin_orders = self._portfolio_margin_list_response(
            "margin open-order", self._api.papiGetMarginOpenOrders
        )
        margin_order_lists = self._portfolio_margin_list_response(
            "margin OCO order-list", self._api.papiGetMarginOpenOrderList
        )

        conflicts = []
        for position in cm_positions:
            try:
                amount = abs(float(position.get("positionAmt") or 0.0))
            except (TypeError, ValueError) as e:
                raise OperationalException(
                    "Binance Portfolio Margin trading blocked because PAPI returned "
                    "an invalid COIN-M position amount."
                ) from e
            if amount:
                conflicts.append(
                    f"{position.get('symbol') or 'unknown'}: unsupported COIN-M position"
                )
        if cm_orders:
            conflicts.append("exchange has unsupported COIN-M open orders")
        if cm_conditional_orders:
            conflicts.append("exchange has unsupported COIN-M conditional orders")
        if margin_orders:
            conflicts.append("exchange has unsupported margin open orders")
        if margin_order_lists:
            conflicts.append("exchange has unsupported margin OCO order lists")
        return conflicts

    def validate_existing_positions(
        self, positions: dict[str, Any], open_trades: list[Any]
    ) -> None:
        """Block live Portfolio Margin when exchange positions and the DB disagree."""
        if (
            not self._portfolio_margin
            or self._config.get("dry_run", True)
            or self.trading_mode != TradingMode.FUTURES
        ):
            return

        trades_by_pair: dict[str, Any] = {}
        conflicts: list[str] = []
        for trade in open_trades:
            if not trade.amount:
                continue
            if trade.pair in trades_by_pair:
                conflicts.append(f"{trade.pair}: multiple open database trades")
                continue
            trades_by_pair[trade.pair] = trade

        for pair, position in positions.items():
            trade = trades_by_pair.pop(pair, None)
            if trade is None:
                conflicts.append(
                    f"{pair}: exchange has {position.side} {position.position:g}, "
                    "database has no open trade"
                )
                continue
            if not isclose(position.leverage, 1.0, rel_tol=0.0, abs_tol=1e-9):
                conflicts.append(
                    f"{pair}: exchange leverage is {position.leverage:g}, "
                    "but this Portfolio Margin release requires 1x"
                )
            if position.side != trade.trade_direction:
                conflicts.append(
                    f"{pair}: exchange side is {position.side}, "
                    f"database side is {trade.trade_direction}"
                )
            contract_size = self.get_contract_size(pair)
            amount_tolerance = max((contract_size or 1.0) * 1e-6, 1e-12)
            if not isclose(
                position.position,
                trade.amount,
                rel_tol=1e-9,
                abs_tol=amount_tolerance,
            ):
                conflicts.append(
                    f"{pair}: exchange amount is {position.position:g}, "
                    f"database amount is {trade.amount:g}"
                )

        for pair, trade in trades_by_pair.items():
            conflicts.append(
                f"{pair}: database has {trade.trade_direction} {trade.amount:g}, "
                "exchange has no matching position"
            )

        conflicts.extend(self._portfolio_margin_open_order_conflicts(open_trades))
        conflicts.extend(self._portfolio_margin_unsupported_exposure_conflicts())

        if conflicts:
            raise OperationalException(
                "Binance Portfolio Margin trading blocked because live positions or "
                f"orders conflict with the trade database: {'; '.join(conflicts)}. "
                "Reconcile or close the conflicting state before starting the bot."
            )

    def get_historic_ohlcv(
        self,
        pair: str,
        timeframe: str,
        since_ms: int,
        candle_type: CandleType,
        is_new_pair: bool = False,
        until_ms: int | None = None,
    ) -> DataFrame:
        """
        Overwrite to introduce "fast new pair" functionality by detecting the pair's listing date
        Does not work for other exchanges, which don't return the earliest data when called with "0"
        :param candle_type: Any of the enum CandleType (must match trading mode!)
        """
        if is_new_pair and candle_type in (CandleType.SPOT, CandleType.FUTURES, CandleType.MARK):
            with self._loop_lock:
                x = self.loop.run_until_complete(
                    self._async_get_candle_history(pair, timeframe, candle_type, 0)
                )
            if x and x[3] and x[3][0] and x[3][0][0] > since_ms:
                # Set starting date to first available candle.
                since_ms = x[3][0][0]
                logger.info(
                    f"Candle-data for {pair} available starting with "
                    f"{datetime.fromtimestamp(since_ms // 1000, tz=UTC).isoformat()}."
                )
                if until_ms and since_ms >= until_ms:
                    logger.warning(
                        f"No available candle-data for {pair} before "
                        f"{dt_from_ts(until_ms).isoformat()}"
                    )
                    return DataFrame(columns=DEFAULT_DATAFRAME_COLUMNS)

        if (
            not self._can_use_data_download_fast
            or self._config["exchange"].get("only_from_ccxt", False)
            or
            # only download timeframes with significant improvements,
            # otherwise fall back to rest API
            not (
                (candle_type == CandleType.SPOT and timeframe in ["1s", "1m", "3m", "5m"])
                or (
                    candle_type == CandleType.FUTURES
                    and timeframe in ["1m", "3m", "5m", "15m", "30m"]
                )
            )
        ):
            return super().get_historic_ohlcv(
                pair=pair,
                timeframe=timeframe,
                since_ms=since_ms,
                candle_type=candle_type,
                is_new_pair=is_new_pair,
                until_ms=until_ms,
            )
        else:
            # Download from data.binance.vision
            return self.get_historic_ohlcv_fast(
                pair=pair,
                timeframe=timeframe,
                since_ms=since_ms,
                candle_type=candle_type,
                is_new_pair=is_new_pair,
                until_ms=until_ms,
            )

    def get_historic_ohlcv_fast(
        self,
        pair: str,
        timeframe: str,
        since_ms: int,
        candle_type: CandleType,
        is_new_pair: bool = False,
        until_ms: int | None = None,
    ) -> DataFrame:
        """
        Fastly fetch OHLCV data by leveraging https://data.binance.vision.
        """
        with self._loop_lock:
            df = self.loop.run_until_complete(
                download_archive_ohlcv(
                    candle_type=candle_type,
                    pair=pair,
                    timeframe=timeframe,
                    since_ms=since_ms,
                    until_ms=until_ms,
                    markets=self.markets,
                )
            )

        # download the remaining data from rest API
        if df.empty:
            rest_since_ms = since_ms
        else:
            rest_since_ms = dt_ts(df.iloc[-1].date) + timeframe_to_msecs(timeframe)

        # make sure since <= until
        if until_ms and rest_since_ms > until_ms:
            rest_df = DataFrame()
        else:
            rest_df = super().get_historic_ohlcv(
                pair=pair,
                timeframe=timeframe,
                since_ms=rest_since_ms,
                candle_type=candle_type,
                is_new_pair=is_new_pair,
                until_ms=until_ms,
            )
        all_df = concat_safe([df, rest_df])
        return all_df

    def funding_fee_cutoff(self, open_date: datetime):
        """
        Funding fees are only charged at full hours (usually every 4-8h).
        Therefore a trade opening at 10:00:01 will not be charged a funding fee until the next hour.
        On binance, this cutoff is 15s.
        https://github.com/freqtrade/freqtrade/pull/5779#discussion_r740175931
        :param open_date: The open date for a trade
        :return: True if the date falls on a full hour, False otherwise
        """
        return open_date.minute == 0 and open_date.second < 15

    def fetch_funding_rates(self, symbols: list[str] | None = None) -> dict[str, dict[str, float]]:
        """
        Fetch funding rates for the given symbols.
        :param symbols: List of symbols to fetch funding rates for
        :return: Dict of funding rates for the given symbols
        """
        try:
            if self.trading_mode == TradingMode.FUTURES:
                rates = self._api.fetch_funding_rates(symbols)
                return rates
            return {}
        except ccxt.DDoSProtection as e:
            raise DDosProtection(e) from e
        except (ccxt.OperationFailed, ccxt.ExchangeError) as e:
            raise TemporaryError(
                f"Error in additional_exchange_init due to {e.__class__.__name__}. Message: {e}"
            ) from e

        except ccxt.BaseError as e:
            raise OperationalException(e) from e

    def dry_run_liquidation_price(
        self,
        pair: str,
        open_rate: float,
        is_short: bool,
        amount: float,
        stake_amount: float,
        leverage: float,
        wallet_balance: float,
        open_trades: list,
    ) -> float | None:
        """
        Important: Must be fetching data from cached values as this is used by backtesting!
        MARGIN: https://www.binance.com/en/support/faq/f6b010588e55413aa58b7d63ee0125ed
        PERPETUAL: https://www.binance.com/en/support/faq/b3c689c1f50a44cabb3a84e663b81d93

        :param pair: Pair to calculate liquidation price for
        :param open_rate: Entry price of position
        :param is_short: True if the trade is a short, false otherwise
        :param amount: Absolute value of position size incl. leverage (in base currency)
        :param stake_amount: Stake amount - Collateral in settle currency.
        :param leverage: Leverage used for this position.
        :param wallet_balance: Amount of margin_mode in the wallet being used to trade
            Cross-Margin Mode: crossWalletBalance
            Isolated-Margin Mode: isolatedWalletBalance
        :param open_trades: List of open trades in the same wallet

        # * Only required for Cross
        :param mm_ex_1: (TMM)
            Cross-Margin Mode: Maintenance Margin of all other contracts, excluding Contract 1
            Isolated-Margin Mode: 0
        :param upnl_ex_1: (UPNL)
            Cross-Margin Mode: Unrealized PNL of all other contracts, excluding Contract 1.
            Isolated-Margin Mode: 0
        :param other
        """
        cross_vars: float = 0.0

        # mm_ratio: Binance's formula specifies maintenance margin rate which is mm_ratio * 100%
        # maintenance_amt: (CUM) Maintenance Amount of position
        mm_ratio, maintenance_amt = self.get_maintenance_ratio_and_amt(pair, stake_amount)

        if self.margin_mode == MarginMode.CROSS:
            mm_ex_1: float = 0.0
            upnl_ex_1: float = 0.0
            pairs = [trade.pair for trade in open_trades]
            if self._config["runmode"] in ("live", "dry_run"):
                funding_rates = self.fetch_funding_rates(pairs)
            for trade in open_trades:
                if trade.pair == pair:
                    # Only "other" trades are considered
                    continue
                if self._config["runmode"] in ("live", "dry_run"):
                    mark_price = funding_rates[trade.pair]["markPrice"]
                else:
                    # Fall back to open rate for backtesting
                    mark_price = trade.open_rate
                mm_ratio1, maint_amnt1 = self.get_maintenance_ratio_and_amt(
                    trade.pair, trade.stake_amount
                )
                maint_margin = trade.amount * mark_price * mm_ratio1 - maint_amnt1
                mm_ex_1 += maint_margin

                upnl_ex_1 += trade.amount * mark_price - trade.amount * trade.open_rate

            cross_vars = upnl_ex_1 - mm_ex_1

        side_1 = -1 if is_short else 1

        if maintenance_amt is None:
            raise OperationalException(
                "Parameter maintenance_amt is required by Binance.liquidation_price"
                f"for {self.trading_mode}"
            )

        if self.trading_mode == TradingMode.FUTURES:
            return (
                (wallet_balance + cross_vars + maintenance_amt) - (side_1 * amount * open_rate)
            ) / ((amount * mm_ratio) - (side_1 * amount))
        else:
            raise OperationalException(
                "Freqtrade only supports isolated futures for leverage trading"
            )

    def load_leverage_tiers(self) -> dict[str, list[dict]]:
        if self.trading_mode == TradingMode.FUTURES:
            if self._config["dry_run"]:
                leverage_tiers_path = Path(__file__).parent / "binance_leverage_tiers.json"
                with leverage_tiers_path.open() as json_file:
                    return json_load(json_file)
            else:
                return self.get_leverage_tiers()
        else:
            return {}

    async def _async_get_trade_history_id_startup(
        self, pair: str, since: int
    ) -> tuple[list[list], str]:
        """
        override for initial call

        Binance only provides a limited set of historic trades data.
        Using from_id=0, we can get the earliest available trades.
        So if we don't get any data with the provided "since", we can assume to
        download all available data.
        """
        t, from_id = await self._async_fetch_trades(pair, since=since)
        if not t:
            return [], "0"
        return t, from_id

    async def _async_get_trade_history_id(
        self, pair: str, until: int, since: int, from_id: str | None = None
    ) -> tuple[str, list[list]]:
        logger.info(f"Fetching trades for {pair} from Binance, {from_id=}, {since=}, {until=}")

        if (
            not self._config["exchange"].get("only_from_ccxt", False)
            and self._can_use_data_download_fast
        ):
            if from_id is None or not since:
                trades = await self._api_async.fetch_trades(
                    pair,
                    params={
                        self._ft_has["trades_pagination_arg"]: "0",
                    },
                    limit=5,
                )
                listing_date: int = trades[0]["timestamp"]
                since = max(since, listing_date)

            _, res = await download_archive_trades(
                CandleType.FUTURES if self.trading_mode == "futures" else CandleType.SPOT,
                pair,
                since_ms=since,
                until_ms=until,
                markets=self.markets,
            )

            if not res:
                end_time = since
                end_id = from_id
            else:
                end_time = res[-1][0]
                end_id = res[-1][1]

            if end_time and end_time >= until:
                return pair, res
            else:
                _, res2 = await super()._async_get_trade_history_id(
                    pair, until=until, since=end_time, from_id=end_id
                )
                res.extend(res2)
                return pair, res

        return await super()._async_get_trade_history_id(
            pair, until=until, since=since, from_id=from_id
        )

    def _check_delisting_futures(self, pair: str) -> datetime | None:
        delivery_time = self.markets.get(pair, {}).get("info", {}).get("deliveryDate", None)
        if delivery_time:
            if isinstance(delivery_time, str) and (delivery_time != ""):
                delivery_time = int(delivery_time)

            # Binance set a very high delivery time for all perpetuals.
            # We compare with delivery time of BTC/USDT:USDT which assumed to never be delisted
            btc_delivery_time = (
                self.markets.get("BTC/USDT:USDT", {}).get("info", {}).get("deliveryDate", None)
            )

            if delivery_time == btc_delivery_time:
                return None

            delivery_time = dt_from_ts(delivery_time)

        return delivery_time

    def check_delisting_time(self, pair: str) -> datetime | None:
        """
        Check if the pair gonna be delisted.
        By default, it returns None.
        :param pair: Market symbol
        :return: Datetime if the pair gonna be delisted, None otherwise
        """
        if self._config["runmode"] not in TRADE_MODES:
            return None

        if self.trading_mode == TradingMode.FUTURES:
            return self._check_delisting_futures(pair)
        return self._get_spot_pair_delist_time(pair, refresh=False)

    def _get_spot_delist_schedule(self):
        """
        Get the delisting schedule for spot pairs
        Only works in live mode as it requires API keys,
        Return sample:
        [{
            "delistTime": "1759114800000",
            "symbols": [
                "OMNIBTC",
                "OMNIFDUSD",
                "OMNITRY",
                "OMNIUSDC",
                "OMNIUSDT"
            ]
        }]
        """
        try:
            delist_schedule = self._api.sapi_get_spot_delist_schedule()
            return delist_schedule
        except ccxt.DDoSProtection as e:
            raise DDosProtection(e) from e
        except (ccxt.NetworkError, ccxt.OperationFailed, ccxt.ExchangeError) as e:
            raise TemporaryError(
                f"Could not get delist schedule {e.__class__.__name__}. Message: {e}"
            ) from e
        except ccxt.BaseError as e:
            raise OperationalException(e) from e

    def _get_spot_pair_delist_time(self, pair: str, refresh: bool = False) -> datetime | None:
        """
        Get the delisting time for a pair if it will be delisted
        :param pair: Pair to get the delisting time for
        :param refresh: true if you need fresh data
        :return: int: delisting time None if not delisting
        """

        if not pair or not self._config["runmode"] == RunMode.LIVE:
            # Endpoint only works in live mode as it requires API keys
            return None

        cache = self._spot_delist_schedule_cache

        if not refresh:
            if delist_time := cache.get(pair, None):
                return delist_time

        delist_schedule = self._get_spot_delist_schedule()

        if delist_schedule is None:
            return None

        for schedule in delist_schedule:
            delist_dt = dt_from_ts(int(schedule["delistTime"]))
            for symbol in schedule["symbols"]:
                ft_symbol = next(
                    (
                        pair
                        for pair, market in self.markets.items()
                        if market.get("id", None) == symbol
                    ),
                    None,
                )
                if ft_symbol is None:
                    continue

                cache[ft_symbol] = delist_dt

        return cache.get(pair, None)


class Binanceusdm(Binance):
    """Binance USDM Exchange
    Same as Binance - only futures trading is supported (via ccxt).

    Not actually necessary, binance should be preferred.
    """

    _supported_trading_mode_margin_pairs: list[tuple[TradingMode, MarginMode]] = [
        (TradingMode.FUTURES, MarginMode.CROSS),
        (TradingMode.FUTURES, MarginMode.ISOLATED),
    ]


class Binanceus(Binance):
    """Binance US exchange class.
    Minimal adjustment to disable futures trading for the US subsidiary of Binance
    """

    _supported_trading_mode_margin_pairs: list[tuple[TradingMode, MarginMode]] = [
        (TradingMode.SPOT, MarginMode.NONE),
    ]
    # binance vision does not have data for binanceus
    _can_use_data_download_fast = False
