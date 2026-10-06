# -*- coding: utf-8 -*-
"""账号列表查询下推的等价性回归测试。

优化把套餐/Codex/查活/2FA/分组/AT 状态等筛选条件固化成了 SQLite 生成列 + 覆盖索引。
这里用未改动的 Python 判定（``_account_matches_plan_filter`` /
``_matches_*_filter`` / ``_account_at_state`` / ``_redeem_credentials`` /
``_account_group_name``）作为参考实现，逐组合比对 SQL 路径的结果，确保提速没有改变语义。
"""
from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from core import db


def _jwt(exp: float, seed: str = "user@example.test") -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp, "sub": seed}).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def _iso(offset_seconds: float) -> str:
    stamp = datetime.now(tz=timezone.utc) + timedelta(seconds=offset_seconds)
    return stamp.isoformat().replace("+00:00", "Z")


@pytest.fixture
def mixed_accounts():
    """覆盖套餐 / Codex / 查活 / 2FA / 分组 / 兑换 / AT 状态各分支的账号集合。"""
    now = time.time()
    specs = [
        # email, payload 附加字段, 是否有登录密码, archived, 是否已被领取
        ("plus-valid@example.test", {"current_plan_type": "plus", "codex_status": "success",
                                     "live_check_status": "success", "totp_secret": "JBSWY3DPEHPK3PXP",
                                     "group_name": "batch-a"}, "pw", False, False),
        ("free-trial@example.test", {"current_plan_type": "free", "plus_trial_eligible": True,
                                     "codex_status": "failed", "live_check_status": "failed",
                                     "group_name": "batch-a"}, "pw", False, True),
        ("free-promo@example.test", {"current_plan_type": "free",
                                     "eligible_promo_campaigns": {"chatgptplusplan": {
                                         "metadata": {"plan_name": "chatgptplusplan",
                                                      "discount": {"percentage": 50}}}},
                                     "live_check_status": "deactivated", "group_name": "batch-b"}, "", False, False),
        ("team-at-flag@example.test", {"plan_type": "team", "token_expired": True,
                                       "totp_setup_status": "queued", "archived": True}, "pw", True, False),
        ("empty-plan@example.test", {"codex_status": "pending", "live_check_status": "unknown"}, None, False, False),
        ("unicode-Üser@example.test", {"current_plan_type": "free_no_trial", "original_email": "Orig@Example.test",
                                       "note": "关键字 NEedle"}, "pw", False, False),
    ]
    created = {}
    for email, extra_fields, password, archived, _claimed in specs:
        token = _jwt(now - 60 if "at-flag" in email else now + 3600, email)
        payload_extra = {"registration_password": password} if password is not None else None
        acc_id = db.insert_account(
            email=email, access_token=token, extra=payload_extra,
            totp_secret=extra_fields.get("totp_secret"), plan_type=extra_fields.get("plan_type"),
        )
        created[email] = acc_id
        # insert_account 只接受有限字段，其余分支直接改 payload 后再写回。
        row = db.get_account(acc_id)
        row.update({k: v for k, v in extra_fields.items()})
        if extra_fields.get("token_expired"):
            row.pop("token_expires_at", None)
        db._save_accounts([r for r in db._load_accounts() if int(r.get("id")) != acc_id] + [row])
        if archived:
            db.archive_account(acc_id, True)

    claims = db.list_accounts_page(limit=100, archived="all")["items"]
    target = next(item for item in claims if item["email"] == "free-trial@example.test")
    with db._row_write_transaction() as conn:
        conn.execute(
            "INSERT INTO redeem_claims(code_id,account_id,email,claimed_at) VALUES(?,?,?,?)",
            (1, int(target["id"]), target["email"], db._now()),
        )
    return created


def _python_reference(**filters) -> set[int]:
    rows = db._filtered_decorated_accounts(
        archived=filters.get("archived", False),
        plan_filter=filters.get("plan_filter"),
        codex_filter=filters.get("codex_filter"),
        totp_filter=filters.get("totp_filter"),
        group_filter=filters.get("group_filter"),
        redemption_filter=filters.get("redemption_filter"),
        at_filter=filters.get("at_filter"),
        live_filter=filters.get("live_filter"),
    )
    return {int(row["id"]) for row in rows}


