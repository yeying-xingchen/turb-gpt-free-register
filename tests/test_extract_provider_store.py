# -*- coding: utf-8 -*-
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from core import db
from core import extract_provider_store as store
from webui import extract_routes


class ExtractProviderStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        missing = root / "missing"
        self.paths = {
            "_ACCOUNTS_JSON": root / "accounts.json",
            "_OUTLOOK_JSON": root / "outlook.json",
            "_GENERIC_API_EMAIL_JSON": root / "generic.json",
            "_DOMAIN_EMAIL_JSON": root / "domain.json",
            "_JOBS_JSON": root / "jobs.json",
            "_LEGACY_ACCOUNTS_JSON": missing,
            "_LEGACY_OUTLOOK_JSON": missing,
            "_LEGACY_JOBS_JSON": missing,
            "_LEGACY_SQLITE": root / "legacy.db",
            "_CODEX_DIR": root / "codex",
            "_CODEX_AGENT_DIR": root / "codex-agent",
            "_LEGACY_CODEX_EXPORT_STATE": root / "codex-export.json",
            "_DATA_DIR": root,
            "_LOG_DIR": root / "logs",
            "_SQLITE_READY": False,
            "_SQLITE_READY_PATH": None,
        }
        self.storage = patch.multiple(db, **self.paths)
        self.storage.start()
        self.addCleanup(self.storage.stop)

    def test_seed_is_transactional_and_cdk_output_is_masked(self):
        store.ensure_defaults()
        providers = store.list_providers()
        self.assertEqual(len(providers), 1)
        provider = providers[0]
        self.assertEqual(provider["name"], "Lumen Flow")
        self.assertEqual(provider["api_base"], "https://api.int31.space")
        created = store.save_cdk(provider["id"], {"cdk": "ABCD-SECRET-1234", "memo": "main"})
        self.assertNotIn("cdk", created)
        self.assertEqual(created["display_suffix"], "1234")
        listing = store.list_providers()
        self.assertEqual(listing[0]["cdks"][0]["display_suffix"], "1234")
        self.assertNotIn("SECRET", json.dumps(listing))
        store.ensure_defaults()
        self.assertEqual(len(store.list_providers()), 1)

    def test_validation_and_provider_specific_duplicate_semantics(self):
        provider = store.save_provider({
            "name": "Legacy", "api_base": "https://legacy.example.test", "provider_type": "extract",
            "default_link_type": "pix", "enabled": True, "is_default": True, "note": "",
        })
        store.save_cdk(provider["id"], {"cdk": "CaseCode", "enabled": True, "memo": ""})
        store.save_cdk(provider["id"], {"cdk": "casecode", "enabled": True, "memo": ""})
        lumen = store.save_provider({
            "name": "Lumen", "api_base": "https://lumen.example.test", "provider_type": "lumen",
            "default_link_type": "BLIK", "enabled": True, "is_default": False, "note": "",
        })
        store.save_cdk(lumen["id"], {"cdk": "LumenCode", "enabled": True, "memo": ""})
        with self.assertRaises(ValueError):
            store.save_cdk(lumen["id"], {"cdk": "lumencode", "enabled": True, "memo": ""})
        with self.assertRaises(ValueError):
            store.save_provider({"name": "Bad", "api_base": "https://x.test/?secret=x"})
        with self.assertRaises(ValueError):
            store.save_provider({"name": "Bad", "api_base": "https://user:pass@x.test"})

    def test_active_lumen_task_blocks_provider_base_type_and_delete(self):
        provider = store.save_provider({
            "name": "Lumen", "api_base": "https://lumen.example.test", "provider_type": "lumen",
            "default_link_type": "ideal", "enabled": True, "is_default": True, "note": "",
        })
        with db._sqlite_conn() as conn:
            conn.execute(
                "INSERT INTO accounts(id,email,status,payload) VALUES(?,?,?,?)",
                (91, "active@example.test", "", json.dumps({
                    "id": 91, "extract_link_provider_id": provider["id"],
                    "extract_link_provider_type": "lumen", "extract_link_status": "awaiting_blik",
                })),
            )
        with self.assertRaises(RuntimeError):
            store.save_provider({"api_base": "https://other.example.test"}, provider["id"])
        with self.assertRaises(RuntimeError):
            store.delete_provider(provider["id"])

    def test_management_routes_mask_and_validate_upstream(self):
        app = Flask(__name__)
        extract_routes.register_extract_routes(app)
        app.config["TESTING"] = True
        client = app.test_client()
        response = client.post("/api/extract-link/providers", json={
            "name": "Route provider", "provider_type": "extract", "api_base": "https://route.example.test",
            "default_link_type": "pix", "enabled": True, "is_default": False, "note": "",
        })
        self.assertEqual(response.status_code, 201)
        provider_id = response.get_json()["item"]["id"]
        response = client.post(f"/api/extract-link/providers/{provider_id}/cdks", json={"cdk": "RAW-ROUTE-CODE"})
        self.assertEqual(response.status_code, 201)
        body = response.get_json()
        self.assertNotIn("cdk", body["item"])
        cdk_id = body["item"]["id"]
        with patch.object(extract_routes.service, "query_cdk", return_value={"ok": True, "cdk": "RAW-ROUTE-CODE", "remaining": 2}) as query:
            response = client.post(f"/api/extract-link/cdks/{cdk_id}/validate")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("RAW-ROUTE-CODE", response.get_data(as_text=True))
        query.assert_called_once_with(provider_id=provider_id, cdk_id=cdk_id)


if __name__ == "__main__":
    unittest.main()
