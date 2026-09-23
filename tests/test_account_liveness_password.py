# -*- coding: utf-8 -*-
import unittest
from unittest.mock import Mock, patch

from core import account_liveness, codex_oauth


class AccountCredentialHelperTests(unittest.TestCase):
    @patch("core.codex_oauth.db.get_account_by_email")
    def test_registration_password_ignores_mail_pool_password(self, get_account):
        get_account.return_value = {
            "password": "outlook-mail-password",
            "registration_password": "chatgpt-password",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        }
        self.assertEqual(
            codex_oauth._account_registration_password("user@example.com"),
            "chatgpt-password",
        )
        self.assertEqual(
            codex_oauth._account_totp_secret("user@example.com"),
            "JBSWY3DPEHPK3PXP",
        )

    @patch("core.codex_oauth.db.get_account_by_email")
    def test_registration_password_reads_extra_json(self, get_account):
        get_account.return_value = {
            "password": "outlook-mail-password",
            "extra_json": '{"registration_password":"nested-password"}',
        }
        self.assertEqual(
            codex_oauth._account_registration_password("user@example.com"),
            "nested-password",
        )

    @patch("core.codex_oauth.db.get_account_by_email")
    def test_generic_api_chatgpt_material_uses_password_field(self, get_account):
        get_account.return_value = {
            "password": "chatgpt-password",
            "email_source": "generic_api",
            "account_line_format": "chatgpt_api",
            "code_url": "https://mail.example.test/pick",
        }
        self.assertEqual(
            codex_oauth._account_registration_password("user@example.com"),
            "chatgpt-password",
        )

    @patch("core.codex_oauth.db.get_account_by_email")
    def test_outlook_password_field_is_not_used_as_chatgpt_password(self, get_account):
        get_account.return_value = {
            "password": "outlook-mail-password",
            "email_source": "outlook",
        }
        self.assertEqual(
            codex_oauth._account_registration_password("user@example.com"),
            "",
        )

    @patch("core.codex_oauth.db.get_account_by_email")
    def test_no_code_url_import_uses_chatgpt_password(self, get_account):
        get_account.return_value = {
            "password": "chatgpt-password",
            "account_line_format": "chatgpt_api_no_code_url",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        }
        self.assertEqual(
            codex_oauth._account_registration_password("user@example.com"),
            "chatgpt-password",
        )


class PasswordMfaFlowTests(unittest.TestCase):
    def test_password_flow_issues_and_verifies_totp(self):
        session = Mock()
        with patch.object(account_liveness, "_account_registration_password", return_value="chatgpt-password"), \
             patch.object(account_liveness, "_account_totp_secret", return_value="JBSWY3DPEHPK3PXP"), \
             patch.object(account_liveness, "_account_totp_code", return_value="123456"), \
             patch.object(
                 account_liveness,
                 "_password_verify",
                 return_value={"continue_url": "https://auth.openai.com/mfa-challenge/factor-123"},
             ), \
             patch.object(account_liveness, "_mfa_issue_challenge", return_value={}) as issue, \
             patch.object(account_liveness, "_mfa_verify", return_value={}) as verify, \
             patch.object(
                 account_liveness,
                 "_follow_continue_and_fetch",
                 return_value={"accessToken": "new-access-token"},
             ) as follow:
            result = account_liveness._login_via_password_or_otp(
                session, "user@example.com", 0, email_source="generic_api"
            )

        self.assertEqual(result["accessToken"], "new-access-token")
        issue.assert_called_once_with(session, "factor-123")
        verify.assert_called_once_with(session, "factor-123", "123456")
        follow.assert_called_once_with(
            session,
            "https://auth.openai.com/mfa-challenge/factor-123",
            referer="https://auth.openai.com/mfa-challenge/factor-123",
        )

    def test_mfa_detection_accepts_payload_and_query_shapes(self):
        self.assertTrue(account_liveness._is_mfa_challenge(
            {"page": {"type": "totp_challenge", "payload": {"factorId": "f-1"}}},
            "",
        ))
        self.assertTrue(account_liveness._is_mfa_challenge(
            {"requires_mfa": True},
            "https://auth.openai.com/challenge?factor_id=f-2",
        ))
        self.assertFalse(account_liveness._is_mfa_challenge(
            {"page": {"type": "about_you"}, "account": {"structure": "personal"}},
            "https://auth.openai.com/about-you",
        ))
        self.assertFalse(account_liveness._is_mfa_challenge(
            {"user": {"mfa": False}, "account": {"factor": "default"}},
            "https://auth.openai.com/authorize/continue",
        ))

    def test_missing_password_falls_back_to_email_otp(self):
        session = Mock()
        with patch.object(account_liveness, "_account_registration_password", return_value=""), \
             patch.object(
                 account_liveness,
                 "_login_via_email_otp",
                 return_value={"accessToken": "otp-token"},
             ) as fallback:
            result = account_liveness._login_via_password_or_otp(
                session, "user@example.com", 123, email_source="generic_api"
            )
        self.assertEqual(result["accessToken"], "otp-token")
        fallback.assert_called_once_with(
            session, "user@example.com", 123, email_source="generic_api"
        )


if __name__ == "__main__":
    unittest.main()
