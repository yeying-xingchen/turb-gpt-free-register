"""Cancellation behavior while waiting for a Cloak browser slot."""
from types import SimpleNamespace
from unittest.mock import Mock
import threading

import pytest

from core import cloakbrowser_driver as driver_mod
from core import registration_service


def test_gate_cancel_does_not_consume_or_release_another_slot(monkeypatch):
    monkeypatch.setattr(driver_mod, "_cfg", SimpleNamespace(CLOAK_MAX_CONCURRENT=1))
    gate = driver_mod._BrowserGate()
    assert gate.acquire(timeout=0.1) is True

    check_stop = Mock(side_effect=registration_service.StopRequested("用户手动停止"))
    monkeypatch.setattr(registration_service, "check_stop_requested", check_stop)
    with pytest.raises(registration_service.StopRequested, match="用户手动停止"):
        gate.acquire(timeout=5)

    # 被取消的等待者没有拿到额度，不能在上层清理时误减正在运行任务的额度。
    assert gate.snapshot() == {"in_use": 1, "limit": 1}
    gate.release()
    assert gate.snapshot()["in_use"] == 0
    assert check_stop.call_count == 1


def test_gate_checks_cancellation_after_a_wait(monkeypatch):
    monkeypatch.setattr(driver_mod, "_cfg", SimpleNamespace(CLOAK_MAX_CONCURRENT=1))
    gate = driver_mod._BrowserGate()
    assert gate.acquire(timeout=0.1) is True
    check_stop = Mock(side_effect=[None, registration_service.StopRequested("已取消")])
    monkeypatch.setattr(registration_service, "check_stop_requested", check_stop)

    outcome = []

    def worker():
        try:
            gate.acquire(timeout=5)
        except BaseException as exc:
            outcome.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=5)
    assert len(outcome) == 1
    assert isinstance(outcome[0], registration_service.StopRequested)
    assert str(outcome[0]) == "已取消"
    assert gate.snapshot()["in_use"] == 1
    gate.release()
