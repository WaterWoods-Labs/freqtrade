"""Crash-persistent order intents and entry reservations for Binance Portfolio Margin."""

import json
import os
import re
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Literal
from uuid import uuid4

from filelock import FileLock
from filelock import Timeout as FileLockTimeout

from freqtrade.exceptions import OperationalException


PortfolioOrderKind = Literal["regular", "conditional"]
PortfolioOrderPurpose = Literal["submission", "containment", "risk_reduction"]

_CLIENT_ORDER_ID_PATTERN = re.compile(r"^ftpm-[A-Za-z0-9_-]{1,31}$")
_MAX_STATE_BYTES = 64 * 1024
_MAX_PENDING_INTENTS = 8
_MAX_ENTRY_RESERVATIONS = 5
_MAX_RESERVATION_AGE = timedelta(days=30)
_MAX_CLOCK_SKEW = timedelta(minutes=5)


@dataclass(frozen=True)
class PortfolioOrderIntent:
    """Minimum local evidence needed to reconcile one possibly submitted order."""

    client_order_id: str
    pair: str
    order_kind: PortfolioOrderKind
    purpose: PortfolioOrderPurpose = "submission"
    parent_client_order_id: str | None = None


@dataclass(frozen=True)
class PortfolioEntryReservation:
    """Conservative full-pair capacity held after a confirmed Chan entry submission.

    Deliberately do not persist the requested amount, price, exchange response, or credentials.
    ``exposure_seen`` only records whether this process family has observed a non-zero position
    for the reservation's pair. It is required before a filled entry can be released after exit.
    """

    client_order_id: str
    pair: str
    created_at: str
    exposure_seen: bool = False


