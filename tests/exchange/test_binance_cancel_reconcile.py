from contextlib import nullcontext
from datetime import timedelta
from threading import Lock
from types import SimpleNamespace
from unittest.mock import Mock

import ccxt
import pytest
from sqlalchemy import select

from freqtrade.cancel_recovery import CancelRecovery
from freqtrade.enums import State
from freqtrade.exceptions import CancelReconciliationRequired
from freqtrade.exchange.binance import Binance
from freqtrade.exchange.binance_cancel_reconcile import (
    CancelReconcileConflict,
    CancelReconcilePending,
    reconcile_cancel,
)
from freqtrade.exchange.binance_order_intent import (
    PortfolioEntryReservation,
    PortfolioOrderIntent,
    PortfolioOrderIntentStore,
)
from freqtrade.persistence import Order, Trade, init_db
from tests.conftest_trades_usdt import mock_trade_usdt_1


PAIR = "LTC/USDT:USDT"
SECOND = "BTC/USDT:USDT"
STAMP = 1700000000000


class ReadOnlyVenue:
    """Exposes only GET routes; an accidental call to a mutation fails the test."""

    def __init__(self):
        self.records = {}
        self.fills = {}
        self.positions = []
        self.orders = []
        self.stops = {}
        self.stop_history = {}
        self.assets = {PAIR: {"id": "LTCUSDT"}, SECOND: {"id": "BTCUSDT"}}
        self.bad_fee = False
        self.polls = 0
        self.drift = False

    def record(
        self, pair, oid, cid, side, qty, filled, price, reduce=False, status="FILLED", kind="LIMIT"
    ):
        symbol = self.assets[pair]["id"]
        row = {
            "orderId": oid,
            "clientOrderId": cid,
            "symbol": symbol,
            "side": side,
            "status": status,
            "positionSide": "BOTH",
            "reduceOnly": reduce,
            "time": STAMP,
            "updateTime": STAMP + 10,
            "origQty": str(qty),
            "executedQty": str(filled),
            "cumQuote": str(filled * price),
            "type": kind,
            "price": "0" if kind == "MARKET" else str(price),
        }
        self.records[oid] = row
        self.fills[oid] = (
            [
                {
                    "id": oid,
                    "orderId": oid,
                    "symbol": symbol,
                    "side": side,
                    "qty": str(filled),
                    "price": str(price),
                    "commission": "0.01",
                    "commissionAsset": "USDT",
                    "positionSide": "BOTH",
                    "time": STAMP + 10,
                }
            ]
            if filled
            else []
        )

    def papiGetUmOrder(self, params):
        if "orderId" in params:
            row = self.records[params["orderId"]]
        else:
            row = next(
                r
                for r in self.records.values()
                if r["clientOrderId"] == params["origClientOrderId"]
            )
        assert row["symbol"] == params["symbol"]
        return row

    def papiGetUmUserTrades(self, params):
        result = self.fills[params["orderId"]]
        return [{**r, "commissionAsset": "BNB"} for r in result] if self.bad_fee else result

    def papiGetUmOpenOrders(self):
        return self.orders

    def papiGetUmPositionRisk(self):
        self.polls += 1
        if self.drift and self.polls == 2:
            return [
                {"symbol": "OTHERUSDT", "positionSide": "BOTH", "positionAmt": "1", "leverage": "1"}
            ]
        return self.positions

    def algo(self, route, method, args):
        assert method == "GET"
        if route == "um/algo/openAlgoOrders":
            return list(self.stops.values())
        assert route == "um/algo/algoOrder"
        return self.stop_history[args["algoId"]]


