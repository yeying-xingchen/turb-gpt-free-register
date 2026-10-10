"""Web-only manual verification transport, all inputs are offline fixtures."""
import base64
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from flask import Flask
from core import live_check_service as service, task_center_store, task_control
from core.manual_verification import bind_callbacks, current_browser_bridge
from webui.task_routes import register_task_routes


@pytest.fixture
def waiting(monkeypatch):
    account_id = 998877
    monkeypatch.setattr(service._EXECUTOR, "park_current", lambda: nullcontext())
    monkeypatch.setattr(task_center_store, "set_task_control_state", Mock())
    monkeypatch.setattr(service, "_append_log", Mock())
    with service._manual_wait(account_id, "fixture@example.invalid"):
        yield account_id
    task_control.release(service.KIND, account_id)


def browser():
    return SimpleNamespace(url="https://chatgpt.com/auth/login", viewport_size={"width": 800, "height": 600},
                           evaluate=Mock(return_value=True), screenshot=Mock(return_value=b"fixture-jpeg"),
                           mouse=SimpleNamespace(click=Mock()))


def test_frames_are_on_demand_and_clicks_run_on_browser_thread(waiting):
    page = browser()
    service._pump_manual_browser(waiting, page)
    page.screenshot.assert_not_called()
    assert service.manual_verification_frame(waiting)["pending"]
    service._pump_manual_browser(waiting, page)
    frame = service.manual_verification_frame(waiting)["frame"]
    assert base64.b64decode(frame["image"]) == b"fixture-jpeg"
    assert (frame["width"], frame["height"]) == (800, 600)
    assert service.request_verification_input(waiting, {"x": 70, "y": 80, "revision": frame["revision"]})["ok"]
    page.mouse.click.assert_not_called()
    service._pump_manual_browser(waiting, page)
    page.mouse.click.assert_called_once_with(70, 80)
    assert service.request_verification_input(waiting, {"x": 70, "y": 80, "revision": frame["revision"]})["status"] == 409


@pytest.mark.parametrize("point", [(True, 0), (-1, 0), (800, 0), (1, 600), (float("nan"), 0), ("1", 0)])
def test_invalid_coordinates_never_click(waiting, point):
    service.manual_verification_frame(waiting)
    page = browser()
    service._pump_manual_browser(waiting, page)
    frame = service.manual_verification_frame(waiting)["frame"]
    result = service.request_verification_input(waiting, {"x": point[0], "y": point[1], "revision": frame["revision"]})
    assert result["status"] == 400
    page.mouse.click.assert_not_called()


def test_recovered_page_is_not_captured(waiting):
    service.manual_verification_frame(waiting)
    page = browser()
    page.evaluate.return_value = False
    service._pump_manual_browser(waiting, page)
    page.screenshot.assert_not_called()
    assert service.manual_verification_frame(waiting)["pending"]


def test_queue_is_bounded_and_cleared_after_wait(waiting):
    service.manual_verification_frame(waiting)
    page = browser()
    service._pump_manual_browser(waiting, page)
    frame = service.manual_verification_frame(waiting)["frame"]
    click = {"x": 1, "y": 2, "revision": frame["revision"]}
    assert service.request_verification_input(waiting, click)["ok"]
    assert service.request_verification_input(waiting, click)["status"] == 429


def test_non_waiting_account_has_no_frame_or_input():
    assert service.manual_verification_frame(-12345)["status"] == 409
    assert service.request_verification_input(-12345, {})["status"] == 409
    assert service.request_verification_input(-12345, []) ["status"] == 400


def test_frame_route_pending_ready_and_finished(waiting):
    app = Flask(__name__)
    app.config["TESTING"] = True
    register_task_routes(app)
    client = app.test_client()
    response = client.get(f"/api/tasks/manual-verifications/{waiting}/frame")
    assert response.status_code == 202
    assert "no-store" in response.headers["Cache-Control"]
    service._pump_manual_browser(waiting, browser())
    response = client.get(f"/api/tasks/manual-verifications/{waiting}/frame")
    assert response.status_code == 200
    frame = response.get_json()["frame"]
    response = client.post(f"/api/tasks/manual-verifications/{waiting}/input", json={"x": 7, "y": 8, "revision": frame["revision"]})
    assert response.status_code == 200
    assert "no-store" in response.headers["Cache-Control"]
    assert client.get("/api/tasks/manual-verifications/123456/frame").status_code == 409
    assert client.post(f"/api/tasks/manual-verifications/{waiting}/input", json=[]).status_code == 400


def test_recovered_page_discards_queued_click(waiting):
    service.manual_verification_frame(waiting)
    page = browser()
    service._pump_manual_browser(waiting, page)
    frame = service.manual_verification_frame(waiting)["frame"]
    assert service.request_verification_input(waiting, {"x": 5, "y": 6, "revision": frame["revision"]})["ok"]
    page.evaluate.return_value = False
    service._pump_manual_browser(waiting, page)
    page.mouse.click.assert_not_called()
    assert service._REMOTE_VERIFICATIONS[waiting]["frame"] is None


def test_browser_bridge_is_task_scoped():
    before = current_browser_bridge()
    bridge = Mock()
    with bind_callbacks(nullcontext, lambda: None, bridge):
        assert current_browser_bridge() is bridge
    assert current_browser_bridge() is before
