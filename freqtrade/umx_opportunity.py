"""Fail-closed UMX opportunity batches. This is deliberately not a generic sizing callback."""

import hashlib
import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from freqtrade.exceptions import InsufficientFundsError, InvalidOrderException, OperationalException
from freqtrade.persistence import Order, PairLocks, Trade
from freqtrade.umx_opportunity_allocation import allocate


logger = logging.getLogger(__name__)
STRATEGY = "UMXChanB1S1OpportunityMaker"


def number(value) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("Nonfinite opportunity accounting value")
    return result


def enabled(strategy, config) -> bool:
    markers = (
        config.get("umx_opportunity_mode") is True,
        strategy.get_strategy_name() == STRATEGY,
        getattr(strategy, "umx_opportunity_contract_version", None) == 1,
    )
    if any(markers) and not all(markers):
        raise OperationalException("UMX opportunity contract markers disagree")
    return all(markers)


@dataclass(frozen=True)
class EntryIntent:
    id: str
    batch_id: str
    pair: str
    side: str
    entry_tag: str
    signal_key: str
    expires_at: str
    rate: str
    amount: str


class Journal:
    """A committed whole-batch reservation and write-ahead submission journal."""

    def __init__(self, path: Path, binding: dict):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(
                "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY,value TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS batches "
                "(id TEXT PRIMARY KEY,created_at TEXT NOT NULL,payload TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS intents (id TEXT PRIMARY KEY,batch_id TEXT NOT NULL,"
                "signal_key TEXT UNIQUE NOT NULL,spec TEXT NOT NULL,"
                "state TEXT NOT NULL,response TEXT);"
                "CREATE TABLE IF NOT EXISTS funding "
                "(id TEXT PRIMARY KEY,pair TEXT NOT NULL,"
                "timestamp INTEGER NOT NULL,amount TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS checkpoints (key TEXT PRIMARY KEY,value TEXT NOT NULL);"
            )
            existing = dict(db.execute("SELECT key,value FROM metadata"))
            expected = {key: json.dumps(value, sort_keys=True) for key, value in binding.items()}
            if existing and existing != expected:
                raise OperationalException("UMX opportunity journal binding mismatch")
            if not existing:
                db.executemany("INSERT INTO metadata VALUES (?,?)", expected.items())

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path)
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA journal_mode=DELETE")
            with db:
                yield db
        finally:
            db.close()

    def funding_total(self, trades, exchange, *, dry_run):
        if dry_run or not trades:
            return Decimal(0)
        now = datetime.now(UTC)
        earliest = min(t.open_date_utc for t in trades)
        retained = now - timedelta(days=89)
        with self.connect() as db:
            cursor = db.execute("SELECT value FROM checkpoints WHERE key='funding'").fetchone()
        if earliest < retained and (not cursor or datetime.fromisoformat(cursor[0]) < retained):
            raise ValueError("Funding audit gap exceeds venue history retention")
        since = max(earliest, retained)
        rows = exchange.opportunity_funding_history(int(since.timestamp() * 1000), now)
        with self.connect() as db:
            for row in rows:
                timestamp = int(row["timestamp"])
                at = datetime.fromtimestamp(timestamp / 1000, UTC)
                owners = [
                    t
                    for t in trades
                    if t.pair == row["symbol"]
                    and t.open_date_utc <= at
                    and (t.is_open or at <= t.close_date_utc)
                ]
                if not row.get("id") or row.get("code") != "USDT" or len(owners) != 1:
                    raise ValueError("Funding bill identity/currency/strategy ownership mismatch")
                value = (row["symbol"], timestamp, str(number(row["amount"])))
                old = db.execute(
                    "SELECT pair,timestamp,amount FROM funding WHERE id=?", (row["id"],)
                ).fetchone()
                if old is not None and old != value:
                    raise ValueError("Previously recorded funding bill changed")
                db.execute("INSERT OR IGNORE INTO funding VALUES (?,?,?,?)", (row["id"], *value))
            db.execute(
                "INSERT OR REPLACE INTO checkpoints VALUES ('funding',?)", (now.isoformat(),)
            )
            return sum((number(r[0]) for r in db.execute("SELECT amount FROM funding")), Decimal(0))

    def rows(self):
        with self.connect() as db:
            return [
                (EntryIntent(**json.loads(spec)), state, json.loads(response) if response else None)
                for spec, state, response in db.execute(
                    "SELECT spec,state,response FROM intents ORDER BY rowid"
                )
            ]

    def claimed(self, signal_key):
        with self.connect() as db:
            return (
                db.execute("SELECT 1 FROM intents WHERE signal_key=?", (signal_key,)).fetchone()
                is not None
            )

    def has_batch(self, batch_id):
        with self.connect() as db:
            return (
                db.execute("SELECT 1 FROM batches WHERE id=?", (batch_id,)).fetchone() is not None
            )

    def freeze(self, batch_id, now, intents, evidence):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "INSERT INTO batches VALUES (?,?,?)",
                (batch_id, now.isoformat(), json.dumps(evidence, sort_keys=True, default=str)),
            )
            db.executemany(
                "INSERT INTO intents VALUES (?,?,?,?,?,NULL)",
                [(i.id, batch_id, i.signal_key, json.dumps(asdict(i)), "PLANNED") for i in intents],
            )

    def transition(self, intent, expected, state, response=None):
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE intents SET state=?,response=COALESCE(?,response) WHERE id=? AND state=?",
                (
                    state,
                    json.dumps(response) if response is not None else None,
                    intent.id,
                    expected,
                ),
            )
            if cursor.rowcount != 1:
                raise OperationalException("UMX opportunity journal transition conflict")


