"""Service regressions for manual verification, without network or account writes."""
from contextlib import nullcontext
from unittest.mock import Mock
import pytest
from core import live_check_service as service, task_control, task_center_store
from core.manual_verification import bind_callbacks, current_callbacks


def test_wait_keeps_session_and_clears_queue(monkeypatch):
    account_id = 7654321
    handle = task_control.control(service.KIND, account_id)
    monkeypatch.setattr(service._EXECUTOR, "park_current", lambda: nullcontext())
    project = Mock()
    monkeypatch.setattr(task_center_store, "set_task_control_state", project)
    monkeypatch.setattr(service, "_append_log", Mock())
    try:
        with service._manual_wait(account_id, "fixture@example.invalid"):
            assert handle.paused
            assert any(row["account_id"] == account_id for row in service.list_manual_verifications())
            assert service.request_verification_window(account_id)["ok"]
            assert service._manual_checkpoint(account_id) is True
            assert service._manual_checkpoint(account_id) is False
        assert not handle.paused
        assert not any(row["account_id"] == account_id for row in service.list_manual_verifications())
        assert service.request_verification_window(account_id)["status"] == 409
        assert [call.args[-1] for call in project.call_args_list] == ["paused", "running"]
    finally:
        task_control.release(service.KIND, account_id)


def test_cancel_wait_propagates_and_cleans_up(monkeypatch):
    account_id = 7654322
    handle = task_control.control(service.KIND, account_id)
    monkeypatch.setattr(service._EXECUTOR, "park_current", lambda: nullcontext())
    monkeypatch.setattr(task_center_store, "set_task_control_state", Mock())
    monkeypatch.setattr(service, "_append_log", Mock())
    try:
        with pytest.raises(task_control.TaskCancelled):
            with service._manual_wait(account_id, "fixture@example.invalid"):
                handle.cancel()
                service._manual_checkpoint(account_id)
        assert account_id not in service._WAITING
    finally:
        task_control.release(service.KIND, account_id)


def test_callback_binding_is_scoped():
    before = current_callbacks()
    wait, checkpoint = Mock(), Mock()
    with bind_callbacks(wait, checkpoint):
        assert current_callbacks() == (wait, checkpoint)
    assert current_callbacks() == before
