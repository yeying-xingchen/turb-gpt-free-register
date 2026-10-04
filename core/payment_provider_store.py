# -*- coding: utf-8 -*-
"""已保存的支付平台与支付 CDK；原始凭据仅服务端可读。

与提链 CDK 管理保持一致：列表只返回掩码尾号，``get_cdk`` /
``resolve_credential`` 才返回明文，且只允许服务端调用。所有访问都走共享数据库
助手，因此数据库隔离与账号锁同样适用。字段校验复用提链供应商存储的
``_text`` / ``_bool`` / ``_id`` / ``_object``，避免两套规则走偏。
"""
from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime
from urllib.parse import urlsplit

from config import scan_api as cfg
from core import db
from core.extract_provider_store import _bool, _id, _object, _text

# 现有三个平台保持既有 provider 取值，新增发布者 API。
PROVIDER_TYPES = frozenset({"v1", "masi", "orderhub", "seashore"})
PROVIDER_LABELS = {
    "v1": "Astra Scan Workbench",
    "masi": "Masi",
    "orderhub": "UPI OrderHub",
    "seashore": "seashore 发布者 API",
}
DEFAULT_BASES = {
    "v1": cfg.SCAN_API_BASE,
    "masi": cfg.MASI_API_BASE,
    "orderhub": cfg.ORDERHUB_API_BASE,
    "seashore": cfg.SEASHORE_API_BASE,
}
# 仅这些平台允许 OrderHub 式登录会话，其余必须使用凭据。
SESSION_PROVIDERS = frozenset({"orderhub"})

