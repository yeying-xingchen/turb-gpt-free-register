# -*- coding: utf-8 -*-
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import core.playwright_auth as playwright_auth
import core.live_check_service as live_service


class _FakePage:
    def __init__(self, url="https://chatgpt.com/"):
        self.url = url
        self.events = []
        self.keyboard = SimpleNamespace(press=lambda *_args, **_kwargs: None)

    def is_closed(self):
        return False

    def close(self):
        self.events.append("page")


class _FakeContext:
    def __init__(self, page):
        self.page = page
        self.events = page.events
        self.request = None

    def close(self):
        self.events.append("context")


class _FakeBrowser:
    def __init__(self, events):
        self.events = events

    def close(self):
        self.events.append("browser")


class _SessionResponse:
    def __init__(self, status=403, payload=None, headers=None, text=""):
        self.status = status
        self.headers = headers or {}
        self._payload = payload if payload is not None else {"error": "blocked"}
        self.text = text

    def json(self):
        return self._payload


class _Request:
    def __init__(self, responses=None):
        self.responses = list(responses or [_SessionResponse()])
        self.calls = 0

    def get(self, *_args, **_kwargs):
        self.calls += 1
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


class _NavigationPage:
    def __init__(self, statuses, headers=None):
        self.statuses = list(statuses)
        self.headers = list(headers or [{}])
        self.goto_calls = []
        self.waits = []
        self.url = "https://chatgpt.com/"

    def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))
        self.url = url
        status = self.statuses.pop(0)
        headers = self.headers.pop(0) if len(self.headers) > 1 else self.headers[0]
        return SimpleNamespace(status=status, headers=headers)

    def wait_for_timeout(self, milliseconds):
        self.waits.append(milliseconds)


