# -*- coding: utf-8 -*-
"""任务中心控制能力：账号任务暂停/恢复/取消与运行中修改并发。"""
import pytest

from core import db, live_check_service, plus_activation_store, task_control, task_center_store
from webui.app import create_app


@pytest.fixture
def client():
    app = create_app(auth_code="task-control-test")
    client = app.test_client()
    client.environ_base["HTTP_X_AUTH_CODE"] = "task-control-test"
    return client


@pytest.fixture(autouse=True)
def clean_controls():
    yield
    task_control.clear_controls()
    for name in task_control.pool_names():
        pool = task_control.get_pool(name)
        if pool is not None and not pool.closed:
            pool.set_workers(3 if name != "registration" else 4)


def account():
    db._save_accounts([{
        "id": 7, "email": "control@example.test", "access_token": "private-token",
        "created_at": "2026-01-01T00:00:00",
    }])
    return 7


def active_task(client, job_type):
    payload = client.get("/api/tasks/active").get_json()
    return next(task for task in payload["items"] if task["job_type"] == job_type)


def test_running_account_task_exposes_pause_and_cancel(client):
    account_id = account()
    assert db.claim_account_live_check(acc_id=account_id, trigger="manual")
    assert db.mark_account_live_check_running(account_id)
    handle = task_control.control(live_check_service.KIND, account_id)

    task = active_task(client, "live_check")
    assert task["capabilities"] == {"pause": True, "resume": False, "cancel": True}
    assert task["control_state"] == "running"

    paused = client.post(f"/api/tasks/{task['id']}/pause", json={})
    assert paused.status_code == 200
    assert paused.get_json()["display_state"] == "paused"
    assert handle.state() == "paused"
    task = active_task(client, "live_check")
    assert task["status"] == "paused"
    assert task["capabilities"] == {"pause": False, "resume": True, "cancel": True}

    resumed = client.post(f"/api/tasks/{task['id']}/resume", json={})
    assert resumed.status_code == 200
    assert resumed.get_json()["display_state"] == "running"
    assert handle.state() == "running"
    assert active_task(client, "live_check")["status"] == "running"


def test_cancel_running_account_task_shows_stopping(client):
    account_id = account()
    assert db.claim_account_live_check(acc_id=account_id, trigger="manual")
    assert db.mark_account_live_check_running(account_id)
    handle = task_control.control(live_check_service.KIND, account_id)

    task = active_task(client, "live_check")
    cancelled = client.post(f"/api/tasks/{task['id']}/cancel", json={})
    assert cancelled.status_code == 200
    assert cancelled.get_json()["display_state"] == "stopping"
    assert handle.state() == "cancelled"
    assert active_task(client, "live_check")["status"] == "stopping"
    # 已发出取消信号后不再提供任何操作按钮
    assert active_task(client, "live_check")["capabilities"] == {"pause": False, "resume": False, "cancel": False}


def test_cancel_queued_account_task_marks_cancelled(client):
    account_id = account()
    assert db.claim_account_live_check(acc_id=account_id, trigger="manual")
    handle = task_control.control(live_check_service.KIND, account_id)

    task = active_task(client, "live_check")
    assert task["status"] == "pending"
    cancelled = client.post(f"/api/tasks/{task['id']}/cancel", json={})
    assert cancelled.get_json()["display_state"] == "cancelled"
    assert handle.state() == "cancelled"
    # 未开始的任务取消后直接结束，从进行中移入历史。
    history = client.get("/api/tasks/history").get_json()["items"]
    row = next(item for item in history if item["id"] == task["id"])
    assert row["status"] == "cancelled"


def test_action_without_control_is_rejected(client):
    account_id = account()
    assert db.claim_account_live_check(acc_id=account_id, trigger="manual")
    task = active_task(client, "live_check")
    assert task["capabilities"] == {"pause": False, "resume": False, "cancel": False}
    response = client.post(f"/api/tasks/{task['id']}/pause", json={})
    assert response.status_code == 409


def test_plus_activation_capabilities_follow_control(client):
    account_id = account()
    plus_activation_store.claim(account_id)
    task = active_task(client, "plus_activation")
    assert task["capabilities"] == {"pause": False, "resume": False, "cancel": False}

    task_control.control("plus_activation", account_id)
    task = active_task(client, "plus_activation")
    assert task["capabilities"] == {"pause": True, "resume": False, "cancel": True}
    response = client.post(f"/api/tasks/{task['id']}/cancel", json={})
    assert response.status_code == 200
    assert response.get_json()["display_state"] == "cancelled"
    assert task_center_store.get_task(task["id"])["status"] == "cancelled"


def test_concurrency_endpoint_changes_running_pool(client):
    payload = client.get("/api/tasks/concurrency").get_json()
    assert payload["ok"] is True
    names = {pool["name"] for pool in payload["pools"]}
    assert {"registration", "live_check", "plan_check", "extract_link", "plus_activation"} <= names
    assert all(1 <= pool["workers"] <= 16 for pool in payload["pools"])

    response = client.post("/api/tasks/concurrency", json={"job_type": "live_check", "workers": 6})
    assert response.status_code == 200
    body = response.get_json()
    assert body["workers"] == 6 and body["persisted"] == "LIVE_CHECK_WORKERS"
    assert task_control.get_pool("live_check").workers == 6
    assert live_check_service.queue_settings()["workers"] == 6
    assert {pool["name"]: pool["workers"] for pool in body["pools"]}["live_check"] == 6

    again = client.get("/api/tasks/concurrency").get_json()
    assert {pool["name"]: pool["workers"] for pool in again["pools"]}["live_check"] == 6


def test_concurrency_endpoint_updates_registration_pool(client):
    response = client.post("/api/tasks/concurrency", json={"job_type": "registration", "workers": 5})
    assert response.status_code == 200
    assert response.get_json()["workers"] == 5
    from core import registration_service

    assert registration_service.get_executor().workers == 5
    assert registration_service.get_executor_workers() == 5


@pytest.mark.parametrize("body,status", [
    ({}, 400),
    ({"job_type": "live_check"}, 400),
    ({"job_type": "live_check", "workers": 0}, 400),
    ({"job_type": "live_check", "workers": 17}, 400),
    ({"job_type": "nope", "workers": 3}, 404),
])
def test_concurrency_endpoint_validates_input(client, body, status):
    assert client.post("/api/tasks/concurrency", json=body).status_code == status


def test_active_payload_includes_pools(client):
    payload = client.get("/api/tasks/active").get_json()
    assert isinstance(payload["pools"], list) and payload["pools"]
    assert all({"name", "workers", "running", "pending"} <= set(pool) for pool in payload["pools"])