_DEFAULTS_MARKER = "payment_provider_defaults_v1"
_PROVIDER_FIELDS = {"name", "provider_type", "api_base", "enabled", "is_default", "note"}
_CDK_FIELDS = {"cdk", "memo", "enabled", "provider_id"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def _connection(*, write=False):
    db._ensure_sqlite()
    with db._LOCK, closing(db._sqlite_conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield conn


def _api_base(value, provider_type: str) -> str:
    """空值回退到配置默认地址；填写时必须是无凭据的 HTTP(S) 地址。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return ""
    base = _text(value, "api_base", 2048)
    try:
        parsed = urlsplit(base)
        port = parsed.port
        if (parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or "?" in base or "#" in base or "\\" in base
                or any(char.isspace() for char in base)
                or (port is not None and port <= 0)):
            raise ValueError
    except ValueError:
        raise ValueError("api_base 必须是无凭据、查询参数或片段的 HTTP(S) 地址") from None
    return base.rstrip("/")


def _masked(code: str) -> str:
    if len(code) <= 4:
        return "•" * len(code)
    head = code[:4] if len(code) >= 12 else code[:2]
    return f"{head}…{code[-4:]}"


def _provider(row) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    item["enabled"] = bool(item["enabled"])
    item["is_default"] = bool(item["is_default"])
    item["label"] = PROVIDER_LABELS.get(item["provider_type"], item["provider_type"])
    item["effective_api_base"] = item["api_base"] or DEFAULT_BASES.get(item["provider_type"], "")
    item["supports_session"] = item["provider_type"] in SESSION_PROVIDERS
    return item


def _masked_cdk(row) -> dict:
    code = row["cdk"]
    return {
        "id": row["id"], "provider_id": row["provider_id"],
        "memo": row["memo"], "enabled": bool(row["enabled"]),
        "display_suffix": code[-4:] if len(code) > 4 else "",
        "masked": _masked(code),
    }


def _require_provider(conn, provider_id: int) -> dict:
    item = _provider(conn.execute("SELECT * FROM payment_providers WHERE id=?", (provider_id,)).fetchone())
    if item is None:
        raise LookupError("支付平台不存在")
    return item


def ensure_defaults() -> None:
    """只播种一次四个平台，保留既有默认与有意的删除。"""
    with _connection(write=True) as conn:
        if conn.execute("SELECT 1 FROM storage_meta WHERE key=?", (_DEFAULTS_MARKER,)).fetchone():
            return
        now = _now()
        has_default = bool(conn.execute("SELECT 1 FROM payment_providers WHERE is_default=1 LIMIT 1").fetchone())
        for provider_type in ("v1", "masi", "orderhub", "seashore"):
            conn.execute("""
                INSERT OR IGNORE INTO payment_providers
                    (name, provider_type, api_base, enabled, is_default, note, created_at, updated_at)
                VALUES (?, ?, '', 1, ?, '', ?, ?)
            """, (PROVIDER_LABELS[provider_type], provider_type,
                  int(provider_type == "v1" and not has_default), now, now))
        conn.execute("INSERT INTO storage_meta(key, value) VALUES(?, ?)", (_DEFAULTS_MARKER, now))


def list_providers() -> list[dict]:
    with _connection() as conn:
        providers = [_provider(row) for row in
                     conn.execute("SELECT * FROM payment_providers ORDER BY is_default DESC, id")]
        by_id = {item["id"]: item for item in providers}
        for item in providers:
            item["cdks"] = []
        for row in conn.execute("SELECT * FROM payment_cdks ORDER BY id"):
            if row["provider_id"] in by_id:
                by_id[row["provider_id"]]["cdks"].append(_masked_cdk(row))
        return providers


def get_provider(provider_id: int) -> dict | None:
    provider_id = _id(provider_id, "provider_id")
    with _connection() as conn:
        return _provider(conn.execute("SELECT * FROM payment_providers WHERE id=?", (provider_id,)).fetchone())


def get_provider_by_type(provider_type: str) -> dict | None:
    provider_type = _text(provider_type, "provider_type", 16).lower()
    if provider_type not in PROVIDER_TYPES:
        raise ValueError("支付平台无效")
    with _connection() as conn:
        return _provider(conn.execute(
            "SELECT * FROM payment_providers WHERE provider_type=? ORDER BY is_default DESC, id LIMIT 1",
            (provider_type,)).fetchone())


def save_provider(data: dict, provider_id: int | None = None) -> dict:
    data = _object(data, _PROVIDER_FIELDS)
    if provider_id is not None:
        provider_id = _id(provider_id, "provider_id")
    try:
        with _connection(write=True) as conn:
            previous = _require_provider(conn, provider_id) if provider_id is not None else None
            values = dict(previous or {"provider_type": "v1", "enabled": True, "is_default": False, "note": ""})
            values.update(data)
            values["name"] = _text(values.get("name"), "name", 120)
            values["provider_type"] = _text(values["provider_type"], "provider_type", 16).lower()
            if values["provider_type"] not in PROVIDER_TYPES:
                raise ValueError("支付平台类型无效")
            values["api_base"] = _api_base(values.get("api_base"), values["provider_type"])
            values["enabled"] = _bool(values["enabled"], "enabled")
            values["is_default"] = _bool(values["is_default"], "is_default")
            values["note"] = _text(values["note"], "note", 2000, optional=True)
            if previous and previous["provider_type"] != values["provider_type"]:
                used = conn.execute("SELECT 1 FROM payment_cdks WHERE provider_id=? LIMIT 1", (provider_id,)).fetchone()
                if used:
                    raise ValueError("该平台已有保存的 CDK，不能改成其它平台类型")
            now = _now()
            if values["is_default"] is True:
                conn.execute("UPDATE payment_providers SET is_default=0, updated_at=? WHERE is_default=1", (now,))
            columns = ("name", "provider_type", "api_base", "enabled", "is_default", "note")
            params = tuple(values[key] for key in columns)
            if previous is None:
                cursor = conn.execute("""
                    INSERT INTO payment_providers
                    (name, provider_type, api_base, enabled, is_default, note, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (*params, now, now))
                provider_id = cursor.lastrowid
            else:
                conn.execute("""
                    UPDATE payment_providers SET name=?, provider_type=?, api_base=?, enabled=?,
                    is_default=?, note=?, updated_at=? WHERE id=?
                """, (*params, now, provider_id))
            return _require_provider(conn, provider_id)
    except sqlite3.IntegrityError:
        raise ValueError("支付平台名称已存在") from None


def delete_provider(provider_id: int) -> bool:
    provider_id = _id(provider_id, "provider_id")
    with _connection(write=True) as conn:
        _require_provider(conn, provider_id)
        conn.execute("DELETE FROM payment_cdks WHERE provider_id=?", (provider_id,))
        conn.execute("DELETE FROM payment_providers WHERE id=?", (provider_id,))
        return True


def list_cdks(provider_id: int) -> list[dict]:
    provider_id = _id(provider_id, "provider_id")
    with _connection() as conn:
        _require_provider(conn, provider_id)
        return [_masked_cdk(row) for row in
                conn.execute("SELECT * FROM payment_cdks WHERE provider_id=? ORDER BY id", (provider_id,))]


def get_cdk(cdk_id: int) -> dict | None:
    """仅供服务端读取的明文凭据，禁止直接序列化返回。"""
    cdk_id = _id(cdk_id, "cdk_id")
    with _connection() as conn:
        row = conn.execute("SELECT * FROM payment_cdks WHERE id=?", (cdk_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        return item


def save_cdk(provider_id: int, data: dict, cdk_id: int | None = None) -> dict:
    provider_id = _id(provider_id, "provider_id")
    data = _object(data, _CDK_FIELDS)
    if "provider_id" in data and _id(data["provider_id"], "provider_id") != provider_id:
        raise ValueError("CDK 与支付平台不匹配")
    if cdk_id is not None:
        cdk_id = _id(cdk_id, "cdk_id")
    try:
        with _connection(write=True) as conn:
            _require_provider(conn, provider_id)
            previous = None
            if cdk_id is not None:
                previous = conn.execute("SELECT * FROM payment_cdks WHERE id=? AND provider_id=?",
                                        (cdk_id, provider_id)).fetchone()
                if previous is None:
                    raise LookupError("CDK 不存在或与支付平台不匹配")
            values = dict(previous) if previous is not None else {"memo": "", "enabled": True}
            values.update(data)
            code = _text(values.get("cdk"), "cdk", 512)
            if any(char.isspace() for char in code):
                raise ValueError("cdk 不能包含空白字符")
            memo = _text(values["memo"], "memo", 1000, optional=True)
            enabled = _bool(values["enabled"], "enabled")
            for row in conn.execute("SELECT id, cdk FROM payment_cdks WHERE provider_id=?", (provider_id,)):
                if row["id"] != cdk_id and row["cdk"] == code:
                    raise ValueError("该 CDK 已保存在此平台下")
            now = _now()
            if previous is None:
                cursor = conn.execute("""
                    INSERT INTO payment_cdks (provider_id, cdk, memo, enabled, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (provider_id, code, memo, enabled, now, now))
                cdk_id = cursor.lastrowid
            else:
                conn.execute("UPDATE payment_cdks SET cdk=?, memo=?, enabled=?, updated_at=? WHERE id=?",
                             (code, memo, enabled, now, cdk_id))
            return _masked_cdk(conn.execute("SELECT * FROM payment_cdks WHERE id=?", (cdk_id,)).fetchone())
    except sqlite3.IntegrityError:
        raise ValueError("该 CDK 已保存在此平台下") from None


def delete_cdk(cdk_id: int) -> bool:
    cdk_id = _id(cdk_id, "cdk_id")
    with _connection(write=True) as conn:
        if not conn.execute("DELETE FROM payment_cdks WHERE id=?", (cdk_id,)).rowcount:
            raise LookupError("CDK 不存在")
        return True


def resolve_saved(cdk_id) -> dict:
    """按 cdk_id 取出明文凭据（服务端），不校验具体平台。"""
    cdk_id = _id(cdk_id, "cdk_id")
    row = get_cdk(cdk_id)
    if row is None:
        raise LookupError("已保存的支付 CDK 不存在")
    saved = get_provider(row["provider_id"])
    if saved is None:
        raise LookupError("支付平台不存在")
    if not row["enabled"] or not saved["enabled"]:
        raise ValueError("该支付 CDK 或所属平台已停用")
    return {"cdk": row["cdk"], "cdk_id": cdk_id, "provider_id": row["provider_id"],
            "provider_type": saved["provider_type"]}


def resolve_credential(*, provider: str, cdk=None, cdk_id=None) -> dict:
    """把 ``cdk`` 或已保存的 ``cdk_id`` 解析成服务端可用的明文凭据。

    只接受显式选择：不传 cdk_id 也不会自动挑一条已保存的 CDK，避免静默用错凭据。
    """
    provider = _text(provider, "provider", 16).lower()
    if provider not in PROVIDER_TYPES:
        raise ValueError("支付平台无效")
    if cdk is not None and cdk_id is not None:
        raise ValueError("支付 CDK 与已保存的 cdk_id 只能提供一个")
    if cdk_id is not None:
        resolved = resolve_saved(cdk_id)
        if resolved["provider_type"] != provider:
            raise ValueError("已保存的 CDK 不属于所选支付平台")
        return resolved
    if cdk is None:
        raise ValueError("请选择已保存的支付 CDK，或输入本次临时凭据")
    return {"cdk": _text(cdk, "cdk", 512), "cdk_id": None, "provider_id": None, "provider_type": provider}


def credential_summary(provider: str) -> dict:
    """给前端用的已保存凭据概览（永远不含明文）。"""
    item = get_provider_by_type(provider)
    if item is None:
        return {"provider": provider, "provider_id": None, "cdks": [], "enabled": False}
    return {"provider": provider, "provider_id": item["id"], "name": item["name"],
            "enabled": item["enabled"], "api_base": item["effective_api_base"],
            "supports_session": item["supports_session"],
            "cdks": list_cdks(item["id"])}
