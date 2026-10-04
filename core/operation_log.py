# -*- coding: utf-8 -*-
"""提交支付与提链的完整操作日志。

设计要点：

- 每个账号一份日志文件，落在 ``db._LOG_DIR``：``scan-payment-<id>.log`` 与
  ``extract-link-<id>.log``；命名与查活、2FA、换绑邮箱保持一致，任务中心通过
  ``core.task_center_store.read_task_log`` 直接读取。
- 记录逐步动作、目标地址、请求头、请求体、响应状态、响应体、耗时、重试与异常，
  而不是只留一行状态摘要。
- 按用户要求，凭据（支付 CDK / OrderHub API Key / 账号 AT / 提链 CDK / 租户会话
  token / 订单密钥）默认**明文**写入日志文件，便于直接核对与回放；可用
  ``PAYMENT_LOG_CREDENTIALS`` / ``EXTRACT_LOG_CREDENTIALS`` 或 ``.env`` 切回脱敏。
- 单行长度与文件体积都有上限，超出后截断或轮转，避免批次日志无限膨胀。
- 客户端通过 :func:`current` 取当前线程/协程绑定的日志，不需要层层传参。
"""
from __future__ import annotations

import contextvars
import json
import re
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from core import db

# None 表示跟随进程绑定的数据库日志目录（测试可覆盖）。
_LOG_DIR: Path | None = None

PAYMENT = "scan_payment"
EXTRACT = "extract_link"

_FILE_PREFIX = {PAYMENT: "scan-payment", EXTRACT: "extract-link"}
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SECRET_KEY = re.compile(r"cdk|token|secret|password|passwd|authorization|cookie|credential|api[_-]?key|session|(^|_)at($|_)", re.I)
_CURRENT: contextvars.ContextVar = contextvars.ContextVar("turb_operation_log", default=None)
_WRITE_LOCK = threading.RLock()
_MASKED = "[凭据已脱敏]"


def log_dir() -> Path:
    path = _LOG_DIR or db._LOG_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def log_path(kind: str, account_id) -> Path:
    prefix = _FILE_PREFIX.get(str(kind))
    if prefix is None:
        raise ValueError("不支持的操作日志类型")
    return log_dir() / f"{prefix}-{int(account_id)}.log"


def payment_log_path(account_id) -> Path:
    return log_path(PAYMENT, account_id)


def extract_log_path(account_id) -> Path:
    return log_path(EXTRACT, account_id)


def settings(kind: str) -> dict:
    """读取开关；每次调用都取当前配置，便于 .env 热更新与测试替换。"""
    if str(kind) == EXTRACT:
        from config import extract_link as cfg
        names = ("EXTRACT_LOG_ENABLED", "EXTRACT_LOG_CREDENTIALS",
                 "EXTRACT_LOG_VALUE_LIMIT", "EXTRACT_LOG_MAX_BYTES")
    else:
        from config import scan_api as cfg
        names = ("PAYMENT_LOG_ENABLED", "PAYMENT_LOG_CREDENTIALS",
                 "PAYMENT_LOG_VALUE_LIMIT", "PAYMENT_LOG_MAX_BYTES")
    enabled, credentials, limit, max_bytes = (getattr(cfg, name, None) for name in names)
    return {
        "enabled": True if enabled is None else bool(enabled),
        "credentials": True if credentials is None else bool(credentials),
        "value_limit": max(200, int(limit if limit is not None else 8000)),
        "max_bytes": max(4096, int(max_bytes if max_bytes is not None else 5_000_000)),
    }


def enabled(kind: str) -> bool:
    return bool(settings(kind)["enabled"])


def _mask(value, secrets=()) -> str:
    text = str(value)
    for secret in sorted({s for s in secrets if isinstance(s, str) and s}, key=len, reverse=True):
        text = text.replace(secret, _MASKED)
    return text


def _render(value, *, limit: int, secrets=(), credentials: bool) -> str:
    """把任意字段渲染成单行文本；默认保留明文凭据。"""
    if value is None:
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)
    if not credentials and secrets:
        text = _mask(text, secrets)
    text = text.replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t")
    text = _CONTROL.sub(" ", text)
    if len(text) > limit:
        text = f"{text[:limit]}…[已截断，原文 {len(text)} 字符]"
    return text


