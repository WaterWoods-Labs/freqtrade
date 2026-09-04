"""UMX exchange subclass using the native UMX REST API."""

import os
from math import isclose
from typing import Any

from freqtrade.constants import ExchangeConfig
from freqtrade.enums import MarginMode, TradingMode
from freqtrade.exceptions import OperationalException
from freqtrade.exchange import Exchange
from freqtrade.exchange.exchange_types import FtHas
from freqtrade.exchange.umx_api import UMXAsync, UMXSync
from freqtrade.exchange.umx_connector import (
    UMX_BUSINESS_LINEAR_PERPETUAL,
    UMX_BUSINESS_SPOT,
    UMX_DEFAULT_BASE_URL,
)


class UMX(Exchange):
    """UMX exchange class.

    UMX is not provided by ccxt, so this subclass injects a small ccxt-like
    REST wrapper while keeping Freqtrade's regular Exchange call path intact.

    Supports spot trading and U-margined linear perpetuals (cross margin). UMX
    only offers coin-level cross leverage (no isolated mode), so the futures
    margin mode is fixed to ``CROSS``.
    """

    _ft_has: FtHas = {
        "ohlcv_candle_limit": 1000,
        "l2_limit_range_required": False,
        "l2_limit_upper": 5000,
        "order_time_in_force": ["GTC", "IOC", "FOK"],
        "stoploss_on_exchange": False,
        # The batch /ticker/mini payload has no bid/ask fields. Single-symbol fetch_ticker
        # supplements these from depth, but pairlist filters consume the batch response.
        "tickers_have_bid_ask": False,
        "tickers_have_price": True,
        "exchange_has_overrides": {
            "createMarketOrder": True,
            "fetchMyTrades": True,
            "fetchTrades": False,
        },
    }

    _ft_has_futures: FtHas = {
        "mark_ohlcv_price": "mark",
        "mark_ohlcv_timeframe": "1h",
        "funding_fee_timeframe": "1h",
        # UMX totalEquity is account equity and already contains open-position UPL.
        # Wallets subtracts that UPL before exposing Wallet.total to avoid double counting it.
        "balance_includes_unrealized_pnl": True,
        "uses_leverage_tiers": False,
        "exchange_has_overrides": {
            "fetchMarkOHLCV": True,
            "fetchIndexOHLCV": True,
            "fetchPositions": True,
            "fetchLeverage": True,
            "fetchFundingRate": True,
            "setLeverage": True,
            # UMX has no per-symbol margin-mode switch (always cross) and exposes no
            # leverage-tier endpoints usable by Freqtrade.
            "setMarginMode": False,
            "fetchFundingRateHistory": True,
            "fetchFundingHistory": True,
            "fetchLeverageTiers": False,
            "fetchMarketLeverageTiers": False,
        },
    }

    _supported_trading_mode_margin_pairs: list[tuple[TradingMode, MarginMode]] = [
        (TradingMode.SPOT, MarginMode.NONE),
        (TradingMode.FUTURES, MarginMode.CROSS),
    ]

    def _init_ccxt(
        self, exchange_config: ExchangeConfig, sync: bool, ccxt_kwargs: dict[str, Any]
    ) -> UMXSync:
        removed_options = sorted(
            key for key in exchange_config if str(key).lower().startswith("xcoin_")
        )
        if removed_options:
            joined = ", ".join(f"exchange.{key}" for key in removed_options)
            raise OperationalException(
                f"Removed XCoin configuration option(s): {joined}. Use UMX configuration only."
            )

        if self.trading_mode not in (TradingMode.SPOT, TradingMode.FUTURES):
            raise OperationalException(
                "UMX adapter supports spot and U-margined perpetual (futures) trading only."
            )
        if self.trading_mode == TradingMode.FUTURES and self.margin_mode != MarginMode.CROSS:
            raise OperationalException(
                "UMX futures trading only supports cross margin mode. Set `margin_mode: cross`."
            )

        live_enabled = exchange_config.get("umx_live_trading_enabled", False)
        if not self._config.get("dry_run", True) and not live_enabled:
            raise OperationalException(
                "UMX live trading is disabled. Set "
                "`exchange.umx_live_trading_enabled=true` explicitly after dry-run testing."
            )

        api_key = os.environ.get("FREQTRADE__EXCHANGE__KEY") or os.environ.get("UMX_API_KEY")
        api_secret = os.environ.get("FREQTRADE__EXCHANGE__SECRET") or os.environ.get(
            "UMX_API_SECRET"
        )
        if not self._config.get("dry_run", True) and (not api_key or not api_secret):
            raise OperationalException(
                "UMX live trading requires API credentials from environment variables "
                "`FREQTRADE__EXCHANGE__KEY` and `FREQTRADE__EXCHANGE__SECRET`."
            )

        configured_base_urls = [
            exchange_config.get("umx_base_url"),
            exchange_config.get("base_url"),
            ccxt_kwargs.get("base_url"),
        ]
        if any(
            value and str(value).rstrip("/") != UMX_DEFAULT_BASE_URL
            for value in configured_base_urls
        ):
            raise OperationalException(
                f"UMX REST host is fixed to `{UMX_DEFAULT_BASE_URL}`; "
                "custom base URLs are disabled."
            )

        sanitized_ccxt_kwargs = {
            key: value
            for key, value in ccxt_kwargs.items()
            if key
            not in {
                "apiKey",
                "api_key",
                "key",
                "secret",
                "password",
                "privateKey",
                "private_key",
                "base_url",
            }
        }
        business_type = (
            UMX_BUSINESS_LINEAR_PERPETUAL
            if self.trading_mode == TradingMode.FUTURES
            else UMX_BUSINESS_SPOT
        )
        wrapper_config = {
            "apiKey": api_key or "",
            "secret": api_secret or "",
            "accountName": exchange_config.get("account_name")
            or exchange_config.get("accountName")
            or os.environ.get("UMX_ACCOUNT_NAME", ""),
            "base_url": UMX_DEFAULT_BASE_URL,
            "timeout": exchange_config.get("umx_timeout", 10),
            "default_business_type": business_type,
            # This is Freqtrade's history-download shard width, not UMX's settlement cadence.
            "funding_fee_timeframe": self._ft_has["funding_fee_timeframe"],
        }
        wrapper_config.update(sanitized_ccxt_kwargs)

        return UMXSync(wrapper_config) if sync else UMXAsync(wrapper_config)

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
        """Approximate cross-margin liquidation price for dry-run / backtesting.

        UMX uses coin-level cross margin and exposes no leverage-tier table, so the
        core ISOLATED formula does not apply. We approximate the liquidation price from
        the wallet balance available as margin and the maintenance-margin ratio derived
        from ``riskEngineRate``. Returns ``None`` when it cannot be computed safely; the
        core ``get_liquidation_price`` tolerates ``None``.
        """
        market = self.markets.get(pair)
        if not market or not amount:
            return None
        taker_fee_rate = market.get("taker")
        if taker_fee_rate is None:
            taker_fee_rate = 0.0005
        try:
            mm_ratio, _ = self.get_maintenance_ratio_and_amt(pair, stake_amount)
        except Exception:
            return None

        # Cross margin: collateral backing the position is the whole wallet balance.
        value = wallet_balance / amount
        mm_ratio_taker = mm_ratio + taker_fee_rate
        if is_short:
            denominator = 1 + mm_ratio_taker
            return (open_rate + value) / denominator if denominator else None
        denominator = 1 - mm_ratio_taker
        return (open_rate - value) / denominator if denominator else None

    def get_maintenance_ratio_and_amt(
        self,
        pair: str,
        notional_value: float,
    ) -> tuple[float, float | None]:
        """Maintenance margin ratio from UMX's ``riskEngineRate`` (no leverage tiers).

        Returns ``(mm_ratio, None)`` — UMX has no per-tier maintenance amount.
        """
        market = self.markets.get(pair, {})
        risk_rate = market.get("info", {}).get("riskEngineRate")
        if risk_rate in (None, ""):
            raise OperationalException(
                f"Maintenance margin rate for {pair} is unavailable for {self.name}"
            )
        return (float(risk_rate), None)

    def validate_existing_positions(
        self, positions: dict[str, Any], open_trades: list[Any]
    ) -> None:
        """Block live futures trading when exchange positions and the database disagree."""
        if self._config.get("dry_run", True) or self.trading_mode != TradingMode.FUTURES:
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

        if conflicts:
            details = "; ".join(conflicts)
            raise OperationalException(
                "UMX trading blocked because live futures positions conflict with the "
                f"trade database: {details}. Reconcile or close the conflicting positions "
                "before starting or resuming the bot. Use a dedicated exchange account to prevent "
                "untracked positions from being netted into new trades."
            )

    def close(self) -> None:
        if api := getattr(self, "_api", None):
            api.close()
        super().close()
