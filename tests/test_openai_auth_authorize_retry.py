# -*- coding: utf-8 -*-
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import core.openai_auth as openai_auth


class _Session:
    def __init__(self):
        self.calls = 0
        self.urls = []
        self.reset_count = 0
        self.cookie_jar_identity = object()

    def get_auth_navigate_headers(self, **_kwargs):
        return {"sec-fetch-mode": "navigate"}

    def get(self, url, *_args, **_kwargs):
        self.calls += 1
        self.urls.append(url)
        if self.calls == 1:
            error = RuntimeError("HTTP Error 403")
            error.response = SimpleNamespace(status_code=403)
            raise error
        return SimpleNamespace(
            status_code=200,
            url="https://auth.openai.com/email-verification",
            raise_for_status=lambda: None,
        )

    def reset_circuit_breaker(self):
        self.reset_count += 1


class AuthorizeRetryTests(unittest.TestCase):
    def test_403_retries_with_same_session_after_cookie_refresh(self):
        session = _Session()
        cookie_jar_identity = session.cookie_jar_identity

        with patch.object(openai_auth, "_interruptible_sleep") as sleep:
            final_url = openai_auth.follow_authorize(
                session,
                "https://auth.openai.com/api/accounts/authorize?state=test",
            )

        self.assertEqual(final_url, "https://auth.openai.com/email-verification")
        self.assertEqual(session.calls, 2)
        self.assertEqual(session.urls, [
            "https://auth.openai.com/api/accounts/authorize?state=test",
            "https://auth.openai.com/api/accounts/authorize?state=test",
        ])
        self.assertEqual(session.reset_count, 1)
        self.assertIs(session.cookie_jar_identity, cookie_jar_identity)
        sleep.assert_called_once_with(1.0)


if __name__ == "__main__":
    unittest.main()
