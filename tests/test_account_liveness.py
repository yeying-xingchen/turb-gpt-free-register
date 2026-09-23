# -*- coding: utf-8 -*-
import tempfile
import unittest
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


class _HomepageResponse:
    def __init__(self, status_code, url="https://chatgpt.com/"):
        self.status_code = status_code
        self.url = url
        self.text = ""


class _HomepageSession:
    def __init__(self):
        self.calls = []
        self.reset_count = 0
        self.responses = [
            _HomepageResponse(403),
            _HomepageResponse(200),
        ]

    def get_chatgpt_navigate_headers(self, **kwargs):
        return dict(kwargs)

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)

    def reset_circuit_breaker(self):
        self.reset_count += 1


class _DummyQueueSlot:
    def __init__(self):
        self.released = False

    def release(self):
        self.released = True


class AccountLivenessTests(unittest.TestCase):
    def setUp(self):
        _DummyBrowserSession.created = []

    def test_homepage_warmup_retries_403_and_keeps_same_session(self):
        session = _HomepageSession()
        with patch.object(liveness.time, "sleep") as sleep:
            self.assertTrue(liveness._warm_chatgpt_homepage(session, attempts=2))

        self.assertEqual([call[0] for call in session.calls], [
            "https://chatgpt.com/",
            "https://chatgpt.com/",
        ])
        self.assertEqual(session.calls[0][1]["headers"]["referer"], "")
        self.assertTrue(session.calls[0][1]["headers"]["user_initiated"])
        self.assertEqual(session.reset_count, 1)
        sleep.assert_called_once_with(1.0)

    def test_login_warmup_visits_homepage_before_auth_login(self):
        events = []
        session = MagicMock()
        response = SimpleNamespace(
            status_code=200,
            url="https://chatgpt.com/auth/login",
            text="",
            raise_for_status=lambda: None,
        )
        session.get_chatgpt_navigate_headers.return_value = {}
        session.get.side_effect = lambda url, **_kwargs: (events.append(url) or response)
        with patch.object(liveness, "_warm_chatgpt_homepage", side_effect=lambda *_args, **_kwargs: events.append("homepage")), \
             patch("core.chatgpt_bootstrap.anonymous_bootstrap", side_effect=lambda *_args, **_kwargs: events.append("anonymous")), \
             patch.object(liveness, "get_providers", side_effect=lambda *_args, **_kwargs: events.append("providers")), \
             patch.object(liveness, "probe_auth_session", side_effect=lambda *_args, **_kwargs: events.append("session")), \
             patch.object(liveness, "_clear_optional_bootstrap_circuit"):
            liveness._warm_login_fingerprint_context(session)

        self.assertEqual(events, [
            "homepage",
            "https://chatgpt.com/auth/login",
            "anonymous",
            "providers",
            "session",
        ])

    def test_authenticated_warmup_visits_homepage_before_bootstrap(self):
        events = []
        session = MagicMock()
        with patch.object(liveness, "_warm_chatgpt_homepage", side_effect=lambda *_args, **_kwargs: events.append("homepage")), \
             patch("core.chatgpt_bootstrap.authenticated_bootstrap", side_effect=lambda *_args, **_kwargs: events.append("bootstrap")), \
             patch.object(liveness, "_clear_optional_bootstrap_circuit"):
            liveness._warm_authenticated_session(session, "access-token")

        self.assertEqual(events, ["homepage", "bootstrap"])

    def test_full_web_flow_warms_auth_document_before_authorize(self):
        events = []
        session = _DummyBrowserSession(proxy="")
        session_info = {
            "accessToken": "fresh-token",
            "user": {"id": "user-1"},
            "account": {"planType": "free"},
        }
        with patch.object(
            liveness,
            "_network_preflight_with_retry",
            side_effect=lambda *_args, **_kwargs: (events.append("preflight") or (session, "authorize")),
        ), patch.object(
            liveness,
            "_warm_auth_document_for_reauth",
            side_effect=lambda *_args, **_kwargs: events.append("auth-document"),
        ), patch.object(
            liveness,
            "follow_authorize",
            side_effect=lambda *_args, **_kwargs: (events.append("authorize") or "https://auth.openai.com/email-verification"),
        ), patch.object(
            liveness,
            "_login_via_password_or_otp",
            side_effect=lambda *_args, **_kwargs: (events.append("password-or-otp") or session_info),
        ), patch.object(
            liveness,
            "human_delay",
            side_effect=lambda label: events.append(f"delay:{label}"),
        ), patch.object(liveness.time, "time", return_value=123.0):
            result_session, result = liveness._login_via_full_web_flow(
                "user@example.com",
                "",
                email_source="remail",
                fingerprint_state={},
            )

        self.assertIs(result_session, session)
        self.assertEqual(result, session_info)
        self.assertEqual(events, [
            "preflight",
            "delay:api",
            "auth-document",
            "authorize",
            "delay:navigate",
            "password-or-otp",
        ])

    def test_stored_access_token_with_totp_uses_full_web_mfa_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = _DummyBrowserSession(proxy="")
            fresh_info = {
                "accessToken": "fresh-token",
                "user": {"id": "user-1"},
                "account": {"planType": "free"},
            }
            state = {}
            with patch.object(liveness, "_LOG_DIR", Path(tmp)), \
                 patch.object(liveness, "_stored_access_token", return_value="old-token"), \
                 patch.object(liveness, "_account_totp_secret", return_value="totp-secret"), \
                 patch.object(liveness, "_warm_authenticated_session") as warm, \
                 patch.object(liveness, "_login_via_full_web_flow", return_value=(session, fresh_info)) as full_login:
                result = liveness.check_account_liveness(
                    "user@example.com",
                    proxy="",
                    fingerprint_state=state,
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["access_token"], "fresh-token")
        warm.assert_not_called()
        full_login.assert_called_once_with(
            "user@example.com",
            "",
            email_source=None,
            fingerprint_state=state,
        )

    def test_reauth_flow_matches_complete_at_stage_order(self):
        events = []
        session = _DummyBrowserSession(proxy="")
        session_info = {"accessToken": "fresh-token"}

        def trigger(*args, **kwargs):
            events.append("csrf-signin")
            self.assertEqual(kwargs, {"callback_url": "https://chatgpt.com/"})
            return "authorize"

        def follow(*args, **kwargs):
            events.append("authorize")
            self.assertEqual(kwargs, {"warm_auth_document": False})
            return "https://auth.openai.com/email-verification"

        def validate(*args, **kwargs):
            events.append("otp")
            self.assertEqual(args[2], 123.0)
            return "continue"

        with patch.object(liveness, "_trigger_reauth_with_retry", side_effect=trigger), \
             patch.object(liveness, "_warm_auth_document_for_reauth", side_effect=lambda *_args: events.append("auth-document")), \
             patch.object(liveness, "_follow_reauth_with_retry", side_effect=follow), \
             patch.object(liveness, "_validate_reauth_with_retry", side_effect=validate), \
             patch.object(liveness, "_follow_continue_and_fetch", side_effect=lambda *_args, **_kwargs: (events.append("callback-session") or session_info)), \
             patch.object(liveness, "human_delay", side_effect=lambda label: events.append(f"delay:{label}")), \
             patch.object(liveness.time, "time", return_value=123.0):
            result = liveness._login_via_reauth(session, "user@example.com")

        self.assertEqual(result, session_info)
        self.assertEqual(events, [
            "csrf-signin",
            "delay:api",
            "auth-document",
            "authorize",
            "delay:navigate",
            "otp",
            "delay:api",
            "callback-session",
        ])

    def test_reauth_rejects_non_email_verification_landing(self):
        session = _DummyBrowserSession(proxy="")
        with patch.object(liveness, "_trigger_reauth_with_retry", return_value="authorize") as trigger, \
             patch.object(liveness, "_follow_reauth_with_retry", return_value="https://chatgpt.com/"), \
             patch.object(liveness, "_validate_reauth_with_retry") as validate, \
             patch.object(liveness, "human_delay"):
            with self.assertRaisesRegex(RuntimeError, "未进入邮箱验证页面"):
                liveness._login_via_reauth(session, "user@example.com")

        trigger.assert_called_once_with(
            session,
            "user@example.com",
            callback_url="https://chatgpt.com/",
        )
        validate.assert_not_called()

    def test_preflight_preserves_explicit_direct_route_and_skips_providers(self):
        with patch.object(liveness, "BrowserSession", _DummyBrowserSession), \
             patch.object(liveness, "_warm_login_fingerprint_context"), \
             patch.object(liveness, "probe_auth_session"), \
             patch.object(liveness, "get_csrf_token", return_value="csrf"), \
             patch.object(liveness, "signin_openai", return_value="https://auth.example/authorize"):
            session, authorize_url = liveness._network_preflight_with_retry(
                "user@example.com", "", max_attempts=1
            )

        self.assertEqual(session.proxy, "")
        self.assertEqual(authorize_url, "https://auth.example/authorize")
        self.assertTrue(
            session.kwargs["fingerprint_seed"].startswith("live-check:user@example.com:")
        )

    def test_preflight_retries_with_same_session_when_csrf_is_blocked(self):
        csrf_errors = [RuntimeError("HTTP Error 403"), "csrf"]
        with patch.object(liveness, "BrowserSession", _DummyBrowserSession), \
             patch.object(liveness, "_warm_login_fingerprint_context"), \
             patch.object(liveness, "probe_auth_session"), \
             patch.object(liveness, "get_csrf_token", side_effect=csrf_errors), \
             patch.object(liveness, "signin_openai", return_value="authorize"), \
             patch.object(liveness.time, "sleep"):
            session, _ = liveness._network_preflight_with_retry(
                "user@example.com", None, max_attempts=2
            )

        self.assertIs(_DummyBrowserSession.created[-1], session)
        # 403 下发的 CF Cookie 必须留在同一 Cookie Jar 中，不能每轮新建会话。
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
        self.assertEqual(first_kwargs["fingerprint_seed"], second_kwargs["fingerprint_seed"])
        self.assertTrue(str(first_kwargs["fingerprint_seed"]).startswith("live-check:user@example.com:"))

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

    def test_service_403_fallback_really_uses_direct_connection(self):
        slot = _DummyQueueSlot()
        failed = {"ok": False, "status": "failed", "error": "HTTP Error 403: blocked"}
        success = {"ok": True, "status": "live", "access_token": "new-token"}
        with patch.object(live_service, "_QUEUE_SLOTS", slot), \
             patch.object(live_service.db, "mark_account_live_check_running", return_value=True), \
             patch.object(live_service.db, "get_account", return_value={"email_source": "remail"}), \
             patch.object(live_service.db, "update_account_liveness"), \
             patch.object(live_service, "_append_log"), \
             patch.object(live_service, "resolve_plan_check_route", return_value={
                 "proxy": "socks5://proxy.example:1080",
                 "network_route": "proxy",
                 "proxy_mode": "auto",
             }), \
             patch.object(live_service, "check_account_liveness", side_effect=[failed, success]) as check:
            result = live_service._run_live_check(
                account_id=1,
                email="user@example.com",
                proxy=None,
                trigger="manual",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(check.call_args_list[0].kwargs["proxy"], "socks5://proxy.example:1080")
        self.assertEqual(check.call_args_list[0].kwargs["email_source"], "remail")
        self.assertEqual(check.call_args_list[1].kwargs["proxy"], "")
        self.assertEqual(check.call_args_list[1].kwargs["email_source"], "remail")
        self.assertIsNot(
            check.call_args_list[0].kwargs["fingerprint_state"],
            check.call_args_list[1].kwargs["fingerprint_state"],
        )
        self.assertTrue(slot.released)

    def test_service_does_not_fallback_for_unclassified_business_403(self):
        slot = _DummyQueueSlot()
        failed = {
            "ok": False,
            "status": "failed",
            "http_status": 403,
            "error_code": "invalid_otp",
            "error": "MFA challenge required",
        }
        with patch.object(live_service, "_QUEUE_SLOTS", slot), \
             patch.object(live_service.db, "mark_account_live_check_running", return_value=True), \
             patch.object(live_service.db, "get_account", return_value={}), \
             patch.object(live_service.db, "update_account_liveness"), \
             patch.object(live_service, "_append_log"), \
             patch.object(live_service, "resolve_plan_check_route", return_value={
                 "proxy": "socks5://proxy.example:1080",
                 "network_route": "proxy",
                 "proxy_mode": "auto",
             }), \
             patch.object(live_service, "check_account_liveness", return_value=failed) as check, \
             patch.object(live_service, "_run_playwright_live_check") as browser:
            result = live_service._run_live_check(
                account_id=4,
                email="user@example.com",
                proxy=None,
                trigger="manual",
            )

        self.assertEqual(result, failed)
        check.assert_called_once()
        browser.assert_not_called()
        self.assertTrue(slot.released)

    def test_service_403_retries_with_cf_browser_after_protocol_routes(self):
        slot = _DummyQueueSlot()
        failed = {
            "ok": False,
            "status": "failed",
            "http_status": 403,
            "error_code": "edge_http",
            "error": "HTTP Error 403: Cloudflare blocked",
        }
        browser_success = {"ok": True, "status": "live", "access_token": "new-token"}
        with patch.object(live_service, "_QUEUE_SLOTS", slot), \
             patch.object(live_service.db, "mark_account_live_check_running", return_value=True), \
             patch.object(live_service.db, "get_account", return_value={"email_source": "remail"}), \
             patch.object(live_service.db, "update_account_liveness"), \
             patch.object(live_service, "_append_log"), \
             patch.object(live_service, "resolve_plan_check_route", return_value={
                  "proxy": "socks5://proxy.example:1080",
                  "network_route": "proxy",
                  "proxy_mode": "auto",
              }), \
             patch.object(live_service, "_live_check_driver", return_value="auto"), \
             patch.object(live_service, "check_account_liveness", side_effect=[failed, failed]) as check, \
             patch.object(live_service, "_run_playwright_live_check", return_value=browser_success) as browser:
            result = live_service._run_live_check(
                account_id=2,
                email="user@example.com",
                proxy=None,
                trigger="manual",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(check.call_args_list[0].kwargs["proxy"], "socks5://proxy.example:1080")
        self.assertEqual(check.call_args_list[1].kwargs["proxy"], "")
        browser.assert_called_once_with("user@example.com", "", "remail")
        self.assertTrue(slot.released)

    def test_protocol_driver_does_not_fallback_to_playwright(self):
        slot = _DummyQueueSlot()
        failed = {
            "ok": False,
            "status": "failed",
            "http_status": 403,
            "error_code": "edge_http",
            "error": "HTTP Error 403: Cloudflare blocked",
        }
        with patch.object(live_service, "_QUEUE_SLOTS", slot), \
             patch.object(live_service.db, "mark_account_live_check_running", return_value=True), \
             patch.object(live_service.db, "get_account", return_value={"email_source": "remail"}), \
             patch.object(live_service.db, "update_account_liveness"), \
             patch.object(live_service, "_append_log"), \
             patch.object(live_service, "resolve_plan_check_route", return_value={
                 "proxy": "socks5://proxy.example:1080",
                 "network_route": "proxy",
                 "proxy_mode": "proxy",
             }), \
             patch.object(live_service, "_live_check_driver", return_value="protocol"), \
             patch.object(live_service, "check_account_liveness", return_value=failed), \
             patch.object(live_service, "_run_playwright_live_check") as browser:
            result = live_service._run_live_check(
                account_id=3,
                email="user@example.com",
                proxy=None,
                trigger="manual",
            )

        self.assertEqual(result, failed)
        browser.assert_not_called()
        self.assertTrue(slot.released)


if __name__ == "__main__":
    unittest.main()
