# -*- coding: utf-8 -*-
"""额度 / 「银行重置」券查询后台队列。

与套餐查询（plan_check_service）保持同一套调度约定：独立的线程池、队列上限、
请求启动间隔和取消/暂停控制；网络策略仍复用 PLAN_CHECK_PROXY_* 配置，
因为两者访问的都是同一批 chatgpt.com 只读接口。
"""
from __future__ import annotations

import logging
import random
import threading
import time
from datetime import datetime

from config import proxy as proxy_cfg
from core import db, task_control
from core.chatgpt_quota import check_account_quota

logger = logging.getLogger(__name__)

KIND = "quota_check"
_MAX_WORKERS = task_control.MAX_WORKERS


def _int_setting(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(getattr(proxy_cfg, name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


def _float_setting(name: str, default: float, lower: float, upper: float) -> float:
    try:
        value = float(getattr(proxy_cfg, name, default) or 0.0)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


_WORKERS = _int_setting("QUOTA_CHECK_WORKERS", 3, 1, _MAX_WORKERS)
_QUEUE_LIMIT = _int_setting("QUOTA_CHECK_QUEUE_LIMIT", 500, _WORKERS, 5000)
_EXECUTOR = task_control.register_pool(KIND, _WORKERS, max_workers=_MAX_WORKERS)
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_RATE_LOCK = threading.Lock()
_NEXT_REQUEST_AT = 0.0


def _wait_for_rate_slot() -> None:
    """为所有查询线程分配错开的请求启动时间。"""
    global _NEXT_REQUEST_AT
    min_interval = _float_setting("QUOTA_CHECK_MIN_INTERVAL", 1.0, 0.0, 30.0)
    jitter = _float_setting("QUOTA_CHECK_JITTER", 0.8, 0.0, 30.0)
    with _RATE_LOCK:
        now = time.monotonic()
        scheduled = max(now, _NEXT_REQUEST_AT) + (random.uniform(0.0, jitter) if jitter else 0.0)
        _NEXT_REQUEST_AT = scheduled + min_interval
    wait_seconds = scheduled - now
    if wait_seconds > 0:
        time.sleep(wait_seconds)


def _run_quota_check(
    *,
    account_id: int,
    email: str,
    access_token: str,
    trigger: str,
    proxy: str | None,
    timezone_offset_min: str,
) -> dict:
    try:
        task_control.checkpoint(KIND, account_id)
        if not db.mark_account_quota_check_running(account_id):
            return {"ok": False, "error": "账号已删除或额度查询状态已被重置"}

        _wait_for_rate_slot()
        task_control.checkpoint(KIND, account_id)
        result = check_account_quota(
            access_token,
            proxy=proxy,
            timezone_offset_min=timezone_offset_min,
        )
        db.update_account_quota(acc_id=account_id, result=result)
        if result.get("ok"):
            logger.info(
                "[Quota] 后台查询成功: %s, balance=%s, resets=%s, trigger=%s",
                email,
                result.get("quota_balance") or "unknown",
                result.get("reset_credits_available"),
                trigger,
            )
        else:
            logger.warning(
                "[Quota] 后台查询失败: %s, trigger=%s, error=%s",
                email,
                trigger,
                result.get("error") or "未知错误",
            )
        return result
    except task_control.TaskCancelled:
        result = {
            "ok": False,
            "status": "cancelled",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": "用户取消额度查询",
        }
        try:
            db.update_account_quota(acc_id=account_id, result=result)
        except Exception:
            logger.exception("[Quota] 写入取消状态失败: account_id=%s", account_id)
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"{type(exc).__name__}: {str(exc)[:180]}",
        }
        try:
            db.update_account_quota(acc_id=account_id, result=result)
        except Exception:
            logger.exception("[Quota] 写入后台查询异常状态失败: account_id=%s", account_id)
        logger.exception("[Quota] 后台查询异常: %s", email)
        return result
    finally:
        _QUEUE_SLOTS.release()
        task_control.release(KIND, account_id)


def _cancel_pending_quota_check(account_id: int) -> None:
    """排队中的额度查询被取消：释放队列槽位并写回取消状态。"""
    try:
        _QUEUE_SLOTS.release()
    except ValueError:
        pass
    try:
        db.update_account_quota(acc_id=int(account_id), result={
            "ok": False,
            "status": "cancelled",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": "用户取消额度查询",
        })
    except Exception:
        logger.exception("[Quota] 写入取消状态失败: account_id=%s", account_id)
    task_control.release(KIND, account_id)


def enqueue_account_quota_check(
    *,
    account_id: int,
    email: str,
    access_token: str,
    trigger: str,
    proxy: str | None = None,
    timezone_offset_min: str = "-",
) -> dict:
    """把额度查询放入统一线程池；重复查询或队列满时不提交。"""
    account_id = int(account_id)
    email = str(email or "").strip()
    access_token = str(access_token or "").strip()
    if not access_token:
        return {"accepted": False, "busy": False, "error": "账号缺少 access_token"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "queue_full": True, "error": "额度查询队列已满，请稍后重试"}

    if not db.claim_account_quota_check(acc_id=account_id, trigger=trigger):
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": True, "error": "该账号正在查询额度"}

    try:
        accepted = _EXECUTOR.submit(
            task_control.gated(
                _run_quota_check,
                task_control.control(KIND, account_id),
                on_cancel=lambda: _cancel_pending_quota_check(account_id),
            ),
            account_id=account_id,
            email=email,
            access_token=access_token,
            trigger=str(trigger or "manual"),
            proxy=proxy,
            timezone_offset_min=str(timezone_offset_min or "-"),
        )
        if accepted is False:
            raise RuntimeError("额度查询队列已关闭")
    except Exception as exc:
        task_control.release(KIND, account_id)
        _QUEUE_SLOTS.release()
        result = {
            "ok": False,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"额度查询入队失败: {type(exc).__name__}: {str(exc)[:160]}",
        }
        db.update_account_quota(acc_id=account_id, result=result)
        return {"accepted": False, "busy": False, "error": result["error"]}

    return {
        "accepted": True,
        "busy": False,
        "account_id": account_id,
        "email": email,
        "status": "queued",
        "trigger": str(trigger or "manual"),
    }


def apply_settings() -> dict:
    """热加载额度查询并发数并立即对运行中的批次生效。"""
    global _WORKERS, _QUEUE_LIMIT
    _WORKERS = _int_setting("QUOTA_CHECK_WORKERS", 3, 1, _MAX_WORKERS)
    _QUEUE_LIMIT = _int_setting("QUOTA_CHECK_QUEUE_LIMIT", 500, _WORKERS, 5000)
    _EXECUTOR.set_workers(_WORKERS)
    return queue_settings()


def queue_settings() -> dict:
    return {
        "workers": _WORKERS,
        "queue_limit": _QUEUE_LIMIT,
        "min_interval": _float_setting("QUOTA_CHECK_MIN_INTERVAL", 1.0, 0.0, 30.0),
        "jitter": _float_setting("QUOTA_CHECK_JITTER", 0.8, 0.0, 30.0),
    }
