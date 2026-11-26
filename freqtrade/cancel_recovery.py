"""In-process control for a stopped writer reconciling a specific exchange cancellation."""

import logging
from threading import Event, Lock
from typing import TYPE_CHECKING

from freqtrade.enums import RPCMessageType, State
from freqtrade.exceptions import CancelReconciliationRequired, DDosProtection, TemporaryError


if TYPE_CHECKING:
    from freqtrade.freqtradebot import FreqtradeBot

logger = logging.getLogger(__name__)


class CancelRecovery:
    def __init__(self) -> None:
        self.lock = Lock()
        self.epoch = 0
        self.last_command: State | None = None
        self.held = False
        self.changed = Event()

    def note_command(self, target: State | None) -> None:
        """Caller holds lock through the operator's state change, including a repeated /stop."""
        self.epoch += 1
        self.last_command = target
        self.changed.set()

    def snapshot(self, bot: "FreqtradeBot") -> tuple[int, State]:
        with self.lock:
            return self.epoch, bot.state

    def handle(
        self, bot: "FreqtradeBot", incident: CancelReconciliationRequired, before: tuple[int, State]
    ) -> None:
        epoch, original_state = before
        with self.lock:
            self.held = True
            if bot.state == State.RELOAD_CONFIG or (
                self.epoch != epoch and self.last_command == State.RELOAD_CONFIG
            ):
                bot.state = State.RELOAD_CONFIG
                return
            bot.state = State.STOPPED
            if self.epoch != epoch:
                logger.info("Operator activity superseded automatic cancellation recovery.")
                return
            self.changed.clear()

        reason = "No conclusive exchange evidence."
        for delay in (0, 3, 8, 15):
            if self.changed.wait(delay):
                return
            try:
                # Serialize trade and ledger changes without blocking an owner's /stop.
                with bot._exit_lock:
                    if self.changed.is_set():
                        return
                    bot.exchange.reconcile_cancel(bot, incident)
                    with self.lock:
                        if self.epoch == epoch and bot.state == State.STOPPED:
                            self.held = False
                            bot.state = original_state
                            logger.info("Cancellation reconciled. Resuming %s.", original_state)
                return
            except Exception as exc:
                reason = (
                    str(exc)
                    if getattr(exc, "safe_to_notify", False) is True
                    else type(exc).__name__
                )
                if not isinstance(exc, TemporaryError) or isinstance(exc, DDosProtection):
                    break
                logger.info("Waiting for cancellation evidence: %s", reason)

        if not self.changed.is_set():
            bot.notify_status(
                f"Cancellation reconciliation stopped for {incident.pair} "
                f"({incident.kind}, order {incident.order_id}). {reason} "
                "Trading is stopped. Check the exchange fills, positions and protection.",
                msg_type=RPCMessageType.EXCEPTION,
            )
