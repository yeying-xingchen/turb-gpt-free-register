"""Durable submission journal; credentials and account ATs are never stored here."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from contextlib import contextmanager

from core import db

TERMINAL = {"succeeded", "completed", "failed", "rejected", "expired", "cancelled", "canceled",
            "released", "refunded", "not_activated"}
_FALLBACK_TERMINAL = {"failed", "rejected", "expired", "cancelled", "canceled", "released"}


def can_fallback(record) -> bool:
    """Only a confirmed failure may authorize a different payment candidate."""
    if not isinstance(record, dict):
        return False
    status = record.get("status")
    if not isinstance(status, str) or status not in _FALLBACK_TERMINAL:
        return False
    task = record.get("task")
    if task is None:
        task = {}
    if not isinstance(task, dict):
        return False
    task_status = task.get("status")
    if task_status is not None and (not isinstance(task_status, str) or task_status not in _FALLBACK_TERMINAL):
        return False
    if any(record.get(key) or task.get(key)
           for key in ("verifying", "submission_uncertain", "uncertain", "charged")):
        return False
    # A rejection after an ambiguous submit does not resolve that earlier POST.
    # A subsequently confirmed terminal task failure can resolve it.
    if status == "rejected" and (record.get("uncertain_history") or record.get("created") or task.get("created")):
        return False
    return True


def credential_hash(cdk: str) -> str:
    return hashlib.sha256(cdk.encode()).hexdigest()


@contextmanager
def _connection():
    db._ensure_sqlite()
    with db._LOCK:
        conn = db._sqlite_conn()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS scan_submissions (
                    id TEXT PRIMARY KEY,
                    account_id INTEGER NOT NULL,
                    provider TEXT NOT NULL,
                    credential_hash TEXT NOT NULL,
                    link_hash TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    UNIQUE(provider, credential_hash, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS scan_submissions_account ON scan_submissions(account_id);
                CREATE INDEX IF NOT EXISTS scan_submissions_link ON scan_submissions(link_hash);
            """)
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _write(conn, record):
    record["updated_at"] = time.time()
    conn.execute("""INSERT INTO scan_submissions VALUES(?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET payload=excluded.payload""", (
        record["id"], record["account_id"], record["provider"], record["credential_hash"],
        record["link_hash"], record["idempotency_key"], json.dumps(record, ensure_ascii=False),
    ))
    from core import task_center_store
    task_center_store.sync_payment_record(conn, record)
    # Update the compact account view in the same transaction as the journal.
    row = conn.execute("SELECT payload FROM accounts WHERE id=?", (record["account_id"],)).fetchone()
    if row:
        account = json.loads(row["payload"])
        fields = {
            "status": record["status"], "task_id": record.get("task_id", ""),
            "provider": record["provider"], "message": record.get("message", ""),
            "error": record.get("error", ""), "error_code": record.get("code", ""),
            "request_id": record.get("request_id", ""), "retryable": record.get("retryable"),
            "duplicate": record.get("duplicate", False), "ok": record["status"] in {"succeeded", "completed"},
            "checked_at": record["updated_at"],
        }
        account.update({"scan_request_" + key: value for key, value in fields.items()})
        account["updated_at"] = db._now()
        conn.execute("UPDATE accounts SET payload=?, updated_at=? WHERE id=?", (
            json.dumps(account, ensure_ascii=False), account["updated_at"], record["account_id"],
        ))


def latest(account_id: int) -> dict | None:
    with _connection() as conn:
        records = [json.loads(row[0]) for row in conn.execute(
            "SELECT payload FROM scan_submissions WHERE account_id=? ORDER BY rowid DESC", (account_id,),
        )]
    return max(records, key=lambda row: row["created_at"], default=None)


