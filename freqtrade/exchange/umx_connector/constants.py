"""Constants for the internal UMX REST connector."""

UMX_DEFAULT_BASE_URL = "https://api.umx.com/api"

# UMX "businessType" values used across public/private endpoints.
UMX_BUSINESS_SPOT = "spot"
UMX_BUSINESS_LINEAR_PERPETUAL = "linear_perpetual"

# Suffix UMX uses for U-margined perpetual symbols, e.g. ``BTC-USDT-PERP``.
UMX_PERP_SUFFIX = "PERP"

UMX_TIMEFRAMES = {
    "1m": "1m",
    "3m": "3m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "1h": "1h",
    "2h": "2h",
    "4h": "4h",
    "6h": "6h",
    "8h": "8h",
    "12h": "12h",
    "1d": "1d",
    "3d": "3d",
    "1w": "1w",
    "1M": "1M",
}
