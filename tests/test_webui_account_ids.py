"""账号「全选」用的轻量 ID 分页接口：筛选结果 / 全部账号两种范围。"""
import re
from pathlib import Path

import pytest

from core import db
from webui.app import create_app

AUTH = {"X-Auth-Code": "account-ids-auth"}


@pytest.fixture
def client():
    app = create_app(auth_code=AUTH["X-Auth-Code"])
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture
def account_pool():
    """三个正常账号 + 一个已归档账号，其中 id 最小的已被 CDK 领取。"""
    claimed_id = db.insert_account(
        email="claimed@example.test",
        access_token="at-claimed",
        plan_type="plus",
        extra={"registration_password": "claimed-password"},
    )
    normal_id = db.insert_account(
        email="normal@example.test", access_token="at-normal", plan_type="free"
    )
    other_id = db.insert_account(
        email="other@example.test", access_token="at-other", plan_type="free"
    )
    archived_id = db.insert_account(
        email="archived@example.test", access_token="at-archived", plan_type="plus"
    )
    code = db.create_redeem_code(quantity=1, account_group="默认分组")
    assert [item["account_id"] for item in db.redeem_plus_accounts(code["code"])["accounts"]] == [claimed_id]
    updated, _skipped = db.archive_accounts(account_ids=[archived_id], archived=True)
    assert [row["id"] for row in updated] == [archived_id]
    return {
        "claimed": claimed_id,
        "normal": normal_id,
        "other": other_id,
        "archived": archived_id,
        "all": sorted([claimed_id, normal_id, other_id, archived_id], reverse=True),
    }


def get_ids(client, **params):
    response = client.get("/api/accounts/ids", query_string=params, headers=AUTH)
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def test_filtered_scope_follows_the_list_filters(client, account_pool):
    """默认 scope=filtered 与账号列表一致：不传参数时含已兑换但不含已归档。"""
    default = get_ids(client)
    assert default["ok"] is True
    assert default["scope"] == "filtered"
    # 列表页默认 archived=0；兑换状态在接口层不传即不筛选。
    assert default["ids"] == [account_pool["other"], account_pool["normal"], account_pool["claimed"]]
    assert default["total"] == 3

    assert get_ids(client, redemption="unredeemed")["ids"] == [
        account_pool["other"],
        account_pool["normal"],
    ]
    assert get_ids(client, redemption="")["ids"] == [
        account_pool["other"],
        account_pool["normal"],
        account_pool["claimed"],
    ]
    assert get_ids(client, redemption="redeemed")["ids"] == [account_pool["claimed"]]
    assert get_ids(client, archived="1")["ids"] == [account_pool["archived"]]
    assert get_ids(client, archived="1", redemption="redeemed")["total"] == 0
    assert get_ids(client, archived="1", redemption="")["ids"] == [account_pool["archived"]]
    assert get_ids(client, q="normal@example")["ids"] == [account_pool["normal"]]


def test_all_scope_ignores_filters_and_includes_archived(client, account_pool):
    """scope=all 是全选所有账号：忽略关键词与筛选，含已兑换与已归档。"""
    body = get_ids(client, scope="all", q="normal@example", archived="0", redemption="unredeemed")
    assert body["ids"] == account_pool["all"]
    assert body["total"] == 4


def test_ids_paging_and_page_size_cap(client, account_pool):
    first = get_ids(client, scope="all", page="1", page_size="2")
    second = get_ids(client, scope="all", page="2", page_size="2")
    assert first["ids"] == account_pool["all"][:2]
    assert second["ids"] == account_pool["all"][2:]
    assert first["total"] == second["total"] == 4
    assert first["page_size"] == 2
    # 单页最多 5000 个 ID，超出部分由 page 继续取。
    assert get_ids(client, scope="all", page_size="99999")["page_size"] == 5000


