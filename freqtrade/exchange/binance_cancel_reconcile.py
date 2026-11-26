"""Reconcile one owned cancellation using exchange GETs and a single local ledger transaction."""

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from time import sleep

import ccxt
from sqlalchemy import select
from sqlalchemy.orm import Session

from freqtrade.exceptions import DDosProtection, FreqtradeException
from freqtrade.exchange.binance_cancel_evidence import (
    CancelReconcileConflict,
    CancelReconcilePending,
    Execution,
    Venue,
    boolean,
    number,
    require,
)
from freqtrade.exchange.exchange_utils import ROUND_DOWN, ROUND_UP, price_to_precision
from freqtrade.persistence import Order, Trade
from freqtrade.persistence.custom_data import CustomDataWrapper, _CustomData
from freqtrade.persistence.key_value_store import ValueTypesEnum, _KeyValueStoreModel
from freqtrade.util import dt_now


def _custom(session, trade, key):
    return session.scalar(
        select(_CustomData).where(_CustomData.ft_trade_id == trade.id, _CustomData.cd_key == key)
    )


def _set_custom(session, trade, key, value):
    row = _custom(session, trade, key)
    if row is None:
        row = _CustomData(ft_trade_id=trade.id, cd_key=key)
        session.add(row)
    row.cd_value = json.dumps(value)
    row.cd_type = type(value).__name__
    row.updated_at = dt_now()


def _local(session, pair, oid):
    return session.scalars(
        select(Order).where(Order.ft_pair == pair, Order.order_id == str(oid))
    ).one_or_none()


def _native_receipt(venue, trade, order, client=None):
    if order.ft_order_side == "stoploss":
        return venue.stop(trade, order)
    require(
        order.ft_order_side in (trade.entry_side, trade.exit_side), "Unexpected local order role."
    )
    raw = venue.regular(trade.pair, order.order_id, client)
    return venue.execution(
        trade.pair, raw, order.ft_order_side, order.ft_order_side == trade.exit_side
    )


def _record(trade, order, execution: Execution):
    raw = execution.order
    if order is None:
        order = Order.parse_from_ccxt_object(raw, trade.pair, trade.exit_side)
        # MARKET has no limit price. The model requires a reference price, including for
        # an unfilled cancellation where Binance explicitly reports a zero order price.
        order.ft_price = raw["average"] or raw["price"] or 0.0
        trade.orders.append(order)
    else:
        require(
            order.ft_pair == trade.pair
            and order.order_id == raw["id"]
            and number(order.ft_amount) == number(raw["amount"])
            and number(order.filled or 0) <= number(raw["filled"]),
            "Local and venue order quantities disagree.",
        )
        if not order.ft_is_open:
            require(
                order.status == raw["status"] and number(order.filled) == number(raw["filled"]),
                "A settled order disagrees with its receipt.",
            )
        order.update_from_ccxt_object(raw)
    if raw["filled"]:
        order.order_filled_date = datetime.fromtimestamp(raw["lastTradeTimestamp"] / 1000, UTC)
        if order.funding_fee is None:
            order.funding_fee = trade.funding_fee_running or 0.0
            trade.funding_fee_running = 0.0
    return order


