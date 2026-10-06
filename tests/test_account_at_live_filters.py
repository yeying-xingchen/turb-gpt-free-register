"""账号列表支持按「AT 已过期」和「查活失败」筛选。"""
from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from core import db
from webui.app import create_app

AUTH = {"X-Auth-Code": "at-live-filter-auth"}


def jwt_with_exp(exp: float, email: str = "user@example.test") -> str:
    """构造只带 exp 的假 JWT；筛选只解析 payload，不校验签名。"""
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": exp, "https://api.openai.com/profile": {"email": email}}).encode()
    ).decode().rstrip("=")
    return f"header.{payload}.signature"


def iso(offset_seconds: float) -> str:
    stamp = datetime.now(tz=timezone.utc) + timedelta(seconds=offset_seconds)
    return stamp.isoformat().replace("+00:00", "Z")


@pytest.fixture
def mixed_accounts():
    """五个账号覆盖 AT 过期/正常/未知与查活失败/正常/已停用/未查活。"""
    now = time.time()
    ids = {
        # 套餐查询写回「已过期」，AT 本身还没到点。
        "flag_expired": db.insert_account(
            email="flag-expired@example.test", access_token=jwt_with_exp(now + 3600)
        ),
        # 标记还写着未过期，但过期时间已经到点（时间流逝让旧结果失效）。
        "stale_valid": db.insert_account(
            email="stale-valid@example.test", access_token=jwt_with_exp(now + 3600)
        ),
        # 从未查过套餐，直接解析 access_token 的 exp。
        "jwt_expired": db.insert_account(
            email="jwt-expired@example.test", access_token=jwt_with_exp(now - 120)
        ),
        "jwt_valid": db.insert_account(
            email="jwt-valid@example.test", access_token=jwt_with_exp(now + 7200)
        ),
        # 非 JWT 且没有任何过期信息：状态未知。
        "unknown": db.insert_account(email="unknown@example.test", access_token="not-a-jwt"),
    }
    db.update_account_plan_check(
        acc_id=ids["flag_expired"],
        result={"ok": False, "error": "AT已过期/失效", "token_expired": True, "token_expires_at": iso(3600)},
    )
    db.update_account_plan_check(
        acc_id=ids["stale_valid"],
        result={"ok": False, "error": "HTTP 401", "token_expired": False, "token_expires_at": iso(-60)},
    )
    db.update_account_liveness(ids["jwt_expired"], {"ok": False, "status": "failed", "error": "登录失败"})
    db.update_account_liveness(ids["flag_expired"], {"ok": False, "status": "failed", "error": "AT 已过期"})
    db.update_account_liveness(ids["jwt_valid"], {"ok": True, "status": "live"})
    db.update_account_liveness(
        ids["stale_valid"], {"ok": False, "status": "deactivated", "error": "account_deactivated"}
    )
    return ids


def ids_of(result) -> list[int]:
    items = result["items"] if isinstance(result, dict) else result
    return sorted(item["id"] for item in items)


def test_at_status_filter_covers_flag_expiry_and_jwt_fallback(mixed_accounts):
    ids = mixed_accounts

    expired = db.list_accounts_page(limit=50, at_filter="expired")
    assert ids_of(expired) == sorted([ids["flag_expired"], ids["stale_valid"], ids["jwt_expired"]])
    # total 与 LIMIT/OFFSET 使用同一条件，分页统计不会漏算。
    assert expired["total"] == 3

    assert ids_of(db.list_accounts_page(limit=50, at_filter="valid")) == [ids["jwt_valid"]]
    assert ids_of(db.list_accounts_page(limit=50, at_filter="unknown")) == [ids["unknown"]]

    assert ids_of(db.list_accounts(limit=50, at_filter="expired")) == sorted(
        [ids["flag_expired"], ids["stale_valid"], ids["jwt_expired"]]
    )
    # 不传或传空表示全部账号，兼容旧页面和外部调用。
    assert len(db.list_accounts_page(limit=50, at_filter="")["items"]) == 5
    # 无法识别的取值命中空集，而不是静默放宽成全部账号。
    assert db.list_accounts_page(limit=50, at_filter="expird")["total"] == 0


def test_at_expired_flag_is_refreshed_after_live_check_gets_new_token():
    """查活成功换发的新 AT 会刷新过期标记，之后不再被筛成过期账号。"""
    account_id = db.insert_account(
        email="refresh@example.test", access_token=jwt_with_exp(time.time() - 300)
    )
    assert db.list_accounts_page(limit=10, at_filter="expired")["total"] == 1

    new_token = jwt_with_exp(time.time() + 3600)
    assert db.update_account_liveness(
        account_id, {"ok": True, "status": "live", "access_token": new_token}
    )
    row = db.get_account(account_id)
    assert row["token_expired"] is False
    assert row["at_expired"] is False
    assert db.list_accounts_page(limit=10, at_filter="expired")["total"] == 0
    assert [item["id"] for item in db.list_accounts_page(limit=10, at_filter="valid")["items"]] == [account_id]


