# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db
from webui.app import (
    ACCOUNT_EXPORT_FIELDS,
    _ACCOUNT_EXPORT_FIELD_LABELS,
    _account_export_field_value,
    create_app,
)


class WebUiAccountExportTests(unittest.TestCase):
    @staticmethod
    def _storage_patches(root: Path) -> dict:
        return {
            "_ACCOUNTS_JSON": root / "accounts.json",
            "_OUTLOOK_JSON": root / "outlook.json",
            "_GENERIC_API_EMAIL_JSON": root / "generic.json",
            "_DOMAIN_EMAIL_JSON": root / "domain.json",
            "_JOBS_JSON": root / "jobs.json",
            "_LEGACY_ACCOUNTS_JSON": root / "legacy-accounts.json",
            "_LEGACY_OUTLOOK_JSON": root / "legacy-outlook.json",
            "_LEGACY_JOBS_JSON": root / "legacy-jobs.json",
            "_LEGACY_SQLITE": root / "legacy.db",
            "_CODEX_DIR": root / "codex_accounts",
            "_CODEX_AGENT_DIR": root / "codex_agent_accounts",
            "_LEGACY_CODEX_EXPORT_STATE": root / "codex-export.json",
            "_SQLITE_READY": False,
            "_SQLITE_READY_PATH": None,
            "_VIEWER_HTML": root / "viewer.html",
            "_ACCOUNTS_TXT": root / "accounts.txt",
            "_TOKENS_TXT": root / "tokens.txt",
        }

    @staticmethod
    def _accounts():
        return [
            {
                "id": 1,
                "email": "one@example.com",
                "access_token": "AT-ONE-SECRET",
                "password": "mail-password-one",
                "registration_password": "login-password-one",
                "totp_secret": "TOTP-ONE-SECRET",
                "email_source": "outlook",
                "plan_type": "free",
                "created_at": "2026-01-01T00:00:00",
            },
            {
                "id": 2,
                "email": "two@example.com",
                "access_token": "AT-TWO-SECRET",
                "password": "mail-password-two",
                "registration_password": "login-password-two",
                "totp_secret": "TOTP-TWO-SECRET",
                "email_source": "imap",
                "plan_type": "plus",
                "created_at": "2026-01-02T00:00:00",
            },
        ]

    def _client(self, root: Path):
        (root / "accounts.json").write_text(json.dumps(self._accounts()), encoding="utf-8")
        return create_app(auth_code="test-auth").test_client()

    def test_email_secret_bulk_is_email_only_and_cross_page_safe(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                client = self._client(root)
                response = client.post(
                    "/api/accounts/secret-bulk",
                    json={"account_ids": [2, 1], "field": "email"},
                    headers={"X-Auth-Code": "test-auth"},
                )
                self.assertEqual(response.status_code, 200)
                body = response.get_json()
                self.assertEqual([item["value"] for item in body["values"]], ["two@example.com", "one@example.com"])
                self.assertNotIn("AT-ONE-SECRET", response.get_data(as_text=True))
                self.assertNotIn("TOTP-ONE-SECRET", response.get_data(as_text=True))

    def test_export_email_password_and_2fa_without_at(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                client = self._client(root)
                base = {
                    "scope": "selected",
                    "account_ids": [1, 2],
                    "fields": ["email", "email_password", "totp_secret"],
                    "format": "txt",
                    "delimiter": "----",
                    "prepare": True,
                }
                denied = client.post("/api/accounts/export", json=base, headers={"X-Auth-Code": "test-auth"})
                self.assertEqual(denied.status_code, 400)
                self.assertIn("敏感字段", denied.get_json()["error"])

                allowed = dict(base, confirm_sensitive=True)
                response = client.post("/api/accounts/export", json=allowed, headers={"X-Auth-Code": "test-auth"})
                self.assertEqual(response.status_code, 200)
                meta = response.get_json()
                self.assertEqual(meta["count"], 2)
                download = client.get(meta["download_url"], headers={"X-Auth-Code": "test-auth"})
                self.assertEqual(download.status_code, 200)
                text = download.get_data().decode("utf-8-sig")
                self.assertIn("one@example.com----mail-password-one----TOTP-ONE-SECRET", text)
                self.assertIn("two@example.com----mail-password-two----TOTP-TWO-SECRET", text)
                self.assertNotIn("AT-ONE-SECRET", text)
                self.assertNotIn("AT-TWO-SECRET", text)

    def test_export_field_metadata_matches_backend_and_missing_fields_are_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                client = self._client(root)
                metadata = client.get("/api/accounts/export-fields", headers={"X-Auth-Code": "test-auth"})
                self.assertEqual(metadata.status_code, 200)
                fields = metadata.get_json()["fields"]
                self.assertEqual([item["key"] for item in fields], list(ACCOUNT_EXPORT_FIELDS))
                self.assertEqual({item["key"] for item in fields}, set(_ACCOUNT_EXPORT_FIELD_LABELS))
                self.assertEqual({item["key"] for item in fields if item["sensitive"]}, {
                    "password", "email_password", "totp_secret", "totp_code", "access_token", "codex_agent_token", "copy_line",
                })
                for explicit in ([], "", {}, 0):
                    response = client.post("/api/accounts/export", json={"account_ids": [1], "fields": explicit}, headers={"X-Auth-Code": "test-auth"})
                    self.assertEqual(response.status_code, 400, explicit)
                unauthorized = client.get("/api/accounts/export-fields")
                self.assertEqual(unauthorized.status_code, 401)

    def test_export_handles_malformed_extra_json_and_totp_without_500(self):
        row = {"extra_json": "[]", "password": "mail-pass", "email_source": "outlook", "totp_secret": "!!!"}
        self.assertEqual(_account_export_field_value(row, "password"), "")
        self.assertEqual(_account_export_field_value(row, "totp_code"), "")

    def test_export_separates_generic_api_login_password_from_mail_password(self):
        generic = {
            "email": "generic@example.com", "password": "chatgpt-pass", "email_source": "generic_api",
            "account_line_format": "chatgpt_api", "code_url": "https://mail.example.test/code",
        }
        self.assertEqual(_account_export_field_value(generic, "password"), "chatgpt-pass")
        self.assertEqual(_account_export_field_value(generic, "email_password"), "")

    def test_export_uses_no_code_url_login_password_marker(self):
        account = {
            "email": "manual@example.com", "password": "chatgpt-pass",
            "account_line_format": "chatgpt_api_no_code_url",
        }
        self.assertEqual(_account_export_field_value(account, "password"), "chatgpt-pass")
        self.assertEqual(_account_export_field_value(account, "email_password"), "")

    def test_current_page_uses_server_page_and_rejects_mismatched_ids(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                client = self._client(root)
                response = client.post(
                    "/api/accounts/export",
                    json={"scope": "current_page", "page": 2, "page_size": 1, "account_ids": [1], "fields": ["email"], "prepare": True},
                    headers={"X-Auth-Code": "test-auth"},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()["count"], 1)
                mismatch = client.post(
                    "/api/accounts/export",
                    json={"scope": "current_page", "page": 2, "page_size": 1, "account_ids": [2], "fields": ["email"]},
                    headers={"X-Auth-Code": "test-auth"},
                )
                self.assertEqual(mismatch.status_code, 400)

    def test_selected_ids_report_non_positive_and_pseudo_ids(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                client = self._client(root)
                response = client.post(
                    "/api/accounts/export",
                    json={"scope": "selected", "account_ids": [0, -1, False, 1.5, "bad", 1], "fields": ["email"], "prepare": True},
                    headers={"X-Auth-Code": "test-auth"},
                )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()["count"], 1)
                self.assertEqual(response.get_json()["skipped_count"], 5)

    def test_invalid_export_date_range_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                client = self._client(root)
                response = client.post(
                    "/api/accounts/export",
                    json={"scope": "filtered", "filters": {"date_from": "not-a-date"}, "fields": ["email"]},
                    headers={"X-Auth-Code": "test-auth"},
                )
                self.assertEqual(response.status_code, 400)

    def test_export_custom_json_filtered_and_fields_are_ordered(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                client = self._client(root)
                response = client.post(
                    "/api/accounts/export",
                    json={
                        "scope": "filtered",
                        "filters": {"q": "two@example.com"},
                        "fields": ["email", "plan_type"],
                        "format": "json",
                        "prepare": True,
                    },
                    headers={"X-Auth-Code": "test-auth"},
                )
                self.assertEqual(response.status_code, 200)
                meta = response.get_json()
                self.assertEqual(meta["count"], 1)
                download = client.get(meta["download_url"], headers={"X-Auth-Code": "test-auth"})
                self.assertEqual(download.status_code, 200)
                rows = json.loads(download.get_data(as_text=True))
                self.assertEqual(rows, [{"email": "two@example.com", "plan_type": "plus"}])


if __name__ == "__main__":
    unittest.main()
