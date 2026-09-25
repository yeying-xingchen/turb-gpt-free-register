# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from webui.app import create_app


class WebUiSensitiveCacheHeaderTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.client.environ_base["HTTP_X_AUTH_CODE"] = "test-auth"

    @patch("webui.app.db.list_email_pool_page")
    def test_outlook_pool_response_is_not_cacheable(self, list_email_pool_page):
        list_email_pool_page.return_value = {
            "items": [{"email": "pool@example.test", "password": "mail-secret"}],
            "total": 1,
            "offset": 0,
            "limit": 1,
            "latest": "",
        }

        response = self.client.get("/api/outlook?paged=1&page=1&page_size=1")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["items"][0]["password"], "mail-secret")
        self.assertEqual(response.headers.get("Cache-Control"), "no-store, max-age=0")
        self.assertEqual(response.headers.get("Pragma"), "no-cache")

    @patch("webui.app.extract_link_service.query_cdk")
    def test_extract_link_cdk_post_redacts_provider_secrets_and_is_not_cacheable(self, query_cdk):
        query_cdk.return_value = {
            "backend": "cdk",
            "provider": "internal-provider",
            "remaining": 3,
            "code": "secret-cdk",
            "details": {"token": "secret-cdk"},
        }

        response = self.client.post("/api/extract-link/cdk", json={"code": "secret-cdk"})

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["remaining"], 3)
        self.assertNotIn("provider", body)
        self.assertNotIn("code", body)
        self.assertNotIn("secret-cdk", response.get_data(as_text=True))
        self.assertEqual(response.headers.get("Cache-Control"), "no-store, max-age=0")
        self.assertEqual(response.headers.get("Pragma"), "no-cache")
        query_cdk.assert_called_once_with(cdk="secret-cdk")

    @patch("webui.app.extract_link_service.query_cdk")
    def test_extract_link_cdk_rejects_secret_in_get_query(self, query_cdk):
        response = self.client.get("/api/extract-link/cdk?code=secret-cdk")

        self.assertEqual(response.status_code, 400)
        self.assertNotIn("secret-cdk", response.get_data(as_text=True))
        self.assertEqual(response.headers.get("Cache-Control"), "no-store, max-age=0")
        self.assertEqual(response.headers.get("Pragma"), "no-cache")
        query_cdk.assert_not_called()

    @patch("core.codex_agent.upload_sub2api_account")
    @patch("webui.app.db.update_account_codex_agent")
    @patch("webui.app.db.get_account")
    def test_codex_agent_upload_sub2_hides_upstream_response(self, get_account, update_account, upload):
        get_account.return_value = {
            "id": 7,
            "email": "agent@example.test",
            "codex_agent_token": '{"agent_identity": {"email": "agent@example.test"}}',
        }
        upload.return_value = {
            "ok": True,
            "uploaded": True,
            "url": "https://sub2.example.test/import?token=SECRET-UPSTREAM",
            "status_code": 200,
            "payload_mode": "codex_session_import",
            "email": "agent@example.test",
            "total": 2,
            "dedupe_key": "SECRET-DEDUPE",
            "response": {"authorization": "Bearer SECRET-UPSTREAM"},
        }

        response = self.client.post("/api/accounts/7/codex-agent/upload-sub2")

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["result"]["status_code"], 200)
        self.assertEqual(body["result"]["url"], "https://sub2.example.test/import")
        self.assertNotIn("response", body["result"])
        self.assertNotIn("dedupe_key", body["result"])
        self.assertNotIn("SECRET-UPSTREAM", response.get_data(as_text=True))
        self.assertNotIn("SECRET-DEDUPE", response.get_data(as_text=True))
        self.assertEqual(response.headers.get("Cache-Control"), "no-store, max-age=0")
        self.assertEqual(response.headers.get("Pragma"), "no-cache")
        update_account.assert_called_once()

    @patch("core.codex_agent.upload_sub2api_account", side_effect=RuntimeError("upstream SECRET-ERROR"))
    @patch("webui.app.db.get_account")
    def test_codex_agent_upload_sub2_hides_upstream_error(self, get_account, upload):
        get_account.return_value = {
            "id": 8,
            "email": "agent-error@example.test",
            "codex_agent_token": '{"agent_identity": {"email": "agent-error@example.test"}}',
        }

        response = self.client.post("/api/accounts/8/codex-agent/upload-sub2")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], "上传到 sub2api 失败，请检查配置或服务响应")
        self.assertNotIn("SECRET-ERROR", response.get_data(as_text=True))
        self.assertEqual(response.headers.get("Cache-Control"), "no-store, max-age=0")
        self.assertEqual(response.headers.get("Pragma"), "no-cache")
        upload.assert_called_once()

    @patch("core.codex_agent.upload_sub2api_account", side_effect=RuntimeError("upstream SECRET-BULK-ERROR"))
    @patch("webui.app.db.get_account")
    def test_codex_agent_upload_sub2_bulk_hides_upstream_error(self, get_account, upload):
        get_account.return_value = {
            "id": 9,
            "email": "agent-bulk@example.test",
            "codex_agent_status": "success",
            "codex_agent_token": '{"agent_identity": {"email": "agent-bulk@example.test"}}',
        }

        response = self.client.post(
            "/api/accounts/codex-agent/upload-sub2-bulk",
            json={"account_ids": [9]},
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["failed"][0]["error"], "上传到 sub2api 失败，请检查配置或服务响应")
        self.assertNotIn("SECRET-BULK-ERROR", response.get_data(as_text=True))
        self.assertEqual(response.headers.get("Cache-Control"), "no-store, max-age=0")
        self.assertEqual(response.headers.get("Pragma"), "no-cache")
        upload.assert_called_once()


if __name__ == "__main__":
    unittest.main()
