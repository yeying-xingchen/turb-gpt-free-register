# -*- coding: utf-8 -*-
"""账号额度余额、Codex 用量窗口与「银行重置」券查询。

对应「额度.har」中 Codex Browser 账单页发起的只读接口，外加同族的 wham/usage：

    GET /backend-api/accounts/{account_id}/remaining_balance    额度余额
    GET /backend-api/wham/rate-limit-reset-credits              「银行重置」券
    GET /backend-api/wham/usage                                 5 小时/周用量与 credits 权益

三个接口都不消耗额度、不修改账号状态，因此任一失败都只写回各自的错误字段，
不影响同一次套餐查询里已经拿到的套餐/订阅结果。

重置券响应结构（上游公开实现一致）：

    {"available_count": 1,
     "applicable_available_count": 0,
     "credits": [{"id": "RateLimitResetCredit_...", "status": "available",
                  "granted_at": "2026-06-17T17:38:38Z",
                  "expires_at": "2026-07-17T17:38:38Z"}]}

用量窗口响应结构（wham/usage，窗口长度按 limit_window_seconds 判断，
不假设 primary 一定是 5 小时窗口）：

    {"plan_type": "plus",
     "rate_limit": {"allowed": true, "limit_reached": false,
                    "primary_window":   {"used_percent": 22, "reset_at": 1766948068,
                                         "limit_window_seconds": 18000},
                    "secondary_window": {"used_percent": 43, "reset_at": 1767407914,
                                         "limit_window_seconds": 604800}},
     "credits": {"has_credits": true, "unlimited": false, "balance": "12.34"}}

remaining_balance 只返回很小的一个 JSON 对象（观测到的响应体约 45 字节），
灰度字段名不完全一致，因此按候选键名逐个取值，并把原始响应留在诊断字段里。
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import quote

from core.chatgpt_plan import (
    BrowserSession,
    chatgpt_api_headers,
    close_browser_session,
    normalize_token,
    now_iso,
    open_plan_check_proxy,
    resolve_plan_check_route,
    token_claims,
    warm_chatgpt_session,
)

logger = logging.getLogger(__name__)

BALANCE_PATH = "/backend-api/accounts/{account_id}/remaining_balance"
RESET_CREDITS_PATH = "/backend-api/wham/rate-limit-reset-credits"
WHAM_USAGE_PATH = "/backend-api/wham/usage"

# wham/usage 的 rate_limit 按 limit_window_seconds 报告窗口真实长度：
# 5 小时窗口 18000 秒，周窗口 604800 秒；团队/企业套餐的长窗口可能是整月。
USAGE_5H_WINDOW_SECONDS = 18000
USAGE_MIN_LONG_SECONDS = 72000     # 20 小时以上按周/月长窗口归类
USAGE_MONTHLY_WINDOW_SECONDS = 28 * 24 * 3600

# 余额字段在不同灰度下的候选键名，按优先级取值。
_BALANCE_KEYS = (
    "remaining_balance",
    "remainingBalance",
    "balance",
    "credits_remaining",
    "remaining_credits",
    "available_balance",
    "credit_balance",
    "amount",
)
_CURRENCY_KEYS = ("currency", "currency_code", "currencyCode", "unit")
_UNLIMITED_KEYS = ("unlimited", "is_unlimited", "isUnlimited", "has_unlimited")
_HAS_CREDITS_KEYS = ("has_credits", "hasCredits")

# credits[] 里每种状态都返回，只有未使用的券才计入最近到期时间。
_ACTIVE_CREDIT_STATUSES = {"", "available", "active", "granted", "redeemable"}
_MISSING_ACCOUNT_ID_ERROR = "缺少 account_id，无法查询额度余额"


def _clean_text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _as_amount(value: Any) -> Optional[float]:
    """把 12 / "12.34" / "$12.34" / "1,234.5" 归一化成 float。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = _clean_text(value).replace(",", "").replace("$", "").replace("¥", "")
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _as_count(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(float(_clean_text(value)))
    except (TypeError, ValueError):
        return None


def _first_value(data: dict, keys: tuple[str, ...]) -> tuple[str, Any]:
    for key in keys:
        if key in data and data.get(key) is not None:
            return key, data.get(key)
    return "", None


def _iso_sort_key(value: Any) -> tuple[int, str]:
    """ISO 时间排序键：可解析的排在前面并按时间升序，不可解析的排最后。"""
    text = _clean_text(value)
    if not text:
        return (1, "")
    try:
        return (0, datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat())
    except (TypeError, ValueError):
        return (1, text)


def parse_remaining_balance(data: Any) -> dict:
    """解析 remaining_balance 响应；无法识别的字段保留原始 JSON。"""
    if not isinstance(data, dict):
        raise ValueError("额度响应不是 JSON 对象")

    key, raw_balance = _first_value(data, _BALANCE_KEYS)
    amount = _as_amount(raw_balance)
    balance_text = _clean_text(raw_balance)
    _, raw_currency = _first_value(data, _CURRENCY_KEYS)
    _, raw_unlimited = _first_value(data, _UNLIMITED_KEYS)
    _, raw_has_credits = _first_value(data, _HAS_CREDITS_KEYS)

    return {
        "quota_balance": balance_text or None,
        "quota_balance_amount": amount,
        "quota_balance_key": key or None,
        "quota_currency": _clean_text(raw_currency) or None,
        "quota_unlimited": bool(raw_unlimited) if raw_unlimited is not None else False,
        "quota_has_credits": bool(raw_has_credits) if raw_has_credits is not None else None,
        "quota_response": data,
    }


def parse_reset_credits(data: Any) -> dict:
    """解析 wham/rate-limit-reset-credits 响应，输出张数与最近到期时间。"""
    if not isinstance(data, dict):
        raise ValueError("重置券响应不是 JSON 对象")

    credits = data.get("credits")
    credits = credits if isinstance(credits, list) else []
    available_count = _as_count(data.get("available_count"))
    if available_count is None and credits:
        # 老响应可能只有列表：按未使用的券数量兜底。
        available_count = sum(
            1
            for item in credits
            if isinstance(item, dict)
            and _clean_text(item.get("status")).lower() in _ACTIVE_CREDIT_STATUSES
            and not _clean_text(item.get("redeemed_at"))
        )

    detail: list[dict] = []
    expiry_candidates: list[str] = []
    for item in credits:
        if not isinstance(item, dict):
            continue
        status = _clean_text(item.get("status")).lower()
        record = {
            "id": _clean_text(item.get("id")) or None,
            "status": status or None,
            "granted_at": _clean_text(item.get("granted_at")) or None,
            "expires_at": _clean_text(item.get("expires_at")) or None,
            "redeemed_at": _clean_text(item.get("redeemed_at")) or None,
        }
        detail.append(record)
        if status in _ACTIVE_CREDIT_STATUSES and not record["redeemed_at"] and record["expires_at"]:
            expiry_candidates.append(record["expires_at"])

    return {
        "reset_credits_available": available_count,
        "reset_credits_applicable": _as_count(data.get("applicable_available_count")),
        "reset_credits_expires_at": min(expiry_candidates, key=_iso_sort_key) if expiry_candidates else None,
        "reset_credits_detail": detail,
        "reset_credits_response": data,
    }


def _as_percent(value: Any) -> Optional[float]:
    """已用百分比：容忍 77 / "77" / 77.5，超范围时截断到 0–100。"""
    amount = _as_amount(value)
    if amount is None:
        return None
    return max(0.0, min(100.0, round(amount, 2)))


def _window_seconds(window: dict) -> Optional[int]:
    for key in ("limit_window_seconds", "limitWindowSeconds", "window_seconds", "windowSeconds"):
        value = _as_count(window.get(key))
        if value and value > 0:
            return value
    for key in ("limit_window_minutes", "limitWindowMinutes", "window_minutes"):
        value = _as_count(window.get(key))
        if value and value > 0:
            return value * 60
    return None


def _window_reset_at(window: dict) -> Optional[str]:
    """reset_at 上游是 unix 秒；也可能是毫秒或 ISO 字符串，统一成 ISO。"""
    raw = window.get("reset_at", window.get("resetAt"))
    if isinstance(raw, bool) or raw in (None, ""):
        return None
    if isinstance(raw, (int, float)):
        seconds = float(raw)
        if seconds > 1e11:  # 毫秒
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(timespec="seconds")
        except (OverflowError, OSError, ValueError):
            return None
    text = _clean_text(raw)
    if not text:
        return None
    if text.isdigit():
        return _window_reset_at({"reset_at": int(text)})
    return text


def _window_reset_after(window: dict) -> Optional[int]:
    for key in ("reset_after_seconds", "resetAfterSeconds", "reset_after"):
        value = _as_count(window.get(key))
        if value is not None and value >= 0:
            return value
    return None


def _usage_window_entry(window: dict) -> dict:
    """把单个限流窗口归一化成展示/存储用的字段。

    `started` 为假表示窗口还没开始用：已用 0% 且剩余重置时间仍是整个窗口
    （用于区分「刚重置还没用过」和「用完之后又重置」）。
    """
    percent = _as_percent(window.get("used_percent", window.get("usedPercent")))
    seconds = _window_seconds(window)
    reset_after = _window_reset_after(window)
    return {
        "percent": percent,
        "window_seconds": seconds,
        "reset_at": _window_reset_at(window),
        "reset_after_seconds": reset_after,
        "started": not (
            percent == 0
            and reset_after is not None
            and seconds is not None
            and reset_after >= seconds
        ),
    }


def _limit_reached_type(data: dict) -> Optional[str]:
    raw = data.get("rate_limit_reached_type", data.get("rateLimitReachedType"))
    if isinstance(raw, dict):
        return _clean_text(raw.get("type")) or None
    return _clean_text(raw) or None


def parse_wham_usage(data: Any) -> dict:
    """解析 wham/usage：5 小时/周(月) 用量窗口、credits 与限流状态。

    窗口按 `limit_window_seconds` 的真实长度归类，不假设 primary 一定是 5h：
    短窗口（< 20 小时）进 `usage_5h_*`，长窗口进 `usage_week_*`（团队套餐的
    月度窗口也落在这里，前端按 `usage_week_window_seconds` 显示「月」）。
    """
    if not isinstance(data, dict):
        raise ValueError("用量响应不是 JSON 对象")

    rate_limit = data.get("rate_limit") if isinstance(data.get("rate_limit"), dict) else data.get("rateLimit")
    rate_limit = rate_limit if isinstance(rate_limit, dict) else {}
    windows = [
        rate_limit.get("primary_window") or rate_limit.get("primaryWindow"),
        rate_limit.get("secondary_window") or rate_limit.get("secondaryWindow"),
    ]

    slots: dict[str, dict] = {}
    for index, window in enumerate(windows):
        if not isinstance(window, dict):
            continue
        seconds = _window_seconds(window)
        if seconds is None:
            # 缺少窗口长度时按上游习惯回退到位置：primary=5h，secondary=长窗口。
            slot = "usage_5h" if index == 0 else "usage_week"
        else:
            slot = "usage_5h" if seconds < USAGE_MIN_LONG_SECONDS else "usage_week"
        slots.setdefault(slot, _usage_window_entry(window))

    def _flag(*keys: str) -> Optional[bool]:
        for key in keys:
            if key in rate_limit and rate_limit.get(key) is not None:
                return bool(rate_limit.get(key))
        return None

    result: dict = {
        "usage_plan_type": _clean_text(data.get("plan_type", data.get("planType"))) or None,
        "usage_allowed": _flag("allowed"),
        "usage_limit_reached": _flag("limit_reached", "limitReached"),
        "usage_limit_reached_type": _limit_reached_type(data),
        "usage_response": data,
    }
    for slot in ("usage_5h", "usage_week"):
        entry = slots.get(slot) or {}
        result.update({
            f"{slot}_percent": entry.get("percent"),
            f"{slot}_window_seconds": entry.get("window_seconds"),
            f"{slot}_reset_at": entry.get("reset_at"),
            f"{slot}_reset_after_seconds": entry.get("reset_after_seconds"),
            f"{slot}_started": entry.get("started"),
        })

    credits = data.get("credits") if isinstance(data.get("credits"), dict) else {}
    if credits:
        has_credits = credits.get("has_credits", credits.get("hasCredits"))
        overage = credits.get("overage_limit_reached", credits.get("overageLimitReached"))
        balance = _clean_text(credits.get("balance")) or None
        result.update({
            # has_credits=false 才是「这个账号没有 credits 额度」，用于区分余额 0 的两种含义。
            "quota_has_credits": bool(has_credits) if has_credits is not None else None,
            "quota_unlimited": bool(credits.get("unlimited")) if credits.get("unlimited") is not None else None,
            "quota_overage_limit_reached": bool(overage) if overage is not None else None,
            "quota_credits_balance": balance,
            "quota_credits_balance_amount": _as_amount(balance),
        })

    return result


def _get_json(
    env: Any,
    url: str,
    headers: dict,
    *,
    timeout: float,
) -> tuple[Optional[int], Any, str, Optional[str]]:
    """执行一次只读请求，返回 (http_status, json, preview, error)。"""
    response = None
    response_text = ""
    try:
        response = env.get(url, headers=headers, allow_redirects=False, timeout=timeout)
        response_text = response.text or ""
        status = int(response.status_code)
        if not (200 <= status < 300):
            return status, None, response_text[:500], f"HTTP {status}"
        try:
            data: Any = response.json()
        except Exception:
            data = json.loads(response_text) if response_text.strip().startswith(("{", "[")) else None
        if not isinstance(data, dict):
            return status, None, response_text[:500], "响应不是 JSON 对象"
        return status, data, response_text[:500], None
    except Exception as exc:
        status = getattr(response, "status_code", None)
        return (
            int(status) if status else None,
            None,
            response_text[:500],
            f"{type(exc).__name__}: {str(exc)[:180]}",
        )


def _retryable_quota_status(status: Optional[int]) -> bool:
    if status is None:
        return True
    return status in {403, 408, 409, 425, 429} or status >= 500


def fetch_account_quota(
    env: Any,
    token: str,
    account_id: str,
    *,
    timeout: float = 15.0,
    claims: dict | None = None,
) -> dict:
    """在已建立的 ChatGPT 会话上查询额度与重置券；两个接口互不影响。

    请求顺序与「额度.har」一致：先列重置券，再读额度余额。
    """
    account_id = _clean_text(account_id)
    claims = dict(claims or {})
    if account_id:
        claims["account_id"] = account_id
    result: dict = {"quota_checked_at": now_iso(), "reset_credits_checked_at": now_iso()}

    status, data, preview, error = _get_json(
        env,
        f"https://chatgpt.com{RESET_CREDITS_PATH}",
        chatgpt_api_headers(env, token, claims, target_path=RESET_CREDITS_PATH),
        timeout=timeout,
    )
    result.update({
        "reset_credits_http_status": status,
        "reset_credits_error": error,
        "reset_credits_response_preview": None if error is None else preview,
    })
    if error is None:
        try:
            parsed = parse_reset_credits(data)
        except ValueError as exc:
            result["reset_credits_error"] = str(exc)
        else:
            result["reset_credits_response_preview"] = json.dumps(data, ensure_ascii=False)[:500]
            result.update(parsed)

    if account_id:
        path = BALANCE_PATH.format(account_id=quote(account_id))
        status, data, preview, error = _get_json(
            env,
            f"https://chatgpt.com{path}",
            chatgpt_api_headers(env, token, claims, target_path=path),
            timeout=timeout,
        )
    else:
        status, data, preview, error = None, None, "", _MISSING_ACCOUNT_ID_ERROR
    result.update({
        "quota_http_status": status,
        "quota_error": error,
        "quota_response_preview": None if error is None else preview,
    })
    if error is None:
        try:
            parsed = parse_remaining_balance(data)
        except ValueError as exc:
            result["quota_error"] = str(exc)
        else:
            result["quota_response_preview"] = json.dumps(data, ensure_ascii=False)[:500]
            result.update(parsed)

    # 5 小时/周用量与 credits 权益：同一个只读接口，顺便把「余额 0」的两种含义分开。
    status, data, preview, error = _get_json(
        env,
        f"https://chatgpt.com{WHAM_USAGE_PATH}",
        chatgpt_api_headers(env, token, claims, target_path=WHAM_USAGE_PATH),
        timeout=timeout,
    )
    result["usage_checked_at"] = now_iso()
    result.update({
        "usage_http_status": status,
        "usage_error": error,
        "usage_response_preview": None if error is None else preview,
    })
    if error is None:
        try:
            parsed = parse_wham_usage(data)
        except ValueError as exc:
            result["usage_error"] = str(exc)
        else:
            result["usage_response_preview"] = json.dumps(data, ensure_ascii=False)[:500]
            result.update(parsed)
            # remaining_balance 没有可用数值或直接失败时，用 credits.balance 兜底，
            # 保证额度列至少能显示上游的另一处余额（来源会记录在 tooltip 里）。
            if result.get("quota_balance") is None and parsed.get("quota_credits_balance") is not None:
                result.update({
                    "quota_balance": parsed.get("quota_credits_balance"),
                    "quota_balance_amount": parsed.get("quota_credits_balance_amount"),
                    "quota_balance_fallback": True,
                })

    return result


def check_account_quota(
    token: str,
    *,
    proxy: Optional[str] = None,
    timezone_offset_min: str = "-",
    timeout: float | None = None,
    max_attempts: int | None = None,
    retry_delay: float | None = None,
) -> dict:
    """独立查询一个账号的额度与重置券（复用查套餐的网络策略与会话指纹）。"""
    # 网络策略、重试退避和熔断复位与查套餐共用同一套实现，避免两套会话逻辑漂移。
    from core.chatgpt_plan import _clear_plan_circuit, _plan_check_settings, _retry_wait_seconds

    token = normalize_token(token)
    if not token:
        return {"ok": False, "checked_at": now_iso(), "error": "token 为空"}
    claims = token_claims(token)
    if claims.get("token_expired") is True:
        return {
            "ok": False,
            "checked_at": now_iso(),
            "http_status": None,
            "error": "AT已过期/失效，请手动查活刷新",
            "needs_live_check": True,
            **{k: v for k, v in claims.items() if k != "payload"},
        }

    try:
        route = resolve_plan_check_route(proxy)
    except Exception as exc:
        return {
            "ok": False,
            "checked_at": now_iso(),
            "http_status": None,
            "error": f"额度查询网络配置错误: {exc}",
            **{k: v for k, v in claims.items() if k != "payload"},
        }
    route_meta = {k: v for k, v in route.items() if k not in {"proxy", "upstream_proxy"}}
    try:
        timeout_seconds, attempts, base_delay = _plan_check_settings(timeout, max_attempts, retry_delay)
    except Exception as exc:
        return {
            "ok": False,
            "checked_at": now_iso(),
            "http_status": None,
            "error": f"额度查询重试配置错误: {exc}",
            "retryable": False,
            **route_meta,
            **{k: v for k, v in claims.items() if k != "payload"},
        }

    account_id = _clean_text(claims.get("account_id"))
    identity = str(claims.get("email") or account_id or token[:32]).lower()
    task_seed = f"quota-check:{identity}:{uuid.uuid4()}"
    env: BrowserSession | None = None
    relay = None
    last_result: dict | None = None
    try:
        effective_proxy, relay = open_plan_check_proxy(route, route["proxy"], timeout=timeout_seconds)
        env = BrowserSession(proxy=effective_proxy, detect_exit_geo=True, fingerprint_seed=task_seed)
        effective_tz = str(timezone_offset_min or "").strip()
        if not effective_tz or effective_tz == "-":
            effective_tz = str(env.js_timezone_offset_min())
        logger.info(
            "[Quota] 统一会话已创建：proxy=%s device_id=%s %s",
            route_meta.get("proxy_used") or route_meta.get("network_route") or "direct",
            str(env.device_id)[:12] + "...",
            env.fingerprint_summary_text(),
        )
        warm_chatgpt_session(env)

        for attempt in range(1, attempts + 1):
            task_meta = {
                "attempt_count": attempt,
                "max_attempts": attempts,
                "request_timeout": timeout_seconds,
                "timezone_offset_min": effective_tz,
            }
            try:
                quota = fetch_account_quota(
                    env,
                    token,
                    account_id,
                    timeout=timeout_seconds,
                    claims=claims,
                )
            except Exception as exc:
                logger.debug("额度查询失败: %s: %s", type(exc).__name__, exc, exc_info=True)
                stamp = now_iso()
                last_result = {
                    "ok": False,
                    "checked_at": stamp,
                    "http_status": None,
                    "error": f"{type(exc).__name__}: {exc}",
                    "retryable": True,
                    "quota_checked_at": stamp,
                    "quota_error": f"{type(exc).__name__}: {str(exc)[:180]}",
                    "reset_credits_checked_at": stamp,
                    "reset_credits_error": f"{type(exc).__name__}: {str(exc)[:180]}",
                    "usage_checked_at": stamp,
                    "usage_error": f"{type(exc).__name__}: {str(exc)[:180]}",
                    **task_meta,
                }
            else:
                quota_ok = not quota.get("quota_error")
                credits_ok = not quota.get("reset_credits_error")
                usage_ok = not quota.get("usage_error")
                any_ok = bool(quota_ok or credits_ok or usage_ok)
                statuses = [
                    quota.get("quota_http_status"),
                    quota.get("reset_credits_http_status"),
                    quota.get("usage_http_status"),
                ]
                last_result = {
                    "ok": any_ok,
                    "checked_at": quota.get("quota_checked_at") or now_iso(),
                    "http_status": (
                        quota.get("quota_http_status")
                        if quota_ok
                        else next((value for value in statuses[1:] if value is not None), None)
                    ),
                    "error": None if any_ok else (
                        quota.get("quota_error")
                        or quota.get("reset_credits_error")
                        or quota.get("usage_error")
                    ),
                    # 三个接口都属于可重试的只读查询；全部失败时按状态码决定是否重试。
                    "retryable": not any_ok and any(_retryable_quota_status(value) for value in statuses),
                    **quota,
                    **task_meta,
                }
            last_result.update({**route_meta, **{k: v for k, v in claims.items() if k != "payload"}})
            if last_result.get("ok") or not last_result.get("retryable") or attempt >= attempts:
                return last_result

            _clear_plan_circuit(env)
            wait_seconds = _retry_wait_seconds(None, base_delay, attempt)
            logger.warning(
                "额度查询临时失败，第 %s/%s 次，保留 session/deviceId/CF Cookie，%.1fs 后重试: %s",
                attempt,
                attempts,
                wait_seconds,
                last_result.get("error"),
            )
            if wait_seconds > 0:
                time.sleep(wait_seconds)
    except Exception as exc:
        logger.debug("额度查询会话初始化失败: %s: %s", type(exc).__name__, exc, exc_info=True)
        return {
            "ok": False,
            "checked_at": now_iso(),
            "http_status": None,
            "error": f"{type(exc).__name__}: {exc}",
            "retryable": True,
            "attempt_count": 0,
            "max_attempts": attempts,
            "request_timeout": timeout_seconds,
            **route_meta,
            **{k: v for k, v in claims.items() if k != "payload"},
        }
    finally:
        if env is not None:
            try:
                close_browser_session(env)
            except Exception:
                pass
        if relay is not None:
            relay.close()

    return last_result or {
        "ok": False,
        "checked_at": now_iso(),
        "http_status": None,
        "error": "额度查询未执行",
        "retryable": False,
        **route_meta,
        **{k: v for k, v in claims.items() if k != "payload"},
    }
