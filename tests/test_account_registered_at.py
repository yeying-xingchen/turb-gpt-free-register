# -*- coding: utf-8 -*-
"""注册完成时间字段与日期筛选回归测试。"""
from __future__ import annotations

import json
import sqlite3

from core import db
from webui.app import create_app


AUTH = {"X-Auth-Code": "registered-at-auth"}


def _insert(email: str, registered_at: str) -> int:
    return db.insert_account(
        email=email,
        access_token=f"at-{email}",
        registered_at=registered_at,
    )


def test_registered_at_is_persisted_and_duplicate_save_preserves_original():
    account_id = _insert("registered@example.test", "2025-01-02T03:04:05")

    first = db.get_account(account_id)
    assert first["registered_at"] == "2025-01-02T03:04:05"
    with sqlite3.connect(db._SQLITE_PATH) as conn:
        conn.row_factory = sqlite3.Row
        stored = conn.execute(
            "SELECT registered_at, payload FROM accounts WHERE id=?", (account_id,)
        ).fetchone()
    assert stored["registered_at"] == "2025-01-02T03:04:05"
    assert json.loads(stored["payload"])["registered_at"] == "2025-01-02T03:04:05"

    same_id = db.insert_account(
        email="registered@example.test",
        access_token="refreshed-token",
        registered_at="2026-06-07T08:09:10",
    )
    assert same_id == account_id
    refreshed = db.get_account(account_id)
    assert refreshed["access_token"] == "refreshed-token"
    assert refreshed["registered_at"] == "2025-01-02T03:04:05"


def test_registered_date_filter_is_inclusive_and_consistent_across_apis():
    before_id = _insert("before@example.test", "2025-01-01T23:59:59")
    boundary_id = _insert("boundary@example.test", "2025-01-02T00:00:00")
    end_id = _insert("end@example.test", "2025-01-02T23:59:59.999999")
    after_id = _insert("after@example.test", "2025-01-03T00:00:00")
    unicode_id = _insert("边界@example.test", "2025-01-02T12:00:00")

    expected = {boundary_id, end_id, unicode_id}
    page = db.list_accounts_page(
        limit=50,
        date_from="2025-01-02",
        date_to="2025-01-02",
    )
    assert {int(row["id"]) for row in page["items"]} == expected
    assert before_id not in expected and after_id not in expected

    ids = db.list_account_ids_page(
        limit=50,
        date_from="2025-01-02",
        date_to="2025-01-02",
    )
    assert set(ids["ids"]) == expected

    # 非 ASCII 邮箱走 Python fallback，也必须以 registered_at 为准。
    matched = db.find_accounts_by_emails(
        ["边界@example.test"],
        date_from="2025-01-02",
        date_to="2025-01-02",
    )
    assert [int(row["id"]) for row in matched] == [unicode_id]

    client = create_app(auth_code=AUTH["X-Auth-Code"]).test_client()
    query = {"date_from": "2025-01-02", "date_to": "2025-01-02", "paged": "1"}
    response = client.get("/api/accounts", query_string=query, headers=AUTH)
    assert response.status_code == 200
    body = response.get_json()
    assert {int(row["id"]) for row in body["items"]} == expected
    assert all(row["registered_at"] for row in body["items"])

    response = client.get("/api/accounts/ids", query_string=query, headers=AUTH)
    assert response.status_code == 200
    assert set(response.get_json()["ids"]) == expected

    response = client.post(
        "/api/accounts/lookup",
        json={
            "emails": ["boundary@example.test", "边界@example.test", "after@example.test"],
            "date_from": "2025-01-02",
            "date_to": "2025-01-02",
        },
        headers=AUTH,
    )
    assert response.status_code == 200
    lookup = response.get_json()
    assert {int(row["id"]) for row in lookup["matches"]} == {boundary_id, unicode_id}
    assert lookup["not_found"] == ["after@example.test"]


def test_existing_accounts_table_gets_registered_at_and_backfills_created_at(tmp_path):
    # 模拟升级前只有 created_at 的最小 SQLite 表；启动逻辑会创建其余表并迁移该表。
    path = db._SQLITE_PATH
    payload = {
        "id": 7,
        "email": "legacy@example.test",
        "created_at": "2024-12-31T23:00:00",
        "access_token": "legacy-token",
    }
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE accounts (
                id INTEGER NOT NULL,
                email TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                archived INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                payload TEXT NOT NULL,
                PRIMARY KEY (id)
            );
            INSERT INTO accounts(id,email,created_at,updated_at,payload)
            VALUES(7,'legacy@example.test','2024-12-31T23:00:00','2024-12-31T23:00:00',
                   '{"id":7,"email":"legacy@example.test","created_at":"2024-12-31T23:00:00","access_token":"legacy-token"}');
            """
        )

    db._SQLITE_READY = False
    db._SQLITE_READY_PATH = None
    rows = db.list_accounts(limit=10, archived="all")
    assert rows[0]["registered_at"] == "2024-12-31T23:00:00"

    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(accounts)")}
        assert "registered_at" in columns
        stored = conn.execute(
            "SELECT registered_at, payload FROM accounts WHERE id=7"
        ).fetchone()
        assert stored[0] == "2024-12-31T23:00:00"
        assert json.loads(stored[1])["registered_at"] == "2024-12-31T23:00:00"
        indexes = {row[1] for row in conn.execute("PRAGMA index_list(accounts)")}
        assert "idx_accounts_archived_registered" in indexes