class PlaywrightAuthUnitTests(unittest.TestCase):
    def test_proxy_conversion_and_redaction(self):
        with patch.object(playwright_auth, "_effective_proxy", return_value="socks5://user:secret@proxy.example:3000"):
            self.assertEqual(
                playwright_auth._proxy_for_playwright("ignored"),
                {
                    "server": "socks5://proxy.example:3000",
                    "username": "user",
                    "password": "secret",
                },
            )
        safe = playwright_auth._safe_error_text(
            "https://user:pass@auth.openai.com/authorize?code=secret&state=oauth "
            "password=supersecret"
        )
        self.assertNotIn("secret", safe)
        self.assertNotIn("supersecret", safe)
        self.assertIn("auth.openai.com/authorize", safe)

    def test_start_url_rejects_query_and_external_host(self):
        with patch.object(playwright_auth._cfg, "PLAYWRIGHT_START_URL", "https://chatgpt.com/auth/login?state=secret"):
            with self.assertRaises(playwright_auth.PlaywrightAuthError) as ctx:
                playwright_auth._validated_start_url()
        self.assertEqual(ctx.exception.code, "url_config")
        with patch.object(playwright_auth._cfg, "PLAYWRIGHT_START_URL", "https://evil.example/login"):
            with self.assertRaises(playwright_auth.PlaywrightAuthError):
                playwright_auth._validated_start_url()

    def test_close_order_is_page_context_browser_and_removes_profile_lock(self):
        events = []
        page = _FakePage()
        page.events = events
        context = _FakeContext(page)
        browser = _FakeBrowser(events)
        lock = __import__("threading").Lock()
        lock.acquire()
        playwright_auth._HELD_PROFILE_LOCKS[id(context)] = ("profile", lock)
        playwright_auth._PERSISTENT_CONTEXTS.add(id(context))
        playwright_auth._close_playwright_objects(browser, context, page)
        self.assertEqual(events, ["page", "context", "browser"])
        self.assertFalse(lock.locked())
        self.assertNotIn(id(context), playwright_auth._PERSISTENT_CONTEXTS)
        self.assertNotIn(id(context), playwright_auth._HELD_PROFILE_LOCKS)

    def test_session_403_is_structured_and_does_not_leak_response(self):
        page = _FakePage("https://chatgpt.com/")
        context = SimpleNamespace(request=_Request())
        result = playwright_auth._read_session(context, page)
        self.assertEqual(result["_http_status"], 403)
        self.assertEqual(result["_error_code"], "edge_http")
        with patch.object(playwright_auth, "_settle_edge_challenge"):
            with self.assertRaises(playwright_auth.PlaywrightAuthError) as ctx:
                playwright_auth._wait_session(context, page, timeout=5)
        self.assertEqual(ctx.exception.code, "edge_http")
        self.assertEqual(ctx.exception.http_status, 403)

    def test_session_403_retries_after_same_context_document_recovery(self):
        page = _NavigationPage([200])
        context = SimpleNamespace(request=_Request([
            _SessionResponse(403),
            _SessionResponse(200, payload={"accessToken": "fresh-token"}),
        ]))
        with patch.object(playwright_auth, "_settle_edge_challenge"):
            result = playwright_auth._wait_session(context, page, timeout=5)
        self.assertEqual(result["accessToken"], "fresh-token")
        self.assertEqual(context.request.calls, 2)
        self.assertEqual(len(page.goto_calls), 1)

    def test_session_challenge_header_is_structured_and_bounded(self):
        page = _NavigationPage([200, 200])
        context = SimpleNamespace(request=_Request([
            _SessionResponse(200, headers={"cf-mitigated": "challenge"}),
            _SessionResponse(200, headers={"cf-mitigated": "challenge"}),
            _SessionResponse(200, headers={"cf-mitigated": "challenge"}),
        ]))
        result = playwright_auth._read_session(context, page)
        self.assertEqual(result["_error_code"], "challenge_required")
        with patch.object(playwright_auth, "_settle_edge_challenge"):
            with self.assertRaises(playwright_auth.PlaywrightAuthError) as ctx:
                playwright_auth._wait_session(context, page, timeout=5)
        self.assertEqual(ctx.exception.code, "challenge_required")
        # One direct probe above the wait plus the bounded two retries.
        self.assertEqual(context.request.calls, 4)

    def test_session_dead_account_body_is_not_classified_as_edge(self):
        page = _FakePage("https://chatgpt.com/")
        response = _SessionResponse(
            403,
            text='{"error":{"code":"account_deactivated"}}',
        )
        context = SimpleNamespace(request=_Request([response]))
        result = playwright_auth._read_session(context, page)
        self.assertEqual(result["_error_code"], "account_deactivated")
        with self.assertRaises(playwright_auth.PlaywrightAuthError) as ctx:
            playwright_auth._wait_session(context, page, timeout=5)
        self.assertEqual(ctx.exception.code, "account_deactivated")
        self.assertEqual(context.request.calls, 2)

    def test_initial_edge_navigation_retries_in_same_browser_context(self):
        page = _NavigationPage([403, 200])
        with patch.object(playwright_auth, "_settle_edge_challenge") as settle:
            result = playwright_auth._goto_page_with_edge_retry(
                page, "https://chatgpt.com/", phase="homepage", attempts=3
            )
        self.assertEqual(result.status, 200)
        self.assertEqual(len(page.goto_calls), 2)
        settle.assert_called_once_with(page, 5.0)

    def test_cf_mitigated_200_navigation_retries_without_token_injection(self):
        page = _NavigationPage(
            [200, 200],
            headers=[{"cf-mitigated": "challenge"}, {}],
        )
        with patch.object(playwright_auth, "_settle_edge_challenge") as settle:
            result = playwright_auth._goto_page_with_edge_retry(
                page, "https://chatgpt.com/", phase="homepage", attempts=2
            )
        self.assertEqual(result.status, 200)
        self.assertEqual(len(page.goto_calls), 2)
        settle.assert_called_once_with(page, 5.0)

    def test_initial_non_edge_http_error_is_not_retried(self):
        page = _NavigationPage([400, 200])
        with self.assertRaises(playwright_auth.PlaywrightAuthError) as ctx:
            playwright_auth._goto_page_with_edge_retry(
                page, "https://chatgpt.com/", phase="homepage", attempts=3
            )
        self.assertEqual(ctx.exception.code, "http_error")
        self.assertEqual(ctx.exception.http_status, 400)
        self.assertEqual(len(page.goto_calls), 1)

    def test_state_timeout_is_structured(self):
        page = _FakePage()
        with patch.object(playwright_auth, "_auth_state", return_value="other"):
            with self.assertRaises(playwright_auth.PlaywrightAuthError) as ctx:
                playwright_auth._wait_state(page, {"chatgpt"}, timeout=0.01)
        self.assertEqual(ctx.exception.code, "state_timeout")
        self.assertEqual(ctx.exception.phase, "state")

    def test_state_challenge_waits_then_reloads_same_page(self):
        page = _FakePage()
        with patch.object(playwright_auth, "_auth_state", side_effect=["edge_challenge", "chatgpt"]), \
             patch.object(playwright_auth, "_edge_challenge_wait_seconds", return_value=0), \
             patch.object(playwright_auth, "_reload_page_after_edge") as reload:
            state = playwright_auth._wait_state(page, {"chatgpt"}, timeout=1)
        self.assertEqual(state, "chatgpt")
        reload.assert_called_once_with(page)

    def test_liveness_failure_keeps_structured_error_and_redacts_proxy(self):
        error = playwright_auth.PlaywrightAuthError(
            "HTTP 403 https://proxy-user:proxy-pass@edge.example/path?token=secret",
            code="edge_http", http_status=403, phase="login",
        )
        with patch.object(playwright_auth, "_effective_proxy", return_value="http://user:pass@proxy.example:8080"), \
             patch.object(playwright_auth, "run_playwright_liveness", side_effect=error):
            result = playwright_auth.check_playwright_liveness("user@example.com", proxy="http://user:pass@proxy.example:8080")
        self.assertEqual(result["error_code"], "edge_http")
        self.assertEqual(result["http_status"], 403)
        self.assertEqual(result["phase"], "login")
        self.assertEqual(result["proxy_used"], "http://***:***@proxy.example:8080")
        self.assertNotIn("proxy-pass", result["error"])

    def test_driver_aliases_and_status_based_network_detection(self):
        with patch("config.playwright.LIVE_CHECK_DRIVER", "pw"):
            self.assertEqual(live_service._live_check_driver(), "playwright")
        with patch("config.playwright.LIVE_CHECK_DRIVER", "cf"):
            self.assertEqual(live_service._live_check_driver(), "playwright")
        with patch("config.playwright.LIVE_CHECK_DRIVER", "cloudflare"):
            self.assertEqual(live_service._live_check_driver(), "playwright")
        self.assertTrue(live_service._is_network_failure({
            "ok": False, "status": "failed", "http_status": 403, "error": "blocked",
        }))
        self.assertTrue(live_service._is_network_failure({
            "ok": False, "status": "failed", "error_code": "challenge_required", "error": "edge challenge",
        }))
        self.assertFalse(live_service._is_network_failure({
            "ok": False, "status": "failed", "http_status": 403,
            "error": "account has been deactivated",
        }))
        self.assertFalse(live_service._is_network_failure({
            "ok": False, "status": "failed", "error": "MFA challenge required",
        }))
        self.assertFalse(live_service._is_network_failure({
            "ok": False, "status": "deactivated", "http_status": 403, "error": "account banned",
        }))


if __name__ == "__main__":
    unittest.main()
