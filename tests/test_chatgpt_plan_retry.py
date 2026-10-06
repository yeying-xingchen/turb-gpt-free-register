# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

import core.chatgpt_plan as plan


class _Response:
    def __init__(self, status, data=None, text=""):
        self.status_code = status
        self._data = data
        self.text = text
        self.headers = {}

    def json(self):
        return self._data


class _HttpSession:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _PlanSession:
    created = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.device_id = "device-one"
        self.oai_session_id = "session-one"
        self.session = _HttpSession()
        self.reset_count = 0
        self.requests = []
        self.responses = [
            _Response(403, text="blocked"),
            _Response(200, data={
                "accounts": {
                    "acc-1": {
                        "account": {"account_id": "acc-1", "plan_type": "free"},
                        "entitlement": {},
                    }
                }
            }),
            _Response(200, data={
                "active_start": "2025-01-01T00:00:00Z",
                "active_until": "2025-02-01T00:00:00Z",
                "became_delinquent_timestamp": "2025-01-15T00:00:00Z",
                "grace_period_end_timestamp": "2025-01-22T00:00:00Z",
                "billing_currency": "USD",
                "billing_period": "monthly",
                "plan_type": "chatgptplusplan",
            }),
            # 额度 / 用量 / 「银行重置」券：与套餐查询共用同一条已登录会话，
            # 请求顺序与「额度.har」一致（先列重置券，再读额度余额，最后读用量）。
            _Response(200, data={
                "available_count": 2,
                "applicable_available_count": 1,
                "credits": [
                    {"id": "c1", "status": "available", "expires_at": "2026-07-17T17:38:38Z"},
                ],
            }),
            _Response(200, data={"remaining_balance": "12.34", "currency": "USD"}),
            _Response(200, data={
                "plan_type": "plus",
                "rate_limit": {
                    "allowed": True,
                    "limit_reached": False,
                    "primary_window": {"used_percent": 22, "reset_at": 1766948068, "limit_window_seconds": 18000},
                    "secondary_window": {"used_percent": 94, "reset_at": 1767407914, "limit_window_seconds": 604800},
                },
                "credits": {"has_credits": True, "unlimited": False, "balance": "12.34"},
            }),
        ]
        self.created.append(self)

    def get_chatgpt_headers(self, **_kwargs):
        return {
            "content-type": "application/json",
            "oai-device-id": self.device_id,
            "oai-session-id": self.oai_session_id,
            "oai-client-build-number": "build",
            "oai-client-version": "version",
        }

    def get(self, url, headers=None, **_kwargs):
        self.requests.append((url, dict(headers or {})))
        return self.responses.pop(0)

    def reset_circuit_breaker(self):
        self.reset_count += 1

    def js_timezone_offset_min(self):
        return -540

    def fingerprint_summary_text(self):
        return "lang=ja-JP tz=Asia/Tokyo"


class ChatgptPlanRetryTests(unittest.TestCase):
    def setUp(self):
        _PlanSession.created = []

    def test_403_retries_in_same_session_with_complete_frontend_headers(self):
        claims = {
            "payload": {}, "email": "one@example.com", "account_id": "acc-1",
            "token_expired": False, "claim_plan_type": "free",
        }
        with patch.object(plan, "BrowserSession", _PlanSession), patch.object(
            plan, "token_claims", return_value=claims
        ), patch.object(plan, "resolve_plan_check_route", return_value={
            "proxy": "socks5://proxy:1080", "proxy_mode": "proxy",
            "network_route": "proxy", "proxy_used": "socks5://***:***@proxy:1080",
            "proxy_fallback_reason": None,
        }), patch.object(plan, "_warm_plan_session"), patch.object(
            plan.time, "sleep"
        ):
            result = plan.check_account_plan(
                "token", max_attempts=2, retry_delay=0,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["attempt_count"], 2)
        self.assertEqual(result["timezone_offset_min"], "-540")
        self.assertEqual(result["subscription_grace_period_end_at"], "2025-01-22T00:00:00Z")
        self.assertEqual(result["subscription_became_delinquent_at"], "2025-01-15T00:00:00Z")
        self.assertEqual(len(_PlanSession.created), 1)
        session = _PlanSession.created[0]
        self.assertEqual(session.reset_count, 1)
        self.assertTrue(session.session.closed)
        self.assertTrue(session.kwargs["detect_exit_geo"])
        self.assertTrue(session.kwargs["fingerprint_seed"].startswith("plan-check:one@example.com:"))
        first_headers = session.requests[0][1]
        self.assertEqual(first_headers["chatgpt-account-id"], "acc-1")
        self.assertEqual(first_headers["oai-device-id"], "device-one")
        self.assertEqual(first_headers["oai-session-id"], "session-one")
        self.assertNotIn("content-type", first_headers)
        subscription_requests = [
            (url, headers) for url, headers in session.requests
            if "/backend-api/subscriptions" in url
        ]
        self.assertEqual(len(subscription_requests), 1)
        subscription_url, subscription_headers = subscription_requests[0]
        self.assertIn("/backend-api/subscriptions?account_id=acc-1", subscription_url)
        self.assertEqual(subscription_headers["x-openai-target-path"], "/backend-api/subscriptions")
        self.assertEqual(subscription_headers["x-openai-target-route"], "/backend-api/subscriptions")
        self.assertEqual(subscription_headers["chatgpt-account-id"], "acc-1")

        # 同一次会话追加三个只读接口（参考 额度.har + wham/usage），结果合并进套餐结果。
        quota_urls = [url for url, _headers in session.requests]
        self.assertIn("https://chatgpt.com/backend-api/accounts/acc-1/remaining_balance", quota_urls)
        self.assertIn("https://chatgpt.com/backend-api/wham/rate-limit-reset-credits", quota_urls)
        self.assertIn("https://chatgpt.com/backend-api/wham/usage", quota_urls)
        self.assertLess(
            quota_urls.index("https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"),
            quota_urls.index("https://chatgpt.com/backend-api/accounts/acc-1/remaining_balance"),
        )
        self.assertLess(
            quota_urls.index("https://chatgpt.com/backend-api/accounts/acc-1/remaining_balance"),
            quota_urls.index("https://chatgpt.com/backend-api/wham/usage"),
        )
        self.assertEqual(result["quota_balance"], "12.34")
        self.assertEqual(result["quota_currency"], "USD")
        self.assertIsNone(result["quota_error"])
        self.assertEqual(result["reset_credits_available"], 2)
        self.assertEqual(result["reset_credits_applicable"], 1)
        self.assertEqual(result["reset_credits_expires_at"], "2026-07-17T17:38:38Z")
        self.assertIsNone(result["reset_credits_error"])
        # 5 小时/周用量与 credits 权益一并写回套餐结果。
        self.assertIsNone(result["usage_error"])
        self.assertEqual(result["usage_5h_percent"], 22.0)
        self.assertEqual(result["usage_5h_window_seconds"], 18000)
        self.assertEqual(result["usage_week_percent"], 94.0)
        self.assertEqual(result["usage_week_window_seconds"], 604800)
        self.assertIs(result["quota_has_credits"], True)

    def test_403_is_retryable(self):
        self.assertTrue(plan._retryable_plan_error(403))
        self.assertFalse(plan._retryable_plan_error(401))


if __name__ == "__main__":
    unittest.main()
