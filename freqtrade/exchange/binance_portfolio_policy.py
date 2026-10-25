"""Pure Chan risk policy shared by the Portfolio Margin adapter and RPC gate."""

import re
from math import isfinite
from typing import Any


_PORTFOLIO_MARGIN_CHAN_POLICY = "chan_multi_pair"
_PORTFOLIO_MARGIN_DCA_POLICY = "chan_dca4_56_v1"
_PORTFOLIO_MARGIN_DCA_2000_POLICY = "chan_dca4_56_2000_v1"
_PORTFOLIO_MARGIN_DCA_POLICIES = (
    _PORTFOLIO_MARGIN_DCA_POLICY,
    _PORTFOLIO_MARGIN_DCA_2000_POLICY,
)
# Pair, aggregate, initial entry and single-add notional ceilings, in USDT.
_PORTFOLIO_MARGIN_DCA_LIMITS = {
    _PORTFOLIO_MARGIN_DCA_POLICY: (75, 950, 40, 35),
    _PORTFOLIO_MARGIN_DCA_2000_POLICY: (200, 2000, 100, 100),
}
_PORTFOLIO_MARGIN_CHAN_POLICIES = frozenset(
    {_PORTFOLIO_MARGIN_CHAN_POLICY, *_PORTFOLIO_MARGIN_DCA_POLICIES}
)
_PORTFOLIO_MARGIN_DCA_PAIRS = frozenset(
    f"{symbol}/USDT:USDT"
    for symbol in [
        "LITE",
        "SNDK",
        "CBRS",
        "MSTR",
        "NBIS",
        "SKHY",
        "WDC",
        "AAOI",
        "BE",
        "ARM",
        "AMD",
        "TSLA",
        "MU",
        "NVDA",
        "AXTI",
        "SPCX",
        "META",
        "DELL",
        "INTC",
        "AAPL",
        "MRVL",
        "CRCL",
        "HOOD",
        "GOOGL",
        "GLW",
        "COIN",
        "ORCL",
        "TSM",
        "PLTR",
        "RKLB",
        "AMZN",
        "IBM",
        "MSFT",
        "BABA",
        "MVLL",
        "KORU",
        "SOXL",
        "DRAM",
        "SNXX",
        "QQQ",
        "SPY",
        "EWY",
        "TQQQ",
        "XAU",
        "XAG",
        "COPPER",
        "NATGAS",
        "CL",
        "BZ",
        "BTC",
        "ETH",
        "SOL",
        "HYPE",
        "BNB",
        "XRP",
        "UNI",
    ]
)
_PORTFOLIO_MARGIN_CHAN_PAIRS = frozenset(
    {
        "BTC/USDT:USDT",
        "ETH/USDT:USDT",
        "BNB/USDT:USDT",
        "SOL/USDT:USDT",
        "SPY/USDT:USDT",
    }
)
_PORTFOLIO_MARGIN_CHAN_RISK_KEYS = frozenset(
    {
        "account_namespace",
        "policy",
        "pairs",
        "allowed_sides",
        "max_leverage",
        "max_total_entry_notional",
        "force_entry_order_type",
        "reject_force_entry_price",
    }
)
_PORTFOLIO_MARGIN_ACCOUNT_NAMESPACE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")


def _portfolio_margin_chan_risk_policy_valid(risk: Any) -> bool:
    if not isinstance(risk, dict) or set(risk) != _PORTFOLIO_MARGIN_CHAN_RISK_KEYS:
        return False
    pair_limits = risk.get("pairs")
    max_leverage = risk.get("max_leverage")
    max_total_notional = risk.get("max_total_entry_notional")
    account_namespace = risk.get("account_namespace")
    policy = risk.get("policy")
    if not isinstance(policy, str):
        return False
    dca = policy in _PORTFOLIO_MARGIN_DCA_POLICIES
    pair_cap, total_cap, *_ = _PORTFOLIO_MARGIN_DCA_LIMITS.get(policy, (100, 500, 0, 0))
    pairs = _PORTFOLIO_MARGIN_DCA_PAIRS if dca else _PORTFOLIO_MARGIN_CHAN_PAIRS
    return (
        policy in _PORTFOLIO_MARGIN_CHAN_POLICIES
        and isinstance(account_namespace, str)
        and _PORTFOLIO_MARGIN_ACCOUNT_NAMESPACE_PATTERN.fullmatch(account_namespace) is not None
        and isinstance(pair_limits, dict)
        and set(pair_limits) == pairs
        and all(
            not isinstance(limit, bool)
            and isinstance(limit, (int, float))
            and isfinite(limit)
            and 0 < limit <= pair_cap
            for limit in pair_limits.values()
        )
        and risk.get("allowed_sides") == ["long", "short"]
        and not isinstance(max_leverage, bool)
        and isinstance(max_leverage, (int, float))
        and isfinite(max_leverage)
        and max_leverage == 1
        and not isinstance(max_total_notional, bool)
        and isinstance(max_total_notional, (int, float))
        and isfinite(max_total_notional)
        and 0 < max_total_notional <= total_cap
        and risk.get("force_entry_order_type") == "disabled"
        and risk.get("reject_force_entry_price") is True
    )
