"""Regression tests for bounded writes and process-safe claims, using temporary SQLite only."""
import json
import multiprocessing
from contextlib import closing, contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

from core import db


@contextmanager
def _storage(root):
    """Spawned workers do not run pytest fixtures, so bind all storage explicitly."""
    root = Path(root)
    paths = {
        name: root / name for name in (
            "_ACCOUNTS_JSON", "_OUTLOOK_JSON", "_GENERIC_API_EMAIL_JSON", "_DOMAIN_EMAIL_JSON",
            "_JOBS_JSON", "_LEGACY_ACCOUNTS_JSON", "_LEGACY_OUTLOOK_JSON", "_LEGACY_JOBS_JSON",
            "_LEGACY_SQLITE", "_CODEX_DIR", "_CODEX_AGENT_DIR", "_LEGACY_CODEX_EXPORT_STATE",
            "_OUTLOOK_TXT", "_GENERIC_API_EMAIL_TXT", "_ACCOUNTS_TXT", "_TOKENS_TXT",
        )
    }
    with patch.multiple(db, **paths, _DATA_DIR=root, _LOG_DIR=root / "logs",
                        _SQLITE_PATH=root / "turb.sqlite3", _DEFAULT_SQLITE_PATH=root / "turb.sqlite3",
                        _SQLITE_READY=False, _SQLITE_READY_PATH=None):
        yield


def _worker(root, barrier, queue, operation, value):
    try:
        with _storage(root):
            db._ensure_sqlite()
            barrier.wait(timeout=15)
            if operation == "claim":
                row = getattr(db, "claim_next_" + value)()
                result = row["id"] if row else None
            elif operation == "domain":
                result = db.claim_next_domain_email(value)["id"]
            elif operation == "create":
                result = [db.create_job("outlook")["id"] for _ in range(5)]
            elif operation == "retry":
                row, created = db.create_retry_job(value, job_type="registration", email_source="outlook")
                result = (row["id"], created)
            elif operation == "update":
                # Same-record and different-record updates must both survive.
                for _ in range(5):
                    db.update_job(value + 1, progress=70 + value)
                    db.update_job(10, **({"progress": 81} if value == 0 else {"stage": "worker-1"}))
                result = True
            elif operation == "account":
                if value == 0:
                    db.update_account_plan_check(1, result={"ok": True, "current_plan_type": "plus"})
                else:
                    db.update_account_codex_status("one@example.test", "success")
                result = True
            elif operation == "plan_claim":
                result = db.claim_account_plan_check(acc_id=1)
            else:
                raise AssertionError(operation)
            queue.put(("ok", result))
    except BaseException as exc:
        queue.put(("error", repr(exc)))
        raise


def _race(root, operation, values):
    context = multiprocessing.get_context("spawn")
    barrier, queue = context.Barrier(len(values)), context.Queue()
    processes = [context.Process(target=_worker, args=(str(root), barrier, queue, operation, value)) for value in values]
    try:
        for process in processes:
            process.start()
        results = [queue.get(timeout=30) for _ in processes]
        for process in processes:
            process.join(timeout=15)
            assert process.exitcode == 0
        assert all(status == "ok" for status, _ in results), results
        return [value for _, value in results]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        queue.close()
        queue.join_thread()


def _raw(table, row_id):
    with closing(db._sqlite_conn()) as conn:
        row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()
    return dict(row) if row else None


def _assert_metadata(table, row_id):
    stored = _raw(table, row_id)
    payload = json.loads(stored["payload"])
    assert stored["email"] == str(payload.get("email") or "")
    assert stored["status"] == str(payload.get("status") or "")
    assert stored["archived"] == int(bool(payload.get("archived")))
    assert stored["updated_at"] == str(payload.get("updated_at") or "")
    assert stored["created_at"] == str(payload.get("created_at") or payload.get("imported_at") or "")
    return payload


@contextmanager
def _trace_hot_writes():
    db._ensure_sqlite()
    statements = []
    connect = db._sqlite_conn

    def traced():
        conn = connect()
        conn.set_trace_callback(statements.append)
        return conn

    with patch.object(db, "_sqlite_conn", side_effect=traced), \
         patch.object(db, "_load_collection", side_effect=AssertionError("unbounded collection read")), \
         patch.object(db, "_save_collection", side_effect=AssertionError("collection rewrite")):
        yield statements
    assert not any(sql.lstrip().upper().startswith("DELETE ") for sql in statements), statements
    assert "BEGIN IMMEDIATE" in statements


