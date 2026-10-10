"""Transactional administrator notifications with a retryable SMTP outbox."""
from __future__ import annotations

from contextlib import closing
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from html import escape
import json
import logging
import re
import smtplib
import ssl
import threading
import time
import uuid

from config import notifications as settings
from core import db

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 5


def _enabled(category: str) -> bool:
    return bool(settings.SMTP_ENABLED and {
        "task": settings.SMTP_NOTIFY_TASKS,
        "upload": settings.SMTP_NOTIFY_UPLOADS,
    }.get(category, False))


def _initialize(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS mail_notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_key TEXT NOT NULL UNIQUE, category TEXT NOT NULL,
        payload TEXT NOT NULL, message_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        next_attempt REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
        sent_at TEXT, last_error TEXT NOT NULL DEFAULT ''
    )""")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_mail_notifications_pending
        ON mail_notifications(status, next_attempt, id)""")


def enqueue_notification(conn, *, event_key: str, category: str, title: str,
                         fields: dict, emails: list[str]) -> None:
    """Persist public summaries in the caller's transaction; never send here."""
    if not _enabled(category):
        return
    try:
        # Optional notification failures must not lose the account/job change.
        if not conn.in_transaction:
            conn.execute("BEGIN")
        with_savepoint = False
        try:
            conn.execute("SAVEPOINT mail_notification")
            with_savepoint = True
            _initialize(conn)
            payload = json.dumps({"title": title, "fields": fields,
                                  "emails": list(dict.fromkeys(emails))}, ensure_ascii=False)
            conn.execute("""INSERT OR IGNORE INTO mail_notifications
                (event_key,category,payload,message_id,created_at) VALUES(?,?,?,?,?)""",
                (event_key, category, payload, make_msgid(), db._now()))
            conn.execute("RELEASE mail_notification")
        except Exception:
            if with_savepoint:
                conn.execute("ROLLBACK TO mail_notification")
                conn.execute("RELEASE mail_notification")
            raise
    except Exception as exc:
        logger.warning("邮件通知入队失败（%s）", type(exc).__name__)


def notify_account_upload(*, emails: list[str], inserted: int, skipped: int,
                          source: str) -> None:
    """One message per successful import, containing only newly inserted emails."""
    if inserted <= 0 or not emails or not _enabled("upload"):
        return
    try:
        db._ensure_sqlite()
        with db._LOCK, closing(db._sqlite_conn()) as conn, conn:
            enqueue_notification(
                conn, event_key="upload:" + uuid.uuid4().hex, category="upload",
                title="收到账号上传",
                fields={"上传来源": source, "成功导入": str(inserted),
                        "跳过行数": str(skipped), "上传时间": db._now()}, emails=emails,
            )
    except Exception as exc:
        logger.warning("上传邮件通知入队失败（%s）", type(exc).__name__)


def _addresses(value: str) -> list[str]:
    addresses = list(dict.fromkeys(item.strip() for item in re.split(r"[,;\s]+", value) if item.strip()))
    if not addresses or any(not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+", item) for item in addresses):
        raise ValueError("管理员邮箱格式无效")
    return addresses


def build_message(payload: dict, *, sender: str, recipients: list[str], message_id: str) -> EmailMessage:
    """Render escaped HTML with inline styles and a plain-text alternative."""
    title = str(payload["title"])
    fields = payload.get("fields", {})
    emails = payload.get("emails", [])
    text = title + "\n\n" + "\n".join(f"{key}：{value}" for key, value in fields.items())
    text += "\n\n账号邮箱：\n" + ("\n".join(emails) or "未提供")
    rows = "".join(
        '<tr><th style="text-align:left;padding:10px;border-bottom:1px solid #e2e8f0;color:#64748b">'
        + escape(str(key)) + '</th><td style="padding:10px;border-bottom:1px solid #e2e8f0">'
        + escape(str(value)) + "</td></tr>" for key, value in fields.items()
    )
    accounts = "".join('<li style="padding:4px 0;overflow-wrap:anywhere">' + escape(str(email)) + "</li>"
                       for email in emails) or "<li>未提供</li>"
    html = ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"></head>'
            '<body style="margin:0;padding:24px;background:#f1f5f9;font-family:Arial,sans-serif;color:#0f172a">'
            '<div style="max-width:680px;margin:auto;padding:28px;background:#fff;border-radius:12px">'
            '<h1 style="font-size:22px;margin-top:0">' + escape(title) + '</h1>'
            '<table style="width:100%;border-collapse:collapse;font-size:14px">' + rows + '</table>'
            '<h2 style="font-size:16px;margin-top:24px">账号邮箱</h2><ul style="padding-left:22px">'
            + accounts + '</ul><p style="font-size:12px;color:#64748b;margin-top:28px">'
            '系统自动通知 · 请在管理后台查看完整结果</p></div></body></html>')
    message = EmailMessage()
    message["Subject"] = "[账号管理] " + title
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = message_id
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    return message


