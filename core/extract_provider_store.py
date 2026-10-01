# -*- coding: utf-8 -*-
"""Local extract-provider configuration; raw CDKs are server-only.

All access uses the shared database helpers so database isolation and the account
lock also apply here.  Seeding is explicit: callers invoke ``ensure_defaults``
when listing configuration, without making any upstream requests.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime
from urllib.parse import urlsplit

from core import db

EXTRACT_LINK_TYPES = frozenset({"pix", "upi", "kakao_pay", "ideal"})
LUMEN_LINK_TYPES = frozenset({
    "ideal", "upi", "pix", "paypal", "kakao_pay", "momo", "blik", "twint", "gcash", "gopay",
})
ACTIVE_LUMEN_STATUSES = ("queued", "running", "awaiting_blik", "unknown", "interrupted")
_DEFAULTS_MARKER = "extract_provider_defaults_v1"
_PROVIDER_FIELDS = {"name", "provider_type", "api_base", "default_link_type", "enabled", "is_default", "note"}
_CDK_FIELDS = {"cdk", "memo", "enabled", "provider_id"}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _id(value, label="id") -> int:
    if type(value) is not int or value <= 0 or value > 9223372036854775807:
        raise ValueError(f"{label} 必须是正整数")
    return value


def _object(data, allowed: set[str]) -> dict:
    if not isinstance(data, dict):
        raise ValueError("请求数据必须是对象")
    if set(data) - allowed:
        raise ValueError("请求包含不支持的字段")
    return data


def _text(value, label: str, limit: int, *, optional=False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} 必须是字符串")
    if len(value) > limit:
        raise ValueError(f"{label} 超过长度限制")
    value = value.strip()
    if not optional and not value:
        raise ValueError(f"{label} 不能为空")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{label} 包含无效字符")
    return value


def _bool(value, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} 必须是布尔值")
    return value


def _api_base(value) -> str:
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


@contextmanager
def _connection(*, write=False):
    db._ensure_sqlite()
    with db._LOCK, closing(db._sqlite_conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield conn


def _provider(row) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    item["enabled"] = bool(item["enabled"])
    item["is_default"] = bool(item["is_default"])
    return item


def _masked_cdk(row) -> dict:
    code = row["cdk"]
    return {
        "id": row["id"], "provider_id": row["provider_id"],
        "memo": row["memo"], "enabled": bool(row["enabled"]),
        "display_suffix": code[-4:] if len(code) > 4 else "",
    }


def _require_provider(conn, provider_id: int) -> dict:
    item = _provider(conn.execute("SELECT * FROM extract_providers WHERE id=?", (provider_id,)).fetchone())
    if item is None:
        raise LookupError("供应商不存在")
    return item


def _refuse_active_lumen(conn, provider: dict) -> None:
    statuses = ",".join("?" for _ in ACTIVE_LUMEN_STATUSES)
    active = conn.execute(f"""
        SELECT 1 FROM accounts
        WHERE CAST(json_extract(payload, '$.extract_link_provider_id') AS INTEGER)=?
          AND lower(json_extract(payload, '$.extract_link_status')) IN ({statuses})
          AND (?='lumen' OR lower(json_extract(payload, '$.extract_link_provider_type'))='lumen')
        LIMIT 1
    """, (provider["id"], *ACTIVE_LUMEN_STATUSES, provider["provider_type"])).fetchone()
    if active is not None:
        raise RuntimeError("供应商存在进行中或可恢复的 Lumen 任务")


def ensure_defaults() -> None:
    """Seed Lumen once, preserving existing defaults and intentional deletion."""
    with _connection(write=True) as conn:
        if conn.execute("SELECT 1 FROM storage_meta WHERE key=?", (_DEFAULTS_MARKER,)).fetchone():
            return
        now = _now()
        has_default = bool(conn.execute("SELECT 1 FROM extract_providers WHERE is_default=1 LIMIT 1").fetchone())
        conn.execute("""
            INSERT OR IGNORE INTO extract_providers
                (name, provider_type, api_base, default_link_type, enabled, is_default, note, created_at, updated_at)
            VALUES ('Lumen Flow', 'lumen', 'https://api.int31.space', 'ideal', 1, ?, '', ?, ?)
        """, (int(not has_default), now, now))
        conn.execute("INSERT INTO storage_meta(key, value) VALUES(?, ?)", (_DEFAULTS_MARKER, now))


def list_providers() -> list[dict]:
    """Return persisted providers with masked CDKs; legacy id 0 is service-owned."""
    with _connection() as conn:
        providers = [_provider(row) for row in conn.execute("SELECT * FROM extract_providers ORDER BY is_default DESC, id")]
        by_id = {item["id"]: item for item in providers}
        for item in providers:
            item["cdks"] = []
        for row in conn.execute("SELECT * FROM extract_provider_cdks ORDER BY id"):
            if row["provider_id"] in by_id:
                by_id[row["provider_id"]]["cdks"].append(_masked_cdk(row))
        return providers


def get_provider(provider_id: int) -> dict | None:
    provider_id = _id(provider_id, "provider_id")
    with _connection() as conn:
        return _provider(conn.execute("SELECT * FROM extract_providers WHERE id=?", (provider_id,)).fetchone())


def save_provider(data: dict, provider_id: int | None = None) -> dict:
    data = _object(data, _PROVIDER_FIELDS)
    if provider_id is not None:
        provider_id = _id(provider_id, "provider_id")
    try:
        with _connection(write=True) as conn:
            previous = _require_provider(conn, provider_id) if provider_id is not None else None
            values = dict(previous or {"provider_type": "extract", "enabled": True, "is_default": False, "note": ""})
            values.update(data)
            values["name"] = _text(values.get("name"), "name", 120)
            values["provider_type"] = _text(values["provider_type"], "provider_type", 16).lower()
            if values["provider_type"] not in {"extract", "lumen"}:
                raise ValueError("provider_type 无效")
            values["api_base"] = _api_base(values.get("api_base"))
            methods = LUMEN_LINK_TYPES if values["provider_type"] == "lumen" else EXTRACT_LINK_TYPES
            default_method = "ideal" if values["provider_type"] == "lumen" else "pix"
            values["default_link_type"] = _text(values.get("default_link_type", default_method), "default_link_type", 24).lower()
            if values["default_link_type"] not in methods:
                raise ValueError("default_link_type 无效")
            values["enabled"] = _bool(values["enabled"], "enabled")
            values["is_default"] = _bool(values["is_default"], "is_default")
            values["note"] = _text(values["note"], "note", 2000, optional=True)
            if previous and any(values[key] != previous[key] for key in ("api_base", "provider_type")):
                _refuse_active_lumen(conn, previous)
            if previous and previous["provider_type"] != "lumen" and values["provider_type"] == "lumen":
                codes = [row[0].casefold() for row in conn.execute("SELECT cdk FROM extract_provider_cdks WHERE provider_id=?", (provider_id,))]
                if len(codes) != len(set(codes)):
                    raise ValueError("供应商存在大小写重复的 CDK，无法切换为 Lumen")
            now = _now()
            if data.get("is_default") is True:
                conn.execute("UPDATE extract_providers SET is_default=0, updated_at=? WHERE is_default=1", (now,))
            columns = ("name", "provider_type", "api_base", "default_link_type", "enabled", "is_default", "note")
            params = tuple(values[key] for key in columns)
            if previous is None:
                cursor = conn.execute("""
                    INSERT INTO extract_providers
                    (name, provider_type, api_base, default_link_type, enabled, is_default, note, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (*params, now, now))
                provider_id = cursor.lastrowid
            else:
                conn.execute("""
                    UPDATE extract_providers SET name=?, provider_type=?, api_base=?, default_link_type=?,
                    enabled=?, is_default=?, note=?, updated_at=? WHERE id=?
                """, (*params, now, provider_id))
            return _require_provider(conn, provider_id)
    except sqlite3.IntegrityError:
        raise ValueError("供应商名称已存在") from None


