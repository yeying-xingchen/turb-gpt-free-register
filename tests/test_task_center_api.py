"""Task center API integration with isolated SQLite and no provider calls."""
import threading
from unittest.mock import patch

import pytest

from core import db, plus_activation_store
from webui.app import create_app


@pytest.fixture
def client():
    app = create_app(auth_code="task-center-test")
    client = app.test_client()
    client.environ_base["HTTP_X_AUTH_CODE"] = "task-center-test"
    return client


def account():
    db._save_accounts([{
        "id": 7, "email": "tasks@example.test", "access_token": "private-account-token",
        "totp_secret": "private-totp-secret", "registration_password": "private-password",
        "created_at": "2026-01-01T00:00:00",
    }])
    return 7


def test_active_tasks_include_plus_live_and_registration_with_distinct_ids(client):
    account_id = account()
    job = db.create_job("outlook")
    assert db.claim_account_live_check(account_id)
    plus_activation_store.claim(account_id)

    response = client.get("/api/tasks/active")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["total"] == 3
    assert payload["status_counts"]["pending"] == 3
    tasks = {row["job_type"]: row for row in payload["items"]}
    assert set(tasks) == {"registration", "live_check", "plus_activation"}
    assert tasks["registration"]["id"] == f"registration-{job['id']}"
    assert tasks["registration"]["job_id"] == job["id"]
    assert tasks["live_check"]["id"].startswith("account-")
    assert len({row["id"] for row in tasks.values()}) == 3
    assert tasks["plus_activation"]["capabilities"] == {"pause": False, "resume": False, "cancel": False}
    assert "private-" not in response.get_data(as_text=True)
    assert response.headers["Cache-Control"] == "no-store, max-age=0"
    # Registration's separate page and cancellation counts keep their meaning.
    assert client.get("/api/jobs?paged=1").get_json()["total"] == 1


def test_existing_live_and_plus_submission_routes_populate_task_center(client):
    from core import live_check_service, plus_activation_service

    account_id = account()
    with patch.object(live_check_service, "_QUEUE_SLOTS", threading.BoundedSemaphore(10)), \
         patch.object(live_check_service._EXECUTOR, "submit") as live_submit, \
         patch.object(plus_activation_service, "_QUEUE_SLOTS", threading.BoundedSemaphore(10)), \
         patch.object(plus_activation_service._EXECUTOR, "submit") as plus_submit, \
         patch.object(plus_activation_service, "_settings", return_value=([({}, "fixture")], [{}])):
        live_body = {"account_ids": [account_id]}
        plus_body = {"account_ids": [account_id], "extraction": {}, "payment": {}}
        assert client.post("/api/accounts/check-live-bulk", json=live_body).get_json()["started_count"] == 1
        assert client.post("/api/accounts/activate-plus", json=plus_body).get_json()["started_count"] == 1
        assert client.post("/api/accounts/check-live-bulk", json=live_body).get_json()["busy_count"] == 1
        assert client.post("/api/accounts/activate-plus", json=plus_body).get_json()["busy_count"] == 1
        live_submit.assert_called_once()
        plus_submit.assert_called_once()
    response = client.get("/api/tasks/active").get_json()
    assert response["total"] == 2
    assert {task["job_type"] for task in response["items"]} == {"live_check", "plus_activation"}


def test_live_enqueue_failure_is_recorded_as_history(client):
    from core import live_check_service

    account_id = account()
    with patch.object(live_check_service, "_QUEUE_SLOTS", threading.BoundedSemaphore(10)), \
         patch.object(live_check_service._EXECUTOR, "submit", side_effect=RuntimeError("executor closed")):
        response = client.post("/api/accounts/check-live-bulk", json={"account_ids": [account_id]})
    assert response.get_json()["failed_count"] == 1
    assert client.get("/api/tasks/active").get_json()["total"] == 0
    history = client.get("/api/tasks/history").get_json()
    assert history["total"] == 1
    assert history["items"][0]["job_type"] == "live_check"
    assert history["items"][0]["status"] == "failed"


def test_plus_progress_and_attention_result_move_to_history(client):
    account_id = account()
    claimed, _ = plus_activation_store.claim(account_id)
    run_id = claimed["plus_activation_run_id"]
    plus_activation_store.update(account_id, run_id, "paying", "正在支付")
    task = client.get("/api/tasks/active").get_json()["items"][0]
    assert task["job_type"] == "plus_activation"
    assert task["status"] == "running"
    assert task["source_status"] == "paying"
    assert 0 < task["progress"] < 100

    plus_activation_store.update(account_id, run_id, "needs_attention", "支付结果待核实")
    assert client.get("/api/tasks/active").get_json()["total"] == 0
    history = client.get("/api/tasks/history").get_json()
    assert history["total"] == 1
    assert history["items"][0]["id"] == task["id"]
    assert history["items"][0]["status"] == "needs_attention"
    log = client.get(f"/api/tasks/{task['id']}/log").get_json()
    assert log["job"]["id"] == task["id"]
    assert log["log"]