FILTER_COMBOS = [
    {},
    {"archived": "0"},
    {"archived": "1"},
    {"archived": "all"},
    {"plan_filter": "plus"},
    {"plan_filter": "free"},
    {"plan_filter": "free_no_trial"},
    {"plan_filter": "plus_trial"},
    {"plan_filter": "promo"},
    {"plan_filter": "team"},
    {"codex_filter": "success"},
    {"codex_filter": "failed"},
    {"codex_filter": "pending"},
    {"codex_filter": "deactivated"},
    {"live_filter": "failed"},
    {"live_filter": "success"},
    {"live_filter": "deactivated"},
    {"live_filter": "never"},
    {"totp_filter": "enabled"},
    {"totp_filter": "disabled"},
    {"totp_filter": "pending"},
    {"totp_filter": "failed"},
    {"at_filter": "expired"},
    {"at_filter": "valid"},
    {"at_filter": "unknown"},
    {"redemption_filter": "redeemed"},
    {"redemption_filter": "unredeemed"},
    {"group_filter": "batch-a"},
    {"group_filter": "batch-b"},
    {"plan_filter": "free", "at_filter": "valid", "live_filter": "success"},
    {"plan_filter": "plus", "codex_filter": "failed", "archived": "all"},
    {"redemption_filter": "unredeemed", "totp_filter": "disabled", "archived": "0"},
]


@pytest.mark.parametrize("filters", FILTER_COMBOS, ids=lambda f: ",".join(f"{k}={v}" for k, v in f.items()) or "none")
def test_sql_filters_match_python_reference(mixed_accounts, filters):
    sql_ids = set(db.list_account_ids_page(limit=500, **filters)["ids"])
    assert sql_ids == _python_reference(**filters)


def test_at_filter_uses_indexed_column_and_keeps_exact_fallback(mixed_accounts):
    """生成列可用时走索引路径；出现“无过期信息但有可解析 JWT”的行时回退精确表达式。"""
    conn = db._sqlite_conn()
    assert conn.execute(db._AT_STATE_PROBE_SQL).fetchone() is None
    assert db._at_state_filter_sql(conn, "expired") == db._AT_STATE_FAST_SQL["expired"]

    # 绕过 _refresh_token_expiry 直接写入一行历史数据：AT 可解析但没有过期字段。
    legacy_jwt = _jwt(time.time() - 120, "legacy@example.test")
    payload = {"id": 999, "email": "legacy@example.test", "access_token": legacy_jwt,
               "group_name": "batch-a"}
    conn.execute(
        "INSERT INTO accounts(id,email,status,archived,created_at,updated_at,payload) VALUES(?,?,?,?,?,?,?)",
        (999, "legacy@example.test", "registered", 0, "", "", json.dumps(payload, ensure_ascii=False)),
    )
    conn.commit()
    assert conn.execute(db._AT_STATE_PROBE_SQL).fetchone() is not None
    assert db._at_state_filter_sql(conn, "expired") == f"({db._AT_STATE_SQL}) = 1"
    expired = set(db.list_account_ids_page(limit=500, archived="0", at_filter="expired")["ids"])
    assert 999 in expired
    assert expired == _python_reference(archived="0", at_filter="expired")


