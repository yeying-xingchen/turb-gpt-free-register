# -*- coding: utf-8 -*-
"""账号查活后台队列：协议或 CloakBrowser 重新登录 + 独立日志。"""
from __future__ import annotations

import logging
import base64
import uuid
from urllib.parse import urlparse
import math
import random
import threading
import time
from datetime import datetime
from pathlib import Path
from contextlib import contextmanager

from core.manual_verification import bind_callbacks

from core import db, task_control
from core.account_liveness import _failure_result, check_account_liveness, log_path
from core.openai_auth import AccountUnusableError, detect_account_unusable_text
from core.chatgpt_plan import _mask_proxy, open_plan_check_proxy, resolve_plan_check_route

logger = logging.getLogger(__name__)

KIND = "live_check"
_MAX_WORKERS = task_control.MAX_WORKERS


def _int_setting(name: str, default: int, lower: int, upper: int) -> int:
    from config import live_check as live_cfg
    try:
        value = int(getattr(live_cfg, name, default) or default)
    except (TypeError, ValueError, OverflowError):
        value = default
    return max(lower, min(upper, value))


_WORKERS = _int_setting("LIVE_CHECK_WORKERS", 3, 1, _MAX_WORKERS)
_QUEUE_LIMIT = 500
_EXECUTOR = task_control.register_pool(KIND, _WORKERS, max_workers=_MAX_WORKERS)
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)
_RUNNING: set[int] = set()
_WAITING: dict[int, dict] = {}
_FOCUS_REQUESTS: set[int] = set()
_REMOTE_VERIFICATIONS: dict[int, dict] = {}
_LOCK = threading.Lock()
_NETWORK_RETRY_HINTS = (
    "403", "408", "425", "429", "500", "502", "503", "504", "proxy", "socks",
    "timeout", "timed out", "connection", "closed", "reset", "ssl", "tls",
    "temporarily unavailable",
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
    if detect_account_unusable_text(text) or detect_account_unusable_text(str(result.get("error_code") or "")):
        return False
    retryable = result.get("retryable")
    if isinstance(retryable, bool):
        return retryable
    # 兼容旧调用方：HTTP 业务错误不能因错误文案包含 proxy 等词而重试。
    try:
        status = int(result.get("http_status") or 0)
    except (TypeError, ValueError, OverflowError):
        status = 0
    if status:
        return status in {403, 408, 425, 429} or 500 <= status <= 599
    return any(hint in text for hint in _NETWORK_RETRY_HINTS)


def _live_retry_settings(proxy_cfg) -> tuple[int, float]:
    """读取独立的查活重试设置；次数包含首次执行。"""
    try:
        attempts = int(getattr(proxy_cfg, "LIVE_CHECK_MAX_ATTEMPTS", 3))
    except (TypeError, ValueError, OverflowError):
        attempts = 3
    try:
        delay = float(getattr(proxy_cfg, "LIVE_CHECK_RETRY_DELAY", 2.0))
    except (TypeError, ValueError, OverflowError):
        delay = 2.0
    if not math.isfinite(delay):
        delay = 2.0
    return max(1, min(5, attempts)), max(0.0, min(60.0, delay))


@contextmanager
def _manual_wait(account_id: int, email: str):
    from core import task_center_store
    handle = task_control.control(KIND, account_id)
    with _EXECUTOR.park_current():
        with _LOCK:
            _WAITING[account_id] = {"account_id": account_id, "email": email,
                                    "waiting_since": datetime.now().isoformat(timespec="seconds")}
            _REMOTE_VERIFICATIONS[account_id] = {
                "session": uuid.uuid4().hex, "counter": 0, "frame": None,
                "commands": [], "requested_at": None, "captured_at": None,
            }
        owned_pause = handle.pause()
        try:
            task_center_store.set_task_control_state(KIND, account_id, "paused")
            _append_log(email, "[查活] 等待人工验证：保留当前浏览器会话、Cookie 和出口；在任务中心完成验证，其它任务继续执行")
            yield
        finally:
            with _LOCK:
                _WAITING.pop(account_id, None)
                _REMOTE_VERIFICATIONS.pop(account_id, None)
                _FOCUS_REQUESTS.discard(account_id)
            if owned_pause:
                handle.resume()
            if not handle.cancelled:
                task_center_store.set_task_control_state(KIND, account_id, "running")
    _append_log(email, "[查活] 人工验证已结束，继续同一浏览器会话")


def list_manual_verifications() -> list[dict]:
    with _LOCK:
        return [dict(item) for item in _WAITING.values()]


def request_verification_window(account_id: int) -> dict:
    account_id = int(account_id)
    with _LOCK:
        if account_id not in _WAITING:
            return {"ok": False, "status": 409, "error": "该任务当前没有等待人工验证的窗口"}
        _FOCUS_REQUESTS.add(account_id)
    return {"ok": True, "message": "已请求显示运行程序桌面上的验证窗口，请手动完成验证"}


def manual_verification_frame(account_id: int) -> dict:
    with _LOCK:
        remote = _REMOTE_VERIFICATIONS.get(int(account_id))
        if remote is None:
            return {"ok": False, "status": 409, "error": "验证已结束或该任务不在等待人工验证"}
        remote["requested_at"] = time.monotonic()
        frame = remote["frame"]
        if frame is None:
            return {"ok": True, "pending": True, "status": 202}
        return {"ok": True, "pending": False, "frame": dict(frame)}


def request_verification_input(account_id: int, data: dict) -> dict:
    if not isinstance(data, dict):
        return {"ok": False, "status": 400, "error": "输入必须是 JSON 对象"}
    with _LOCK:
        remote = _REMOTE_VERIFICATIONS.get(int(account_id))
        if remote is None:
            return {"ok": False, "status": 409, "error": "验证已结束或该任务不在等待人工验证"}
        frame = remote["frame"]
        if frame is None or data.get("revision") != frame["revision"]:
            return {"ok": False, "status": 409, "error": "画面已更新，请在最新画面上点击"}
        x, y = data.get("x"), data.get("y")
        if (type(x) not in (int, float) or type(y) not in (int, float)
                or not math.isfinite(x) or not math.isfinite(y)
                or not 0 <= x < frame["width"] or not 0 <= y < frame["height"]):
            return {"ok": False, "status": 400, "error": "点击坐标无效或超出画面范围"}
        if remote["commands"]:
            return {"ok": False, "status": 429, "error": "上一次点击正在处理，请稍后再试"}
        remote["commands"].append({"x": x, "y": y, "revision": frame["revision"]})
        remote["requested_at"] = time.monotonic()
    return {"ok": True, "message": "已提交你的手动点击，正在等待验证页面响应"}


def _pump_manual_browser(account_id: int, page) -> None:
    """Run only on the browser owner thread, only while the challenge is present."""
    _manual_checkpoint(account_id)
    if urlparse(str(page.url)).hostname not in {"chatgpt.com", "auth.openai.com"}:
        return
    now = time.monotonic()
    with _LOCK:
        remote = _REMOTE_VERIFICATIONS.get(account_id)
        if remote is None:
            return
        commands = list(remote["commands"])
        remote["commands"].clear()
        requested = remote["requested_at"]
        captured = remote["captured_at"]
        frame = remote["frame"]
    challenge_detector = r"""() => /just a moment/i.test(document.title) ||
        [...document.querySelectorAll('iframe')].some(el =>
          el.getClientRects().length && /challenges\.cloudflare\.com/.test(el.src))"""
    if page.evaluate(challenge_detector) is not True:
        with _LOCK:
            if _REMOTE_VERIFICATIONS.get(account_id) is remote:
                remote["frame"] = None
        return
    for command in commands:
        if frame and command["revision"] == frame["revision"]:
            page.mouse.click(command["x"], command["y"])
    challenge_visible = page.evaluate(challenge_detector)
    if challenge_visible is not True:
        with _LOCK:
            if _REMOTE_VERIFICATIONS.get(account_id) is remote:
                remote["frame"] = None
        return
    if requested is None or now - requested > 5:
        return
    if not commands and captured is not None and now - captured < 1:
        return
    size = page.viewport_size
    if not isinstance(size, dict):
        size = page.evaluate("() => ({width: innerWidth, height: innerHeight})")
    if not isinstance(size, dict) or not all(type(size.get(k)) is int and 0 < size[k] <= 4096 for k in ("width", "height")):
        return
    try:
        image = page.screenshot(type="jpeg", quality=65, scale="css", timeout=2000)
    except Exception:
        # A transient screenshot failure does not restart authentication.
        return
    if not isinstance(image, bytes) or len(image) > 2_000_000:
        return
    with _LOCK:
        if _REMOTE_VERIFICATIONS.get(account_id) is not remote:
            return
        remote["counter"] += 1
        remote["captured_at"] = now
        remote["frame"] = {
            "image": base64.b64encode(image).decode("ascii"),
            "width": size["width"], "height": size["height"],
            "revision": f'{remote["session"]}:{remote["counter"]}',
        }


def _manual_checkpoint(account_id: int):
    handle = task_control.get_control(KIND, account_id)
    if handle is not None and handle.cancelled:
        raise task_control.TaskCancelled("用户取消等待人工验证的查活任务")
    with _LOCK:
        focus = account_id in _FOCUS_REQUESTS
        _FOCUS_REQUESTS.discard(account_id)
    return focus


def is_checking(email: str) -> bool:
    acc = db.get_account_by_email(email)
    if not acc:
        return False
    return str(acc.get("live_check_status") or "") in {"queued", "running", "challenge_waiting"}


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
        task_control.checkpoint(KIND, account_id)
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
        # 整链重试与阶段内重试分开计数。每轮使用独立会话，池非空时绝不直连。
        max_attempts, retry_delay = _live_retry_settings(proxy_cfg)
        used_proxies: set[str] = set()
        result: dict = {"ok": False, "status": "failed", "error": "查活未执行"}
        attempt = 0
        while attempt < max_attempts:
            attempt += 1
            task_control.checkpoint(KIND, account_id)
            selected_proxy = str(route.get("proxy") or "").strip()
            if selected_proxy:
                used_proxies.add(selected_proxy)
            _append_log(
                email,
                f"[查活] 完整登录尝试 {attempt}/{max_attempts}："
                f"{_mask_proxy(selected_proxy) or 'direct'}",
            )
            try:
                effective_proxy, relay = open_plan_check_proxy(
                    route, selected_proxy, timeout=timeout,
                )
                with bind_callbacks(lambda: _manual_wait(account_id, email),
                                    lambda: _manual_checkpoint(account_id),
                                    lambda page: _pump_manual_browser(account_id, page)):
                    result = dict(check_account_liveness(
                        email,
                        proxy=effective_proxy,
                        clear_log=False,
                        email_source=email_source,
                        fingerprint_state={"force_fresh": attempt > 1},
                    ))
            except task_control.TaskCancelled:
                raise
            except Exception as exc:
                dead_code = (
                    getattr(exc, "error_code", "") if isinstance(exc, AccountUnusableError)
                    else detect_account_unusable_text(str(exc))
                )
                if isinstance(exc, AccountUnusableError) or dead_code:
                    result = {
                        "ok": False, "status": "deactivated", "error": dead_code or "account_deactivated",
                        "checked_at": datetime.now().isoformat(timespec="seconds"),
                    }
                else:
                    result = _failure_result(exc, datetime.now().isoformat(timespec="seconds"))
            finally:
                if relay is not None:
                    current_relay, relay = relay, None
                    try:
                        current_relay.close()
                    except Exception:
                        logger.warning("[查活] 关闭代理中继失败", exc_info=True)
            result.update({"attempts": attempt, "max_attempts": max_attempts})
            if not _retryable_live_check_result(result):
                break
            if attempt >= max_attempts:
                _append_log(email, f"[查活] 自动重试次数已用尽（{attempt}/{max_attempts}）")
                break

            remaining = [item for item in candidates if item not in used_proxies]
            if remaining:
                next_proxy = random.choice(remaining)
                route = _live_proxy_route(next_proxy, proxy_cfg)
                retry_action = f"切换代理 {_mask_proxy(next_proxy)}"
            elif candidates:
                retry_action = "当前代理池已无未用出口，使用新会话重试当前代理"
            elif selected_proxy:
                # 未配置代理池时保留原有直连兜底；之后可继续在直连出口有限重试。
                route = resolve_plan_check_route(explicit_proxy="")
                retry_action = "使用新会话直连兜底"
            else:
                retry_action = "使用新会话重试直连"
            delay = min(60.0, retry_delay * (2 ** (attempt - 1)))
            _append_log(
                email,
                f"[查活] 第 {attempt} 次失败：{str(result.get('error') or '临时认证失败')[:300]}；"
                f"{delay:g}s 后进行第 {attempt + 1}/{max_attempts} 次尝试，{retry_action}",
            )
            if delay > 0:
                task_control.sleep(KIND, account_id, delay)
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
    except task_control.TaskCancelled:
        result = {"ok": False, "status": "cancelled", "checked_at": datetime.now().isoformat(timespec="seconds"), "error": "用户取消查活任务"}
        try:
            db.update_account_liveness(account_id, result)
            _append_log(email, "[查活] 已取消：用户手动取消")
        except Exception:
            logger.exception("[查活] 写入取消状态失败: account_id=%s", account_id)
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
        task_control.release(KIND, account_id)


def _cancel_pending_live_check(account_id: int) -> None:
    """排队中的查活任务被取消：释放队列槽位并写回取消状态。"""
    with _LOCK:
        _RUNNING.discard(int(account_id))
    try:
        _QUEUE_SLOTS.release()
    except ValueError:
        pass
    _release_pending_live_check(account_id)


def _release_pending_live_check(account_id: int) -> None:
    try:
        db.update_account_liveness(int(account_id), {
            "ok": False,
            "status": "cancelled",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": "用户取消查活任务",
        })
    except Exception:
        logger.exception("[查活] 写入取消状态失败: account_id=%s", account_id)
    task_control.release(KIND, account_id)


def enqueue_account_live_check(*, account_id: int, email: str, trigger: str = "manual", proxy: str | None = None) -> dict:
    account_id = int(account_id)
    email = str(email or "").strip()
    if not email:
        return {"accepted": False, "busy": False, "error": "email 为空"}
    with _LOCK:
        if account_id in _RUNNING:
            return {"accepted": False, "busy": True, "error": "该账号正在查活或等待人工验证"}
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "queue_full": True, "error": "查活队列已满，请稍后重试"}
    if not db.claim_account_live_check(acc_id=account_id, trigger=trigger):
        _QUEUE_SLOTS.release()
        return {"accepted": False, "busy": True, "error": "该账号正在查活"}

    _append_log(email, f"[查活] 已入队 account_id={account_id} trigger={trigger}", clear=True)
    handle = task_control.control(KIND, account_id)
    try:
        accepted = _EXECUTOR.submit(
            task_control.gated(_run_live_check, handle, on_cancel=lambda: _cancel_pending_live_check(account_id)),
            account_id=account_id,
            email=email,
            proxy=proxy,
            trigger=str(trigger or "manual"),
        )
        if accepted is False:
            raise RuntimeError("查活队列已关闭")
    except Exception as exc:
        task_control.release(KIND, account_id)
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


def apply_settings() -> dict:
    """热加载查活并发数并立即对运行中的批次生效。"""
    global _WORKERS
    _WORKERS = _int_setting("LIVE_CHECK_WORKERS", 3, 1, _MAX_WORKERS)
    _EXECUTOR.set_workers(_WORKERS)
    return queue_settings()


def queue_settings() -> dict:
    return {"workers": _WORKERS, "queue_limit": _QUEUE_LIMIT}
