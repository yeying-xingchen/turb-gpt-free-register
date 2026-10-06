"""额度、用量窗口（wham/usage）与「银行重置」券的查询、解析与存储。"""
import json
from unittest.mock import patch

import pytest

from core import db
from core.chatgpt_quota import (
    BALANCE_PATH,
    RESET_CREDITS_PATH,
    WHAM_USAGE_PATH,
    fetch_account_quota,
    parse_remaining_balance,
    parse_reset_credits,
    parse_wham_usage,
)
from webui.app import create_app

AUTH = {"X-Auth-Code": "quota-test-auth"}


class _Response:
    def __init__(self, status, data=None, text=""):
        self.status_code = status
        self._data = data
        self.text = text
        self.headers = {}

    def json(self):
        return self._data


class _QuotaSession:
    """最小会话桩：按顺序返回响应并记录请求 URL/请求头。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.device_id = "device-one"

    def get_chatgpt_headers(self, **_kwargs):
        return {"content-type": "application/json", "oai-device-id": self.device_id}

    def get(self, url, headers=None, **_kwargs):
        self.requests.append((url, dict(headers or {})))
        return self.responses.pop(0)

    def reset_circuit_breaker(self):
        pass


def test_parse_remaining_balance_reads_string_and_numeric_forms():
    parsed = parse_remaining_balance({"remaining_balance": "12.34", "currency": "USD"})
    assert parsed["quota_balance"] == "12.34"
    assert parsed["quota_balance_amount"] == 12.34
    assert parsed["quota_currency"] == "USD"
    assert parsed["quota_unlimited"] is False

    numeric = parse_remaining_balance({"balance": 0, "currency": "USD", "unlimited": False})
    assert numeric["quota_balance"] == "0"
    assert numeric["quota_balance_amount"] == 0.0
    assert numeric["quota_balance_key"] == "balance"

    unlimited = parse_remaining_balance({"remaining_balance": None, "unlimited": True})
    assert unlimited["quota_balance"] is None
    assert unlimited["quota_unlimited"] is True


def test_parse_remaining_balance_keeps_unknown_payload_for_diagnostics():
    payload = {"credits_left": "5.00", "currency": "USD", "has_credits": True}
    parsed = parse_remaining_balance(payload)
    # 未识别的键名不猜测数值，但原始响应保留下来供排查灰度字段。
    assert parsed["quota_balance"] is None
    assert parsed["quota_has_credits"] is True
    assert parsed["quota_response"] == payload


def test_parse_remaining_balance_rejects_non_object():
    with pytest.raises(ValueError):
        parse_remaining_balance(["not", "an", "object"])


def test_parse_reset_credits_counts_only_usable_credits():
    parsed = parse_reset_credits({
        "available_count": 2,
        "applicable_available_count": 0,
        "credits": [
            {"id": "c1", "status": "available", "expires_at": "2026-08-01T00:00:00Z"},
            {"id": "c2", "status": "available", "expires_at": "2026-07-17T17:38:38Z"},
            {"id": "c3", "status": "redeemed", "expires_at": "2026-06-01T00:00:00Z"},
            {"id": "c4", "status": "expired", "expires_at": "2026-06-02T00:00:00Z"},
        ],
    })
    assert parsed["reset_credits_available"] == 2
    assert parsed["reset_credits_applicable"] == 0
    assert parsed["reset_credits_expires_at"] == "2026-07-17T17:38:38Z"
    assert [item["id"] for item in parsed["reset_credits_detail"]] == ["c1", "c2", "c3", "c4"]


def test_parse_reset_credits_falls_back_to_list_length():
    parsed = parse_reset_credits({"credits": [{"id": "c1", "status": "available", "expires_at": "2026-08-01T00:00:00Z"}]})
    assert parsed["reset_credits_available"] == 1
    assert parsed["reset_credits_applicable"] is None
    assert parse_reset_credits({})["reset_credits_available"] is None


def test_parse_wham_usage_classifies_windows_by_real_length():
    parsed = parse_wham_usage({
        "plan_type": "plus",
        "rate_limit": {
            "allowed": True,
            "limit_reached": False,
            "primary_window": {"used_percent": 22, "reset_at": 1766948068, "limit_window_seconds": 18000},
            "secondary_window": {"used_percent": 43.5, "reset_at": 1767407914, "limit_window_seconds": 604800},
        },
        "credits": {"has_credits": True, "unlimited": False, "balance": "12.34"},
    })
    assert parsed["usage_plan_type"] == "plus"
    assert parsed["usage_allowed"] is True
    assert parsed["usage_limit_reached"] is False
    assert parsed["usage_5h_percent"] == 22.0
    assert parsed["usage_5h_window_seconds"] == 18000
    assert parsed["usage_5h_reset_at"] == "2025-12-28T18:54:28+00:00"
    assert parsed["usage_5h_started"] is True
    assert parsed["usage_week_percent"] == 43.5
    assert parsed["usage_week_window_seconds"] == 604800
    assert parsed["quota_has_credits"] is True
    assert parsed["quota_credits_balance"] == "12.34"


def test_parse_wham_usage_keeps_monthly_window_and_never_started_flag():
    parsed = parse_wham_usage({
        "rate_limit": {
            "primary_window": {
                "used_percent": 0,
                "reset_after_seconds": 2592000,
                "limit_window_seconds": 2592000,
            },
        },
        "credits": {"has_credits": False, "balance": "0"},
    })
    # 团队套餐的整月窗口落在长窗口槽位，并带上真实窗口长度供前端显示「月」。
    assert parsed["usage_week_window_seconds"] == 2592000
    assert parsed["usage_week_percent"] == 0.0
    assert parsed["usage_week_reset_at"] is None
    assert parsed["usage_week_reset_after_seconds"] == 2592000
    # 0% 且剩余时间仍是整个窗口：窗口还没开始用，不是「用完又重置」。
    assert parsed["usage_week_started"] is False
    assert parsed["usage_5h_percent"] is None
    assert parsed["quota_has_credits"] is False


def test_parse_wham_usage_tolerates_camel_case_and_missing_windows():
    parsed = parse_wham_usage({
        "planType": "pro",
        "rateLimit": {
            "allowed": False,
            "limitReached": True,
            "primaryWindow": {"usedPercent": "80", "resetAt": 1766948068000, "limitWindowSeconds": 18000},
        },
        "rateLimitReachedType": {"type": "primary"},
    })
    assert parsed["usage_plan_type"] == "pro"
    assert parsed["usage_allowed"] is False
    assert parsed["usage_limit_reached"] is True
    assert parsed["usage_limit_reached_type"] == "primary"
    assert parsed["usage_5h_percent"] == 80.0
    assert parsed["usage_5h_reset_at"] == "2025-12-28T18:54:28+00:00"
    assert parsed["usage_week_percent"] is None

    with pytest.raises(ValueError):
        parse_wham_usage([])


def test_fetch_account_quota_uses_reference_paths_and_isolates_failures():
    session = _QuotaSession([
        _Response(403, text="blocked"),
        _Response(200, data={"remaining_balance": "12.34", "currency": "USD"}),
        _Response(200, data={
            "plan_type": "plus",
            "rate_limit": {
                "primary_window": {"used_percent": 10, "reset_at": 1766948068, "limit_window_seconds": 18000},
                "secondary_window": {"used_percent": 90, "reset_at": 1767407914, "limit_window_seconds": 604800},
            },
            "credits": {"has_credits": True, "balance": "12.34"},
        }),
    ])
    result = fetch_account_quota(
        session,
        "access-token",
        "acc-1",
        timeout=5.0,
        claims={"account_id": "acc-1"},
    )
    # 顺序与「额度.har」一致：先列重置券，再读额度余额，最后补 wham/usage 用量。
    assert [url for url, _headers in session.requests] == [
        f"https://chatgpt.com{RESET_CREDITS_PATH}",
        f"https://chatgpt.com{BALANCE_PATH.format(account_id='acc-1')}",
        f"https://chatgpt.com{WHAM_USAGE_PATH}",
    ]
    # 重置券失败只影响重置券字段，额度和用量结果照常返回。
    assert result["reset_credits_error"] == "HTTP 403"
    assert "reset_credits_available" not in result
    assert result["quota_error"] is None
    assert result["quota_balance"] == "12.34"
    assert result["quota_currency"] == "USD"
    assert result["usage_error"] is None
    assert result["usage_5h_percent"] == 10.0
    assert result["usage_week_percent"] == 90.0
    for _url, headers in session.requests:
        assert headers["authorization"] == "Bearer access-token"
        assert headers["chatgpt-account-id"] == "acc-1"
        assert headers["x-openai-target-path"].startswith("/backend-api/")


def test_fetch_account_quota_falls_back_to_usage_credits_balance():
    session = _QuotaSession([
        _Response(200, data={"available_count": 0, "credits": []}),
        _Response(500, text="boom"),
        _Response(200, data={
            "credits": {"has_credits": True, "balance": "7.50"},
            "rate_limit": {},
        }),
    ])
    result = fetch_account_quota(session, "access-token", "acc-1", timeout=5.0)
    assert result["quota_error"] == "HTTP 500"
    # 余额端点失败时用 wham/usage 的 credits.balance 兜底，并标记来源。
    assert result["quota_balance"] == "7.50"
    assert result["quota_balance_amount"] == 7.5
    assert result["quota_balance_fallback"] is True
    assert result["quota_has_credits"] is True


def test_fetch_account_quota_without_account_id_still_reads_reset_credits():
    session = _QuotaSession([
        _Response(200, data={"available_count": 0, "credits": []}),
        _Response(200, data={"rate_limit": {}, "credits": {"has_credits": False}}),
    ])
    result = fetch_account_quota(session, "access-token", "", timeout=5.0)
    assert result["quota_error"] == "缺少 account_id，无法查询额度余额"
    assert result["reset_credits_available"] == 0
    assert result["reset_credits_error"] is None
    assert result["usage_error"] is None
    assert result["quota_has_credits"] is False
    assert len(session.requests) == 2


def test_update_account_quota_persists_values_and_keeps_last_balance_on_failure():
    account_id = db.insert_account(email="quota@example.test", access_token="private-at")

    assert db.claim_account_quota_check(account_id, trigger="manual")
    assert db.mark_account_quota_check_running(account_id)
    assert db.update_account_quota(acc_id=account_id, result={
        "ok": True,
        "checked_at": "2026-10-06T10:00:00",
        "quota_checked_at": "2026-10-06T10:00:00",
        "quota_balance": "12.34",
        "quota_balance_amount": 12.34,
        "quota_currency": "USD",
        "quota_unlimited": False,
        "quota_error": None,
        "quota_http_status": 200,
        "reset_credits_checked_at": "2026-10-06T10:00:00",
        "reset_credits_available": 2,
        "reset_credits_applicable": 1,
        "reset_credits_expires_at": "2026-07-17T17:38:38Z",
        "reset_credits_detail": [{"id": "c1", "status": "available", "expires_at": "2026-07-17T17:38:38Z"}],
        "reset_credits_error": None,
        "reset_credits_http_status": 200,
    })

    row = db.get_account(account_id)
    assert row["quota_check_status"] == "success"
    assert row["quota_check_ok"] is True
    assert row["quota_balance"] == "12.34"
    assert row["quota_currency"] == "USD"
    assert row["reset_credits_available"] == 2
    assert row["reset_credits_expires_at"] == "2026-07-17T17:38:38Z"
    assert row["quota_last_success_at"]

    # 下一次只有重置券端点成功：额度保留上次数值，失败原因写进 quota_error。
    assert db.claim_account_quota_check(account_id, trigger="manual_bulk")
    assert db.update_account_quota(acc_id=account_id, result={
        "ok": True,
        "checked_at": "2026-10-06T11:00:00",
        "quota_checked_at": "2026-10-06T11:00:00",
        "quota_error": "HTTP 500",
        "quota_http_status": 500,
        "reset_credits_checked_at": "2026-10-06T11:00:00",
        "reset_credits_available": 0,
        "reset_credits_applicable": 0,
        "reset_credits_expires_at": None,
        "reset_credits_detail": [],
        "reset_credits_error": None,
        "reset_credits_http_status": 200,
    })
    row = db.get_account(account_id)
    assert row["quota_balance"] == "12.34"
    assert row["quota_error"] == "HTTP 500"
    assert row["reset_credits_available"] == 0

    # 排队中的任务在下次占用时不被重复接受，恢复启动后被标记为失败。
    assert db.claim_account_quota_check(account_id, trigger="manual")
    assert not db.claim_account_quota_check(account_id, trigger="manual")
    assert db.recover_interrupted_quota_checks() == 1
    assert db.get_account(account_id)["quota_check_status"] == "failed"


def test_account_list_exposes_quota_columns_without_credentials():
    account_id = db.insert_account(email="quota-list@example.test", access_token="private-at")
    db.update_account_quota(acc_id=account_id, result={
        "ok": True,
        "checked_at": "2026-10-06T10:00:00",
        "quota_checked_at": "2026-10-06T10:00:00",
        "quota_balance": "3.50",
        "quota_balance_amount": 3.5,
        "quota_currency": "USD",
        "quota_error": None,
        "quota_http_status": 200,
        "reset_credits_checked_at": "2026-10-06T10:00:00",
        "reset_credits_available": 1,
        "reset_credits_applicable": 1,
        "reset_credits_expires_at": "2026-07-17T17:38:38Z",
        "reset_credits_detail": [
            {"id": "RateLimitResetCredit_private", "status": "available", "expires_at": "2026-07-17T17:38:38Z"},
        ],
        "reset_credits_error": None,
        "reset_credits_http_status": 200,
        "usage_checked_at": "2026-10-06T10:00:00",
        "usage_plan_type": "plus",
        "usage_allowed": True,
        "usage_limit_reached": False,
        "usage_5h_percent": 22.0,
        "usage_5h_window_seconds": 18000,
        "usage_5h_reset_at": "2025-12-28T18:54:28+00:00",
        "usage_5h_started": True,
        "usage_week_percent": 94.0,
        "usage_week_window_seconds": 604800,
        "usage_week_reset_at": "2026-01-03T02:38:34+00:00",
        "usage_week_started": True,
        "usage_error": None,
        "usage_http_status": 200,
        "quota_has_credits": True,
    })
    app = create_app(auth_code=AUTH["X-Auth-Code"])
    client = app.test_client()
    response = client.get("/api/accounts?paged=1&page_size=50", headers=AUTH)
    assert response.status_code == 200
    row = next(item for item in response.get_json()["items"] if item["id"] == account_id)
    assert row["quota_balance"] == "3.50"
    assert row["quota_currency"] == "USD"
    assert row["reset_credits_available"] == 1
    assert row["reset_credits_expires_at"] == "2026-07-17T17:38:38Z"
    # 用量窗口字段按原样下发，前端据窗口长度显示 5h/周/月。
    assert row["usage_5h_percent"] == 22.0
    assert row["usage_5h_window_seconds"] == 18000
    assert row["usage_week_percent"] == 94.0
    assert row["usage_week_reset_at"] == "2026-01-03T02:38:34+00:00"
    assert row["quota_has_credits"] is True
    # 上游 credit id 属于账号内部标识，列表只下发状态和到期时间。
    assert row["reset_credits_detail"] == [
        {"status": "available", "expires_at": "2026-07-17T17:38:38Z"}
    ]
    body = response.get_data(as_text=True)
    assert "private-at" not in body
    assert "RateLimitResetCredit_private" not in body


def test_bulk_quota_route_skips_accounts_without_token():
    token_id = db.insert_account(email="quota-bulk@example.test", access_token="private-at")
    empty_id = db.insert_account(email="quota-bulk-empty@example.test", access_token="")
    app = create_app(auth_code=AUTH["X-Auth-Code"])
    client = app.test_client()

    queued = []

    def fake_enqueue(**kwargs):
        queued.append(kwargs)
        return {"accepted": True, "busy": False, "account_id": kwargs["account_id"], "status": "queued"}

    with patch("webui.app.quota_check_service.enqueue_account_quota_check", side_effect=fake_enqueue):
        response = client.post(
            "/api/accounts/check-quota-bulk",
            json={"account_ids": [token_id, empty_id]},
            headers=AUTH,
        )

    assert response.status_code == 202
    payload = response.get_json()
    assert payload["started_count"] == 1
    assert payload["skipped_count"] == 1
    assert payload["skipped"][0]["id"] == empty_id
    assert payload["skipped"][0]["reason"] == "缺少 access_token"
    assert [item["account_id"] for item in queued] == [token_id]
    assert queued[0]["trigger"] == "manual_bulk"


def test_plan_check_piggyback_writes_quota_columns_without_task_state():
    from core import task_center_store as tasks

    account_id = db.insert_account(email="quota-piggyback@example.test", access_token="private-at")
    assert db.claim_account_plan_check(account_id, trigger="manual")
    assert db.mark_account_plan_check_running(account_id)
    db.update_account_plan_check(acc_id=account_id, result={
        "ok": True,
        "checked_at": "2026-10-06T12:00:00",
        "current_plan_type": "plus",
        "quota_checked_at": "2026-10-06T12:00:00",
        "quota_balance": "9.99",
        "quota_balance_amount": 9.99,
        "quota_currency": "USD",
        "quota_error": None,
        "quota_http_status": 200,
        "reset_credits_checked_at": "2026-10-06T12:00:00",
        "reset_credits_available": 3,
        "reset_credits_applicable": 3,
        "reset_credits_expires_at": "2026-07-17T17:38:38Z",
        "reset_credits_error": None,
        "reset_credits_http_status": 200,
        "usage_checked_at": "2026-10-06T12:00:00",
        "usage_5h_percent": 22.0,
        "usage_5h_window_seconds": 18000,
        "usage_5h_reset_at": "2025-12-28T18:54:28+00:00",
        "usage_week_percent": 94.0,
        "usage_week_window_seconds": 604800,
        "usage_error": None,
        "usage_http_status": 200,
        "quota_has_credits": True,
    })

    row = db.get_account(account_id)
    assert row["current_plan_type"] == "plus"
    assert row["quota_balance"] == "9.99"
    assert row["reset_credits_available"] == 3
    # 5h/周用量与 credits 权益同样跟着套餐查询一起落盘。
    assert row["usage_5h_percent"] == 22.0
    assert row["usage_week_percent"] == 94.0
    assert row["quota_has_credits"] is True
    # piggyback 不占用额度查询状态机：任务中心不会凭空多出一条「查询额度」历史。
    assert not row.get("quota_check_status")
    assert [
        task for task in tasks.list_history_tasks_page(50)["items"]
        if task.get("job_type") == "quota_check"
    ] == []


def test_quota_check_task_kind_is_registered():
    from core import task_center_store

    assert task_center_store.LABELS["quota_check"] == "查询额度与用量"
    assert task_center_store.PREFIXES["quota_check"] == "quota_check"
    assert json.dumps(task_center_store._SUCCESS_MESSAGES, ensure_ascii=False)