def local_trade(pair, oid, side="sell", filled=0, qty=5):
    trade = mock_trade_usdt_1(Mock(return_value=0.001), side == "sell")
    trade.is_open = True
    trade.close_rate = trade.close_profit = trade.close_profit_abs = None
    trade.orders = []
    trade.pair = pair
    trade.exchange = "binance"
    trade.amount = float(filled)
    trade.leverage = 1
    trade.contract_size = 1
    trade.close_date = None
    trade.amount_precision = 0.001
    trade.price_precision = 0.001
    trade.precision_mode = 4
    trade.precision_mode_price = 4
    trade.fee_open = trade.fee_close = 0.0
    o = Order.parse_from_ccxt_object(
        {
            "id": oid,
            "symbol": pair,
            "status": "closed" if filled else "open",
            "side": side,
            "type": "limit",
            "price": 10.0,
            "average": 10.0 if filled else None,
            "amount": float(qty),
            "filled": float(filled),
            "remaining": float(qty - filled),
            "cost": float(filled * 10),
            "timestamp": STAMP,
            "lastTradeTimestamp": STAMP if filled else None,
        },
        pair,
        side,
    )
    trade.orders.append(o)
    Trade.session.add(trade)
    Trade.commit()
    return trade


@pytest.fixture
def incident_case(tmp_path):
    init_db("sqlite:///" + str(tmp_path / "trades.sqlite"))
    a = ReadOnlyVenue()
    a.record(PAIR, "100", "entry", "SELL", 5, 3, 10, status="CANCELED")
    a.record(PAIR, "101", "close", "BUY", 3, 3, 11, True, kind="MARKET")
    primary = local_trade(PAIR, "100")
    second = local_trade(SECOND, "200", filled=2, qty=2)
    a.record(SECOND, "200", "prior", "SELL", 2, 2, 10)
    stop = Order.parse_from_ccxt_object(
        {
            "id": "300",
            "symbol": SECOND,
            "status": "open",
            "side": "buy",
            "type": "stoploss",
            "price": 11.0,
            "stopPrice": 11.0,
            "amount": 2.0,
            "filled": 0.0,
            "timestamp": STAMP,
        },
        SECOND,
        "stoploss",
    )
    second.orders.append(stop)
    Trade.commit()
    parent = {
        "symbol": "BTCUSDT",
        "algoId": "300",
        "algoStatus": "FINISHED",
        "side": "BUY",
        "orderType": "STOP_MARKET",
        "workingType": "CONTRACT_PRICE",
        "closePosition": False,
        "priceProtect": False,
        "positionSide": "BOTH",
        "reduceOnly": True,
        "quantity": "2",
        "triggerPrice": "11",
        "actualQty": "2",
        "actualOrderId": "301",
        "createTime": STAMP,
    }
    a.stop_history["300"] = parent
    a.record(SECOND, "301", "stop-child", "BUY", 2, 2, 11, True, kind="MARKET")
    store = SimpleNamespace(
        path=tmp_path / "state.json",
        intents=[PortfolioOrderIntent("entry", PAIR, "regular")],
        reservations=[PortfolioEntryReservation("prior", SECOND, "2026-01-01", True)],
    )

    def remove(cid):
        store.reservations = [r for r in store.reservations if r.client_order_id != cid]

    store.remove_reservation = remove
    ex = SimpleNamespace(
        _api=a,
        markets=a.assets,
        get_contract_size=lambda pair: 1,
        _portfolio_algo_request=a.algo,
        _portfolio_margin_unsupported_exposure_conflicts=list,
        _portfolio_order_intent_store=store,
        _get_portfolio_create_lock=nullcontext,
        _portfolio_containment_stable_snapshots=3,
        _portfolio_order_recovery_delay=0,
        clear_reconciled_cancel=Mock(),
    )
    obj = CancelReconciliationRequired(
        "partial_entry", PAIR, "100", "entry", (("close", "101"),), "sell"
    )
    obj.observed_amount, obj.observed_fill = 5, 2
    obj.observed_base_amount, obj.observed_base_fill = 5, 2
    return SimpleNamespace(exchange=ex), obj, a, primary.id, second.id


