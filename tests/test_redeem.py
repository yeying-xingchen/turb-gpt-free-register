# -*- coding: utf-8 -*-
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from core import db
from webui.app import create_app


class RedeemTests(unittest.TestCase):
    @staticmethod
    def _storage_patches(root: Path) -> dict:
        missing = root / "missing.json"
        return {
            "_ACCOUNTS_JSON": root / "accounts.json",
            "_OUTLOOK_JSON": root / "outlook.json",
            "_GENERIC_API_EMAIL_JSON": root / "generic.json",
            "_DOMAIN_EMAIL_JSON": root / "domain.json",
            "_JOBS_JSON": root / "jobs.json",
            "_LEGACY_ACCOUNTS_JSON": missing,
            "_LEGACY_OUTLOOK_JSON": missing,
            "_LEGACY_JOBS_JSON": missing,
            "_LEGACY_SQLITE": root / "legacy.db",
            "_CODEX_DIR": root / "codex_accounts",
            "_CODEX_AGENT_DIR": root / "codex_agent_accounts",
            "_LEGACY_CODEX_EXPORT_STATE": root / "codex-export.json",
            "_SQLITE_READY": False,
            "_SQLITE_READY_PATH": None,
        }

    def test_public_redeem_exports_plus_login_credentials_once(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                db.insert_account(
                    email="plus@example.test",
                    access_token="at-plus",
                    plan_type="ChatGPT Plus",
                    totp_secret="TOTPSECRET",
                    extra={"registration_password": "plus-password"},
                )
                db.insert_account(
                    email="free@example.test",
                    access_token="at-free",
                    plan_type="free",
                    extra={"registration_password": "free-password"},
                )
                app = create_app(auth_code="admin-secret")
                client = app.test_client()

                self.assertEqual(client.get("/redeem").status_code, 200)
                self.assertEqual(client.get("/api/redeem/codes").status_code, 401)
                created = client.post(
                    "/api/redeem/codes",
                    json={"quantity": 1, "expires_in_days": 7, "note": "活动", "account_group": "默认分组"},
                    headers={"X-Auth-Code": "admin-secret"},
                )
                self.assertEqual(created.status_code, 201)
                code = created.get_json()["item"]["code"]
                self.assertEqual(created.get_json()["item"]["account_group"], "默认分组")

                response = client.post("/api/redeem", json={"cdk": code.lower()})
                self.assertEqual(response.status_code, 200)
                body = response.get_json()
                self.assertEqual(body["count"], 1)
                self.assertEqual(body["remaining"], 0)

                download = client.get(body["download_url"])
                self.assertEqual(download.status_code, 200)
                self.assertIn("plus@example.test---plus-password---TOTPSECRET", download.get_data(as_text=True))
                self.assertNotIn("at-plus", download.get_data(as_text=True))
                self.assertEqual(client.get(body["download_url"]).status_code, 404)
                self.assertEqual(client.post("/api/redeem", json={"cdk": code}).status_code, 410)

                listing = client.get("/api/redeem/codes", headers={"X-Auth-Code": "admin-secret"}).get_json()
                all_listing = client.get("/api/redeem/codes?limit=all", headers={"X-Auth-Code": "admin-secret"}).get_json()
                self.assertEqual(len(all_listing["items"]), 1)
                self.assertEqual(listing["items"][0]["status"], "exhausted")
                self.assertEqual(listing["items"][0]["redeemed_accounts"][0]["email"], "plus@example.test")
                self.assertEqual(listing["items"][0]["redeemed_accounts"][0]["account_id"], 1)
                self.assertTrue(listing["items"][0]["is_redeemed"])
                self.assertEqual(listing["stock"]["available"], 0)

    def test_redeem_listing_can_return_all_codes_and_marks_unredeemed_codes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                db.insert_account(
                    email="claimable@example.test",
                    access_token="at-claimable",
                    plan_type="plus",
                    extra={"registration_password": "password"},
                )
                first = db.create_redeem_code(quantity=1, account_group="默认分组")
                second = db.create_redeem_code(quantity=1, account_group="默认分组")
                db.redeem_plus_accounts(first["code"])

                items = db.list_redeem_codes(limit=None)
                self.assertEqual({item["code"] for item in items}, {first["code"], second["code"]})
                claimed = next(item for item in items if item["code"] == first["code"])
                unclaimed = next(item for item in items if item["code"] == second["code"])
                self.assertTrue(claimed["is_redeemed"])
                self.assertEqual([account["email"] for account in claimed["redeemed_accounts"]], ["claimable@example.test"])
                self.assertFalse(unclaimed["is_redeemed"])
                self.assertEqual(unclaimed["redeemed_accounts"], [])

    def test_redeem_requires_full_stock_before_consuming_code(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                db.insert_account(
                    email="plus-one@example.test",
                    access_token="at-one",
                    plan_type="plus",
                    extra={"registration_password": "password-one"},
                )
                code = db.create_redeem_code(quantity=2, account_group="默认分组")["code"]
                with self.assertRaises(db.RedeemError) as ctx:
                    db.redeem_plus_accounts(code)
                self.assertEqual(ctx.exception.code, "insufficient_stock")
                self.assertEqual(db.list_redeem_codes()[0]["redeemed_count"], 0)
                self.assertEqual(db.redeem_stock_summary()["available"], 1)

    def test_revoke_prevents_public_redeem(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with patch.multiple(db, **self._storage_patches(root)):
                code = db.create_redeem_code(quantity=1, expires_at=(datetime.now() + timedelta(days=1)).isoformat(), account_group="默认分组")["code"]
                item = db.list_redeem_codes()[0]
                self.assertEqual(db.revoke_redeem_code(item["id"])["status"], "revoked")
                with self.assertRaises(db.RedeemError) as ctx:
                    db.redeem_plus_accounts(code)
                self.assertEqual(ctx.exception.code, "code_revoked")


if __name__ == "__main__":
    unittest.main()
