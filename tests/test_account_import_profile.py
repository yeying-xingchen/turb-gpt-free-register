import unittest
from copy import deepcopy
from unittest.mock import Mock, patch

import core.account_import as importer


class AccountProfileFetchTests(unittest.TestCase):
    def setUp(self):
        self.response = Mock(status_code=200, text="secret-response")
        self.response.json.return_value = {"name": "  测试 User  ", "email": "USER@example.com"}
        self.session = Mock()
        self.session.get.return_value = self.response
        self.session.get_chatgpt_headers.return_value = {
            "content-type": "application/json", "oai-device-id": "device-test",
        }
        self.relay = Mock()
        self.route = {"proxy": "socks5h://proxy.test:1080", "upstream_proxy": "http://upstream.test:8080"}
        self.route_mock = self.enterContext(patch.object(importer, "resolve_plan_check_route", return_value=self.route))
        self.open_mock = self.enterContext(patch.object(importer, "open_plan_check_proxy", return_value=("http://127.0.0.1:12345", self.relay)))
        self.session_mock = self.enterContext(patch.object(importer, "BrowserSession", return_value=self.session))
        self.close_mock = self.enterContext(patch.object(importer, "close_browser_session"))

    def test_fetch_uses_normalized_token_proxy_timeout_and_closes_resources(self):
        result = importer.fetch_account_user_name("Authorization: Bearer opaque-token", email="user@example.com", timeout=4)
        self.assertEqual(result, {"ok": True, "user_name": "测试 User"})
        self.open_mock.assert_called_once_with(self.route, self.route["proxy"], timeout=4.0)
        self.session_mock.assert_called_once_with(proxy="http://127.0.0.1:12345", detect_exit_geo=False)
        args, kwargs = self.session.get.call_args
        self.assertEqual(args, ("https://chatgpt.com/backend-api/me",))
        self.assertEqual(kwargs["headers"]["authorization"], "Bearer opaque-token")
        self.assertEqual(kwargs["headers"]["oai-device-id"], "device-test")
        self.assertNotIn("content-type", kwargs["headers"])
        self.assertEqual(kwargs["timeout"], 4.0)
        self.assertFalse(kwargs["allow_redirects"])
        self.close_mock.assert_called_once_with(self.session)
        self.relay.close.assert_called_once_with()

    def test_missing_response_email_is_allowed(self):
        self.response.json.return_value = {"name": "User"}
        self.assertEqual(importer.fetch_account_user_name("opaque-token", email="user@example.com"),
                         {"ok": True, "user_name": "User"})

    def test_mismatched_or_invalid_email_is_rejected(self):
        for email in ("other@example.com", "", None, [], 123):
            with self.subTest(email=email):
                self.response.json.return_value = {"name": "User", "email": email}
                result = importer.fetch_account_user_name("opaque-token", email="user@example.com")
                self.assertEqual(result, {"ok": False, "error": "AT 对应邮箱与导入邮箱不一致"})

    def test_response_must_be_object_with_nonempty_string_name(self):
        for payload in ([], "secret-response", None, {}, {"name": ""}, {"name": "  "}, {"name": None}, {"name": 123}):
            with self.subTest(payload=payload):
                self.response.json.return_value = payload
                result = importer.fetch_account_user_name("opaque-token")
                self.assertFalse(result["ok"])
                self.assertIsInstance(result["error"], str)
                self.assertNotIn("secret-response", repr(result))
                self.assertNotIn("opaque-token", repr(result))

    def test_http_failures_do_not_parse_or_echo_response_and_are_not_retried(self):
        for status in (302, 401, 403, 429, 500):
            with self.subTest(status=status):
                self.session.get.reset_mock()
                self.response.json.reset_mock()
                self.response.status_code = status
                result = importer.fetch_account_user_name("opaque-token")
                self.assertFalse(result["ok"])
                self.assertNotIn("secret-response", repr(result))
                self.assertNotIn("opaque-token", repr(result))
                self.response.json.assert_not_called()
                self.session.get.assert_called_once()

    def test_json_and_network_exceptions_are_safe_and_resources_are_closed(self):
        for target in (self.response.json, self.session.get):
            with self.subTest(target=target):
                target.side_effect = RuntimeError("opaque-token proxy-user:proxy-password")
                self.close_mock.reset_mock()
                self.relay.close.reset_mock()
                result = importer.fetch_account_user_name("opaque-token")
                self.assertFalse(result["ok"])
                self.assertNotIn("opaque-token", repr(result))
                self.assertNotIn("proxy-password", repr(result))
                self.close_mock.assert_called_once_with(self.session)
                self.relay.close.assert_called_once_with()
                target.side_effect = None

    def test_session_initialization_failure_closes_relay(self):
        self.session_mock.side_effect = RuntimeError("secret-response")
        result = importer.fetch_account_user_name("opaque-token")
        self.assertFalse(result["ok"])
        self.assertNotIn("secret-response", repr(result))
        self.close_mock.assert_not_called()
        self.relay.close.assert_called_once_with()

    def test_cleanup_errors_do_not_replace_success(self):
        self.close_mock.side_effect = RuntimeError("secret-response")
        self.relay.close.side_effect = RuntimeError("secret-response")
        self.assertEqual(importer.fetch_account_user_name("opaque-token"),
                         {"ok": True, "user_name": "测试 User"})
        self.relay.close.assert_called_once_with()


