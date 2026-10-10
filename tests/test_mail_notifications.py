"""Offline SMTP transport, durable queue and upload notification regressions."""
from contextlib import closing
import json
import smtplib
from unittest.mock import MagicMock

import pytest

from config import notifications as settings
from core import db, mail_notifications as mail
from webui.app import create_app


@pytest.fixture
def enabled(monkeypatch):
    for key, value in {
        "SMTP_ENABLED": True, "SMTP_NOTIFY_TASKS": True, "SMTP_NOTIFY_UPLOADS": True,
        "SMTP_HOST": "smtp.example.test", "SMTP_PORT": 465, "SMTP_SECURITY": "ssl",
        "SMTP_USERNAME": "sender@example.test", "SMTP_PASSWORD": "private-smtp-password",
        "SMTP_FROM": "", "SMTP_ADMIN_EMAILS": "admin@example.test; other@example.test",
        "SMTP_TIMEOUT": 15,
    }.items():
        monkeypatch.setattr(settings, key, value)


def rows():
    with closing(db._sqlite_conn()) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='mail_notifications'").fetchone():
            return []
        return [dict(row) for row in conn.execute("SELECT * FROM mail_notifications ORDER BY id")]


def enqueue(key="test:event"):
    db._ensure_sqlite()
    with closing(db._sqlite_conn()) as conn, conn:
        mail.enqueue_notification(conn, event_key=key, category="task", title="任务完成",
                                  fields={"状态": "成功"}, emails=["account@example.test"])


def test_message_is_html_and_plain_with_escaped_values():
    message = mail.build_message(
        {"title": "账号上传 <script>", "fields": {"<label>": "<img src=x onerror=alert(1)>"},
         "emails": ["a&b@example.test"]}, sender="sender@example.test",
        recipients=["admin@example.test"], message_id="<fixed@example.test>",
    )
    assert message.get_content_type() == "multipart/alternative"
    html = message.get_body(preferencelist=("html",)).get_content()
    assert "&lt;script&gt;" in html and "<script>" not in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html
    assert "a&amp;b@example.test" in html
    assert "a&b@example.test" in message.get_body(preferencelist=("plain",)).get_content()
    assert message["Message-ID"] == "<fixed@example.test>"


@pytest.mark.parametrize("security,port", [("ssl", 465), ("starttls", 587), ("plain", 25)])
def test_smtp_modes_authentication_and_recipients(enabled, monkeypatch, security, port):
    monkeypatch.setattr(settings, "SMTP_SECURITY", security)
    monkeypatch.setattr(settings, "SMTP_PORT", port)
    smtp_factory, ssl_factory = MagicMock(), MagicMock()
    monkeypatch.setattr(mail.smtplib, "SMTP", smtp_factory)
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", ssl_factory)
    factory = ssl_factory if security == "ssl" else smtp_factory
    client = factory.return_value.__enter__.return_value
    client.send_message.return_value = {}
    mail.send_notification({"title": "任务完成", "emails": ["user@example.test"]}, "<event@example.test>")
    assert factory.call_args.kwargs["port"] == port
    assert factory.call_args.kwargs["timeout"] == 15
    client.login.assert_called_once_with("sender@example.test", "private-smtp-password")
    assert client.starttls.call_count == (1 if security == "starttls" else 0)
    if security == "starttls":
        calls = [call[0] for call in client.mock_calls]
        assert calls.index("starttls") < calls.index("login")
    sent = client.send_message.call_args
    assert sent.kwargs["to_addrs"] == ["admin@example.test", "other@example.test"]
    assert sent.kwargs["from_addr"] == "sender@example.test"
    assert "user@example.test" in sent.args[0].get_body(preferencelist=("html",)).get_content()
    assert "private-smtp-password" not in sent.args[0].as_string()


def test_anonymous_relay_and_explicit_sender(enabled, monkeypatch):
    monkeypatch.setattr(settings, "SMTP_USERNAME", "")
    monkeypatch.setattr(settings, "SMTP_FROM", "notify@example.test")
    factory = MagicMock()
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", factory)
    client = factory.return_value.__enter__.return_value
    client.send_message.return_value = {}
    mail.send_notification({"title": "收到上传"}, "<event@example.test>")
    client.login.assert_not_called()
    assert client.send_message.call_args.kwargs["from_addr"] == "notify@example.test"


def test_outbox_is_transactional_unique_and_does_not_send_inline(enabled, monkeypatch):
    send = MagicMock()
    monkeypatch.setattr(mail, "send_notification", send)
    db._ensure_sqlite()
    with closing(db._sqlite_conn()) as conn:
        conn.execute("BEGIN")
        mail.enqueue_notification(conn, event_key="rolled-back", category="task", title="任务完成", fields={}, emails=[])
        conn.rollback()
    assert rows() == []
    enqueue()
    enqueue()
    assert len(rows()) == 1
    send.assert_not_called()
    assert mail.process_once()
    assert rows()[0]["status"] == "sent"
    assert rows()[0]["payload"] == "{}"
    assert not mail.process_once()
    send.assert_called_once()
    enqueue()
    assert len(rows()) == 1


