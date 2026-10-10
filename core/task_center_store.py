"""Public task projections and transactional, credential-free account execution history.

Account hooks use the connection owning the account/payment write. Registration
jobs remain the source of truth; their payloads are never returned. Account events
contain generated text from explicit status and phase allowlists.
"""
from __future__ import annotations

import json
import re
import time
from contextlib import closing
from datetime import datetime

from core import db, task_control

LABELS = {
    "registration": "账号注册", "codex_retry": "Codex补跑",
    "plus_activation": "开通 Plus", "live_check": "账号查活", "plan_check": "查询套餐",
    "quota_check": "查询额度与用量",
    "extract_link": "提取支付链接", "scan_payment": "扫码支付", "totp_setup": "开启 2FA",
    "email_change": "邮箱换绑", "codex_agent": "生成 Codex Agent",
}
PREFIXES = {kind: kind for kind in LABELS if kind not in {"registration", "codex_retry"}}
PREFIXES.update(scan_payment="scan_request", codex_retry="codex")
ACTIVE = ("pending", "running", "paused", "stopping")
_STATUS_GROUPS = {
    "pending": {"pending", "queued", "waiting"},
    "running": {"running", "checking", "extracting", "paying", "verifying", "submitting",
                "submitted", "accepted", "processing", "claimed", "retrying", "in_progress"},
    "success": {"success", "succeeded", "completed", "complete", "done", "live", "paid"},
    "failed": {"failed", "error", "rejected", "expired", "deactivated", "refunded", "not_activated"},
    "cancelled": {"cancelled", "canceled", "released", "account_deleted", "reset"},
    "stopped": {"stopped"},
    "needs_attention": {"unknown", "interrupted", "needs_attention", "awaiting_blik"},
    "paused": {"paused"}, "stopping": {"stopping"},
}
_NORMALIZED = {source: status for status, sources in _STATUS_GROUPS.items() for source in sources}
_PHASES = {
    "checking": (15, "检查账号"), "extracting": (35, "提取支付链接"),
    "paying": (65, "提交支付"), "verifying": (85, "核验 Plus 套餐"),
}
_STAGE = {
    "pending": "等待执行", "running": "执行中", "success": "已完成", "failed": "失败",
    "cancelled": "已取消", "stopped": "已停止", "needs_attention": "待核实",
    "paused": "已暂停", "stopping": "取消中",
}
_MESSAGES = {
    "pending": "任务已入队，等待执行", "running": "任务正在执行", "success": "任务已完成",
    "failed": "任务失败，请在对应功能页核对原因", "cancelled": "任务已取消",
    "stopped": "任务已停止", "needs_attention": "执行结果待核实，请在对应功能页核对原任务",
    "paused": "任务已暂停，等待恢复", "stopping": "已发送取消信号，等待任务退出",
}
_SUCCESS_MESSAGES = {
    "plus_activation": "Plus 套餐核验成功", "live_check": "账号查活成功，账号可用",
    "plan_check": "套餐查询成功", "quota_check": "额度与用量查询成功", "extract_link": "支付链接提取成功",
    "scan_payment": "支付成功", "totp_setup": "2FA 已开启", "email_change": "邮箱换绑成功",
    "codex_agent": "Codex Agent 凭证生成成功", "codex_retry": "Codex 补跑成功",
}
_MIGRATION_KEY = "account_tasks_backfill_v1"


def _source(value) -> str:
    text = str(value or "").strip().lower()
    return text if text in _NORMALIZED else "unknown"


def _timestamp(value, fallback=None):
    """Validate timestamps rather than storing arbitrary payload strings."""
    if value in (None, ""):
        return fallback
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value).isoformat(timespec="microseconds")
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return fallback


def _email(value):
    text = str(value or "")
    return text if re.fullmatch(r"[^\s@<>]+@[^\s@<>]+", text) and len(text) <= 320 else ""


def _progress(value, fallback=0):
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError, OverflowError):
        return fallback


def _presentation(kind, source, progress=None):
    status = _NORMALIZED[source]
    stage = _STAGE[status]
    message = _SUCCESS_MESSAGES.get(kind, _MESSAGES[status]) if status == "success" else _MESSAGES[status]
    amount = 100 if status == "success" else 0 if status == "pending" else 50 if status == "running" else 0
    if kind == "plus_activation" and source in _PHASES:
        amount, stage = _PHASES[source]
        message = stage
    elif source == "deactivated":
        stage = message = "账号已废"
    elif source == "account_deleted":
        if kind in {"scan_payment", "plus_activation", "extract_link"}:
            status, stage = "needs_attention", "待核实"
            message = "账号已删除，请核对原支付或提链任务"
        else:
            message = "账号已删除，任务已结束"
    elif source == "reset":
        message = "任务状态已重置，原执行已结束"
    elif source == "interrupted":
        message = "任务已中断，请核对原任务后继续"
    elif source == "awaiting_blik":
        message = "等待 BLIK 确认，请核对原支付任务"
    if kind != "plus_activation" and status == "running":
        amount = _progress(progress, amount)
    return status, amount, stage, message, message if status in {"failed", "needs_attention"} else ""


