# -*- coding: utf-8 -*-
"""已保存支付平台 / CDK 的存储与路由：明文只允许服务端读取。"""
import json
import unittest
from unittest.mock import patch

from flask import Flask

from core import db
from core import payment_provider_store as store
from webui import payment_provider_routes as routes

CDK = "PBK-04A0-3254-AEB7-9C31"
OTHER = "PBK-1111-2222-3333-4444"


class PaymentProviderStoreTests(unittest.TestCase):
    def setUp(self):
        db._ensure_sqlite()
        store.ensure_defaults()

    def test_defaults_seed_once_and_keep_v1_default(self):
        store.ensure_defaults()
        providers = store.list_providers()
        self.assertEqual([item["provider_type"] for item in providers], ["v1", "masi", "orderhub", "seashore"])
        self.assertEqual([item["provider_type"] for item in providers if item["is_default"]], ["v1"])
        self.assertEqual(store.get_provider_by_type("seashore")["effective_api_base"],
                         "https://seashore.lol/api/publisher")
        store.ensure_defaults()
        self.assertEqual(len(store.list_providers()), 4)

    def test_saved_cdk_is_masked_everywhere(self):
        provider = store.get_provider_by_type("seashore")
        created = store.save_cdk(provider["id"], {"cdk": CDK, "memo": "主号"})
        self.assertNotIn("cdk", created)
        self.assertEqual(created["display_suffix"], "9C31")
        self.assertEqual(created["masked"], "PBK-…9C31")
        listing = json.dumps(store.list_providers())
        self.assertNotIn(CDK, listing)
        self.assertNotIn("3254", listing)
        summary = json.dumps(store.credential_summary("seashore"))
        self.assertNotIn(CDK, summary)
        self.assertEqual(json.loads(summary)["cdks"][0]["display_suffix"], "9C31")

    def test_duplicate_and_cross_provider_credentials(self):
        seashore = store.get_provider_by_type("seashore")
        masi = store.get_provider_by_type("masi")
        store.save_cdk(seashore["id"], {"cdk": CDK})
        with self.assertRaises(ValueError):
            store.save_cdk(seashore["id"], {"cdk": CDK})
        store.save_cdk(masi["id"], {"cdk": CDK})  # 同一条凭据可以存在不同平台下
        with self.assertRaises(ValueError):
            store.save_cdk(seashore["id"], {"cdk": "has space"})
        with self.assertRaises(ValueError):
            store.save_cdk(seashore["id"], {"cdk": ""})

    def test_resolve_credential_requires_explicit_choice(self):
        seashore = store.get_provider_by_type("seashore")
        saved = store.save_cdk(seashore["id"], {"cdk": CDK})
        resolved = store.resolve_credential(provider="seashore", cdk_id=saved["id"])
        self.assertEqual(resolved["cdk"], CDK)
        self.assertEqual(resolved["provider_type"], "seashore")
        self.assertEqual(store.resolve_credential(provider="seashore", cdk=OTHER)["cdk"], OTHER)
        with self.assertRaises(ValueError):
            store.resolve_credential(provider="seashore")
        with self.assertRaises(ValueError):
            store.resolve_credential(provider="seashore", cdk=OTHER, cdk_id=saved["id"])
        with self.assertRaises(ValueError):
            store.resolve_credential(provider="masi", cdk_id=saved["id"])
        with self.assertRaises(LookupError):
            store.resolve_credential(provider="seashore", cdk_id=saved["id"] + 999)
        with self.assertRaises(ValueError):
            store.resolve_credential(provider="unknown", cdk=OTHER)

    def test_disabled_cdk_or_provider_is_refused(self):
        seashore = store.get_provider_by_type("seashore")
        saved = store.save_cdk(seashore["id"], {"cdk": CDK, "enabled": False})
        with self.assertRaises(ValueError):
            store.resolve_credential(provider="seashore", cdk_id=saved["id"])
        store.save_cdk(seashore["id"], {"cdk": CDK, "enabled": True}, cdk_id=saved["id"])
        self.assertEqual(store.resolve_credential(provider="seashore", cdk_id=saved["id"])["cdk"], CDK)
        store.save_provider({"enabled": False}, seashore["id"])
        with self.assertRaises(ValueError):
            store.resolve_credential(provider="seashore", cdk_id=saved["id"])

    def test_provider_type_cannot_change_while_credentials_exist(self):
        seashore = store.get_provider_by_type("seashore")
        store.save_cdk(seashore["id"], {"cdk": CDK})
        with self.assertRaises(ValueError):
            store.save_provider({"provider_type": "masi"}, seashore["id"])
        with self.assertRaises(ValueError):
            store.save_provider({"api_base": "https://user:pass@x.test"}, seashore["id"])
        with self.assertRaises(ValueError):
            store.save_provider({"api_base": "https://x.test/?token=1"}, seashore["id"])
        store.save_provider({"api_base": "https://publisher.example.test"}, seashore["id"])
        self.assertEqual(store.get_provider_by_type("seashore")["api_base"], "https://publisher.example.test")

    def test_default_moves_when_another_provider_is_marked(self):
        masi = store.get_provider_by_type("masi")
        store.save_provider({"is_default": True}, masi["id"])
        defaults = [item["provider_type"] for item in store.list_providers() if item["is_default"]]
        self.assertEqual(defaults, ["masi"])

    def test_delete_provider_removes_its_credentials(self):
        seashore = store.get_provider_by_type("seashore")
        saved = store.save_cdk(seashore["id"], {"cdk": CDK})
        store.delete_provider(seashore["id"])
        self.assertIsNone(store.get_cdk(saved["id"]))
        self.assertIsNone(store.get_provider_by_type("seashore"))
        with self.assertRaises(LookupError):
            store.delete_cdk(saved["id"])


