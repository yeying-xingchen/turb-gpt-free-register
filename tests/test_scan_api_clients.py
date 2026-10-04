"""Mock HTTP contracts for payment providers; never creates a real paid task."""
import json
from unittest.mock import MagicMock

import pytest
import requests

from core.scan_api_client import ScanApiClient, MasiClient, ScanApiError, validate_payment_link
from core import orderhub_client

CDK = "PAYMENT-secret-123456"
AT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature"
LINK = "https://payments.stripe.com/upi/instructions/test"
KEY = "submit-key-12345"


@pytest.fixture
def http(monkeypatch):
    session = MagicMock()
    session.__enter__.return_value = session
    session.cookies = requests.cookies.RequestsCookieJar()
    response = MagicMock(status_code=201, headers={})
    response.json.return_value = {"data": {"task": {"id": "task-1", "status": "queued"}}}
    session.request.return_value = response
    session.post.return_value = response
    monkeypatch.setattr(requests, "Session", lambda: session)
    monkeypatch.setattr(orderhub_client.cfg, "SCAN_API_BASE", "https://scan-qr.hixinghai.com/api/v1")
    monkeypatch.setattr(orderhub_client.cfg, "MASI_API_BASE", "https://masi.cc.cd")
    monkeypatch.setattr(orderhub_client.cfg, "ORDERHUB_API_BASE", "https://upi.xxsyun.xyz/api/v1")
    yield session, response
    with orderhub_client._SESSION_LOCK:
        orderhub_client._SESSIONS.clear()


def submit(client=None, **changes):
    client = client or ScanApiClient()
    args = dict(cdk=CDK, link=LINK, idempotency_key=KEY, email="a@example.test")
    if isinstance(client, (MasiClient, orderhub_client.OrderHubClient)):
        args["access_token"] = AT
    args.update(changes)
    return client.submit_upi(**args)


@pytest.mark.parametrize("value", ["http://payments.stripe.com/x", "https://payments.stripe.com.evil.test/x", "https://u:p@payments.stripe.com/x", "https://payments.stripe.com:123/x", "https://payments.stripe.com\n/x", None])
def test_link_rejects_unsafe_origin(value):
    with pytest.raises(ValueError):
        validate_payment_link(value)


def test_astra_request_omits_at_and_keeps_full_cdk(http):
    session, _ = http
    result = submit(cdk=CDK * 30)
    call = session.request.call_args
    assert call.args == ("POST", "https://scan-qr.hixinghai.com/api/v1/scan-requests")
    assert call.kwargs["json"] == {"channel": "upi", "inputType": "LINK", "link": LINK, "email": "a@example.test"}
    assert call.kwargs["headers"]["X-CDK-Code"] == CDK * 30
    assert call.kwargs["headers"]["Idempotency-Key"] == KEY
    assert call.kwargs["allow_redirects"] is False
    assert result["task_id"] == "task-1"


@pytest.mark.parametrize("key", [None, "tiny", "x" * 129, "key/12345678"])
def test_astra_requires_key_before_http(http, key):
    with pytest.raises(ValueError):
        submit(idempotency_key=key)
    http[0].request.assert_not_called()


def test_masi_full_at_and_no_undocumented_idempotency(http):
    session, response = http
    response.json.return_value = {"ok": True, "order": {"order_id": "order-1", "status": "awaiting_worker"}, "order_secret": "secret-value"}
    full = AT * 20
    result = submit(MasiClient(), access_token=full)
    kwargs = session.request.call_args.kwargs
    assert kwargs["headers"]["X-CDK"] == CDK
    assert "Idempotency-Key" not in kwargs["headers"]
    assert kwargs["json"]["access_token"] == full
    assert kwargs["json"]["dispatch_mode"] == "capacity_priority"
    assert not result["duplicate"]
    response.status_code = 200
    assert submit(MasiClient())["duplicate"]
    assert "secret-value" not in json.dumps(result)


@pytest.mark.parametrize("has_task", [False, True])
def test_202_is_uncertain_even_with_ok_false(http, has_task):
    response = http[1]
    response.status_code = 202
    response.json.return_value = {"ok": False, "requestId": "envelope-only", "data": {"task": {"id": "task-202", "status": "queued"}} if has_task else {}}
    result = submit()
    assert result["uncertain"] and not result["duplicate"]
    assert result["task_id"] == ("task-202" if has_task else None)
    assert result["request_id"] == "envelope-only"


@pytest.mark.parametrize("payload", [{"data": {"requestId": "task-1"}}, {"data": {"task": {"id": "other-id", "status": "queued"}}}])
def test_query_rejects_wrong_or_missing_task_id(http, payload):
    http[1].status_code = 200
    http[1].json.return_value = payload
    with pytest.raises(ScanApiError, match="ID"):
        ScanApiClient().get_task(cdk=CDK, task_id="task-1")


@pytest.mark.parametrize("status", [307, 500, 503])
def test_uncertain_http_never_retries_or_redirects(http, status):
    session, response = http
    response.status_code = status
    response.json.return_value = {"error": {"code": "BUSY", "message": CDK + AT, "retryable": True}}
    with pytest.raises(ScanApiError) as raised:
        submit(MasiClient())
    assert raised.value.uncertain
    assert CDK not in str(raised.value) and AT not in str(raised.value)
    session.request.assert_called_once()
    assert session.request.call_args.kwargs["allow_redirects"] is False


