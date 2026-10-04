"""Task history regressions: transactions, execution identity, privacy and SQL paging."""
import json
from contextlib import closing

import pytest

from core import db, plus_activation_store as plus, scan_payment_store as payment
from core import task_center_store as tasks


def seed(**fields):
    account = {"id": 1, "email": "task@example.test", "access_token": "private-at",
               "totp_secret": "private-totp", "extra_json": '{"registration_password":"private-password"}',
               "created_at": "2025-01-01T00:00:00", **fields}
    db._save_accounts([account])
    return db.get_account(1)


def account_history(kind=None):
    return [row for row in tasks.list_history_tasks_page(500)["items"]
            if row["id"].startswith("account-") and (kind is None or row["job_type"] == kind)]


def test_same_second_executions_busy_and_repeated_results(monkeypatch):
    stamp = db._now()
    monkeypatch.setattr(db, "_now", lambda: stamp)
    seed()
    for _ in range(2):
        assert db.claim_account_live_check(1)
        assert not db.claim_account_live_check(1)
        assert len(tasks.list_active_tasks()) == 1
        assert db.mark_account_live_check_running(1)
        assert db.mark_account_live_check_running(1)
        db.update_account_liveness(1, {"ok": True, "status": "live"})
        db.update_account_liveness(1, {"ok": True, "status": "live"})
    history = account_history("live_check")
    assert len(history) == 2
    assert len({row["id"] for row in history}) == 2
    assert len({row["created_at"] for row in history}) == 1
    for row in history:
        assert row["status"] == "success" and row["source_status"] == "live"
        assert row["started_at"] and row["completed_at"]
        assert "job_id" not in row
        assert row["capabilities"] == {"pause": False, "resume": False, "cancel": False}
        assert len(tasks.read_task_log(row["id"]).splitlines()) == 3
    assert tasks.task_status_counts()["total"] == 2
    assert tasks.task_status_counts()["active"] == 0


@pytest.mark.parametrize("claim,running,finish,kind", [
    (lambda: db.claim_account_plan_check(1), lambda: db.mark_account_plan_check_running(1),
     lambda: db.update_account_plan_check(1, result={"ok": True}), "plan_check"),
    (lambda: db.claim_account_extract(1), lambda: db.mark_account_extract_running(1),
     lambda: db.update_account_extract(1, {"ok": True}), "extract_link"),
    (lambda: db.claim_account_totp_setup(1), lambda: db.mark_account_totp_setup_running(1),
     lambda: db.update_account_totp_secret(1, {"ok": True}), "totp_setup"),
    (lambda: db.claim_account_email_change(1, "outlook"), lambda: db.mark_account_email_change_running(1, "next@example.test"),
     lambda: db.finish_account_email_change(1, ok=True, new_email="next@example.test"), "email_change"),
    (lambda: db.claim_account_codex_agent(1), lambda: db.mark_account_codex_agent_running(1),
     lambda: db.update_account_codex_agent(1, {"ok": True}), "codex_agent"),
])
def test_single_and_bulk_account_paths_track_one_execution(claim, running, finish, kind):
    seed()
    assert claim()
    task = tasks.list_active_tasks()[0]
    assert task["job_type"] == kind
    assert not claim()
    assert running()
    assert tasks.get_task(task["id"])["status"] == "running"
    assert finish()
    completed = tasks.get_task(task["id"])
    assert completed["status"] == "success"
    assert len(account_history(kind)) == 1
    assert len(tasks.read_task_log(task["id"]).splitlines()) == 3


