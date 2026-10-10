"""Task notification integration contracts, isolated from transport/configuration."""
import json
from contextlib import closing

import pytest

from core import db, task_center_store as tasks


@pytest.fixture
def notifications(monkeypatch):
    from core import mail_notifications as module

    monkeypatch.setattr(module.settings, "SMTP_ENABLED", True)
    monkeypatch.setattr(module.settings, "SMTP_NOTIFY_TASKS", True)
    calls = []
    original = module.enqueue_notification

    def enqueue(conn, **message):
        assert conn.in_transaction
        calls.append(message)
        original(conn, **message)

    monkeypatch.setattr(module, "enqueue_notification", enqueue)
    monkeypatch.setattr(module, "send_notification", lambda *a, **kw: pytest.fail("network send inside task write"))
    db._ensure_sqlite()
    with closing(db._sqlite_conn()) as conn, conn:
        module._initialize(conn)
    return module, calls


def queued():
    with closing(db._sqlite_conn()) as conn:
        return [{"event_key": row["event_key"], "category": row["category"], **json.loads(row["payload"])}
                for row in conn.execute("SELECT * FROM mail_notifications ORDER BY id")]


def seed():
    db._save_accounts([{"id": 1, "email": "task@example.test", "access_token": "SECRET-TOKEN",
                        "password": "SECRET-PASSWORD", "created_at": "2025-01-01T00:00:00"}])


@pytest.mark.parametrize("source,expected", [("live", "success"), ("failed", "failed"),
    ("cancelled", "cancelled"), ("stopped", "stopped"), ("interrupted", "needs_attention")])
def test_account_terminal_transitions_are_safe_once_per_execution(notifications, source, expected):
    seed()
    for _ in range(2):
        assert db.claim_account_live_check(1)
        db.mark_account_live_check_running(1)
        db.update_account_liveness(1, {"status": source, "error": "SECRET-ERROR SECRET-TOKEN"})
        db.update_account_liveness(1, {"status": source, "error": "SECRET-ERROR SECRET-TOKEN"})
    messages = queued()
    assert len(messages) == len(notifications[1]) == 2
    assert messages[0]["event_key"] != messages[1]["event_key"]
    for message in messages:
        assert message["category"] == "task"
        assert message["emails"] == ["task@example.test"]
        assert set(message["fields"]) == {"任务编号", "任务类型", "任务状态", "结束时间"}
        assert message["fields"]["任务类型"] == tasks.LABELS["live_check"]
        assert message["fields"]["任务状态"] == tasks._STAGE[expected]
        assert message["fields"]["结束时间"]
    assert "SECRET" not in json.dumps(messages)


def test_terminal_resolution_and_cancel_control_do_not_notify_twice(notifications):
    seed()
    db.claim_account_extract(1)
    assert tasks.set_task_control_state("extract_link", 1, "cancelled")
    db.update_account_extract(1, {"status": "cancelled"})
    assert len(queued()) == 1
    db.claim_account_extract(1)
    db.update_account_extract(1, {"status": "unknown"})
    db.update_account_extract(1, {"ok": True, "status": "success"})
    assert len(queued()) == len(notifications[1]) == 2


@pytest.mark.parametrize("status", ["success", "failed", "cancelled", "stopped", "needs_attention"])
def test_registration_completion_and_retry(notifications, status):
    job = db.create_job("outlook")
    for state in ("running", "paused", "stopping"):
        db.update_job(job["id"], status=state)
    assert queued() == []
    db.update_job(job["id"], status=status, email="reg@example.test", error="SECRET-ERROR",
                  stage="SECRET-STAGE", progress_message="SECRET-TOKEN")
    db.update_job(job["id"], status=status, completed_at="2025-02-03T00:00:00")
    db.update_job(job["id"], status="failed")
    assert len(queued()) == len(notifications[1]) == 1
    assert queued()[0]["fields"]["任务状态"] == tasks._STAGE[status]
    assert queued()[0]["fields"]["任务编号"] == f"registration-{job['id']}"
    assert "SECRET" not in json.dumps(queued())
    retry, created = db.create_retry_job(job["id"], job_type="codex_retry", email_source="outlook",
                                       email="reg@example.test")
    assert created
    db.update_job(retry["id"], status="success")
    assert len(queued()) == 2
    assert queued()[1]["fields"]["任务类型"] == tasks.LABELS["codex_retry"]
    assert queued()[0]["event_key"] != queued()[1]["event_key"]