def test_group_and_redeem_summaries_match_python_reference(mixed_accounts):
    conn = db._sqlite_conn()
    rows = conn.execute("SELECT id, payload, archived FROM accounts").fetchall()
    claimed = {int(row["account_id"]) for row in conn.execute("SELECT account_id FROM redeem_claims")}
    counters: dict[str, dict] = {}
    for raw in rows:
        payload = json.loads(raw["payload"])
        payload.setdefault("id", int(raw["id"]))
        payload.setdefault("archived", bool(raw["archived"]))
        group = db._account_group_name(payload)
        entry = counters.setdefault(group, {"total": 0, "redeemable": 0})
        entry["total"] += 1
        if int(raw["id"]) not in claimed and db._redeem_credentials(payload):
            entry["redeemable"] += 1
    expected = {name: (entry["total"], entry["redeemable"]) for name, entry in counters.items()}
    got = {item["group_name"]: (item["total"], item["redeemable"]) for item in db.list_account_groups()}
    assert got == expected
    assert any(item["redeemable"] for item in db.list_account_groups())

    for group_name in (None, "batch-a", "batch-b", "默认分组"):
        wanted = db._account_group_name({"group_name": group_name}) if group_name else None
        available = known_plus = 0
        for raw in rows:
            payload = json.loads(raw["payload"])
            payload.setdefault("id", int(raw["id"]))
            payload.setdefault("archived", bool(raw["archived"]))
            if bool(payload.get("archived")):
                continue
            group_ok = not wanted or db._account_group_name(payload).casefold() == wanted.casefold()
            plan = db._redeem_plan(payload)
            if group_ok and "plus" in plan and "free" not in plan:
                known_plus += 1
            if int(raw["id"]) in claimed or not group_ok:
                continue
            if not db._redeem_credentials(payload):
                continue
            if wanted or ("plus" in plan and "free" not in plan):
                available += 1
        assert db.redeem_stock_summary(group_name=group_name) == {
            "available": available, "known_plus": known_plus,
        }, group_name


def test_email_lookup_paths_match_python_reference(mixed_accounts):
    emails = [row["email"] for row in db.list_accounts_page(limit=100, archived="all")["items"]]
    sql_rows = db.find_accounts_by_emails(emails)
    targets = {email.strip().casefold() for email in emails}
    expected = [
        row for row in db._filtered_decorated_accounts(archived=False)
        if str(row.get("email") or "").strip().casefold() in targets
        or str(row.get("original_email") or "").strip().casefold() in targets
    ]
    assert [row["id"] for row in sql_rows] == [row["id"] for row in expected]

    # original_email 命中也走索引列。
    original = db.find_accounts_by_emails(["orig@example.test"])
    assert [row["email"] for row in original] == ["unicode-Üser@example.test"]

    # 非 ASCII 邮箱回退到 Python lower()。
    account = db.get_account_by_email("UNICODE-ÜSER@EXAMPLE.TEST")
    assert account is not None and account["email"] == "unicode-Üser@example.test"
    # 不做 strip，保持旧行为。
    assert db.get_account_by_email(" plus-valid@example.test") is None


def test_schema_upgrade_is_idempotent_and_backfills_legacy_tokens(tmp_path, monkeypatch):
    """重复执行迁移不应报错；历史账号补写 token_expires_at 后 AT 判定不变。"""
    db._ensure_sqlite()
    conn = db._sqlite_conn()
    legacy_jwt = _jwt(time.time() + 3600, "legacy@example.test")
    conn.execute(
        "INSERT INTO accounts(id,email,status,archived,created_at,updated_at,payload) VALUES(?,?,?,?,?,?,?)",
        (77, "legacy@example.test", "registered", 0, "", "",
         json.dumps({"id": 77, "email": "legacy@example.test", "access_token": legacy_jwt}, ensure_ascii=False)),
    )
    conn.commit()
    before = db.get_account(77)
    assert not before.get("token_expires_at")
    assert db._account_at_state(before) == "valid"

    db._ensure_account_filter_schema(conn)          # 第二次执行不应报错
    db._ensure_query_indexes(conn)
    conn.execute("DELETE FROM storage_meta WHERE key=?", (db._AT_BACKFILL_KEY,))
    assert db._backfill_account_at_expiry(conn) >= 1
    conn.commit()
    after = db.get_account(77)
    assert after["token_expires_at"]
    assert db._account_at_state(after) == db._account_at_state(before) == "valid"
    # 回填后探测不到需要 Python 解析 JWT 的行，AT 筛选可以稳定走索引。
    db._SQLITE_READY = False
    db._ensure_sqlite()
    conn = db._sqlite_conn()
    assert conn.execute(db._AT_STATE_PROBE_SQL).fetchone() is None
