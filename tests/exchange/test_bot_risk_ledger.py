from unittest.mock import Mock

import pytest

from freqtrade.exceptions import InvalidOrderException, OperationalException
from freqtrade.exchange.bot_risk_ledger import EntryLedger
from tests.test_bot_risk import risk


def submit(ledger, identifier, snapshot, send=None, pair="BTC/USDT:USDT", amount=100, initial=True):
    send = send or (
        lambda: {"id": identifier, "clientOrderId": identifier, "symbol": pair, "side": "buy"}
    )
    return ledger.submit(
        pair=pair,
        side="buy",
        notional=amount,
        initial=initial,
        client_id=identifier,
        snapshot=snapshot,
        send=send,
        now=0,
    )


def flat():
    return {"BTC/USDT:USDT": 0, "ETH/USDT:USDT": 0}, set()


def test_write_ahead_unknown_survives_restart(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = EntryLedger(path, risk())

    def timeout():
        assert path.exists()
        assert ledger.load()["entries"]["one"]["outcome"] == "unknown"
        raise TimeoutError("no confirmed response")

    with pytest.raises(TimeoutError):
        submit(ledger, "one", flat, timeout)
    restarted = EntryLedger(path, risk(500))
    send = Mock()
    with pytest.raises(OperationalException, match="Unknown submission"):
        submit(restarted, "two", flat, send, amount=20)
    send.assert_not_called()


def test_pending_partial_fill_and_slots_are_reserved(tmp_path):
    policy = risk()
    policy["max_occupied_symbols"] = 1
    ledger = EntryLedger(tmp_path / "ledger.json", policy)
    submit(ledger, "one", flat)
    with pytest.raises(InvalidOrderException, match="occupied-symbol"):
        submit(ledger, "two", flat, pair="ETH/USDT:USDT")

    def partial():
        return ({"BTC/USDT:USDT": 50, "ETH/USDT:USDT": 0}, {"BTC/USDT:USDT"})

    with pytest.raises(OperationalException, match="pending"):
        submit(ledger, "add", partial, initial=False)

    def held():
        return ({"BTC/USDT:USDT": 50, "ETH/USDT:USDT": 0}, set())

    with pytest.raises(OperationalException, match="terminal fill"):
        submit(ledger, "add", held, initial=False)


def test_exact_terminal_flat_twice_releases_and_unknown_does_not(tmp_path):
    ledger = EntryLedger(tmp_path / "ledger.json", risk())
    submit(ledger, "one", flat)
    assert ledger.reconcile_flat(lambda *args: None, flat, now=100) == 1

    def query(client, pair):
        return {
            "id": client,
            "clientOrderId": client,
            "symbol": pair,
            "side": "buy",
            "status": "canceled",
            "filled": 0,
        }

    assert ledger.reconcile_flat(query, flat, now=100) == 1
    assert ledger.reconcile_flat(query, flat, now=159) == 1
    assert ledger.reconcile_flat(query, flat, now=160) == 0


def test_lower_cap_and_restart_cannot_erase_previous_commitment(tmp_path):
    path = tmp_path / "ledger.json"
    submit(EntryLedger(path, risk()), "one", flat)
    policy = risk(500)
    policy["max_total_notional_usdt"] = 100
    restarted = EntryLedger(path, policy)
    with pytest.raises(InvalidOrderException, match="limit exceeded"):
        submit(restarted, "two", flat, pair="ETH/USDT:USDT", amount=1)


def test_one_confirmed_same_direction_add_and_third_entry_rejected(tmp_path):
    ledger = EntryLedger(tmp_path / "ledger.json", risk())
    submit(ledger, "one", flat)

    def held():
        return {"BTC/USDT:USDT": 100, "ETH/USDT:USDT": 0}, set()

    def query(client_id, pair):
        return {
            "id": client_id,
            "clientOrderId": client_id,
            "symbol": pair,
            "side": "buy",
            "status": "closed",
            "filled": 1,
        }

    ledger.reconcile_flat(query, held, now=100)
    submit(ledger, "add", held, initial=False)
    with pytest.raises(OperationalException, match="add count"):
        submit(ledger, "third", held, initial=False, amount=1)


def test_adapter_exits_bypass_entry_budget_and_unknown_ledger(mocker):
    from freqtrade.exchange import Exchange
    from freqtrade.exchange.umx import UMX

    exchange = object.__new__(UMX)
    exchange._exchange_ws = None
    exchange._config = {"bot_risk": risk(), "dry_run": False}
    original = mocker.patch.object(Exchange, "create_order", return_value={"id": "exit"})
    ledger = mocker.patch.object(
        exchange,
        "_bot_risk_ledger",
        side_effect=AssertionError("must not load entry ledger for exits"),
    )
    assert (
        exchange.create_order(
            pair="BTC/USDT:USDT",
            ordertype="market",
            side="sell",
            amount=1,
            rate=100,
            leverage=1,
            reduceOnly=True,
        )["id"]
        == "exit"
    )
    ledger.assert_not_called()
    original.assert_called_once()


def test_exact_terminal_response_can_resolve_unknown_but_wrong_order_id_cannot(tmp_path):
    ledger = EntryLedger(tmp_path / "ledger.json", risk())
    with pytest.raises(TimeoutError):
        submit(ledger, "one", flat, Mock(side_effect=TimeoutError()))

    def held():
        return {"BTC/USDT:USDT": 100, "ETH/USDT:USDT": 0}, set()

    result = {
        "id": "venue-one",
        "clientOrderId": "one",
        "symbol": "BTC/USDT:USDT",
        "side": "buy",
        "status": "closed",
        "filled": 1,
    }
    ledger.reconcile_flat(lambda *args: result, held, now=100)
    assert ledger.load()["entries"]["one"]["outcome"] == "confirmed"
    assert ledger.load()["entries"]["one"]["order_id"] == "venue-one"
    ledger.reconcile_flat(lambda *args: {**result, "id": "wrong-order"}, flat, now=200)
    ledger.reconcile_flat(lambda *args: {**result, "id": "wrong-order"}, flat, now=300)
    assert len(ledger.load()["entries"]) == 1


def test_adapter_reserves_venue_rounded_notional(mocker):
    from freqtrade.exchange import Exchange
    from freqtrade.exchange.umx import UMX

    exchange = object.__new__(UMX)
    exchange._exchange_ws = None
    exchange._config = {"bot_risk": risk(), "dry_run": False}
    mocker.patch.object(Exchange, "create_order", return_value={"id": "rounded"})
    ledger = Mock()
    ledger.submit.return_value = {"id": "rounded"}
    mocker.patch.object(exchange, "_bot_risk_ledger", return_value=ledger)
    mocker.patch.object(exchange, "_amount_to_contracts", return_value=12.8)
    mocker.patch.object(exchange, "amount_to_precision", return_value=12)
    mocker.patch.object(exchange, "_contracts_to_amount", return_value=1.2)
    mocker.patch.object(exchange, "price_to_precision", return_value=83.4)
    exchange.create_order(
        pair="BTC/USDT:USDT",
        ordertype="limit",
        side="buy",
        amount=1.28,
        rate=83.38,
        leverage=1,
        time_in_force="PO",
    )
    assert ledger.submit.call_args.kwargs["notional"] == pytest.approx(100.08)