class PortfolioOrderIntentStore:
    """Atomically persist order evidence without exchange responses or secrets."""

    _legacy_version = 1
    _reservation_version = 2

    def __init__(
        self,
        path: Path | None,
        allowed_pairs: set[str],
        *,
        reservations_enabled: bool = False,
    ) -> None:
        self.path = path
        self._allowed_pairs = allowed_pairs
        self._reservations_enabled = reservations_enabled
        self._intents: dict[str, PortfolioOrderIntent] = {}
        self._reservations: dict[str, PortfolioEntryReservation] = {}
        self._file_lock = FileLock(f"{path}.lock") if path is not None else None
        self.operation_lock = _PortfolioOrderIntentOperationLock(self)
        if path is not None:
            with self._locked():
                self._intents, self._reservations = self._load()

    @property
    def intents(self) -> tuple[PortfolioOrderIntent, ...]:
        if self.path is None:
            return ()
        with self._locked():
            self._intents, self._reservations = self._load()
            return tuple(self._intents.values())

    @property
    def reservations(self) -> tuple[PortfolioEntryReservation, ...]:
        if self.path is None or not self._reservations_enabled:
            return ()
        with self._locked():
            self._intents, self._reservations = self._load()
            return tuple(self._reservations.values())

    def add(self, intent: PortfolioOrderIntent) -> None:
        if self.path is None:
            return
        with self._locked():
            current_intents, current_reservations = self._load()
            if (
                intent.client_order_id in current_intents
                or intent.client_order_id in current_reservations
            ):
                raise OperationalException(
                    "Binance Portfolio Margin refused to overwrite pending local order evidence. "
                    "Trading remains stopped."
                )
            if current_intents and intent.purpose == "submission":
                raise OperationalException(
                    "Binance Portfolio Margin found an order intent from another operation. "
                    "No new order was submitted and trading remains stopped."
                )
            if intent.purpose in ("containment", "risk_reduction") and any(
                item.purpose in ("containment", "risk_reduction")
                for item in current_intents.values()
            ):
                raise OperationalException(
                    "Binance Portfolio Margin found a pending risk-lowering order intent. "
                    "No additional order was submitted and trading remains stopped."
                )
            if (
                intent.purpose in ("containment", "risk_reduction")
                and sum(item.purpose == "submission" for item in current_intents.values()) > 1
            ):
                raise OperationalException(
                    "Binance Portfolio Margin found ambiguous pending submission intents. "
                    "No risk-lowering order was submitted and trading remains stopped."
                )
            new_intents = {**current_intents, intent.client_order_id: intent}
            self._validate_or_raise(new_intents, current_reservations)
            self._atomic_write(new_intents, current_reservations)
            self._intents = new_intents
            self._reservations = current_reservations

    def remove(self, client_order_id: str) -> None:
        if self.path is None:
            return
        with self._locked():
            current_intents, current_reservations = self._load()
            if client_order_id not in current_intents:
                raise OperationalException(
                    "Binance Portfolio Margin could not clear an absent order intent. "
                    "Trading remains stopped."
                )
            new_intents = {
                key: intent for key, intent in current_intents.items() if key != client_order_id
            }
            self._validate_or_raise(new_intents, current_reservations)
            self._atomic_write(new_intents, current_reservations)
            self._intents = new_intents
            self._reservations = current_reservations

    def promote_to_entry_reservation(self, client_order_id: str) -> None:
        """Atomically replace a confirmed Chan entry intent with a full-pair reservation."""
        if self.path is None or not self._reservations_enabled:
            raise OperationalException(
                "Binance Portfolio Margin Chan entry reservations are unavailable. "
                "Trading remains stopped."
            )
        with self._locked():
            current_intents, current_reservations = self._load()
            intent = current_intents.get(client_order_id)
            if (
                intent is None
                or intent.order_kind != "regular"
                or intent.purpose != "submission"
                or intent.parent_client_order_id is not None
            ):
                raise OperationalException(
                    "Binance Portfolio Margin could not promote the exact confirmed entry "
                    "intent to a reservation. Trading remains stopped."
                )
            if any(item.pair == intent.pair for item in current_reservations.values()):
                raise OperationalException(
                    "Binance Portfolio Margin found an existing entry reservation for this "
                    "pair. Trading remains stopped."
                )
            new_intents = {
                key: item for key, item in current_intents.items() if key != client_order_id
            }
            reservation = PortfolioEntryReservation(
                client_order_id=client_order_id,
                pair=intent.pair,
                created_at=datetime.now(UTC).isoformat(),
            )
            new_reservations = {
                **current_reservations,
                client_order_id: reservation,
            }
            self._validate_or_raise(new_intents, new_reservations)
            self._atomic_write(new_intents, new_reservations)
            self._intents = new_intents
            self._reservations = new_reservations

    def mark_reservation_exposure_seen(self, client_order_id: str) -> None:
        if self.path is None or not self._reservations_enabled:
            raise OperationalException(
                "Binance Portfolio Margin Chan entry reservations are unavailable. "
                "Trading remains stopped."
            )
        with self._locked():
            current_intents, current_reservations = self._load()
            reservation = current_reservations.get(client_order_id)
            if reservation is None:
                raise OperationalException(
                    "Binance Portfolio Margin could not update an absent entry reservation. "
                    "Trading remains stopped."
                )
            if reservation.exposure_seen:
                self._intents = current_intents
                self._reservations = current_reservations
                return
            new_reservations = {
                **current_reservations,
                client_order_id: PortfolioEntryReservation(
                    client_order_id=reservation.client_order_id,
                    pair=reservation.pair,
                    created_at=reservation.created_at,
                    exposure_seen=True,
                ),
            }
            self._validate_or_raise(current_intents, new_reservations)
            self._atomic_write(current_intents, new_reservations)
            self._intents = current_intents
            self._reservations = new_reservations

    def remove_reservation(self, client_order_id: str) -> None:
        if self.path is None or not self._reservations_enabled:
            raise OperationalException(
                "Binance Portfolio Margin Chan entry reservations are unavailable. "
                "Trading remains stopped."
            )
        with self._locked():
            current_intents, current_reservations = self._load()
            if client_order_id not in current_reservations:
                raise OperationalException(
                    "Binance Portfolio Margin could not clear an absent entry reservation. "
                    "Trading remains stopped."
                )
            new_reservations = {
                key: item for key, item in current_reservations.items() if key != client_order_id
            }
            self._validate_or_raise(current_intents, new_reservations)
            self._atomic_write(current_intents, new_reservations)
            self._intents = current_intents
            self._reservations = new_reservations

    @contextmanager
    def _locked(self):
        file_lock = self._file_lock
        if file_lock is None:
            yield
            return
        lock_path = Path(file_lock.lock_file)
        if lock_path.is_symlink():
            raise OperationalException(
                "Binance Portfolio Margin refused an unsafe order-intent lock path. "
                "Trading remains stopped."
            )
        try:
            with file_lock.acquire(timeout=0):
                try:
                    lock_path.chmod(0o600)
                except OSError as exc:
                    raise OperationalException(
                        "Binance Portfolio Margin could not secure its order-intent lock. "
                        "Trading remains stopped."
                    ) from exc
                yield
        except FileLockTimeout as exc:
            raise OperationalException(
                "Another process is updating Binance Portfolio Margin order intents or entry "
                "reservations. No order was submitted and trading remains stopped."
            ) from exc

    def _load(
        self,
    ) -> tuple[
        dict[str, PortfolioOrderIntent],
        dict[str, PortfolioEntryReservation],
    ]:
        path = self.path
        if path is None or not path.exists():
            return {}, {}
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_STATE_BYTES:
                raise ValueError("invalid state file")
            with path.open(encoding="utf-8") as file:
                payload = json.load(file)
            expected_keys = (
                {"version", "intents", "reservations"}
                if self._reservations_enabled
                else {"version", "intents"}
            )
            expected_version = (
                self._reservation_version if self._reservations_enabled else self._legacy_version
            )
            if not isinstance(payload, dict) or set(payload) != expected_keys:
                raise ValueError("invalid state object")
            if payload["version"] != expected_version or not isinstance(payload["intents"], list):
                raise ValueError("unsupported state schema")

            intent_fields = {
                "client_order_id",
                "pair",
                "order_kind",
                "purpose",
                "parent_client_order_id",
            }
            intents = []
            for item in payload["intents"]:
                if not isinstance(item, dict) or set(item) != intent_fields:
                    raise ValueError("invalid intent object")
                intents.append(PortfolioOrderIntent(**item))

            reservations = []
            if self._reservations_enabled:
                if not isinstance(payload["reservations"], list):
                    raise ValueError("invalid reservations object")
                reservation_fields = {
                    "client_order_id",
                    "pair",
                    "created_at",
                    "exposure_seen",
                }
                for item in payload["reservations"]:
                    if not isinstance(item, dict) or set(item) != reservation_fields:
                        raise ValueError("invalid reservation object")
                    reservations.append(PortfolioEntryReservation(**item))

            intent_map = {intent.client_order_id: intent for intent in intents}
            reservation_map = {
                reservation.client_order_id: reservation for reservation in reservations
            }
            self._validate_state(tuple(intents), tuple(reservations))
            return intent_map, reservation_map
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise OperationalException(
                "Binance Portfolio Margin could not safely load its pending order intents or "
                "entry reservations. Trading remains stopped; inspect the local runtime state "
                "before restarting."
            ) from exc

    def _validate_or_raise(
        self,
        intents: dict[str, PortfolioOrderIntent],
        reservations: dict[str, PortfolioEntryReservation],
    ) -> None:
        try:
            self._validate_state(tuple(intents.values()), tuple(reservations.values()))
        except ValueError as exc:
            raise OperationalException(
                "Binance Portfolio Margin rejected unsafe local order evidence. "
                "Trading remains stopped."
            ) from exc

    def _validate_state(  # noqa: C901
        self,
        intents: tuple[PortfolioOrderIntent, ...],
        reservations: tuple[PortfolioEntryReservation, ...],
    ) -> None:
        if len(intents) > _MAX_PENDING_INTENTS:
            raise ValueError("too many pending intents")
        client_ids = {intent.client_order_id for intent in intents}
        if len(client_ids) != len(intents):
            raise ValueError("duplicate client order id")
        conditional_count = 0
        submission_count = 0
        risk_lowering_count = 0
        for intent in intents:
            if not _CLIENT_ORDER_ID_PATTERN.fullmatch(intent.client_order_id):
                raise ValueError("invalid client order id")
            if intent.pair not in self._allowed_pairs:
                raise ValueError("intent pair is not configured")
            if intent.order_kind not in ("regular", "conditional"):
                raise ValueError("invalid order kind")
            if intent.purpose not in ("submission", "containment", "risk_reduction"):
                raise ValueError("invalid intent purpose")
            if intent.purpose == "containment":
                risk_lowering_count += 1
                if (
                    intent.order_kind != "regular"
                    or intent.parent_client_order_id not in client_ids
                    or intent.parent_client_order_id == intent.client_order_id
                ):
                    raise ValueError("orphaned containment intent")
                parent = next(
                    item
                    for item in intents
                    if item.client_order_id == intent.parent_client_order_id
                )
                if parent.pair != intent.pair or parent.purpose != "submission":
                    raise ValueError("invalid containment parent")
            elif intent.purpose == "risk_reduction":
                risk_lowering_count += 1
                if intent.order_kind != "regular" or intent.parent_client_order_id is not None:
                    raise ValueError("invalid risk-reduction intent")
            elif intent.parent_client_order_id is not None:
                raise ValueError("unexpected parent client order id")
            else:
                submission_count += 1
            if intent.order_kind == "conditional":
                conditional_count += 1
        if conditional_count > 1:
            raise ValueError("multiple pending conditional intents")
        has_risk_reduction = any(intent.purpose == "risk_reduction" for intent in intents)
        if has_risk_reduction and submission_count > 1:
            raise ValueError("risk reduction has multiple pending submission intents")
        if has_risk_reduction and risk_lowering_count > 1:
            raise ValueError("multiple pending risk-lowering intents")

        if reservations and not self._reservations_enabled:
            raise ValueError("reservations disabled")
        if len(reservations) > _MAX_ENTRY_RESERVATIONS:
            raise ValueError("too many entry reservations")
        reservation_ids = {reservation.client_order_id for reservation in reservations}
        if len(reservation_ids) != len(reservations) or client_ids & reservation_ids:
            raise ValueError("duplicate local order evidence")
        reservation_pairs = {reservation.pair for reservation in reservations}
        if len(reservation_pairs) != len(reservations):
            raise ValueError("multiple reservations for one pair")
        now = datetime.now(UTC)
        for reservation in reservations:
            if not _CLIENT_ORDER_ID_PATTERN.fullmatch(reservation.client_order_id):
                raise ValueError("invalid reservation client order id")
            if reservation.pair not in self._allowed_pairs:
                raise ValueError("reservation pair is not configured")
            if type(reservation.exposure_seen) is not bool:
                raise ValueError("invalid reservation exposure evidence")
            created_at = datetime.fromisoformat(reservation.created_at)
            if created_at.tzinfo is None or created_at.utcoffset() != timedelta(0):
                raise ValueError("reservation time is not UTC")
            if created_at > now + _MAX_CLOCK_SKEW or now - created_at > _MAX_RESERVATION_AGE:
                raise ValueError("expired reservation")

    def _atomic_write(
        self,
        intents: dict[str, PortfolioOrderIntent],
        reservations: dict[str, PortfolioEntryReservation],
    ) -> None:
        path = self.path
        if path is None:
            return
        payload = {
            "version": (
                self._reservation_version if self._reservations_enabled else self._legacy_version
            ),
            "intents": [asdict(intent) for intent in intents.values()],
        }
        if self._reservations_enabled:
            payload["reservations"] = [asdict(reservation) for reservation in reservations.values()]
        encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
        temporary_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        file_descriptor: int | None = None
        try:
            file_descriptor = os.open(
                temporary_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            with os.fdopen(file_descriptor, "wb") as file:
                file_descriptor = None
                file.write(encoded)
                file.flush()
                os.fsync(file.fileno())
            temporary_path.replace(path)
            self._sync_parent_directory(path.parent)
        except OSError as exc:
            if file_descriptor is not None:
                os.close(file_descriptor)
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise OperationalException(
                "Binance Portfolio Margin could not atomically persist pending local order "
                "evidence. No order may be submitted and trading remains stopped."
            ) from exc

    @staticmethod
    def _sync_parent_directory(directory: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class _PortfolioOrderIntentOperationLock:
    """Serialize order submission in-process and across processes sharing state."""

    def __init__(self, store: PortfolioOrderIntentStore) -> None:
        self._store = store
        self._thread_lock = Lock()
        self._file_context = None

    def __enter__(self):
        self._thread_lock.acquire()
        try:
            self._file_context = self._store._locked()
            self._file_context.__enter__()
        except Exception:
            self._thread_lock.release()
            raise
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if self._file_context is not None:
                return self._file_context.__exit__(exc_type, exc_value, traceback)
            return False
        finally:
            self._file_context = None
            self._thread_lock.release()