def delete_provider(provider_id: int) -> bool:
    provider_id = _id(provider_id, "provider_id")
    with _connection(write=True) as conn:
        provider = _require_provider(conn, provider_id)
        _refuse_active_lumen(conn, provider)
        conn.execute("DELETE FROM extract_provider_cdks WHERE provider_id=?", (provider_id,))
        conn.execute("DELETE FROM extract_providers WHERE id=?", (provider_id,))
        return True


def list_cdks(provider_id: int) -> list[dict]:
    provider_id = _id(provider_id, "provider_id")
    with _connection() as conn:
        _require_provider(conn, provider_id)
        return [_masked_cdk(row) for row in conn.execute("SELECT * FROM extract_provider_cdks WHERE provider_id=? ORDER BY id", (provider_id,))]


def get_cdk(cdk_id: int) -> dict | None:
    """Return a raw credential for server use only. Never serialize this result."""
    cdk_id = _id(cdk_id, "cdk_id")
    with _connection() as conn:
        row = conn.execute("SELECT * FROM extract_provider_cdks WHERE id=?", (cdk_id,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        return item


def save_cdk(provider_id: int, data: dict, cdk_id: int | None = None) -> dict:
    provider_id = _id(provider_id, "provider_id")
    data = _object(data, _CDK_FIELDS)
    if "provider_id" in data and _id(data["provider_id"], "provider_id") != provider_id:
        raise ValueError("CDK 与供应商不匹配")
    if cdk_id is not None:
        cdk_id = _id(cdk_id, "cdk_id")
    try:
        with _connection(write=True) as conn:
            provider = _require_provider(conn, provider_id)
            previous = None
            if cdk_id is not None:
                previous = conn.execute("SELECT * FROM extract_provider_cdks WHERE id=? AND provider_id=?", (cdk_id, provider_id)).fetchone()
                if previous is None:
                    raise LookupError("CDK 不存在或与供应商不匹配")
            values = dict(previous) if previous is not None else {"memo": "", "enabled": True}
            values.update(data)
            code = _text(values.get("cdk"), "cdk", 512)
            if any(char.isspace() for char in code):
                raise ValueError("cdk 不能包含空白字符")
            memo = _text(values["memo"], "memo", 1000, optional=True)
            enabled = _bool(values["enabled"], "enabled")
            for row in conn.execute("SELECT id, cdk FROM extract_provider_cdks WHERE provider_id=?", (provider_id,)):
                same = row["cdk"].casefold() == code.casefold() if provider["provider_type"] == "lumen" else row["cdk"] == code
                if row["id"] != cdk_id and same:
                    raise ValueError("CDK 已存在")
            now = _now()
            if previous is None:
                cursor = conn.execute("""
                    INSERT INTO extract_provider_cdks (provider_id, cdk, memo, enabled, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (provider_id, code, memo, enabled, now, now))
                cdk_id = cursor.lastrowid
            else:
                conn.execute("UPDATE extract_provider_cdks SET cdk=?, memo=?, enabled=?, updated_at=? WHERE id=?", (code, memo, enabled, now, cdk_id))
            return _masked_cdk(conn.execute("SELECT * FROM extract_provider_cdks WHERE id=?", (cdk_id,)).fetchone())
    except sqlite3.IntegrityError:
        raise ValueError("CDK 已存在") from None


def delete_cdk(cdk_id: int) -> bool:
    cdk_id = _id(cdk_id, "cdk_id")
    with _connection(write=True) as conn:
        if not conn.execute("DELETE FROM extract_provider_cdks WHERE id=?", (cdk_id,)).rowcount:
            raise LookupError("CDK 不存在")
        return True
