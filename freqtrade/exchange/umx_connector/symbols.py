"""Symbol conversion helpers for UMX spot and linear-perpetual markets.

Mapping:
- spot:               ``BTC-USDT``       <-> ``BTC/USDT``
- U-margined perp:    ``BTC-USDT-PERP``  <-> ``BTC/USDT:USDT``
"""

from freqtrade.exchange.umx_connector.constants import UMX_PERP_SUFFIX


def umx_symbol_to_ccxt(symbol: str) -> str:
    """Convert UMX symbol format to Freqtrade/ccxt symbol format."""
    if not symbol:
        return symbol
    if "/" in symbol:
        return symbol
    parts = symbol.split("-")
    if len(parts) >= 3 and parts[2] == UMX_PERP_SUFFIX:
        base, quote = parts[0], parts[1]
        # ccxt linear-perpetual notation settles in the quote currency.
        return f"{base}/{quote}:{quote}"
    base, quote, *_ = parts
    return f"{base}/{quote}"


def ccxt_symbol_to_umx(symbol: str) -> str:
    """Convert Freqtrade/ccxt symbol format to UMX symbol format."""
    if "-" in symbol and "/" not in symbol:
        return symbol
    if ":" in symbol:
        # Linear perpetual, e.g. ``BTC/USDT:USDT`` -> ``BTC-USDT-PERP``.
        pair, _settle = symbol.split(":", 1)
        base, quote = pair.split("/")
        return f"{base}-{quote}-{UMX_PERP_SUFFIX}"
    return symbol.replace("/", "-")