def test_containment_and_downtime_stop_are_booked_once(incident_case):
    bot, incident, _venue, primary, secondary = incident_case
    Trade.session.get(Trade, secondary).exit_reason = "previous_unfilled_exit"
    Trade.commit()
    # A retry after the DB was committed and before the latch was cleared must be harmless.
    bot.exchange.clear_reconciled_cancel.side_effect = [RuntimeError("interrupted"), None]
    with pytest.raises(RuntimeError, match="interrupted"):
        reconcile_cancel(bot, incident)
    reconcile_cancel(bot, incident)
    t = Trade.session.get(Trade, primary)
    assert not t.is_open and t.exit_reason == "papi_partial_entry_containment"
    assert [o.order_id for o in t.orders] == ["100", "101"]
    assert t.amount == 3 and t.close_rate == 11 and t.close_profit_abs == pytest.approx(-3.02)
    assert t.fee_open_cost == t.fee_close_cost == 0.01
    other = Trade.session.get(Trade, secondary)
    assert not other.is_open and other.exit_reason == "stoploss_on_exchange"
    assert other.orders[-1].order_id == "300" and other.orders[-1].filled == 2
    assert other.close_rate_requested == 11
    assert other.close_profit_abs == pytest.approx(-2.02)
    assert not bot.exchange._portfolio_order_intent_store.reservations


@pytest.mark.parametrize(
    "defect", ["fees", "foreign_order", "moving_account", "wrong_child", "unknown_intent"]
)
def test_unproven_account_rolls_back_without_clearing(incident_case, defect):
    bot, incident, venue, primary, secondary = incident_case
    if defect == "fees":
        venue.bad_fee = True
    elif defect == "foreign_order":
        venue.orders = [{"symbol": "OTHERUSDT", "orderId": "45"}]
    elif defect == "moving_account":
        venue.drift = True
    elif defect == "wrong_child":
        venue.records["101"]["clientOrderId"] = "alien"
    else:
        bot.exchange._portfolio_order_intent_store.intents.append(
            PortfolioOrderIntent("alien", SECOND, "regular")
        )
    with pytest.raises((CancelReconcileConflict, CancelReconcilePending)):
        reconcile_cancel(bot, incident)
    assert Trade.session.get(Trade, primary).is_open and Trade.session.get(Trade, secondary).is_open
    assert Trade.session.scalar(select(Order).where(Order.order_id == "101")) is None
    assert len(bot.exchange._portfolio_order_intent_store.reservations) == 1
    bot.exchange.clear_reconciled_cancel.assert_not_called()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("observed_amount", 6),
        ("observed_fill", 4),
        ("observed_base_amount", "5.0000000000000001"),
        ("observed_base_fill", "1.9999999999999999"),
    ],
)
def test_partial_cannot_rewind_original_observation(incident_case, field, value):
    bot, incident, _, primary, _ = incident_case
    setattr(incident, field, value)
    with pytest.raises(CancelReconcileConflict, match="partial fill"):
        reconcile_cancel(bot, incident)
    assert Trade.session.get(Trade, primary).is_open


def test_no_retry_on_rate_limit_and_no_private_error_details(incident_case):
    bot, incident, venue, primary, _ = incident_case
    venue.papiGetUmPositionRisk = Mock(side_effect=ccxt.RateLimitExceeded("private text"))
    with pytest.raises(CancelReconcileConflict, match="rate limit") as error:
        reconcile_cancel(bot, incident)
    assert "private text" not in str(error.value) and error.value.safe_to_notify
    assert Trade.session.get(Trade, primary).is_open


