"""Read exact, native PAPI execution evidence without changing any exchange order."""

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from freqtrade.exceptions import OperationalException, TemporaryError


class CancelReconcilePending(TemporaryError):
    safe_to_notify = True


class CancelReconcileConflict(OperationalException):
    safe_to_notify = True


def require(ok, reason):
    if not ok:
        raise CancelReconcileConflict(reason)


def number(value):
    if value in (None, ""):
        raise CancelReconcilePending("Exchange quantity or execution data is missing.")
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise CancelReconcileConflict("Invalid exchange quantity.") from None
    require(result.is_finite(), "Invalid exchange quantity.")
    return result


def boolean(value):
    require(str(value).lower() in {"true", "false"}, "Invalid exchange order flag.")
    return str(value).lower() == "true"


def rows(value):
    require(
        isinstance(value, list) and all(isinstance(r, dict) for r in value),
        "Unexpected exchange response structure.",
    )
    return value


def unique(records, key):
    values = {}
    for row in rows(records):
        ident = row.get(key)
        require(
            ident not in (None, "") and str(ident) not in values,
            "Duplicate or missing exchange identity.",
        )
        values[str(ident)] = row
    return values


@dataclass
class Execution:
    order: dict
    commission: Decimal
    notional: Decimal
    contracts: Decimal
    filled_contracts: Decimal


