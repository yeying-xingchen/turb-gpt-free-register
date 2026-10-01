# -*- coding: utf-8 -*-
"""账号查活后台队列：协议 BrowserSession 指纹环境 + 独立日志。"""
from __future__ import annotations

import logging
import random
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from core import db
from core.account_liveness import check_account_liveness, log_path
from core.chatgpt_plan import _mask_proxy, open_plan_check_proxy, resolve_plan_check_route

logger = logging.getLogger(__name__)

_WORKERS = 3
_QUEUE_LIMIT = 500
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="live-check")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_RUNNING: set[int] = set()
_LOCK = threading.Lock()
_NETWORK_RETRY_HINTS = (
    "403", "429", "502", "503", "504", "proxy", "socks", "timeout",
    "timed out", "connection", "closed", "reset",
)


def _configured_live_check_proxies(proxy_cfg) -> list[str]:
    """查活优先使用套餐专用代理池，未配置时使用注册代理池。"""
    values = list(getattr(proxy_cfg, "PLAN_CHECK_PROXY", []) or [])
    if not values:
        values = list(getattr(proxy_cfg, "PROXY_POOL", []) or [])
    return [str(value).strip() for value in values if str(value).strip()]


def _live_proxy_route(proxy_url: str, proxy_cfg) -> dict:
    route = resolve_plan_check_route(explicit_proxy=proxy_url)
    upstream = str(getattr(proxy_cfg, "PLAN_CHECK_UPSTREAM_PROXY", "") or "").strip()
    if upstream:
        route["upstream_proxy"] = upstream
        route["upstream_proxy_used"] = _mask_proxy(upstream) or None
        route["network_route"] = "proxy_chain"
    route["proxy_mode"] = "proxy_pool"
    return route


def _retryable_live_check_result(result: dict) -> bool:
    if result.get("ok") or result.get("status") == "deactivated":
        return False
    text = str(result.get("error") or "").lower()
    return any(hint in text for hint in _NETWORK_RETRY_HINTS)


def is_checking(email: str) -> bool:
    acc = db.get_account_by_email(email)
    if not acc:
        return False
    return str(acc.get("live_check_status") or "") in {"queued", "running"}


def _append_log(email: str, line: str, *, clear: bool = False) -> None:
    p = log_path(email)
    p.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H:%M:%S")
    mode = "w" if clear else "a"
    with p.open(mode, encoding="utf-8") as f:
        f.write(f"{stamp} [INFO] {line}\n")