class OperationLog:
    """单文件顺序写入；同一进程内多线程共享同一文件时保证整行原子。"""

    def __init__(self, path: Path, *, kind: str = PAYMENT, account_id=None, enable=None,
                 credentials=None, value_limit=None, max_bytes=None, truncate=False, secrets=()):
        settings_map = settings(kind)
        self.kind = kind
        self.account_id = int(account_id) if account_id is not None else None
        self.path = Path(path)
        self.enabled = settings_map["enabled"] if enable is None else bool(enable)
        self.credentials = settings_map["credentials"] if credentials is None else bool(credentials)
        self.value_limit = settings_map["value_limit"] if value_limit is None else max(200, int(value_limit))
        self.max_bytes = settings_map["max_bytes"] if max_bytes is None else max(4096, int(max_bytes))
        self.secrets = tuple(item for item in secrets if isinstance(item, str) and item)
        self._truncate = bool(truncate)
        self._handle = None
        self._size = 0
        self._closed = False

    # ------------------------------------------------------------ 文件管理
    def _open(self) -> None:
        if self._handle is not None or self._closed:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        mode = "w" if self._truncate else "a"
        self._truncate = False
        self._handle = self.path.open(mode, encoding="utf-8")
        try:
            self._size = self.path.stat().st_size if mode == "a" else 0
        except OSError:
            self._size = 0

    def _rotate(self, incoming: int) -> None:
        self.close()
        backup = self.path.with_name(self.path.name + ".1")
        try:
            if backup.exists():
                backup.unlink()
            self.path.replace(backup)
        except OSError:
            pass
        self._closed = False
        self._handle = None
        self._size = 0

    def _write(self, text: str) -> None:
        if not self.enabled or self._closed:
            return
        payload = text + "\n"
        raw = payload.encode("utf-8")
        with _WRITE_LOCK:
            self._open()
            if self._handle is None:
                return
            if self._size and self._size + len(raw) > self.max_bytes:
                self._rotate(len(raw))
                self._open()
            try:
                self._handle.write(payload)
                self._handle.flush()
            except (OSError, ValueError):
                return
            self._size += len(raw)

    def close(self) -> None:
        with _WRITE_LOCK:
            handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass

    # ---------------------------------------------------------------- 写入
    # level/event 只按位置传入：调用方会用 event= 记录 SSE 事件名等业务字段，
    # 若这两个参数可按关键字传入，就会与 **fields 冲突而抛 TypeError。
    def line(self, level: str, event: str, /, *, secrets=(), **fields) -> None:
        if not self.enabled:
            return
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        known = tuple({*self.secrets, *(s for s in secrets if isinstance(s, str) and s)})
        parts = [f"[{stamp}]", f"[{level}]", str(event)]
        for key, value in fields.items():
            rendered = _render(value, limit=self.value_limit // 2, secrets=known,
                               credentials=self.credentials)
            if rendered == "":
                continue
            if not self.credentials and _SECRET_KEY.search(str(key)):
                rendered = _MASKED
            parts.append(f"{key}={rendered}")
        self._write(" | ".join(parts))

    def step(self, message: str, /, **fields) -> None:
        self.line("STEP", message, **fields)

    def add_secrets(self, *values) -> None:
        """运行中补充需要脱敏的值（仅在凭据开关关闭时生效）。"""
        self.secrets = tuple(dict.fromkeys(
            (*self.secrets, *(item for item in values if isinstance(item, str) and item))))

    def detail(self, message: str, /, **fields) -> None:
        self.line("INFO", message, **fields)

    def request(self, *, method, url, headers=None, body=None, params=None, timeout=None,
                cookies=None, note=None, secrets=()) -> None:
        self.line("HTTP →", note or "发送请求", method=method, url=url,
                  params=_render_params(params), headers=headers, cookies=cookies, body=body,
                  timeout=timeout, secrets=secrets)

    def response(self, *, status, headers=None, body=None, elapsed=None, note=None, secrets=()) -> None:
        self.line("HTTP ←", note or "收到响应", status=status, elapsed=_seconds(elapsed),
                  headers=headers, body=body, secrets=secrets)

    def failure(self, exc, *, note=None, secrets=(), **fields) -> None:
        # 与 step/detail/line 一致地接受附加字段；提链等调用会带回
        # reason、job_id、data、logs 等上下文，缺失时这些调用会抛 TypeError。
        payload = {
            "error": f"{type(exc).__name__}: {exc}",
            "status": getattr(exc, "status", None),
            "code": getattr(exc, "code", None),
            "uncertain": getattr(exc, "uncertain", None),
        }
        payload.update(fields)
        self.line("ERROR", note or "操作异常", secrets=secrets, **payload)

    # 便捷别名：调用方读起来更接近业务语义。
    def info(self, message: str, /, **fields) -> None:
        self.detail(message, **fields)

    def warn(self, message: str, /, **fields) -> None:
        self.line("WARN", message, **fields)


