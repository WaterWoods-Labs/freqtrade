"""Versioned, exchange-neutral bot capital contract. Amounts are quote notional at 1x."""

import re
from math import isfinite
from typing import Any


BOT_RISK_VERSION = 1
BOT_RISK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "integer", "enum": [1]},
        "account_namespace": {"type": "string", "pattern": "^[a-z0-9][a-z0-9_-]{7,63}$"},
        "capital_usdt": {"type": "number", "minimum": 0, "exclusiveMinimum": True},
        "reserve_ratio": {"type": "number", "minimum": 0, "maximum": 1, "exclusiveMaximum": True},
        "max_total_notional_usdt": {"type": "number", "minimum": 0, "exclusiveMinimum": True},
        "pair_limits": {
            "type": "object",
            "minProperties": 1,
            "additionalProperties": {"type": "number", "minimum": 0, "exclusiveMinimum": True},
        },
        "max_initial_notional_usdt": {"type": "number", "minimum": 0, "exclusiveMinimum": True},
        "max_add_notional_usdt": {"type": "number", "minimum": 0, "exclusiveMinimum": True},
        "max_occupied_symbols": {"type": "integer", "minimum": 1},
    },
    "required": [
        "schema_version",
        "account_namespace",
        "capital_usdt",
        "reserve_ratio",
        "max_total_notional_usdt",
        "pair_limits",
        "max_initial_notional_usdt",
        "max_add_notional_usdt",
        "max_occupied_symbols",
    ],
    "additionalProperties": False,
}


def positive(value):
    return type(value) in (int, float) and isfinite(value) and value > 0


def _validate_pairs(pairs, total):
    if not isinstance(pairs, dict) or not pairs:
        raise ValueError("At least one explicit pair limit is required")
    for pair, limit in pairs.items():
        if not isinstance(pair, str) or not re.fullmatch(r"[A-Z0-9_]+/USDT:USDT", pair):
            raise ValueError("Contract v1 supports explicit USDT linear futures pairs")
        if not positive(limit) or limit > total:
            raise ValueError("Pair limit must be positive and at most the total limit")


def validate_bot_risk(risk):
    if not isinstance(risk, dict) or set(risk) != set(BOT_RISK_SCHEMA["required"]):
        raise ValueError("bot_risk fields must match contract v1")
    if type(risk["schema_version"]) is not int or risk["schema_version"] != 1:
        raise ValueError("Unsupported bot_risk schema")
    if not isinstance(risk["account_namespace"], str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9_-]{7,63}", risk["account_namespace"]
    ):
        raise ValueError("Invalid bot_risk account namespace")
    for key in (
        "capital_usdt",
        "max_total_notional_usdt",
        "max_initial_notional_usdt",
        "max_add_notional_usdt",
    ):
        if not positive(risk[key]):
            raise ValueError(f"bot_risk {key} must be finite and positive")
    reserve = risk["reserve_ratio"]
    if type(reserve) not in (int, float) or not isfinite(reserve) or not 0 <= reserve < 1:
        raise ValueError("Invalid reserve ratio")
    total = risk["max_total_notional_usdt"]
    if total > risk["capital_usdt"] * (1 - reserve) + 1e-9:
        raise ValueError("Total notional exceeds allocated capital after reserve")
    pairs = risk["pair_limits"]
    _validate_pairs(pairs, total)
    if any(
        risk[key] > min(pairs.values())
        for key in ("max_initial_notional_usdt", "max_add_notional_usdt")
    ):
        raise ValueError("Initial/add limit exceeds a pair limit")
    slots = risk["max_occupied_symbols"]
    if type(slots) is not int or not 1 <= slots <= len(pairs):
        raise ValueError("Occupied-symbol limit must fit the selected universe")
    return risk


def validate_bot_config(config):
    risk = validate_bot_risk(config["bot_risk"])
    expected = {
        "available_capital": risk["capital_usdt"],
        "tradable_balance_ratio": 1 - risk["reserve_ratio"],
        "max_open_trades": risk["max_occupied_symbols"],
        "stake_currency": "USDT",
        "trading_mode": "futures",
        "margin_mode": "cross",
    }
    for key, value in expected.items():
        if config.get(key) != value:
            raise ValueError(f"Derived config differs from bot_risk: {key}")
    pairs = config.get("exchange", {}).get("pair_whitelist", [])
    if len(pairs) != len(set(pairs)) or set(pairs) != set(risk["pair_limits"]):
        raise ValueError("Pair whitelist differs from bot_risk")
    return risk