def test_backfills_and_historical_job_edits_do_not_notify(notifications, monkeypatch):
    initialize, sync = tasks._initialize, tasks.sync_account
    monkeypatch.setattr(tasks, "_initialize", lambda conn: None)
    monkeypatch.setattr(tasks, "sync_account", lambda *args, **kwargs: None)
    seed()
    db.update_account_liveness(1, {"status": "live"})
    job = db.create_job("outlook")
    with db._row_write_transaction() as conn:
        job["status"] = "success"
        db._write_collection_row(conn, "jobs", job)
        conn.execute("DELETE FROM storage_meta WHERE key=?", (tasks._MIGRATION_KEY,))
    monkeypatch.setattr(tasks, "_initialize", initialize)
    monkeypatch.setattr(tasks, "sync_account", sync)
    with db._row_write_transaction() as conn:
        tasks._initialize(conn)
        tasks.sync_payment_record(conn, {"id": "old-payment", "account_id": 1,
                                       "status": "completed"}, backfill=True)
    db.update_job(job["id"], status="success", progress=100)
    db.update_account_note(1, "historical account edit")
    assert tasks.list_history_tasks_page()["total"] == 3
    assert queued() == notifications[1] == []


def test_account_enqueue_and_state_rollback_together(notifications):
    seed()
    db.claim_account_live_check(1)
    task_id = tasks.list_active_tasks()[0]["id"]
    with pytest.raises(RuntimeError, match="abort"):
        with db._row_write_transaction() as conn:
            account = db._select_collection_row(conn, "accounts", row_id=1)
            account["live_check_status"] = "live"
            db._write_collection_row(conn, "accounts", account)
            assert conn.execute("SELECT COUNT(*) FROM mail_notifications").fetchone()[0] == 1
            raise RuntimeError("abort")
    assert queued() == []
    assert tasks.get_task(task_id)["status"] == "pending"
    db.update_account_liveness(1, {"status": "live"})
    assert len(queued()) == 1


def test_payment_reactivation_preserves_execution_deduplication(notifications):
    seed()
    record = {"id": "payment-one", "account_id": 1, "status": "unknown"}
    with db._row_write_transaction() as conn:
        tasks.sync_payment_record(conn, record)
        tasks.sync_payment_record(conn, {**record, "status": "queued"})
        tasks.sync_payment_record(conn, {**record, "status": "completed"})
    assert len(notifications[1]) == 2
    assert len(queued()) == 1
    with db._row_write_transaction() as conn:
        tasks.sync_payment_record(conn, {**record, "id": "payment-two", "status": "completed"})
    assert len(queued()) == 2


@pytest.mark.parametrize("flag", ["SMTP_ENABLED", "SMTP_NOTIFY_TASKS"])
def test_disabled_notifications_leave_task_updates_successful(notifications, monkeypatch, flag):
    monkeypatch.setattr(notifications[0].settings, flag, False)
    seed()
    db.claim_account_live_check(1)
    db.update_account_liveness(1, {"status": "live"})
    job = db.create_job("outlook")
    db.update_job(job["id"], status="success")
    assert tasks.list_history_tasks_page()["total"] == 2
    assert queued() == []


def test_registration_enqueue_failure_rolls_back_business_state(notifications):
    module, _ = notifications
    job = db.create_job("outlook")
    enqueue = module.enqueue_notification

    def fail(conn, **kwargs):
        enqueue(conn, **kwargs)
        raise RuntimeError("outbox failed")

    module.enqueue_notification = fail
    with pytest.raises(RuntimeError, match="outbox failed"):
        db.update_job(job["id"], status="success")
    assert db.get_job(job["id"])["status"] == "pending"
    assert queued() == []
    module.enqueue_notification = enqueue
    db.update_job(job["id"], status="success")
    assert len(queued()) == 1
