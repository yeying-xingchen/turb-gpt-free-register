# -*- coding: utf-8 -*-
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db, payment_checker, payment_method_service
from core.payment_checker import QualificationResult


class PaymentCheckerTests(unittest.TestCase):
    def test_proxy_normalization_accepts_legacy_and_curl_forms(self):
        self.assertEqual(
            payment_checker._proxy("host.example:8080:user:pa:ss"),
            "http://user:pa%3Ass@host.example:8080",
        )
        self.assertEqual(
            payment_checker._proxy("curl --proxy host.example:8080 --proxy-user 'u:p@ss'"),
            "http://u:p%40ss@host.example:8080",
        )

    def test_proxy_normalization_rejects_invalid_port(self):
        with self.assertRaises(payment_checker.GCashCheckerError):
            payment_checker._proxy("host.example:65536:user:pass")

    def test_qualification_result_uses_explicit_field_contract(self):
        result = QualificationResult(
            qualified=True,
            checkout_session_id="oaics_private",
            target_channel="gcash",
            processor_entity="gcash",
            payment_method_type="gcash",
            checkout_amount=100,
            checkout_currency="PHP",
            proxy_configured=True,
            evidence="published",
            available_channels=["gcash"],
            channel_availability={"gcash": True},
            account_email="private@example.com",
            access_token="secret-token",
            country="PH",
        )
        public = result.as_dict()
        self.assertTrue(public["qualified"])
        self.assertEqual(public["payment_method_type"], "gcash")
        self.assertNotIn("access_token", public)
        self.assertNotIn("account_email", public)
        self.assertNotIn("checkout_session_id", public)

    def test_custom_checkout_constructor_is_keyword_based(self):
        source = Path(payment_checker.__file__).read_text(encoding="utf-8")
        self.assertIn("payment_method_type=target_channel if available else \"\"", source)
        self.assertNotIn("return QualificationResult(available, meta[\"checkout_session_id\"]", source)


class PaymentDbTests(unittest.TestCase):
    def test_claim_and_result_preserves_summary_on_failure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            accounts = root / "accounts.json"
            accounts.write_text(json.dumps([{"id": 1, "email": "a@example.com", "access_token": "token"}], ensure_ascii=False), encoding="utf-8")
            with patch.object(db, "_ACCOUNTS_JSON", accounts), patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"), patch.object(db, "_TOKENS_TXT", root / "tokens.txt"), patch.object(db, "_VIEWER_HTML", root / "viewer.html"), patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy.json"):
                self.assertTrue(db.claim_account_payment_method_check(1))
                self.assertFalse(db.claim_account_payment_method_check(1))
                self.assertTrue(db.mark_account_payment_method_check_running(1))
                self.assertTrue(db.update_account_payment_method_check(1, result={"ok": True, "checked_at": "2026-01-01T00:00:00", "available_channels": ["gcash"], "regions": [{"name": "gcash", "qualified": True}]}))
                row = db.get_account(1)
                self.assertEqual(row["payment_method_check_status"], "success")
                self.assertEqual(row["payment_method_available_channels"], ["gcash"])
                self.assertTrue(db.claim_account_payment_method_check(1))
                self.assertTrue(db.update_account_payment_method_check(1, result={"ok": False, "error": "temporary failure"}))
                row = db.get_account(1)
                self.assertEqual(row["payment_method_check_status"], "failed")
                self.assertEqual(row["payment_method_available_channels"], ["gcash"])

                self.assertTrue(db.claim_account_payment_method_check(1))
                self.assertTrue(db.update_account_payment_method_check(1, result={
                    "ok": False, "error": "Bearer eyJhbGciOiJ9.abc.def via http://user:pass@proxy:80",
                }))
                row = db.get_account(1)
                self.assertNotIn("eyJhbGci", row["payment_method_check_error"])
                self.assertNotIn("user:pass", row["payment_method_check_error"])


