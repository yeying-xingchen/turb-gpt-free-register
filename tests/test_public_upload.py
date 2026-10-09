"""Public upload requires a separate key, including for logged-in admins."""
import pytest

from core import db
from webui.app import create_app

URL = "/api/public/accounts/import"
LINE = "upload@example.test--password--JBSWY3DPEHPK3PXP--test-token"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("PUBLIC_UPLOAD_KEY", "upload-test-key")
    monkeypatch.setattr("core.account_import.fetch_account_user_name", lambda *a, **kw: {"ok": True, "user_name": "Upload"})
    return create_app(auth_code="admin-test-key").test_client()


def test_public_page_and_key_separation(client, tmp_path):
    (tmp_path / "index.html").write_text("upload frontend", encoding="utf-8")
    client.application.config["NUXT_DIST_DIR"] = tmp_path
    assert client.get("/upload").status_code == 200
    assert client.get("/upload/").status_code == 200
    assert client.get("/api/accounts", headers={"X-Auth-Code": "upload-test-key"}).status_code == 401
    assert client.post(URL, json={"text": LINE}).status_code == 401
    assert client.post(URL, json={"text": LINE}, headers={"X-Upload-Key": "wrong"}).status_code == 401
    client.post("/api/auth/login", json={"auth_code": "admin-test-key"})
    assert client.post(URL, json={"text": LINE}).status_code == 401
    assert db.get_account_by_email("upload@example.test") is None


def test_disabled_when_unconfigured(client, monkeypatch):
    monkeypatch.setenv("PUBLIC_UPLOAD_KEY", "")
    assert client.post(URL, json={"text": LINE}, headers={"X-Upload-Key": "upload-test-key"}).status_code == 503


def test_upload_and_duplicates(client):
    headers = {"X-Upload-Key": "upload-test-key"}
    response = client.post(URL, json={"text": LINE + "\nbad-line"}, headers=headers)
    assert response.status_code == 200
    assert response.json["inserted"] == 1
    assert response.json["skipped"] == 1
    assert "no-store" in response.headers["Cache-Control"]
    assert "test-token" not in response.get_data(as_text=True)
    row = db.get_account_by_email("upload@example.test")
    assert row["access_token"] == "test-token"
    again = client.post(URL, json={"text": LINE}, headers=headers)
    assert again.json["inserted"] == 0
    assert again.json["skipped"] == 1


@pytest.mark.parametrize("payload", [[], "text", {}, {"text": 42}, {"text": "bad-line"}])
def test_invalid_payload(client, payload):
    assert client.post(URL, json=payload, headers={"X-Upload-Key": "upload-test-key"}).status_code == 400


def test_key_not_accepted_in_body_or_query(client):
    response = client.post(URL + "?key=upload-test-key", json={"key": "upload-test-key", "text": LINE})
    assert response.status_code == 401


def test_oversized_text(client):
    response = client.post(URL, json={"text": "x" * (10 * 1024 * 1024 + 1)}, headers={"X-Upload-Key": "upload-test-key"})
    assert response.status_code == 400


def test_redeem_route_still_registered(client):
    assert client.post("/api/redeem", json={}).status_code == 400
