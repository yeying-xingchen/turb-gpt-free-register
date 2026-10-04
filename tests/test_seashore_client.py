# -*- coding: utf-8 -*-
"""发布者 API 客户端的离线契约：只做模拟 HTTP，不创建真实支付任务。"""
import json
from unittest.mock import MagicMock

import pytest
import requests

from core.seashore_client import SeashorePublisherClient, CHARGED_REJECTIONS, STATUS_ALIASES
from core.scan_api_client import ScanApiError

CDK = "PBK-04A0-3254-AEB7-9C31"
AT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature"
LINK = "https://payments.stripe.com/upi/instructions/test"
BASE = "https://seashore.lol/api/publisher"
PID = "ORD-1A2B3C4D"


@pytest.fixture
def http(monkeypatch):
    session = MagicMock()
    session.__enter__.return_value = session
    response = MagicMock(status_code=200, headers={})
    response.json.return_value = {"task": {
        "public_id": PID, "email": "a@example.test", "status": "pending",
        "status_text": "等待接单", "can_cancel": True, "created_at": 1789489771,
        "resolved_at": 0, "pay_link": LINK,
    }}
    session.request.return_value = response
    monkeypatch.setattr(requests, "Session", lambda: session)
    yield session, response


def client():
    return SeashorePublisherClient(api_base=BASE)


def submit(c=None, **changes):
    args = dict(cdk=CDK, link=LINK, email="a@example.test", access_token=AT)
    args.update(changes)
    return (c or client()).submit_upi(**args)


def test_submit_uses_bearer_header_and_documented_body(http):
    session, _ = http
    result = submit()
    call = session.request.call_args
    assert call.args == ("POST", BASE + "/tasks")
    assert call.kwargs["json"] == {"email": "a@example.test", "access_token": AT, "pay_link": LINK}
    assert call.kwargs["headers"]["Authorization"] == "Bearer " + CDK
    # 发布者 API 没有幂等键：出现 Idempotency-Key 会让上游忽略或报错。
    assert "Idempotency-Key" not in call.kwargs["headers"]
    assert call.kwargs["allow_redirects"] is False
    assert result["task_id"] == PID and result["uncertain"] is False
    assert result["task"]["status"] == "queued" and result["task"]["providerStatus"] == "pending"
    assert result["task"]["statusText"] == "等待接单" and result["task"]["canCancel"] is True


def test_submit_requires_full_at_and_email_before_http(http):
    session, _ = http
    for change in ({"access_token": "not-a-jwt"}, {"email": ""}, {"email": None}, {"access_token": None}):
        with pytest.raises(ValueError):
            submit(**change)
    session.request.assert_not_called()


@pytest.mark.parametrize("value", ["http://payments.stripe.com/x", "https://evil.test/x", None])
def test_submit_rejects_non_stripe_link(http, value):
    with pytest.raises(ValueError):
        submit(link=value)
    http[0].request.assert_not_called()


@pytest.mark.parametrize("upstream,local", sorted(STATUS_ALIASES.items()))
def test_status_aliases_keep_provider_code(http, upstream, local):
    _, response = http
    response.json.return_value = {"task": {"public_id": PID, "status": upstream, "status_text": "文案"}}
    task = client().get_task(cdk=CDK, task_id=PID)["task"]
    assert task["status"] == local and task["providerStatus"] == upstream


def test_submitted_is_verifying_and_completed_is_terminal(http):
    _, response = http
    response.json.return_value = {"task": {"public_id": PID, "status": "submitted", "status_text": "已提交（待验证）"}}
    task = client().get_task(cdk=CDK, task_id=PID)["task"]
    assert task["status"] == "verifying" and task["verifying"] is True
    response.json.return_value = {"task": {"public_id": PID, "status": "completed", "status_text": "已完成"}}
    task = client().get_task(cdk=CDK, task_id=PID)["task"]
    assert task["status"] == "completed" and "verifying" not in task


@pytest.mark.parametrize("payload", [
    {"task": {"public_id": "ORD-OTHER", "status": "completed"}},
    {"task": {"status": "completed"}},
    {},
])
def test_query_rejects_wrong_or_missing_task_id(http, payload):
    http[1].json.return_value = payload
    with pytest.raises(ScanApiError):
        client().get_task(cdk=CDK, task_id=PID)


@pytest.mark.parametrize("code", sorted(CHARGED_REJECTIONS))
def test_charged_rejection_marks_charged_without_task(http, code):
    _, response = http
    response.status_code = 400
    response.json.return_value = {"error": "该商品不被接受", "code": code}
    with pytest.raises(ScanApiError) as raised:
        submit()
    error = raised.value
    assert error.code == code and error.charged is True and error.created is False
    assert error.uncertain is False and error.status == 400
    assert "已扣 1 次" in str(error)