class OpportunityController:
    def __init__(self, bot):
        self.bot = bot
        self.active_intent = None
        self.recovering = False
        self.blocked = False
        config = bot.config
        if (
            config["exchange"]["name"] != "umx"
            or config.get("available_capital") != 1000
            or config.get("max_open_trades") != 5
            or bot.strategy.position_adjustment_enable
            or bot.strategy.order_time_in_force.get("entry") != "PO"
        ):
            raise OperationalException("UMX opportunity capital/order contract mismatch")
        path = Path(config["user_data_dir"]) / "umx_b1s1_maker_state/opportunity.sqlite"
        if not path.exists() and Trade.get_trades_proxy():
            raise OperationalException("Missing opportunity journal for a nonempty database")
        self.journal = Journal(
            path,
            {
                "version": 1,
                "strategy": STRATEGY,
                "database": config["db_url"],
                "capital": 1000,
                "universe": config["exchange"]["pair_whitelist"],
            },
        )

    def authorized(self, intent):
        return intent is not None and intent is self.active_intent

    def exact_values(self, intent):
        rate, amount = float(intent.rate), float(intent.amount)
        if (
            self.bot.exchange.amount_to_contract_precision(intent.pair, amount) != amount
            or self.bot.exchange.price_to_precision(intent.pair, rate) != rate
        ):
            raise OperationalException("Frozen opportunity quantity/price changed at transport")
        return rate, amount

    def _register(self, intent, order):
        self.active_intent = intent
        self.recovering = True
        try:
            result = self.bot.execute_entry(
                intent.pair,
                float(number(intent.amount) * number(intent.rate)),
                is_short=intent.side == "short",
                enter_tag=intent.entry_tag,
                opportunity_intent=intent,
                recovered_order=order,
            )
            self._registration_complete(intent, result, order)
        finally:
            self.active_intent = None
            self.recovering = False

    def _registration_complete(self, intent, result, response):
        saved = dict(response)
        if result:
            saved["native_registered"] = True
        self.journal.transition(intent, "ACKED", "REGISTERED" if result else "DONE", saved)

    @staticmethod
    def _owned_native_order(intent, order_id):
        existing = (
            Order.session.query(Order)
            .filter_by(order_id=str(order_id), ft_pair=intent.pair)
            .first()
        )
        if existing is not None and (
            existing.trade.strategy != STRATEGY
            or existing.trade.pair != intent.pair
            or existing.trade.trade_direction != intent.side
            or existing.ft_order_side != existing.trade.entry_side
        ):
            raise ValueError("Recovered native trade ownership mismatch")
        return existing

    def reconcile(self, *, startup=False):  # noqa: C901
        """Never resubmit uncertain requests or infer rejection from elapsed time."""
        self.blocked = False
        for intent, state, response in self.journal.rows():
            if (
                state == "DONE"
                and response
                and response.get("native_registered")
                and self._owned_native_order(intent, response["id"]) is None
            ):
                self.blocked = True
                logger.error("Opportunity history is missing; allocated capital cannot reset")
            if state in {"DONE", "SKIPPED"}:
                continue
            if state not in {"PLANNED", "SUBMITTING", "ACKED", "REGISTERED"}:
                raise OperationalException("Unknown opportunity journal state")
            if state == "PLANNED":
                # A crash before send forfeits this signal; it does not create a new plan.
                if startup or datetime.now(UTC) >= datetime.fromisoformat(intent.expires_at):
                    self.journal.transition(intent, state, "SKIPPED")
                else:
                    self.blocked = True
                continue
            try:
                if state == "SUBMITTING":
                    response = self.bot.exchange.fetch_order_by_client_id(intent.id, intent.pair)
                    self._verify_response(intent, response)
                    self.journal.transition(intent, state, "ACKED", self._safe_order(response))
                    state = "ACKED"
                if state == "ACKED":
                    existing = self._owned_native_order(intent, response["id"])
                    if existing is None:
                        self._register(intent, response)
                    else:
                        self._registration_complete(intent, True, response)
                elif state == "REGISTERED":
                    existing = self._owned_native_order(intent, response["id"])
                    if existing is None:
                        # Freqtrade removes zero-filled canceled trades. Verify before releasing.
                        order = self.bot.exchange.fetch_order(str(response["id"]), intent.pair)
                        if order.get("status") in {
                            "canceled",
                            "expired",
                            "rejected",
                        } and not number(order.get("filled", 0)):
                            self.journal.transition(intent, state, "DONE", self._safe_order(order))
                        else:
                            raise ValueError("Registered entry disappeared from trade database")
                    elif not existing.trade.is_open:
                        self.journal.transition(intent, state, "DONE")
            except Exception:
                self.blocked = True
                logger.exception("UMX opportunity recovery blocks new entries for %s", intent.id)
        return not self.blocked

    @staticmethod
    def _safe_order(order):
        keys = (
            "id",
            "clientOrderId",
            "symbol",
            "type",
            "side",
            "amount",
            "price",
            "average",
            "filled",
            "remaining",
            "status",
            "timestamp",
            "lastTradeTimestamp",
            "cost",
            "fee",
        )
        return {key: order[key] for key in keys if key in order}

    def _quantity_equal(self, pair, actual, expected):
        # CCXT uses binary floats when normalizing contracts and calculating remaining.
        # This admits representation dust, never even one hundred-millionth of a contract.
        tolerance = number(self.bot.exchange.get_contract_size(pair)) * Decimal("0.00000001")
        return abs(number(actual) - number(expected)) <= tolerance

    def _verify_response(self, intent, order):
        if (
            not order.get("id")
            or order.get("symbol") != intent.pair
            or order.get("clientOrderId") != intent.id
            or order.get("side") != ("sell" if intent.side == "short" else "buy")
            or not self._quantity_equal(intent.pair, order.get("amount"), intent.amount)
            or number(order.get("price")) != number(intent.rate)
            or order.get("status") not in {"open", "closed", "canceled", "expired", "rejected"}
            or number(order.get("filled")) < 0
            or (
                number(order.get("filled")) > number(intent.amount)
                and not self._quantity_equal(intent.pair, order.get("filled"), intent.amount)
            )
            or not self._quantity_equal(
                intent.pair,
                order.get("remaining"),
                number(intent.amount) - number(order.get("filled")),
            )
            or number(order.get("timestamp")) <= 0
        ):
            raise ValueError("UMX opportunity response identity/quantity mismatch")

    def before_submit(self, intent):
        if not self.authorized(intent) or self.recovering:
            raise OperationalException("UMX opportunity request lacks a frozen intent")
        if datetime.now(UTC) >= datetime.fromisoformat(intent.expires_at):
            self.journal.transition(intent, "PLANNED", "SKIPPED")
            return False
        self.journal.transition(intent, "PLANNED", "SUBMITTING")
        return True

    def after_response(self, intent, order):
        self._verify_response(intent, order)
        self.journal.transition(intent, "SUBMITTING", "ACKED", self._safe_order(order))

    def rejected(self, intent):
        self.journal.transition(intent, "SUBMITTING", "DONE")

    @staticmethod
    def invalidate_stale_fee(trade, order):
        previous = trade.select_order_by_order_id(str(order["id"]))
        fee = order.get("fee") or {}
        if previous is not None and number(order.get("filled", previous.safe_filled)) != number(
            previous.safe_filled
        ):
            # Older partial-fill evidence cannot stand in for a new cumulative quantity.
            if fee.get("cost") is None or fee.get("currency") != "USDT":
                previous.ft_fee_cost = None
                previous.ft_fee_currency = None
            if order.get("average") is None:
                previous.average = None
            if order.get("cost") is None:
                previous.cost = None

    @staticmethod
    def _actual_fill_price(order):
        if order.average is not None and number(order.average) > 0:
            return number(order.average)
        if order.cost is not None and number(order.cost) > 0 and number(order.safe_filled) > 0:
            return number(order.cost) / number(order.safe_filled)
        raise ValueError("Actual cumulative fill price/cost is not yet available")

    def _validate_conditionals(self, trades, remaining):
        native = {
            str(order.order_id): (trade, order)
            for trade in trades
            if trade.is_open
            for order in trade.orders
            if order.ft_is_open and order.ft_order_side == "stoploss"
        }
        attached = {
            "ftsa" + hashlib.sha256(intent.id.encode()).hexdigest()[:28]: intent
            for intent, state, _ in self.journal.rows()
            if state in {"ACKED", "REGISTERED"}
        }
        protected = set()
        for row in self.bot.exchange.opportunity_conditional_snapshot():
            info = row.get("tpslOrder") or {}
            native_record = native.get(str(row.get("complexOId")))
            trade = native_record[0] if native_record else None
            intent = attached.get(info.get("tpslClOrdId"))
            if trade is None and intent is None:
                raise ValueError("Unowned TPSL order in opportunity account")
            pair = trade.pair if trade is not None else intent.pair
            if (
                row.get("symbol") != self.bot.exchange.markets[pair]["id"]
                or info.get("slOrderType") != "market"
                or info.get("stopLossType") != "last_price"
                or number(info.get("stopLoss")) <= 0
                or number(row.get("qty", 0)) < 0
                or (
                    pair in remaining
                    and number(row.get("qty", 0)) < remaining[pair][0]
                    and not self._quantity_equal(pair, row.get("qty", 0), remaining[pair][0])
                )
            ):
                raise ValueError("Opportunity TPSL protection parameters differ")
            if intent is not None:
                direction = 1 if intent.side == "long" else -1
                distance = (
                    (number(intent.rate) - number(info["stopLoss"]))
                    * direction
                    / number(intent.rate)
                )
                if (
                    row.get("side") != ("buy" if direction == 1 else "sell")
                    or not 0 < distance <= Decimal("0.03")
                    or (
                        number(row.get("qty", 0)) > number(intent.amount)
                        and not self._quantity_equal(pair, row.get("qty", 0), intent.amount)
                    )
                ):
                    raise ValueError("Attached opportunity protection differs from its intent")
            elif trade is not None:
                expected_side = (
                    trade.entry_side
                    if str(info.get("tpslClOrdId", "")).startswith("ftsa")
                    else trade.exit_side
                )
                current = remaining.get(pair)
                if (
                    row.get("side") != expected_side
                    or current is None
                    or not self._quantity_equal(pair, row.get("qty", 0), current[0])
                    or number(info["stopLoss"]) != number(native_record[1].safe_price)
                ):
                    raise ValueError("Native opportunity protection quantity or side differs")
            protected.add(pair)
        if not set(remaining).issubset(protected):
            raise ValueError("Actual opportunity position lacks verified native protection")

    def _snapshot(self):  # noqa: C901
        bot = self.bot
        trades = Trade.get_trades_proxy()
        if any(t.strategy != STRATEGY for t in trades):
            raise ValueError("Opportunity database contains another strategy")
        bot.wallets.update()
        # Reconstruct allocated cash using actual fills and signed fees, never account equity.
        cash = Decimal(1000)
        remaining = {}
        pending = Decimal(0)
        known_open_orders = {}
        occupied = set()
        for trade in trades:
            entry_qty = entry_value = exit_qty = exit_value = Decimal(0)
            fees = Decimal(0)
            for order in trade.orders:
                if order.ft_is_open and order.ft_order_side != "stoploss":
                    known_open_orders[str(order.order_id)] = order
                qty = number(order.safe_filled)
                if qty < 0:
                    raise ValueError("Invalid actual fill quantity")
                if qty:
                    fill_price = self._actual_fill_price(order)
                    if bot.config.get("dry_run") and not order.has_fee_cost:
                        fee = (
                            trade.fee_open
                            if order.ft_order_side == trade.entry_side
                            else trade.fee_close
                        )
                        fees += qty * fill_price * number(fee)
                    else:
                        if not order.has_fee_cost or order.ft_fee_currency != "USDT":
                            raise ValueError("Actual USDT order fees not yet available")
                        fees += number(order.ft_fee_cost)
                    if order.ft_order_side == trade.entry_side:
                        entry_qty += qty
                        entry_value += qty * fill_price
                    else:
                        exit_qty += qty
                        exit_value += qty * fill_price
                if order.ft_is_open and order.ft_order_side == trade.entry_side:
                    if not 0 <= number(order.safe_remaining) <= number(order.safe_amount):
                        raise ValueError("Invalid pending entry quantity")
                    if number(order.safe_placement_price) <= 0:
                        raise ValueError("Invalid pending entry price")
                    pending += number(order.safe_remaining) * number(order.safe_placement_price)
                    occupied.add(trade.pair)
            qty_left = entry_qty - exit_qty
            if qty_left < 0 or (exit_qty and not entry_qty):
                raise ValueError("Opportunity fill ledger quantity mismatch")
            if entry_qty:
                direction = -1 if trade.is_short else 1
                cash += direction * (exit_value - exit_qty * entry_value / entry_qty)
            cash -= fees
            if qty_left:
                if trade.pair in remaining or not trade.is_open:
                    raise ValueError("Opportunity position ownership mismatch")
                remaining[trade.pair] = (qty_left, trade.trade_direction, entry_value / entry_qty)
                occupied.add(trade.pair)
        cash += self.journal.funding_total(trades, bot.exchange, dry_run=bot.config.get("dry_run"))
        if bot.config.get("dry_run"):
            positions = []
            for pair, (qty, side, entry) in remaining.items():
                mark = number(
                    bot.exchange.get_rate(pair, side="exit", is_short=side == "short", refresh=True)
                )
                positions.append(
                    {
                        "symbol": pair,
                        "contracts": qty / number(bot.exchange.get_contract_size(pair)),
                        "side": side,
                        "markPrice": mark,
                        "unrealizedPnl": qty * (mark - entry) * (-1 if side == "short" else 1),
                    }
                )
        else:
            positions, open_orders = bot.exchange.opportunity_account_snapshot()
            if {str(o["id"]) for o in open_orders} != set(known_open_orders):
                raise ValueError("Exchange ordinary orders differ from opportunity database")
            for actual in open_orders:
                local = known_open_orders[str(actual["id"])]
                fee = actual.get("fee") or {}
                if (
                    actual["symbol"] != local.ft_pair
                    or actual["side"] != local.side
                    or not self._quantity_equal(local.ft_pair, actual["amount"], local.safe_amount)
                    or not self._quantity_equal(local.ft_pair, actual["filled"], local.safe_filled)
                    or not self._quantity_equal(
                        local.ft_pair, actual["remaining"], local.safe_remaining
                    )
                    or number(actual["price"]) != number(local.safe_placement_price)
                    or (
                        local.safe_filled
                        and (
                            fee.get("currency") != "USDT"
                            or fee.get("cost") is None
                            or number(fee["cost"]) != number(local.ft_fee_cost)
                        )
                    )
                ):
                    raise ValueError(
                        "Exchange pending order differs from reconciled local quantity"
                    )
        marked = upl = Decimal(0)
        seen = set()
        for position in positions:
            qty = number(position.get("contracts", 0)) * number(
                bot.exchange.get_contract_size(position["symbol"])
            )
            if qty < 0:
                raise ValueError("Negative absolute exchange position quantity")
            if not qty:
                continue
            pair = position["symbol"]
            expected = remaining.get(pair)
            tolerance = number(bot.exchange.get_contract_size(pair)) * Decimal("0.000001")
            if (
                pair in seen
                or expected is None
                or expected[1] != position["side"]
                or abs(expected[0] - qty) > tolerance
            ):
                raise ValueError("Exchange position differs from actual opportunity fills")
            seen.add(pair)
            mark = number(position["markPrice"])
            if mark <= 0:
                raise ValueError("Invalid position mark price")
            marked += qty * mark
            upl += number(position["unrealizedPnl"])
        if seen != set(remaining):
            raise ValueError("An opportunity position is absent from the exchange")
        if not bot.config.get("dry_run"):
            self._validate_conditionals(trades, remaining)
        equity = cash + upl
        budget = max(Decimal(0), equity * Decimal("0.95") - marked - pending)
        # A wallet is a physical availability ceiling, never the strategy's capital base.
        budget = min(budget, max(Decimal(0), number(bot.wallets.get_free("USDT"))))
        minute = datetime.now(UTC).replace(second=0, microsecond=0)
        exited = {t.pair for t in trades if not t.is_open and t.close_date_utc >= minute}
        return {
            "equity": equity,
            "marked": marked,
            "pending": pending,
            "budget": budget,
            "occupied": occupied,
            "exited": exited,
        }

    def run(self, free_slots):  # noqa: C901
        try:
            if not self.reconcile() or PairLocks.is_global_lock(side="*"):
                return 0
            now = datetime.now(UTC)
            batch_id = now.replace(second=0, microsecond=0).isoformat()
            if self.journal.has_batch(batch_id):
                return 0
            snapshot = self._snapshot()
            candidates = []
            for pair in self.bot.config["exchange"]["pair_whitelist"]:
                if pair not in self.bot.active_pair_whitelist or pair in (
                    snapshot["occupied"] | snapshot["exited"]
                ):
                    continue
                candidate = self.bot.strategy.get_opportunity_candidate(pair, current_time=now)
                if candidate is None:
                    continue
                candidate = dict(candidate, pair=pair)
                expires = candidate["expires_at"]
                if (
                    not isinstance(expires, datetime)
                    or expires.utcoffset() is None
                    or not now < expires
                    or (expires - now).total_seconds() > 60
                    or candidate["side"] not in {"long", "short"}
                    or not candidate["signal_key"]
                    or candidate["entry_tag"] not in {"B1_DIV", "S1_DIV"}
                ):
                    raise ValueError("Invalid opportunity candidate")
                if self.journal.claimed(
                    candidate["signal_key"]
                ) or self.bot.strategy.is_pair_locked(pair, side=candidate["side"]):
                    continue
                market = self.bot.exchange.markets[pair]
                contract = number(market["contractSize"])
                if market["precision"]["amount"] != 0 or contract <= 0:
                    raise ValueError("Unsupported UMX opportunity contract precision")
                limits = market["limits"]
                candidate.update(rate=number(candidate["rate"]), step=contract)
                for kind in ("min", "max"):
                    amount = limits.get("amount", {}).get(kind)
                    cost = limits.get("cost", {}).get(kind)
                    candidate[f"{kind}_amount"] = (
                        number(amount) * contract if amount is not None else None
                    )
                    candidate[f"{kind}_cost"] = number(cost) if cost is not None else None
                rate = float(candidate["rate"])
                if rate <= 0 or self.bot.exchange.price_to_precision(pair, rate) != rate:
                    raise ValueError("Opportunity price is not exactly representable")
                candidates.append(candidate)
            plans = allocate(
                candidates, snapshot["budget"], min(free_slots, 5 - len(snapshot["occupied"]))
            )
            if not plans:
                # Candle delivery may lag the minute boundary. No submission plan exists yet;
                # allow another observation, while an actual frozen batch remains immutable.
                return 0
            intents = [
                EntryIntent(
                    id="ftop" + hashlib.sha256(p["signal_key"].encode()).hexdigest()[:28],
                    batch_id=batch_id,
                    pair=p["pair"],
                    side=p["side"],
                    entry_tag=p["entry_tag"],
                    signal_key=p["signal_key"],
                    expires_at=p["expires_at"].isoformat(),
                    rate=str(p["rate"]),
                    amount=str(p["amount"]),
                )
                for p in plans
            ]
            self.journal.freeze(batch_id, now, intents, {**snapshot, "candidates": candidates})
            count = 0
            for intent in intents:
                self.active_intent = intent
                try:
                    result = self.bot.execute_entry(
                        intent.pair,
                        float(number(intent.amount) * number(intent.rate)),
                        is_short=intent.side == "short",
                        enter_tag=intent.entry_tag,
                        opportunity_intent=intent,
                    )
                except (InvalidOrderException, InsufficientFundsError) as error:
                    if getattr(error.__cause__, "umx_definitive_rejection", False) is not True:
                        raise
                    self.rejected(intent)
                    result = False
                self.active_intent = None
                # The core commits acknowledgement before its native Trade transaction.
                records = {i.id: (state, response) for i, state, response in self.journal.rows()}
                if records[intent.id][0] == "ACKED":
                    self._registration_complete(intent, result, records[intent.id][1])
                count += bool(result)
            return count
        except Exception:
            self.blocked = True
            logger.exception("UMX opportunity batch blocked; no fallback entry or redistribution")
            return 0
        finally:
            self.active_intent = None