def test_nested_quota_error_metadata_and_unknown_code(http):
    http[1].status_code = 429
    http[1].json.return_value = {"error": {"code": "FUTURE_QUOTA_ERROR", "message": "full", "created": False, "charged": False, "retry_after": 42, "retryable": True}}
    with pytest.raises(ScanApiError) as raised:
        submit()
    error = raised.value
    assert not error.uncertain and error.created is False and error.charged is False
    assert error.retry_after_seconds == 42 and error.retryable
    assert error.code == "FUTURE_QUOTA_ERROR"


def test_transport_redacts_and_legacy_completion_is_settlement(http):
    session, response = http
    session.request.side_effect = requests.Timeout(CDK + AT)
    with pytest.raises(ScanApiError) as raised:
        submit()
    assert raised.value.uncertain and CDK not in str(raised.value)
    session.request.side_effect = None
    response.status_code = 200
    response.json.return_value = {"data": {"task": {"id": "task-1", "status": "succeeded", "verificationPolicy": "legacy", "message": "Plus"}}}
    result = ScanApiClient().get_task(cdk=CDK, task_id="task-1")
    assert result["task"]["message"] == "已按原有 AT 变化规则完成结算"


def test_verification_quota_does_not_return_credential(http):
    response = http[1]
    response.status_code = 200
    response.json.return_value = {"ok": True, "ticket": {"available_uses": 3, "pending_uses": 1, "cdk": CDK}}
    assert MasiClient().verify_cdk(CDK)["data"] == {"available_uses": 3, "pending_uses": 1}


@pytest.mark.parametrize("group", ["created", "duplicated", "failed"])
def test_orderhub_http200_checks_every_array(http, group):
    session, response = http
    response.status_code = 200
    data = {"ok": True, "created": [], "duplicated": [], "failed": []}
    data[group] = [{"order": {"id": "order-1", "status": "awaiting_worker"}}] if group != "failed" else [{"index": 0, "error": {"code": "ACCOUNT_MISMATCH", "message": "Mismatch"}}]
    response.json.return_value = data
    if group == "failed":
        with pytest.raises(ScanApiError) as raised:
            submit(orderhub_client.OrderHubClient())
        assert raised.value.code == "ACCOUNT_MISMATCH" and not raised.value.uncertain
    else:
        result = submit(orderhub_client.OrderHubClient())
        assert result["duplicate"] == (group == "duplicated")
        assert result["task_id"] == "order-1"
    kwargs = session.request.call_args.kwargs
    assert kwargs["headers"]["Authorization"] == "Bearer " + CDK
    assert kwargs["headers"]["Idempotency-Key"] == KEY
    assert kwargs["json"] == {"items": [{"at": AT, "link": LINK}], "dispatch_mode": "auto"}
    assert not kwargs["allow_redirects"]


def test_orderhub_login_keeps_cookie_server_side(http):
    session, response = http
    session.cookies.set("token", "upstream-private-cookie", domain="upi.xxsyun.xyz", path="/")
    response.status_code = 200
    response.json.return_value = {"ok": True, "user": {"id": 7, "username": "00123456", "employer_status": "active", "token": "upstream-private-cookie"}}
    handle, user = orderhub_client.login("00123456", "ExamplePass1!")
    saved = orderhub_client.get_session(handle)
    assert saved["identity"] == "orderhub-user:7"
    assert "token" not in user and "upstream-private-cookie" not in handle
    assert session.post.call_args.kwargs["json"]["username"] == "00123456"
    response.json.return_value = {"ok": True, "order": {"id": "order-1", "status": "verifying", "payment_window_ends_at": 1, "payment_expiry_kind": "dynamic"}}
    client = orderhub_client.OrderHubClient()
    client.session_auth = saved
    task = client.get_task(cdk=saved["identity"], task_id="order-1")["task"]
    assert "Authorization" not in session.request.call_args.kwargs["headers"]
    assert task["status"] == "verifying" and task["paymentWindowEndsAt"] == 1
    orderhub_client.logout(handle)
    with pytest.raises(ValueError):
        orderhub_client.get_session(handle)


@pytest.mark.parametrize("username,password", [("123", "ExamplePass1!"), ("abcdefghi", "ExamplePass1!"), ("00123456", "lowercase1!"), ("00123456", "ExamplePass1")])
def test_orderhub_login_validation_before_http(http, username, password):
    with pytest.raises(ValueError):
        orderhub_client.login(username, password)
    http[0].post.assert_not_called()


@pytest.mark.parametrize("key", [None, "", "short"])
def test_orderhub_requires_idempotency_before_http(http, key):
    with pytest.raises(ValueError):
        submit(orderhub_client.OrderHubClient(), idempotency_key=key)
    http[0].request.assert_not_called()


def test_orderhub_never_returns_secret_ids_or_statuses(http):
    response = http[1]
    response.status_code = 200
    response.json.return_value = {"ok": True, "created": [{"order": {"id": CDK, "status": "queued"}}], "duplicated": [], "failed": []}
    with pytest.raises(ScanApiError) as raised:
        submit(orderhub_client.OrderHubClient())
    assert CDK not in str(raised.value)
    response.json.return_value["created"][0]["order"] = {"id": "safe-id", "status": CDK + AT, "message": CDK + AT}
    result = submit(orderhub_client.OrderHubClient())
    assert CDK not in json.dumps(result) and AT not in json.dumps(result)