class PaymentProviderRouteTests(unittest.TestCase):
    def setUp(self):
        db._ensure_sqlite()
        app = Flask(__name__)
        routes.register_payment_provider_routes(app)
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_management_routes_never_return_raw_credentials(self):
        store.ensure_defaults()
        listing = self.client.get("/api/payment-providers").get_json()["items"]
        provider_id = next(item["id"] for item in listing if item["provider_type"] == "seashore")
        response = self.client.post(f"/api/payment-providers/{provider_id}/cdks", json={"cdk": CDK, "memo": "路由"})
        self.assertEqual(response.status_code, 201)
        body = response.get_json()
        self.assertNotIn("cdk", body["item"])
        self.assertNotIn(CDK, response.get_data(as_text=True))
        cdk_id = body["item"]["id"]
        text = self.client.get("/api/payment-providers").get_data(as_text=True)
        self.assertNotIn(CDK, text)
        summary = self.client.get("/api/payment-providers/seashore/credentials")
        self.assertEqual(summary.status_code, 200)
        self.assertNotIn(CDK, summary.get_data(as_text=True))

        with patch.object(routes.service, "verify_cdk",
                          return_value={"data": {"remaining_uses": 8, "balance": 8}, "http_status": 200}) as verify:
            response = self.client.post(f"/api/payment-cdks/{cdk_id}/validate")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["result"]["data"]["remaining_uses"], 8)
        self.assertNotIn(CDK, response.get_data(as_text=True))
        verify.assert_called_once_with(provider="seashore", cdk=CDK)

    def test_upstream_401_is_not_reported_as_local_401(self):
        store.ensure_defaults()
        provider_id = store.get_provider_by_type("seashore")["id"]
        cdk_id = self.client.post(f"/api/payment-providers/{provider_id}/cdks",
                                  json={"cdk": CDK}).get_json()["item"]["id"]
        from core.scan_api_client import ScanApiError
        error = ScanApiError("CDK 已被禁用", status=401, code="invalid_credential")
        with patch.object(routes.service, "verify_cdk", side_effect=error):
            response = self.client.post(f"/api/payment-cdks/{cdk_id}/validate")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["code"], "invalid_credential")
        self.assertNotIn(CDK, response.get_data(as_text=True))

    def test_validation_errors_and_missing_records(self):
        store.ensure_defaults()
        provider_id = store.get_provider_by_type("seashore")["id"]
        self.assertEqual(self.client.post(f"/api/payment-providers/{provider_id}/cdks",
                                          json={"cdk": " "}).status_code, 400)
        self.assertEqual(self.client.put("/api/payment-cdks/9999", json={"memo": "x"}).status_code, 404)
        self.assertEqual(self.client.delete("/api/payment-cdks/9999").status_code, 404)
        self.assertEqual(self.client.get("/api/payment-providers/nope/credentials").status_code, 400)


if __name__ == "__main__":
    unittest.main()
