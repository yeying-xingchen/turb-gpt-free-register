"""Manual verification routes delegate only to the owning local-browser service."""
from unittest.mock import patch

from flask import Flask
import pytest

from core import live_check_service
from webui.task_routes import register_task_routes


@pytest.fixture
def client():
    app = Flask(__name__)
    app.config["TESTING"] = True
    register_task_routes(app)
    return app.test_client()


def test_list_manual_verifications_uses_service_without_task_projection(client):
    items = [{"account_id": 7, "email": "manual@example.test",
              "waiting_since": "2026-01-01T12:00:00"}]
    with patch.object(live_check_service, "list_manual_verifications", return_value=items, create=True) as listing, \
         patch("webui.task_routes.task_center_store.list_active_tasks") as projection:
        response = client.get("/api/tasks/manual-verifications")
    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "items": items}
    listing.assert_called_once_with()
    projection.assert_not_called()


def test_list_manual_verifications_can_be_empty(client):
    with patch.object(live_check_service, "list_manual_verifications", return_value=[], create=True):
        response = client.get("/api/tasks/manual-verifications")
    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "items": []}


def test_show_manual_verification_window_returns_service_result(client):
    result = {"ok": True, "message": "请在运行程序的桌面手动完成验证", "status": 200}
    with patch.object(live_check_service, "request_verification_window", return_value=result, create=True) as show:
        response = client.post("/api/tasks/manual-verifications/7/show", json={})
    assert response.status_code == 200
    assert response.get_json() == result
    show.assert_called_once_with(7)


@pytest.mark.parametrize("result", [
    {"ok": False, "error": "该账号没有待人工验证的窗口", "status": 409},
    {"ok": False, "error": "验证窗口已关闭"},
])
def test_show_manual_verification_window_conflict(client, result):
    with patch.object(live_check_service, "request_verification_window", return_value=result, create=True) as show:
        response = client.post("/api/tasks/manual-verifications/7/show", json={})
    assert response.status_code == 409
    assert response.get_json() == result
    show.assert_called_once_with(7)


def test_show_manual_verification_window_preserves_service_failure_status(client):
    result = {"ok": False, "error": "账号不存在", "status": 404}
    with patch.object(live_check_service, "request_verification_window", return_value=result, create=True):
        response = client.post("/api/tasks/manual-verifications/99/show", json={})
    assert response.status_code == 404
    assert response.get_json() == result


def test_show_manual_verification_window_rejects_non_integer_account_id(client):
    with patch.object(live_check_service, "request_verification_window", create=True) as show:
        response = client.post("/api/tasks/manual-verifications/invalid/show", json={})
    assert response.status_code == 404
    show.assert_not_called()
