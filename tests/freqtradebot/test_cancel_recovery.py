import pytest

from freqtrade.enums import RPCMessageType, State
from freqtrade.exceptions import (
    CancelReconciliationPending,
    CancelReconciliationRequired,
    OperationalException,
    TemporaryError,
)
from freqtrade.persistence import Trade
from freqtrade.rpc.rpc import RPC, RPCException
from tests.conftest import get_patched_worker


@pytest.fixture
def recovery_case(mocker, default_conf):
    worker = get_patched_worker(mocker, default_conf)
    bot = worker.freqtrade
    incident = CancelReconciliationRequired("partial_entry", "ETH/USDT:USDT", "123", "entry")
    proof = mocker.patch.object(bot.exchange, "reconcile_cancel")
    mocker.patch.object(bot, "process", side_effect=incident)
    mocker.patch.object(bot, "notify_status")
    mocker.patch.object(bot.cancel_recovery.changed, "wait", return_value=False)
    return worker, bot, incident, proof


@pytest.mark.parametrize("previous", [State.RUNNING, State.PAUSED])
def test_temporary_evidence_resumes_original_state_silently(recovery_case, previous):
    worker, bot, incident, proof = recovery_case
    bot.state = previous

    def verified(*args):
        assert args == (bot, incident)
        assert bot.state is State.STOPPED
        assert bot.cancel_recovery.held
        if proof.call_count == 1:
            raise TemporaryError("not yet visible")

    proof.side_effect = verified
    worker._process_running()
    assert proof.call_count == 2
    assert bot.state is previous
    assert not bot.cancel_recovery.held
    bot.notify_status.assert_not_called()


@pytest.mark.parametrize("error,attempts", [(TemporaryError, 4), (OperationalException, 1)])
def test_unresolved_holds_stopped_and_notifies_once(recovery_case, mocker, error, attempts):
    worker, bot, _, proof = recovery_case
    proof.side_effect = error("private response body")
    worker._process_running()
    assert bot.state is State.STOPPED
    assert proof.call_count == attempts
    rpc = RPC(bot)
    with pytest.raises(RPCException, match="holds trading stopped"):
        rpc._rpc_start()
    with pytest.raises(RPCException, match="locked"):
        rpc._rpc_delete(1)
    with pytest.raises(RPCException, match="locked"):
        rpc._rpc_reload_trade_from_exchange(1)
    bot.config["cancel_open_orders_on_exit"] = True
    cancel = mocker.patch.object(bot, "cancel_all_open_orders")
    mocker.patch.object(worker, "_sleep")
    worker._worker(State.RUNNING)
    worker._worker(State.STOPPED)
    rollback = mocker.spy(Trade.session, "rollback")
    commit = mocker.spy(Trade, "commit")
    bot.cleanup()
    rollback.assert_called_once()
    commit.assert_not_called()
    cancel.assert_not_called()
    bot.notify_status.assert_called_once()
    args, kwargs = bot.notify_status.call_args
    assert "private response body" not in args[0]
    assert "123" in args[0]
    assert kwargs["msg_type"] is RPCMessageType.EXCEPTION


@pytest.mark.parametrize("when", ["before_exception", "during_proof"])
@pytest.mark.parametrize("command", ["_rpc_stop", "_rpc_reload_config"])
def test_operator_veto_survives_successful_proof(recovery_case, when, command):
    worker, bot, incident, proof = recovery_case
    control = getattr(RPC(bot), command)
    if when == "before_exception":

        def stop_first():
            control()
            raise incident

        bot.process.side_effect = stop_first
    else:
        proof.side_effect = lambda *_: control()
    worker._process_running()
    assert bot.state is (State.STOPPED if command == "_rpc_stop" else State.RELOAD_CONFIG)
    assert bot.cancel_recovery.held
    assert proof.call_count == int(when == "during_proof")
    bot.notify_status.assert_not_called()


def test_pending_cancel_uses_normal_retry_until_it_causes_stop(recovery_case, mocker):
    worker, bot, incident, proof = recovery_case
    pending = CancelReconciliationPending("waiting", incident)
    bot.process.side_effect = pending
    sleep = mocker.patch("freqtrade.worker.time.sleep")
    worker._process_running()
    sleep.assert_called_once()
    proof.assert_not_called()

    def stopped():
        bot.state = State.STOPPED
        raise pending

    bot.process.side_effect = stopped
    worker._process_running()
    proof.assert_called_once_with(bot, incident)
    assert bot.state is State.RUNNING


def test_reload_of_unresolved_cancel_requires_fresh_start(recovery_case, mocker):
    worker, bot, _, proof = recovery_case
    proof.side_effect = OperationalException("conflict")
    worker._process_running()
    RPC(bot)._rpc_reload_config()
    mocker.patch.object(bot, "cleanup")
    fresh = mocker.Mock(state=State.RUNNING)
    mocker.patch.object(worker, "_init", side_effect=lambda _: setattr(worker, "freqtrade", fresh))
    worker._reconfigure()
    assert fresh.state is State.STOPPED