def _account_trade(session, venue, trade, receipts, reason):
    ledger_row = _custom(session, trade, "papi_fees")
    ledger = json.loads(ledger_row.cd_value) if ledger_row else {}
    require(isinstance(ledger, dict), "Stored commission records are invalid.")
    filled = [o for o in trade.orders if not o.ft_is_open and o.safe_filled > 0]
    for o in filled:
        proof = receipts.get(o.order_id)
        if proof is None:
            # Before altering a trade, establish a fee record for every filled leg.
            proof = _native_receipt(venue, trade, o)
            require(
                number(o.safe_filled) == number(proof.order["filled"])
                and abs(number(o.safe_price) * number(o.safe_filled) - proof.notional)
                <= max(Decimal("0.000001"), proof.notional * Decimal("0.000001")),
                "Existing filled leg disagrees with the venue.",
            )
        ledger[o.order_id] = {
            "filled": proof.order["filled"],
            "notional": float(proof.notional),
            "cost": float(proof.commission),
        }
    for side, field in ((trade.entry_side, "open"), (trade.exit_side, "close")):
        members = [
            ledger[o.order_id]
            for o in filled
            if o.ft_order_side == side or (field == "close" and o.ft_order_side == "stoploss")
        ]
        if members:
            fee = sum(r["cost"] for r in members)
            notional = sum(r["notional"] for r in members)
            require(notional > 0, "Executed notional is missing.")
            setattr(trade, "fee_" + field, fee / notional)
            setattr(trade, "fee_" + field + "_cost", fee)
            setattr(trade, "fee_" + field + "_currency", "USDT")
    _set_custom(session, trade, "papi_fees", ledger)
    _set_custom(session, trade, "papi_fee_pending", False)
    net = sum(
        number(o.safe_amount_after_fee) * (1 if o.ft_order_side == trade.entry_side else -1)
        for o in filled
    )
    require(net >= 0, "Exit fills exceed confirmed entry quantity.")
    if net:
        require(trade.is_open, "Closed trade has remaining exposure.")
        trade.recalc_trade_from_orders()
        return False
    if any(o.ft_is_open for o in trade.orders):
        # A separate unfilled entry can still be working on an empty trade.
        require(not filled, "Flat trade still has an active order.")
        return False
    if not filled:
        session.delete(trade)
        return True
    exits = [o for o in filled if o.ft_order_side != trade.entry_side]
    last = max(exits, key=lambda o: o.order_filled_utc)
    if last.ft_order_side == "stoploss":
        trade.exit_reason = "stoploss_on_exchange"
        trade.close_rate_requested = last.stop_price
    else:
        trade.exit_reason = reason or trade.exit_reason or "exchange_reconciliation"
    trade.close_date = last.order_filled_date
    # Rebuild the native cost basis immediately before the last exit, including recovered adds.
    # This temporary flag is restored before the transaction can be flushed.
    with session.no_autoflush:
        last.ft_is_open = True
        try:
            trade.recalc_trade_from_orders()
        finally:
            last.ft_is_open = False
    trade.close(last.safe_price, show_msg=False)
    return False