def test_job_writes_only_touch_target_and_preserve_retry_semantics(tmp_path):
    with _storage(tmp_path):
        db._save_collection("jobs", [{"id": 1, "status": "failed", "retry_attempt": 2},
                                     {"id": 2, "status": "success", "unrelated": {"keep": True}}])
        before = _raw("registration_jobs", 2)
        with _trace_hot_writes():
            job = db.create_job("imap")
            assert job["id"] == 3
            db.update_job(3, status="running", email="job@example.test", progress=150,
                          stage="working", network_traffic={"total_bytes": 11})
            db.update_job(999, status="failed")
            retry, created = db.create_retry_job(1, job_type="registration", email_source="outlook")
            assert created and retry["retry_attempt"] == 3 and retry["parent_job_id"] == 1
            same, created = db.create_retry_job(1, job_type="registration", email_source="outlook")
            assert not created and same["id"] == retry["id"]
            with pytest.raises(ValueError):
                db.create_retry_job(1, job_type="codex_retry", email_source="outlook")
            db.update_job(retry["id"], status="failed")
            child, created = db.create_retry_job(retry["id"], job_type="registration", email_source="outlook")
            assert created and child["root_job_id"] == 1 and child["retry_attempt"] == 4
        row = _assert_metadata("registration_jobs", 3)
        assert row["progress"] == 100 and row["network_traffic"] == {"total_bytes": 11}
        assert row["updated_at"]
        assert _raw("registration_jobs", 2) == before


def test_job_update_rolls_back_payload_and_metadata_on_database_error(tmp_path):
    with _storage(tmp_path):
        job = db.create_job("outlook")
        before = _raw("registration_jobs", job["id"])
        with closing(db._sqlite_conn()) as conn, conn:
            conn.execute("CREATE TRIGGER reject_progress BEFORE UPDATE ON registration_jobs "
                         "WHEN NEW.status='running' BEGIN SELECT RAISE(ABORT, 'fixture failure'); END")
        with pytest.raises(db.sqlite3.IntegrityError, match="fixture failure"):
            db.update_job(job["id"], status="running", email="changed@example.test", progress=60)
        assert _raw("registration_jobs", job["id"]) == before


def test_account_status_writes_preserve_unrelated_rows_and_fields(tmp_path):
    with _storage(tmp_path):
        db._save_collection("accounts", [
            {"id": 1, "email": "ÜSER@example.test", "group_name": "Group", "note": "keep",
             "archived": True, "plan_type": "plus", "subscription_grace_period_end_at": "old",
             "extra_json": '{"registration_password":"fixture"}'},
            {"id": 2, "email": "other@example.test", "unrelated": [1, 2]},
        ])
        before = _raw("accounts", 2)
        with _trace_hot_writes():
            assert db.claim_account_plan_check(email="üser@EXAMPLE.test")
            assert not db.claim_account_plan_check(acc_id=1)
            assert db.mark_account_plan_check_running(1)
            assert db.update_account_plan_check(1, result={"ok": False, "error": "timeout"})
            assert db.get_account(1)["plan_type"] == "plus"
            assert db.update_account_plan_check(email="üser@example.test", result={
                "ok": True, "current_plan_type": "free", "subscription_grace_period_end_at": None,
                "eligible_promo_campaigns": {"go": {"id": "offer"}},
            })
            assert db.claim_account_live_check(1)
            assert not db.claim_account_live_check(1)
            assert db.mark_account_live_check_running(1)
            assert db.update_account_liveness(1, {"ok": True, "access_token": "fresh-token", "proxy_used": None})
            assert db.update_account_codex_status("üser@example.test", "deactivated", "disabled")
            assert not db.update_account_codex_status("absent@example.test", "success")
            assert not db.mark_account_plan_check_running(999)
        row = _assert_metadata("accounts", 1)
        assert row["group_name"] == "Group" and row["note"] == "keep" and row["archived"]
        assert row["subscription_grace_period_end_at"] is None
        assert row["eligible_promo_campaigns"] == {"go": {"id": "offer"}}
        assert row["live_check_status"] == "deactivated" and row["codex_status"] == "deactivated"
        assert row["access_token"] == "fresh-token" and row["live_check_proxy_used"] is None
        assert row["copy_line"] == db._account_line(row)
        assert _raw("accounts", 2) == before


@pytest.mark.parametrize("collection,suffix", [("outlook", "outlook"), ("generic_api", "generic_api_email"),
                                                ("imap", "imap_email"), ("domain", "domain_email")])
