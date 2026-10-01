import unittest
from unittest.mock import patch

from core import db
from core.account_import import parse_existing_account_text


class ExistingAccountImportTests(unittest.TestCase):
    def test_parser_supports_three_or_four_hyphen_separators(self):
        records, errors = parse_existing_account_text(
            "user@example.com---Password!---JBSW Y3DP EHPK3PXP---eyJ.a-b-c\n"
            "other@example.com----Password2----ABC----opaque-token"
        )

        self.assertEqual(errors, [])
        self.assertEqual([row["email"] for row in records], ["user@example.com", "other@example.com"])
        self.assertEqual(records[0]["totp_secret"], "JBSWY3DPEHPK3PXP")
        self.assertEqual(records[0]["access_token"], "eyJ.a-b-c")

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
        self.assertEqual(rows[0]["email_source"], "imported")
        self.assertEqual(rows[0]["access_token"], "access-token")
        self.assertEqual(rows[0]["totp_secret"], "JBSWY3DPEHPK3PXP")
        self.assertIn('"registration_password": "ChatGPTPassword"', rows[0]["extra_json"])
        save.assert_called_once()


if __name__ == "__main__":
    unittest.main()
