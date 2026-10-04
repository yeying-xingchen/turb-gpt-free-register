import unittest
from unittest.mock import patch

from core import db
from core.account_import import parse_existing_account_text


class ExistingAccountImportTests(unittest.TestCase):
    def test_parser_supports_two_three_or_four_hyphen_separators(self):
        records, errors = parse_existing_account_text(
            "user@example.com---Password!---JBSW Y3DP EHPK3PXP---eyJ.a-b-c\n"
            "other@example.com----Password2----ABC----opaque-token\n"
            "dash@example.com--Password3--DEF--eyJ.d-e-f"
        )

        self.assertEqual(errors, [])
        self.assertEqual(
            [row["email"] for row in records],
            ["user@example.com", "other@example.com", "dash@example.com"],
        )
        self.assertEqual(records[0]["totp_secret"], "JBSWY3DPEHPK3PXP")
        self.assertEqual(records[0]["access_token"], "eyJ.a-b-c")
        # "--" 分隔时，AT 里的单个短横线不会被当作分隔符。
        self.assertEqual(records[2]["access_token"], "eyJ.d-e-f")
        self.assertEqual(records[2]["totp_secret"], "DEF")

    def test_parser_reports_invalid_and_duplicate_lines_without_secrets(self):
        records, errors = parse_existing_account_text(
            "user@example.com---pw---ABC---token\n"
            "USER@example.com---pw2---DEF---token2\n"
            "not-an-email---pw---ABC---token"
        )

        self.assertEqual(len(records), 1)
        self.assertEqual(len(errors), 2)
        self.assertIn("邮箱重复", errors[0]["reason"])
        self.assertNotIn("pw2", repr(errors))
        self.assertNotIn("token2", repr(errors))

    def test_db_import_stores_chatgpt_password_and_skips_duplicate(self):
        rows = []
        with patch.object(db, "_load_accounts", return_value=rows), patch.object(db, "_save_accounts") as save:
            inserted, skipped = db.import_existing_accounts([
                {
                    "email": "user@example.com",
                    "password": "ChatGPTPassword",
                    "totp_secret": "JBSWY3DPEHPK3PXP",
                    "access_token": "access-token",
                },
                {
                    "email": "USER@example.com",
                    "password": "another",
                    "totp_secret": "ABC",
                    "access_token": "another-token",
                },
            ])

        self.assertEqual(inserted, 1)
        self.assertEqual(skipped[0]["reason"], "本次内容中邮箱重复")
        self.assertEqual(rows[0]["user_name"], "Imported Account")
        self.assertEqual(rows[0]["email_source"], "imported")
        self.assertEqual(rows[0]["access_token"], "access-token")
        self.assertEqual(rows[0]["totp_secret"], "JBSWY3DPEHPK3PXP")
        self.assertIn('"registration_password": "ChatGPTPassword"', rows[0]["extra_json"])
        save.assert_called_once()

    def test_db_import_preserves_fetched_user_name(self):
        rows = []
        with patch.object(db, "_load_accounts", return_value=rows), patch.object(db, "_save_accounts"):
            inserted, skipped = db.import_existing_accounts([{
                "email": "user@example.com",
                "password": "ChatGPTPassword",
                "totp_secret": "ABC",
                "access_token": "access-token",
                "user_name": "  张三 Alice  ",
            }])

        self.assertEqual((inserted, skipped), (1, []))
        self.assertEqual(rows[0]["user_name"], "张三 Alice")


class ExistingAccountImportApiTests(unittest.TestCase):
    def setUp(self):
        from webui.app import create_app
        self.enterContext(patch("core.plus_activation_store.recover_interrupted", return_value=0))
        for name in (
            "recover_interrupted_plan_checks", "recover_interrupted_extract_links",
            "recover_interrupted_live_checks", "recover_interrupted_codex_agents",
            "recover_interrupted_totp_setups", "recover_interrupted_email_changes",
        ):
            self.enterContext(patch.object(db, name, return_value=0))
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    def test_import_fetches_names_and_reports_failures_separately_from_skips(self):
        from core import account_import

        def fetch_name(token, **kwargs):
            if token == "good-token":
                return {"ok": True, "user_name": "Alice 最新姓名"}
            return {"ok": False, "error": "AT 已过期或失效"}

        rows = []
        with patch.object(account_import, "fetch_account_user_name", side_effect=fetch_name) as fetch, \
                patch.object(db, "get_account_by_email", return_value=None), \
                patch.object(db, "_load_accounts", return_value=rows), \
                patch.object(db, "_save_accounts") as save:
            response = self.client.post("/api/accounts/import", json={"text": (
                "alice@example.com---PasswordSecret---TOTPSECRET---good-token\n"
                "bob@example.com---PasswordSecret---TOTPSECRET---expired-token\n"
                "ALICE@example.com---PasswordSecret---TOTPSECRET---duplicate-token\n"
                "invalid-line"
            )})

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual((body["parsed"], body["inserted"], body["skipped"]), (2, 2, 2))
        self.assertEqual(body["user_names_fetched"], 1)
        self.assertEqual(len(body["user_name_warnings"]), 1)
        self.assertEqual(body["user_name_warnings"][0]["email"], "bob@example.com")
        self.assertIn("账号已导入", body["user_name_warnings"][0]["reason"])
        self.assertEqual(rows[0]["user_name"], "Alice 最新姓名")
        self.assertEqual(rows[1]["user_name"], "Imported Account")
        self.assertEqual(fetch.call_count, 2)
        save.assert_called_once()
        for secret in ("PasswordSecret", "TOTPSECRET", "good-token", "expired-token", "duplicate-token"):
            self.assertNotIn(secret, response.get_data(as_text=True))

    def test_invalid_input_does_not_start_name_requests(self):
        with patch("core.account_import.fetch_account_user_name") as fetch:
            response = self.client.post("/api/accounts/import", json={"text": "invalid-line"})
        self.assertEqual(response.status_code, 400)
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