def test_failed_smtp_retries_without_exposing_provider_error(enabled, monkeypatch, caplog):
    clock = [1000.0]
    monkeypatch.setattr(mail.time, "time", lambda: clock[0])
    send = MagicMock(side_effect=smtplib.SMTPAuthenticationError(535, b"private-smtp-password private-token"))
    monkeypatch.setattr(mail, "send_notification", send)
    enqueue()
    message_id = rows()[0]["message_id"]
    for attempt in range(1, mail.MAX_ATTEMPTS + 1):
        assert mail.process_once()
        event = rows()[0]
        assert event["attempts"] == attempt
        assert event["last_error"] == "SMTPAuthenticationError"
        assert not mail.process_once()
        clock[0] = event["next_attempt"]
    assert event["status"] == "failed"
    assert "private-smtp-password" not in caplog.text
    assert "private-token" not in caplog.text
    assert all(call.args[1] == message_id for call in send.call_args_list)


def test_pending_event_survives_connection_restart_and_disabled_switch(enabled, monkeypatch):
    enqueue()
    monkeypatch.setattr(settings, "SMTP_NOTIFY_TASKS", False)
    assert not mail.process_once()
    enqueue("disabled-event")
    assert len(rows()) == 1
    monkeypatch.setattr(db, "_SQLITE_READY", False)
    monkeypatch.setattr(settings, "SMTP_NOTIFY_TASKS", True)
    send = MagicMock()
    monkeypatch.setattr(mail, "send_notification", send)
    assert mail.process_once()
    send.assert_called_once()


@pytest.mark.parametrize("public", [True, False])
def test_upload_sends_only_inserted_emails_and_no_credentials(enabled, monkeypatch, public):
    monkeypatch.setenv("PUBLIC_UPLOAD_KEY", "upload-test-key")
    monkeypatch.setattr("core.account_import.fetch_account_user_name", lambda *a, **kw: {"ok": True, "user_name": "User"})
    client = create_app(auth_code="admin-test-key").test_client()
    url = "/api/public/accounts/import" if public else "/api/accounts/import"
    headers = {"X-Upload-Key": "upload-test-key"} if public else {"X-Auth-Code": "admin-test-key"}
    line = "first@example.test--private-password--private-totp--private-token"
    response = client.post(url, json={"text": line}, headers=headers)
    assert response.status_code == 200 and response.json["inserted"] == 1
    assert len(rows()) == 1
    response = client.post(url, json={"text": line + "\nsecond@example.test--password--totp--token\nbad-line"}, headers=headers)
    assert response.json["inserted"] == 1
    assert len(rows()) == 2
    payload = json.loads(rows()[-1]["payload"])
    assert payload["emails"] == ["second@example.test"]
    assert payload["fields"]["跳过行数"] == "2"
    assert payload["fields"]["上传来源"] == ("公共上传" if public else "管理员导入")
    all_payloads = " ".join(row["payload"] for row in rows())
    for secret in ("private-password", "private-totp", "private-token"):
        assert secret not in all_payloads
    assert client.post(url, json={"text": line}, headers=headers).json["inserted"] == 0
    assert len(rows()) == 2
    assert client.post(url, json={"text": "bad-line"}, headers=headers).status_code == 400
    assert len(rows()) == 2


def test_partial_refusal_retries_only_remaining_recipient(enabled, monkeypatch):
    factory = MagicMock()
    monkeypatch.setattr(mail.smtplib, "SMTP_SSL", factory)
    client = factory.return_value.__enter__.return_value
    client.send_message.side_effect = [{"other@example.test": (450, b"try later")}, {}]
    enqueue()
    assert mail.process_once()
    event = rows()[0]
    assert json.loads(event["payload"])["_remaining_recipients"] == "other@example.test"
    with closing(db._sqlite_conn()) as conn, conn:
        conn.execute("UPDATE mail_notifications SET next_attempt=0")
    assert mail.process_once()
    assert rows()[0]["status"] == "sent"
    assert client.send_message.call_args_list[0].kwargs["to_addrs"] == ["admin@example.test", "other@example.test"]
    assert client.send_message.call_args_list[1].kwargs["to_addrs"] == ["other@example.test"]


def test_worker_starts_once_and_stops_with_database_owner(monkeypatch):
    from core.runtime import acquire_runtime_owner, recover_startup

    monkeypatch.setattr(settings, "SMTP_ENABLED", False)
    with acquire_runtime_owner() as owner:
        recover_startup(owner)
        worker = owner.mail_worker
        assert worker.thread.is_alive()
        assert recover_startup(owner) == {}
        assert owner.mail_worker is worker
    assert worker.stop_event.is_set()
    assert not worker.thread.is_alive()


def test_unauthorized_upload_never_enqueues(enabled, monkeypatch):
    monkeypatch.setenv("PUBLIC_UPLOAD_KEY", "upload-test-key")
    client = create_app(auth_code="admin-test-key").test_client()
    response = client.post("/api/public/accounts/import", json={"text": "user@example.test--pw--totp--token"})
    assert response.status_code == 401
    assert rows() == []


def test_upload_queue_failure_does_not_fail_import(enabled, monkeypatch):
    monkeypatch.setenv("PUBLIC_UPLOAD_KEY", "upload-test-key")
    monkeypatch.setattr("core.account_import.fetch_account_user_name", lambda *a, **kw: {"ok": True, "user_name": "User"})
    monkeypatch.setattr(mail, "enqueue_notification", MagicMock(side_effect=RuntimeError("disk unavailable")))
    client = create_app(auth_code="admin-test-key").test_client()
    response = client.post("/api/public/accounts/import", headers={"X-Upload-Key": "upload-test-key"},
                           json={"text": "user@example.test--pw--totp--token"})
    assert response.status_code == 200
    assert response.json["inserted"] == 1
