"""UMX exchange subclass using the native UMX REST API."""

import hashlib
import os
import uuid
from decimal import Decimal, InvalidOperation
from math import isclose, isfinite
from pathlib import Path
from typing import Any

from freqtrade.constants import ExchangeConfig
from freqtrade.enums import MarginMode, PriceType, TradingMode
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

    umx_maker_contract_version = 3
    umx_opportunity_contract_version = 1
    bot_risk_contract_version = 1

    def validate_config(self, config):
        super().validate_config(config)
        if "bot_risk" in config:
            from freqtrade.bot_risk import validate_bot_config

            try:
                validate_bot_config(config)
            except (ValueError, TypeError, KeyError) as exc:
                raise OperationalException(str(exc)) from exc

    def _bot_risk_ledger(self):
        from freqtrade.bot_risk import validate_bot_config
        from freqtrade.exchange.bot_risk_ledger import EntryLedger

        risk = validate_bot_config(self._config)
        namespace = hashlib.sha256(risk["account_namespace"].encode()).hexdigest()[:24]
        return EntryLedger(
            Path(self._config["user_data_dir"]) / f".bot-risk-{namespace}.json", risk
        )

    def _bot_risk_snapshot(self, target_pair=None, target_side=None):
        from freqtrade.bot_risk import positive

        risk = self._config["bot_risk"]
        exposure = dict.fromkeys(risk["pair_limits"], 0.0)
        pending = set()
        positions, orders = self.opportunity_account_snapshot()
        # Reject conditional entries outside ordinary orders.
        self.opportunity_conditional_snapshot()
        seen = set()
        for row in positions:
            amount = row.get("contracts")
            if type(amount) not in (int, float) or not isfinite(amount) or amount < 0:
                raise OperationalException("Invalid position size")
            if not amount:
                continue
            pair = row.get("symbol")
            if (
                pair not in exposure
                or pair in seen
                or row.get("side") not in {"long", "short"}
                or row.get("leverage") != 1
            ):
                raise OperationalException("Unowned or ambiguous account position")
            seen.add(pair)
            if pair == target_pair and row["side"] != ("long" if target_side == "buy" else "short"):
                raise OperationalException(
                    "Add direction conflicts with the actual account position"
                )
            mark, size = row.get("markPrice"), self.get_contract_size(pair)
            if not positive(mark) or not positive(size):
                raise OperationalException("Missing position mark or contract size")
            exposure[pair] += amount * size * mark
        for row in orders:
            if row.get("reduceOnly") is True or (row.get("info") or {}).get("reduceOnly") is True:
                continue
            pair = row.get("symbol")
            amount, price = row.get("amount"), row.get("price")
            if pair not in exposure or not positive(amount) or not positive(price):
                raise OperationalException("Unowned or unpriced open entry order")
            # Full order size deliberately over-reserves partial fills during snapshot lag.
            exposure[pair] += amount * price
            pending.add(pair)
        return exposure, pending

    def create_order(
        self,
        *,
        pair,
        ordertype,
        side,
        amount,
        rate,
        leverage,
        time_in_force="GTC",
        reduceOnly=False,
        initial_order=True,
        client_order_id=None,
    ):
        arguments = {
            "pair": pair,
            "ordertype": ordertype,
            "side": side,
            "amount": amount,
            "rate": rate,
            "leverage": leverage,
            "time_in_force": time_in_force,
            "reduceOnly": reduceOnly,
            "initial_order": initial_order,
            "client_order_id": client_order_id,
        }
        create = super().create_order
        # Exits and protection remain available when budgets are lowered or recovery is pending.
        if "bot_risk" not in self._config or reduceOnly or self._config["dry_run"]:
            return create(**arguments)
        from freqtrade.bot_risk import positive

        if (
            ordertype != "limit"
            or time_in_force not in {"PO", "IOC"}
            or type(leverage) not in (int, float)
            or leverage != 1
            or not positive(amount)
            or not positive(rate)
        ):
            raise OperationalException("Bot risk contract requires bounded 1x PO/IOC entries")
        ledger = self._bot_risk_ledger()
        ledger.reconcile_flat(self.fetch_order_by_client_id, self._bot_risk_snapshot)
        # Reserve the exact venue-rounded notional, including contract conversion.
        contracts = self.amount_to_precision(pair, self._amount_to_contracts(pair, amount))
        rounded_amount = self._contracts_to_amount(pair, contracts)
        rounded_rate = self.price_to_precision(pair, rate)
        arguments.update(amount=rounded_amount, rate=rounded_rate)
        arguments["client_order_id"] = client_order_id or "ftbr-" + uuid.uuid4().hex[:24]
        return ledger.submit(
            pair=pair,
            side=side,
            notional=rounded_amount * rounded_rate,
            initial=initial_order,
            client_id=arguments["client_order_id"],
            snapshot=lambda: self._bot_risk_snapshot(pair, side),
            send=lambda: create(**arguments),
        )

    def fetch_order_by_client_id(self, client_order_id, pair):
        """GET-only recovery of a durable entry intent; a miss never authorizes retry."""
        if self._config["dry_run"]:
            for order in self._dry_run_open_orders.values():
                if order.get("clientOrderId") == client_order_id:
                    return self.fetch_dry_run_order(order["id"])
            raise OperationalException("Unknown simulated entry; private recovery is prohibited")
        order = self._api.fetch_order_by_client_id(client_order_id, pair)
        return self._order_contracts_to_amount(order)

    def opportunity_funding_history(self, since, now):
        if self._config["dry_run"]:
            raise OperationalException("Private opportunity funding is prohibited in dry run")
        return self._api.fetch_funding_history(
            since=since, params={"endTime": int(now.timestamp() * 1000), "opportunityStrict": True}
        )

    def opportunity_account_snapshot(self):
        """Unfiltered positions and ordinary orders for strict ownership reconciliation."""
        positions = self._api.fetch_positions()
        raw = self._api.client.positions().get("data")
        if not isinstance(raw, list):
            raise OperationalException("UMX unfiltered position evidence is invalid")
        unfiltered = [self._api._parse_position(row) for row in raw]
        if any(row is None for row in unfiltered):
            raise OperationalException("UMX unfiltered position quantities are unavailable")

        def signature(rows):
            return sorted(
                (row["symbol"], row["side"], row["contracts"]) for row in rows if row["contracts"]
            )

        if signature(positions) != signature(unfiltered):
            raise OperationalException("UMX unfiltered and linear position scopes disagree")
        orders = [self._order_contracts_to_amount(o) for o in self._api.fetch_open_orders()]
        return positions, orders

    def opportunity_conditional_snapshot(self):
        """Reject conditional entries and return TPSL records for ownership validation."""
        client = self._api.client
        oco = client.open_orders({"orderFilter": "oco"}).get("data")
        if not isinstance(oco, list):
            raise OperationalException("UMX opportunity OCO evidence is invalid")
        results = {}
        for kind in ("trigger", "tpsl"):
            payload = client.request(
                "GET",
                "/v2/trade/openOrderComplex",
                private=True,
                params=client.private_params({"complexType": kind}),
            )
            rows = payload.get("data")
            if not isinstance(rows, list) or (kind == "trigger" and rows):
                raise OperationalException("UMX opportunity conditional entry scope is not empty")
            results[kind] = rows
        # Attached position stops also appear in openOrders(orderFilter=oco).
        # Accept only the same protection exposed by the TPSL endpoint; the
        # controller must still verify its ownership and coverage against trades.
        tpsl_by_id = {}
        for row in results["tpsl"]:
            order_id = str(row.get("complexOId") or "") if isinstance(row, dict) else ""
            if not order_id or order_id in tpsl_by_id:
                raise OperationalException("UMX opportunity TPSL identities are invalid")
            tpsl_by_id[order_id] = row
        seen = set()
        for row in oco:
            order_id = str(row.get("orderId") or "") if isinstance(row, dict) else ""
            protection = tpsl_by_id.get(order_id)
            if protection is None or order_id in seen:
                raise OperationalException("UMX opportunity OCO order lacks unique TPSL evidence")
            seen.add(order_id)
            try:
                quantity = Decimal(str(row.get("qty")))
                same_quantity = (
                    quantity.is_finite()
                    and quantity > 0
                    and quantity == Decimal(str(protection.get("qty")))
                )
            except InvalidOperation:
                same_quantity = False
            if (
                row.get("orderType") != "oco"
                or row.get("orderFilter") != "oco"
                or row.get("reduceOnly") is not True
                or not same_quantity
                or not isinstance(row.get("tpslOrder"), dict)
                or not row["tpslOrder"]
                or any(
                    row.get(field) != protection.get(field)
                    for field in ("symbol", "side", "status", "tpslOrder")
                )
            ):
                raise OperationalException("UMX opportunity OCO and TPSL protection disagree")
        return results["tpsl"]

    _ft_has: FtHas = {
        "ohlcv_candle_limit": 1000,
        "l2_limit_range_required": False,
        "l2_limit_upper": 5000,
        "order_time_in_force": ["GTC", "IOC", "FOK", "PO"],
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
        "stoploss_on_exchange": True,
        "stoploss_order_types": {"market": "stop_market"},
        "stoploss_query_requires_stop_flag": True,
        "stoploss_algo_order_info_id": "triggeredOrderId",
        "stop_price_type_field": "stopLossType",
        "stop_price_type_value_mapping": {PriceType.LAST: "last_price"},
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

    def _get_params(self, side, ordertype, leverage, reduceOnly, time_in_force="GTC"):
        if time_in_force == "PO":
            if ordertype != "limit":
                raise OperationalException("UMX PO requires a limit order")
            params = super()._get_params(side, ordertype, leverage, reduceOnly, "GTC")
            params["postOnly"] = True
            return params
        return super()._get_params(side, ordertype, leverage, reduceOnly, time_in_force)

    def _lev_prep(self, pair, leverage, side, accept_fail=False):
        if self._config.get("exchange", {}).get("umx_leverage_readonly", False):
            if leverage != 1:
                raise OperationalException("UMX read-only leverage policy requires 1x")
            if not self._config.get("dry_run", True):
                value = self._api.fetch_leverage(pair)
                if value.get("longLeverage") != 1 or value.get("shortLeverage") != 1:
                    raise OperationalException("UMX existing symbol leverage must already be 1x")
            return
        return super()._lev_prep(pair, leverage, side, accept_fail)

    def cancel_stoploss_order(self, order_id, pair, params=None):
        if self._config.get("dry_run", True):
            return super().cancel_stoploss_order(order_id, pair, params)
        super().cancel_stoploss_order(order_id, pair, params)
        # Resolve a trigger/cancel race to the actual execution order and filled amount.
        return self.fetch_stoploss_order(order_id, pair, params)

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
            "umx_public_request_interval": exchange_config.get("umx_public_request_interval", 0),
            "default_business_type": business_type,
            # This is Freqtrade's history-download shard width, not UMX's settlement cadence.
            "funding_fee_timeframe": self._ft_has["funding_fee_timeframe"],
        }
        wrapper_config.update(sanitized_ccxt_kwargs)
        # These policy fields cannot be overridden through generic ccxt options.
        wrapper_config["umx_order_read_only"] = exchange_config.get(
            "umx_order_read_only", False
        ) or (
            self._config.get("strategy") == "UMXChanB1S1Maker"
            and self._config.get("maker_live_approved") is not True
        )
        wrapper_config["umx_entry_stoploss"] = exchange_config.get("umx_entry_stoploss")

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
