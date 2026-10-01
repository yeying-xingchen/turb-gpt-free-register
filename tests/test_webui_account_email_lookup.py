# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db
from webui.app import create_app


class WebUiAccountEmailLookupTests(unittest.TestCase):
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

    def test_lookup_matches_case_insensitively_and_reports_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "accounts.json").write_text(json.dumps([
                {"id": 1, "email": "a@example.com"},
                {"id": 2, "email": "new@example.com", "original_email": "old@example.com"},
                {"id": 3, "email": "archived@example.com", "archived": True},
            ]), encoding="utf-8")

            with patch.multiple(db, **self._storage_patches(root)):
                app = create_app(auth_code="test-auth")
                client = app.test_client()
                response = client.post(
                    "/api/accounts/lookup",
                    json={"emails": ["A@EXAMPLE.COM", "old@example.com", "missing@example.com"]},
                    headers={"X-Auth-Code": "test-auth"},
                )

            self.assertEqual(response.status_code, 200)
            body = response.get_json()
            self.assertTrue(body["ok"])
            self.assertEqual([item["id"] for item in body["matches"]], [1, 2])
            self.assertEqual(body["matches"][0]["matched_by"], "email")
            self.assertEqual(body["matches"][1]["matched_by"], "original_email")
            self.assertEqual(body["not_found"], ["missing@example.com"])

    def test_lookup_respects_current_archive_filter(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "accounts.json").write_text(json.dumps([
                {"id": 1, "email": "active@example.com"},
                {"id": 2, "email": "archived@example.com", "archived": True},
            ]), encoding="utf-8")

            with patch.multiple(db, **self._storage_patches(root)):
                app = create_app(auth_code="test-auth")
                client = app.test_client()
                response = client.post(
                    "/api/accounts/lookup",
                    json={"emails": ["archived@example.com"], "archived": "only"},
                    headers={"X-Auth-Code": "test-auth"},
                )

            self.assertEqual(response.status_code, 200)
            body = response.get_json()
            self.assertEqual([item["id"] for item in body["matches"]], [2])
            self.assertEqual(body["not_found"], [])

    def test_lookup_validates_input(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                app = create_app(auth_code="test-auth")
                client = app.test_client()
                empty = client.post(
                    "/api/accounts/lookup",
                    json={"emails": []},
                    headers={"X-Auth-Code": "test-auth"},
                )
                too_many = client.post(
                    "/api/accounts/lookup",
                    json={"emails": [f"{i}@example.com" for i in range(5001)]},
                    headers={"X-Auth-Code": "test-auth"},
                )

            self.assertEqual(empty.status_code, 400)
            self.assertEqual(too_many.status_code, 400)


if __name__ == "__main__":
    unittest.main()