class Venue:
    def __init__(self, exchange):
        self.exchange = exchange
        self.api = exchange._api
        self.receipts = {}

    def exact(self, endpoint, params):
        if endpoint == "order":
            result = self.api.papiGetUmOrder(params)
        elif endpoint == "fills":
            result = self.api.papiGetUmUserTrades(params)
        else:
            require(endpoint == "stop", "Unsupported evidence source.")
            result = self.exchange._portfolio_algo_request("um/algo/algoOrder", "GET", params)
        canonical = (
            sorted(rows(result), key=lambda r: str(r.get("id"))) if endpoint == "fills" else result
        )
        stamp = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        key = (endpoint, tuple(sorted(params.items())))
        if self.receipts.setdefault(key, stamp) != stamp:
            raise CancelReconcilePending("Exact exchange order or execution history has changed.")
        return result

    def confirm_receipts(self):
        for endpoint, params in list(self.receipts):
            self.exact(endpoint, dict(params))

    def market(self, pair):
        return self.exchange.markets[pair]["id"], number(self.exchange.get_contract_size(pair))

    def regular(self, pair, order_id=None, client_id=None):
        symbol, _ = self.market(pair)
        require(bool(order_id or client_id), "Missing order identity.")

        def read(params):
            row = self.exact("order", {"symbol": symbol, **params})
            require(
                isinstance(row, dict)
                and row.get("symbol") == symbol
                and row.get("orderId") is not None
                and row.get("clientOrderId"),
                "Exchange order identity does not match.",
            )
            if order_id is not None:
                require(str(row["orderId"]) == str(order_id), "Exchange order ID does not match.")
            if client_id is not None:
                require(row["clientOrderId"] == client_id, "Exchange client ID does not match.")
            return row

        result = read({"orderId": order_id} if order_id else {"origClientOrderId": client_id})
        if client_id and order_id:
            other = read({"origClientOrderId": client_id})
            for key in (
                "orderId",
                "symbol",
                "clientOrderId",
                "side",
                "status",
                "origQty",
                "executedQty",
                "cumQuote",
            ):
                if other.get(key) != result.get(key):
                    raise CancelReconcilePending("Exact order lookups have not converged.")
        return result

    def execution(self, pair, raw, side, reduce):
        symbol, size = self.market(pair)
        require(
            raw.get("symbol") == symbol
            and raw.get("side") == side.upper()
            and raw.get("positionSide") == "BOTH"
            and boolean(raw.get("reduceOnly")) == reduce,
            "Order side or ownership disagrees.",
        )
        status = raw.get("status")
        if status in ("NEW", "PARTIALLY_FILLED", "PENDING_CANCEL"):
            raise CancelReconcilePending("Order is still active.")
        statuses = {
            "FILLED": "closed",
            "CANCELED": "canceled",
            "EXPIRED": "expired",
            "EXPIRED_IN_MATCH": "expired",
            "REJECTED": "rejected",
        }
        require(status in statuses, "Unknown terminal order status.")
        amount, filled = number(raw.get("origQty")), number(raw.get("executedQty"))
        require(
            amount > 0 and 0 <= filled <= amount and (status != "FILLED" or amount == filled),
            "Terminal order quantities disagree.",
        )
        ident = str(raw["orderId"])
        trades = rows(self.exact("fills", {"symbol": symbol, "orderId": ident, "limit": 1000}))
        if len(trades) >= 1000:
            raise CancelReconcilePending("Exact fill history exceeds the verified page.")
        unique(trades, "id")
        qty = quote = fee = Decimal(0)
        last = None
        for item in trades:
            require(
                str(item.get("orderId")) == ident
                and item.get("symbol") == symbol
                and item.get("side") == raw.get("side")
                and item.get("positionSide") == "BOTH",
                "Fill belongs to a different order or side.",
            )
            if item.get("commissionAsset") != "USDT":
                raise CancelReconcilePending("Exact USDT commission is unavailable.")
            contracts, price = number(item.get("qty")), number(item.get("price"))
            require(contracts > 0 and price > 0, "Invalid individual fill.")
            qty += contracts
            value = contracts * size * price
            if "quoteQty" in item:
                exact_quote = number(item["quoteQty"])
                require(
                    exact_quote > 0 and abs(value - exact_quote) < Decimal("0.00000001"),
                    "Execution price and quoted value disagree.",
                )
                value = exact_quote
            quote += value
            fee += number(item.get("commission"))
            last = max(last or 0, int(item["time"]))
        if qty != filled:
            raise CancelReconcilePending("Executed quantity and fill history have not converged.")
        reported = number(raw.get("cumQuote"))
        require(
            reported >= 0
            and (not filled or reported > 0)
            and (reported == quote or abs(reported - quote) < Decimal("0.00000001")),
            "Executed value disagrees with individual fills.",
        )
        price = number(raw.get("price"))
        norm = {
            "id": ident,
            "symbol": pair,
            "side": side,
            "type": raw["type"].lower(),
            "status": statuses[status],
            "amount": float(amount * size),
            "filled": float(filled * size),
            "remaining": float((amount - filled) * size),
            "cost": float(quote),
            "price": float(price) if price else None,
            "average": float(quote / (qty * size)) if qty else None,
            "timestamp": int(raw["time"]),
            "lastTradeTimestamp": last,
        }
        return Execution(norm, fee, quote, amount, filled)

    def stop(self, trade, local):
        pair = trade.pair
        symbol, size = self.market(pair)
        raw = self.exact("stop", {"algoId": local.order_id})
        require(
            isinstance(raw, dict)
            and str(raw.get("algoId")) == local.order_id
            and raw.get("symbol") == symbol
            and raw.get("side") == trade.exit_side.upper()
            and raw.get("positionSide") == "BOTH"
            and boolean(raw.get("reduceOnly"))
            and raw.get("orderType") == "STOP_MARKET"
            and number(raw.get("triggerPrice")) == number(local.stop_price)
            and number(raw.get("quantity")) * size == number(local.amount),
            "Conditional stop does not match the local protection.",
        )
        child = raw.get("actualOrderId")
        status = raw.get("algoStatus")
        if status in ("NEW", "TRIGGERING"):
            raise CancelReconcilePending("Conditional stop has not reached a terminal state.")
        quantities = [number(raw[k]) for k in ("actualQty", "filledQty", "executedQty") if k in raw]
        require(
            quantities
            and len(set(quantities)) == 1
            and 0 <= quantities[0] <= number(raw["quantity"]),
            "Stop parent has conflicting executed quantities.",
        )
        if status in ("CANCELED", "EXPIRED", "REJECTED") and child in (None, "", "0", 0):
            base = float(number(raw["quantity"]) * size)
            require(quantities[0] == 0, "Stop execution lacks its child order.")
            return Execution(
                {
                    "id": local.order_id,
                    "symbol": pair,
                    "side": trade.exit_side,
                    "type": "stoploss",
                    "status": "canceled" if status == "CANCELED" else status.lower(),
                    "amount": base,
                    "filled": 0.0,
                    "remaining": base,
                    "cost": 0,
                    "timestamp": int(raw["createTime"]),
                },
                Decimal(0),
                Decimal(0),
                number(raw["quantity"]),
                Decimal(0),
            )
        if not child or str(child) == "0":
            raise CancelReconcilePending("Stop execution child is not yet visible.")
        require(
            status in ("FINISHED", "TRIGGERED", "CANCELED", "EXPIRED"),
            "Unknown conditional stop state.",
        )
        raw_child = self.regular(pair, str(child))
        require(
            raw_child.get("type") == "MARKET"
            and quantities[0] == number(raw_child.get("executedQty")),
            "Stop parent and execution child disagree.",
        )
        execution = self.execution(pair, raw_child, trade.exit_side, True)
        execution.order.update(
            id=local.order_id,
            type="stoploss",
            stopPrice=float(raw["triggerPrice"]),
            amount=float(number(raw["quantity"]) * size),
            timestamp=int(raw["createTime"]),
        )
        execution.order["remaining"] = execution.order["amount"] - execution.order["filled"]
        require(execution.order["remaining"] >= 0, "Stop filled more than its parent.")
        return execution

    def snapshot(self):
        if self.exchange._portfolio_margin_unsupported_exposure_conflicts():
            raise CancelReconcileConflict("Account has unsupported exposure or orders.")
        positions = {}
        for p in rows(self.api.papiGetUmPositionRisk()):
            amount = number(p.get("positionAmt"))
            if amount:
                symbol = p.get("symbol")
                require(
                    symbol
                    and symbol not in positions
                    and p.get("positionSide") == "BOTH"
                    and number(p.get("leverage")) == 1,
                    "Unsupported live position mode or leverage.",
                )
                positions[symbol] = amount
        ordinary = unique_orders(
            self.api.papiGetUmOpenOrders(),
            "orderId",
            (
                "side",
                "status",
                "origQty",
                "executedQty",
                "reduceOnly",
                "positionSide",
                "type",
                "price",
                "clientOrderId",
            ),
        )
        stops = unique_orders(
            self.exchange._portfolio_algo_request("um/algo/openAlgoOrders", "GET", {}),
            "algoId",
            (
                "side",
                "algoStatus",
                "quantity",
                "triggerPrice",
                "reduceOnly",
                "positionSide",
                "orderType",
                "workingType",
                "closePosition",
                "priceProtect",
            ),
        )
        return positions, ordinary, stops


def unique_orders(records, key, fields):
    out = {}
    for row in rows(records):
        symbol, ident = row.get("symbol"), row.get(key)
        require(
            symbol and ident and (symbol, str(ident)) not in out,
            "Duplicate or missing venue order identity.",
        )
        out[symbol, str(ident)] = {k: row.get(k) for k in fields}
    return out