def test_plus_phases_attention_and_retries_keep_original_log():
    seed()
    account, claimed = plus.claim(1)
    assert claimed
    assert not plus.claim(1)[1]
    run_id = account["plus_activation_run_id"]
    task = tasks.list_active_tasks()[0]
    for phase, percent, stage in [("checking", 15, "检查账号"), ("extracting", 35, "提取支付链接"),
                                   ("paying", 65, "提交支付"), ("verifying", 85, "核验 Plus 套餐")]:
        assert plus.update(1, run_id, phase, "private-cdk https://checkout.test/private")
        current = tasks.get_task(task["id"])
        assert (current["status"], current["source_status"], current["progress"], current["stage"]) == ("running", phase, percent, stage)
    assert plus.update(1, run_id, "needs_attention", "private-at private-cdk private-totp")
    first_log = tasks.read_task_log(task["id"])
    assert len(first_log.splitlines()) == 6
    assert tasks.get_task(task["id"])["completed_at"]
    assert not tasks.list_active_tasks()
    second, claimed = plus.claim(1)
    assert claimed
    assert plus.complete(1, second["plus_activation_run_id"], "verified")
    assert len(account_history("plus_activation")) == 2
    assert tasks.read_task_log(task["id"]) == first_log
    assert "private-" not in first_log


@pytest.mark.parametrize("source", ["unknown", "interrupted", "needs_attention", "awaiting_blik"])
def test_attention_sources_are_terminal_and_resolvable(source):
    seed()
    db.claim_account_extract(1)
    db.update_account_extract(1, {"status": source})
    row = account_history("extract_link")[0]
    assert row["status"] == "needs_attention" and row["completed_at"]
    assert not tasks.list_active_tasks()
    db.update_account_extract(1, {"ok": True, "status": "success"})
    assert tasks.get_task(row["id"])["status"] == "success"
    assert len(account_history("extract_link")) == 1


def test_bulk_recovery_ends_tasks_and_unrelated_writes_do_not_add_history():
    seed()
    claims = [lambda: db.claim_account_live_check(1), lambda: db.claim_account_plan_check(1),
              lambda: db.claim_account_extract(1), lambda: db.claim_account_totp_setup(1),
              lambda: db.claim_account_codex_agent(1), lambda: db.claim_account_email_change(1, "outlook")]
    for claim in claims:
        assert claim()
    plus.claim(1)
    assert len(tasks.list_active_tasks()) == 7
    for recover in [db.recover_interrupted_live_checks, db.recover_interrupted_plan_checks,
                    db.recover_interrupted_extract_links, db.recover_interrupted_totp_setups,
                    db.recover_interrupted_codex_agents, db.recover_interrupted_email_changes,
                    plus.recover_interrupted]:
        assert recover() == 1
    assert tasks.task_status_counts()["active"] == 0
    history = account_history()
    before = {row["id"]: tasks.read_task_log(row["id"]) for row in history}
    db.update_account_note(1, "keep")
    assert {row["id"]: tasks.read_task_log(row["id"]) for row in account_history()} == before
    assert all(row["completed_at"] for row in history)


def test_delete_account_closes_tasks_and_keeps_history():
    seed()
    db.claim_account_live_check(1)
    plus.claim(1)
    assert db.delete_account(1)
    assert not tasks.list_active_tasks()
    history = account_history()
    assert len(history) == 2
    assert {row["job_type"]: row["status"] for row in history} == {"live_check": "cancelled", "plus_activation": "needs_attention"}
    assert all(row["completed_at"] for row in history)
    assert all("账号已删除" in tasks.read_task_log(row["id"]) for row in history)
    seed()
    assert db.claim_account_live_check(1)
    assert len(account_history()) == 2
    assert tasks.list_active_tasks()[0]["id"] not in {row["id"] for row in history}


def test_task_hook_failure_rolls_back_account_and_plus_and_payment():
    seed()
    with closing(db._sqlite_conn()) as conn, conn:
        conn.execute("CREATE TRIGGER deny_task BEFORE INSERT ON account_tasks BEGIN SELECT RAISE(ABORT, 'reject task'); END")
    with pytest.raises(db.sqlite3.IntegrityError, match="reject task"):
        db.claim_account_live_check(1)
    assert not db.get_account(1).get("live_check_status")
    with pytest.raises(db.sqlite3.IntegrityError, match="reject task"):
        plus.claim(1)
    assert not db.get_account(1).get("plus_activation_status")
    with pytest.raises(db.sqlite3.IntegrityError, match="reject task"):
        payment.reserve(account_id=1, provider="v1", api_base="https://api.test", cdk="private-cdk",
                        body={"link": "https://checkout.test/private"}, idempotency_key="private-idempotency")
    with closing(db._sqlite_conn()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM scan_submissions").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM account_task_events").fetchone()[0] == 0