class AccountProfileImportTests(unittest.TestCase):
    @staticmethod
    def record(email):
        return {"email": email, "password": "chatgpt-password", "totp_secret": "totp-secret", "access_token": "opaque-token"}

    def test_existing_accounts_skip_queries_and_success_and_failure_both_import(self):
        records = [self.record("old@example.com"), self.record("new@example.com"), self.record("failed@example.com")]
        original = deepcopy(records)
        with patch.object(importer.db, "get_account_by_email", side_effect=lambda email: {"id": 1} if email == "old@example.com" else None), \
             patch.object(importer, "fetch_account_user_name", side_effect=lambda token, email: {"ok": True, "user_name": "Fetched Name"} if email == "new@example.com" else {"ok": False, "error": "用户名查询响应缺少有效姓名"}) as fetch, \
             patch.object(importer.db, "import_existing_accounts", return_value=(2, [])) as save:
            result = importer.import_existing_accounts(records)
        self.assertEqual(result[0], 2)
        self.assertEqual(result[1], [{"email": "old@example.com", "reason": "账号已存在"}])
        self.assertEqual(result[2], [
            {"email": "new@example.com", "ok": True, "user_name": "Fetched Name"},
            {"email": "failed@example.com", "ok": False, "error": "用户名查询响应缺少有效姓名"},
        ])
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(save.call_args.args[0], [{**records[1], "user_name": "Fetched Name"}, records[2]])
        self.assertEqual(records, original)
        for secret in ("opaque-token", "chatgpt-password", "totp-secret"):
            self.assertNotIn(secret, repr(result))

    def test_unexpected_worker_failure_preserves_import_with_fixed_error(self):
        record = self.record("user@example.com")
        with patch.object(importer.db, "get_account_by_email", return_value=None), \
             patch.object(importer, "fetch_account_user_name", side_effect=RuntimeError("opaque-token secret-response")), \
             patch.object(importer.db, "import_existing_accounts", return_value=(1, [])) as save:
            inserted, skipped, details = importer.import_existing_accounts([record])
        self.assertEqual((inserted, skipped), (1, []))
        self.assertEqual(details, [{"email": record["email"], "ok": False, "error": "用户名查询请求失败，请稍后重试"}])
        save.assert_called_once_with([record])

    def test_concurrent_duplicate_is_excluded_from_name_details(self):
        records = [self.record("Race@Example.com"), self.record("new@example.com")]
        skipped = [{"email": "race@example.com", "reason": "账号已存在"}]
        with patch.object(importer.db, "get_account_by_email", return_value=None), \
             patch.object(importer, "fetch_account_user_name", return_value={"ok": True, "user_name": "Name"}), \
             patch.object(importer.db, "import_existing_accounts", return_value=(1, skipped)):
            result = importer.import_existing_accounts(records)
        self.assertEqual(result, (1, skipped, [{"email": "new@example.com", "ok": True, "user_name": "Name"}]))

    def test_empty_or_all_existing_records_do_not_query_profiles(self):
        with patch.object(importer.db, "get_account_by_email", return_value={"id": 1}), \
             patch.object(importer, "fetch_account_user_name") as fetch, \
             patch.object(importer.db, "import_existing_accounts") as save:
            self.assertEqual(importer.import_existing_accounts([]), (0, [], []))
            self.assertEqual(importer.import_existing_accounts([self.record("old@example.com")]),
                             (0, [{"email": "old@example.com", "reason": "账号已存在"}], []))
        fetch.assert_not_called()
        save.assert_not_called()
