"""Evidence from a single partial-entry cancellation on the Binance USD-M PAPI."""

import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from math import isclose, ulp

from freqtrade.exceptions import OperationalException
from freqtrade.exchange.exchange_types import CcxtOrder


logger = logging.getLogger(__name__)
_STATUSES = {
    "NEW": "open",
    "PARTIALLY_FILLED": "open",
    "PENDING_CANCEL": "canceling",
    "FILLED": "closed",
    "CANCELED": "canceled",
    "EXPIRED": "expired",
    "EXPIRED_IN_MATCH": "expired",
}


def _number(info: dict, key: str) -> Decimal | None:
    value = info.get(key)
    if value in (None, ""):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise OperationalException(f"PAPI partial-entry receipt has invalid {key}.") from exc
    if not number.is_finite() or number < 0:
        raise OperationalException(f"PAPI partial-entry receipt has invalid {key}.")
    return number


def _updated(order: CcxtOrder) -> int:
    stamp = (order.get("info") or {}).get("updateTime") or order.get("lastUpdateTimestamp")
    return max(int(stamp), 0) if stamp else 0


@dataclass
class PortfolioPartialEntryEvidence:
    """Compare regular-order identity, execution and chronology in contract units."""

    order_id: str
    client_id: str
    pair: str
    market_id: str
    side: str
    amount: Decimal
    highest_fill: Decimal
    contract_size: Decimal
    latest_open_update: int = 0
    terminal: CcxtOrder | None = None
    terminal_status: str | None = None
    terminal_fill: Decimal | None = None
    terminal_price: Decimal | None = None
    terminal_quote: Decimal | None = None
    open_reads: dict[str, int] = field(default_factory=dict)
    terminal_get_seen: bool = False

    @classmethod
    def from_observed(
        cls, order: CcxtOrder, client_id: str, pair: str, market_id: str, size: float
    ):
        info = order.get("info") or {}
        amount, filled = _number(info, "origQty"), _number(info, "executedQty")
        if amount is None or filled is None or not 0 < filled < amount:
            raise OperationalException(
                "PAPI partial-entry observation lacks exact contract quantities."
            )
        for key, raw in (("amount", amount), ("filled", filled)):
            expected = float(raw) * size
            if not isclose(order[key], expected, rel_tol=0, abs_tol=ulp(expected)):
                raise OperationalException(
                    "PAPI partial-entry observation has conflicting contract units."
                )
        evidence = cls(
            str(order["id"]),
            client_id,
            pair,
            market_id,
            order["side"],
            amount,
            filled,
            Decimal(str(size)),
            _updated(order),
        )
        if (
            not evidence._matches(order, info, "initial GET")
            or info.get("status") != "PARTIALLY_FILLED"
        ):
            raise OperationalException(
                "PAPI partial-entry observation lacks the original Maker receipt."
            )
        return evidence

    def _matches(self, order: CcxtOrder, info: dict, source: str) -> bool:
        fields = (
            (info.get("orderId"), self.order_id),
            (order.get("id"), self.order_id),
            (info.get("clientOrderId"), self.client_id),
            (order.get("clientOrderId"), self.client_id),
            (info.get("symbol"), self.market_id),
            (order.get("symbol"), self.pair),
            (info.get("side"), self.side.upper()),
            (order.get("side"), self.side),
        )
        if any(actual is not None and str(actual) != expected for actual, expected in fields):
            raise OperationalException(
                f"PAPI partial-entry {source} returned a conflicting identity."
            )
        return all(actual is not None for actual, _ in fields)

    def observe(self, order: CcxtOrder | None, source: str) -> None:
        order = order or {}
        info = order.get("info") or {}
        if not self._matches(order, info, source):
            logger.info("PAPI partial-entry %s lacks exact order identity.", source)
            return
        status = order.get("status")
        if info.get("status") is None and status is None:
            return
        if not status or _STATUSES.get(str(info.get("status"))) != status:
            raise OperationalException(
                f"PAPI partial-entry {source} has an unexpected order state."
            )
        amount, filled = _number(info, "origQty"), _number(info, "executedQty")
        if amount is not None and (amount != self.amount or order.get("amount") != float(amount)):
            raise OperationalException(
                f"PAPI partial-entry {source} changed the original quantity."
            )
        if filled is not None:
            if filled > self.amount or order.get("filled") != float(filled):
                raise OperationalException(
                    f"PAPI partial-entry {source} has conflicting fill evidence."
                )
            self.highest_fill = max(self.highest_fill, filled)
        logger.info(
            "PAPI partial-entry %s observed status=%s, explicit_fill=%s.",
            source,
            status,
            filled is not None,
        )
        if status in ("open", "canceling"):
            stamp = _updated(order)
            self.latest_open_update = max(self.latest_open_update, stamp)
            if source != "DELETE":
                previous = self.open_reads.get(source, stamp)
                self.open_reads[source] = max(previous, stamp) if previous and stamp else 0
        else:
            self._record_terminal(order, amount, filled, source)
        stamp = _updated(self.terminal) if self.terminal else 0
        if (self.terminal_fill is not None and self.highest_fill > self.terminal_fill) or (
            stamp and stamp < self.latest_open_update
        ):
            raise OperationalException(
                "PAPI partial-entry terminal receipt conflicts with an observation."
            )

    def _record_terminal(
        self, order: CcxtOrder, amount: Decimal | None, filled: Decimal | None, source: str
    ) -> None:
        if self.terminal_status is not None and self.terminal_status != order["status"]:
            raise OperationalException("PAPI partial-entry terminal states disagree.")
        self.terminal_status = order["status"]
        if filled is None:
            return
        if self.terminal_fill is not None and self.terminal_fill != filled:
            raise OperationalException("PAPI partial-entry terminal quantities disagree.")
        self.terminal_fill = filled
        if amount is None:
            return
        if order["status"] == "closed" and filled != self.amount:
            raise OperationalException("PAPI partial-entry FILLED receipt is incomplete.")
        self._record_execution(order, filled, source)

    def _record_execution(self, order: CcxtOrder, filled: Decimal, source: str) -> None:
        price = _number(order["info"], "avgPrice") or None
        quote = _number(order["info"], "cumQuote") or None
        if price is not None and self.terminal_price is not None and price != self.terminal_price:
            raise OperationalException("PAPI partial-entry terminal execution prices disagree.")
        if quote is not None and self.terminal_quote is not None and quote != self.terminal_quote:
            raise OperationalException("PAPI partial-entry terminal execution values disagree.")
        self.terminal_price = price or self.terminal_price
        self.terminal_quote = quote or self.terminal_quote
        if self.terminal_price and self.terminal_quote and filled:
            # Binance publishes avgPrice rounded to the precision of that field.
            exponent = self.terminal_price.as_tuple().exponent
            quantum = Decimal(1).scaleb(exponent) if isinstance(exponent, int) else Decimal(0)
            if (
                abs(self.terminal_quote / (filled * self.contract_size) - self.terminal_price)
                >= quantum
            ):
                raise OperationalException(
                    "PAPI partial-entry average price disagrees with executed value."
                )
        self.open_reads.pop(source, None)
        if source != "DELETE" and (price or quote):
            self.terminal_get_seen = True
        if not self.terminal or _updated(order) >= _updated(self.terminal):
            self.terminal = order

    def result(self) -> CcxtOrder | None:
        if self.terminal is None:
            return None
        updated = _updated(self.terminal)
        if any(not stamp or stamp >= updated for stamp in self.open_reads.values()):
            return None
        filled = Decimal(str(self.terminal["info"]["executedQty"]))
        if self.terminal_quote is not None:
            quote = self.terminal_quote
        elif self.terminal_price is not None:
            quote = filled * self.contract_size * self.terminal_price
        else:
            return None
        # Binance reports cumQuote in quote currency. Freqtrade scales amount and cost together.
        contract_cost = quote / self.contract_size
        return {
            **self.terminal,
            "average": float(contract_cost / filled),
            "cost": float(contract_cost),
        }