@pytest.mark.parametrize("defect", ["late_fill", "late_fee", "stop_amount", "stop_child_type"])
def test_conflicting_exact_evidence_rolls_back_with_flat_account(incident_case, defect):
    bot, incident, venue, primary, _ = incident_case
    initial_lookup, initial_fills = venue.papiGetUmOrder, venue.papiGetUmUserTrades
    if defect == "late_fill":

        def lookup(params):
            raw = initial_lookup(params)
            return (
                {**raw, "executedQty": "4"} if venue.polls > 1 and raw["orderId"] == "100" else raw
            )

        venue.papiGetUmOrder = lookup
    elif defect == "late_fee":

        def fills(params):
            data = initial_fills(params)
            return [{**v, "commission": "0.02"} for v in data] if venue.polls > 1 else data

        venue.papiGetUmUserTrades = fills
    elif defect == "stop_amount":
        venue.stop_history["300"]["filledQty"] = "0"
    else:
        venue.records["301"]["type"] = "LIMIT"
    with pytest.raises((CancelReconcileConflict, CancelReconcilePending)):
        reconcile_cancel(bot, incident)
    assert Trade.session.get(Trade, primary).is_open
    assert bot.exchange._portfolio_order_intent_store.reservations
    bot.exchange.clear_reconciled_cancel.assert_not_called()


def test_regular_zero_fill_retry_after_db_commit(incident_case):
    bot, _, venue, primary, _ = incident_case
    store = bot.exchange._portfolio_order_intent_store
    store.intents = []
    store.reservations.append(PortfolioEntryReservation("entry", PAIR, "2026-01-01"))
    venue.record(PAIR, "100", "entry", "SELL", 5, 0, 10, status="CANCELED")
    incident = CancelReconciliationRequired("regular", PAIR, "100")
    bot.exchange.clear_reconciled_cancel.side_effect = [RuntimeError("interrupted"), None]
    with pytest.raises(RuntimeError, match="interrupted"):
        reconcile_cancel(bot, incident)
    reconcile_cancel(bot, incident)
    assert Trade.session.get(Trade, primary) is None and not store.reservations


def test_worker_incident_real_binance_and_durable_stores(incident_case, tmp_path):
    bot, _, api, primary, _ = incident_case
    store = PortfolioOrderIntentStore(
        tmp_path / "live-ledger.json", {PAIR, SECOND}, reservations_enabled=True, dca_enabled=True
    )
    for cid, pair in (("ftpm-prior", SECOND), ("ftpm-entry", PAIR)):
        store.add(PortfolioOrderIntent(cid, pair, "regular"))
        store.promote_to_entry_reservation(cid)
    store.mark_reservation_exposure_seen("ftpm-prior")
    for oid, name in (("100", "entry"), ("101", "close"), ("200", "prior")):
        api.records[oid]["clientOrderId"] = "ftpm-" + name

    exchange = object.__new__(Binance)
    exchange.close = lambda: None
    exchange._api, exchange._markets = api, api.assets
    exchange._config = {"dry_run": False}
    exchange._portfolio_margin_risk = {"policy": "bot_config_v1"}
    exchange._portfolio_order_intent_store = store
    exchange._portfolio_create_lock = store.operation_lock
    exchange._papi_cancel_recovery_enabled = True
    exchange._portfolio_unknown_order_latched = False
    exchange._active_cancel_incident = None
    exchange._portfolio_unknown_conditional_client_order_id = None
    exchange._portfolio_unknown_conditional_pair = None
    exchange._portfolio_order_recovery_delay = 0
    exchange.get_contract_size = lambda pair: 1
    exchange._portfolio_algo_request = api.algo
    exchange._portfolio_margin_unsupported_exposure_conflicts = list
    # Simulate the original inconclusive cancel and its already-submitted containment exit.
    exchange._resolve_portfolio_partial_cancel = lambda evidence: None
    exchange._contain_unknown_portfolio_order = lambda pair, parent, children: children.update(
        {"ftpm-close": "101"}
    )
    observed = {
        "id": "100",
        "clientOrderId": "ftpm-entry",
        "symbol": PAIR,
        "side": "sell",
        "amount": 5.0,
        "filled": 2.0,
        "status": "open",
        "info": {**api.records["100"], "status": "PARTIALLY_FILLED", "executedQty": "2"},
    }
    bot.exchange, bot.state = exchange, State.RUNNING
    bot._exit_lock, bot.notify_status = Lock(), Mock()
    worker = CancelRecovery()
    before = worker.snapshot(bot)
    with pytest.raises(CancelReconciliationRequired) as caught:
        exchange.settle_portfolio_dca_partial_entry(observed, PAIR)
    assert exchange._portfolio_unknown_order_latched and store.intents
    worker.handle(bot, caught.value, before)
    assert bot.state == State.RUNNING and not worker.held
    assert (
        not exchange._portfolio_unknown_order_latched
        and not store.intents
        and not store.reservations
    )
    assert not Trade.session.get(Trade, primary).is_open and not Trade.get_open_trades()
    bot.notify_status.assert_not_called()


