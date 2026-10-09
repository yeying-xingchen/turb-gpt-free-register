from unittest.mock import patch

import pytest

from core import account_import, db, live_check_service
from webui.app import create_app


@pytest.mark.parametrize("sep", ["--", "---", "----"])
@pytest.mark.parametrize("trailing", [False, True])
def test_optional_token_parser(sep, trailing):
    line = sep.join(["new@example.com", "password", "JBSW Y3DP"])
    records, errors = account_import.parse_existing_account_text(line + (sep if trailing else ""))
    assert not errors
    assert records == [{"email": "new@example.com", "password": "password", "totp_secret": "JBSWY3DP", "access_token": ""}]


@pytest.mark.parametrize("line", ["a@example.com--pw", "a@example.com--pw-- ", "invalid--pw--ABC"])
def test_required_fields_still_validated(line):
    records, errors = account_import.parse_existing_account_text(line)
    assert not records
    assert errors


@pytest.mark.parametrize("public", [False, True])
def test_import_queues_only_new_tokenless_accounts(public, monkeypatch):
    client = create_app(auth_code="test-auth").test_client()
    from config import env_loader
    original_env_str = env_loader.env_str
    monkeypatch.setattr(env_loader, "env_str", lambda key, default="": "upload-test-key" if key == "PUBLIC_UPLOAD_KEY" else original_env_str(key, default))
    rows = [{"id": 1, "email": "existing@example.com"}]
    lookup = lambda email: next((row for row in rows if row["email"].casefold() == email.casefold()), None)
    with patch.object(db, "_load_accounts", return_value=rows), \
         patch.object(db, "_save_accounts"), \
         patch.object(db, "get_account_by_email", side_effect=lookup), \
         patch.object(account_import, "fetch_account_user_name", return_value={"ok": True, "user_name": "Alice"}) as fetch, \
         patch.object(live_check_service, "enqueue_account_live_check", return_value={"accepted": True}) as enqueue:
        response = client.post("/api/public/accounts/import" if public else "/api/accounts/import", headers={"X-Auth-Code": "test-auth", "X-Upload-Key": "upload-test-key"}, json={"text": "\n".join([
            "new@example.com--password--ABC",
            "token@example.com--password--ABC--token-secret",
            "EXISTING@example.com--password--ABC",
            "NEW@example.com--password--ABC",
        ])})
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert (body["inserted"], body["skipped"], body["live_checks_queued"]) == (2, 2, 1)
    assert body["live_check_warnings"] == []
    assert body["user_name_warnings"] == []
    enqueue.assert_called_once_with(account_id=2, email="new@example.com", trigger="import")
    fetch.assert_called_once_with("token-secret", email="token@example.com")
    assert rows[1]["access_token"] == ""
    assert "registration_password" in rows[1]["extra_json"]
    assert "token-secret" not in response.get_data(as_text=True)


@pytest.mark.parametrize("queue_result", [{"accepted": False, "queue_full": True}, RuntimeError("private-secret")])
def test_enqueue_failure_preserves_import_and_reports_safe_warning(queue_result):
    client = create_app(auth_code="test-auth").test_client()
    rows = []
    with patch.object(db, "_load_accounts", return_value=rows), \
         patch.object(db, "_save_accounts"), \
         patch.object(db, "get_account_by_email", side_effect=lambda email: rows[0] if rows else None), \
         patch.object(account_import, "fetch_account_user_name") as fetch, \
         patch.object(live_check_service, "enqueue_account_live_check", **({"side_effect": queue_result} if isinstance(queue_result, Exception) else {"return_value": queue_result})):
        response = client.post("/api/accounts/import", headers={"X-Auth-Code": "test-auth"}, json={"text": "new@example.com--password--ABC"})
    body = response.get_json()
    assert response.status_code == 200, response.get_json()
    assert body["inserted"] == 1
    assert body["live_checks_queued"] == 0
    assert len(body["live_check_warnings"]) == 1
    assert body["user_name_warnings"] == []
    assert "private-secret" not in response.get_data(as_text=True)
    fetch.assert_not_called()