def send_notification(payload: dict, message_id: str) -> None:
    host = settings.SMTP_HOST.strip()
    sender = (settings.SMTP_FROM or settings.SMTP_USERNAME).strip()
    recipients = _addresses(payload.get("_remaining_recipients") or settings.SMTP_ADMIN_EMAILS)
    if not host or len(_addresses(sender)) != 1:
        raise ValueError("SMTP 主机或发件人未配置")
    port, timeout = int(settings.SMTP_PORT), int(settings.SMTP_TIMEOUT)
    if not 1 <= port <= 65535 or not 1 <= timeout <= 120:
        raise ValueError("SMTP 端口或超时无效")
    security = settings.SMTP_SECURITY
    if security not in {"ssl", "starttls", "plain"}:
        raise ValueError("SMTP 加密方式无效")
    message = build_message(payload, sender=sender, recipients=recipients, message_id=message_id)
    context = ssl.create_default_context()
    options = {"host": host, "port": port, "timeout": timeout}
    factory = smtplib.SMTP_SSL if security == "ssl" else smtplib.SMTP
    if security == "ssl":
        options["context"] = context
    with factory(**options) as smtp:
        if security == "starttls":
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
        if settings.SMTP_USERNAME:
            smtp.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
        refused = smtp.send_message(message, from_addr=sender, to_addrs=recipients)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)


def process_once() -> bool:
    """Deliver one committed event. The runtime owner guarantees a single worker."""
    categories = [category for category in ("task", "upload") if _enabled(category)]
    if not categories:
        return False
    db._ensure_sqlite()
    with closing(db._sqlite_conn()) as conn, conn:
        _initialize(conn)
        placeholders = ",".join("?" for _ in categories)
        row = conn.execute(f"""SELECT * FROM mail_notifications
            WHERE status='pending' AND next_attempt<=? AND category IN ({placeholders})
            ORDER BY id LIMIT 1""", (time.time(), *categories)).fetchone()
    if row is None:
        return False
    attempts = row["attempts"] + 1
    payload = json.loads(row["payload"])
    try:
        # No database locks are held while communicating with SMTP.
        send_notification(payload, row["message_id"])
    except Exception as exc:
        status = "failed" if attempts >= MAX_ATTEMPTS else "pending"
        error = type(exc).__name__
        if isinstance(exc, smtplib.SMTPRecipientsRefused) and exc.recipients:
            payload["_remaining_recipients"] = ",".join(exc.recipients)
        with closing(db._sqlite_conn()) as conn, conn:
            conn.execute("""UPDATE mail_notifications SET attempts=?,status=?,next_attempt=?,last_error=?,payload=?
                WHERE id=?""", (attempts, status, time.time() + min(3600, 30 * 2 ** (attempts - 1)), error,
                               json.dumps(payload, ensure_ascii=False), row["id"]))
        logger.warning("邮件通知 #%s 发送失败（%s，第 %s/%s 次）", row["id"], error, attempts, MAX_ATTEMPTS)
    else:
        with closing(db._sqlite_conn()) as conn, conn:
            conn.execute("""UPDATE mail_notifications SET status='sent',attempts=?,sent_at=?,
                payload='{}',last_error='' WHERE id=?""", (attempts, db._now(), row["id"]))
    return True


class NotificationWorker:
    def __init__(self, owner):
        self.owner = owner
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, name="smtp-notifications", daemon=True)
        self.thread.start()

    def _run(self):
        while not self.stop_event.is_set():
            try:
                self.owner.assert_current()
                worked = process_once()
            except Exception as exc:
                logger.warning("邮件通知队列暂不可用（%s）", type(exc).__name__)
                worked = False
            self.stop_event.wait(0.1 if worked else 2)

    def stop(self):
        self.stop_event.set()
        self.thread.join()
