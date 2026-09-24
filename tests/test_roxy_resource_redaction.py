# -*- coding: utf-8 -*-
"""Focused resource/redaction tests for Roxy/Cloak integration."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from core.roxybrowser_client import (
    RoxyBrowserClient,
    _proxy_url_to_roxy_info,
    _redact_sensitive,
    _redact_text,
)


class _Session:
    def __init__(self):
        self.headers = {}
        self.closed = False

    def close(self):
        self.closed = True


class _Relay:
    def __init__(self):
        self.closed = False
        self.last_error = ""

    def close(self):
        self.closed = True


class RoxyResourceRedactionTests(unittest.TestCase):
    def test_close_and_context_manager_release_session_and_relay(self):
        client = RoxyBrowserClient(api_base="http://127.0.0.1:50000", token="")
        session = _Session()
        relay = _Relay()
        client.http = session
        client._proxy_pool_relay = relay

        with client:
            self.assertIs(client.http, session)
        self.assertTrue(session.closed)
        self.assertTrue(relay.closed)
        self.assertIsNone(client.http)
        self.assertIsNone(client._proxy_pool_relay)
        # close is idempotent, including after context-manager exit.
        client.close()

    def test_redacts_socks_urls_and_tokenized_webdriver_ws_urls(self):
        text = (
            "proxy=socks4a://alice:secret@proxy.example:1080 "
            "proxy2=socks5h://bob:secret2@proxy.example:1081 "
            "webdriver=http://user:pw@127.0.0.1:9515/wd/hub/session/"
            "1234567890abcdef?access_token=webdriver-secret "
            "ws=wss://127.0.0.1:9222/devtools/page/"
            "short-token?token=ws-secret"
        )
        safe = _redact_text(text)
        for secret in ("alice", "secret", "bob", "secret2", "user:pw", "webdriver-secret", "ws-secret", "1234567890abcdef"):
            self.assertNotIn(secret, safe)
        self.assertIn("socks4a://***:***@proxy.example:1080", safe)
        self.assertIn("socks5h://***:***@proxy.example:1081", safe)
        self.assertIn("http://127.0.0.1:9515/wd/hub/session/***", safe)
        self.assertIn("wss://127.0.0.1:9222/devtools/page/***", safe)

    def test_nested_open_result_redaction_does_not_persist_credentials(self):
        value = {
            "webdriver_url": "http://127.0.0.1:9515/wd/hub?token=webdriver-secret",
            "wsEndpoint": "ws://127.0.0.1:9222/devtools/browser/0123456789abcdef",
            "proxyInfo": {"url": "socks5h://u:p@host:1080", "proxyUserName": "alice"},
            "proxy": "host:8080:user:pass",
            "token": {"value": "nested-secret"},
            "password": "do-not-store",
        }
        safe = _redact_sensitive(value)
        self.assertNotIn("webdriver-secret", str(safe))
        self.assertNotIn("0123456789abcdef", str(safe))
        self.assertNotIn("u:p", str(safe))
        self.assertNotIn("alice", str(safe))
        self.assertNotIn("user:pass", str(safe))
        self.assertNotIn("nested-secret", str(safe))
        self.assertEqual(safe["password"], "***")

    def test_roxy_accepts_socks4_and_socks4a_as_socks4(self):
        for scheme in ("socks4", "socks4a"):
            info = _proxy_url_to_roxy_info(f"{scheme}://alice:secret@proxy.example:1080")
            self.assertEqual(info["protocol"], "SOCKS4")
            self.assertEqual(info["proxyCategory"], "SOCKS4")
            self.assertEqual(info["host"], "proxy.example")
            self.assertEqual(info["port"], "1080")
            self.assertEqual(info["proxyUserName"], "alice")
            self.assertEqual(info["proxyPassword"], "secret")

    def test_open_failure_cleans_created_profile_without_result(self):
        client = RoxyBrowserClient(api_base="http://127.0.0.1:50000", token="")
        session = _Session()
        client.http = session
        with patch.object(client, "create_profile", return_value="created-profile"), \
             patch.object(client, "request", side_effect=RuntimeError("malformed open")), \
             patch.object(client, "close_profile") as close_profile, \
             patch.object(client, "delete_profile") as delete_profile:
            with patch("core.roxybrowser_client._cfg.ROXY_OPEN_PATH", "/browser/open/{missing}"):
                with self.assertRaises(KeyError):
                    client.open_profile()
        close_profile.assert_called_once_with("created-profile")
        delete_profile.assert_called_once_with("created-profile")
        self.assertTrue(session.closed)


if __name__ == "__main__":
    unittest.main()
