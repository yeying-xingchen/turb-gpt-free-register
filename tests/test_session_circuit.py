# -*- coding: utf-8 -*-
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.session import BrowserSession


class SessionCircuitBreakerTests(unittest.TestCase):
    @staticmethod
    def _session():
        session = object.__new__(BrowserSession)
        session.blocked_until = 0.0
        session.blocked_reason = ""
        return session

    def test_403_observes_cookie_before_opening_circuit(self):
        session = self._session()
        events = []

        def observe_cf_cookie_changes(url):
            events.append(("cookie", url, session.blocked_until, session.blocked_reason))

        session._observe_cf_cookie_changes = observe_cf_cookie_changes
        response = SimpleNamespace(status_code=403, headers={})

        with patch("core.session.time.time", return_value=100.0):
            result = session._observe_response_for_circuit_breaker(
                response, "https://auth.openai.com/api/accounts/authorize"
            )

        self.assertIs(result, response)
        self.assertEqual(events, [
            (
                "cookie",
                "https://auth.openai.com/api/accounts/authorize",
                0.0,
                "",
            ),
        ])
        self.assertEqual(session.blocked_until, 1000.0)
        self.assertEqual(
            session.blocked_reason,
            "HTTP 403 from https://auth.openai.com/api/accounts/authorize",
        )

    def test_429_honors_retry_after_but_caps_cooldown(self):
        session = self._session()
        session._observe_cf_cookie_changes = lambda _url: None

        with patch("core.session.time.time", return_value=200.0):
            session._observe_response_for_circuit_breaker(
                SimpleNamespace(
                    status_code=429,
                    headers={"retry-after": "99999"},
                ),
                "https://chatgpt.com/api/auth/session",
            )

        self.assertEqual(session.blocked_until, 3800.0)
        self.assertEqual(
            session.blocked_reason,
            "HTTP 429 from https://chatgpt.com/api/auth/session",
        )

    def test_non_circuit_response_does_not_change_state(self):
        session = self._session()
        session._observe_cf_cookie_changes = lambda _url: None

        with patch("core.session.time.time", return_value=300.0):
            response = session._observe_response_for_circuit_breaker(
                SimpleNamespace(status_code=500, headers={}),
                "https://auth.openai.com/",
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(session.blocked_until, 0.0)
        self.assertEqual(session.blocked_reason, "")

    def test_reset_circuit_breaker_allows_next_request_check(self):
        session = self._session()
        session.blocked_until = 9999.0
        session.blocked_reason = "HTTP 403 from test"

        session.reset_circuit_breaker()
        session._raise_if_circuit_open()

        self.assertEqual(session.blocked_until, 0.0)
        self.assertEqual(session.blocked_reason, "")


if __name__ == "__main__":
    unittest.main()
