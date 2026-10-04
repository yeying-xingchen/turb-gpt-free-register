"""Atomic account activation progress. Credentials never persist here."""
from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager

from core import db

ACTIVE = {"queued", "checking", "extracting", "paying", "verifying"}
PUBLIC_FIELDS = ("status", "message", "updated_at")
PREFIX = "plus_activation_"


@contextmanager
def _connection():
    db._ensure_sqlite()
    with db._LOCK:
        conn = db._sqlite_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _read(conn, account_id):
    row = conn.execute("SELECT payload FROM accounts WHERE id=?", (account_id,)).fetchone()
    return json.loads(row[0]) if row else None


def _write(conn, account_id, account, **changes):
    before = dict(account)
    account.update({PREFIX + key: value for key, value in changes.items()})
    account[PREFIX + "updated_at"] = time.time()
    account["updated_at"] = db._now()
    conn.execute("UPDATE accounts SET payload=?, updated_at=? WHERE id=?", (
        json.dumps(account, ensure_ascii=False), account["updated_at"], account_id,
    ))
    from core import task_center_store
    task_center_store.sync_account(conn, before, account)


def public_view(account):
    return {"id": account["id"], **{key: account.get(PREFIX + key) for key in PUBLIC_FIELDS}}


def _group_exists(conn, name):
    if name == db.DEFAULT_ACCOUNT_GROUP:
        return True
    if conn.execute("SELECT 1 FROM account_groups WHERE group_name=?", (name,)).fetchone():
        return True
    # Legacy groups may exist only on accounts, without metadata yet.
    return conn.execute(
        "SELECT 1 FROM accounts WHERE account_group_name(json_extract(payload, '$.group_name')) = ? LIMIT 1",
        (name,),
    ).fetchone() is not None


def validate_success_group(value):
    """None preserves a resumed task's setting; an empty string disables moving."""
    if value is None or value == "":
        return value
    name = db._validate_account_group_name(value)
    with _connection() as conn:
        if not _group_exists(conn, name):
            raise ValueError("成功后转入的分组不存在，请刷新分组列表后重新选择")
    return name


def claim(account_id, *, success_group=None):
    with _connection() as conn:
        account = _read(conn, account_id)
        if not account:
            raise LookupError("账号不存在")
        if account.get(PREFIX + "status") in ACTIVE:
            return account, False
        changes = {} if success_group is None else {"success_group": success_group}
        _write(conn, account_id, account, run_id=uuid.uuid4().hex, status="queued", message="已加入 Plus 开通队列", **changes)
        return account, True


def update(account_id, run_id, status, message, *, step=None, checkout_key=None, failed_checkout_key=None):
    with _connection() as conn:
        account = _read(conn, account_id)
        if not account or account.get(PREFIX + "run_id") != run_id:
            return False
        changes = {"status": status, "message": message}
        if step is not None:
            changes["step"] = step
        if checkout_key is not None:
            changes["checkout_key"] = checkout_key
        if failed_checkout_key is not None:
            changes["failed_checkout_key"] = failed_checkout_key
        _write(conn, account_id, account, **changes)
        return True


def complete(account_id, run_id, message):
    """Commit verified success and the optional group move in one transaction."""
    with _connection() as conn:
        account = _read(conn, account_id)
        if (not account or account.get(PREFIX + "run_id") != run_id
                or account.get(PREFIX + "status") not in ACTIVE):
            return False
        if account.get("archived"):
            raise LookupError("账号已归档，未自动转移分组")
        name = account.get(PREFIX + "success_group")
        if name:
            if not _group_exists(conn, name):
                raise LookupError("Plus 套餐核验成功，但目标分组已不存在；请重新选择分组后继续")
            db._upsert_group_meta(conn, name)
            account["group_name"] = name
            message += f"，已转入分组「{name}」"
        _write(conn, account_id, account, status="succeeded", message=message, step="done")
        return True


def recover_interrupted():
    """Restart never silently repeats a charge; a new click resumes saved work."""
    with _connection() as conn:
        recovered = 0
        for row in conn.execute("SELECT id, payload FROM accounts").fetchall():
            account = json.loads(row["payload"])
            if account.get(PREFIX + "status") in ACTIVE:
                _write(conn, row["id"], account, status="interrupted",
                       message="服务重启，开通已暂停；请重新选择原凭据点击开通 Plus 继续核对原任务")
                recovered += 1
        return recovered
