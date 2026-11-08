import logging
from copy import deepcopy

import pytest

from freqtrade.constants import CANCEL_REASON
from freqtrade.exceptions import TemporaryError
from freqtrade.persistence import Trade
from tests.conftest import EXMS, get_patched_freqtradebot
from tests.conftest_trades import entry_side
from tests.conftest_trades_usdt import mock_trade_usdt_4


@pytest.fixture
def pending_cancel(mocker, default_conf_usdt, limit_order, fee):
    bot = get_patched_freqtradebot(mocker, default_conf_usdt)
    mocker.patch.object(bot, "_notify_enter_cancel")
    trade = mock_trade_usdt_4(fee, False)
    Trade.session.add(trade)
    Trade.commit()
    obj = trade.open_orders[0]
    order = deepcopy(limit_order[entry_side(False)])
    order.update(id=obj.order_id, status="open", filled=0.0)
    cancel = mocker.patch(f"{EXMS}.cancel_order_with_result", return_value=order)
    clock = mocker.patch("freqtrade.freqtradebot.monotonic", return_value=100.0)
    sleep = mocker.patch("freqtrade.freqtradebot.sleep")
    assert not bot.handle_cancel_enter(trade, order, obj, CANCEL_REASON["TIMEOUT"])
    return bot, trade, obj, order, cancel, clock, sleep


def test_delayed_zero_fill_cancel_is_confirmed_without_extra_queries(
    pending_cancel, mocker, caplog
):
    bot, _trade, obj, order, cancel, clock, sleep = pending_cancel
    pending = [r for r in caplog.get_records("setup") if "confirmation pending" in r.message]
    assert len(pending) == 1 and pending[0].levelno == logging.INFO
    assert obj.ft_is_open
    clock.return_value = 107.0
    terminal = {**order, "status": "canceled"}
    fetch = mocker.patch(f"{EXMS}.fetch_order", return_value=terminal)
    bot.manage_open_orders()
    assert fetch.call_count == 1
    assert cancel.call_count == 1
    sleep.assert_not_called()
    assert not obj.ft_is_open
    assert obj.status == "canceled"
    assert obj.filled == 0
    assert not bot._pending_entry_cancels
    assert any("status=canceled" in m and "elapsed_s=7.000" in m for m in caplog.messages)


@pytest.mark.parametrize("status,filled", [("closed", 1.0), ("canceled", 0.5)])
def test_cancel_racing_fill_reports_actual_terminal_outcome(
    pending_cancel, mocker, caplog, status, filled
):
    bot, trade, obj, order, cancel, _clock, sleep = pending_cancel
    terminal = {**order, "status": status, "filled": filled, "remaining": 0.0}
    # Let native update_trade_state consume the venue result, then stop before fee/position work.
    mocker.patch.object(bot.exchange, "check_order_canceled_empty", return_value=True)
    bot.update_trade_state(trade, obj.order_id, terminal)
    assert obj.status == status and obj.filled == filled
    assert not bot._pending_entry_cancels
    assert any(f"status={status} filled={filled}" in m for m in caplog.messages)
    assert cancel.call_count == 1
    sleep.assert_not_called()


def test_persistent_open_cancel_warns_and_keeps_order_open(pending_cancel, caplog):
    bot, trade, obj, order, cancel, clock, sleep = pending_cancel
    clock.return_value = 108.0
    caplog.clear()
    assert not bot.handle_cancel_enter(trade, order, obj, CANCEL_REASON["TIMEOUT"])
    assert obj.ft_is_open
    assert cancel.call_count == 2  # Existing native retry policy is unchanged.
    assert any(r.levelno == logging.WARNING and "checks=2" in r.message for r in caplog.records)
    sleep.assert_not_called()


def test_followup_query_timeout_warns_without_losing_pending_order(pending_cancel, mocker, caplog):
    bot, trade, obj, _order, cancel, _clock, sleep = pending_cancel
    mocker.patch(f"{EXMS}.fetch_order", side_effect=TemporaryError("query timed out"))
    caplog.clear()
    bot.manage_open_orders()
    assert obj.ft_is_open
    assert (trade.pair, obj.order_id) in bot._pending_entry_cancels
    assert cancel.call_count == 1
    assert any(
        r.levelno == logging.WARNING and "status=query_failed" in r.message for r in caplog.records
    )
    sleep.assert_not_called()


def test_unknown_cancel_status_warns_immediately(mocker, default_conf_usdt, caplog):
    bot = get_patched_freqtradebot(mocker, default_conf_usdt)
    bot._report_entry_cancel_pending("ETH/USDT", "unknown-status", {})
    assert any(r.levelno == logging.WARNING and "status=None" in r.message for r in caplog.records)


def test_exhausted_replacement_confirmation_keeps_warning(pending_cancel, mocker, caplog):
    bot, trade, obj, order, _cancel, _clock, _sleep = pending_cancel
    bot._pending_entry_cancels.clear()
    mocker.patch(f"{EXMS}.fetch_order", return_value=order)
    caplog.clear()
    assert not bot.handle_cancel_enter(trade, order, obj, CANCEL_REASON["REPLACE"], replacing=True)
    assert any(
        r.levelno == logging.WARNING and "confirmation pending" in r.message for r in caplog.records
    )
