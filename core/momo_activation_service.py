# -*- coding: utf-8 -*-
"""后台 MoMo 一键开通服务。

该模块只对接文档公开的 MoMo API v1：

* ``POST /api/v1/momo/checkout`` 创建自动支付任务；
* ``GET /api/v1/jobs/{job_id}`` 查询任务；
* 超时后尽力调用 ``POST /api/v1/jobs/{job_id}/cancel``。

远端返回的 ``job_secret`` 只存在于当前 worker 的内存中，不写入账号记录、日志、
HTTP 响应或前端。账号 access token 也只从调用方传入到请求体，不会被结果摘要保存。
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

try:
    import requests
except Exception:  # pragma: no cover - requests is a declared dependency
    requests = None

from config import momo_activation as cfg
from core import db

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = {"succeeded", "done", "failed", "cancelled", "canceled"}
_SUCCESS_STATUSES = {"succeeded", "success", "successful", "activated", "completed", "complete"}
_FAILURE_STATUSES = {"failed", "failure", "error", "cancelled", "canceled", "rejected", "ineligible"}


def _int_setting(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(getattr(cfg, name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


def _float_setting(name: str, default: float, lower: float, upper: float) -> float:
    try:
        value = float(getattr(cfg, name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


def _text(value: Any, limit: int = 500) -> str:
    text = str(value or "").strip()
    # Do not allow accidental credential-like strings into UI errors/logs.
    text = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[redacted]", text)
    text = re.sub(r"(?i)(job[_-]?secret|access[_-]?token|session|cdk)\s*[:=]\s*[^,;\s]+", r"\1=[redacted]", text)
    return text[:limit]


def _validated_base() -> str:
    raw = str(getattr(cfg, "MOMO_ACTIVATION_API_BASE", "") or "").strip().rstrip("/")
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("MOMO_ACTIVATION_API_BASE 必须是 http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("MOMO_ACTIVATION_API_BASE 不得包含用户名、密码、query 或 fragment")
    return raw


def _proxy_values() -> list[str]:
    configured = getattr(cfg, "MOMO_ACTIVATION_ENTRY_PROXIES", None)
    if not configured:
        try:
            from config import proxy as proxy_cfg
            configured = getattr(proxy_cfg, "PROXY_POOL", [])
        except Exception:
            configured = []
    if isinstance(configured, str):
        configured = configured.splitlines()
    result: list[str] = []
    try:
        from config.proxy import normalize_proxy_url
    except Exception:
        normalize_proxy_url = None
    for raw in configured or []:
        value = str(raw or "").strip()
        if not value:
            continue
        try:
            value = normalize_proxy_url(value) if normalize_proxy_url else value
        except Exception as exc:
            logger.warning("[MoMo] 跳过无效入口代理: %s", _text(exc, 160))
            continue
        if value and value not in result:
            result.append(value)
    return result[:500]


def _safe_summary(value: Any, *, depth: int = 0) -> dict[str, Any]:
    """Extract a bounded, non-secret summary from the remote job response."""
    allowed = {
        "status", "remote_status", "message", "ok", "success", "activated",
        "plan_type", "current_plan_type", "expires_at", "plan_expires_at",
        "activation_status", "activation_id", "job_id", "provider", "channel",
        "country", "currency", "amount", "currency_code", "checked_at",
        "completed_at", "error_code", "percent", "text", "progress",
    }
    blocked = {
        "token", "access_token", "accesstoken", "refresh_token", "id_token",
        "secret", "job_secret", "authorization", "cookie", "cookies", "headers",
        "raw", "payload", "body", "request", "response", "session", "credential",
        "credentials", "password", "passwd", "email", "account_email",
        "checkout_session_id", "client_secret", "api_key", "apikey", "proxy",
    }
    if depth > 3:
        return {}
    if not isinstance(value, dict):
        return {}
    output: dict[str, Any] = {}
    for key, raw in value.items():
        key_text = str(key)
        normalized = key_text.lower().replace("-", "_")
        if normalized in blocked or normalized not in allowed:
            continue
        if isinstance(raw, dict):
            cleaned = _safe_summary(raw, depth=depth + 1)
            if cleaned:
                output[key_text] = cleaned
        elif isinstance(raw, list):
            # Lists in a job result are not needed for activation; keep only short scalar progress data.
            values = [str(item)[:160] for item in raw[:20] if isinstance(item, (str, int, float, bool))]
            if values:
                output[key_text] = values
        elif isinstance(raw, (str, int, float, bool)) or raw is None:
            output[key_text] = _text(raw, 500) if isinstance(raw, str) else raw
    return output


def _response_payload(response: Any) -> dict[str, Any]:
    try:
        payload = response.json()
    except Exception:
        try:
            payload = json.loads((response.text or "{}").strip() or "{}")
        except Exception:
            payload = {}
    return payload if isinstance(payload, dict) else {}


def _request_json(method: str, path: str, *, payload: dict[str, Any] | None = None, headers: dict[str, str] | None = None, timeout: float = 30.0) -> tuple[int, dict[str, Any]]:
    base = _validated_base()
    url = f"{base}/{str(path or '').lstrip('/')}"
    safe_headers = {"Accept": "application/json"}
    if payload is not None:
        safe_headers["Content-Type"] = "application/json"
    if headers:
        safe_headers.update(headers)
    if requests is not None:
        response = requests.request(method.upper(), url, json=payload, headers=safe_headers, timeout=timeout)
        return int(response.status_code), _response_payload(response)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = Request(url, data=body, headers=safe_headers, method=method.upper())
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            try:
                data = json.loads(raw or "{}")
            except Exception:
                data = {}
            return int(getattr(response, "status", 200)), data if isinstance(data, dict) else {}
    except Exception as exc:
        status = getattr(exc, "code", None)
        if status is None:
            raise
        body = getattr(exc, "read", lambda: b"{}")()
        try:
            data = json.loads(body.decode("utf-8", "replace") or "{}")
        except Exception:
            data = {}
        return int(status), data if isinstance(data, dict) else {}


def _remote_error(status: int, payload: dict[str, Any]) -> str:
    error = payload.get("error") or payload.get("message") or payload.get("code")
    if isinstance(error, dict):
        error = error.get("message") or error.get("code")
    return _text(error or f"MoMo API HTTP {status}", 300)


def create_checkout(*, access_token: str, trial_days: int | None = None) -> dict[str, Any]:
    """Create one remote checkout task; returns only the in-memory job secret."""
    if not str(access_token or "").strip():
        raise ValueError("access_token 为空")
    if not bool(getattr(cfg, "MOMO_ACTIVATION_ENABLED", True)):
        raise ValueError("MOMO_ACTIVATION_ENABLED=False")
    auto_pay = bool(getattr(cfg, "MOMO_ACTIVATION_AUTO_PAY", True))
    payment_cdk = str(getattr(cfg, "MOMO_ACTIVATION_PAYMENT_CDK", "") or "").strip()
    if auto_pay and not payment_cdk:
        raise ValueError("请先配置 MOMO_ACTIVATION_PAYMENT_CDK")
    timeout = _int_setting("MOMO_ACTIVATION_TIMEOUT", 25, 8, 120)
    days = _int_setting("MOMO_ACTIVATION_TRIAL_DAYS", 30, 1, 90)
    if trial_days is not None:
        try:
            days = max(1, min(90, int(trial_days)))
        except (TypeError, ValueError):
            raise ValueError("trial_days 必须是 1-90 的整数")
    body: dict[str, Any] = {
        "session": str(access_token).strip(),
        "timeout": timeout,
        "trial_days": days,
        "retry_count": 5,
        "auto_pay": auto_pay,
        "strategy": "custom_promo",
        "pre_proxy": "off",
    }
    if auto_pay:
        body["auto_payment_cdk"] = payment_cdk
    proxies = _proxy_values()
    if proxies:
        body["entry_proxies"] = proxies
    status, response = _request_json("POST", "/api/v1/momo/checkout", payload=body, timeout=float(timeout))
    if status < 200 or status >= 300:
        raise RuntimeError(_remote_error(status, response))
    job_id = str(response.get("job_id") or "").strip()
    job_secret = str(response.get("job_secret") or "").strip()
    if not job_id or not job_secret:
        raise RuntimeError("MoMo API 未返回完整任务凭证")
    return {
        "job_id": job_id,
        "job_secret": job_secret,
        "status": str(response.get("status") or "queued").strip().lower() or "queued",
        "queue_position": response.get("queue_position"),
    }


def get_job(job_id: str, job_secret: str) -> dict[str, Any]:
    if not str(job_id or "").strip() or not str(job_secret or "").strip():
        raise ValueError("MoMo 任务凭证为空")
    status, response = _request_json(
        "GET",
        f"/api/v1/jobs/{str(job_id).strip()}",
        headers={"X-Momo-Job-Secret": str(job_secret).strip()},
        timeout=float(_int_setting("MOMO_ACTIVATION_TIMEOUT", 25, 8, 120)),
    )
    if status < 200 or status >= 300:
        raise RuntimeError(_remote_error(status, response))
    job = response.get("job") if isinstance(response.get("job"), dict) else response
    return job if isinstance(job, dict) else {}


def cancel_job(job_id: str, job_secret: str) -> bool:
    try:
        status, response = _request_json(
            "POST",
            f"/api/v1/jobs/{str(job_id).strip()}/cancel",
            payload={},
            headers={"X-Momo-Job-Secret": str(job_secret).strip()},
            timeout=float(_int_setting("MOMO_ACTIVATION_TIMEOUT", 25, 8, 120)),
        )
        return 200 <= status < 300 and str(response.get("status") or "").lower() not in {"failed", "error"}
    except Exception:
        logger.warning("[MoMo] 任务超时取消失败: job_id=%s", _text(job_id, 120))
        return False


def _job_status(job: dict[str, Any]) -> str:
    return str(job.get("status") or job.get("state") or job.get("phase") or "running").strip().lower()


def _job_result(job: dict[str, Any]) -> dict[str, Any]:
    result = job.get("result")
    return result if isinstance(result, dict) else {}


def _job_succeeded(status: str, job: dict[str, Any]) -> bool:
    if status in {"succeeded", "success", "successful", "activated"}:
        return True
    if status != "done":
        return False
    result = _job_result(job)
    result_status = str(result.get("status") or result.get("state") or result.get("activation_status") or "").strip().lower()
    plan = str(result.get("plan_type") or result.get("current_plan_type") or "").strip().lower()
    if bool(result.get("activated")) or bool(result.get("success")) or result_status in _SUCCESS_STATUSES:
        return True
    return any(item in plan for item in ("plus", "pro", "team", "go")) and result_status not in _FAILURE_STATUSES


def _poll_until_terminal(job_id: str, job_secret: str) -> dict[str, Any]:
    deadline = time.monotonic() + _int_setting("MOMO_ACTIVATION_MAX_WAIT", 900, 30, 3600)
    interval = _float_setting("MOMO_ACTIVATION_POLL_INTERVAL", 2.0, 0.2, 30.0)
    last_job: dict[str, Any] = {}
    while True:
        last_job = get_job(job_id, job_secret)
        status = _job_status(last_job)
        if status in _TERMINAL_STATUSES:
            summary = _safe_summary(last_job)
            result = _job_result(last_job)
            ok = _job_succeeded(status, last_job)
            message = _text(last_job.get("text") or last_job.get("message") or result.get("message") or ("MoMo Plus 开通成功" if ok else "MoMo 任务未确认开通"), 300)
            error = "" if ok else _text(last_job.get("error") or result.get("error") or ("任务已结束但未确认 Plus 开通" if status == "done" else message), 300)
            return {
                "ok": ok,
                "status": "success" if ok else "failed",
                "remote_status": status,
                "job_id": job_id,
                "message": message,
                "error": error,
                "result_summary": summary,
                "checked_at": datetime.now().isoformat(timespec="seconds"),
            }
        if time.monotonic() >= deadline:
            cancel_job(job_id, job_secret)
            return {
                "ok": False,
                "status": "failed",
                "remote_status": status,
                "job_id": job_id,
                "message": "MoMo 任务等待超时，已尝试取消远端任务",
                "error": "远端任务等待超时",
                "result_summary": _safe_summary(last_job),
                "checked_at": datetime.now().isoformat(timespec="seconds"),
            }
        time.sleep(interval)


def _persist(account_id: int, result: dict[str, Any], nonce: str | None = None) -> None:
    updater = getattr(db, "update_account_activation", None)
    if callable(updater):
        try:
            updater(account_id, result, nonce=nonce)
        except Exception:
            logger.exception("[MoMo] 写入账号开通状态失败: account_id=%s", account_id)


def _run_activation(*, account_id: int, email: str, access_token: str, trigger: str, nonce: str) -> dict[str, Any]:
    try:
        marker = getattr(db, "mark_account_activation_running", None)
        if callable(marker) and not marker(account_id, nonce=nonce):
            return {"ok": False, "status": "failed", "error": "账号开通状态已被重置"}
        created = create_checkout(access_token=access_token)
        job_id = created["job_id"]
        # The secret stays local to this worker and is never persisted.
        _persist(account_id, {
            "ok": False, "status": "running", "job_id": job_id,
            "remote_status": created.get("status") or "queued",
            "message": "MoMo 任务已创建，等待远端完成",
        }, nonce=nonce)
        result = _poll_until_terminal(job_id, created["job_secret"])
        result["trigger"] = trigger
        _persist(account_id, result, nonce=nonce)
        if result.get("ok"):
            logger.info("[MoMo] Plus 开通成功: account_id=%s", account_id)
        else:
            logger.warning("[MoMo] Plus 开通失败: account_id=%s error=%s", account_id, _text(result.get("error"), 200))
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "status": "failed",
            "error": _text(exc, 300),
            "message": "MoMo 开通任务失败",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
        }
        _persist(account_id, result, nonce=nonce)
        logger.warning("[MoMo] 开通异常: account_id=%s error=%s", account_id, result["error"])
        return result
    finally:
        _QUEUE_SLOTS.release()


_WORKERS = _int_setting("MOMO_ACTIVATION_WORKERS", 2, 1, 16)
_QUEUE_LIMIT = _int_setting("MOMO_ACTIVATION_QUEUE_LIMIT", 100, _WORKERS, 5000)
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="momo-activation")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)


def queue_settings() -> dict[str, Any]:
    return {
        "workers": _WORKERS,
        "queue_limit": _QUEUE_LIMIT,
        "enabled": bool(getattr(cfg, "MOMO_ACTIVATION_ENABLED", True)),
        "auto_pay": bool(getattr(cfg, "MOMO_ACTIVATION_AUTO_PAY", True)),
        "api_base": str(getattr(cfg, "MOMO_ACTIVATION_API_BASE", "") or "").strip(),
    }


def health_check() -> dict[str, Any]:
    status, payload = _request_json("GET", "/api/v1/health", timeout=10)
    return {"ok": 200 <= status < 300, "status_code": status, **_safe_summary(payload)}


def enqueue_account_activation(*, account_id: int, email: str, access_token: str, trigger: str = "manual", trial_days: int | None = None) -> dict[str, Any]:
    if not bool(getattr(cfg, "MOMO_ACTIVATION_ENABLED", True)):
        return {"accepted": False, "busy": False, "error": "MoMo 一键开通未启用"}
    if bool(getattr(cfg, "MOMO_ACTIVATION_AUTO_PAY", True)) and not str(getattr(cfg, "MOMO_ACTIVATION_PAYMENT_CDK", "") or "").strip():
        return {"accepted": False, "busy": False, "error": "请先配置 MOMO_ACTIVATION_PAYMENT_CDK"}
    if not str(access_token or "").strip():
        return {"accepted": False, "busy": False, "error": "账号缺少 access_token"}
    try:
        account_id = int(account_id)
    except (TypeError, ValueError):
        return {"accepted": False, "busy": False, "error": "account_id 非法"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "queue_full": True, "error": "MoMo 开通队列已满，请稍后重试"}
    claimer = getattr(db, "claim_account_activation", None)
    nonce = None
    try:
        if callable(claimer):
            nonce = claimer(account_id, trigger=str(trigger or "manual"))
            if not nonce:
                _QUEUE_SLOTS.release()
                return {"accepted": False, "busy": True, "error": "该账号正在开通或任务仍未过期"}
        else:
            nonce = f"local-{time.time_ns()}"
        try:
            future = _EXECUTOR.submit(
                _run_activation,
                account_id=account_id,
                email=str(email or ""),
                access_token=str(access_token or "").strip(),
                trigger=str(trigger or "manual"),
                nonce=str(nonce),
            )
        except Exception as exc:
            _QUEUE_SLOTS.release()
            _persist(account_id, {"ok": False, "status": "failed", "error": f"MoMo 开通入队失败: {_text(exc, 160)}"}, nonce=str(nonce))
            return {"accepted": False, "busy": False, "error": "MoMo 开通入队失败"}
        return {
            "accepted": True,
            "busy": False,
            "account_id": account_id,
            "email": str(email or ""),
            "status": "queued",
            "trigger": str(trigger or "manual"),
            "queue": queue_settings(),
            "future": future,
        }
    except Exception:
        _QUEUE_SLOTS.release()
        raise
