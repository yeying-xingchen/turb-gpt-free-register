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
    def test_extract_link_cdk_response_is_not_cacheable(self, query_cdk):
        query_cdk.return_value = {"backend": "cdk", "remaining": 3}

        response = self.client.get("/api/extract-link/cdk?code=secret-cdk")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["remaining"], 3)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store, max-age=0")
        self.assertEqual(response.headers.get("Pragma"), "no-cache")
        query_cdk.assert_called_once_with(cdk="secret-cdk")


if __name__ == "__main__":
    unittest.main()
