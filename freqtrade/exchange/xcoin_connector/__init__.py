"""Internal XCoin connector primitives used by the Freqtrade adapter."""

from freqtrade.exchange.xcoin_connector.client import XCoinClient
from freqtrade.exchange.xcoin_connector.constants import (
    XCOIN_BUSINESS_LINEAR_PERPETUAL,
    XCOIN_BUSINESS_SPOT,
    XCOIN_DEFAULT_BASE_URL,
    XCOIN_PERP_SUFFIX,
    XCOIN_TIMEFRAMES,
)
from freqtrade.exchange.xcoin_connector.symbols import ccxt_symbol_to_xcoin, xcoin_symbol_to_ccxt


__all__ = [
    "XCOIN_BUSINESS_LINEAR_PERPETUAL",
    "XCOIN_BUSINESS_SPOT",
    "XCOIN_DEFAULT_BASE_URL",
    "XCOIN_PERP_SUFFIX",
    "XCOIN_TIMEFRAMES",
    "XCoinClient",
    "ccxt_symbol_to_xcoin",
    "xcoin_symbol_to_ccxt",
]
