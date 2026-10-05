"""账号列表标记「是否已被兑换」，并支持按兑换状态筛选。"""
import pytest

from core import db
from webui.app import create_app

AUTH = {"X-Auth-Code": "redeem-status-auth"}


@pytest.fixture
def redeemed_accounts():
    """两个可兑换账号，其中 id 较小的一个已被 CDK 领取。"""
    claimed_id = db.insert_account(
        email="claimed@example.test",
        access_token="at-claimed",
        plan_type="plus",
        extra={"registration_password": "claimed-password"},
    )
    available_id = db.insert_account(
        email="available@example.test",
        access_token="at-available",
        plan_type="plus",
        extra={"registration_password": "available-password"},
    )
    code = db.create_redeem_code(quantity=1, account_group="默认分组")
    result = db.redeem_plus_accounts(code["code"])
    assert [item["account_id"] for item in result["accounts"]] == [claimed_id]
    return claimed_id, available_id, code["code"]


@pytest.fixture
def client():
    app = create_app(auth_code=AUTH["X-Auth-Code"])
    app.config["TESTING"] = True
    return app.test_client()


def test_account_page_marks_redeemed_accounts(redeemed_accounts):
    claimed_id, available_id, _ = redeemed_accounts
    items = {item["id"]: item for item in db.list_accounts_page(limit=10)["items"]}
    assert items[claimed_id]["redeemed"] is True
    assert items[claimed_id]["redeemed_at"]
    assert items[available_id]["redeemed"] is False
    assert "redeemed_at" not in items[available_id]


def test_redemption_filter_applies_to_sql_paging_and_lookup(redeemed_accounts):
    claimed_id, available_id, _ = redeemed_accounts

    claimed_page = db.list_accounts_page(limit=10, redemption_filter="redeemed")
    assert [item["id"] for item in claimed_page["items"]] == [claimed_id]
    # total 与 LIMIT/OFFSET 使用同一条件，分页统计不会把未兑换账号算进来。
    assert claimed_page["total"] == 1

    available_page = db.list_accounts_page(limit=10, redemption_filter="unredeemed")
    assert [item["id"] for item in available_page["items"]] == [available_id]
    assert available_page["total"] == 1

    assert [item["id"] for item in db.list_accounts(limit=10, redemption_filter="redeemed")] == [claimed_id]
    assert [item["id"] for item in db.list_accounts_page(limit=10, redemption_filter="")["items"]] == [
        available_id, claimed_id,
    ]

    # 兼容旧筛选别名，便于外部调用方复用。
    assert [item["id"] for item in db.list_accounts_page(limit=10, redemption_filter="1")["items"]] == [claimed_id]
    assert [item["id"] for item in db.list_accounts_page(limit=10, redemption_filter="0")["items"]] == [available_id]

    lookup = db.find_accounts_by_emails(
        ["claimed@example.test", "available@example.test"],
        redemption_filter="unredeemed",
    )
    assert [row["email"] for row in lookup] == ["available@example.test"]
    assert lookup[0]["redeemed"] is False


def test_account_api_exposes_redemption_state_without_leaking_code(redeemed_accounts, client):
    claimed_id, available_id, code = redeemed_accounts

    response = client.get("/api/accounts?paged=1&page_size=50&archived=all", headers=AUTH)
    assert response.status_code == 200
    rows = {row["id"]: row for row in response.get_json()["items"]}
    assert rows[claimed_id]["redeemed"] is True
    assert rows[claimed_id]["redeemed_at"]
    assert rows[available_id]["redeemed"] is False

    # 兑换状态不需要暴露 CDK 或账号凭据。
    body = response.get_data(as_text=True)
    assert code not in body
    assert "claimed-password" not in body
    assert "at-claimed" not in body

    only_claimed = client.get(
        "/api/accounts?paged=1&page_size=50&archived=all&redemption=redeemed", headers=AUTH
    ).get_json()
    assert [row["id"] for row in only_claimed["items"]] == [claimed_id]
    assert only_claimed["total"] == 1

    only_available = client.get(
        "/api/accounts?paged=1&page_size=50&archived=all&redemption=unredeemed", headers=AUTH
    ).get_json()
    assert [row["id"] for row in only_available["items"]] == [available_id]

    legacy = client.get("/api/accounts?archived=all&redemption=redeemed", headers=AUTH).get_json()
    assert [row["id"] for row in legacy] == [claimed_id]

    lookup = client.post(
        "/api/accounts/lookup",
        json={
            "emails": ["claimed@example.test", "available@example.test"],
            "archived": "all",
            "redemption": "redeemed",
        },
        headers=AUTH,
    ).get_json()
    assert [row["id"] for row in lookup["matches"]] == [claimed_id]
    assert lookup["matches"][0]["redeemed"] is True
    # 与其他筛选一致：被当前兑换状态筛掉的邮箱按“未匹配”返回。
    assert lookup["not_found"] == ["available@example.test"]


def test_redemption_filter_applies_to_plan_check_status(redeemed_accounts, client):
    """账号列表默认隐藏已兑换账号，套餐状态轮询必须使用同一筛选。"""
    claimed_id, available_id, _ = redeemed_accounts

    snapshot = db.list_account_plan_check_statuses(limit=10, redemption_filter="unredeemed")
    assert [item["id"] for item in snapshot["items"]] == [available_id]
    assert snapshot["total"] == 1

    claimed_snapshot = db.list_account_plan_check_statuses(limit=10, redemption_filter="redeemed")
    assert [item["id"] for item in claimed_snapshot["items"]] == [claimed_id]

    response = client.get(
        "/api/accounts/plan-check-status?page=1&page_size=10&redemption=unredeemed", headers=AUTH
    )
    payload = response.get_json()
    assert [item["id"] for item in payload["items"]] == [available_id]
    assert payload["total"] == 1

    claimed_payload = client.get(
        "/api/accounts/plan-check-status?page=1&page_size=10&redemption=redeemed", headers=AUTH
    ).get_json()
    assert [item["id"] for item in claimed_payload["items"]] == [claimed_id]

    # 不传或传空表示全部账号，兼容旧页面和外部调用。
    all_payload = client.get(
        "/api/accounts/plan-check-status?page=1&page_size=10&redemption=", headers=AUTH
    ).get_json()
    assert all_payload["total"] == 2
