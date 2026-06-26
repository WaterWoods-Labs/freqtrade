"""XCoin exchange subclass using the native XCoin REST API."""

import logging
import os
from typing import Any

from freqtrade.constants import ExchangeConfig
from freqtrade.enums import MarginMode, TradingMode
from freqtrade.exceptions import OperationalException
from freqtrade.exchange import Exchange
from freqtrade.exchange.exchange_types import FtHas
from freqtrade.exchange.xcoin_api import XCoinAsync, XCoinSync
from freqtrade.exchange.xcoin_connector import (
    XCOIN_BUSINESS_LINEAR_PERPETUAL,
    XCOIN_BUSINESS_SPOT,
    XCOIN_DEFAULT_BASE_URL,
)


logger = logging.getLogger(__name__)


class Xcoin(Exchange):
    """XCoin exchange class.

    XCoin is not provided by ccxt, so this subclass injects a small ccxt-like
    REST wrapper while keeping Freqtrade's regular Exchange call path intact.

    Supports spot trading and U-margined linear perpetuals (cross margin). XCoin
    only offers coin-level cross leverage (no isolated mode), so the futures
    margin mode is fixed to ``CROSS``.
    """

    _ft_has: FtHas = {
        "ohlcv_candle_limit": 1000,
        "l2_limit_range_required": False,
        "l2_limit_upper": 5000,
        "order_time_in_force": ["GTC", "IOC", "FOK"],
        "stoploss_on_exchange": False,
        "tickers_have_bid_ask": True,
        "tickers_have_price": True,
        "exchange_has_overrides": {
            "createMarketOrder": True,
            "fetchMyTrades": False,
            "fetchTrades": False,
        },
    }

    _ft_has_futures: FtHas = {
        "mark_ohlcv_price": "mark",
        "mark_ohlcv_timeframe": "1h",
        "funding_fee_timeframe": "1h",
        "uses_leverage_tiers": False,
        "exchange_has_overrides": {
            "fetchMarkOHLCV": True,
            "fetchIndexOHLCV": True,
            "fetchPositions": True,
            "setLeverage": True,
            # XCoin has no per-symbol margin-mode switch (always cross) and exposes no
            # leverage-tier endpoints usable by Freqtrade.
            "setMarginMode": False,
            "fetchFundingRateHistory": True,
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
    ) -> XCoinSync:
        if self.trading_mode not in (TradingMode.SPOT, TradingMode.FUTURES):
            raise OperationalException(
                "XCoin adapter supports spot and U-margined perpetual (futures) trading only."
            )
        if self.trading_mode == TradingMode.FUTURES and self.margin_mode != MarginMode.CROSS:
            raise OperationalException(
                "XCoin futures trading only supports cross margin mode. "
                "Set `margin_mode: cross`."
            )

        live_enabled = exchange_config.get("xcoin_live_trading_enabled", False)
        if not self._config.get("dry_run", True) and not live_enabled:
            raise OperationalException(
                "XCoin live trading is disabled. Set "
                "`exchange.xcoin_live_trading_enabled=true` explicitly after dry-run testing."
            )

        api_key = os.environ.get("FREQTRADE__EXCHANGE__KEY") or os.environ.get("XCOIN_API_KEY")
        api_secret = os.environ.get("FREQTRADE__EXCHANGE__SECRET") or os.environ.get(
            "XCOIN_API_SECRET"
        )
        if not self._config.get("dry_run", True) and (not api_key or not api_secret):
            raise OperationalException(
                "XCoin live trading requires API credentials from environment variables "
                "`FREQTRADE__EXCHANGE__KEY` and `FREQTRADE__EXCHANGE__SECRET`."
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
            }
        }
        business_type = (
            XCOIN_BUSINESS_LINEAR_PERPETUAL
            if self.trading_mode == TradingMode.FUTURES
            else XCOIN_BUSINESS_SPOT
        )
        wrapper_config = {
            "apiKey": api_key or "",
            "secret": api_secret or "",
            "accountName": exchange_config.get("account_name")
            or exchange_config.get("accountName")
            or os.environ.get("XCOIN_ACCOUNT_NAME", ""),
            "base_url": exchange_config.get("xcoin_base_url")
            or exchange_config.get("base_url")
            or XCOIN_DEFAULT_BASE_URL,
            "timeout": exchange_config.get("xcoin_timeout", 10),
            "default_business_type": business_type,
        }
        wrapper_config.update(sanitized_ccxt_kwargs)

        return XCoinSync(wrapper_config) if sync else XCoinAsync(wrapper_config)

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

        XCoin uses coin-level cross margin and exposes no leverage-tier table, so the
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
        """Maintenance margin ratio from XCoin's ``riskEngineRate`` (no leverage tiers).

        Returns ``(mm_ratio, None)`` — XCoin has no per-tier maintenance amount.
        """
        market = self.markets.get(pair, {})
        risk_rate = market.get("info", {}).get("riskEngineRate")
        if risk_rate in (None, ""):
            raise OperationalException(
                f"Maintenance margin rate for {pair} is unavailable for {self.name}"
            )
        return (float(risk_rate), None)

    def close(self) -> None:
        if api := getattr(self, "_api", None):
            api.close()
        super().close()
