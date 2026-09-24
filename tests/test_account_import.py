# -*- coding: utf-8 -*-
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import account_import, db


class AccountImportParserTests(unittest.TestCase):
    def test_parses_generic_api_five_field_account_line(self):
        code_url = "https://mail.antsgo.xyz/pick-mail?code=abc&mail=user%40example.com"
        access_token = "eyJhbGciOiJSUzI1NiJ9." + "x" * 80
        text = "----".join([
            "user@example.com",
            "chatgpt-password",
            "JBSWY3DPEHPK3PXP",
            code_url,
            access_token,
        ])

        records, errors = account_import.parse_account_text(text)

        self.assertEqual(errors, [])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["email"], "user@example.com")
        self.assertEqual(records[0]["password"], "chatgpt-password")
        self.assertEqual(records[0]["registration_password"], "chatgpt-password")
        self.assertEqual(records[0]["totp_secret"], "JBSWY3DPEHPK3PXP")
        self.assertEqual(records[0]["code_url"], code_url)
        self.assertNotIn("client_id", records[0])
        self.assertEqual(records[0]["access_token"], access_token)
        self.assertEqual(records[0]["email_source"], "generic_api")
        self.assertEqual(records[0]["material_line"], "----".join(text.split("----")[:4]))

    def test_parses_outlook_five_field_account_line(self):
        text = "----".join([
            "user@example.com",
            "mail-password",
            "client-id",
            "refresh-token",
            "access-token",
        ])

        records, errors = account_import.parse_account_text(text)

        self.assertEqual(errors, [])
        self.assertEqual(records[0]["email_source"], "outlook")
        self.assertEqual(records[0]["refresh_token"], "refresh-token")
        self.assertEqual(records[0]["access_token"], "access-token")

    def test_parses_double_hyphen_account_line_with_jwt(self):
        token = "eyJ" + "-x" * 40
        records, errors = account_import.parse_account_text(
            "user@example.com--password,client-secret--client-id--" + token
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["email"], "user@example.com")
        self.assertEqual(records[0]["access_token"], token)

    def test_parses_chatgpt_account_without_code_url(self):
        text = "user@example.com----chatgpt-password----JBSWY3DPEHPK3PXP----access-token"

        records, errors = account_import.parse_account_text(text)

        self.assertEqual(errors, [])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["access_token"], "access-token")
        self.assertEqual(records[0]["password"], "chatgpt-password")
        self.assertEqual(records[0]["registration_password"], "chatgpt-password")
        self.assertEqual(records[0]["totp_secret"], "JBSWY3DPEHPK3PXP")
        self.assertEqual(records[0]["account_line_format"], "chatgpt_api_no_code_url")
        self.assertNotIn("code_url", records[0])
        self.assertNotIn("email_source", records[0])
        self.assertEqual(records[0]["material_line"], "user@example.com----chatgpt-password----JBSWY3DPEHPK3PXP")

    def test_parses_chatgpt_account_with_empty_code_url_slot(self):
        text = "user@example.com----chatgpt-password----JBSWY3DPEHPK3PXP--------access-token"

        records, errors = account_import.parse_account_text(text)

        self.assertEqual(errors, [])
        self.assertEqual(records[0]["access_token"], "access-token")
        self.assertEqual(records[0]["account_line_format"], "chatgpt_api_no_code_url")
        self.assertNotIn("code_url", records[0])

    def test_rejects_url_in_last_slot_without_access_token(self):
        records, errors = account_import.parse_account_text(
            "user@example.com----chatgpt-password----JBSWY3DPEHPK3PXP----https://mail.example.test/pick"
        )

        self.assertEqual(records, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("URL 放在第二段", errors[0]["reason"])

    def test_promotes_generic_password_to_registration_password(self):
        records, errors = account_import.parse_account_text(
            "user@example.com----chatgpt-password----JBSWY3DPEHPK3PXP----"
            "https://mail.example.test/pick----access-token"
        )
        self.assertEqual(errors, [])
        self.assertEqual(records[0]["registration_password"], "chatgpt-password")

    def test_parses_bearer_and_optional_totp_in_extended_line(self):
        text = "----".join([
            "user@example.com",
            "chatgpt-password",
            "JBSWY3DPEHPK3PXP",
            "https://mail.example.test/pick",
            "Bearer access-token",
            "JBSWY3DPEHPK3PXP",
        ])

        records, errors = account_import.parse_account_text(text)

        self.assertEqual(errors, [])
        self.assertEqual(records[0]["access_token"], "access-token")
        self.assertEqual(records[0]["totp_secret"], "JBSWY3DPEHPK3PXP")

    def test_keeps_legacy_short_text_formats(self):
        records, errors = account_import.parse_account_text(
            "one@example.com----token----totp\n"
            "two@example.com----https://mail.test/pick----token2"
        )

        self.assertEqual(errors, [])
        self.assertEqual(records[0]["access_token"], "token")
        self.assertEqual(records[0]["totp_secret"], "totp")
        self.assertEqual(records[1]["code_url"], "https://mail.test/pick")
        self.assertEqual(records[1]["access_token"], "token2")


class AccountImportStorageTests(unittest.TestCase):
    def test_import_without_code_url_keeps_login_credentials_without_pool_row(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            accounts_path = root / "accounts.json"
            generic_path = root / "generic.json"
            with patch.object(db, "_ACCOUNTS_JSON", accounts_path), \
                 patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy-accounts.json"), \
                 patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"), \
                 patch.object(db, "_TOKENS_TXT", root / "tokens.txt"), \
                 patch.object(db, "_VIEWER_HTML", root / "viewer.html"), \
                 patch.object(db, "_GENERIC_API_EMAIL_JSON", generic_path), \
                 patch.object(db, "_GENERIC_API_EMAIL_TXT", root / "generic.txt"):
                result = db.import_registered_accounts([{
                    "email": "manual@example.com",
                    "access_token": "access-token",
                    "password": "chatgpt-password",
                    "registration_password": "chatgpt-password",
                    "totp_secret": "JBSWY3DPEHPK3PXP",
                    "account_line_format": "chatgpt_api_no_code_url",
                    "material_line": "manual@example.com----chatgpt-password----JBSWY3DPEHPK3PXP",
                }])

                self.assertEqual(result["inserted_count"], 1)
                account = db.get_account_by_email("manual@example.com")
                self.assertEqual(account["account_line_format"], "chatgpt_api_no_code_url")
                self.assertEqual(account["copy_line"], "manual@example.com----chatgpt-password----JBSWY3DPEHPK3PXP----access-token")
                self.assertIsNone(db.get_generic_api_email_by_email("manual@example.com"))

    def test_import_with_code_url_links_generic_api_pool(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            accounts_path = root / "accounts.json"
            generic_path = root / "generic.json"
            with patch.object(db, "_ACCOUNTS_JSON", accounts_path), \
                 patch.object(db, "_LEGACY_ACCOUNTS_JSON", root / "legacy-accounts.json"), \
                 patch.object(db, "_ACCOUNTS_TXT", root / "accounts.txt"), \
                 patch.object(db, "_TOKENS_TXT", root / "tokens.txt"), \
                 patch.object(db, "_VIEWER_HTML", root / "viewer.html"), \
                 patch.object(db, "_GENERIC_API_EMAIL_JSON", generic_path), \
                 patch.object(db, "_GENERIC_API_EMAIL_TXT", root / "generic.txt"):
                result = db.import_registered_accounts([{
                    "email": "user@example.com",
                    "access_token": "access-token",
                    "password": "chatgpt-password",
                    "totp_secret": "JBSWY3DPEHPK3PXP",
                    "code_url": "https://mail.example.test/pick",
                    "account_line_format": "chatgpt_api",
                    "material_line": "user@example.com----chatgpt-password----JBSWY3DPEHPK3PXP----https://mail.example.test/pick",
                    "email_source": "generic_api",
                }])

                self.assertEqual(result["inserted_count"], 1)
                pool = db.get_generic_api_email_by_email("USER@example.com")
                self.assertIsNotNone(pool)
                self.assertEqual(pool["code_url"], "https://mail.example.test/pick")
                self.assertEqual(pool["status"], "used")
                account = db.get_account_by_email("user@example.com")
                self.assertEqual(account["access_token"], "access-token")
                self.assertEqual(account["totp_secret"], "JBSWY3DPEHPK3PXP")
                self.assertEqual(account["copy_line"], "user@example.com----chatgpt-password----JBSWY3DPEHPK3PXP----https://mail.example.test/pick----access-token")


if __name__ == "__main__":
    unittest.main()