def test_payment_journal_identity_and_redaction_and_late_updates():
    seed()
    records = []
    for index in range(2):
        record, claimed = payment.reserve(account_id=1, provider="v1", api_base="https://api.test", cdk="private-cdk",
                                         body={"link": f"https://checkout.test/private-{index}", "email": "task@example.test"},
                                         idempotency_key=f"private-key-{index}")
        assert claimed
        same, claimed = payment.reserve(account_id=1, provider="v1", api_base="https://api.test", cdk="private-cdk",
                                        body=record["body"], idempotency_key=f"private-key-{index}")
        assert not claimed and same["id"] == record["id"]
        records.append(record)
        payment.finish(record, status="failed", error="private-cdk private-at private-password")
    history = account_history("scan_payment")
    assert len(history) == 2
    first_id = history[-1]["id"]
    payment.finish(records[0], status="completed")
    assert tasks.get_task(first_id)["status"] == "success"
    assert len(account_history("scan_payment")) == 2
    db.update_account_note(1, "no duplicate compact account task")
    assert len(account_history("scan_payment")) == 2
    with closing(db._sqlite_conn()) as conn:
        stored = json.dumps([dict(row) for row in conn.execute("SELECT * FROM account_tasks")])
    public = json.dumps(history) + stored + "".join(tasks.read_task_log(row["id"]) for row in history)
    for secret in ["private-cdk", "private-at", "private-password", "checkout.test", "credential_hash", "link_hash", "body", "idempotency"]:
        assert secret not in public


def test_standalone_payment_after_account_deletion_cannot_hang():
    seed()
    record, _ = payment.reserve(account_id=1, provider="v1", api_base="https://api.test", cdk="private-cdk",
                                body={"link": "https://checkout.test/private"}, idempotency_key="private-key")
    task_id = tasks.list_active_tasks()[0]["id"]
    db.delete_account(1)
    payment.finish(record, status="queued")
    assert tasks.get_task(task_id)["status"] == "needs_attention"
    assert not tasks.list_active_tasks()
    payment.finish(record, status="completed")
    assert tasks.get_task(task_id)["status"] == "success"
    assert tasks.get_task(task_id)["email"] == "task@example.test"


def test_registration_projection_capabilities_and_global_pagination(monkeypatch):
    seed()
    for number in range(1, 7):
        stamp = f"2026-01-0{number}T00:00:00"
        monkeypatch.setattr(db, "_now", lambda stamp=stamp: stamp)
        if number % 2:
            db.claim_account_live_check(1)
            db.update_account_liveness(1, {"ok": True})
        else:
            job = db.create_job("outlook")
            db.update_job(job["id"], status="success", progress=100)
    all_items = tasks.list_history_tasks_page(500)["items"]
    assert [row["created_at"] for row in all_items] == sorted([row["created_at"] for row in all_items], reverse=True)
    assert [row["job_type"] for row in all_items] == ["registration", "live_check"] * 3
    pages = [tasks.list_history_tasks_page(2, offset)["items"] for offset in (0, 2, 4)]
    assert [row["id"] for page in pages for row in page] == [row["id"] for row in all_items]
    assert tasks.list_history_tasks_page(2, 100) == {"items": [], "total": 6}
    job = db.create_job("outlook")
    task_id = f"registration-{job['id']}"
    for state, capabilities in [("pending", (True, False, True)), ("running", (True, False, True)),
                                ("paused", (False, True, True)), ("stopping", (False, False, False)),
                                ("failed", (False, False, False))]:
        db.update_job(job["id"], status=state, account_id=1, error="private-password", progress_message="private-at", stage="private-totp")
        row = tasks.get_task(task_id)
        assert tuple(row["capabilities"].values()) == capabilities
        assert row["job_id"] == job["id"] and row["label"] == "账号注册"
        assert "private-" not in json.dumps(row)
        assert "payload" not in row and "log_file" not in row
    assert tasks.read_task_log(task_id) == ""