def test_unmatched_protection_prevents_resume(incident_case):
    bot, incident, venue, _, _ = incident_case
    venue.positions = [
        {"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "-2", "leverage": "1"}
    ]
    venue.stops["300"] = {**venue.stop_history["300"], "algoStatus": "NEW", "quantity": "1.9"}
    with pytest.raises(CancelReconcileConflict, match="protection"):
        reconcile_cancel(bot, incident)
    venue.stops["300"]["quantity"] = "2"
    reconcile_cancel(bot, incident)
    assert Trade.get_open_trades()[0].pair == SECOND
    assert len(bot.exchange._portfolio_order_intent_store.reservations) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [("workingType", "MARK_PRICE"), ("closePosition", True), ("priceProtect", True)],
)
def test_stop_mode_and_trigger_source_are_part_of_stable_proof(incident_case, field, value):
    bot, incident, api, _, _ = incident_case
    api.positions = [
        {"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "-2", "leverage": "1"}
    ]
    api.stops["300"] = {**api.stop_history["300"], "algoStatus": "NEW", field: value}
    with pytest.raises(CancelReconcileConflict, match="protection"):
        reconcile_cancel(bot, incident)
    bot.exchange.clear_reconciled_cancel.assert_not_called()


def test_open_dca_and_persisted_chan_stop_context(incident_case):
    bot, incident, api, _, secondary = incident_case

    class ChanB1S1_DCA4_24H_PAPI:
        order_types = {"stoploss_price_type": "last"}

    bot.strategy = ChanB1S1_DCA4_24H_PAPI()
    bot.strategy.last_exit = {}
    trade = Trade.session.get(Trade, secondary)
    entry = trade.orders[0]
    first = entry.order_filled_utc
    trade.stop_loss = trade.orders[1].stop_price = 10.3
    api.stops["300"] = {**api.stop_history["300"], "triggerPrice": "10.3", "algoStatus": "NEW"}
    api.positions = [
        {"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "-2", "leverage": "1"}
    ]
    add = Order.parse_from_ccxt_object(
        {
            "id": "201",
            "symbol": SECOND,
            "side": "sell",
            "type": "limit",
            "price": 12,
            "amount": 1,
            "filled": 0,
            "status": "open",
            "timestamp": STAMP,
        },
        SECOND,
        "sell",
    )
    trade.orders.append(add)
    Trade.commit()
    api.record(SECOND, "201", "add", "SELL", 1, 0, 12, status="NEW")
    api.orders = [api.records["201"]]
    with pytest.raises(CancelReconcilePending, match="strategy"):
        reconcile_cancel(bot, incident)
    for key, val in {
        "first_price": 10.0,
        "first_time": first.isoformat(),
        "deadline": (first + timedelta(hours=24)).isoformat(),
        "tp_allowed": first.isoformat(),
        "risk_rates": {"200": 10.0},
        "chan_trailing_stop": 10.3,
    }.items():
        trade.set_custom_data(key, val)
    with pytest.raises(CancelReconcileConflict, match="reservation"):
        reconcile_cancel(bot, incident)
    bot.exchange._portfolio_order_intent_store.reservations.append(
        PortfolioEntryReservation("add", SECOND, "2026-01-01")
    )
    reconcile_cancel(bot, incident)
    assert Trade.get_open_trades() == [trade]
    assert trade.orders[-1].ft_is_open and trade.orders[-1].order_id == "201"
    assert PAIR in bot.strategy.last_exit and SECOND not in bot.strategy.last_exit
