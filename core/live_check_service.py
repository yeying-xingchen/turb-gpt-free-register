# -*- coding: utf-8 -*-
"""账号查活后台队列：协议 BrowserSession 指纹环境 + 独立日志。"""
from __future__ import annotations

import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit
from pathlib import Path

from core import db
from core.account_liveness import check_account_liveness, log_path
from core.chatgpt_plan import _mask_proxy, open_plan_check_proxy, resolve_plan_check_route
from core.openai_auth import detect_account_unusable_text


_NETWORK_FAILURE_MARKERS = (
    "403", "408", "425", "429", "500", "502", "503", "504",
    "proxy", "socks", "timeout", "timed out", "connection", "closed",
    "reset", "熔断冷却", "network",
)
_EDGE_ERROR_CODES = {"edge_http", "challenge_required", "network"}
_DEAD_ERROR_CODES = {"account_deactivated", "account_deleted", "account_banned"}


def _live_check_driver() -> str:
    """读取查活驱动；旧配置缺失时保持协议优先并可自动兜底。"""
    try:
        from config import playwright as playwright_cfg
        value = str(getattr(playwright_cfg, "LIVE_CHECK_DRIVER", "auto") or "auto").strip().lower()
    except Exception:
        value = "auto"
    aliases = {
        "pw": "playwright",
        "local": "playwright",
        "local_playwright": "playwright",
        "local-playwright": "playwright",
        "browser": "playwright",
        # Cloudflare/CF 不是独立的登录协议；这里将显式的 CF 别名映射到
        # 真浏览器路线，让 Chromium 负责执行页面上的 JS challenge。
        "cf": "playwright",
        "cloudflare": "playwright",
        "cloudflare_browser": "playwright",
        "api": "protocol",
    }
    value = aliases.get(value, value)
    if value not in {"auto", "protocol", "playwright"}:
        logger.warning("[查活] 未知 LIVE_CHECK_DRIVER=%r，回退 auto", value)
        return "auto"
    return value


def _is_network_failure(result: dict | None) -> bool:
    """仅把结构化边缘/传输失败交给浏览器兜底，业务错误不切驱动。"""
    if not isinstance(result, dict) or result.get("ok") or result.get("status") != "failed":
        return False
    code = str(result.get("error_code") or "").strip().lower()
    if code in _DEAD_ERROR_CODES:
        return False
    text = str(result.get("error") or "")
    if detect_account_unusable_text(text):
        return False
    # Protocol challenge/edge results are eligible for the explicitly bounded
    # direct/browser handoff in auto mode. A browser result is not sent back
    # through this predicate by _run_live_check. Once a non-edge code is
    # present, preserve it as a business/auth result even if its HTTP status is
    # 403; only unclassified status-only failures use the fallback below.
    if code:
        return code in _EDGE_ERROR_CODES
    try:
        status = int(result.get("http_status") or 0)
    except (TypeError, ValueError):
        status = 0
    if status in {403, 408, 425, 429} or status >= 500:
        return True
    if code:
        return False
    return any(marker in text.lower() for marker in _NETWORK_FAILURE_MARKERS)


def _run_playwright_live_check(email: str, proxy: str | None, email_source: str | None) -> dict:
    """将浏览器驱动结果转换为统一查活结果；导入保持惰性，避免 Playwright 影响协议启动。"""
    from core.playwright_auth import check_playwright_liveness
    return check_playwright_liveness(email, proxy=proxy, email_source=email_source)


logger = logging.getLogger(__name__)

_WORKERS = 3
_QUEUE_LIMIT = 500
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="live-check")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_RUNNING: set[int] = set()
_LOCK = threading.Lock()


