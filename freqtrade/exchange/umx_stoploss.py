"""UMX position-bound TPSL transport for native Freqtrade stop orders.

Only market stop losses on an existing net perpetual position are supported.
Trigger acknowledgement is not a fill: Freqtrade must query the execution order.
"""

import math
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from uuid import uuid4

import ccxt


class UMXStoploss:
    def _entry_protection(self, symbol, order_type, side, price, params):
        loss = getattr(self, "entry_stoploss", None)
        if loss is None or params.get("reduceOnly") is True:
            return params
        if (
            loss != -0.03
            or not self._is_futures_symbol(symbol)
            or order_type != "post_only"
            or side not in {"buy", "sell"}
            or price is None
            or not math.isfinite(price)
            or price <= 0
            or "tpslOrder" in params
        ):
            raise ccxt.InvalidOrder("Maker protection requires a PO perpetual entry and 3% stop")
        decimals = self._market(symbol).get("precision", {}).get("price")
        if not isinstance(decimals, int) or not 0 <= decimals <= 16:
            raise ccxt.InvalidOrder("Maker protection requires verified price precision")
        tick = Decimal(1).scaleb(-decimals)
        factor = Decimal("0.97") if side == "buy" else Decimal("1.03")
        stop = (Decimal(str(price)) * factor).quantize(
            tick, rounding=ROUND_CEILING if side == "buy" else ROUND_FLOOR
        )
        if stop <= 0 or (
            stop >= Decimal(str(price)) if side == "buy" else stop <= Decimal(str(price))
        ):
            raise ccxt.InvalidOrder("Maker stop cannot be represented at market price precision")
        # The protection travels with the PO order; it never waits for the bot's fill callback.
        # Actual venue activation on partial fills remains a live-acceptance requirement.
        return {
            **params,
            "clientOrderId": params.get("clientOrderId") or "ftme" + uuid4().hex[:28],
            "tpslOrder": {
                "tpslClOrdId": "ftsa" + uuid4().hex[:28],
                "stopLoss": format(stop, "f"),
                "stopLossType": "last_price",
                "slOrderType": "market",
                "slLimitPrice": None,
            },
        }

    def _stop_rows(self, symbol, *, history=False, end_id=None):
        params = {
            "symbol": self.market_id(symbol),
            "complexType": "tpsl",
            "businessType": "linear_perpetual",
        }
        if history:
            params["limit"] = 100
            if end_id:
                params["endId"] = end_id
        response = self.client.request(
            "GET",
            "/v2/history/orderComplexs" if history else "/v2/trade/openOrderComplex",
            params=self.client.private_params(params),
            private=True,
        )
        rows = response.get("data")
        if not isinstance(rows, list):
            raise ccxt.ExchangeError("UMX stop-order response is not a list")
        if any(row.get("symbol") != self.market_id(symbol) for row in rows):
            raise ccxt.ExchangeError("UMX stop-order response has a different symbol")
        return rows

    @staticmethod
    def _stop_info(raw):
        info = raw.get("tpslOrder")
        if not isinstance(info, dict):
            raise ccxt.ExchangeError("UMX stop-order parameters unavailable")
        return info

    def _create_stoploss(self, symbol, side, amount, params):
        stop = float(params.get("stopLossPrice", 0))
        if not self._is_futures_symbol(symbol) or params.get("reduceOnly") is not True:
            raise ccxt.InvalidOrder("UMX stop loss requires a reduce-only perpetual position")
        if side not in {"buy", "sell"} or not all(
            math.isfinite(v) and v > 0 for v in (stop, amount)
        ):
            raise ccxt.InvalidOrder("Invalid UMX stop-loss price, side or quantity")
        positions = [p for p in self.fetch_positions([symbol]) if p.get("contracts", 0) > 0]
        expected_side = "long" if side == "sell" else "short"
        if len(positions) != 1 or positions[0]["side"] != expected_side:
            raise ccxt.InvalidOrder("UMX stop loss must close the existing net position")
        if amount > positions[0]["contracts"] + 1e-10:
            raise ccxt.InvalidOrder("UMX stop-loss quantity exceeds the current position")
        qty = Decimal(str(amount)) * Decimal(str(self._contract_size(symbol)))
        # A prior timeout may have placed the protection. Adopt an exact owned match.
        # Venue rows for attached (ftsa*) protections carry the entry side; independent
        # (ftsl*) stops carry the exit side requested here.
        entry_side = "buy" if side == "sell" else "sell"
        matches = []
        for row in self._stop_rows(symbol):
            info = self._stop_info(row)
            client_id = str(info.get("tpslClOrdId", ""))
            if client_id.startswith(("ftsl", "ftsa")):
                attached = client_id.startswith("ftsa")
                accepted_stop = float(info.get("stopLoss", 0))
                protective = accepted_stop >= stop if side == "sell" else 0 < accepted_stop <= stop
                if (
                    row.get("side") != (entry_side if attached else side)
                    or (not attached and info.get("tpslMode") != "partially_position")
                    or not math.isclose(float(row.get("qty", 0)), qty)
                    or (not protective if attached else not math.isclose(accepted_stop, stop))
                    or info.get("slOrderType") != "market"
                    or info.get("stopLossType") != "last_price"
                ):
                    raise ccxt.InvalidOrder("Conflicting existing UMX stop protection")
                matches.append(row)
        if len(matches) > 1:
            raise ccxt.InvalidOrder("Multiple existing UMX stop protections require reconciliation")
        if matches:
            return self._parse_stoploss(matches[0], symbol)
        client_id = "ftsl" + uuid4().hex[:28]
        payload = self.client.request(
            "POST",
            "/v2/trade/stopPosition",
            private=True,
            data=self.client.private_params(
                {
                    "symbol": self.market_id(symbol),
                    "positionIdx": "net",
                    "tpslMode": "partially_position",
                    "tpslQty": format(qty, "f"),
                    "tpslClOrdId": client_id,
                    "stopLoss": format(Decimal(str(stop)), "f"),
                    "stopLossType": "last_price",
                    "slOrderType": "market",
                }
            ),
        )
        order_id = (payload.get("data") or {}).get("orderId")
        if not order_id:
            raise ccxt.ExchangeError("UMX stop-loss acknowledgement omitted order ID")
        # Independently read the venue's accepted parameters; never invent a live order.
        accepted = self._find_stoploss(str(order_id), symbol)
        info = self._stop_info(accepted)
        if (
            info.get("tpslClOrdId") != client_id
            or info.get("tpslMode") != "partially_position"
            or info.get("slOrderType") != "market"
            or info.get("stopLossType") != "last_price"
            or accepted.get("side") != side
            or not math.isclose(float(accepted.get("qty", 0)), qty)
            or not math.isclose(float(info.get("stopLoss", 0)), stop)
        ):
            raise ccxt.ExchangeError("UMX accepted protection differs from the requested stop")
        return self._parse_stoploss(accepted, symbol)

    def _find_stoploss(self, order_id, symbol):
        if not symbol:
            raise ccxt.BadRequest("UMX stop lookup requires an explicit symbol")
        for row in self._stop_rows(symbol):
            if str(row.get("complexOId")) == str(order_id):
                return row
        cursor = None
        for _ in range(10):
            rows = self._stop_rows(symbol, history=True, end_id=cursor)
            for row in rows:
                if str(row.get("complexOId")) == str(order_id):
                    return row
            if len(rows) < 100:
                break
            next_cursor = str(rows[-1].get("id") or rows[-1].get("complexOId") or "")
            if not next_cursor or next_cursor == cursor:
                raise ccxt.ExchangeError("UMX stop-history pagination did not advance")
            cursor = next_cursor
        raise ccxt.OrderNotFound("UMX stop order not found in open or recent history")

    def _parse_stoploss(self, raw, symbol):
        info = self._stop_info(raw)
        state = raw.get("status")
        if state not in {"live", "untrigger", "canceled", "fail", "slEffective", "filled"}:
            raise ccxt.ExchangeError(f"Unsupported UMX stop-order state: {state!r}")
        status = {
            "live": "open",
            "untrigger": "open",
            "canceled": "canceled",
            "fail": "rejected",
            "slEffective": "closed",
            "filled": "closed",
        }[state]
        amount = self._coin_amount_to_contracts(symbol, float(raw.get("qty", 0)))
        order = self._parse_order(
            {**raw, "orderId": str(raw["complexOId"]), "orderType": "stop_market"},
            symbol=symbol,
            amount=amount,
            status=status,
        )
        # Attached protections report the entry side; Freqtrade tracks the closing side.
        if str(info.get("tpslClOrdId", "")).startswith("ftsa"):
            if raw.get("side") not in {"buy", "sell"}:
                raise ccxt.ExchangeError("Invalid UMX attached-stop entry side")
            order["side"] = "sell" if raw["side"] == "buy" else "buy"
        order.update(
            stopLossPrice=float(info["stopLoss"]),
            stopPrice=float(info["stopLoss"]),
            filled=0.0,
            remaining=amount,
            cost=0.0,
        )
        if state == "slEffective":
            client_id = info.get("tpslClOrdId")
            if not client_id:
                raise ccxt.ExchangeError("Triggered UMX stop has no execution client ID")
            actual = self.client.order_info({"clientOrderId": client_id, "orderFilter": "order"})
            execution = actual.get("data") or {}
            if execution.get("symbol") != self.market_id(symbol) or not execution.get("orderId"):
                raise ccxt.ExchangeError("UMX stop execution order cannot be reconciled")
            order["info"] = {**raw, "triggeredOrderId": str(execution["orderId"])}
        if state == "filled":
            # Live-observed attached-stop trigger (2026-09-16, SOXL-USDT-PERP): the venue
            # reports "filled", the stop record carries no execution-order reference, and
            # order_info cannot resolve tpslClOrdId. Reconcile the execution order
            # strictly from bounded recent fills; never invent a linkage.
            order["info"] = {
                **raw,
                "triggeredOrderId": self._find_triggered_order_id(raw, symbol),
            }
        return order

    def _find_triggered_order_id(self, raw, symbol):
        """Resolve the execution order of a triggered stop from bounded recent fills."""
        try:
            stop_ms = int(raw["updateTime"])
            qty = float(raw["qty"])
        except (KeyError, TypeError, ValueError) as error:
            raise ccxt.ExchangeError("Triggered UMX stop lacks update time or quantity") from error
        if qty <= 0:
            raise ccxt.ExchangeError("Triggered UMX stop has no positive quantity")
        window_ms = 5 * 60 * 1000
        response = self.client.trade_history(
            symbol=self.market_id(symbol),
            business_type="linear_perpetual",
            begin_time=stop_ms,
            limit=100,
        )
        fills = response.get("data")
        if not isinstance(fills, list):
            raise ccxt.ExchangeError("UMX fill history response is not a list")
        candidates = {}
        for fill in fills:
            try:
                fill_ms = int(fill["fillTime"])
                fill_qty = float(fill["fillQty"])
            except (KeyError, TypeError, ValueError) as error:
                raise ccxt.ExchangeError("UMX fill record lacks time or quantity") from error
            if not stop_ms <= fill_ms <= stop_ms + window_ms:
                continue
            if fill.get("symbol") != self.market_id(symbol):
                continue
            if fill.get("orderType") != "market" or fill.get("side") not in {"buy", "sell"}:
                continue
            order_id = str(fill.get("orderId"))
            candidates.setdefault(order_id, []).append((fill_ms, fill_qty, fill["side"]))
        matches = [
            order_id
            for order_id, parts in candidates.items()
            if len({side for _, _, side in parts}) == 1
            and math.isclose(sum(q for _, q, _ in parts), qty, rel_tol=1e-9, abs_tol=1e-9)
        ]
        if len(matches) != 1:
            raise ccxt.ExchangeError(
                "Triggered UMX stop execution cannot be reconciled from fills: "
                f"expected exactly one market order with quantity {qty}, found {len(matches)}"
            )
        return matches[0]

    def _fetch_stoploss(self, order_id, symbol):
        return self._parse_stoploss(self._find_stoploss(order_id, symbol), symbol)

    def _cancel_stoploss(self, order_id, symbol):
        raw = self._find_stoploss(order_id, symbol)
        if raw.get("status") in {"live", "untrigger"}:
            self.client.request(
                "POST",
                "/v1/trade/cancelComplex",
                private=True,
                data=self.client.private_params(
                    {
                        "symbol": self.market_id(symbol),
                        "complexType": "tpsl",
                        "complexOId": order_id,
                    }
                ),
            )
        # A trigger can race with cancel; report the actual state instead of assuming cancellation.
        return self._fetch_stoploss(order_id, symbol)
