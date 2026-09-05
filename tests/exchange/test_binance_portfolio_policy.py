"""Both public entrypoints keep the complete reviewed risk gate."""

from copy import deepcopy

import pytest

from freqtrade.exchange.binance import Binance
from freqtrade.rpc.rpc import RPC, RPCException


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("account_namespace", "../account"),
        ("account_namespace", "short"),
        ("max_leverage", 2),
        ("max_leverage", True),
        ("max_total_entry_notional", float("nan")),
        ("max_total_entry_notional", 501),
        ("pairs", {"BTC/USDT:USDT": 100}),
        ("allowed_sides", ["long"]),
        ("force_entry_order_type", "market"),
        ("reject_force_entry_price", False),
    ],
)
def test_portfolio_risk_contract_shared_by_adapter_and_rpc(field, value):
    risk = {
        "account_namespace": "chan-live-account",
        "policy": "chan_multi_pair",
        "pairs": {f"{symbol}/USDT:USDT": 100 for symbol in ("BTC", "ETH", "BNB", "SOL", "SPY")},
        "allowed_sides": ["long", "short"],
        "max_leverage": 1,
        "max_total_entry_notional": 500,
        "force_entry_order_type": "disabled",
        "reject_force_entry_price": True,
    }
    original = deepcopy(risk)
    assert Binance._portfolio_margin_chan_risk_policy_valid(risk)
    assert RPC._portfolio_margin_chan_force_entry_pair_limits(risk) == risk["pairs"]
    assert risk == original
    risk[field] = value
    assert not Binance._portfolio_margin_chan_risk_policy_valid(risk)
    with pytest.raises(RPCException, match="risk policy is invalid"):
        RPC._portfolio_margin_chan_force_entry_pair_limits(risk)