class PaymentServiceTests(unittest.TestCase):
    def test_region_result_does_not_retain_sensitive_fields(self):
        result = QualificationResult(True, "oaics_secret", "gcash", "gcash", "gcash", 1, "PHP", True, "Bearer abc", account_email="a@example.com", access_token="secret")
        safe = payment_method_service._region_result(result, {"name": "gcash", "preset": "gcash", "channel": "gcash", "country": "PH", "currency": "PHP"})
        blob = json.dumps(safe, ensure_ascii=False)
        self.assertNotIn("secret", blob)
        self.assertNotIn("a@example.com", blob)
        self.assertNotIn("oaics_", blob)

    def test_configured_proxies_are_parsed(self):
        with patch.object(payment_method_service, "_setting", side_effect=lambda name, default: {"PAYMENT_METHOD_CHECK_PROXIES": "gcash=http://x\ndefault=http://y"}.get(name, default)):
            self.assertEqual(payment_method_service._proxy_for("gcash"), "http://x")
            self.assertEqual(payment_method_service._proxy_for("card"), "http://y")

    def test_remote_baseurl_api_sends_private_fields_only_to_transport(self):
        class Response:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, limit):
                return json.dumps({
                    "ok": True, "qualified": True, "target_channel": "gcash",
                    "country": "PH", "currency": "PHP", "available_channels": ["gcash"],
                    "channel_availability": {"gcash": True}, "checkout_session_id": "oaics_secret",
                    "access_token": "remote-secret", "account_email": "private@example.com",
                }).encode()

        captured = {}
        def fake_urlopen(req, timeout):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode())
            captured["timeout"] = timeout
            return Response()

        region = {"name": "gcash", "preset": "gcash", "channel": "gcash", "country": "PH", "currency": "PHP"}
        with patch.object(payment_method_service.urllib_request, "urlopen", side_effect=fake_urlopen), patch.object(payment_method_service, "_setting", return_value=""):
            result = asyncio.run(payment_method_service._remote_qualification_check("private-token", "http://user:pass@proxy:80", region, "http://qualification.test/api/gcash/check", 30))
        self.assertEqual(captured["url"], "http://qualification.test/api/gcash/check")
        self.assertEqual(captured["body"]["token"], "private-token")
        self.assertEqual(captured["body"]["proxy"], "http://user:pass@proxy:80")
        self.assertEqual(result["ok"], True)
        safe = payment_method_service._region_result(result, region)
        self.assertNotIn("remote-secret", json.dumps(safe))
        self.assertNotIn("oaics_secret", json.dumps(safe))

    def test_remote_api_url_is_restricted_to_http_api_path(self):
        with patch.object(payment_method_service, "_setting", side_effect=lambda name, default: {
            "PAYMENT_QUALIFICATION_API_BASE": "https://qualification.test",
            "PAYMENT_QUALIFICATION_API_PATH": "/not-an-api",
        }.get(name, default)):
            with self.assertRaises(ValueError):
                payment_method_service._qualification_api_url()

    def test_account_check_uses_remote_api_without_loading_checker(self):
        async def fake_remote(*args, **kwargs):
            return {"ok": True, "qualified": True, "target_channel": "gcash", "country": "PH", "currency": "PHP", "available_channels": ["gcash"], "channel_availability": {"gcash": True}}

        region = {"name": "gcash", "preset": "gcash", "channel": "gcash", "country": "PH", "currency": "PHP", "plan": "plus"}
        with patch.object(payment_method_service, "_qualification_api_url", return_value="http://qualification.test/api/gcash/check"), patch.object(payment_method_service, "_checker", side_effect=AssertionError("local checker should not load")), patch.object(payment_method_service, "_proxy_for", return_value="http://proxy"), patch.object(payment_method_service, "_remote_qualification_check", side_effect=fake_remote):
            result = asyncio.run(payment_method_service._check_account_async("private-token", [region], 1))
        self.assertTrue(result["ok"])
        self.assertEqual(result["checked_regions"], ["gcash"])
        self.assertEqual(result["available_channels"], ["gcash"])


if __name__ == "__main__":
    unittest.main()
