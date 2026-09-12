# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from webui.app import create_app


class PaymentApiTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(auth_code="payment-auth")
        self.client = self.app.test_client()
        self.headers = {"X-Auth-Code": "payment-auth"}

    def test_single_requires_json_object_and_server_token(self):
        response = self.client.post("/api/accounts/check-payment", headers=self.headers)
        self.assertEqual(response.status_code, 400)
        with patch("webui.app.db.get_account", return_value={"id": 7, "email": "a@example.com", "access_token": "server-token"}), patch("webui.app.payment_method_service.enqueue_account_payment_method_check", return_value={"accepted": True, "status": "queued"}) as enqueue:
            response = self.client.post("/api/accounts/check-payment", json={"account_id": 7, "access_token": "browser-token", "regions": ["gcash"]}, headers=self.headers)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(enqueue.call_args.kwargs["access_token"], "server-token")
        self.assertEqual(enqueue.call_args.kwargs["regions"], ["gcash"])

    def test_single_rejects_unknown_region(self):
        response = self.client.post("/api/accounts/check-payment", json={"account_id": 7, "regions": ["unknown"]}, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn("supported_regions", response.get_json())

    def test_bulk_validates_ids_and_auth(self):
        response = self.client.post("/api/accounts/check-payment-bulk", json={"account_ids": ["nope"]}, headers=self.headers)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["skipped_count"], 1)
        response = self.client.post("/api/accounts/check-payment-bulk", json={"account_ids": []}, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/api/accounts/check-payment-bulk", json={"account_ids": [1]}, headers={})
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
