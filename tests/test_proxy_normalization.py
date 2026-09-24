# -*- coding: utf-8 -*-
import unittest
from unittest.mock import MagicMock, patch

from config.proxy import normalize_proxy_list, normalize_proxy_url, redact_proxy_url
from core.session import BrowserSession


class ProxyNormalizationTests(unittest.TestCase):
    def test_legacy_host_port_user_password_becomes_http_url(self):
        self.assertEqual(
            normalize_proxy_url("us.1024proxy.io:3000:user-region-VN:password"),
            "http://user-region-VN:password@us.1024proxy.io:3000",
        )

    def test_explicit_scheme_and_legacy_authority_are_normalized(self):
        self.assertEqual(
            normalize_proxy_url("socks5://host.example:1080:user:pa:ss"),
            "socks5://user:pa%3Ass@host.example:1080",
        )

    def test_standard_url_and_scheme_less_authority_are_supported(self):
        self.assertEqual(
            normalize_proxy_url("http://user:pa%40ss@host.example:8080"),
            "http://user:pa%40ss@host.example:8080",
        )
        self.assertEqual(
            normalize_proxy_url("user:pa@ss@host.example:8080"),
            "http://user:pa%40ss@host.example:8080",
        )

    def test_standard_url_with_numeric_password_component_is_not_legacy(self):
        self.assertEqual(
            normalize_proxy_url("http://user:8080:foo@host.example:8080"),
            "http://user:8080%3Afoo@host.example:8080",
        )
        self.assertEqual(
            normalize_proxy_url("http://user:1234:foo@host.example:8080"),
            "http://user:1234%3Afoo@host.example:8080",
        )

    def test_credentials_are_encoded_once(self):
        self.assertEqual(
            normalize_proxy_url("host.example:8080:u ser:p@ss:#=:%"),
            "http://u%20ser:p%40ss%3A%23%3D%3A%25@host.example:8080",
        )
        self.assertEqual(
            normalize_proxy_url("host.example:8080:user:p@ss:with:colon"),
            "http://user:p%40ss%3Awith%3Acolon@host.example:8080",
        )
        self.assertEqual(
            normalize_proxy_url("host.example:8080:user:p@ss"),
            "http://user:p%40ss@host.example:8080",
        )
        self.assertEqual(
            normalize_proxy_url("host.example:8080:user:1234"),
            "http://user:1234@host.example:8080",
        )

    def test_bracketed_ipv6_and_zone_id_are_supported(self):
        self.assertEqual(
            normalize_proxy_url("[2001:db8::1]:8080:user:pass"),
            "http://user:pass@[2001:db8::1]:8080",
        )
        self.assertEqual(
            normalize_proxy_url("http://user:pass@[fe80::1%25eth0]:8080"),
            "http://user:pass@[fe80::1%25eth0]:8080",
        )

    def test_host_port_gets_default_scheme(self):
        self.assertEqual(
            normalize_proxy_url("127.0.0.1:7897"),
            "http://127.0.0.1:7897",
        )

    def test_username_password_host_port_format_encodes_credentials(self):
        self.assertEqual(
            normalize_proxy_url("user:p@ ss:proxy.example.test:8080"),
            "http://user:p%40%20ss@proxy.example.test:8080",
        )

    def test_existing_scheme_is_preserved(self):
        self.assertEqual(
            normalize_proxy_url("socks5h://127.0.0.1:7897"),
            "socks5h://127.0.0.1:7897",
        )

    def test_direct_and_invalid_values(self):
        self.assertEqual(normalize_proxy_url(None), "")
        self.assertEqual(normalize_proxy_url(""), "")
        for value in (
            "host.example:0:user:pass",
            "host.example:65536:user:pass",
            "host.example:not-a-port:user:pass",
            "http://host.example:8080/path",
            "http://host.example:8080?query=1",
            "ftp://host.example:8080",
            "2001:db8::1:8080:user:pass",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_proxy_url(value)

    def test_redaction_never_contains_credentials(self):
        value = "host.example:8080:my-user:my-secret"
        redacted = redact_proxy_url(value)
        self.assertEqual(redacted, "http://***:***@host.example:8080")
        self.assertNotIn("my-user", redacted)
        self.assertNotIn("my-secret", redacted)

    def test_normalize_proxy_list_filters_blank_entries(self):
        self.assertEqual(
            normalize_proxy_list(["", "  ", "127.0.0.1:7897"]),
            ["http://127.0.0.1:7897"],
        )

    def test_browser_session_passes_normalized_proxy_to_curl(self):
        fake_http_session = MagicMock()
        with patch("core.session.Session", return_value=fake_http_session), \
             patch.object(BrowserSession, "_detect_exit_geo", return_value={}), \
             patch.object(BrowserSession, "_enforce_proxy_quality"):
            session = BrowserSession(
                proxy="host.example:8080:user:pass",
                detect_exit_geo=False,
            )

        self.assertEqual(session.proxy, "http://user:pass@host.example:8080")
        self.assertEqual(
            fake_http_session.proxies,
            {
                "http": "http://user:pass@host.example:8080",
                "https": "http://user:pass@host.example:8080",
            },
        )
        self.assertEqual(
            session.fingerprint_summary()["proxy"],
            "http://***:***@host.example:8080",
        )
        session.close()


if __name__ == "__main__":
    unittest.main()
