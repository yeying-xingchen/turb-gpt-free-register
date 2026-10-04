"""Regression coverage for construction, ownership and orphaned-task recovery."""
from pathlib import Path
import subprocess
import sys

import pytest

from core import db
from core.runtime import DatabaseOwner, acquire_runtime_owner, recover_startup
from webui.app import create_app


def test_application_construction_does_not_open_database():
    database = db._active_sqlite_path()
    assert not database.exists()
    create_app(auth_code="test-secret")
    create_app(auth_code="test-secret")
    assert not database.exists()
    assert db._SQLITE_READY is False


def test_application_can_bind_storage_before_first_database_use(tmp_path, monkeypatch):
    from core import account_liveness, codex_retry_service, email_change_service, twofa_service
    modules = (account_liveness, codex_retry_service, email_change_service, twofa_service)
    for module in modules:
        monkeypatch.setattr(module, "_LOG_DIR", None)
    target = tmp_path / "instance"
    create_app(auth_code="test-secret", data_dir=target)
    assert db._active_sqlite_path() == target / "turb.sqlite3"
    assert not db._active_sqlite_path().exists()
    account_id = db.insert_account(email="isolated@example.test", access_token="fixture-token")
    assert db.get_account(account_id)["email"] == "isolated@example.test"
    assert db._active_sqlite_path().exists()
    assert all(module.log_path("1").is_relative_to(target) for module in modules)
    with pytest.raises(RuntimeError, match="不能"):
        create_app(auth_code="test-secret", data_dir=tmp_path / "another")


def test_missing_explicit_env_file_does_not_search_parent_directories(tmp_path, monkeypatch):
    from config import env_loader
    from unittest.mock import patch
    monkeypatch.setattr(env_loader, "_ENV_PATH", tmp_path / "missing.env")
    with patch("dotenv.load_dotenv") as load:
        env_loader.load_env()
    load.assert_not_called()


def test_second_application_does_not_recover_live_tasks():
    account_id = db.insert_account(email="live@example.test", access_token="fixture-token")
    db.claim_account_plan_check(account_id, trigger="test")
    job = db.create_job("outlook")
    db.update_job(job["id"], status="running")
    create_app(auth_code="test-secret")
    assert db.get_job(job["id"])["status"] == "running"
    assert db.get_account(account_id)["plan_check_status"] == "queued"


def test_database_lock_is_independent_of_web_port():
    with acquire_runtime_owner():
        with pytest.raises(RuntimeError, match="已有"):
            acquire_runtime_owner()
    with acquire_runtime_owner() as owner:
        owner.assert_current()


def test_database_lock_excludes_another_process():
    script = """
import sys
from pathlib import Path
from core.runtime import DatabaseOwner
try:
    owner = DatabaseOwner(Path(sys.argv[1]))
except RuntimeError:
    print('busy')
else:
    owner.close()
    print('acquired')
"""
    with acquire_runtime_owner():
        result = subprocess.run(
            [sys.executable, "-c", script, str(db._active_sqlite_path())],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, timeout=15, check=True,
        )
        assert result.stdout.strip() == "busy"


def test_recovery_requires_matching_current_owner(tmp_path):
    with DatabaseOwner(tmp_path / "other.sqlite3") as other:
        with pytest.raises(RuntimeError, match="不匹配"):
            recover_startup(other)
    owner = acquire_runtime_owner()
    owner.close()
    with pytest.raises(RuntimeError, match="当前进程"):
        recover_startup(owner)


def test_startup_recovery_marks_orphans_once_and_preserves_completed_tasks():
    jobs = []
    for state in ("pending", "running", "paused", "stopping", "success", "cancelled"):
        job = db.create_job("outlook")
        db.update_job(job["id"], status=state)
        jobs.append(job["id"])
    with acquire_runtime_owner() as owner:
        assert recover_startup(owner)["registration"] == 4
        assert [db.get_job(j)["status"] for j in jobs] == ["failed"] * 4 + ["success", "cancelled"]
        new_job = db.create_job("outlook")
        assert recover_startup(owner) == {}
        assert db.get_job(new_job["id"])["status"] == "pending"


def test_recovered_job_with_account_only_retries_authorization():
    from core.registration_service import get_retry_info
    account_id = db.insert_account(email="saved@example.test", access_token="fixture-token", codex_status="retrying")
    job = db.create_job("outlook")
    db.update_job(job["id"], email="saved@example.test", account_id=account_id, status="running")
    with acquire_runtime_owner() as owner:
        recover_startup(owner)
    info = get_retry_info(db.get_job(job["id"]))
    assert info["retryable"] is True
    assert info["retry_action"] == "codex"
    assert db.get_account(account_id)["codex_status"] == "failed"