def _seconds(value):
    try:
        return f"{float(value):.3f}s"
    except (TypeError, ValueError):
        return value


def _render_params(params):
    if not params:
        return None
    if isinstance(params, dict):
        return "&".join(f"{key}={value}" for key, value in params.items())
    return params


class FanoutLog:
    """批次任务：同一次上游交互写进该批次涉及的每个账号日志。"""

    def __init__(self, logs):
        self.logs = list(logs)

    def line(self, level: str, event: str, /, **fields) -> None:
        for log in self.logs:
            log.line(level, event, **fields)

    def step(self, message: str, /, **fields) -> None:
        self.line("STEP", message, **fields)

    def add_secrets(self, *values) -> None:
        for log in self.logs:
            log.add_secrets(*values)

    def detail(self, message: str, /, **fields) -> None:
        self.line("INFO", message, **fields)

    def info(self, message: str, /, **fields) -> None:
        self.detail(message, **fields)

    def warn(self, message: str, /, **fields) -> None:
        self.line("WARN", message, **fields)

    def request(self, **fields) -> None:
        for log in self.logs:
            log.request(**fields)

    def response(self, **fields) -> None:
        for log in self.logs:
            log.response(**fields)

    def failure(self, exc, **fields) -> None:
        for log in self.logs:
            log.failure(exc, **fields)

    def close(self) -> None:
        for log in self.logs:
            log.close()


def start(kind: str, account_id, *, header=None, truncate=False, secrets=(), **fields) -> OperationLog:
    """创建一份日志并写入首行；默认追加（truncate=True 才清空原文件）。"""
    log = OperationLog(log_path(kind, account_id), kind=kind, account_id=account_id,
                       truncate=truncate, secrets=secrets)
    if not log.enabled:
        return log
    log.line("INFO", header or "开始记录操作日志", account_id=account_id, log_file=str(log.path), **fields)
    return log


def start_many(kind: str, account_ids, *, header=None, truncate=False, secrets=(), **fields) -> FanoutLog:
    logs = [OperationLog(log_path(kind, account_id), kind=kind, account_id=account_id,
                         truncate=truncate, secrets=secrets)
            for account_id in dict.fromkeys(int(value) for value in account_ids)]
    fanout = FanoutLog(logs)
    if any(log.enabled for log in logs):
        fanout.line("INFO", header or "开始记录批次操作日志", account_ids=sorted(log.account_id for log in logs),
                    log_files=[str(log.path) for log in logs], **fields)
    return fanout


def current():
    """当前绑定的日志对象；未绑定时返回 None，所有便捷函数都会变成空操作。"""
    return _CURRENT.get()


@contextmanager
def use(log):
    token = _CURRENT.set(log)
    try:
        yield log
    finally:
        _CURRENT.reset(token)
        try:
            log.close()
        except Exception:  # pragma: no cover - 关闭日志失败不应影响业务
            pass


@contextmanager
def operation(kind: str, account_id, *, header=None, truncate=False, secrets=(), **fields):
    with use(start(kind, account_id, header=header, truncate=truncate, secrets=secrets, **fields)) as log:
        yield log


@contextmanager
def batch_operation(kind: str, account_ids, *, header=None, truncate=False, secrets=(), **fields):
    with use(start_many(kind, account_ids, header=header, truncate=truncate, secrets=secrets, **fields)) as log:
        yield log


# ------------------------------------------------------------------ 便捷函数
def step(message: str, /, **fields) -> None:
    log = _CURRENT.get()
    if log is not None:
        log.step(message, **fields)


def detail(message: str, /, **fields) -> None:
    log = _CURRENT.get()
    if log is not None:
        log.detail(message, **fields)


def request(**fields) -> None:
    log = _CURRENT.get()
    if log is not None:
        log.request(**fields)


def response(**fields) -> None:
    log = _CURRENT.get()
    if log is not None:
        log.response(**fields)


def failure(exc, **fields) -> None:
    log = _CURRENT.get()
    if log is not None:
        log.failure(exc, **fields)


def read(kind: str, account_id) -> str:
    """读取日志文本；不存在时返回空字符串。"""
    try:
        path = log_path(kind, account_id)
    except (TypeError, ValueError):
        return ""
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def clear(kind: str, account_id) -> bool:
    try:
        path = log_path(kind, account_id)
    except (TypeError, ValueError):
        return False
    try:
        if path.exists():
            path.unlink()
        return True
    except OSError:
        return False


def masked(value, secrets=()) -> str:
    """给不方便绑定日志的调用方复用的脱敏渲染。"""
    return _mask(value, secrets)
