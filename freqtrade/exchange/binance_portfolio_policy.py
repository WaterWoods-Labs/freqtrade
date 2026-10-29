"""Pure Chan risk policy shared by the Portfolio Margin adapter and RPC gate."""

import re
from math import isfinite
from typing import Any

from freqtrade.binance_portfolio_usdt235 import PAIRS as USDT235_PAIRS


_PORTFOLIO_MARGIN_CHAN_POLICY = "chan_multi_pair"
_PORTFOLIO_MARGIN_DCA_POLICY = "chan_dca4_56_v1"
_PORTFOLIO_MARGIN_DCA_2000_POLICY = "chan_dca4_56_2000_v1"
_PORTFOLIO_MARGIN_DCA_235_POLICY = "chan_dca4_235_2000_v1"
_PORTFOLIO_MARGIN_DCA_POLICIES = (
    "bot_config_v1",
    _PORTFOLIO_MARGIN_DCA_POLICY,
    _PORTFOLIO_MARGIN_DCA_2000_POLICY,
    _PORTFOLIO_MARGIN_DCA_235_POLICY,
)
# Pair, aggregate, initial entry and single-add notional ceilings, in USDT.
_PORTFOLIO_MARGIN_DCA_LIMITS = {
    _PORTFOLIO_MARGIN_DCA_POLICY: (75, 950, 40, 35),
    _PORTFOLIO_MARGIN_DCA_2000_POLICY: (200, 2000, 100, 100),
    _PORTFOLIO_MARGIN_DCA_235_POLICY: (200, 2000, 100, 100),
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
    if isinstance(risk, dict) and risk.get("policy") == "bot_config_v1":
        return _config_risk_valid(risk)
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
    if policy == _PORTFOLIO_MARGIN_DCA_235_POLICY:
        pairs = USDT235_PAIRS
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


def _config_risk_valid(risk: dict) -> bool:
    from freqtrade.bot_risk import validate_bot_risk

    try:
        # Capital is validated at config loading; this policy verifies submission invariants.
        candidate = validate_bot_risk(
            {
                "schema_version": 1,
                "account_namespace": risk["account_namespace"],
                "capital_usdt": risk["max_total_entry_notional"],
                "reserve_ratio": 0,
                "max_total_notional_usdt": risk["max_total_entry_notional"],
                "pair_limits": risk["pairs"],
                "max_initial_notional_usdt": risk["max_initial_entry_notional"],
                "max_add_notional_usdt": risk["max_add_entry_notional"],
                "max_occupied_symbols": risk["max_occupied_symbols"],
            }
        )
        return type(risk.get("max_leverage")) in (int, float) and risk == portfolio_policy(
            candidate
        )
    except (KeyError, TypeError, ValueError):
        return False


def dca_order_limits(risk: dict) -> tuple[float, float]:
    if risk.get("policy") == "bot_config_v1":
        return risk["max_initial_entry_notional"], risk["max_add_entry_notional"]
    return _PORTFOLIO_MARGIN_DCA_LIMITS[risk["policy"]][2:]


def portfolio_policy(risk):
    from freqtrade.bot_risk import validate_bot_risk

    risk = validate_bot_risk(risk)
    return {
        "policy": "bot_config_v1",
        "account_namespace": risk["account_namespace"],
        "pairs": dict(risk["pair_limits"]),
        "allowed_sides": ["long", "short"],
        "max_leverage": 1,
        "max_total_entry_notional": risk["max_total_notional_usdt"],
        "max_initial_entry_notional": risk["max_initial_notional_usdt"],
        "max_add_entry_notional": risk["max_add_notional_usdt"],
        "max_occupied_symbols": risk["max_occupied_symbols"],
        "force_entry_order_type": "disabled",
        "reject_force_entry_price": True,
    }