def reserve(*, account_id: int, provider: str, api_base: str, cdk: str, body: dict,
            idempotency_key: str, lease_seconds: int = 120, fallback_from=None) -> tuple[dict, bool]:
    """Commit a request snapshot BEFORE HTTP; replays retain the exact body/key.

    Live leases stop concurrent submits. Expired Masi leases remain unknown and
    cannot be resubmitted; public v1 replays use its original idempotency key.
    Internal fallback consumes the latest confirmed failure in this transaction.
    """
    fingerprint = credential_hash(cdk)
    link_hash = hashlib.sha256(body["link"].encode()).hexdigest()
    with _connection() as conn:
        exact = conn.execute("""SELECT payload FROM scan_submissions
            WHERE provider=? AND credential_hash=? AND idempotency_key=?""",
            (provider, fingerprint, idempotency_key)).fetchone()
        record = json.loads(exact[0]) if exact else None
        if record and (record["body"] != body or record["account_id"] != account_id or record["api_base"] != api_base):
            raise ValueError("同一幂等键的请求内容发生变化，请查询原支付任务")
        candidates = [json.loads(row[0]) for row in conn.execute(
            "SELECT payload FROM scan_submissions WHERE account_id=? OR link_hash=? ORDER BY rowid DESC",
            (account_id, link_hash),
        )]
        candidates.sort(key=lambda row: row["created_at"], reverse=True)
        previous = next((row for row in candidates if row["account_id"] == account_id), None)
        if record and previous and record["id"] != previous["id"]:
            raise ValueError("此请求已有后续支付记录，请查询本账号最新任务，不能重放历史请求")
        if fallback_from is not None:
            if not isinstance(fallback_from, str) or not previous or previous["id"] != fallback_from:
                raise ValueError("支付回退必须引用本账号当前最新的失败记录")
            if not can_fallback(previous):
                raise ValueError("原支付未明确失败，不能切换支付候选，请核对原任务")
            if any(row["account_id"] == account_id and row["provider"] == provider
                   and row["credential_hash"] == fingerprint for row in candidates):
                raise ValueError("支付回退必须使用尚未尝试的支付平台和凭据组合")
            # Inspect the whole history: an older success/uncertain order or any
            # other account's ownership of this link must not be hidden by a failure.
            if any(row["account_id"] != account_id or not can_fallback(row) for row in candidates):
                raise ValueError("此账号或链接仍有成功、待核实或其他账号的支付记录，不能回退")
            candidates = []
        if not record:
            # An unresolved order prevents paying a newly extracted link as well.
            record = next((row for row in candidates if row["account_id"] == account_id
                           and row["status"] not in TERMINAL), None)
        if not record:
            record = next((row for row in candidates if row["link_hash"] == link_hash
                           and (row.get("task_id") or row["status"] != "rejected"
                                or (row["credential_hash"] == fingerprint and row["provider"] == provider))), None)
        if record:
            if record["account_id"] != account_id:
                raise ValueError("此链接已由其他账号提交，请查询原账号的支付任务")
            if record["provider"] != provider or record["credential_hash"] != fingerprint or record["api_base"] != api_base:
                raise ValueError("此账号或链接已有支付记录，请使用原平台和原支付 CDK 查询，勿跨平台重复提交")
            if record["body"] != body:
                raise ValueError("此账号已有待处理支付记录，链接或邮箱已变化，请先查询原任务")
            if record.get("task_id") or record["status"] in TERMINAL - {"rejected"}:
                return record, False
            if record.get("lease_until", 0) > time.time():
                return record, False
            if record["provider"] == "masi" and record["status"] != "rejected":
                record.update(status="unknown", message="提交结果待核实；请到原平台核对订单与额度，不能自动重提")
                _write(conn, record)
                return record, False
            if record["provider"] == "seashore" and record["status"] != "rejected":
                # 发布者 API 没有幂等键，受理不明的提交重提会重复建单并重复暂扣次数。
                record.update(status="unknown", message="提交结果待核实；请到发布者平台核对任务与额度，不能自动重提")
                _write(conn, record)
                return record, False
            if (record["provider"] == "seashore" and record["status"] == "rejected"
                    and record.get("charged")):
                # link_rejected 之类已经扣掉 1 次、且不会创建任务：同一条链接重提只会再扣一次。
                raise ValueError("该支付链已被发布者平台拒绝并扣次，请重新提链后再提交")
            if record["status"] != "rejected":
                record["uncertain_history"] = True
        else:
            record = {
                "id": uuid.uuid4().hex, "account_id": account_id, "provider": provider,
                "api_base": api_base, "credential_hash": fingerprint, "link_hash": link_hash,
                "body": body, "idempotency_key": idempotency_key,
                "created_at": time.time(), "task_id": "", "task": {},
            }
            if fallback_from is not None:
                record["fallback_from"] = fallback_from
        record.update(status="submitting", message="正在提交，受理结果待确认", error="", code="",
                      lease_until=time.time() + lease_seconds)
        _write(conn, record)
        return record, True


def finish(record: dict, **changes) -> dict:
    with _connection() as conn:
        saved = conn.execute("SELECT payload FROM scan_submissions WHERE id=?", (record["id"],)).fetchone()
        if saved is None:
            raise RuntimeError("支付提交记录不存在")
        current = json.loads(saved[0])
        # A slow query must not regress a terminal result to queued/claimed.
        if current["status"] in TERMINAL and current["status"] != "rejected" and changes.get("status") not in TERMINAL:
            return current
        if changes.get("status") == "unknown":
            changes["uncertain_history"] = True
        if current.get("uncertain_history") and changes.get("status") == "rejected":
            changes["status"] = "unknown"
        current.update(changes, lease_until=0)
        _write(conn, current)
        return current


def public_view(record: dict) -> dict:
    status = record["status"]
    if status == "submitting" and record.get("lease_until", 0) <= time.time():
        status = "unknown"
    fields = ("task_id", "provider", "message", "error", "code", "retryable", "request_id",
              "http_status", "duplicate", "task", "updated_at", "created", "charged", "retry_after_seconds")
    result = {key: record[key] for key in fields if key in record}
    result.update(id=record["account_id"], status=status, email=record["body"].get("email", ""),
                  can_retry_same_key=record["provider"] in {"v1", "orderhub"} and not record.get("task_id")
                  and status in {"unknown", "rejected"})
    return result
