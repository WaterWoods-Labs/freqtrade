"""Durable single-account entry reservations; unknown outcomes never authorize resubmission."""

import json
import math
import os
import time
from pathlib import Path

from filelock import FileLock

from freqtrade.bot_risk import positive, validate_bot_risk
from freqtrade.exceptions import InvalidOrderException, OperationalException


class EntryLedger:
    def __init__(self, path: Path, risk: dict):
        self.path = path
        self.risk = validate_bot_risk(risk)
        if not path.parent.is_dir() or any(item.is_symlink() for item in (path, *path.parents)):
            raise OperationalException(
                "Entry ledger requires an existing private runtime directory"
            )
        self.lock = FileLock(str(path) + ".lock", timeout=0)

    def load(self):
        if not self.path.exists():
            return {"schema_version": 1, "entries": {}}
        if self.path.is_symlink():
            raise OperationalException("Unsafe entry ledger")
        state = json.loads(self.path.read_text())
        if (
            not isinstance(state, dict)
            or set(state) != {"schema_version", "entries"}
            or state["schema_version"] != 1
            or not isinstance(state["entries"], dict)
        ):
            raise OperationalException("Invalid entry ledger")
        for key, entry in state["entries"].items():
            if (
                not isinstance(key, str)
                or not isinstance(entry, dict)
                or entry.get("pair") not in self.risk["pair_limits"]
                or not positive(entry.get("notional"))
            ):
                raise OperationalException(
                    "Unresolved entry ledger requires the original pair universe"
                )
        return state

    def save(self, state):
        staging = self.path.with_suffix(".tmp")
        if staging.is_symlink():
            raise OperationalException("Unsafe entry ledger staging path")
        with staging.open("w", encoding="utf-8") as stream:
            staging.chmod(0o600)
            json.dump(state, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        staging.replace(self.path)
        if os.name != "nt":
            descriptor = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

    def submit(self, *, pair, side, notional, initial, client_id, snapshot, send, now=None):
        """snapshot returns conservative position+open-order notionals and pending pairs.

        Resolved reservations remain conservative until explicit flat reconciliation. This
        intentionally cannot free capacity on a single possibly lagging venue snapshot.
        """
        now = time.time() if now is None else now
        with self.lock:
            state = self.load()
            if side not in {"buy", "sell"}:
                raise OperationalException("Invalid entry side")
            if client_id in state["entries"]:
                raise OperationalException(
                    "Entry client id already reserved; reconcile instead of retrying"
                )
            if any(entry.get("outcome") != "confirmed" for entry in state["entries"].values()):
                raise OperationalException(
                    "Unknown submission outcome; entries blocked pending reconciliation"
                )
            exposure, pending = snapshot()
            limits = self.risk["pair_limits"]
            if set(exposure) != set(limits) or any(
                type(v) not in (int, float) or not math.isfinite(v) or v < 0
                for v in exposure.values()
            ):
                raise OperationalException("Incomplete account exposure evidence")
            reserved = dict.fromkeys(limits, 0.0)
            counts = dict.fromkeys(limits, 0)
            for entry in state["entries"].values():
                if entry["pair"] == pair and entry.get("side") != side:
                    raise OperationalException("Add direction differs from its reserved entry")
                reserved[entry["pair"]] += entry["notional"]
                counts[entry["pair"]] += 1
            effective = {p: max(exposure[p], reserved[p]) for p in limits}
            cap = self.risk["max_initial_notional_usdt" if initial else "max_add_notional_usdt"]
            if pair not in limits or not positive(notional) or notional > cap + 1e-9:
                raise InvalidOrderException("Initial/add entry limit exceeded")
            if (
                pair in pending
                or (initial and effective[pair] > 1e-9)
                or (not initial and (counts[pair] != 1 or exposure[pair] <= 0))
            ):
                raise OperationalException(
                    "Entry conflicts with pending/unknown position or add count"
                )
            if not initial and any(
                not entry.get("terminal_filled")
                for entry in state["entries"].values()
                if entry["pair"] == pair
            ):
                raise OperationalException("Add requires exact terminal fill evidence")
            occupied = sum(v > 1e-9 for v in effective.values())
            if (
                (effective[pair] == 0 and occupied >= self.risk["max_occupied_symbols"])
                or effective[pair] + notional > limits[pair] + 1e-9
                or sum(effective.values()) + notional > self.risk["max_total_notional_usdt"] + 1e-9
            ):
                raise InvalidOrderException(
                    "Projected pair, total or occupied-symbol limit exceeded"
                )
            entry = {
                "pair": pair,
                "side": side,
                "notional": notional,
                "outcome": "unknown",
                "created_at": now,
            }
            state["entries"][client_id] = entry
            self.save(state)  # Persist intent before the first exchange write.
            result = send()
            if (
                not isinstance(result, dict)
                or not result.get("id")
                or result.get("clientOrderId") != client_id
                or result.get("symbol") != pair
                or result.get("side") != side
            ):
                raise OperationalException(
                    "Submission response does not prove exact entry identity"
                )
            entry.update(outcome="confirmed", order_id=str(result["id"]))
            self.save(state)
            return result

    def reconcile_flat(self, query, snapshot, *, now=None):
        """GET-only recovery; every exact order terminal and flat twice, >=60 seconds apart.

        Unknown exact orders stay reserved. No administrative clear/delete escape hatch.
        A lower budget does not change the ledger identity or erase previous reservations.
        """
        now = time.time() if now is None else now
        with self.lock:
            state = self.load()
            exposure, pending = snapshot()
            for client_id, entry in list(state["entries"].items()):
                order = query(client_id, entry["pair"])
                terminal = (
                    isinstance(order, dict)
                    and bool(order.get("id"))
                    and (not entry.get("order_id") or str(order["id"]) == entry["order_id"])
                    and order.get("clientOrderId") == client_id
                    and order.get("symbol") == entry["pair"]
                    and order.get("side") == entry["side"]
                    and order.get("status") in {"closed", "canceled", "expired", "rejected"}
                )
                filled = order.get("filled") if isinstance(order, dict) else None
                terminal = (
                    terminal
                    and type(filled) in (int, float)
                    and math.isfinite(filled)
                    and filled >= 0
                )
                entry["terminal_filled"] = bool(terminal and filled > 0)
                if terminal:
                    entry.update(outcome="confirmed", order_id=str(order["id"]))
                flat = (
                    terminal
                    and entry["pair"] in exposure
                    and exposure[entry["pair"]] == 0
                    and entry["pair"] not in pending
                )
                if flat:
                    first = entry.setdefault("flat_since", now)
                    if now - first >= 60:
                        del state["entries"][client_id]
                else:
                    entry.pop("flat_since", None)
            self.save(state)
            return len(state["entries"])