def _safe_live_error(value: object, limit: int = 500) -> str:
    """查活日志/DB 错误只保留脱敏 URL 和短文本，避免泄漏 token/OTP。"""
    text = str(value or "")
    def _url(match):
        raw = match.group(0)
        try:
            parsed = urlsplit(raw)
            netloc = parsed.netloc.rsplit("@", 1)[-1]
            keys = [part.split("=", 1)[0] + "=<redacted>" for part in parsed.query.split("&") if part]
            return urlunsplit((parsed.scheme, netloc, parsed.path, "&".join(keys), ""))
        except Exception:
            return raw.split("?", 1)[0]
    text = re.sub(r"https?://[^\s'\"<>]+", _url, text, flags=re.IGNORECASE)
    text = re.sub(
        r"(?i)(['\"]?(?:access[_-]?token|refresh[_-]?token|csrf(?:token)?|password|secret|otp|code)['\"]?\s*[:=]\s*['\"]?)[^,}\s'\"]+",
        r"\1<redacted>",
        text,
    )
    text = re.sub(r"[\r\n\x00-\x1f\x7f]", " ", text)
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


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
        route = resolve_plan_check_route(explicit_proxy=proxy)
        selected_proxy = route.get("proxy")
        from config import proxy as proxy_cfg
        timeout = float(getattr(proxy_cfg, "PLAN_CHECK_TIMEOUT", 15.0) or 15.0)
        effective_proxy, relay = open_plan_check_proxy(
            route, selected_proxy, timeout=timeout,
        )
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
        driver = _live_check_driver()
        _append_log(email, f"[查活] driver={driver}")
        if driver == "playwright":
            _append_log(email, "[查活] 直接使用本地 Playwright 浏览器登录（独立 context）")
            result = _run_playwright_live_check(email, effective_proxy, email_source)
        else:
            # 每个网络路由尝试拥有自己的任务级身份状态；同一路由的完整认证链及
            # 内部重试复用同一组 device/session 标识，不同账号绝不共享。
            fingerprint_state: dict = {}
            result = check_account_liveness(
                email,
                proxy=effective_proxy,
                clear_log=False,
                email_source=email_source,
                fingerprint_state=fingerprint_state,
            )
            # 认证链早期边缘/传输错误通常不是账号死亡。auto 模式下先用独立
            # 直连协议会话，再在仍为网络错误时切换全新 Chromium context。
            if (
                driver == "auto"
                and _is_network_failure(result)
                and selected_proxy
                and str(route.get("network_route") or "") in {"proxy", "proxy_chain"}
            ):
                _append_log(
                    email,
                    "[查活] 代理路线遇到边缘/网络错误，先启动独立直连协议会话兜底（不复用代理画像/Cookie/会话ID）",
                )
                result = check_account_liveness(
                    email,
                    proxy="",
                    clear_log=False,
                    email_source=email_source,
                    fingerprint_state={},
                )

            if driver == "auto" and _is_network_failure(result):
                _append_log(
                    email,
                    "[查活] 协议路线仍失败，切换独立 Playwright context（不复用 Cookie、画像、session ID）",
                )
                browser_result = _run_playwright_live_check(email, "", email_source)
                if browser_result.get("ok") or browser_result.get("status") == "deactivated":
                    result = browser_result
                else:
                    protocol_error = result.get("error")
                    result = dict(browser_result)
                    if protocol_error:
                        result["protocol_error"] = _safe_live_error(protocol_error)

        # 路由元数据要在写库前补齐，失败/成功结果都可供审计和前端轮询使用。
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
            _append_log(email, f"[查活] 完成：账号已废 {_safe_live_error(result.get('error'))}")
        else:
            _append_log(email, f"[查活] 完成：失败 {_safe_live_error(result.get('error'))}")
        return result
    except Exception as exc:
        result = {
            "ok": False,
            "status": "failed",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": f"{type(exc).__name__}: {_safe_live_error(exc)}",
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

    try:
        _append_log(email, f"[查活] 已入队 account_id={account_id} trigger={trigger}", clear=True)
    except Exception as exc:
        _QUEUE_SLOTS.release()
        result = {"ok": False, "status": "failed", "checked_at": datetime.now().isoformat(timespec="seconds"), "error": f"查活日志初始化失败: {type(exc).__name__}"}
        try:
            db.update_account_liveness(account_id, result)
        except Exception:
            logger.exception("[查活] 回滚日志失败后的状态失败: account_id=%s", account_id)
        return {"accepted": False, "busy": False, "error": result["error"]}
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