def _run_live_check(*, account_id: int, email: str, proxy: str | None, trigger: str) -> dict:
    relay = None
    try:
        with _LOCK:
            _RUNNING.add(int(account_id))
        if not db.mark_account_live_check_running(account_id):
            _append_log(email, "[查活] 账号已删除或查活状态已被重置，取消执行")
            return {"ok": False, "status": "failed", "error": "账号已删除或查活状态已被重置"}
        from config import proxy as proxy_cfg
        candidates = _configured_live_check_proxies(proxy_cfg)
        route = resolve_plan_check_route(explicit_proxy=proxy)
        # 查活规则独立于套餐查询的 direct/auto 选择：只要任一代理池有数据，
        # 就禁止直连，并从池中随机选择一个出口。
        if candidates and not str(route.get("proxy") or "").strip():
            route = _live_proxy_route(random.choice(candidates), proxy_cfg)
        selected_proxy = str(route.get("proxy") or "").strip()
        timeout = float(getattr(proxy_cfg, "PLAN_CHECK_TIMEOUT", 15.0) or 15.0)
        # 查活必须沿用账号注册时记录的邮箱来源。不能只调用
        # resolve_email_source(email)：Remail 等临时邮箱的上下文只在领取进程
        # 内存中存在，服务重启后按当前 EMAIL_SOURCE 推断会把来源判错。
        try:
            account = db.get_account(account_id) or {}
        except Exception:
            account = {}
        email_source = str(account.get("email_source") or "").strip() or None
        if email_source:
            _append_log(email, f"[查活] 使用注册时保存的邮箱来源：{email_source}")
        _append_log(
            email,
            "[查活] 开始后台执行 "
            f"trigger={trigger} network_route={route.get('network_route')} "
            f"proxy_mode={route.get('proxy_mode')} proxy_used={route.get('proxy_used') or '-'} "
            f"fallback_reason={route.get('proxy_fallback_reason') or '-'}"
        )
        # 每个代理出口使用独立 BrowserSession/指纹/Cookie。网络类失败后换池中
        # 尚未使用的代理；池非空时绝不直连。
        max_routes = max(1, int(getattr(proxy_cfg, "PLAN_CHECK_MAX_ATTEMPTS", 3) or 3))
        used_proxies: set[str] = set()
        result: dict = {"ok": False, "status": "failed", "error": "查活未执行"}
        for route_attempt in range(1, max_routes + 1):
            selected_proxy = str(route.get("proxy") or "").strip()
            if selected_proxy:
                used_proxies.add(selected_proxy)
            effective_proxy, relay = open_plan_check_proxy(
                route, selected_proxy, timeout=timeout,
            )
            _append_log(
                email,
                f"[查活] 网络出口尝试 {route_attempt}/{max_routes}："
                f"{_mask_proxy(selected_proxy) or 'direct'}",
            )
            result = check_account_liveness(
                email,
                proxy=effective_proxy,
                clear_log=False,
                email_source=email_source,
                fingerprint_state={"force_fresh": route_attempt > 1},
            )
            if relay is not None:
                relay.close()
                relay = None
            if not _retryable_live_check_result(result):
                break

            remaining = [item for item in candidates if item not in used_proxies]
            if remaining and route_attempt < max_routes:
                next_proxy = random.choice(remaining)
                _append_log(
                    email,
                    "[查活] 当前代理发生网络/403错误，放弃该会话并随机切换下一代理："
                    f"{_mask_proxy(next_proxy)}",
                )
                route = _live_proxy_route(next_proxy, proxy_cfg)
                continue

            if candidates:
                break

            # 没有任何代理池数据时，保留原先的一次直连兜底行为。
            if selected_proxy and route_attempt < max_routes:
                _append_log(
                    email,
                    "[查活] 未配置可替换代理，启动独立直连会话兜底一次",
                )
                route = resolve_plan_check_route(explicit_proxy="")
                continue
            break
        result.update({
            "network_route": route.get("network_route"),
            "proxy_used": _mask_proxy(selected_proxy) or None,
            "upstream_proxy_used": route.get("upstream_proxy_used"),
            "proxy_mode": route.get("proxy_mode"),
            "proxy_fallback_reason": route.get("proxy_fallback_reason"),
        })
        db.update_account_liveness(account_id, result)
        if result.get("ok"):
            _append_log(email, "[查活] 完成：账号正常，已刷新最新 AT/accessToken")
        elif result.get("status") == "deactivated":
            _append_log(email, f"[查活] 完成：账号已废 {result.get('error') or ''}")
        else:
            _append_log(email, f"[查活] 完成：失败 {result.get('error') or ''}")
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "status": "failed",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"{type(exc).__name__}: {str(exc)[:500]}",
        }
        try:
            db.update_account_liveness(account_id, result)
        except Exception:
            logger.exception("[查活] 写入异常状态失败: account_id=%s", account_id)
        logger.exception("[查活] 后台异常: %s", email)
        try:
            _append_log(email, f"[查活] 后台异常：{result['error']}")
        except Exception:
            pass
        return result
    finally:
        if relay is not None:
            relay.close()
        with _LOCK:
            _RUNNING.discard(int(account_id))
        _QUEUE_SLOTS.release()


def enqueue_account_live_check(*, account_id: int, email: str, trigger: str = "manual", proxy: str | None = None) -> dict:
    account_id = int(account_id)
    email = str(email or "").strip()
    if not email:
        return {"accepted": False, "busy": False, "error": "email 为空"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "queue_full": True, "error": "查活队列已满，请稍后重试"}
    if not db.claim_account_live_check(acc_id=account_id, trigger=trigger):
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": True, "error": "该账号正在查活"}

    _append_log(email, f"[查活] 已入队 account_id={account_id} trigger={trigger}", clear=True)
    try:
        _EXECUTOR.submit(
            _run_live_check,
            account_id=account_id,
            email=email,
            proxy=proxy,
            trigger=str(trigger or "manual"),
        )
    except Exception as exc:
        _QUEUE_SLOTS.release()
        result = {
            "ok": False,
            "status": "failed",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"查活入队失败: {type(exc).__name__}: {str(exc)[:160]}",
        }
        db.update_account_liveness(account_id, result)
        _append_log(email, result["error"])
        return {"accepted": False, "busy": False, "error": result["error"]}

    return {
        "accepted": True,
        "busy": False,
        "account_id": account_id,
        "email": email,
        "status": "queued",
        "trigger": str(trigger or "manual"),
    }


def queue_settings() -> dict:
    return {"workers": _WORKERS, "queue_limit": _QUEUE_LIMIT}
