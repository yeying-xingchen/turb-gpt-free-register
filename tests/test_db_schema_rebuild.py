# -*- coding: utf-8 -*-
"""生成列重建与接口错误格式的回归测试。

背景（用户可见的故障）：「分组加载失败：服务响应异常（500）」。

账号列表的筛选条件被固化成了 SQLite 生成列 + 覆盖索引，表达式指纹变化时
``_ensure_account_filter_schema`` 会重建这些列。重建要先删索引再删列，而旧版本
留下的索引（改过名字、已不在 ``_ACCOUNT_FILTER_INDEXES`` 里）如果还引用生成列，
``ALTER TABLE accounts DROP COLUMN`` 会报
``error in index ...: no such column``，异常从 ``_ensure_sqlite()`` 抛到**每个**
接口，指纹也写不回去，于是每次请求都是 500。Flask 默认返回 HTML 错误页，
前端 ``response.json()`` 解析失败，只能显示「服务响应异常（500）」。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db
from webui.app import create_app


class AccountFilterSchemaRebuildTests(unittest.TestCase):
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

    def test_stale_index_on_generated_column_is_cleaned_up_instead_of_failing(self):
        """旧索引引用生成列时，重建必须自愈，而不是让所有接口 500。"""
        with tempfile.TemporaryDirectory() as td:
            with patch.multiple(db, **self.storage(Path(td))):
                account_id = db.insert_account(
                    email="stale@example.test",
                    access_token="at-stale",
                    plan_type="free",
                    extra={"registration_password": "password"},
                )
                db.update_accounts_group([account_id], "VIP")
                with db.closing(db._sqlite_conn()) as conn:
                    # 模拟历史版本留下的索引：名字已经不在当前清单里，但引用生成列。
                    conn.execute("CREATE INDEX idx_accounts_at_expired ON accounts(at_state_exp)")
                    conn.execute("CREATE INDEX idx_accounts_group_archive ON accounts(group_key, archived)")
                    # 模拟表达式变化：指纹对不上，触发一次重建。
                    conn.execute(
                        "UPDATE storage_meta SET value='stale-fingerprint' WHERE key=?",
                        (db._ACCOUNT_FILTER_SCHEMA_KEY,),
                    )
                    conn.commit()
                db._SQLITE_READY = False  # 让下一次调用重新执行建表/迁移

                groups = db.list_account_groups()
                self.assertEqual(
                    {x["group_name"]: x["total"] for x in groups}, {"默认分组": 0, "VIP": 1}
                )
                with db.closing(db._sqlite_conn()) as conn:
                    names = {
                        str(row[0])
                        for row in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='accounts'"
                        )
                    }
                    recorded = conn.execute(
                        "SELECT value FROM storage_meta WHERE key=?", (db._ACCOUNT_FILTER_SCHEMA_KEY,)
                    ).fetchone()
                    columns = {str(row[1]).lower() for row in conn.execute("PRAGMA table_xinfo(accounts)")}
                self.assertNotIn("idx_accounts_at_expired", names)
                self.assertNotIn("idx_accounts_group_archive", names)
                for name, _definition in db._ACCOUNT_FILTER_INDEXES:
                    self.assertIn(name, names)
                for name, _expression in db._ACCOUNT_GENERATED_COLUMNS:
                    self.assertIn(name, columns)
                self.assertIsNotNone(recorded)
                self.assertNotEqual(str(recorded[0]), "stale-fingerprint")

    def test_account_groups_survives_missing_redeemable_index(self):
        """INDEXED BY 只是执行计划提示，索引缺失时不能变成 500。"""
        with tempfile.TemporaryDirectory() as td:
            with patch.multiple(db, **self.storage(Path(td))):
                account_id = db.insert_account(
                    email="missing-index@example.test",
                    access_token="at-missing",
                    plan_type="free",
                    extra={"registration_password": "password"},
                )
                db.update_accounts_group([account_id], "VIP")
                self.assertEqual(len(db.list_account_groups()), 2)
                with db.closing(db._sqlite_conn()) as conn:
                    conn.execute("DROP INDEX idx_accounts_redeemable")
                    conn.commit()
                groups = {x["group_name"]: x for x in db.list_account_groups()}
                self.assertEqual(groups["VIP"]["total"], 1)
                self.assertEqual(groups["VIP"]["redeemable"], 1)

    def test_api_exception_returns_json_message_instead_of_html(self):
        """接口异常要返回可读的 JSON，前端才不会只显示「服务响应异常（500）」。"""
        app = create_app(auth_code="secret")
        client = app.test_client()
        with patch.object(db, "list_account_groups", side_effect=RuntimeError("schema broken")):
            response = client.get("/api/account-groups", headers={"X-Auth-Code": "secret"})
        self.assertEqual(response.status_code, 500)
        self.assertIn("application/json", response.headers.get("Content-Type", ""))
        payload = response.get_json()
        self.assertFalse(payload["ok"])
        self.assertIn("RuntimeError", payload["error"])
        self.assertIn("schema broken", payload["error"])

    def test_public_endpoint_error_hides_internal_detail(self):
        """公开兑换接口的 500 不回内部细节，但仍返回 JSON。"""
        app = create_app(auth_code="secret")
        client = app.test_client()
        with patch.object(db, "list_account_groups", side_effect=RuntimeError("secret detail")):
            response = client.get("/api/redeem/public-stock")
        self.assertEqual(response.status_code, 500)
        self.assertIn("application/json", response.headers.get("Content-Type", ""))
        error = response.get_json()["error"]
        self.assertNotIn("RuntimeError", error)
        self.assertNotIn("secret detail", error)

    def test_unknown_api_path_returns_json_not_html(self):
        app = create_app(auth_code="secret")
        client = app.test_client()
        response = client.get("/api/not-a-real-endpoint", headers={"X-Auth-Code": "secret"})
        self.assertEqual(response.status_code, 404)
        self.assertIn("application/json", response.headers.get("Content-Type", ""))
        self.assertFalse(response.get_json()["ok"])

    def test_non_api_route_keeps_html_error_page(self):
        app = create_app(auth_code="secret")
        client = app.test_client()
        response = client.get("/not-a-real-page")
        self.assertNotIn("application/json", response.headers.get("Content-Type", ""))


if __name__ == "__main__":
    unittest.main()