class Recovery:
    def __init__(self, bot, incident, session):
        self.exchange = bot.exchange
        self.strategy = getattr(bot, "strategy", None)
        self.chan = type(self.strategy).__name__ == "ChanB1S1_DCA4_24H_PAPI"
        orders = getattr(self.strategy, "order_types", {})
        self.working_type = {"last": "CONTRACT_PRICE", "mark": "MARK_PRICE"}.get(
            orders.get("stoploss_price_type", "last")
        )
        self.exits = {}
        self.venue = Venue(bot.exchange)
        self.incident = incident
        self.session = session
        self.store = self.exchange._portfolio_order_intent_store
        require(
            self.store is not None and self.store.path is not None,
            "Persistent account ledger is unavailable.",
        )
        descriptor = [
            str(self.store.path),
            incident.kind,
            incident.pair,
            incident.order_id,
            incident.client_id,
            incident.containment_orders,
        ]
        fingerprint = hashlib.sha256(json.dumps(descriptor).encode()).hexdigest()
        self.mark_key = "papi_cancel_" + fingerprint[:38]
        self.marker = session.scalar(
            select(_KeyValueStoreModel).where(_KeyValueStoreModel.key == self.mark_key)
        )
        self.owned_children = dict(incident.containment_orders)
        require(
            len(self.owned_children) == len(incident.containment_orders),
            "Duplicate containment order identity.",
        )
        self.target = _local(session, incident.pair, incident.order_id)
        if self.target is None and self.marker is None:
            raise CancelReconcileConflict("Cancellation has no matching local order.")
        self.deleted_order = None
        self.releases = []
        self.retain = False

    def recover(self, snapshot):
        incident = self.incident
        _positions, ordinary, stops = snapshot
        live_trades = list(self.session.scalars(select(Trade).where(Trade.is_open.is_(True))))
        if self.target is not None:
            target_trade = self.target.trade
            if target_trade not in live_trades:
                live_trades.append(target_trade)
        else:
            # A previous DB commit removed a proven zero-fill initial placeholder.
            require(
                incident.kind == "regular" and not self.owned_children,
                "Missing trade for filled cancellation.",
            )
            raw = self.venue.regular(incident.pair, incident.order_id, incident.client_id)
            require(
                number(raw.get("executedQty")) == 0
                and raw.get("status") in ("CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"),
                "Previously removed trade no longer has a zero-fill terminal receipt.",
            )
            self.deleted_order = (incident.pair, str(incident.order_id))
        for trade in live_trades:
            self.recover_trade(trade, ordinary, stops)
        self.session.flush()
        self.validate(snapshot)

    def recover_trade(self, trade, ordinary, stops):
        incident = self.incident
        sym, size = self.venue.market(trade.pair)
        touched = {}
        for order in list(trade.orders):
            primary = order is self.target
            if not primary and not order.ft_is_open:
                continue
            is_stop = order.ft_order_side == "stoploss"
            if primary:
                require(
                    is_stop == (incident.kind == "stop"),
                    "Incident order role disagrees with the database.",
                )
                if incident.kind == "partial_entry":
                    require(
                        order.ft_order_side == trade.entry_side
                        and incident.side == trade.entry_side,
                        "Partial entry side disagrees with the database.",
                    )
            if (sym, order.order_id) in (stops if is_stop else ordinary):
                if primary:
                    raise CancelReconcilePending("Affected order is still open.")
                continue
            proof = _native_receipt(
                self.venue, trade, order, incident.client_id if primary else None
            )
            if primary and incident.kind == "partial_entry":
                require(
                    proof.contracts == number(incident.observed_amount)
                    and proof.filled_contracts >= number(incident.observed_fill) > 0
                    and proof.contracts * size == number(incident.observed_base_amount)
                    and number(incident.observed_fill) * size
                    == number(incident.observed_base_fill),
                    "Terminal receipt contradicts the observed partial fill.",
                )
            _record(trade, order, proof)
            touched[order.order_id] = proof
        contained = (
            self.target is not None and self.target.trade is trade and bool(self.owned_children)
        )
        if contained:
            self.recover_containment(trade, touched)
        if touched:
            deleted = _account_trade(
                self.session,
                self.venue,
                trade,
                touched,
                "papi_partial_entry_containment" if contained else None,
            )
            if deleted and self.target is not None and self.target.trade is trade:
                self.deleted_order = (trade.pair, self.target.order_id)
            if self.chan and not deleted and not trade.is_open:
                # order_filled normally timestamps the strategy's per-pair exit cooldown.
                self.exits[trade.pair] = trade.close_date_utc.replace(second=0, microsecond=0)

    def recover_containment(self, trade, touched):
        for cid, oid in self.owned_children.items():
            native = self.venue.regular(trade.pair, oid, cid)
            require(
                native.get("type") == "MARKET" and native.get("side") == trade.exit_side.upper(),
                "Containment order is not the owned reducing market exit.",
            )
            proof = self.venue.execution(trade.pair, native, trade.exit_side, True)
            existing = _local(self.session, trade.pair, proof.order["id"])
            require(
                existing is None or existing.trade is trade,
                "Containment exit belongs to another trade.",
            )
            _record(trade, existing, proof)
            touched[proof.order["id"]] = proof

    def validate(self, snapshot):
        pos, ordinary, stops = snapshot
        trades = list(self.session.scalars(select(Trade).where(Trade.is_open.is_(True))))
        originals = set()
        protections = set()
        seen = set()
        exposed = set()
        for t in trades:
            sym, size = self.venue.market(t.pair)
            require(
                t.exchange.lower() == "binance" and sym not in seen and number(t.leverage) == 1,
                "Duplicate or unsupported local position.",
            )
            seen.add(sym)
            signed = number(t.amount) * (-1 if t.is_short else 1)
            require(pos.get(sym, 0) * size == signed, "Position quantities or directions disagree.")
            if signed:
                exposed.add(sym)
                if self.chan:
                    self.check_strategy_anchor(t)
            for o in t.orders:
                if not o.ft_is_open:
                    continue
                key = (sym, o.order_id)
                if o.ft_order_side == "stoploss":
                    s = stops.get(key)
                    require(
                        s
                        and s["algoStatus"] == "NEW"
                        and s["orderType"] == "STOP_MARKET"
                        and s["side"] == t.exit_side.upper()
                        and boolean(s["reduceOnly"])
                        and not boolean(s["closePosition"])
                        and not boolean(s["priceProtect"])
                        and self.working_type
                        and s["workingType"] == self.working_type
                        and s["positionSide"] == "BOTH"
                        and number(s["quantity"]) * size == abs(signed) == number(o.amount)
                        and number(s["triggerPrice"]) == number(o.stop_price),
                        "Active exchange protection is missing or inconsistent.",
                    )
                    protections.add(key)
                else:
                    r = ordinary.get(key)
                    require(
                        r
                        and r["side"] == o.ft_order_side.upper()
                        and r["positionSide"] == "BOTH"
                        and r["status"] in ("NEW", "PARTIALLY_FILLED")
                        and boolean(r["reduceOnly"]) == (o.ft_order_side == t.exit_side)
                        and number(r["origQty"]) * size == number(o.ft_amount)
                        and number(r["executedQty"]) * size == number(o.filled or 0),
                        "Active regular order differs from its database row.",
                    )
                    require(
                        r["type"] == str(o.order_type).upper()
                        and r["clientOrderId"]
                        and (
                            r["type"] != "LIMIT"
                            or number(r["price"]) == number(o.price or o.ft_price)
                        ),
                        "Active order price, type or identity disagrees.",
                    )
                    originals.add(key)
        require(
            exposed == set(pos)
            and originals == set(ordinary)
            and protections == set(stops)
            and Counter(k[0] for k in protections) == Counter(exposed),
            "Account has unmatched positions, orders or stop protection.",
        )
        self.reconcile_reservations(snapshot, trades)

    def check_strategy_anchor(self, trade):
        """Resuming the installed Chan strategy requires its original, persisted fill context."""

        def value(key):
            row = _custom(self.session, trade, key)
            if row is None:
                raise CancelReconcilePending("Persisted strategy fill or stop context is missing.")
            return CustomDataWrapper._convert_custom_data(row).value

        entries = [o for o in trade.orders if o.ft_order_side == trade.entry_side and o.safe_filled]
        require(
            entries and number(value("first_price")) == number(entries[0].safe_price),
            "Strategy first-fill price disagrees with the entry ledger.",
        )
        first = datetime.fromisoformat(value("first_time"))
        due = datetime.fromisoformat(value("deadline"))
        tp = datetime.fromisoformat(value("tp_allowed"))
        rates = value("risk_rates")
        stop = number(value("chan_trailing_stop"))
        initial = number(
            price_to_precision(
                float(
                    number(entries[0].safe_price)
                    * (Decimal("1.03") if trade.is_short else Decimal("0.97"))
                ),
                trade.price_precision,
                trade.precision_mode_price,
                rounding_mode=ROUND_DOWN if trade.is_short else ROUND_UP,
            )
        )
        require(
            first == entries[0].order_filled_utc
            and first < due
            and tp >= first
            and all(number(rates.get(o.order_id)) >= number(o.safe_price) for o in entries)
            and stop > 0
            and number(trade.stop_loss) == stop
            and (stop <= initial if trade.is_short else stop >= initial),
            "Persisted strategy exposure or trailing stop disagrees with the trade.",
        )
        orders = [o for o in trade.orders if o.ft_order_side == "stoploss" and o.ft_is_open]
        require(
            len(orders) == 1 and number(orders[0].stop_price) == stop,
            "Native stop differs from the strategy anchor.",
        )

    def reconcile_reservations(self, snapshot, trades):
        incident = self.incident
        for item in self.store.intents:
            parent = (
                item.pair == incident.pair
                and item.client_order_id == incident.client_id
                and item.purpose == "submission"
                and item.order_kind == "regular"
                and incident.kind == "partial_entry"
            )
            child = (
                item.pair == incident.pair
                and item.client_order_id in self.owned_children
                and item.purpose == "containment"
                and item.parent_client_order_id == incident.client_id
            )
            require(parent or child, "Another unresolved submission is present.")
        occupied = set()
        working_entries = {
            key: row["clientOrderId"]
            for key, row in snapshot[1].items()
            if not boolean(row["reduceOnly"])
        }
        for r in self.store.reservations:
            raw = self.venue.regular(r.pair, client_id=r.client_order_id)
            oid = str(raw["orderId"])
            row = _local(self.session, r.pair, oid)
            gone = self.deleted_order == (r.pair, oid)
            require(
                (row is not None and row.ft_order_side == row.trade.entry_side) or gone,
                "Reservation has no matching local entry.",
            )
            sym, size = self.venue.market(r.pair)
            if (sym, oid) in working_entries:
                require(
                    working_entries.pop((sym, oid)) == r.client_order_id
                    and raw.get("status") == snapshot[1][sym, oid]["status"],
                    "Working entry reservation disagrees with the open order.",
                )
            filled = number(raw.get("executedQty"))
            require(
                not boolean(raw.get("reduceOnly"))
                and (
                    (gone and filled == 0)
                    or (
                        row is not None
                        and raw["side"] == row.ft_order_side.upper()
                        and filled * size == number(row.filled or 0)
                    )
                ),
                "Reservation entry evidence disagrees.",
            )
            if row is not None and row.trade.is_open and (row.ft_is_open or filled):
                occupied.add(row.trade.id)
                continue
            require(
                raw.get("status")
                in ("CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED", "FILLED")
                and (not filled or (r.exposure_seen and sym not in snapshot[0]))
                and not any(
                    k[0] == sym and not boolean(o["reduceOnly"]) for k, o in snapshot[1].items()
                ),
                "Reservation lacks stable release evidence.",
            )
            self.releases.append(r.client_order_id)
        self.retain = bool(
            incident.kind == "partial_entry"
            and self.target
            and self.target.trade.is_open
            and self.target.safe_filled
        )
        if self.retain:
            occupied.add(self.target.trade.id)
        require(
            not working_entries and {t.id for t in trades} == occupied,
            "An open trade is missing its owned entry reservation.",
        )


