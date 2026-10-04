# -*- coding: utf-8 -*-
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import core.account_liveness as liveness
import core.live_check_service as live_service


class _DummyHttpSession:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _DummyBrowserSession:
    created = []

    def __init__(self, proxy=None, **kwargs):
        self.proxy = proxy
        self.received_proxy = proxy
        self.device_id = "device-test"
        self.session = _DummyHttpSession()
        self.blocked_until = 0.0
        self.blocked_reason = ""
        self.created.append(self)
        self.kwargs = kwargs

    def fingerprint_summary(self):
        return {
            "proxy": self.proxy or "",
            "user_agent": "test-agent",
            "accept_language": "en-US",
            "timezone_iana": "UTC",
            "timezone_offset_minutes": 0,
            "screen_width": 1440,
            "screen_height": 900,
            "device_pixel_ratio": 2,
            "hardware_concurrency": 8,
            "device_memory": 8,
            "geo_country": "US",
            "geo_city": "Test",
        }

    def fingerprint_summary_text(self):
        return "test-fingerprint"

    def reset_circuit_breaker(self):
        self.blocked_until = 0.0
        self.blocked_reason = ""


class _DummyQueueSlot:
    def __init__(self):
        self.released = False

    def release(self):
        self.released = True


class AccountLivenessTests(unittest.TestCase):
    def setUp(self):
        _DummyBrowserSession.created = []
        for name in ("_account_registration_password", "_account_totp_secret"):
            patcher = patch.object(liveness, name, return_value="")
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_preflight_preserves_explicit_direct_route_and_skips_providers(self):
        with patch.object(liveness, "BrowserSession", _DummyBrowserSession), \
             patch.object(liveness, "_warm_login_fingerprint_context"), \
             patch.object(liveness, "get_csrf_token", return_value="csrf"), \
             patch.object(liveness, "signin_openai", return_value="https://auth.example/authorize"):
            session, authorize_url = liveness._network_preflight_with_retry(
                "user@example.com", "", max_attempts=1
            )

        self.assertEqual(session.proxy, "")
        self.assertEqual(authorize_url, "https://auth.example/authorize")
        self.assertIsNone(session.kwargs["fingerprint_seed"])

    def test_preflight_retries_with_same_session_when_csrf_is_blocked(self):
        csrf_errors = [RuntimeError("HTTP Error 403"), "csrf"]
        with patch.object(liveness, "BrowserSession", _DummyBrowserSession), \
             patch.object(liveness, "_warm_login_fingerprint_context"), \
             patch.object(liveness, "get_csrf_token", side_effect=csrf_errors), \
             patch.object(liveness, "signin_openai", return_value="authorize"), \
             patch.object(liveness.time, "sleep"):
            session, _ = liveness._network_preflight_with_retry(
                "user@example.com", None, max_attempts=2
            )

        self.assertIs(_DummyBrowserSession.created[-1], session)
        self.assertEqual(len(_DummyBrowserSession.created), 1)
        self.assertFalse(_DummyBrowserSession.created[0].session.closed)
        self.assertIsNone(_DummyBrowserSession.created[0].received_proxy)

    def test_callback_403_retries_in_same_session_before_fetching_token(self):
        session = _DummyBrowserSession(proxy="proxy")
        with patch.object(
            liveness,
            "follow_oauth_callback",
            side_effect=[RuntimeError("HTTP 403 from callback/openai"), "https://chatgpt.com/"],
        ) as callback, patch.object(
            liveness,
            "fetch_session",
            return_value={"accessToken": "token"},
        ) as fetch, patch.object(liveness.time, "sleep"):
            result = liveness._follow_continue_and_fetch(
                session,
                "https://auth.openai.com/authorize/continue?state=test",
                referer="https://auth.openai.com/email-verification",
            )

        self.assertEqual(result["accessToken"], "token")
        self.assertEqual(callback.call_count, 2)
        fetch.assert_called_once_with(session)

    def test_fingerprint_identity_is_pinned_when_session_is_recreated_in_one_attempt(self):
        state = {}
        first = MagicMock()
        first.browser_profile = {
            "navigator_language": "ja-JP",
            "timezone_iana": "Asia/Tokyo",
            "screen_width": 1512,
        }
        second = MagicMock()
        second.browser_profile = dict(first.browser_profile)
        with patch.object(liveness, "BrowserSession", side_effect=[first, second]) as factory:
            self.assertIs(
                liveness._new_fingerprint_pinned_session(
                    "user@example.com", "socks5://proxy:1080", state,
                ),
                first,
            )
            self.assertIs(
                liveness._new_fingerprint_pinned_session(
                    "user@example.com", "", state,
                ),
                second,
            )

        first_kwargs = factory.call_args_list[0].kwargs
        second_kwargs = factory.call_args_list[1].kwargs
        self.assertTrue(first_kwargs["detect_exit_geo"])
        self.assertIsNone(first_kwargs["browser_profile"])
        self.assertFalse(second_kwargs["detect_exit_geo"])
        self.assertEqual(second_kwargs["browser_profile"]["navigator_language"], "ja-JP")
        self.assertEqual(second_kwargs["browser_profile"]["timezone_iana"], "Asia/Tokyo")
        self.assertIsNone(first_kwargs["fingerprint_seed"])
        self.assertIsNone(second_kwargs["fingerprint_seed"])

    def test_reuse_mode_uses_registration_seed_for_same_email(self):
        state = {}
        session = MagicMock()
        session.browser_profile = {"navigator_language": "en-US"}
        with patch.object(liveness, "BrowserSession", return_value=session), \
             patch("config.register.PROTOCOL_REUSE_FINGERPRINT_BY_EMAIL", True):
            liveness._new_fingerprint_pinned_session("User@Example.com", "proxy", state)
        self.assertEqual(state["fingerprint_seed"], "registration:user@example.com")
        self.assertEqual(state["fingerprint_mode"], "registration_email_stable")

    def test_direct_fallback_can_force_fresh_seed_in_reuse_mode(self):
        state = {"force_fresh": True}
        session = MagicMock()
        session.browser_profile = {"navigator_language": "en-US"}
        with patch.object(liveness, "BrowserSession", return_value=session), \
             patch("config.register.PROTOCOL_REUSE_FINGERPRINT_BY_EMAIL", True):
            liveness._new_fingerprint_pinned_session("user@example.com", "", state)
        self.assertEqual(state["fingerprint_seed"], "")

    def test_fresh_live_check_uses_same_uuid4_shape_as_registration(self):
        state = {}
        with patch("config.register.PROTOCOL_REUSE_FINGERPRINT_BY_EMAIL", False):
            session = liveness._new_fingerprint_pinned_session(
                "user@example.com", "", state,
            )
        try:
            self.assertEqual(uuid.UUID(session.device_id).version, 4)
            self.assertEqual(uuid.UUID(session.oai_session_id).version, 4)
            self.assertEqual(uuid.UUID(session.auth_session_logging_id).version, 4)
        finally:
            session.close()

    def test_reauth_otp_dead_account_error_is_not_retried(self):
        response = SimpleNamespace(
            status_code=403,
            text='{"error":{"code":"account_deactivated"}}',
        )
        error = RuntimeError("HTTP Error 403")
        error.response = response
        session = _DummyBrowserSession(proxy="proxy")

        with patch.object(liveness, "_validate_reauth_otp", side_effect=error), \
             patch.object(liveness, "wait_for_otp", return_value="123456"):
            with self.assertRaises(liveness.AccountUnusableError) as ctx:
                liveness._validate_reauth_with_retry(session, "user@example.com", 1.0)

        self.assertEqual(ctx.exception.error_code, "account_deactivated")

    def test_existing_access_token_uses_reauth_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _DummyBrowserSession(proxy="")
            with patch.object(liveness, "_LOG_DIR", Path(tmp)), \
                 patch.object(liveness, "_stored_access_token", return_value="old-token"), \
                 patch.object(liveness, "BrowserSession", return_value=session), \
                 patch.object(liveness, "_warm_authenticated_session") as warm, \
                 patch.object(liveness, "human_delay"), \
                 patch.object(liveness, "_login_via_reauth", return_value={
                     "accessToken": "new-token",
                     "user": {"id": "user-1"},
                     "account": {"planType": "free"},
                 }) as reauth:
                result = liveness.check_account_liveness("user@example.com", proxy="")

        self.assertTrue(result["ok"])
        self.assertEqual(result["access_token"], "new-token")
        warm.assert_called_once_with(session, "old-token")
        reauth.assert_called_once()
        self.assertTrue(session.session.closed)

    def test_reauth_403_falls_back_to_clean_full_web_login_on_same_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            reauth_session = _DummyBrowserSession(proxy="socks5://proxy.example:1080")
            full_session = _DummyBrowserSession(proxy="socks5://proxy.example:1080")
            fresh_info = {
                "accessToken": "new-token",
                "user": {"id": "user-1"},
                "account": {"planType": "free"},
            }
            state = {}
            with patch.object(liveness, "_LOG_DIR", Path(tmp)), \
                 patch.object(liveness, "_stored_access_token", return_value="old-token"), \
                 patch.object(liveness, "_account_totp_secret", return_value=""), \
                 patch.object(liveness, "_new_fingerprint_pinned_session", return_value=reauth_session), \
                 patch.object(liveness, "_warm_authenticated_session"), \
                 patch.object(liveness, "human_delay"), \
                 patch.object(liveness, "_login_via_reauth", side_effect=RuntimeError("HTTP 403 callback")), \
                 patch.object(liveness, "_login_via_full_web_flow", return_value=(
                     full_session, fresh_info,
                 )) as full_login:
                result = liveness.check_account_liveness(
                    "user@example.com",
                    proxy=None,
                    fingerprint_state=state,
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["access_token"], "new-token")
        full_login.assert_called_once_with(
            "user@example.com",
            "socks5://proxy.example:1080",
            email_source=None,
            fingerprint_state=state,
        )
        self.assertTrue(reauth_session.session.closed)
        self.assertTrue(full_session.session.closed)

    def test_service_403_rotates_proxy_and_never_falls_back_direct_when_pool_exists(self):
        slot = _DummyQueueSlot()
        failed = {"ok": False, "status": "failed", "error": "HTTP Error 403: blocked"}
        success = {"ok": True, "status": "live", "access_token": "new-token"}
        proxy_a = "socks5://proxy-a.example:1080"
        proxy_b = "socks5://proxy-b.example:1080"

        def route_for(explicit_proxy=None):
            selected = proxy_a if explicit_proxy is None else explicit_proxy
            return {
                "proxy": selected,
                "network_route": "proxy" if selected else "direct",
                "proxy_mode": "proxy",
                "proxy_used": selected or None,
                "upstream_proxy": "",
                "upstream_proxy_used": None,
                "proxy_fallback_reason": None,
            }

        with patch.object(live_service, "_QUEUE_SLOTS", slot), \
             patch.object(live_service.db, "mark_account_live_check_running", return_value=True), \
             patch.object(live_service.db, "get_account", return_value={"email_source": "remail"}), \
             patch.object(live_service.db, "update_account_liveness"), \
             patch.object(live_service, "_append_log"), \
             patch.object(live_service, "resolve_plan_check_route", side_effect=route_for), \
             patch.object(live_service, "open_plan_check_proxy", side_effect=lambda route, selected, timeout: (selected, None)), \
             patch("config.proxy.PLAN_CHECK_PROXY", [proxy_a, proxy_b]), \
             patch("config.proxy.LIVE_CHECK_MAX_ATTEMPTS", 3), \
             patch("config.proxy.LIVE_CHECK_RETRY_DELAY", 0.0), \
             patch.object(live_service.random, "choice", return_value=proxy_b), \
             patch.object(live_service, "check_account_liveness", side_effect=[failed, success]) as check:
            result = live_service._run_live_check(
                account_id=1,
                email="user@example.com",
                proxy=None,
                trigger="manual",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(check.call_args_list[0].kwargs["proxy"], proxy_a)
        self.assertEqual(check.call_args_list[0].kwargs["email_source"], "remail")
        self.assertEqual(check.call_args_list[1].kwargs["proxy"], proxy_b)
        self.assertEqual(check.call_args_list[1].kwargs["email_source"], "remail")
        self.assertIsNot(
            check.call_args_list[0].kwargs["fingerprint_state"],
            check.call_args_list[1].kwargs["fingerprint_state"],
        )
        self.assertTrue(slot.released)

    def test_service_does_not_use_direct_when_only_one_pool_proxy_exists(self):
        slot = _DummyQueueSlot()
        failed = {"ok": False, "status": "failed", "error": "HTTP Error 403: blocked"}
        proxy_url = "socks5://only-proxy.example:1080"
        route = {
            "proxy": proxy_url,
            "network_route": "proxy",
            "proxy_mode": "proxy",
            "proxy_used": proxy_url,
            "upstream_proxy": "",
            "upstream_proxy_used": None,
            "proxy_fallback_reason": None,
        }
        with patch.object(live_service, "_QUEUE_SLOTS", slot), \
             patch.object(live_service.db, "mark_account_live_check_running", return_value=True), \
             patch.object(live_service.db, "get_account", return_value={}), \
             patch.object(live_service.db, "update_account_liveness"), \
             patch.object(live_service, "_append_log"), \
             patch.object(live_service, "resolve_plan_check_route", return_value=route), \
             patch.object(live_service, "open_plan_check_proxy", side_effect=lambda route, selected, timeout: (selected, None)), \
             patch("config.proxy.PLAN_CHECK_PROXY", [proxy_url]), \
             patch("config.proxy.LIVE_CHECK_MAX_ATTEMPTS", 3), \
             patch("config.proxy.LIVE_CHECK_RETRY_DELAY", 0.0), \
             patch.object(live_service, "check_account_liveness", return_value=failed) as check:
            result = live_service._run_live_check(
                account_id=1, email="user@example.com", proxy=None, trigger="manual",
            )

        self.assertFalse(result["ok"])
        self.assertEqual(check.call_count, 3)
        self.assertEqual(result["attempts"], 3)
        self.assertTrue(all(entry.kwargs["proxy"] == proxy_url for entry in check.call_args_list))
        self.assertTrue(check.call_args_list[1].kwargs["fingerprint_state"]["force_fresh"])
        self.assertTrue(slot.released)

    def test_direct_success_clears_previous_proxy_record(self):
        row = {"id": 1, "email": "user@example.com", "live_check_proxy_used": "old-proxy"}
        liveness.db._save_collection("accounts", [row])
        liveness.db.update_account_liveness(1, {
            "ok": True, "access_token": "new-token", "proxy_used": None,
        })
        row = liveness.db.get_account(1)
        self.assertIsNone(row["live_check_proxy_used"])
        self.assertEqual(row["access_token"], "new-token")

    def test_credential_and_transient_errors_never_mark_account_dead(self):
        for status, code, expected in (
            (401, "invalid_username_or_password", "密码"),
            (401, "invalid_otp", "验证码"),
            (403, "", "认证请求被拒绝"),
            (429, "rate_limit_exceeded", "限流"),
        ):
            with self.subTest(status=status, code=code):
                error = RuntimeError("HTTP failure")
                error.response = SimpleNamespace(
                    status_code=status, text='{"error":{"code":"' + code + '"}}',
                )
                result = liveness._failure_result(error, "now")
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["http_status"], status)
                self.assertIn(expected, result["error"])
                self.assertEqual(result["retryable"], status in {403, 429})

    def test_dead_account_http_response_is_not_retried(self):
        error = RuntimeError("HTTP 403")
        error.response = SimpleNamespace(
            status_code=403, text='{"error":{"code":"account_deactivated"}}',
        )
        self.assertFalse(liveness._is_retryable_network_error(error))


if __name__ == "__main__":
    unittest.main()