def test_live_status_filter_separates_failed_from_deactivated(mixed_accounts):
    ids = mixed_accounts

    failed = db.list_accounts_page(limit=50, live_filter="failed")
    assert ids_of(failed) == sorted([ids["flag_expired"], ids["jwt_expired"]])
    assert failed["total"] == 2

    assert ids_of(db.list_accounts_page(limit=50, live_filter="success")) == [ids["jwt_valid"]]
    assert ids_of(db.list_accounts_page(limit=50, live_filter="deactivated")) == [ids["stale_valid"]]
    # 未查活 = live_check_status 缺失或为空。
    assert ids_of(db.list_accounts_page(limit=50, live_filter="never")) == [ids["unknown"]]
    assert db.list_accounts_page(limit=50, live_filter="bogus")["total"] == 0
    assert len(db.list_accounts_page(limit=50, live_filter="")["items"]) == 5

    # 两个条件可叠加：查活失败且 AT 已过期。
    combined = db.list_accounts_page(limit=50, live_filter="failed", at_filter="expired")
    assert ids_of(combined) == sorted([ids["flag_expired"], ids["jwt_expired"]])


def test_filters_apply_to_lookup_ids_and_plan_check_snapshot(mixed_accounts):
    ids = mixed_accounts

    lookup = db.find_accounts_by_emails(
        ["flag-expired@example.test", "jwt-valid@example.test"], at_filter="expired"
    )
    assert [row["email"] for row in lookup] == ["flag-expired@example.test"]

    lookup_live = db.find_accounts_by_emails(
        ["flag-expired@example.test", "jwt-valid@example.test"], live_filter="success"
    )
    assert [row["email"] for row in lookup_live] == ["jwt-valid@example.test"]

    ids_page = db.list_account_ids_page(limit=50, archived="all", at_filter="expired")
    assert sorted(ids_page["ids"]) == sorted([ids["flag_expired"], ids["stale_valid"], ids["jwt_expired"]])
    assert ids_page["total"] == 3

    snapshot = db.list_account_plan_check_statuses(limit=50, live_filter="failed")
    assert ids_of(snapshot) == sorted([ids["flag_expired"], ids["jwt_expired"]])
    assert snapshot["total"] == 2
    at_snapshot = db.list_account_plan_check_statuses(limit=50, at_filter="expired")
    assert all(item["at_expired"] is True for item in at_snapshot["items"])


@pytest.fixture
def client():
    app = create_app(auth_code=AUTH["X-Auth-Code"])
    app.config["TESTING"] = True
    return app.test_client()


def test_account_api_exposes_at_and_live_filters(mixed_accounts, client):
    ids = mixed_accounts

    expired = client.get(
        "/api/accounts?paged=1&page_size=50&archived=all&at_status=expired", headers=AUTH
    ).get_json()
    assert [row["id"] for row in expired["items"]] == sorted(
        [ids["flag_expired"], ids["stale_valid"], ids["jwt_expired"]], reverse=True
    )
    assert expired["total"] == 3
    assert all(row["at_expired"] is True for row in expired["items"])

    failed = client.get(
        "/api/accounts?paged=1&page_size=50&archived=all&live_status=failed", headers=AUTH
    ).get_json()
    assert failed["total"] == 2
    assert {row["id"] for row in failed["items"]} == {ids["flag_expired"], ids["jwt_expired"]}

    combined = client.get(
        "/api/accounts?paged=1&page_size=50&archived=all&at_status=expired&live_status=failed",
        headers=AUTH,
    ).get_json()
    assert combined["total"] == 2

    legacy = client.get("/api/accounts?archived=all&at_status=expired", headers=AUTH).get_json()
    assert len(legacy) == 3

    ids_only = client.get("/api/accounts/ids?archived=all&live_status=failed", headers=AUTH).get_json()
    assert sorted(ids_only["ids"]) == sorted([ids["flag_expired"], ids["jwt_expired"]])

    lookup = client.post(
        "/api/accounts/lookup",
        json={
            "emails": ["jwt-expired@example.test", "jwt-valid@example.test"],
            "archived": "all",
            "at_status": "expired",
        },
        headers=AUTH,
    ).get_json()
    assert [row["id"] for row in lookup["matches"]] == [ids["jwt_expired"]]
    assert lookup["not_found"] == ["jwt-valid@example.test"]

    snapshot = client.get(
        "/api/accounts/plan-check-status?page=1&page_size=50&live_status=failed", headers=AUTH
    ).get_json()
    assert snapshot["total"] == 2
    assert {item["id"] for item in snapshot["items"]} == {ids["flag_expired"], ids["jwt_expired"]}


def test_account_api_never_returns_tokens_for_new_filters(mixed_accounts, client):
    response = client.get(
        "/api/accounts?paged=1&page_size=50&archived=all&at_status=expired&live_status=failed",
        headers=AUTH,
    )
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "signature" not in body
    assert "not-a-jwt" not in body