def reconcile_cancel(bot, incident):
    """The worker holds its trading/RPC exit lock; never invoke a trading method here."""
    exchange = bot.exchange
    try:
        with exchange._get_portfolio_create_lock():
            worker_session = Trade.session()
            require(
                not worker_session.new
                and not worker_session.deleted
                and not any(worker_session.is_modified(obj) for obj in worker_session.dirty),
                "Worker has uncommitted trade changes.",
            )
            with Session(worker_session.get_bind()) as session, session.begin():
                recovery = Recovery(bot, incident, session)
                first = recovery.venue.snapshot()
                recovery.recover(first)
                for _ in range(1, exchange._portfolio_containment_stable_snapshots):
                    sleep(exchange._portfolio_order_recovery_delay)
                    if recovery.venue.snapshot() != first:
                        raise CancelReconcilePending("Exchange account snapshots have changed.")
                    recovery.venue.confirm_receipts()
                if recovery.marker is None:
                    session.add(
                        _KeyValueStoreModel(
                            key=recovery.mark_key,
                            value_type=ValueTypesEnum.STRING,
                            string_value="accounted",
                        )
                    )
            # Native ledger cleanup only follows the durable accounting proof. If interrupted,
            # the next attempt rechecks ownership, fills, protection and the remaining reservations.
            worker_session.expire_all()
            _CustomData.session.expire_all()
            if recovery.exits:
                for pair, closed in recovery.exits.items():
                    previous = recovery.strategy.last_exit.get(pair)
                    recovery.strategy.last_exit[pair] = (
                        max(previous, closed) if previous else closed
                    )
            for cid in recovery.releases:
                recovery.store.remove_reservation(cid)
            exchange.clear_reconciled_cancel(incident, retain_entry=recovery.retain)
    except (CancelReconcilePending, CancelReconcileConflict):
        raise
    except (ccxt.DDoSProtection, ccxt.RateLimitExceeded, ccxt.AuthenticationError, DDosProtection):
        raise CancelReconcileConflict(
            "Exchange rate limit or authentication prevents verification."
        ) from None
    except (KeyError, TypeError, ValueError):
        raise CancelReconcileConflict(
            "Stored or exchange evidence has an invalid format."
        ) from None
    except (ccxt.BaseError, FreqtradeException) as exc:
        if isinstance(
            exc.__cause__,
            (ccxt.DDoSProtection, ccxt.RateLimitExceeded, ccxt.AuthenticationError, DDosProtection),
        ):
            raise CancelReconcileConflict(
                "Exchange rate limit or authentication prevents verification."
            ) from None
        raise CancelReconcilePending(
            "Exchange evidence or account ledger is unavailable."
        ) from None