def test_repeated_live_checks_retain_separate_history_and_global_pagination(client):
    account_id = account()
    assert db.claim_account_live_check(account_id)
    db.mark_account_live_check_running(account_id)
    db.update_account_liveness(account_id, {"ok": True, "status": "live"})
    first = client.get("/api/tasks/history").get_json()["items"][0]
    assert first["status"] == "success"
    assert db.claim_account_live_check(account_id)
    db.update_account_liveness(account_id, {"ok": False, "status": "deactivated"})
    job = db.create_job("outlook")
    db.update_job(job["id"], status="success", completed_at=db._now(), progress=100)

    items = []
    for page in range(1, 4):
        response = client.get(f"/api/tasks/history?page={page}&page_size=1").get_json()
        assert response["total"] == 3
        assert response["page"] == page
        assert response["page_size"] == 1
        items.extend(response["items"])
    assert len({row["id"] for row in items}) == 3
    assert any(row["source_status"] == "deactivated" for row in items)
    assert client.get("/api/tasks/history?page=4&page_size=1").get_json()["items"] == []
    old_log = client.get(f"/api/tasks/{first['id']}/log").get_json()
    assert old_log["job"]["source_status"] == "live"


def test_controls_use_owning_service_and_reject_account_operations(client):
    account_id = account()
    db.claim_account_live_check(account_id)
    task_id = client.get("/api/tasks/active").get_json()["items"][0]["id"]
    with patch("webui.task_routes.registration_service.request_stop_job") as stop:
        for action in ("pause", "resume", "cancel"):
            assert client.post(f"/api/tasks/{task_id}/{action}", json={}).status_code == 409
        stop.assert_not_called()
    assert db.get_account(account_id)["live_check_status"] == "queued"

    job = db.create_job("outlook")
    task_id = f"registration-{job['id']}"
    assert client.post(f"/api/tasks/{task_id}/pause", json={}).get_json()["state"] == "paused"
    assert client.post(f"/api/tasks/{task_id}/resume", json={}).get_json()["state"] == "pending"
    assert client.post(f"/api/tasks/{task_id}/cancel", json={}).get_json()["state"] == "cancelled"
    assert db.get_job(job["id"])["status"] == "cancelled"


@pytest.mark.parametrize("task_id", ["missing", "account-0", "registration-99999", "registration-nope"])
def test_unknown_ids_are_404(client, task_id):
    assert client.get(f"/api/tasks/{task_id}/log").status_code == 404
    assert client.post(f"/api/tasks/{task_id}/cancel", json={}).status_code == 404


def test_history_bounds_and_authentication(client):
    result = client.get("/api/tasks/history?page=-2&page_size=10000").get_json()
    assert result["page"] == 1
    assert result["page_size"] == 100
    assert client.post("/api/tasks/account-1/delete", json={}).status_code == 404
    client.environ_base.pop("HTTP_X_AUTH_CODE")
    for url in ("/api/tasks/active", "/api/tasks/history", "/api/tasks/account-1/log"):
        assert client.get(url).status_code == 401


def test_startup_recovery_updates_all_local_task_snapshots(client):
    from core.runtime import acquire_runtime_owner, recover_startup

    account_id = account()
    db.create_job("outlook")
    db.claim_account_live_check(account_id)
    db.claim_account_plan_check(account_id)
    db.claim_account_totp_setup(account_id)
    db.claim_account_email_change(account_id, "outlook")
    db.claim_account_codex_agent(account_id)
    db.claim_account_extract(account_id)
    db.update_account_codex_status("tasks@example.test", "retrying")
    plus_activation_store.claim(account_id)
    assert client.get("/api/tasks/active").get_json()["total"] == 9

    with acquire_runtime_owner() as owner:
        recover_startup(owner)
    assert client.get("/api/tasks/active").get_json()["total"] == 0
    history = client.get("/api/tasks/history").get_json()
    assert history["total"] == 9
    assert all(row["completed_at"] for row in history["items"])


def test_registration_partial_success_display_is_preserved(client):
    account_id = account()
    job = db.create_job("outlook")
    db.update_job(job["id"], account_id=account_id, status="failed", completed_at=db._now())
    row = client.get("/api/tasks/history").get_json()["items"][0]
    assert row["job_id"] == job["id"]
    assert row["status"] == "failed"
    assert row["display_status"] == "partial_success"
    details = client.get(f"/api/tasks/{row['id']}/log").get_json()
    assert details["job"]["display_status"] == "partial_success"


def test_account_task_log_uses_complete_service_log_and_latest_lookup(client):
    account_id = account()
    assert db.claim_account_live_check(account_id)
    active = client.get("/api/tasks/active").get_json()["items"]
    task = next(item for item in active if item["job_type"] == "live_check")

    with patch("core.task_center_store._account_detail_log", return_value="第一阶段\n第二阶段\n最终结果"):
        details = client.get(f"/api/tasks/{task['id']}/log")
    assert details.status_code == 200
    payload = details.get_json()
    assert payload["complete"] is True
    assert payload["log"] == "第一阶段\n第二阶段\n最终结果"

    latest = client.get(
        "/api/accounts/tasks/latest",
        query_string={"account_ids": str(account_id), "job_type": "live_check"},
    )
    assert latest.status_code == 200
    assert latest.get_json()["task_ids"] == [task["id"]]



    job = db.create_job("outlook")
    task_id = f"registration-{job['id']}"
    with patch("webui.task_routes.registration_service.read_job_log", return_value="运行中") as read_log:
        response = client.get(f"/api/tasks/{task_id}/log")
    assert response.status_code == 200
    read_log.assert_called_once_with(job["id"])
    result = response.get_json()
    assert result["log"] == "运行中"
    assert result["job"]["id"] == task_id
    assert "log_file" not in result["job"]
