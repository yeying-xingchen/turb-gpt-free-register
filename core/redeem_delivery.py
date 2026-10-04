"""Durable, short-lived redemption delivery tied to the original requester.

The browser creates a random recovery key before submitting a CDK. Only its hash
is stored. A retry with that key retrieves the original allocation, never a new
one. Download URLs may be retried during the fixed delivery window.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
import hashlib
import json
import re
import secrets
import time

DELIVERY_TTL_SECONDS = 600
_KEY = re.compile(r"[A-Za-z0-9_-]{32,128}\Z")


def validate_request_id(value):
    if value is None:
        return None
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        from core.db import RedeemError
        raise RedeemError("领取凭证格式无效，请刷新页面后重试", code="invalid_request_id")
    return value


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _download_id(code: str, request_id: str) -> str:
    return _digest("turb-redeem-download:" + code + ":" + request_id)


def ensure_schema(conn):
    # execute (not executescript) preserves the caller's allocation transaction.
    conn.execute("""CREATE TABLE IF NOT EXISTS redeem_deliveries (
        code_id INTEGER NOT NULL,
        request_hash TEXT NOT NULL,
        download_hash TEXT NOT NULL UNIQUE,
        expires_at REAL NOT NULL,
        filename TEXT NOT NULL,
        content BLOB NOT NULL,
        result_json TEXT NOT NULL,
        PRIMARY KEY (code_id, request_hash)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_redeem_delivery_expiry ON redeem_deliveries(expires_at)")


def resume(conn, *, code_id: int, code: str, request_id: str | None, quantity: int | None = None):
    if request_id is None:
        return None
    ensure_schema(conn)
    row = conn.execute("SELECT * FROM redeem_deliveries WHERE code_id=? AND request_hash=?",
                       (code_id, _digest(request_id))).fetchone()
    if row is None:
        return None
    if row["expires_at"] <= time.time():
        from core.db import RedeemError
        raise RedeemError("领取凭证已过期，请联系管理员恢复交付", code="delivery_expired", status=410)
    result = json.loads(row["result_json"])
    if result.get("requested_quantity") != quantity:
        from core.db import RedeemError
        raise RedeemError("原领取请求的数量已改变，请使用原数量恢复下载", code="request_conflict", status=409)
    result["lines"] = bytes(row["content"]).decode("utf-8").split("\n")[2:-1]
    result.update(download_id=_download_id(code, request_id), filename=row["filename"],
                  delivery_expires_at=row["expires_at"], resumed=True)
    return result


def save(conn, *, code: str, request_id: str | None, result: dict) -> dict:
    ensure_schema(conn)
    now = time.time()
    # Clear expired credential snapshots but retain idempotency tombstones.
    # A replay after expiry must not allocate another batch from the same CDK.
    conn.execute("UPDATE redeem_deliveries SET content=X'' WHERE expires_at<=? AND length(content)>0", (now,))
    request_id = request_id or secrets.token_urlsafe(32)
    download_id = _download_id(code, request_id)
    filename = f"chatgpt-credentials-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
    content = "\n".join([
        f"# ChatGPT 登录凭据（分组：{result.get('group_name') or '默认分组'}）",
        "# 格式：邮箱---密码---2FA密钥（没有 2FA 时最后一段为空）",
        *result["lines"], "",
    ]).encode("utf-8")
    metadata = {key: value for key, value in result.items() if key != "lines"}
    conn.execute("""INSERT INTO redeem_deliveries
        (code_id,request_hash,download_hash,expires_at,filename,content,result_json)
        VALUES(?,?,?,?,?,?,?)""", (
            result["code_id"], _digest(request_id), _digest(download_id),
            now + DELIVERY_TTL_SECONDS, filename, content,
            json.dumps(metadata, ensure_ascii=False),
        ))
    return {**result, "download_id": download_id, "filename": filename,
            "delivery_expires_at": now + DELIVERY_TTL_SECONDS, "resumed": False}


def read_download(download_id: str):
    from core import db
    if not isinstance(download_id, str) or not re.fullmatch(r"[a-f0-9]{64}", download_id):
        return None
    db._ensure_sqlite()
    with db._LOCK, closing(db._sqlite_conn()) as conn, conn:
        ensure_schema(conn)
        row = conn.execute("""SELECT d.*, c.status AS code_status
            FROM redeem_deliveries d JOIN redeem_codes c ON c.id=d.code_id
            WHERE d.download_hash=?""", (_digest(download_id),)).fetchone()
        if row is None:
            return None
        if row["expires_at"] <= time.time():
            conn.execute("UPDATE redeem_deliveries SET content=X'' WHERE download_hash=?", (_digest(download_id),))
            return None
        if row["code_status"] == "revoked":
            return None
        return {"content": bytes(row["content"]), "filename": row["filename"]}
