# -*- coding: utf-8 -*-
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db
from webui.app import create_app


class AccountGroupRedeemTests(unittest.TestCase):
    @staticmethod
    def storage(root: Path) -> dict:
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

    def test_group_can_ship_free_account_and_stock_excludes_claim(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.multiple(db, **self.storage(Path(td))):
                account_id = db.insert_account(
                    email="free-vip@example.test",
                    access_token="at-free-vip",
                    plan_type="free",
                    extra={"registration_password": "free-password"},
                )
                db.update_account_group_meta("VIP", redeem_prefix="vip", public_stock=True)
                db.update_accounts_group([account_id], "VIP")
                code = db.create_redeem_code(quantity=1, account_group="VIP")
                self.assertTrue(code["code"].startswith("VIP-"))
                self.assertEqual(code["expires_at"], "")
                self.assertEqual(db.redeem_stock_summary("VIP")["available"], 1)

                result = db.redeem_plus_accounts(code["code"])
                self.assertEqual(result["accounts"][0]["account_id"], account_id)
                self.assertIn("free-vip@example.test---free-password", result["lines"][0])
                self.assertEqual(db.redeem_stock_summary("VIP")["available"], 0)
                group = next(x for x in db.list_account_groups() if x["group_name"] == "VIP")
                self.assertEqual(group["total"], 1)
                self.assertEqual(group["redeemable"], 0)

    def test_new_code_requires_known_group_and_legacy_code_remains_plus_only(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.multiple(db, **self.storage(Path(td))):
                plus_id = db.insert_account(
                    email="plus@example.test", access_token="at-plus", plan_type="ChatGPT Plus",
                    extra={"registration_password": "plus-password"},
                )
                db.insert_account(
                    email="free@example.test", access_token="at-free", plan_type="free",
                    extra={"registration_password": "free-password"},
                )
                with self.assertRaises(db.RedeemError) as ctx:
                    db.create_redeem_code(quantity=1, account_group="UNKNOWN")
                self.assertEqual(ctx.exception.code, "group_not_found")
                db._ensure_sqlite()
                with db.closing(db._sqlite_conn()) as conn:
                    conn.execute(
                        "INSERT INTO redeem_codes(code,quantity,redeemed_count,status,account_group,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                        ("LEGACY-CODE", 1, 0, "active", None, db._now(), db._now()),
                    )
                    conn.commit()
                self.assertEqual(db.redeem_plus_accounts("legacy-code")["accounts"][0]["account_id"], plus_id)
                self.assertEqual(db.redeem_stock_summary()["available"], 0)

    def test_public_stock_is_anonymous_and_only_published_groups_are_returned(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.multiple(db, **self.storage(Path(td))):
                account_id = db.insert_account(
                    email="public@example.test", access_token="at-public", plan_type="free",
                    extra={"registration_password": "password"},
                )
                db.update_account_group_meta("PUBLIC", public_stock=True)
                db.update_accounts_group([account_id], "PUBLIC")
                db.update_account_group_meta("HIDDEN", public_stock=False)
                app = create_app(auth_code="secret")
                client = app.test_client()
                response = client.get("/api/redeem/public-stock")
                self.assertEqual(response.status_code, 200)
                self.assertEqual([x["group_name"] for x in response.get_json()["groups"]], ["PUBLIC"])
                self.assertEqual(client.get("/api/account-groups").status_code, 401)

    def test_delete_blocks_active_code_then_moves_accounts_after_revoke(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.multiple(db, **self.storage(Path(td))):
                account_id = db.insert_account(
                    email="delete@example.test", access_token="at-delete", plan_type="free",
                    extra={"registration_password": "password"},
                )
                db.update_accounts_group([account_id], "TEMP")
                code = db.create_redeem_code(quantity=1, account_group="TEMP")
                with self.assertRaises(ValueError):
                    db.delete_account_group("TEMP")
                row = db.list_redeem_codes()[0]
                db.revoke_redeem_code(row["id"])
                moved, target = db.delete_account_group("TEMP")
                self.assertEqual((moved, target), (1, "默认分组"))
                self.assertEqual(db.get_account(account_id)["group_name"], "默认分组")
                self.assertEqual(db.list_redeem_codes()[0]["account_group"], "TEMP")

    def test_legacy_account_without_group_name_matches_default_group(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.multiple(db, **self.storage(Path(td))):
                account_id = db.insert_account(
                    email="legacy@example.test", access_token="at-legacy", plan_type="free",
                    extra={"registration_password": "password"},
                )
                db._ensure_sqlite()
                with db.closing(db._sqlite_conn()) as conn:
                    row = conn.execute("SELECT payload FROM accounts WHERE id=?", (account_id,)).fetchone()
                    payload = json.loads(row["payload"])
                    payload.pop("group_name", None)
                    conn.execute("UPDATE accounts SET payload=? WHERE id=?", (json.dumps(payload), account_id))
                    conn.commit()
                code = db.create_redeem_code(quantity=1, account_group="默认分组")
                self.assertEqual(db.redeem_plus_accounts(code["code"])["count"], 1)


        with tempfile.TemporaryDirectory() as td:
            with patch.multiple(db, **self.storage(Path(td))):
                account_id = db.insert_account(
                    email="filter@example.test", access_token="at-filter", plan_type="free",
                    extra={"registration_password": "password"},
                )
                app = create_app(auth_code="secret")
                client = app.test_client()
                headers = {"X-Auth-Code": "secret"}
                moved = client.post(
                    "/api/accounts/group-bulk",
                    json={"account_ids": [account_id], "group_name": "FILTER"},
                    headers=headers,
                )
                self.assertEqual(moved.status_code, 200)
                filtered = client.get("/api/accounts?paged=1&page=1&page_size=20&group=FILTER", headers=headers)
                self.assertEqual(filtered.status_code, 200)
                self.assertEqual(filtered.get_json()["total"], 1)
                self.assertEqual(filtered.get_json()["items"][0]["group_name"], "FILTER")


        with tempfile.TemporaryDirectory() as td:
            with patch.multiple(db, **self.storage(Path(td))):
                account_id = db.insert_account(
                    email="ordered@example.test", access_token="at-ordered", plan_type="free",
                    extra={"registration_password": "password"},
                )
                code = db.create_redeem_code(quantity=1, account_group="默认分组")
                observed = {}
                def enqueue(**kwargs):
                    observed.update(kwargs)
                    observed["claimed"] = bool(db.list_redeem_codes()[0]["redeemed_accounts"])
                    return {"accepted": True}
                app = create_app(auth_code="secret")
                client = app.test_client()
                with patch("webui.app.plan_check_service.enqueue_account_plan_check", side_effect=enqueue):
                    response = client.post("/api/redeem", json={"cdk": code["code"]})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(observed["account_id"], account_id)
                self.assertTrue(observed["claimed"])
                self.assertEqual(observed["trigger"], "redeem_after_ship")

    def test_group_bulk_updates_multiple_accounts_and_skips_missing_ids(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.multiple(db, **self.storage(Path(td))):
                first_id = db.insert_account(
                    email="first-bulk@example.test", access_token="at-first", plan_type="free",
                    extra={"registration_password": "password"},
                )
                second_id = db.insert_account(
                    email="second-bulk@example.test", access_token="at-second", plan_type="free",
                    extra={"registration_password": "password"},
                )
                app = create_app(auth_code="secret")
                client = app.test_client()
                response = client.post(
                    "/api/accounts/group-bulk",
                    json={"account_ids": [first_id, second_id, 99999], "group_name": "批量 VIP"},
                    headers={"X-Auth-Code": "secret"},
                )

                self.assertEqual(response.status_code, 200)
                payload = response.get_json()
                self.assertEqual(payload["updated_count"], 2)
                self.assertEqual(payload["skipped_count"], 1)
                self.assertEqual(
                    {db.get_account(first_id)["group_name"], db.get_account(second_id)["group_name"]},
                    {"批量 VIP"},
                )
                group = next(item for item in payload["groups"] if item["group_name"] == "批量 VIP")
                self.assertEqual(group["total"], 2)


if __name__ == "__main__":
    unittest.main()
