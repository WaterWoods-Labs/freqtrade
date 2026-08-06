"""Crash-persistent order intents for Binance Portfolio Margin."""

import json
import os
import re
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Lock
from typing import Literal
from uuid import uuid4

from filelock import FileLock
from filelock import Timeout as FileLockTimeout

from freqtrade.exceptions import OperationalException


PortfolioOrderKind = Literal["regular", "conditional"]
PortfolioOrderPurpose = Literal["submission", "containment"]

_CLIENT_ORDER_ID_PATTERN = re.compile(r"^ftpm-[A-Za-z0-9_-]{1,31}$")
_MAX_STATE_BYTES = 64 * 1024
_MAX_PENDING_INTENTS = 8


@dataclass(frozen=True)
class PortfolioOrderIntent:
    """Minimum local evidence needed to reconcile one possibly submitted order."""

    client_order_id: str
    pair: str
    order_kind: PortfolioOrderKind
    purpose: PortfolioOrderPurpose = "submission"
    parent_client_order_id: str | None = None


class PortfolioOrderIntentStore:
    """Atomically persist outstanding order intents without exchange responses or secrets."""

    _version = 1

    def __init__(self, path: Path | None, allowed_pairs: set[str]) -> None:
        self.path = path
        self._allowed_pairs = allowed_pairs
        self._intents: dict[str, PortfolioOrderIntent] = {}
        self._file_lock = FileLock(f"{path}.lock") if path is not None else None
        self.operation_lock = _PortfolioOrderIntentOperationLock(self)
        if path is not None:
            with self._locked():
                self._intents = self._load()

    @property
    def intents(self) -> tuple[PortfolioOrderIntent, ...]:
        if self.path is None:
            return ()
        with self._locked():
            self._intents = self._load()
            return tuple(self._intents.values())

    def add(self, intent: PortfolioOrderIntent) -> None:
        if self.path is None:
            return
        with self._locked():
            current_intents = self._load()
            if intent.client_order_id in current_intents:
                raise OperationalException(
                    "Binance Portfolio Margin refused to overwrite a pending order intent. "
                    "Trading remains stopped."
                )
            if current_intents and intent.purpose == "submission":
                raise OperationalException(
                    "Binance Portfolio Margin found an order intent from another operation. "
                    "No new order was submitted and trading remains stopped."
                )
            new_intents = {**current_intents, intent.client_order_id: intent}
            try:
                self._validate_intents(tuple(new_intents.values()))
            except ValueError as exc:
                raise OperationalException(
                    "Binance Portfolio Margin rejected an invalid pending order intent. "
                    "Trading remains stopped."
                ) from exc
            self._atomic_write(new_intents)
            self._intents = new_intents

    def remove(self, client_order_id: str) -> None:
        if self.path is None:
            return
        with self._locked():
            current_intents = self._load()
            if client_order_id not in current_intents:
                raise OperationalException(
                    "Binance Portfolio Margin could not clear an absent order intent. "
                    "Trading remains stopped."
                )
            new_intents = {
                key: intent for key, intent in current_intents.items() if key != client_order_id
            }
            try:
                self._validate_intents(tuple(new_intents.values()))
            except ValueError as exc:
                raise OperationalException(
                    "Binance Portfolio Margin refused to orphan a pending containment intent. "
                    "Trading remains stopped."
                ) from exc
            self._atomic_write(new_intents)
            self._intents = new_intents

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
                "Another process is updating Binance Portfolio Margin order intents. "
                "No order was submitted and trading remains stopped."
            ) from exc

    def _load(self) -> dict[str, PortfolioOrderIntent]:
        path = self.path
        if path is None or not path.exists():
            return {}
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > _MAX_STATE_BYTES:
                raise ValueError("invalid state file")
            with path.open(encoding="utf-8") as file:
                payload = json.load(file)
            if not isinstance(payload, dict) or set(payload) != {"version", "intents"}:
                raise ValueError("invalid state object")
            if payload["version"] != self._version or not isinstance(payload["intents"], list):
                raise ValueError("unsupported state schema")
            intents = []
            expected_fields = {
                "client_order_id",
                "pair",
                "order_kind",
                "purpose",
                "parent_client_order_id",
            }
            for item in payload["intents"]:
                if not isinstance(item, dict) or set(item) != expected_fields:
                    raise ValueError("invalid intent object")
                intents.append(PortfolioOrderIntent(**item))
            self._validate_intents(tuple(intents))
            return {intent.client_order_id: intent for intent in intents}
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise OperationalException(
                "Binance Portfolio Margin could not safely load its pending order intents. "
                "Trading remains stopped; inspect the local runtime state before restarting."
            ) from exc

    def _validate_intents(  # noqa: C901
        self, intents: tuple[PortfolioOrderIntent, ...]
    ) -> None:
        if len(intents) > _MAX_PENDING_INTENTS:
            raise ValueError("too many pending intents")
        client_ids = {intent.client_order_id for intent in intents}
        if len(client_ids) != len(intents):
            raise ValueError("duplicate client order id")
        conditional_count = 0
        for intent in intents:
            if not _CLIENT_ORDER_ID_PATTERN.fullmatch(intent.client_order_id):
                raise ValueError("invalid client order id")
            if intent.pair not in self._allowed_pairs:
                raise ValueError("intent pair is not configured")
            if intent.order_kind not in ("regular", "conditional"):
                raise ValueError("invalid order kind")
            if intent.purpose not in ("submission", "containment"):
                raise ValueError("invalid intent purpose")
            if intent.purpose == "containment":
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
            elif intent.parent_client_order_id is not None:
                raise ValueError("unexpected parent client order id")
            if intent.order_kind == "conditional":
                conditional_count += 1
        if conditional_count > 1:
            raise ValueError("multiple pending conditional intents")

    def _atomic_write(self, intents: dict[str, PortfolioOrderIntent]) -> None:
        path = self.path
        if path is None:
            return
        payload = {
            "version": self._version,
            "intents": [asdict(intent) for intent in intents.values()],
        }
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
                "Binance Portfolio Margin could not atomically persist pending order intent. "
                "No order may be submitted and trading remains stopped."
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