def test_queries_are_bounded_and_do_not_rescan_account_payloads(monkeypatch):
    seed()
    db.claim_account_live_check(1)
    statements = []
    original = db._sqlite_conn
    def connect():
        conn = original()
        conn.set_trace_callback(statements.append)
        return conn
    monkeypatch.setattr(db, "_sqlite_conn", connect)
    monkeypatch.setattr(db, "_load_accounts", lambda: pytest.fail("full account scan"))
    assert len(tasks.list_active_tasks(1)) == 1
    assert tasks.list_history_tasks_page(1, 1)["items"] == []
    assert tasks.get_task("account-1")
    assert tasks.task_status_counts()["active"] == 1
    assert tasks.read_task_log("account-1")
    assert not any("FROM accounts" in sql for sql in statements)
    projected_queries = [sql for sql in statements if " AS sort_at" in sql]
    assert projected_queries and all("LIMIT " in sql for sql in projected_queries)


def test_migration_backfills_once_and_payment_records_separately(monkeypatch):
    # Simulate a pre-feature DB by suppressing initialization during initial writes.
    initialize, sync_account, sync_payment = tasks._initialize, tasks.sync_account, tasks.sync_payment_record
    monkeypatch.setattr(tasks, "_initialize", lambda conn: None)
    monkeypatch.setattr(tasks, "sync_account", lambda *args, **kwargs: None)
    monkeypatch.setattr(tasks, "sync_payment_record", lambda *args, **kwargs: None)
    seed(live_check_status="live", live_checked_at="2025-10-01T00:00:00", extract_link_status="awaiting_blik")
    record, _ = payment.reserve(account_id=1, provider="v1", api_base="https://api.test", cdk="private-cdk",
                                body={"link": "https://checkout.test/private"}, idempotency_key="private-key")
    payment.finish(record, status="unknown")
    monkeypatch.setattr(tasks, "_initialize", initialize)
    monkeypatch.setattr(tasks, "sync_account", sync_account)
    monkeypatch.setattr(tasks, "sync_payment_record", sync_payment)
    monkeypatch.setattr(db, "_SQLITE_READY", False)
    first = tasks.list_history_tasks_page(500)
    assert first["total"] == 3
    assert {row["job_type"] for row in first["items"]} == {"live_check", "extract_link", "scan_payment"}
    monkeypatch.setattr(db, "_SQLITE_READY", False)
    second = tasks.list_history_tasks_page(500)
    assert second == first
    assert all(len(tasks.read_task_log(row["id"]).splitlines()) == 1 for row in first["items"])


def test_codex_retry_only_starts_at_retrying_and_skips_registration_duplicate():
    seed(codex_status="success")
    assert tasks.task_status_counts()["total"] == 0
    db.update_account_codex_status("task@example.test", "retrying")
    task_id = tasks.list_active_tasks()[0]["id"]
    db.update_account_codex_status("task@example.test", "success")
    assert tasks.get_task(task_id)["job_type"] == "codex_retry"
    assert tasks.get_task(task_id)["status"] == "success"
    db.update_account_codex_status("task@example.test", "retrying")
    db.update_account_codex_status("task@example.test", "")
    assert len(account_history("codex_retry")) == 2
    job = db.create_job("outlook")
    db.update_job(job["id"], status="failed")
    retry, _ = db.create_retry_job(job["id"], job_type="codex_retry", email_source="outlook",
                                    email="task@example.test", account_id=1)
    db.update_account_codex_status("task@example.test", "retrying")
    assert tasks.list_active_tasks()[0]["id"] == f"registration-{retry['id']}"
    assert len(tasks.list_active_tasks()) == 1
    assert len(account_history("codex_retry")) == 2


