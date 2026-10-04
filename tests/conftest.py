"""Offline test defaults: isolated storage, configuration and outbound HTTP."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile

import pytest

# Set these before pytest imports application modules during collection.
_collection_storage = tempfile.TemporaryDirectory(prefix="turb-tests-")
_original_env = {key: os.environ.get(key) for key in ("TURB_DATA_DIR", "TURB_ENV_FILE")}
os.environ["TURB_DATA_DIR"] = _collection_storage.name
os.environ["TURB_ENV_FILE"] = str(Path(_collection_storage.name) / ".env")


def pytest_unconfigure(config):
    for key, value in _original_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    _collection_storage.cleanup()


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    from config import env_loader
    from core import db

    # Preserve legacy tests that temporarily replace their JSON migration paths.
    paths = {
        "_DATA_DIR": tmp_path,
        "_LEGACY_DATA_DIR": tmp_path / "data",
        "_LOG_DIR": tmp_path / "logs",
        "_SQLITE_PATH": tmp_path / "turb.sqlite3",
        "_ACCOUNTS_JSON": tmp_path / "accounts.json",
        "_OUTLOOK_JSON": tmp_path / "outlook.json",
        "_GENERIC_API_EMAIL_JSON": tmp_path / "generic.json",
        "_DOMAIN_EMAIL_JSON": tmp_path / "domain.json",
        "_JOBS_JSON": tmp_path / "jobs.json",
        "_OUTLOOK_TXT": tmp_path / "outlook.txt",
        "_GENERIC_API_EMAIL_TXT": tmp_path / "generic.txt",
        "_ACCOUNTS_TXT": tmp_path / "accounts.txt",
        "_TOKENS_TXT": tmp_path / "tokens.txt",
        "_VIEWER_HTML": tmp_path / "viewer.html",
        "_CODEX_DIR": tmp_path / "codex_accounts",
        "_CODEX_AGENT_DIR": tmp_path / "codex_agent_accounts",
        "_LEGACY_SQLITE": tmp_path / "data" / "legacy.sqlite3",
        "_LEGACY_OUTLOOK_JSON": tmp_path / "data" / "outlook.json",
        "_LEGACY_ACCOUNTS_JSON": tmp_path / "data" / "accounts.json",
        "_LEGACY_JOBS_JSON": tmp_path / "data" / "jobs.json",
        "_LEGACY_CODEX_EXPORT_STATE": tmp_path / "codex-export.json",
    }
    for name, value in paths.items():
        monkeypatch.setattr(db, name, value)
    for name in ("SQLITE_PATH", "ACCOUNTS_JSON", "OUTLOOK_JSON", "JOBS_JSON"):
        monkeypatch.setattr(db, "_DEFAULT_" + name, paths["_" + name])
    monkeypatch.setattr(db, "_SQLITE_READY", False)
    monkeypatch.setattr(db, "_STORAGE_BOUND", False)
    monkeypatch.setattr(db, "_SQLITE_READY_PATH", None)
    monkeypatch.setenv("TURB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TURB_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setattr(env_loader, "_ENV_PATH", tmp_path / ".env")

    from core import account_liveness, codex_retry_service, email_change_service, operation_log, twofa_service
    for module in (account_liveness, codex_retry_service, email_change_service, operation_log, twofa_service):
        monkeypatch.setattr(module, "_LOG_DIR", tmp_path / "logs")

    def no_live_http(*args, **kwargs):
        raise AssertionError("Tests must mock outbound HTTP; real providers are disabled")

    import requests
    import curl_cffi.requests
    monkeypatch.setattr(requests.sessions.Session, "request", no_live_http)
    monkeypatch.setattr(curl_cffi.requests.Session, "request", no_live_http)

    # Delivery tests may explicitly replace this with their own enqueue mock.
    # Unrelated HTTP tests must not launch a background real-provider check.
    from core import plan_check_service
    enqueue = plan_check_service.enqueue_account_plan_check

    def enqueue_offline(*args, **kwargs):
        if kwargs.get("trigger") == "redeem_after_ship":
            return False
        return enqueue(*args, **kwargs)

    monkeypatch.setattr(plan_check_service, "enqueue_account_plan_check", enqueue_offline)
    yield tmp_path
