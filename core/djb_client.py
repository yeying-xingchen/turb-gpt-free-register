# -*- coding: utf-8 -*-
"""DJB 提链 API 客户端。

协议来自 DJB API 对接文档 V1.01：
- POST /api/tasks 创建任务
- GET /api/tasks/{id} 查询任务
- DELETE /api/tasks/{id} 取消任务
- POST /api/card/check 校验卡密（不消耗次数）
- GET /api/meta 获取通道和引擎信息

DJB 的 accounts[].at 是 access token 明文。客户端只把它放进请求体，
不会写入日志或持久化响应；请求配置在建单前冻结，避免 WebUI 热加载时
把轮询切换到另一套服务/卡密/代理。
"""
from __future__ import annotations

import ast
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import requests

from config import extract_link as cfg
from config.proxy import normalize_proxy_url, redact_proxy_url


DJB_MODES = {"paypal", "gopay", "gcash", "momo", "upi", "card", "pix", "ideal"}
DJB_TERMINAL_STATUSES = {"success", "plus_success", "failed", "timed_out", "canceled", "error"}
DJB_SUCCESS_STATUSES = {"success", "plus_success"}


class DjbApiError(RuntimeError):
    """DJB API 请求或响应不符合约定。"""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class DjbTaskConfig:
    """一次 DJB 任务使用的不可变配置快照。"""

    base_url: str
    card_code: str
    proxies_text: str
    exit_proxies_text: str
    proxy_mode: str
    concurrency: int
    timeout_ms: int
    poll_interval_ms: int
    params_text: str
    request_timeout: float


def _load_env() -> None:
    try:
        from config.env_loader import load_env
        load_env(override=True)
    except Exception:
        pass


def _runtime_setting(name: str, default: Any = None, *, preserve_empty: bool = False) -> Any:
    _load_env()
    raw = os.getenv(name)
    if raw is not None and (preserve_empty or str(raw).strip() != ""):
        return raw
    return getattr(cfg, name, default)


def _base_url() -> str:
    value = str(_runtime_setting("DJB_API_BASE", "") or "").strip().rstrip("/")
    if not value:
        raise DjbApiError("DJB_API_BASE 为空")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise DjbApiError("DJB_API_BASE 必须是无账号密码的 http(s) URL")
    # 官方服务应使用 HTTPS；仅允许本机 HTTP 便于本地自建服务调试。
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise DjbApiError("DJB_API_BASE 非本机地址必须使用 HTTPS")
    return value


def _url(path: str, *, base_url: str | None = None) -> str:
    return urljoin((base_url or _base_url()) + "/", str(path).lstrip("/"))


def _request_timeout() -> float:
    try:
        value = float(_runtime_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30) or 30)
    except (TypeError, ValueError):
        value = 30.0
    return max(5.0, min(300.0, value))


def _headers() -> dict[str, str]:
    return {"Accept": "application/json", "Content-Type": "application/json"}


