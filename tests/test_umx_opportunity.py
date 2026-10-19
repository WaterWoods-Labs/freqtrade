"""Accounting, durable batch and fail-closed boundaries independent of exchange writes."""

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import ccxt
import pytest

from freqtrade.exceptions import OperationalException
from freqtrade.exchange.umx_api import UMXSync
from freqtrade.freqtradebot import FreqtradeBot
from freqtrade.umx_opportunity import STRATEGY, EntryIntent, Journal, OpportunityController, enabled


D = Decimal


def intent(pair="A/USDT:USDT", signal="signal", amount="4.75"):
    return EntryIntent(
        "ftop" + signal,
        "batch",
        pair,
        "long",
        "B1_DIV",
        signal,
        (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
        "100",
        amount,
    )


@pytest.fixture
def controller(tmp_path, mocker):
    mocker.patch("freqtrade.umx_opportunity.Trade.get_trades_proxy", return_value=[])
    mocker.patch("freqtrade.umx_opportunity.PairLocks.is_global_lock", return_value=False)
    bot = SimpleNamespace(
        config={
            "exchange": {"name": "umx", "pair_whitelist": ["A/USDT:USDT", "B/USDT:USDT"]},
            "available_capital": 1000,
            "max_open_trades": 5,
            "dry_run": False,
            "db_url": "sqlite:///fixture.sqlite",
            "user_data_dir": tmp_path,
        },
        strategy=SimpleNamespace(
            position_adjustment_enable=False,
            order_time_in_force={"entry": "PO"},
            is_pair_locked=Mock(return_value=False),
        ),
        wallets=SimpleNamespace(update=Mock(), get_free=Mock(return_value=100000)),
        exchange=SimpleNamespace(
            get_contract_size=Mock(return_value=1),
            opportunity_account_snapshot=Mock(return_value=([], [])),
            opportunity_funding_history=Mock(return_value=[]),
            fetch_order_by_client_id=Mock(side_effect=ValueError("Unknown request")),
            price_to_precision=lambda pair, rate: rate,
        ),
        execute_entry=Mock(),
        active_pair_whitelist=["A/USDT:USDT", "B/USDT:USDT"],
    )
    result = OpportunityController(bot)
    mocker.patch.object(result, "_validate_conditionals")
    return result


def response(spec, *, status="rejected", filled=0):
    return {
        "id": spec.id,
        "clientOrderId": spec.id,
        "symbol": spec.pair,
        "side": "buy",
        "type": "limit",
        "amount": float(spec.amount),
        "price": float(spec.rate),
        "filled": filled,
        "remaining": float(spec.amount) - filled,
        "status": status,
        "timestamp": int(datetime.now(UTC).timestamp() * 1000),
    }


def test_batch_claims_are_atomic_and_persistent(tmp_path):
    path = tmp_path / "journal.sqlite"
    journal = Journal(path, {"version": 1})
    first = intent()
    journal.freeze("batch", datetime.now(UTC), [first], {})
    with pytest.raises(sqlite3.IntegrityError):
        journal.freeze("conflict", datetime.now(UTC), [first], {})
    assert not journal.has_batch("conflict")
    reopened = Journal(path, {"version": 1})
    assert reopened.claimed("signal")
    assert reopened.rows()[0][1] == "PLANNED"
    with pytest.raises(OperationalException, match="binding"):
        Journal(path, {"version": 2})


def test_unknown_request_never_expires_or_retries(controller):
    spec = intent()
    controller.journal.freeze("batch", datetime.now(UTC), [spec], {})
    controller.journal.transition(spec, "PLANNED", "SUBMITTING")
    assert controller.reconcile(startup=True) is False
    assert controller.journal.rows()[0][1] == "SUBMITTING"
    controller.bot.execute_entry.assert_not_called()
    assert controller.run(5) == 0
    assert controller.bot.exchange.fetch_order_by_client_id.call_count == 2


def test_unsubmitted_plan_blocks_until_skipped_on_restart(controller):
    spec = intent()
    controller.journal.freeze("batch", datetime.now(UTC), [spec], {})
    assert not controller.reconcile()
    assert controller.reconcile(startup=True)
    assert controller.journal.rows()[0][1] == "SKIPPED"
    assert controller.journal.claimed(spec.signal_key)


def test_done_trade_history_loss_blocks_capital_reset(controller, mocker):
    spec = intent()
    controller.journal.freeze("batch", datetime.now(UTC), [spec], {})
    controller.journal.transition(
        spec, "PLANNED", "DONE", {"id": "native", "native_registered": True}
    )
    mocker.patch.object(controller, "_owned_native_order", return_value=None)
    assert not controller.reconcile()
    assert controller.run(5) == 0


def test_account_balance_does_not_expand_strategy_capital(controller):
    snapshot = controller._snapshot()
    assert snapshot["equity"] == 1000
    assert snapshot["budget"] == 950


def order(order_side, amount, price, fee, **extra):
    return SimpleNamespace(
        safe_filled=amount,
        safe_price=price,
        average=price,
        cost=amount * price,
        ft_fee_cost=fee,
        has_fee_cost=fee is not None,
        ft_fee_currency="USDT",
        ft_is_open=False,
        ft_order_side=order_side,
        **extra,
    )


def trade(orders, **extra):
    values = {
        "strategy": STRATEGY,
        "pair": "A/USDT:USDT",
        "orders": orders,
        "entry_side": "buy",
        "is_short": False,
        "trade_direction": "long",
        "is_open": True,
        "open_date_utc": datetime.now(UTC) - timedelta(hours=2),
        "fee_open": 0.001,
        "fee_close": 0.001,
    }
    values.update(extra)
    return SimpleNamespace(**values)


def test_partial_exit_cash_actual_fees_funding_and_unrealized(controller, mocker):
    t = trade([order("buy", 5, 100, -0.5), order("sell", 2, 110, 0.22)])
    mocker.patch("freqtrade.umx_opportunity.Trade.get_trades_proxy", return_value=[t])
    controller.bot.exchange.opportunity_account_snapshot.return_value = (
        [{"symbol": t.pair, "contracts": 3, "side": "long", "markPrice": 120, "unrealizedPnl": 60}],
        [],
    )
    controller.bot.exchange.opportunity_funding_history.return_value = [
        {
            "id": "funding1",
            "symbol": t.pair,
            "code": "USDT",
            "amount": -5,
            "timestamp": int((datetime.now(UTC) - timedelta(hours=1)).timestamp() * 1000),
        }
    ]
    snapshot = controller._snapshot()
    assert snapshot["equity"] == D("1075.28")  # 1000+20+.5-.22-5+60
    assert snapshot["budget"] == D("661.5160")
    assert controller._snapshot()["equity"] == snapshot["equity"]  # funding ID is deduplicated
    controller.bot.exchange.opportunity_funding_history.side_effect = ValueError("GET failed")
    with pytest.raises(ValueError, match="GET failed"):
        controller._snapshot()


def test_dry_unrealized_uses_current_rate_without_private_api(controller, mocker):
    controller.bot.config["dry_run"] = True
    mocker.patch(
        "freqtrade.umx_opportunity.Trade.get_trades_proxy",
        return_value=[trade([order("buy", 5, 100, None)])],
    )
    controller.bot.exchange.get_rate = Mock(return_value=120)
    snapshot = controller._snapshot()
    assert snapshot["equity"] == D("1099.5")
    controller.bot.exchange.opportunity_account_snapshot.assert_not_called()
    controller.bot.exchange.opportunity_funding_history.assert_not_called()


def test_missing_actual_fee_blocks_live_accounting(controller, mocker):
    mocker.patch(
        "freqtrade.umx_opportunity.Trade.get_trades_proxy",
        return_value=[trade([order("buy", 5, 100, None)])],
    )
    with pytest.raises(ValueError, match="fees"):
        controller._snapshot()


def test_changed_fill_invalidates_previous_fee():
    previous = order("buy", 1, 100, 0.1)
    t = SimpleNamespace(select_order_by_order_id=lambda _: previous)
    OpportunityController.invalidate_stale_fee(t, {"id": "a", "filled": 2, "fee": None})
    assert previous.ft_fee_cost is None
    assert previous.ft_fee_currency is None
    assert previous.average is None
    assert previous.cost is None


def test_actual_exit_price_cannot_fall_back_to_requested_price(controller, mocker):
    exit_order = order("sell", 5, 200, 0.5)
    exit_order.average = exit_order.cost = None
    t = trade(
        [order("buy", 5, 100, 0), exit_order], is_open=False, close_date_utc=datetime.now(UTC)
    )
    mocker.patch("freqtrade.umx_opportunity.Trade.get_trades_proxy", return_value=[t])
    with pytest.raises(ValueError, match="fill price/cost"):
        controller._snapshot()
    exit_order.cost = 450  # Actual 90 average, not requested 200.
    assert controller._snapshot()["equity"] == D("949.5")


def test_new_partial_quantity_never_reuses_previous_average_or_cost():
    previous = order("buy", 1, 100, 0.1)
    t = SimpleNamespace(select_order_by_order_id=lambda _: previous)
    OpportunityController.invalidate_stale_fee(
        t, {"id": "a", "filled": 2, "fee": {"cost": 0.2, "currency": "USDT"}}
    )
    previous.safe_filled = 2
    with pytest.raises(ValueError, match="fill price/cost"):
        OpportunityController._actual_fill_price(previous)


def test_pending_same_id_changed_quantity_blocks(controller, mocker):
    pending = order(
        "buy",
        0,
        100,
        None,
        order_id="one",
        safe_amount=2,
        safe_remaining=2,
        safe_placement_price=100,
        ft_pair="A/USDT:USDT",
        side="buy",
    )
    pending.ft_is_open = True
    mocker.patch(
        "freqtrade.umx_opportunity.Trade.get_trades_proxy", return_value=[trade([pending])]
    )
    controller.bot.exchange.opportunity_account_snapshot.return_value = (
        [],
        [
            {
                "id": "one",
                "symbol": "A/USDT:USDT",
                "side": "buy",
                "amount": 3,
                "filled": 0,
                "remaining": 3,
                "price": 100,
            }
        ],
    )
    with pytest.raises(ValueError, match="pending order"):
        controller._snapshot()


def setup_batch(controller):
    bot = controller.bot
    bot.exchange.markets = {
        p: {"contractSize": 0.01, "precision": {"amount": 0}, "limits": {"amount": {}, "cost": {}}}
        for p in bot.active_pair_whitelist
    }
    bot.strategy.get_opportunity_candidate = lambda pair, current_time: {
        "rate": 100,
        "side": "long",
        "entry_tag": "B1_DIV",
        "signal_key": pair,
        "expires_at": current_time + timedelta(seconds=30),
    }


def test_entire_batch_frozen_before_first_request_no_reallocation(controller):
    setup_batch(controller)
    observed = []

    def execute(pair, stake, **kwargs):
        spec = kwargs["opportunity_intent"]
        rows = controller.journal.rows()
        assert len(rows) == 2
        observed.append(D(spec.amount))
        assert controller.before_submit(spec)
        assert controller.journal.rows()[len(observed) - 1][1] == "SUBMITTING"
        controller.after_response(spec, response(spec))
        controller.bot.wallets.get_free.return_value = 50  # cannot change second frozen order
        return False

    controller.bot.execute_entry = execute
    assert controller.run(5) == 0
    assert observed == [D("4.75"), D("4.75")]
    assert [state for _, state, _ in controller.journal.rows()] == ["DONE", "DONE"]
    controller.run(5)
    assert len(observed) == 2


def test_callback_exception_has_no_framework_fallback(controller):
    setup_batch(controller)
    controller.bot.strategy.get_opportunity_candidate = Mock(side_effect=ValueError("broken"))
    assert controller.run(5) == 0
    controller.bot.execute_entry.assert_not_called()
    assert controller.journal.rows() == []


def test_empty_observation_does_not_claim_decision_minute(controller):
    setup_batch(controller)
    controller.bot.strategy.get_opportunity_candidate = Mock(return_value=None)
    assert controller.run(5) == 0
    assert not controller.journal.has_batch(
        datetime.now(UTC).replace(second=0, microsecond=0).isoformat()
    )
    setup_batch(controller)
    controller.bot.execute_entry = Mock(return_value=False)
    controller.run(5)
    assert len(controller.journal.rows()) == 2


def test_unknown_first_request_keeps_remaining_reservations(controller):
    setup_batch(controller)

    def execute(pair, stake, **kwargs):
        assert controller.before_submit(kwargs["opportunity_intent"])
        raise TimeoutError("Lost POST response")

    controller.bot.execute_entry = execute
    assert controller.run(5) == 0
    assert [state for _, state, _ in controller.journal.rows()] == ["SUBMITTING", "PLANNED"]


def test_execute_entry_cannot_bypass_controller(controller):
    bot = FreqtradeBot.__new__(FreqtradeBot)
    bot._umx_opportunity = controller
    with pytest.raises(OperationalException, match="frozen"):
        bot.execute_entry("A/USDT:USDT", 950)


@pytest.mark.parametrize("marker", [False, None, "true"])
def test_contract_markers_cannot_silently_disable_core(marker):
    strategy = SimpleNamespace(
        get_strategy_name=lambda: STRATEGY, umx_opportunity_contract_version=1
    )
    with pytest.raises(OperationalException, match="markers"):
        enabled(strategy, {"umx_opportunity_mode": marker})


@pytest.mark.parametrize("bad_mark", [0, -1, float("nan")])
def test_invalid_mark_cannot_expand_available_budget(controller, mocker, bad_mark):
    t = trade([order("buy", 5, 100, 0)])
    mocker.patch("freqtrade.umx_opportunity.Trade.get_trades_proxy", return_value=[t])
    controller.bot.exchange.opportunity_account_snapshot.return_value = (
        [
            {
                "symbol": t.pair,
                "contracts": 5,
                "side": "long",
                "markPrice": bad_mark,
                "unrealizedPnl": 0,
            }
        ],
        [],
    )
    with pytest.raises(ValueError):
        controller._snapshot()


def test_native_stop_close_keeps_independent_funding_cash(controller, mocker):
    t = trade(
        [order("buy", 5, 100, 0), order("stoploss", 5, 97, 0.5)],
        is_open=False,
        close_date_utc=datetime.now(UTC),
        funding_fees=0,
    )
    mocker.patch("freqtrade.umx_opportunity.Trade.get_trades_proxy", return_value=[t])
    controller.bot.exchange.opportunity_funding_history.return_value = [
        {
            "id": "stop_funding",
            "symbol": t.pair,
            "code": "USDT",
            "amount": -10,
            "timestamp": int((datetime.now(UTC) - timedelta(hours=1)).timestamp() * 1000),
        }
    ]
    snapshot = controller._snapshot()
    assert snapshot["equity"] == D("974.5")
    assert t.pair in snapshot["exited"]


def test_positive_entry_fee_reduces_next_budget(controller, mocker):
    t = trade([order("buy", 5, 100, 2)])
    mocker.patch("freqtrade.umx_opportunity.Trade.get_trades_proxy", return_value=[t])
    controller.bot.exchange.opportunity_account_snapshot.return_value = (
        [{"symbol": t.pair, "contracts": 5, "side": "long", "markPrice": 100, "unrealizedPnl": 0}],
        [],
    )
    snapshot = controller._snapshot()
    assert snapshot["equity"] == D(998)
    assert snapshot["budget"] == D("448.10")


@pytest.mark.parametrize(
    "changed",
    [
        {"remaining": -1},
        {"filled": 6},
        {"amount": 6},
        {"status": "unknown"},
        {"clientOrderId": "another"},
        {"symbol": "other"},
        {"side": "sell"},
    ],
)
def test_order_response_identity_and_quantity_fail_closed(controller, changed):
    spec = intent()
    with pytest.raises(ValueError):
        controller._verify_response(spec, {**response(spec), **changed})


def test_recovery_allows_only_float_dust_below_one_contract(controller):
    spec = intent(amount="0.3")
    controller.bot.exchange.get_contract_size.return_value = 0.1
    actual = response(spec, status="open", filled=0.1)
    actual["amount"] = 3 * 0.1
    actual["remaining"] = actual["amount"] - 0.1
    controller._verify_response(spec, actual)
    controller._verify_response(spec, {**actual, "filled": 3 * 0.1, "remaining": 0})
    with pytest.raises(ValueError):
        controller._verify_response(spec, {**actual, "amount": 0.4})


def test_unowned_conditionals_block_new_entries(controller):
    controller.bot.exchange.opportunity_conditional_snapshot = Mock(
        return_value=[{"complexOId": "unknown", "tpslOrder": {"tpslClOrdId": "unknown"}}]
    )
    with pytest.raises(ValueError, match="Unowned"):
        OpportunityController._validate_conditionals(controller, [], {})


def test_position_without_protection_blocks_new_entries(controller):
    controller.bot.exchange.opportunity_conditional_snapshot = Mock(return_value=[])
    with pytest.raises(ValueError, match="lacks verified"):
        OpportunityController._validate_conditionals(
            controller, [], {"A/USDT:USDT": (5, "long", 100)}
        )


def test_attached_protection_must_cover_actual_position(controller):
    import hashlib

    spec = intent()
    controller.journal.freeze("batch", datetime.now(UTC), [spec], {})
    controller.journal.transition(spec, "PLANNED", "ACKED")
    controller.bot.exchange.markets = {spec.pair: {"id": "A-USDT-PERP"}}
    controller.bot.exchange.opportunity_conditional_snapshot = Mock(
        return_value=[
            {
                "symbol": "A-USDT-PERP",
                "side": "buy",
                "qty": "1",
                "complexOId": "attached",
                "tpslOrder": {
                    "tpslClOrdId": "ftsa" + hashlib.sha256(spec.id.encode()).hexdigest()[:28],
                    "slOrderType": "market",
                    "stopLossType": "last_price",
                    "stopLoss": "97",
                },
            }
        ]
    )
    with pytest.raises(ValueError, match="protection parameters"):
        OpportunityController._validate_conditionals(
            controller, [], {spec.pair: (D(3), "long", D(100))}
        )


@pytest.mark.parametrize(
    "change",
    [
        {"qty": None},
        {"qty": "NaN"},
        {"qty": "Infinity"},
        {"qty": "1e1000"},
        {"createTime": None},
        {"createTime": 0},
        {"createTime": "nan"},
        {"createTime": "1.5"},
        {"id": None},
        {"symbol": None},
        {"actionType": None},
    ],
)
def test_strict_funding_rejects_missing_or_invalid_raw_evidence(change):
    raw = {
        "id": "bill",
        "actionType": "18",
        "symbol": "A-USDT-PERP",
        "currency": "USDT",
        "qty": "-0.2",
        "createTime": 1000,
    }
    with pytest.raises(ccxt.ExchangeError):
        UMXSync._validate_funding_bill({**raw, **change})


def test_strict_funding_page_cannot_be_missing_or_conflicting(mocker):
    api = UMXSync({"options": {"defaultType": "swap"}})
    mocker.patch.object(api.client, "bills", return_value={})
    with pytest.raises(ccxt.ExchangeError, match="page"):
        api.fetch_funding_history(since=1000, params={"endTime": 2000, "opportunityStrict": True})
    raw = {
        "id": "bill",
        "actionType": "18",
        "symbol": "A-USDT-PERP",
        "currency": "USDT",
        "qty": "-0.2",
        "createTime": 1000,
    }
    api.client.bills.return_value = {"data": [raw, {**raw, "qty": "0"}]}
    with pytest.raises(ccxt.ExchangeError, match="changed"):
        api.fetch_funding_history(since=1000, params={"endTime": 2000, "opportunityStrict": True})