def test_mailbox_release_and_recycling_touch_only_the_selected_row(tmp_path, collection, suffix):
    with _storage(tmp_path):
        db._save_collection(collection, [
            {"id": 1, "email": "Üser@example.test", "status": "available", "note": "old",
             "code_url": "https://example.test/code", "imap_password": "fixture", "imported_at": "2025-01-01"},
            {"id": 2, "email": "other@example.test", "status": "available", "note": "keep"},
        ])
        before = _raw("email_pool", 2)
        with _trace_hot_writes():
            if collection == "domain":
                row = db.claim_next_domain_email("üser@example.test")
                assert row["status"] == "available"
                db.release_domain_email(row["email"], "used")
            else:
                row = getattr(db, "claim_next_" + suffix)()
                assert row["status"] == "used" and row["note"] is None
            assert row["id"] == 1
            release = getattr(db, "release_" + suffix)
            recycle = getattr(db, "release_unconsumed_" + suffix)
            assert recycle("üser@example.test", "recycled")
            assert not recycle("üser@example.test")
            release("üser@example.test", "disabled", "fixture-disabled")
            assert not recycle("üser@example.test")
            release("üser@example.test", "used")
        db._save_collection("accounts", [{"id": 1, "email": "ÜSER@example.test"}])
        with _trace_hot_writes():
            assert not recycle("üser@example.test")
        stored = _assert_metadata("email_pool", 1)
        assert stored["status"] == "used" and stored["note"] == "fixture-disabled"
        assert _raw("email_pool", 1)["source"] == db._EMAIL_SOURCES[collection]
        assert _raw("email_pool", 2) == before


@pytest.mark.parametrize("collection,suffix", [("outlook", "outlook"), ("generic_api", "generic_api_email"), ("imap", "imap_email")])
def test_processes_cannot_claim_the_same_mailbox(tmp_path, collection, suffix):
    with _storage(tmp_path):
        db._save_collection(collection, [{"id": 1, "email": "one@example.test", "status": "available"}])
        outcomes = _race(tmp_path, "claim", [suffix] * 4)
        assert outcomes.count(1) == 1 and outcomes.count(None) == 3
        assert _assert_metadata("email_pool", 1)["status"] == "used"


def test_domain_registration_is_idempotent_across_processes_and_sources(tmp_path):
    with _storage(tmp_path):
        db._save_collection("outlook", [{"id": 40, "email": "other@example.test", "status": "available"}])
        before = _raw("email_pool", 40)
        outcomes = _race(tmp_path, "domain", ["Üser@example.test", "üser@example.test"] * 2)
        assert len(set(outcomes)) == 1 and outcomes[0] == 41
        with closing(db._sqlite_conn()) as conn:
            assert conn.execute("SELECT COUNT(*) FROM email_pool WHERE source='cloudflare_domain'").fetchone()[0] == 1
        assert _raw("email_pool", 40) == before


def test_concurrent_process_job_creation_and_retry_deduplication(tmp_path):
    with _storage(tmp_path):
        first = db.create_job("outlook")
        db.update_job(first["id"], status="failed")
        created = _race(tmp_path, "create", [None] * 3)
        ids = [row_id for batch in created for row_id in batch]
        assert len(ids) == len(set(ids)) == 15
        results = _race(tmp_path, "retry", [first["id"]] * 3)
        assert len({row_id for row_id, _ in results}) == 1
        assert sum(was_created for _, was_created in results) == 1


def test_process_updates_merge_fields_and_do_not_overwrite_other_records(tmp_path):
    with _storage(tmp_path):
        db._save_collection("jobs", [{"id": row_id, "status": "pending", "note": "keep"} for row_id in (1, 2, 3, 10)])
        untouched = _raw("registration_jobs", 3)
        assert all(_race(tmp_path, "update", [0, 1]))
        assert db.get_job(1)["progress"] == 70 and db.get_job(2)["progress"] == 71
        shared = db.get_job(10)
        assert shared["progress"] == 81 and shared["stage"] == "worker-1" and shared["note"] == "keep"
        assert _raw("registration_jobs", 3) == untouched
        db._save_collection("accounts", [{"id": 1, "email": "one@example.test", "note": "keep"}])
        assert all(_race(tmp_path, "account", [0, 1]))
        row = db.get_account(1)
        assert row["plan_type"] == "plus" and row["codex_status"] == "success" and row["note"] == "keep"
        claims = _race(tmp_path, "plan_claim", [None] * 3)
        assert claims.count(True) == 1 and claims.count(False) == 2