def test_codex_restart_hook_and_deactivated_result():
    seed()
    db.update_account_codex_status("task@example.test", "retrying")
    assert tasks.recover_interrupted_codex_retries() == 1
    assert tasks.recover_interrupted_codex_retries() == 0
    assert account_history("codex_retry")[0]["status"] == "needs_attention"
    db.update_account_codex_status("task@example.test", "failed")
    db.update_account_codex_status("task@example.test", "retrying")
    db.update_account_codex_status("task@example.test", "deactivated")
    row = account_history("codex_retry")[0]
    assert row["status"] == "failed" and row["source_status"] == "deactivated"
    assert "账号已废" in row["progress_message"]


@pytest.mark.parametrize("task_id", ["account-0", "registration-x", "unknown-1", "account-9223372036854775808", None, 1])
def test_invalid_task_ids(task_id):
    assert tasks.get_task(task_id) is None
    assert tasks.read_task_log(task_id) == ""


def test_expired_payment_lease_converges_counts_and_keeps_journal(monkeypatch):
    monkeypatch.setattr(tasks.time, "time", lambda: 1000.0)
    seed()
    record, _ = payment.reserve(account_id=1, provider="v1", api_base="https://api.test", cdk="private-cdk",
                                body={"link": "https://checkout.test/private"}, idempotency_key="private-key")
    task_id = tasks.list_active_tasks()[0]["id"]
    monkeypatch.setattr(tasks.time, "time", lambda: 1200.0)
    assert tasks.task_status_counts()["active"] == 0
    assert tasks.get_task(task_id)["status"] == "needs_attention"
    assert tasks.get_task(task_id)["source_status"] == "unknown"
    assert len(tasks.read_task_log(task_id).splitlines()) == 2
    assert payment.latest(1)["status"] == "submitting"
    payment.finish(record, status="queued")
    assert tasks.task_status_counts()["active"] == 1
    assert tasks.get_task(task_id)["source_status"] == "queued"
    monkeypatch.setattr(tasks.time, "time", lambda: 9999.0)
    assert tasks.task_status_counts()["active"] == 1


def test_completed_history_keeps_original_email_and_result_snapshot():
    seed()
    db.claim_account_live_check(1)
    db.update_account_liveness(1, {"ok": True, "status": "live"})
    first = account_history("live_check")[0]
    original_log = tasks.read_task_log(first["id"])
    db.update_account_codex_status("task@example.test", "deactivated")
    assert tasks.get_task(first["id"])["source_status"] == "live"
    assert len(account_history("live_check")) == 2
    db.claim_account_email_change(1, "generic_api")
    db.finish_account_email_change(1, ok=True, new_email="new@example.test")
    assert tasks.get_task(first["id"])["email"] == "task@example.test"
    assert tasks.read_task_log(first["id"]) == original_log


def test_safe_result_details_survive_projection_and_registration_redaction():
    seed()
    db.claim_account_plan_check(1)
    db.update_account_plan_check(1, result={"ok": True, "current_plan_type": "plus"})
    plan = account_history("plan_check")[0]
    assert "plus" in plan["progress_message"]
    assert "plus" in tasks.read_task_log(plan["id"])
    db.claim_account_plan_check(1)
    db.update_account_plan_check(1, result={"ok": False, "error": "timeout private-at private-password"})
    failure = account_history("plan_check")[0]
    assert "超时" in failure["error_message"]
    assert "private-" not in tasks.read_task_log(failure["id"])
    job = db.create_job("outlook")
    db.update_job(job["id"], status="running", account_id=1, stage="浏览器登录",
                  progress_message="正在等待邮箱验证码 private-at https://checkout.test/private",
                  error="password=private-password totp_secret=private-totp")
    public = tasks.get_task(f"registration-{job['id']}")
    assert public["stage"] == "浏览器登录"
    assert "正在等待邮箱验证码" in public["progress_message"]
    assert "private-" not in json.dumps(public)
    assert "checkout.test" not in json.dumps(public)
    assert tasks.get_task("account-" + "9" * 5000) is None
