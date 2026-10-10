"""Exclusive ownership and startup recovery for a local SQLite runtime.

Application construction is deliberately independent of this lifecycle. Both
supported CLI entry points acquire the same database lock before doing work.
"""
from __future__ import annotations

import atexit
import logging
import os
from pathlib import Path
from contextlib import closing

from core import db

logger = logging.getLogger(__name__)


class DatabaseOwner:
    def __init__(self, database_path: Path):
        self.database_path = database_path.expanduser().resolve()
        self.pid = os.getpid()
        self.recovered = False
        self.mail_worker = None
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.database_path.with_name("." + self.database_path.name + ".runtime.lock")
        self._handle = lock_path.open("a+b")
        try:
            self._handle.seek(0, os.SEEK_END)
            if self._handle.tell() == 0:
                self._handle.write(b"0")
                self._handle.flush()
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError) as exc:
            self._handle.close()
            raise RuntimeError("该数据库已有 CLI/WebUI 实例运行，请先停止原实例") from exc
        # Keep ownership until interpreter exit, after ThreadPoolExecutor workers
        # have drained. Releasing it when the HTTP loop exits would be too early.
        atexit.register(self.close)

    def assert_current(self):
        if self._handle.closed or self.pid != os.getpid():
            raise RuntimeError("启动恢复需要当前进程持有数据库运行锁")
        if self.database_path != db._active_sqlite_path().expanduser().resolve():
            raise RuntimeError("运行锁与当前数据库不匹配")

    def close(self):
        if self._handle.closed:
            return
        if self.pid == os.getpid():
            if self.mail_worker is not None:
                self.mail_worker.stop()
                self.mail_worker = None
            if os.name == "nt":
                import msvcrt
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        atexit.unregister(self.close)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def acquire_runtime_owner() -> DatabaseOwner:
    return DatabaseOwner(db._active_sqlite_path())


def _recover_registration_jobs() -> int:
    """Mark orphaned local execution as retryable, without replaying side effects."""
    db._ensure_sqlite()
    now = db._now()
    message = "服务重启导致任务中断，请重试；已生成账号的任务会优先补跑授权"
    with db._LOCK, closing(db._sqlite_conn()) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute("""
            UPDATE registration_jobs
               SET status='failed', updated_at=?,
                   payload=json_set(payload, '$.status', 'failed', '$.stage', '已中断',
                                    '$.error_message', ?, '$.progress_message', ?,
                                    '$.completed_at', ?, '$.updated_at', ?)
             WHERE status IN ('pending', 'running', 'paused', 'stopping')
        """, (now, message, message, now, now))
        count = cursor.rowcount
        conn.execute("""
            UPDATE accounts SET updated_at=?,
                payload=json_set(payload, '$.codex_status', 'failed',
                    '$.codex_error', '服务重启导致授权中断，请补跑 Codex', '$.updated_at', ?)
            WHERE json_extract(payload, '$.codex_status')='retrying'
        """, (now, now))
        from core import task_center_store
        task_center_store.recover_interrupted_codex_retries(conn)
    return count


def recover_startup(owner: DatabaseOwner) -> dict[str, int]:
    """Run once, after acquiring exclusive database ownership and before serving."""
    owner.assert_current()
    if owner.recovered:
        return {}
    from core import plus_activation_store
    recoveries = {
        "registration": _recover_registration_jobs,
        "plus_activation": plus_activation_store.recover_interrupted,
        "plan_check": db.recover_interrupted_plan_checks,
        "quota_check": db.recover_interrupted_quota_checks,
        "extract_link": db.recover_interrupted_extract_links,
        "live_check": db.recover_interrupted_live_checks,
        "codex_agent": db.recover_interrupted_codex_agents,
        "totp_setup": db.recover_interrupted_totp_setups,
        "email_change": db.recover_interrupted_email_changes,
    }
    results = {name: recover() for name, recover in recoveries.items()}
    from core.mail_notifications import NotificationWorker
    owner.mail_worker = NotificationWorker(owner)
    owner.recovered = True
    for name, count in results.items():
        if count:
            logger.warning("已恢复 %s 个上次进程遗留任务：%s", count, name)
    return results
