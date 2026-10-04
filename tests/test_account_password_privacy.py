"""Account lists omit credentials; explicit secret reads remain authorized and uncached."""
import pytest

from core import db
from webui.app import create_app

PASSWORD = '<img src=x onerror="password-test">&private-password'
ACCESS_TOKEN = "private-account-access-token"
TOTP_SECRET = "JBSWY3DPEHPK3PXP"
EMAIL = "password-test@example.test"
AUTH = {"X-Auth-Code": "password-test-auth"}


@pytest.fixture
def account_client(isolated_runtime):
    # Assert isolation before creating an application or writing any fixtures.
    assert db._active_sqlite_path().parent == isolated_runtime
    account_id = db.insert_account(
        email=EMAIL, access_token=ACCESS_TOKEN, totp_secret=TOTP_SECRET,
        extra={"registration_password": PASSWORD},
    )
    empty_id = db.insert_account(email="no-password@example.test", access_token="")
    app = create_app(auth_code=AUTH["X-Auth-Code"])
    app.config["TESTING"] = True
    return app.test_client(), account_id, empty_id


def assert_no_store(response):
    assert response.cache_control.no_store
    assert response.headers["Pragma"] == "no-cache"


@pytest.mark.parametrize("query", ["", "?paged=1&page_size=50"])
def test_account_lists_only_expose_password_presence(account_client, query):
    client, account_id, empty_id = account_client
    response = client.get("/api/accounts" + query, headers=AUTH)
    assert response.status_code == 200
    data = response.get_json()
    rows = data["items"] if isinstance(data, dict) else data
    by_id = {row["id"]: row for row in rows}
    assert by_id[account_id]["has_password"] is True
    assert by_id[empty_id]["has_password"] is False
    forbidden = {"password", "registration_password", "access_token", "totp_secret", "extra_json", "copy_line"}
    assert all(not forbidden.intersection(row) for row in rows)
    body = response.get_data(as_text=True)
    for secret in (PASSWORD, ACCESS_TOKEN, TOTP_SECRET):
        assert secret not in body
    assert_no_store(response)


@pytest.mark.parametrize("field,expected", [
    ("password", PASSWORD),
    ("access_token", ACCESS_TOKEN),
    ("totp_secret", TOTP_SECRET),
    ("login_credentials", f"{EMAIL}---{PASSWORD}---{TOTP_SECRET}"),
])
def test_explicit_secret_read_requires_auth_and_cannot_be_cached(account_client, field, expected):
    client, account_id, _ = account_client
    url = f"/api/accounts/{account_id}/secret?field={field}"
    unauthorized = client.get(url)
    assert unauthorized.status_code == 401
    assert expected not in unauthorized.get_data(as_text=True)
    assert_no_store(unauthorized)
    response = client.get(url, headers=AUTH)
    assert response.status_code == 200
    assert response.get_json()["value"] == expected
    assert_no_store(response)


def test_bulk_password_read_is_explicit_authorized_and_uncached(account_client):
    client, account_id, _ = account_client
    payload = {"account_ids": [account_id, account_id], "field": "password"}
    unauthorized = client.post("/api/accounts/secret-bulk", json=payload)
    assert unauthorized.status_code == 401
    assert_no_store(unauthorized)
    response = client.post("/api/accounts/secret-bulk", json=payload, headers=AUTH)
    assert response.status_code == 200
    assert response.get_json()["values"] == [{"id": account_id, "email": EMAIL, "value": PASSWORD}]
    assert_no_store(response)


@pytest.mark.parametrize("method,url,body,status", [
    ("GET", "/api/accounts/999999/secret?field=password", None, 404),
    ("GET", "/api/accounts/{id}/secret?field=unsupported", None, 400),
    ("POST", "/api/accounts/secret-bulk", {"account_ids": [], "field": "password"}, 400),
])
def test_secret_error_responses_are_uncached(account_client, method, url, body, status):
    client, account_id, _ = account_client
    response = client.open(url.format(id=account_id), method=method, json=body, headers=AUTH)
    assert response.status_code == status
    assert_no_store(response)
    assert PASSWORD not in response.get_data(as_text=True)


def test_api_cache_policy_preserves_versioned_asset_caching(account_client):
    client, _, _ = account_client
    with client.application.test_request_context():
        asset_url = client.application.jinja_env.globals["ui_asset_url"]("console.js")
    asset = client.get(asset_url, headers=AUTH)
    assert asset.status_code == 200
    assert asset.cache_control.immutable
    assert not asset.cache_control.no_store
    assert_no_store(client.get("/api/does-not-exist", headers=AUTH))