def _result_message(account, kind, source, default):
    """Classify untrusted errors; emit only fixed phrases and allowlisted plans."""
    status = _NORMALIZED[source]
    if kind == "plan_check" and status == "success":
        plan = str(account.get("current_plan_type") or account.get("plan_type") or "").lower()
        if plan in {"free", "plus", "pro", "team", "enterprise", "edu", "go"}:
            return f"套餐查询成功，当前套餐：{plan}"
    if status not in {"failed", "needs_attention"} or source in {"deactivated", "interrupted", "awaiting_blik"}:
        return default
    prefix = PREFIXES.get(kind, kind)
    text = str(account.get(prefix + "_error") or account.get(prefix + "_message") or "").lower()
    reasons = (
        (("重启", "restart", "中断", "interrupted"), "服务重启或执行中断"),
        (("余额", "额度", "quota", "insufficient", "balance"), "平台额度不足或额度受限"),
        (("超时", "timeout", "timed out"), "请求超时，受理结果需核对"),
        (("频率", "限流", "rate limit", "429"), "上游请求频率受限"),
        (("过期", "expired", "失效", "unauthorized", "401"), "账号或授权凭据已失效"),
        (("网络", "network", "connection", "proxy", "代理"), "网络或代理连接失败"),
        (("分组", "group"), "目标分组不可用"),
        (("核验", "verify", "verification"), "结果核验未通过"),
        (("拒绝", "rejected", "forbidden", "403"), "上游拒绝请求"),
    )
    for needles, reason in reasons:
        if any(needle in text for needle in needles):
            return f"{reason}，请在对应功能页核对"
    return default


def _initialize(conn):
    """Called once by db initialization, after existing data has been imported."""
    # execute(), not executescript(): never implicitly commit the caller's writes.
    conn.execute("""CREATE TABLE IF NOT EXISTS account_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id INTEGER NOT NULL, job_type TEXT NOT NULL,
        source_record_id TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL, source_status TEXT NOT NULL,
        email TEXT NOT NULL DEFAULT '', progress INTEGER NOT NULL DEFAULT 0,
        stage TEXT NOT NULL DEFAULT '', progress_message TEXT NOT NULL DEFAULT '',
        error_message TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, started_at TEXT, completed_at TEXT,
        updated_at TEXT NOT NULL, lease_until REAL
    )""")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(account_tasks)")}
    if "lease_until" not in columns:
        conn.execute("ALTER TABLE account_tasks ADD COLUMN lease_until REAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS account_task_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, task_id INTEGER NOT NULL,
        created_at TEXT NOT NULL, source_status TEXT NOT NULL, message TEXT NOT NULL
    )""")
    for statement in (
        "CREATE INDEX IF NOT EXISTS idx_account_tasks_status ON account_tasks(status, id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_account_tasks_lease ON account_tasks(job_type, source_status, status, lease_until)",
        "CREATE INDEX IF NOT EXISTS idx_account_tasks_account ON account_tasks(account_id, job_type, id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_account_tasks_created ON account_tasks(created_at DESC, id DESC)",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_account_tasks_record ON account_tasks(job_type, source_record_id) WHERE source_record_id!=''",
        "CREATE INDEX IF NOT EXISTS idx_account_task_events_task ON account_task_events(task_id, id)",
    ):
        conn.execute(statement)
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    if conn.execute("SELECT 1 FROM storage_meta WHERE key=?", (_MIGRATION_KEY,)).fetchone():
        return
    # The migration is durable and batched. Normal reads never rescan accounts.
    if db._table_exists(conn, "scan_submissions"):
        cursor = conn.execute("SELECT payload FROM scan_submissions ORDER BY rowid")
        while batch := cursor.fetchmany(250):
            for stored in batch:
                sync_payment_record(conn, json.loads(stored["payload"]), backfill=True)
    cursor = conn.execute("SELECT payload FROM accounts ORDER BY id")
    while batch := cursor.fetchmany(250):
        for stored in batch:
            sync_account(conn, None, json.loads(stored["payload"]), backfill=True)
    conn.execute("INSERT INTO storage_meta(key,value) VALUES(?,?)", (_MIGRATION_KEY, db._now()))


def _latest(conn, account_id, kind):
    return conn.execute(
        "SELECT * FROM account_tasks WHERE account_id=? AND job_type=? ORDER BY id DESC LIMIT 1",
        (account_id, kind),
    ).fetchone()


def _event(conn, task_id, kind, source, timestamp, message=None):
    _, _, stage, default, _ = _presentation(kind, source)
    message = default if message is None else message
    conn.execute(
        "INSERT INTO account_task_events(task_id,created_at,source_status,message) VALUES(?,?,?,?)",
        (task_id, timestamp, source, f"{LABELS[kind]} · {stage}：{message}"),
    )


def _notify_completion(conn, *, task_id, event_key, kind, status, completed_at, email):
    """Enqueue only allowlisted task metadata in the caller's transaction."""
    from core.mail_notifications import enqueue_notification

    kind = kind if kind in LABELS else "registration"
    email = _email(email)
    enqueue_notification(
        conn, event_key=event_key, category="task", title=f"{LABELS[kind]} · {_STAGE[status]}",
        fields={"任务编号": task_id, "任务类型": LABELS[kind], "任务状态": _STAGE[status],
                "结束时间": _timestamp(completed_at, db._now())},
        emails=[email] if email else [],
    )