@pytest.mark.parametrize("status,code", [
    (400, "paylink_expired"), (400, "paylink_already_paid"), (400, "at_expired"),
    (400, "invalid_pay_link"), (409, "conflict"), (402, "insufficient_uses"),
])
def test_plain_rejections_are_not_charged_or_uncertain(http, status, code):
    _, response = http
    response.status_code = status
    response.json.return_value = {"error": "被拒绝", "code": code}
    with pytest.raises(ScanApiError) as raised:
        submit()
    error = raised.value
    assert error.code == code and not error.charged and error.uncertain is False
    assert error.created is False


def test_invalid_credential_401_never_leaks_and_is_not_uncertain(http):
    _, response = http
    response.status_code = 401
    response.json.return_value = {"error": f"CDK {CDK} 已被禁用", "code": "invalid_credential"}
    with pytest.raises(ScanApiError) as raised:
        submit()
    error = raised.value
    assert error.status == 401 and error.code == "invalid_credential" and error.uncertain is False
    assert CDK not in str(error) and CDK not in json.dumps(error.code or "")


def test_blocked_email_warns_about_disabled_cdk(http):
    _, response = http
    response.status_code = 403
    response.json.return_value = {"error": "邮箱在黑名单中", "code": "email_blocked"}
    with pytest.raises(ScanApiError) as raised:
        submit()
    assert "自动停用" in str(raised.value) and raised.value.uncertain is False


def test_server_error_and_transport_stay_uncertain(http):
    session, response = http
    response.status_code = 503
    response.json.return_value = {"error": "busy"}
    with pytest.raises(ScanApiError) as raised:
        submit()
    assert raised.value.uncertain and raised.value.status == 503
    session.request.reset_mock()
    session.request.side_effect = requests.Timeout(CDK + AT)
    with pytest.raises(ScanApiError) as raised:
        submit()
    assert raised.value.uncertain and raised.value.code == "TRANSPORT_ERROR"
    assert CDK not in str(raised.value) and AT not in str(raised.value)
    session.request.assert_called_once()
    assert session.request.call_args.kwargs["allow_redirects"] is False


def test_rate_limited_is_retryable_and_keeps_credential_out(http):
    _, response = http
    response.status_code = 429
    response.json.return_value = {"error": "认证失败次数过多", "code": "rate_limited", "retry_after_seconds": 30}
    with pytest.raises(ScanApiError) as raised:
        client().verify_cdk(CDK)
    error = raised.value
    assert error.code == "rate_limited" and error.retryable is True
    assert error.retry_after_seconds == 30 and CDK not in str(error)


def test_verify_me_returns_only_numbers(http):
    _, response = http
    response.json.return_value = {
        "code": CDK, "remaining_uses": 8, "uses_total": 10, "uses_used": 2, "uses_held": 0,
        "uses_consumed": 2, "success_count": 3, "pending_orders": 21, "failed_count": 1,
        "capacity": {"accept_per_minute": 1.4, "complete_per_minute": 1.3, "link_budget_seconds": 279,
                     "recommended": 6, "expected_wait_seconds": 3, "idle": False, "note": CDK},
        "site_stats": {"today_success": 12, "window_24h_rate": 91, "unknown": CDK},
        "email": "private@example.test",
    }
    data = client().verify_cdk(CDK)["data"]
    assert data["balance"] == data["remaining_uses"] == 8
    assert data["pending_orders"] == 21 and data["capacity"]["recommended"] == 6
    assert data["site_stats"] == {"today_success": 12, "window_24h_rate": 91}
    assert "code" not in data and "email" not in data
    assert CDK not in json.dumps(data) and "private@example.test" not in json.dumps(data)


def test_idle_capacity_and_unknown_keys_are_dropped(http):
    _, response = http
    response.json.return_value = {"remaining_uses": 0, "capacity": {"idle": True, "expected_wait_seconds": -1},
                                  "orders": [{"public_id": PID, "email": "private@example.test"}]}
    data = client().verify_cdk(CDK)["data"]
    assert data["capacity"] == {"idle": True, "expected_wait_seconds": -1}
    assert "orders" not in data


def test_non_json_response_is_reported_without_credentials(http):
    _, response = http
    response.json.side_effect = ValueError("not json")
    response.text = f"<html>{CDK}</html>"
    with pytest.raises(ScanApiError) as raised:
        client().get_task(cdk=CDK, task_id=PID)
    assert raised.value.code == "INVALID_RESPONSE" and CDK not in str(raised.value)


def test_task_id_must_look_like_public_id(http):
    for value in ("", "has space", "../etc/passwd", 7, None):
        with pytest.raises(ValueError):
            client().get_task(cdk=CDK, task_id=value)
    http[0].request.assert_not_called()