def test_ids_scope_validation_and_auth(client, account_pool):
    bad = client.get("/api/accounts/ids", query_string={"scope": "everything"}, headers=AUTH)
    assert bad.status_code == 400
    assert bad.get_json()["ok"] is False
    assert client.get("/api/accounts/ids").status_code == 401


def test_secret_bulk_can_read_emails_for_large_exports(client, account_pool):
    """导出「邮箱」列不再依赖前端缓存的行，可直接按 ID 批量读取。"""
    response = client.post(
        "/api/accounts/secret-bulk",
        json={"account_ids": [account_pool["claimed"], account_pool["normal"]], "field": "email"},
        headers=AUTH,
    )
    assert response.status_code == 200
    body = response.get_json()
    assert [item["value"] for item in body["values"]] == ["claimed@example.test", "normal@example.test"]
    assert body["count"] == 2


def test_db_list_account_ids_page_orders_newest_first(account_pool):
    page = db.list_account_ids_page(limit=10, archived="all", redemption_filter="")
    assert page["ids"] == account_pool["all"]
    assert page["total"] == 4
    assert db.list_account_ids_page(limit=1, offset=1, archived="all", redemption_filter="")["ids"] == [
        account_pool["all"][1]
    ]


# 前端 frontend/app/utils/accounts.ts 的 accountBatchLimits 是分批提交的唯一来源，
# 这里直接解析它，确保每个批量接口都还能接受前端使用的批次大小。
ACCOUNTS_TS = Path(__file__).resolve().parents[1] / "frontend" / "app" / "utils" / "accounts.ts"
BULK_ROUTES = {
    "live": "/api/accounts/check-live-bulk",
    "plan": "/api/accounts/check-plan-bulk",
    "totp": "/api/accounts/totp-setup-bulk",
    "email": "/api/accounts/change-email-bulk",
    "agent": "/api/accounts/codex-agent-bulk",
    "upload": "/api/accounts/codex-agent/upload-sub2-bulk",
    "retry": "/api/codex/retry-bulk",
    "stop": "/api/codex/stop-bulk",
    "extract": "/api/accounts/extract-link-bulk",
    "note": "/api/accounts/note-bulk",
    "group": "/api/accounts/group-bulk",
    "archive": "/api/accounts/archive-bulk",
    "secret": "/api/accounts/secret-bulk",
    "delete": "/api/accounts/delete-bulk",
}


def frontend_batch_limits() -> dict[str, int]:
    text = ACCOUNTS_TS.read_text(encoding="utf-8")
    block = text.split("export const accountBatchLimits", 1)[1].split("};", 1)[0]
    return {
        key: int(value)
        for key, value in re.findall(r'"?([a-z\-]+)"?:\s*(\d+)', block)
    }


def test_frontend_batch_sizes_below_backend_caps(client):
    limits = frontend_batch_limits()
    assert set(BULK_ROUTES) <= set(limits)
    for action, route in BULK_ROUTES.items():
        size = limits[action]
        # 全部使用不存在的 ID：接口只会把它们记进 skipped，不会真的发起后台任务。
        ids = list(range(900000, 900000 + size))
        body = {"account_ids": ids}
        if action == "note":
            body["note"] = "分批上限校验"
        elif action == "group":
            body["group_name"] = "分批上限校验"
        elif action == "archive":
            body["archived"] = True
        elif action == "secret":
            body["field"] = "email"
        response = client.post(route, json=body, headers=AUTH)
        assert response.status_code != 400 or "最多" not in str(
            response.get_json().get("error", "")
        ), f"{action} 的后端上限低于前端批次大小 {size}: {response.get_json()}"


def test_activation_and_payment_id_limits_match_frontend():
    from core import scan_api_service

    assert frontend_batch_limits()["activate"] == 500
    assert frontend_batch_limits()["pay"] == 500
    # activate-plus 与 scan-requests 共用同一套 ID 校验。
    assert len(scan_api_service._validate_ids(list(range(1, 501)))) == 500
    with pytest.raises(ValueError):
        scan_api_service._validate_ids(list(range(1, 502)))