def _redact(text: Any, secrets: list[str] | None = None) -> str:
    value = str(text or "")
    for secret in secrets or []:
        if secret and len(secret) >= 3:
            value = value.replace(secret, "[redacted]")
    value = re.sub(r"(?i)bearer\s+[^\s,;]+", "Bearer [redacted]", value)
    value = re.sub(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[token-redacted]", value)
    value = re.sub(r"(?i)(https?://)([^/@\s]+):([^/@\s]+)@", r"\1[redacted]@", value)
    value = re.sub(r"\bTL-[A-Za-z0-9][A-Za-z0-9-]{5,}\b", "[card-redacted]", value)
    return value[:300]


def _body_secrets(body: Any) -> list[str]:
    found: list[str] = []

    def visit(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                visit(child_value, str(child_key))
        elif isinstance(value, (list, tuple)):
            for child_value in value:
                visit(child_value, key)
        elif value is not None and any(word in key.lower() for word in ("token", "card", "proxy", "password", "secret")):
            text = str(value)
            if text:
                found.append(text)

    visit(body)
    return found


def _json_payload(response: requests.Response, *, status_code: int, secrets: list[str]) -> dict[str, Any]:
    try:
        payload = response.json()
    except (ValueError, TypeError) as exc:
        raise DjbApiError(f"DJB 返回非 JSON（HTTP {status_code}）", status_code=status_code) from exc
    if not isinstance(payload, dict):
        raise DjbApiError("DJB 返回 JSON 不是对象", status_code=status_code)
    return payload


def _error_text(payload: dict[str, Any], secrets: list[str] | None = None) -> str:
    value = (
        payload.get("error") or payload.get("message") or payload.get("msg")
        or payload.get("detail") or payload.get("code")
    )
    if isinstance(value, dict):
        value = value.get("message") or value.get("detail") or value.get("error") or value.get("code")
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    return _redact(value or "DJB API 请求失败", secrets)


def _retry_delay(response: requests.Response | None, attempt: int) -> float:
    raw = ""
    try:
        raw = str((response.headers or {}).get("Retry-After") or "").strip()
    except Exception:
        raw = ""
    try:
        delay = float(raw) if raw else 0.5 * (2 ** attempt)
    except (TypeError, ValueError):
        delay = 0.5 * (2 ** attempt)
    return max(0.1, min(5.0, delay))


def _request(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    task_config: DjbTaskConfig | None = None,
    retries: int = 0,
) -> dict[str, Any]:
    method = method.upper()
    base_url = task_config.base_url if task_config else _base_url()
    timeout = task_config.request_timeout if task_config else _request_timeout()
    secrets = _body_secrets(body)
    attempts = max(0, int(retries))

    for attempt in range(attempts + 1):
        try:
            response = requests.request(
                method,
                _url(path, base_url=base_url),
                headers=_headers(),
                json=body,
                timeout=timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            if method == "GET" and attempt < attempts:
                time.sleep(_retry_delay(None, attempt))
                continue
            raise DjbApiError(f"DJB 网络请求失败：{type(exc).__name__}") from exc

        status_code = int(getattr(response, "status_code", 0) or 0)
        if 300 <= status_code < 400:
            raise DjbApiError("DJB API 禁止重定向，请检查 DJB_API_BASE", status_code=status_code)
        if status_code in {429, 500, 502, 503, 504} and method == "GET" and attempt < attempts:
            time.sleep(_retry_delay(response, attempt))
            continue
        if status_code == 204:
            return {}
        if status_code < 200 or status_code >= 300:
            try:
                payload = response.json()
            except (ValueError, TypeError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            raise DjbApiError(
                f"DJB HTTP {status_code}: {_error_text(payload, secrets)}",
                status_code=status_code,
            )
        return _json_payload(response, status_code=status_code, secrets=secrets)

    raise DjbApiError("DJB 请求失败")


def _list_setting(name: str) -> list[str]:
    """解析列表配置；仅在 DJB_PROXIES 未出现于环境时复用全局代理池。"""
    _load_env()
    env_raw = os.getenv(name)
    raw: Any = env_raw if env_raw is not None else getattr(cfg, name, [])
    if isinstance(raw, (list, tuple)):
        values = [str(item).strip() for item in raw if str(item).strip()]
    else:
        text = str(raw or "").strip()
        values = []
        if text:
            try:
                parsed = ast.literal_eval(text)
                if isinstance(parsed, (list, tuple)):
                    values = [str(item).strip() for item in parsed if str(item).strip()]
            except (SyntaxError, ValueError):
                pass
            if not values:
                values = [line.strip() for line in text.splitlines() if line.strip()]
    if not values and name == "DJB_PROXIES" and env_raw is None:
        try:
            from config import proxy as proxy_cfg
            values = [str(item).strip() for item in (getattr(proxy_cfg, "PROXY_POOL", []) or []) if str(item).strip()]
        except Exception:
            values = []
    return values


def _int_setting(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(_runtime_setting(name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


def _mode(mode: str | None) -> str:
    value = str(mode or _runtime_setting("EXTRACT_LINK_TYPE", "pix") or "pix").strip().lower()
    if value not in DJB_MODES:
        raise DjbApiError("DJB 通道无效，支持 paypal / gopay / gcash / momo / upi / card / pix / ideal")
    return value


def _account_payload(accounts: list[dict[str, Any]]) -> list[dict[str, str]]:
    if not isinstance(accounts, list) or not accounts:
        raise DjbApiError("DJB accounts 必须是非空数组")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in accounts:
        if not isinstance(item, dict):
            raise DjbApiError("DJB accounts 每项必须是对象")
        email = str(item.get("email") or "").strip()
        token = str(item.get("at") or item.get("access_token") or item.get("token") or "").strip()
        if not email or not token:
            raise DjbApiError("DJB 账号必须包含 email 和 at(access_token)")
        key = email.lower()
        if key in seen:
            raise DjbApiError(f"DJB 账号重复：{email}")
        seen.add(key)
        channel = str(item.get("channel") or "oa").strip().lower() or "oa"
        result.append({"email": email, "at": token, "channel": channel})
    return result


def _params_text(value: str | dict[str, Any] | None) -> str:
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    text = str(value if value is not None else _runtime_setting("DJB_PARAMS_TEXT", "{}") or "{}").strip() or "{}"
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise DjbApiError(f"DJB_PARAMS_TEXT 不是有效 JSON：{exc}") from exc
    if not isinstance(parsed, dict):
        raise DjbApiError("DJB_PARAMS_TEXT 必须是 JSON 对象")
    return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))


def _proxy_text(value: list[str] | str | None, setting_name: str) -> str:
    if value is None:
        values = _list_setting(setting_name)
    elif isinstance(value, str):
        values = [line.strip() for line in value.splitlines() if line.strip()]
    else:
        values = [str(item).strip() for item in value if str(item).strip()]
    if len(values) > 1000:
        raise DjbApiError(f"{setting_name} 最多支持 1000 条代理")
    normalized: list[str] = []
    for value in values:
        try:
            normalized.append(normalize_proxy_url(value))
        except ValueError as exc:
            raise DjbApiError(
                f"{setting_name} 包含无效代理 {redact_proxy_url(value)}"
            ) from exc
    return "\n".join(normalized)


def capture_config(*, card_code: str | None = None) -> DjbTaskConfig:
    """读取并校验一次任务所需配置，返回冻结快照。"""
    # ``None`` means use the current environment; an explicit empty value must
    # remain empty so a cleared card cannot silently fall back to a stale module
    # default (the enqueue path applies the same rule in ``_cdk``).
    raw_code = (
        _runtime_setting("DJB_CARD_CODE", "", preserve_empty=True)
        if card_code is None else card_code
    )
    code = str(raw_code or "").strip()
    if not code:
        raise DjbApiError("DJB_CARD_CODE 为空")
    proxy_mode = str(_runtime_setting("DJB_PROXY_MODE", "custom") or "custom").strip().lower()
    if proxy_mode not in {"custom", "builtin"}:
        raise DjbApiError("DJB_PROXY_MODE 仅支持 custom 或 builtin")
    proxies = _proxy_text(None, "DJB_PROXIES")
    exit_proxies = _proxy_text(None, "DJB_EXIT_PROXIES")
    if proxy_mode == "custom" and not proxies:
        raise DjbApiError("DJB custom 模式需要配置 DJB_PROXIES（或全局 PROXY_POOL）")
    return DjbTaskConfig(
        base_url=_base_url(),
        card_code=code,
        proxies_text=proxies,
        exit_proxies_text=exit_proxies,
        proxy_mode=proxy_mode,
        concurrency=_int_setting("DJB_CONCURRENCY", 3, 1, 100),
        timeout_ms=_int_setting("DJB_TIMEOUT_MS", 600000, 1000, 600000),
        poll_interval_ms=_int_setting("DJB_POLL_INTERVAL_MS", 2000, 500, 10000),
        params_text=_params_text(None),
        request_timeout=_request_timeout(),
    )


def _task_id(payload: dict[str, Any]) -> str:
    for key in ("id", "taskId", "task_id"):
        value = payload.get(key)
        if value:
            return str(value).strip()
    for key in ("data", "task"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            value = _task_id(nested)
            if value:
                return value
    return ""


def create_task(
    *,
    mode: str | None,
    accounts: list[dict[str, Any]],
    card_code: str | None = None,
    proxies: list[str] | str | None = None,
    exit_proxies: list[str] | str | None = None,
    proxy_mode: str | None = None,
    concurrency: int | None = None,
    timeout_ms: int | None = None,
    params_text: str | dict[str, Any] | None = None,
    task_config: DjbTaskConfig | None = None,
) -> dict[str, Any]:
    """创建 DJB 任务，并返回包含 id 的任务快照。"""
    snapshot = task_config or capture_config(card_code=card_code)
    selected_proxy_mode = str(proxy_mode or snapshot.proxy_mode).strip().lower()
    if selected_proxy_mode not in {"custom", "builtin"}:
        raise DjbApiError("DJB_PROXY_MODE 仅支持 custom 或 builtin")
    proxies_text = snapshot.proxies_text if proxies is None else _proxy_text(proxies, "DJB_PROXIES")
    exit_text = snapshot.exit_proxies_text if exit_proxies is None else _proxy_text(exit_proxies, "DJB_EXIT_PROXIES")
    if selected_proxy_mode == "custom" and not proxies_text:
        raise DjbApiError("DJB custom 模式需要配置建单代理")
    selected_concurrency = snapshot.concurrency if concurrency is None else max(1, min(100, int(concurrency)))
    selected_timeout = snapshot.timeout_ms if timeout_ms is None else max(1000, min(600000, int(timeout_ms)))
    selected_params = snapshot.params_text if params_text is None else _params_text(params_text)
    payload: dict[str, Any] = {
        "mode": _mode(mode),
        "cardCode": snapshot.card_code if card_code is None else str(card_code).strip(),
        "accounts": _account_payload(accounts),
        "proxiesText": proxies_text,
        "exitProxiesText": exit_text,
        "proxyMode": selected_proxy_mode,
        "concurrency": selected_concurrency,
        "timeoutMs": selected_timeout,
        "paramsText": selected_params,
    }
    data = _request("POST", "/api/tasks", body=payload, task_config=snapshot)
    task_id = _task_id(data)
    if not task_id:
        raise DjbApiError("DJB 创建任务未返回任务 id")
    # 统一给上层一个稳定的 id；保留官方响应其余字段用于进度展示。
    if not data.get("id"):
        data = dict(data)
        data["id"] = task_id
    return data


def get_task(task_id: str, *, task_config: DjbTaskConfig | None = None) -> dict[str, Any]:
    task_id = str(task_id or "").strip()
    if not task_id:
        raise DjbApiError("DJB task id 为空")
    return _request("GET", f"/api/tasks/{quote(task_id, safe='')}", task_config=task_config, retries=2)


def cancel_task(task_id: str, *, task_config: DjbTaskConfig | None = None) -> dict[str, Any]:
    task_id = str(task_id or "").strip()
    if not task_id:
        raise DjbApiError("DJB task id 为空")
    return _request("DELETE", f"/api/tasks/{quote(task_id, safe='')}", task_config=task_config)


def check_card(code: str | None = None) -> dict[str, Any]:
    value = str(code or _runtime_setting("DJB_CARD_CODE", "") or "").strip()
    if not value:
        raise DjbApiError("DJB_CARD_CODE 为空")
    return _request("POST", "/api/card/check", body={"code": value})


def get_meta() -> dict[str, Any]:
    return _request("GET", "/api/meta")


def get_stats(mode: str | None = None) -> dict[str, Any]:
    path = "/api/stats"
    if mode:
        path += f"?mode={quote(str(mode).strip().lower(), safe='')}"
    return _request("GET", path)


def poll_interval_seconds(task_config: DjbTaskConfig | None = None) -> float:
    value = task_config.poll_interval_ms if task_config is not None else _int_setting("DJB_POLL_INTERVAL_MS", 2000, 500, 10000)
    return value / 1000.0


def task_timeout_seconds(task_config: DjbTaskConfig | None = None) -> float:
    value = task_config.timeout_ms if task_config is not None else _int_setting("DJB_TIMEOUT_MS", 600000, 1000, 600000)
    return value / 1000.0


def _status_value(value: Any) -> str:
    """Normalize a status while treating null/blank values as absent."""
    if value is None:
        return ""
    text = str(value).strip()
    return text.lower().replace("-", "_").replace(" ", "_") if text else ""


def _payload_candidates(snapshot: dict[str, Any], *, _seen: set[int] | None = None) -> list[dict[str, Any]]:
    """Return envelope candidates from outer to nested levels.

    ``task`` is the task object in the DJB protocol; ``data`` is often a
    generic API wrapper.  Prefer the task object and never let null/blank
    nested fields erase a meaningful outer task field.
    """
    if not isinstance(snapshot, dict):
        return []
    seen = _seen if _seen is not None else set()
    marker = id(snapshot)
    if marker in seen:
        return []
    seen.add(marker)
    candidates: list[dict[str, Any]] = []
    for key in ("task", "data"):
        nested = snapshot.get(key)
        if isinstance(nested, dict):
            candidates.extend(_payload_candidates(nested, _seen=seen))
            candidates.append(nested)
    candidates.append(snapshot)
    return candidates


def _has_task_signal(payload: dict[str, Any]) -> bool:
    return any(
        key in payload and payload.get(key) not in (None, "")
        for key in ("status", "items", "results", "accounts", "progress", "id", "taskId", "task_id", "error", "message", "link")
    )


def task_payload(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Flatten DJB ``task``/``data`` envelopes without losing task fields."""
    if not isinstance(snapshot, dict):
        return {}
    candidates = [item for item in _payload_candidates(snapshot) if _has_task_signal(item)]
    if not candidates:
        return snapshot
    # Candidates were collected nested-first, with task before data.  Start
    # with the deepest useful task and fill absent values from its wrappers;
    # explicit task-level values win over generic outer metadata.
    merged: dict[str, Any] = {}
    for candidate in candidates:
        for key, value in candidate.items():
            if key in {"task", "data"} or value in (None, ""):
                continue
            if key not in merged or merged[key] in (None, ""):
                merged[key] = value
    return merged


def task_items(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Find the first non-empty item collection across all envelopes."""
    if not isinstance(snapshot, dict):
        return []
    seen: set[int] = set()

    def visit(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, dict) or id(value) in seen:
            return []
        seen.add(id(value))
        for key in ("items", "results", "accounts"):
            items = value.get(key)
            if isinstance(items, list):
                normalized = [item for item in items if isinstance(item, dict)]
                if normalized:
                    return normalized
        # The real task envelope takes precedence over generic data wrappers.
        for key in ("task", "data"):
            result = visit(value.get(key))
            if result:
                return result
        return []

    return visit(snapshot)


def normalized_status(value: Any) -> str:
    status = _status_value(value)
    if status == "cancelled":
        return "canceled"
    if status in {"done", "completed", "complete", "ok"}:
        return "success"
    if status in {"plusdone", "plus_done", "plus_complete", "plus_completed"}:
        return "plus_success"
    if status in {"timeout", "timedout", "timed_out"}:
        return "timed_out"
    return status