def _store_state(conn, account, kind, source, *, task=None, created_at=None,
                 started_at=None, completed_at=None, record_id="", progress=None, notify=True):
    now = db._now()
    status, amount, stage, message, error = _presentation(kind, source, progress)
    message = _result_message(account, kind, source, message)
    error = message if status in {"failed", "needs_attention"} else ""
    created = _timestamp(created_at, now)
    started = _timestamp(started_at)
    completed = _timestamp(completed_at, now) if status not in ACTIVE else None
    if task:
        created = task["created_at"]
        started = task["started_at"] or started
        if status not in ACTIVE and task["completed_at"] and source == task["source_status"]:
            completed = task["completed_at"]
    if started is None and status == "running":
        started = now
    email = task["email"] if task else _email(account.get("email"))
    values = (status, source, email, amount, stage, message, error, started, completed, now)
    if task:
        conn.execute("""UPDATE account_tasks SET status=?,source_status=?,email=?,progress=?,stage=?,
            progress_message=?,error_message=?,started_at=?,completed_at=?,updated_at=? WHERE id=?""",
            (*values, task["id"]))
        task_id = task["id"]
    else:
        cursor = conn.execute("""INSERT INTO account_tasks
            (status,source_status,email,progress,stage,progress_message,error_message,started_at,
             completed_at,updated_at,account_id,job_type,source_record_id,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (*values, int(account["id"]), kind, record_id, created))
        task_id = cursor.lastrowid
    if task is None or task["source_status"] != source:
        _event(conn, task_id, kind, source, now, message)
    if notify and status not in ACTIVE and (task is None or task["status"] in ACTIVE):
        _notify_completion(conn, task_id=f"account-{task_id}",
                           event_key=f"task:account:{task_id}:completed", kind=kind,
                           status=status, completed_at=completed, email=email)
    return task_id


def _has_registration_retry(conn, account):
    return conn.execute("""SELECT 1 FROM registration_jobs
        WHERE status IN ('pending','running','paused','stopping')
        AND json_extract(payload,'$.job_type')='codex_retry'
        AND (json_extract(payload,'$.account_id')=? OR (email!='' AND lower(email)=lower(?))) LIMIT 1""",
        (int(account["id"]), str(account.get("email") or ""))).fetchone() is not None


def _has_payment_journal(conn, account_id):
    if not db._table_exists(conn, "scan_submissions"):
        return False
    return conn.execute("SELECT 1 FROM scan_submissions WHERE account_id=? LIMIT 1", (account_id,)).fetchone() is not None


def sync_account(conn, before, account, *, backfill=False):
    """Track account state changes inside its write transaction; never own a commit."""
    before = before or {}
    for kind, prefix in PREFIXES.items():
        key = prefix + "_status"
        value, previous = account.get(key), before.get(key)
        checked_key = {"live_check": "live_checked_at", "plan_check": "plan_checked_at",
                       "quota_check": "quota_checked_at"}.get(kind, prefix + "_checked_at")
        watched = [prefix + suffix for suffix in (
            "_status", "_run_id", "_queued_at", "_started_at", "_completed_at", "_progress", "_error", "_message",
        )] + [checked_key]
        if before and all(account.get(field) == before.get(field) for field in watched):
            continue
        if kind == "scan_payment" and _has_payment_journal(conn, account["id"]):
            continue  # The journal, keyed by record ID, owns these executions.
        if not value and not previous:
            continue
        task = _latest(conn, int(account["id"]), kind)
        if backfill and task:
            continue
        if kind == "codex_retry":
            if _has_registration_retry(conn, account) or (not task and value != "retrying"):
                continue
        if not value:
            if task and task["status"] in ACTIVE:
                _store_state(conn, account, kind, "reset", task=task, notify=not backfill)
            continue
        source = _source(value)
        new_queue = source in {"queued", "pending", "retrying"} and (
            value != previous or account.get(prefix + "_run_id") != before.get(prefix + "_run_id")
            or account.get(prefix + "_queued_at") != before.get(prefix + "_queued_at")
        )
        if new_queue and task:
            if task["status"] in ACTIVE:
                _store_state(conn, account, kind, "interrupted", task=task, notify=not backfill)
            task = None
        if task is None and kind == "codex_retry" and source != "retrying":
            continue
        new_result = bool(task and task["status"] not in (*ACTIVE, "needs_attention")
                          and _NORMALIZED[source] != task["status"])
        if new_result:
            if kind == "codex_retry":
                continue  # Only retrying starts an account-side authorization.
            task = None  # A new result must not rewrite a previous completed run.
        created = (account.get(prefix + "_queued_at") or account.get(prefix + "_started_at")
                   or account.get(prefix + "_updated_at") or account.get(checked_key)
                   or account.get("updated_at") or account.get("created_at"))
        if new_result:
            created = account.get(checked_key) or account.get("updated_at") or db._now()
        _store_state(conn, account, kind, source, task=task,
                     created_at=created, started_at=account.get(prefix + "_started_at"),
                     completed_at=account.get(prefix + "_completed_at") or account.get(checked_key),
                     progress=account.get(prefix + "_progress"), notify=not backfill)


def sync_payment_record(conn, record, *, backfill=False):
    """Project one durable payment journal record, without body/hash/CDK/link fields."""
    record_id = str(record["id"])
    task = conn.execute(
        "SELECT * FROM account_tasks WHERE job_type='scan_payment' AND source_record_id=? LIMIT 1",
        (record_id,),
    ).fetchone()
    if task and backfill:
        return
    stored = conn.execute("SELECT id,email FROM accounts WHERE id=?", (int(record["account_id"]),)).fetchone()
    account = dict(stored) if stored else {"id": int(record["account_id"]), "email": ""}
    account.update(scan_request_error=record.get("error"), scan_request_message=record.get("message"))
    source = _source(record.get("status"))
    try:
        lease_until = max(0, float(record.get("lease_until") or 0))
    except (TypeError, ValueError, OverflowError):
        lease_until = 0
    if source == "submitting" and lease_until <= time.time():
        source = "unknown"
    if not stored and _NORMALIZED[source] in ACTIVE:
        source = "account_deleted"
    # Upgrade a compact pre-journal task, avoiding two copies of the same submit.
    if task is None:
        legacy = _latest(conn, account["id"], "scan_payment")
        if legacy and not legacy["source_record_id"] and legacy["status"] in ACTIVE:
            conn.execute("UPDATE account_tasks SET source_record_id=? WHERE id=?", (record_id, legacy["id"]))
            task = legacy
    task_id = _store_state(conn, account, "scan_payment", source, task=task, record_id=record_id,
                           created_at=record.get("created_at"), started_at=record.get("created_at"),
                           completed_at=record.get("updated_at"), notify=not backfill)
    conn.execute("UPDATE account_tasks SET lease_until=? WHERE id=?", (lease_until, task_id))


def _expire_payment_leases():
    """Only inspect expired local submissions; remote queued/claimed work is untouched."""
    db._ensure_sqlite()
    now = time.time()
    where = "job_type='scan_payment' AND source_status='submitting' AND status='running' AND (lease_until IS NULL OR lease_until<=?)"
    with closing(db._sqlite_conn()) as conn:
        if not conn.execute(f"SELECT 1 FROM account_tasks WHERE {where} LIMIT 1", (now,)).fetchone():
            return
    with db._row_write_transaction() as conn:
        while batch := conn.execute(f"SELECT * FROM account_tasks WHERE {where} ORDER BY id LIMIT 250", (now,)).fetchall():
            for task in batch:
                _store_state(conn, {"id": task["account_id"], "email": task["email"]},
                             "scan_payment", "unknown", task=task)


def end_account_tasks(conn, account_id):
    """Deletion retains history and ends every outstanding local/payment operation."""
    tasks = conn.execute("""SELECT * FROM account_tasks WHERE account_id=?
        AND status IN ('pending','running','paused','stopping')""", (int(account_id),)).fetchall()
    for task in tasks:
        _store_state(conn, {"id": account_id, "email": task["email"]}, task["job_type"], "account_deleted", task=task)
    return len(tasks)


def recover_interrupted_codex_retries(conn=None):
    """Startup hook: call with runtime's transaction after its direct Codex reset."""
    if conn is None:
        with db._row_write_transaction() as connection:
            return recover_interrupted_codex_retries(connection)
    tasks = conn.execute("""SELECT * FROM account_tasks WHERE job_type='codex_retry'
        AND status IN ('pending','running','paused','stopping')""").fetchall()
    for task in tasks:
        _store_state(conn, {"id": task["account_id"], "email": task["email"]},
                     "codex_retry", "interrupted", task=task)
    return len(tasks)


# List SQL selects public metadata only. Credential values used separately for
# redaction are never attached to task dictionaries or persisted task events.
_REG_SELECT = """SELECT 'registration' AS kind,id AS sequence,
    'registration-' || id AS id, id AS job_id,
    CASE WHEN json_extract(payload,'$.job_type')='codex_retry' THEN 'codex_retry' ELSE 'registration' END AS job_type,
    status, status AS source_status, email,
    json_extract(payload,'$.account_id') AS account_id,
    json_extract(payload,'$.progress') AS progress,
    json_extract(payload,'$.stage') AS stage,
    json_extract(payload,'$.progress_message') AS progress_message,
    json_extract(payload,'$.error_message') AS error_message,
    created_at,json_extract(payload,'$.started_at') AS started_at,
    json_extract(payload,'$.completed_at') AS completed_at,
    COALESCE(julianday(created_at),0) AS sort_at
    FROM registration_jobs"""
_ACCOUNT_SELECT = """SELECT 'account' AS kind,id AS sequence,'account-' || id AS id,NULL AS job_id,
    job_type,status,source_status,email,account_id,progress,stage,progress_message,error_message,created_at,started_at,completed_at,
    COALESCE(julianday(created_at),0) AS sort_at FROM account_tasks"""
_ACTIVE_SQL = "('pending','running','paused','stopping')"
# 任务中心里可调整并发的任务类型 = 已注册后台任务池的类型。
_CONTROL_ROW_STATES = {
    "paused": ("paused", "已暂停", "任务已暂停，等待恢复"),
    "running": ("running", "继续执行", "任务已恢复，继续执行"),
    "stopping": ("stopping", "取消中", "已发送取消信号，等待任务退出"),
    "cancelled": ("cancelled", "已取消", "用户取消任务"),
}


def set_task_control_state(kind: str, account_id, state: str) -> bool:
    """任务中心按钮的即时反馈：把最新活跃行标记为暂停/恢复/取消。

    账号字段仍由各服务在检查点写回；这里只更新任务投影，让界面立刻看到状态。
    """
    spec = _CONTROL_ROW_STATES.get(str(state))
    if spec is None:
        return False
    status, stage, message = spec
    parsed_id = int(account_id)
    now = db._now()
    with db._row_write_transaction() as conn:
        task = _latest(conn, parsed_id, str(kind))
        if task is None or task["status"] not in ACTIVE:
            return False
        conn.execute(
            """UPDATE account_tasks SET status=?,source_status=?,stage=?,progress_message=?,
               error_message=?,completed_at=?,updated_at=? WHERE id=?""",
            (
                status, status, stage, message,
                message if status == "cancelled" else "",
                now if status == "cancelled" else None,
                now, task["id"],
            ),
        )
        _event(conn, task["id"], kind, status, now, message)
        if status not in ACTIVE:
            _notify_completion(conn, task_id=f"account-{task['id']}",
                               event_key=f"task:account:{task['id']}:completed", kind=kind,
                               status=status, completed_at=now, email=task["email"])
    return True


def _registration_secrets(conn, row):
    # Only selected page rows are inspected, using their indexed IDs. These
    # credential values are used for redaction and never enter a public dict.
    paths = ("access_token", "refresh_token", "id_token", "password", "registration_password",
             "totp_secret", "cdk", "token", "order_secret", "extra_json")
    columns = ",".join(f"json_extract(payload,'$.{field}')" for field in paths)
    values = []
    for table, number in (("registration_jobs", row["job_id"]), ("accounts", row["account_id"])):
        if not isinstance(number, int):
            continue
        found = conn.execute(f"SELECT {columns} FROM {table} WHERE id=? LIMIT 1", (number,)).fetchone()
        if not found:
            continue
        values.extend(value for value in tuple(found)[:-1] if isinstance(value, str) and value)
        try:
            extra = json.loads(found[-1]) if isinstance(found[-1], str) else {}
        except (ValueError, TypeError):
            extra = {}
        if isinstance(extra, dict):
            values.extend(extra.get(field) for field in paths if isinstance(extra.get(field), str) and extra[field])
    return values


def _registration_text(value, secrets):
    from core.scan_api_client import _safe_message
    text = _safe_message(value, secrets)
    text = re.sub(r"(?:https?://|otpauth://)[^\s<>]+", "[链接已隐藏]", text, flags=re.I)
    text = re.sub(r"\b(?:bearer\s+)[^\s,;}]+", "[凭据已隐藏]", text, flags=re.I)
    text = re.sub(r"(?i)(?:password|registration_password|totp(?:_secret)?|cdk|refresh_token|id_token|secret|密码|密钥)\s*[=:：]\s*[^\s,;}]+", "[凭据已隐藏]", text)
    return text


def _public(row, conn):
    result = dict(row)
    kind = result.pop("kind")
    result.pop("sequence")
    result.pop("sort_at")
    source = _source(result["source_status"])
    job_type = result["job_type"]
    status, _, stage, message, error = _presentation(job_type, source)
    result.update(task_id=result["id"], label=LABELS[job_type], status=status, source_status=source,
                  email=_email(result["email"]), progress=_progress(result["progress"]),
                  stage=stage, progress_message=message, error_message=error)
    for key in ("created_at", "started_at", "completed_at"):
        result[key] = _timestamp(result.get(key))
    if kind == "registration":
        secrets = _registration_secrets(conn, row)
        result["stage"] = _registration_text(row["stage"], secrets) or stage
        result["progress_message"] = _registration_text(row["progress_message"], secrets) or message
        result["error_message"] = _registration_text(row["error_message"], secrets) or ("任务失败，请查看注册日志" if status == "failed" else error)
        result["capabilities"] = {
            "pause": status in {"pending", "running"}, "resume": status == "paused",
            "cancel": status in {"pending", "running", "paused"},
        }
        result["control_state"] = status if status in {"paused", "running"} else None
        value = result.get("account_id")
        result["account_id"] = int(value) if isinstance(value, int) and not isinstance(value, bool) else None
    else:
        result.pop("job_id")
        result.update(stage=row["stage"], progress_message=row["progress_message"], error_message=row["error_message"])
        account_id = result.get("account_id")
        account_id = int(account_id) if isinstance(account_id, int) and not isinstance(account_id, bool) else None
        result["account_id"] = account_id
        capabilities, control_state = _account_capabilities(job_type, account_id, status)
        result["capabilities"] = capabilities
        result["control_state"] = control_state
    return result


def _account_capabilities(job_type: str, account_id, status: str) -> tuple[dict, str | None]:
    """账号任务的可控能力来自任务控制层的实时状态，而不是数据库字段。"""
    idle = {"pause": False, "resume": False, "cancel": False}
    if not account_id or status not in ACTIVE or task_control.get_pool(job_type) is None:
        return idle, None
    state = task_control.control_state(job_type, account_id)
    if state is None:
        return idle, None
    return {
        "pause": state == "running",
        "resume": state == "paused",
        "cancel": state in {"running", "paused"},
    }, state


def filter_options() -> dict:
    """筛选下拉项：任务类型取展示标签，状态取统一状态分组。"""
    return {
        "job_types": [{"value": name, "label": label} for name, label in LABELS.items()],
        "statuses": [{"value": name, "label": _STAGE[name]} for name in _STAGE],
    }


def normalize_filters(job_type=None, status=None, keyword=None) -> tuple:
    """校验并归一化筛选条件；未知类型/状态直接拒绝，关键词只做 LIKE 转义。"""
    kind = str(job_type or "").strip().lower()
    if kind and kind not in LABELS:
        raise ValueError("不支持的任务类型")
    group = str(status or "").strip().lower()
    if group and group not in _STATUS_GROUPS:
        raise ValueError("不支持的任务状态")
    text = str(keyword or "").strip()[:200]
    text = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return kind or None, group or None, text or None


def _filter_clauses(table, job_type, status, keyword):
    """一侧 UNION 的筛选片段；值经白名单校验并全部走参数绑定。"""
    clauses, params = [], []
    if job_type:
        if table == "registration":
            if job_type == "codex_retry":
                clauses.append("json_extract(payload,'$.job_type')='codex_retry'")
            elif job_type == "registration":
                clauses.append("COALESCE(json_extract(payload,'$.job_type'),'')!='codex_retry'")
            else:
                clauses.append("1=0")  # 注册任务表没有这类账号任务
        else:
            clauses.append("job_type=?")
            params.append(job_type)
    if status:
        sources = sorted(_STATUS_GROUPS.get(status, ()))
        if sources:
            clauses.append("status IN (" + ",".join("?" for _ in sources) + ")")
            params.extend(sources)
        else:
            clauses.append("1=0")
    if keyword:
        prefix = "registration" if table == "registration" else "account"
        clauses.append(f"(email LIKE ? ESCAPE '\\' OR ('{prefix}-' || id) LIKE ? ESCAPE '\\')")
        params.extend((f"%{keyword}%", f"%{keyword}%"))
    return clauses, params


def _filtered_union(base, job_type=None, status=None, keyword=None) -> tuple:
    """两个来源各自的 WHERE 与参数：筛选条件按表下推，分页总数与列表保持一致。"""
    reg_extra, reg_params = _filter_clauses("registration", job_type, status, keyword)
    acct_extra, acct_params = _filter_clauses("account", job_type, status, keyword)
    reg_where = " AND ".join([base, *reg_extra])
    acct_where = " AND ".join([base, *acct_extra])
    sql = f"{_REG_SELECT} WHERE {reg_where} UNION ALL {_ACCOUNT_SELECT} WHERE {acct_where}"
    return sql, [*reg_params, *acct_params]


def _filter_where(base, job_type, status, keyword) -> tuple:
    """COUNT 查询的按表 WHERE 与参数（顺序与 _filtered_union 一致）。"""
    reg_extra, reg_params = _filter_clauses("registration", job_type, status, keyword)
    acct_extra, acct_params = _filter_clauses("account", job_type, status, keyword)
    return " AND ".join([base, *reg_extra]), " AND ".join([base, *acct_extra]), [*reg_params, *acct_params]


def list_active_tasks(limit=5000, *, job_type=None, status=None, keyword=None) -> list[dict]:
    """Newest executions first, globally across types; at most 5,000 rows."""
    _expire_payment_leases()
    limit = max(1, min(5000, int(limit)))
    job_type, status, keyword = normalize_filters(job_type, status, keyword)
    sql, params = _filtered_union("status IN " + _ACTIVE_SQL, job_type, status, keyword)
    with closing(db._sqlite_conn()) as conn:
        rows = conn.execute(
            f"SELECT * FROM ({sql}) ORDER BY sort_at DESC,sequence DESC,kind DESC LIMIT ?",
            (*params, limit),
        )
        return [_public(row, conn) for row in rows]


def list_history_tasks_page(limit=20, offset=0, *, job_type=None, status=None, keyword=None) -> dict:
    """SQL COUNT and global LIMIT/OFFSET, with needs_attention included in history."""
    _expire_payment_leases()
    limit, offset = max(1, min(500, int(limit))), max(0, int(offset))
    job_type, status, keyword = normalize_filters(job_type, status, keyword)
    base = "status NOT IN " + _ACTIVE_SQL
    reg_where, acct_where, count_params = _filter_where(base, job_type, status, keyword)
    sql, params = _filtered_union(base, job_type, status, keyword)
    with closing(db._sqlite_conn()) as conn, conn:
        conn.execute("BEGIN")  # count and page share a consistent WAL snapshot
        total = conn.execute(f"""SELECT (SELECT COUNT(*) FROM registration_jobs WHERE {reg_where})
            + (SELECT COUNT(*) FROM account_tasks WHERE {acct_where})""", count_params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM ({sql}) ORDER BY sort_at DESC,sequence DESC,kind DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        return {"items": [_public(row, conn) for row in rows], "total": total}


def concurrency_overview() -> list[dict]:
    """任务中心可调整并发的任务类型：当前并发数、运行中与排队数量。"""
    items = []
    for status in task_control.pools_status():
        name = status["name"]
        items.append({**status, "label": LABELS.get(name, name)})
    return sorted(items, key=lambda item: item["name"])


def task_status_counts(*, job_type=None, status=None, keyword=None) -> dict:
    """各状态计数；带筛选时统计口径与当前列表一致。"""
    _expire_payment_leases()
    counts = {group: 0 for group in _STATUS_GROUPS}
    job_type, status, keyword = normalize_filters(job_type, status, keyword)
    reg_where, acct_where, params = _filter_where("1=1", job_type, status, keyword)
    with closing(db._sqlite_conn()) as conn:
        for row in conn.execute(
            f"""SELECT status,COUNT(*) AS n FROM registration_jobs WHERE {reg_where} GROUP BY status
                UNION ALL SELECT status,COUNT(*) AS n FROM account_tasks WHERE {acct_where} GROUP BY status""",
            params,
        ):
            counts[_NORMALIZED[_source(row["status"])]] += row["n"]
    counts["active"] = sum(counts[group] for group in ACTIVE)
    counts["total"] = sum(value for group, value in counts.items() if group != "active")
    return counts


def _parse_id(task_id):
    match = re.fullmatch(r"(registration|account)-([1-9][0-9]{0,18})", str(task_id))
    if not match:
        return None
    number = int(match.group(2))
    return (match.group(1), number) if number <= 2**63 - 1 else None


def get_task(task_id) -> dict | None:
    parsed = _parse_id(task_id)
    if parsed is None:
        return None
    _expire_payment_leases()
    kind, number = parsed
    query = _REG_SELECT if kind == "registration" else _ACCOUNT_SELECT
    with closing(db._sqlite_conn()) as conn:
        row = conn.execute(query + " WHERE id=? LIMIT 1", (number,)).fetchone()
        return _public(row, conn) if row else None


def _operation_log_detail(kind: str, account_id) -> str | None:
    """提交支付 / 提链的完整操作日志（含请求、响应与明文凭据）。"""
    from core import operation_log
    sections = []
    for label, log_kind in (("提链", operation_log.EXTRACT), ("提交支付", operation_log.PAYMENT)):
        if kind not in {"plus_activation", log_kind}:
            continue
        try:
            path = operation_log.log_path(log_kind, account_id)
        except (TypeError, ValueError):
            continue
        text = operation_log.read(log_kind, account_id).strip()
        if text:
            sections.append(f"===== {label}日志 · {path.name} =====\n{text}")
    return "\n\n".join(sections) or None


def _account_detail_log(task: dict) -> str | None:
    """Read the legacy detailed log belonging to an account operation."""
    kind = str(task.get("job_type") or "")
    email = str(task.get("email") or "")
    account_id = task.get("account_id")
    if kind in {"scan_payment", "extract_link", "plus_activation"} and account_id:
        return _operation_log_detail(kind, account_id)
    path = None
    if kind == "live_check" and email:
        from core.account_liveness import log_path
        path = log_path(email)
    elif kind == "totp_setup" and email:
        from core.twofa_service import log_path
        path = log_path(email)
    elif kind == "email_change" and account_id:
        from core.email_change_service import log_path
        path = log_path(int(account_id))
    if path is None or not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def read_task_log(task_id) -> str:
    """Read the complete detailed log, with state events as a safe fallback."""
    parsed = _parse_id(task_id)
    if parsed is None or parsed[0] != "account":
        return ""
    task = get_task(task_id)
    if task is None:
        return ""
    detailed = _account_detail_log(task)
    if detailed is not None:
        return detailed
    db._ensure_sqlite()
    with closing(db._sqlite_conn()) as conn:
        rows = conn.execute(
            "SELECT created_at,message FROM account_task_events WHERE task_id=? ORDER BY id",
            (parsed[1],),
        )
        return "\n".join(f"[{row['created_at']}] {row['message']}" for row in rows)


def list_latest_tasks_for_accounts(account_ids, job_type: str | None = None) -> list[dict]:
    """Return the newest public task for each requested account.

    Registration and Codex retry executions live in ``registration_jobs``;
    account operations live in ``account_tasks``.  Keeping this lookup server
    side lets the UI open the right log without exposing credentials or reading
    an entire task history into the browser.
    """
    ids = []
    seen = set()
    for raw in account_ids or []:
        try:
            number = int(raw)
        except (TypeError, ValueError):
            continue
        if number > 0 and number not in seen:
            seen.add(number)
            ids.append(number)
    if not ids:
        return []
    allowed = set(LABELS) | {"registration"}
    kind = str(job_type or "").strip().lower()
    if kind and kind not in allowed:
        raise ValueError("不支持的任务类型")
    placeholders = ",".join("?" for _ in ids)
    result = []
    with closing(db._sqlite_conn()) as conn:
        if not kind or kind in {"registration", "codex_retry"}:
            type_clause = ""
            params = [*ids]
            if kind:
                type_clause = " AND job_type=?"
                params.append(kind)
            rows = conn.execute(
                f"""SELECT * FROM ({_REG_SELECT})
                    WHERE account_id IN ({placeholders}){type_clause}
                    ORDER BY sequence DESC""",
                params,
            ).fetchall()
            chosen = set()
            for row in rows:
                account_id = int(row["account_id"] or 0)
                if account_id in chosen:
                    continue
                chosen.add(account_id)
                result.append(_public(row, conn))
        if not kind or kind not in {"registration", "codex_retry"}:
            type_clause = ""
            params = [*ids]
            if kind:
                type_clause = " AND job_type=?"
                params.append(kind)
            rows = conn.execute(
                f"""SELECT * FROM ({_ACCOUNT_SELECT})
                    WHERE account_id IN ({placeholders}){type_clause}
                    ORDER BY sequence DESC""",
                params,
            ).fetchall()
            chosen = set()
            for row in rows:
                account_id = int(row["account_id"] or 0)
                if account_id in chosen:
                    continue
                chosen.add(account_id)
                result.append(_public(row, conn))
    return sorted(result, key=lambda item: str(item.get("created_at") or ""), reverse=True)

