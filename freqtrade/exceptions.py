from decimal import Decimal
from typing import Literal


class FreqtradeException(Exception):
    """
    Freqtrade base exception. Handled at the outermost level.
    All other exception types are subclasses of this exception type.
    """


class OperationalException(FreqtradeException):
    """
    Requires manual intervention and will stop the bot.
    Most of the time, this is caused by an invalid Configuration.
    """


class ConfigurationError(OperationalException):
    """
    Configuration error. Usually caused by invalid configuration.
    """


class CancelReconciliationRequired(OperationalException):
    """A known cancellation stopped trading and has process-local identity evidence."""

    def __init__(
        self,
        kind: Literal["partial_entry", "regular", "stop"],
        pair: str,
        order_id: str,
        client_id: str | None = None,
        containment_orders: tuple[tuple[str, str | None], ...] = (),
        side: str | None = None,
        blocker: str | None = None,
        *,
        observed_amount: Decimal | None = None,
        observed_fill: Decimal | None = None,
        observed_base_amount: Decimal | None = None,
        observed_base_fill: Decimal | None = None,
    ) -> None:
        super().__init__(f"{kind} cancel outcome requires verified reconciliation.")
        self.kind = kind
        self.pair = pair
        self.order_id = order_id
        self.client_id = client_id
        self.containment_orders = containment_orders
        self.side = side
        self.blocker = blocker
        self.observed_amount = observed_amount
        self.observed_fill = observed_fill
        self.observed_base_amount = observed_base_amount
        self.observed_base_fill = observed_base_fill


class DependencyException(FreqtradeException):
    """
    Indicates that an assumed dependency is not met.
    This could happen when there is currently not enough money on the account.
    """


class PricingError(DependencyException):
    """
    Subclass of DependencyException.
    Indicates that the price could not be determined.
    Implicitly a buy / sell operation.
    """


class ExchangeError(DependencyException):
    """
    Error raised out of the exchange.
    Has multiple Errors to determine the appropriate error.
    """


class InvalidOrderException(ExchangeError):
    """
    This is returned when the order is not valid. Example:
    If stoploss on exchange order is hit, then trying to cancel the order
    should return this exception.
    """


class RetryableOrderError(InvalidOrderException):
    """
    This is returned when the order is not found.
    This Error will be repeated with increasing backoff (in line with DDosError).
    """


class InsufficientFundsError(InvalidOrderException):
    """
    This error is used when there are not enough funds available on the exchange
    to create an order.
    """


class TemporaryError(ExchangeError):
    """
    Temporary network or exchange related error.
    This could happen when an exchange is congested, unavailable, or the user
    has networking problems. Usually resolves itself after a time.
    """


class DDosProtection(TemporaryError):
    """
    Temporary error caused by DDOS protection.
    Bot will wait for a second and then retry.
    """


class CancelReconciliationPending(TemporaryError):
    """Preserve normal cancel retries unless this exact error caused a trading stop."""

    def __init__(self, message: str, incident: CancelReconciliationRequired) -> None:
        super().__init__(message)
        self.incident = incident


class StrategyError(FreqtradeException):
    """
    Errors with custom user-code detected.
    Usually caused by errors in the strategy.
    """
