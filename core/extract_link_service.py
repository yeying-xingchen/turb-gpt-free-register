# -*- coding: utf-8 -*-
"""Plus 试用提链后台队列。

支持三种后端（config/extract_link.py 的 EXTRACT_LINK_BACKEND）：
  - cdk    原 CDK 提链服务：/api/cdk 查余额、/api/extract 建任务、/api/jobs/{id}/events SSE
  - pay153 pay153-checkout-link 服务：POST /api/checkout 建任务、GET /api/checkout-progress 轮询
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

try:
    from curl_cffi import requests as curl_requests
except Exception:  # WebUI 环境未装 curl_cffi 时使用标准库兜底
    curl_requests = None

from config import extract_link as cfg
from core import db

logger = logging.getLogger(__name__)


class _ExtractOwnershipLost(RuntimeError):
    """任务已被新一代提链任务接管，旧 worker 不得继续工作。"""


@dataclass(frozen=True)
class ExtractRuntimeConfig:
    """入队时冻结的路由、鉴权和超时配置。"""

    backend: str
    base_url: str
    request_timeout: float
    headers: dict[str, str]
    event_timeout: float
    pay_poll_interval: float
    pay_poll_timeout: int
    djb_config: object | None = None


def _update_extract(account_id: int, nonce: str, result: dict) -> bool:
    """只允许当前提链 worker 更新账号状态；失去所有权立即停止。"""
    ok = db.update_account_extract(account_id, result, nonce=nonce)
    if not ok:
        raise _ExtractOwnershipLost("提链任务已被重置或被新任务接管")
    return True


def _runtime_setting(name: str, default=None):
    """
    提链配置多数保存在 .env。服务模块会在 WebUI 启动时较早 import，
    因此每次实际读取时都重新加载 .env，避免“页面已保存但当前进程仍读到空值”。
    """
    try:
        from config.env_loader import load_env
        load_env(override=True)
    except Exception:
        pass
    raw = os.getenv(name)
    if raw is not None and str(raw).strip() != "":
        return str(raw).strip()
    return getattr(cfg, name, default)


def _int_setting(name: str, default: int, lower: int, upper: int) -> int:
    try:
        value = int(_runtime_setting(name, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(lower, min(upper, value))


def _bool_setting(name: str, default: bool) -> bool:
    raw = str(_runtime_setting(name, "1" if default else "0") or "").strip().lower()
    if raw in {"1", "true", "yes", "on", "y"}:
        return True
    if raw in {"0", "false", "no", "off", "n"}:
        return False
    return bool(default)


def _list_setting(name: str) -> list[str]:
    """读取多行/列表配置（入口/出口代理池等）。"""
    raw = _runtime_setting(name, "")
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()]
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        import ast
        val = ast.literal_eval(text)
        if isinstance(val, (list, tuple)):
            return [str(x).strip() for x in val if str(x).strip()]
    except Exception:
        pass
    return [line.strip() for line in text.splitlines() if line.strip()]


CDK_LINK_TYPES = {"pix", "upi", "kakao_pay", "ideal"}
DJB_LINK_TYPES = {"paypal", "gopay", "gcash", "momo", "upi", "card", "pix", "ideal"}
PAY153_LINK_TYPES = {
    "hosted", "ph_short", "paypal", "ideal", "twint",
    "upi", "pix", "momo", "gcash", "kakao",
}


def _backend() -> str:
    b = str(_runtime_setting("EXTRACT_LINK_BACKEND", "cdk") or "cdk").strip().lower()
    if b not in {"cdk", "pay153", "djbnb"}:
        raise ValueError(f"不支持的提链后端: {b}")
    return b


def _link_type(value: str | None = None, backend: str | None = None) -> str:
    backend = backend or _backend()
    t = str(value or _runtime_setting("EXTRACT_LINK_TYPE", "pix") or "pix").strip().lower()
    if backend == "djbnb":
        if t not in DJB_LINK_TYPES:
            raise ValueError("提链类型无效，DJB 后端支持 paypal / gopay / gcash / momo / upi / card / pix / ideal")
        return t
    if backend == "pay153":
        # 兼容旧配置里的 kakao_pay 名称
        if t == "kakao_pay":
            t = "kakao"
        if t not in PAY153_LINK_TYPES:
            raise ValueError("提链类型无效，pay153 后端支持 hosted / ph_short / paypal / ideal / twint / upi / pix / momo / gcash / kakao")
        return t
    if t not in CDK_LINK_TYPES:
        raise ValueError("提链类型无效，仅支持 pix / upi / kakao_pay / ideal")
    return t


def _validated_http_base(value: str, label: str) -> str:
    base = str(value or "").strip().rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{label} 必须是 http(s) URL")
    if parsed.username or parsed.password:
        raise ValueError(f"{label} 不得包含 URL 用户名或密码")
    return base


def _api_base(backend: str | None = None) -> str:
    backend = backend or _backend()
    key = {
        "djbnb": "DJB_API_BASE",
        "pay153": "PAY153_API_BASE",
        "cdk": "EXTRACT_LINK_API_BASE",
    }[backend]
    base = _runtime_setting(key, "")
    if not str(base or "").strip():
        raise ValueError(f"{key} 为空")
    return _validated_http_base(str(base), key)


def _cdk(value: str | None = None, backend: str | None = None) -> str:
    backend = backend or _backend()
    if backend == "pay153":
        # pay153 后端不需要 CDK
        return str(value or "").strip()
    if backend == "djbnb":
        raw = value if value is not None else _runtime_setting("DJB_CARD_CODE", "", preserve_empty=True)
        code = str(raw or "").strip()
        if not code:
            raise ValueError("DJB_CARD_CODE 为空")
        return code
    raw = value if value is not None else _runtime_setting("EXTRACT_LINK_CDK", "")
    cdk = str(raw or "").strip()
    if not cdk:
        raise ValueError("EXTRACT_LINK_CDK/CDK 为空")
    return cdk


def _headers(backend: str | None = None) -> dict[str, str]:
    backend = backend or _backend()
    headers = {"Accept": "application/json"}
    if backend == "pay153":
        key = str(_runtime_setting("PAY153_INTERNAL_KEY", "") or "").strip()
        if key:
            headers["X-Pay153-Internal-Key"] = key
    return headers


_WORKERS = _int_setting("EXTRACT_LINK_WORKERS", 3, 1, 16)
_QUEUE_LIMIT = _int_setting("EXTRACT_LINK_QUEUE_LIMIT", 500, _WORKERS, 5000)
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="extract-link")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)


def queue_settings() -> dict:
    return {"workers": _WORKERS, "queue_limit": _QUEUE_LIMIT}


def _session():
    if curl_requests is None:
        return None
    return curl_requests.Session()


def query_cdk(*, cdk: str | None = None) -> dict:
    """查询提链服务状态。

    cdk 后端返回 CDK 余额；pay153 后端返回服务 /api/config 摘要；
    djbnb 后端调用 /api/card/check（不消耗次数）。
    """
    if _backend() == "djbnb":
        from core import djb_client
        return {"backend": "djbnb", **djb_client.check_card(cdk)}
    if _backend() == "pay153":
        return _query_pay153_service()
    base = _api_base()
    code = _cdk(cdk)
    timeout = _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300)
    s = _session()
    try:
        if s is None:
            req = Request(f"{base}/api/cdk?{urlencode({'code': code})}", headers={"Accept": "application/json"})
            with urlopen(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace") or "{}")
            return payload if isinstance(payload, dict) else {}
        resp = s.get(f"{base}/api/cdk?{urlencode({'code': code})}", timeout=timeout)
        try:
            payload = resp.json()
        except Exception:
            payload = {"error": (resp.text or "")[:300]}
        if resp.status_code < 200 or resp.status_code >= 300:
            raise RuntimeError(payload.get("error") or f"HTTP {resp.status_code}")
        return payload if isinstance(payload, dict) else {}
    finally:
        try:
            s.close()
        except Exception:
            pass


def _query_pay153_service() -> dict:
    base = _api_base()
    timeout = _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300)
    s = _session()
    try:
        if s is None:
            req = Request(f"{base}/api/config", headers=_headers())
            with urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        else:
            resp = s.get(f"{base}/api/config", headers=_headers(), timeout=timeout)
            try:
                data = resp.json()
            except Exception:
                data = {"error": (resp.text or "")[:300]}
            if resp.status_code < 200 or resp.status_code >= 300:
                raise RuntimeError(data.get("error") or f"HTTP {resp.status_code}")
        if not isinstance(data, dict):
            data = {}
        return {
            "backend": "pay153",
            "service": "pay153",
            "link_types": data.get("link_types") or [],
            "disabled_link_types": data.get("disabled_link_types") or [],
            "plans": data.get("plans") or [],
            "task_limits": data.get("task_limits") or {},
        }
    finally:
        try:
            s.close()
        except Exception:
            pass


def _create_extract_job(*, token: str, link_type: str, cdk: str, email: str = "", backend: str | None = None, djb_config=None) -> dict:
    backend = backend or _backend()
    if backend == "djbnb":
        from core import djb_client
        return djb_client.create_task(
            mode=link_type,
            accounts=[{"email": email, "at": token, "channel": "oa"}],
            card_code=cdk or None,
            task_config=djb_config,
        )
    if backend == "pay153":
        return _create_pay153_job(token=token, link_type=link_type)
    return _create_cdk_job(token=token, link_type=link_type, cdk=cdk)


def _create_cdk_job(*, token: str, link_type: str, cdk: str) -> dict:
    base = _api_base()
    timeout = _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300)
    payload = {"link_type": _link_type(link_type), "cdk": _cdk(cdk), "token": token}
    s = _session()
    try:
        if s is None:
            body = json.dumps(payload).encode("utf-8")
            req = Request(
                f"{base}/api/extract",
                data=body,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
            if not isinstance(data, dict) or not data.get("job_id"):
                raise RuntimeError(f"提链服务未返回 job_id: {data}")
            return data
        resp = s.post(f"{base}/api/extract", json=payload, timeout=timeout)
        try:
            data = resp.json()
        except Exception:
            data = {"error": (resp.text or "")[:300]}
        if resp.status_code < 200 or resp.status_code >= 300:
            raise RuntimeError(data.get("error") or f"HTTP {resp.status_code}")
        if not isinstance(data, dict) or not data.get("job_id"):
            raise RuntimeError(f"提链服务未返回 job_id: {data}")
        return data
    finally:
        try:
            s.close()
        except Exception:
            pass


def _create_pay153_job(*, token: str, link_type: str) -> dict:
    base = _api_base()
    timeout = _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300)
    plan = str(_runtime_setting("PAY153_PLAN", "plus") or "plus").strip().lower() or "plus"
    country = str(_runtime_setting("PAY153_COUNTRY", "") or "").strip().upper()
    currency = str(_runtime_setting("PAY153_CURRENCY", "") or "").strip().upper()
    entry_proxies = _list_setting("PAY153_ENTRY_PROXIES")
    exit_proxies = _list_setting("PAY153_EXIT_PROXIES")
    internal_key = str(_runtime_setting("PAY153_INTERNAL_KEY", "") or "").strip()
    retry_count = _int_setting("PAY153_RETRY_COUNT", 3, 1, 50)
    use_promo = _bool_setting("PAY153_USE_PROMO", True)

    payload = {
        "token": token,
        "plan": plan,
        "link_type": _link_type(link_type),
        "country": country or "US",
        "currency": currency,
        "entry_proxies": entry_proxies,
        "exit_proxies": exit_proxies,
        "retry_count": retry_count,
        "use_promo": use_promo if plan == "plus" else False,
    }
    # 内部密钥存在且未配置代理池时，走 pay153 侧动态代理（服务端需启用动态代理 API）
    if internal_key and not entry_proxies and not exit_proxies:
        payload["dynamic_proxy_api"] = True
    elif not entry_proxies:
        raise ValueError("pay153 后端需要填写入口代理池，或配置 PAY153_INTERNAL_KEY 走服务端动态代理")

    s = _session()
    try:
        if s is None:
            body = json.dumps(payload).encode("utf-8")
            req = Request(
                f"{base}/api/checkout",
                data=body,
                headers={"Accept": "application/json", "Content-Type": "application/json", **_headers()},
                method="POST",
            )
            with urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
            if not isinstance(data, dict) or not data.get("job_id"):
                raise RuntimeError(f"pay153 服务未返回 job_id: {data}")
            return data
        resp = s.post(f"{base}/api/checkout", json=payload, headers=_headers(), timeout=timeout)
        try:
            data = resp.json()
        except Exception:
            data = {"error": (resp.text or "")[:300]}
        if resp.status_code < 200 or resp.status_code >= 300:
            raise RuntimeError(data.get("error") or f"HTTP {resp.status_code}")
        if not isinstance(data, dict) or not data.get("job_id"):
            raise RuntimeError(f"pay153 服务未返回 job_id: {data}")
        return data
    finally:
        try:
            s.close()
        except Exception:
            pass


def _pay153_progress(job_id: str) -> dict:
    base = _api_base()
    timeout = _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300)
    url = f"{base}/api/checkout-progress?{urlencode({'job_id': job_id})}"
    s = _session()
    try:
        if s is None:
            req = Request(url, headers=_headers())
            with urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
            return data if isinstance(data, dict) else {}
        resp = s.get(url, headers=_headers(), timeout=timeout)
        try:
            data = resp.json()
        except Exception:
            data = {"error": (resp.text or "")[:300]}
        if resp.status_code < 200 or resp.status_code >= 300:
            raise RuntimeError(data.get("error") or f"HTTP {resp.status_code}")
        return data if isinstance(data, dict) else {}
    finally:
        try:
            s.close()
        except Exception:
            pass


def _iter_sse_events(*, job_id: str, cdk: str):
    base = _api_base()
    timeout = _int_setting("EXTRACT_LINK_EVENT_TIMEOUT", 180, 30, 900)
    url = f"{base}/api/jobs/{quote(job_id, safe='')}/events?{urlencode({'cdk': _cdk(cdk)})}"
    s = _session()
    try:
        if s is None:
            req = Request(url, headers={"Accept": "text/event-stream"})
            with urlopen(req, timeout=timeout) as resp:
                event = "message"
                data_lines: list[str] = []
                for raw in resp:
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if line == "":
                        if data_lines:
                            text = "\n".join(data_lines)
                            try:
                                data = json.loads(text)
                            except Exception:
                                data = {"raw": text}
                            yield event, data
                        event = "message"
                        data_lines = []
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("event:"):
                        event = line.split(":", 1)[1].strip() or "message"
                    elif line.startswith("data:"):
                        data_lines.append(line.split(":", 1)[1].lstrip())
                if data_lines:
                    text = "\n".join(data_lines)
                    try:
                        data = json.loads(text)
                    except Exception:
                        data = {"raw": text}
                    yield event, data
            return
        resp = s.get(url, timeout=timeout, stream=True)
        if resp.status_code < 200 or resp.status_code >= 300:
            raise RuntimeError(f"监听提链事件失败 HTTP {resp.status_code}: {(resp.text or '')[:300]}")
        event = "message"
        data_lines: list[str] = []
        for raw in resp.iter_lines():
            if raw is None:
                continue
            if isinstance(raw, bytes):
                line = raw.decode("utf-8", "replace")
            else:
                line = str(raw)
            line = line.rstrip("\r")
            if line == "":
                if data_lines:
                    text = "\n".join(data_lines)
                    try:
                        data = json.loads(text)
                    except Exception:
                        data = {"raw": text}
                    yield event, data
                event = "message"
                data_lines = []
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event = line.split(":", 1)[1].strip() or "message"
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].lstrip())
        if data_lines:
            text = "\n".join(data_lines)
            try:
                data = json.loads(text)
            except Exception:
                data = {"raw": text}
            yield event, data
    finally:
        try:
            s.close()
        except Exception:
            pass


def _extract_error_message(data) -> str:
    """尽量从提链服务返回的任意错误结构中提取用户可读原因。"""
    if data is None:
        return ""
    if isinstance(data, str):
        return data.strip()
    if not isinstance(data, dict):
        return str(data)
    err = data.get("error")
    if isinstance(err, dict):
        for key in ("message", "detail", "reason", "error", "msg", "description"):
            value = err.get(key)
            if value:
                return str(value).strip()
        return json.dumps(err, ensure_ascii=False)[:500]
    if err:
        return str(err).strip()
    for key in ("message", "detail", "reason", "msg", "description", "raw"):
        value = data.get(key)
        if value:
            return str(value).strip()
    return json.dumps(data, ensure_ascii=False)[:500]


def _format_failure_reason(exc: Exception, logs: list[str] | None = None, last_event: dict | None = None) -> str:
    reason = f"{type(exc).__name__}: {str(exc)}".strip()
    if (not str(exc).strip()) and logs:
        reason = str(logs[-1])
    if last_event and "提链事件流结束但未返回 result" in reason:
        extracted = _extract_error_message(last_event.get("data"))
        if extracted:
            reason = f"提链事件流结束但未返回 result；最后事件 {last_event.get('event')}: {extracted}"
    return reason[:500]


def _map_pay153_result(result: dict, link_type: str) -> dict:
    """把 pay153 的 result 字典映射为账号表里统一的结果载荷。

    统一字段：long_url / copy_paste / image_url_png / image_url_svg /
              payment_method / payment_link_type / expires_at
    """
    if not isinstance(result, dict):
        result = {}

    def first(*keys: str) -> str:
        for key in keys:
            value = result.get(key)
            if value:
                return str(value)
        return ""

    link = first(
        "checkout_url", "paypal_link", "provider_redirect_url",
        "short_link", "verification_url", "qr_data",
    )
    payload = {
        "long_url": link,
        "copy_paste": link,
        "image_url_png": first("qr_image_png"),
        "image_url_svg": first("qr_image_svg"),
        "payment_method": first("payment_method_type") or str(result.get("link_type") or link_type),
        "payment_link_type": str(result.get("link_type") or link_type),
    }
    expires = result.get("expires_at")
    if expires is not None:
        payload["expires_at"] = expires
    return payload


def _run_extract(*, account_id: int, email: str, access_token: str, link_type: str, cdk: str, trigger: str, nonce: str, backend: str, djb_config=None) -> dict:
    """统一 worker 入口；路由异常也必须回写状态并释放队列槽。"""
    try:
        if backend == "djbnb":
            return _run_djb_extract(
                account_id=account_id, email=email, access_token=access_token,
                link_type=link_type, cdk=cdk, trigger=trigger, nonce=nonce,
                djb_config=djb_config,
            )
        if backend == "pay153":
            return _run_pay153_extract(
                account_id=account_id, email=email, access_token=access_token,
                link_type=link_type, cdk=cdk, trigger=trigger, nonce=nonce,
            )
        if backend == "cdk":
            return _run_cdk_extract(
                account_id=account_id, email=email, access_token=access_token,
                link_type=link_type, cdk=cdk, trigger=trigger, nonce=nonce,
            )
        raise ValueError(f"不支持的提链后端: {backend}")
    except Exception as exc:
        result = {
            "ok": False,
            "status": "failed",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": _format_failure_reason(exc),
            "message": "提链 worker 启动失败",
        }
        try:
            _update_extract(account_id, nonce, result)
        except Exception:
            logger.exception("[提链] worker 启动失败状态写入异常: account_id=%s", account_id)
        return result
    finally:
        _QUEUE_SLOTS.release()


def _run_cdk_extract(*, account_id: int, email: str, access_token: str, link_type: str, cdk: str, trigger: str, nonce: str) -> dict:
    logs: list[str] = []
    last_event = None
    try:
        if not db.mark_account_extract_running(account_id, nonce=nonce):
            return {"ok": False, "error": "账号已删除或提链状态已被重置"}
        job = _create_extract_job(
            token=access_token, email=email, link_type=link_type, cdk=cdk,
            backend="cdk",
        )
        job_id = str(job.get("job_id") or job.get("id") or "")
        _update_extract(account_id, nonce, {
            "ok": False,
            "status": "running",
            "job_id": job_id,
            "link_type": link_type,
            "message": "提链任务已创建，等待结果",
            "cdk_remaining": job.get("cdk_remaining"),
        })
        for event, data in _iter_sse_events(job_id=job_id, cdk=cdk):
            last_event = {"event": event, "data": data}
            if event == "log":
                msg = str((data or {}).get("message") or "")[:300]
                if msg:
                    logs.append(msg)
                    _update_extract(account_id, nonce, {
                        "ok": False,
                        "status": "running",
                        "job_id": job_id,
                        "link_type": link_type,
                        "message": msg,
                    })
            elif event == "result":
                result = (data or {}).get("result") if isinstance(data, dict) else None
                if not isinstance(result, dict):
                    result = {}
                final = {"ok": True, "status": "success", "job_id": job_id, "link_type": link_type, "result": result, "logs": logs}
                _update_extract(account_id, nonce, final)
                logger.info("[提链] 成功: %s type=%s job=%s", email, link_type, job_id)
                return final
            elif event == "error":
                msg = _extract_error_message(data)
                raise RuntimeError(msg or "提链任务失败")
            elif event == "done":
                break
        raise RuntimeError(f"提链事件流结束但未返回 result: {last_event}")
    except Exception as exc:
        reason = _format_failure_reason(exc, logs=logs, last_event=last_event)
        result = {
            "ok": False,
            "status": "failed",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": reason,
            "message": reason,
        }
        try:
            _update_extract(account_id, nonce, result)
        except Exception:
            logger.exception("[提链] 写入失败状态异常: account_id=%s", account_id)
        logger.exception("[提链] 失败: %s", email)
        return result


def _djb_item_for_email(snapshot: dict, email: str) -> dict:
    items = snapshot.get("items") if isinstance(snapshot, dict) else None
    if not isinstance(items, list):
        return {}
    target = str(email or "").strip().lower()
    for item in items:
        if isinstance(item, dict) and str(item.get("email") or "").strip().lower() == target:
            return item
    return items[0] if len(items) == 1 and isinstance(items[0], dict) else {}


def _map_djb_result(item: dict, link_type: str) -> dict:
    """把 DJB 单账号 item 转成当前账号表使用的提链结果。"""
    item = item if isinstance(item, dict) else {}
    link = str(item.get("link") or "").strip()
    result = {
        "long_url": link,
        "copy_paste": link,
        "payment_method": str(item.get("orderType") or link_type),
        "payment_link_type": link_type,
    }
    # DJB 当前文档只保证 link；兼容后续服务返回的二维码/过期字段。
    for source, target in (("qrImagePng", "image_url_png"), ("qr_image_png", "image_url_png"),
                           ("qrImageSvg", "image_url_svg"), ("qr_image_svg", "image_url_svg"),
                           ("expiresAt", "expires_at"), ("expires_at", "expires_at")):
        if item.get(source) is not None:
            result[target] = item.get(source)
    return result


def _run_djb_extract(*, account_id: int, email: str, access_token: str, link_type: str, cdk: str, trigger: str, nonce: str, djb_config=None) -> dict:
    from core import djb_client

    try:
        if not db.mark_account_extract_running(account_id, nonce=nonce):
            return {"ok": False, "error": "账号已删除或提链状态已被重置"}
        job = _create_extract_job(token=access_token, email=email, link_type=link_type, cdk=cdk)
        job_id = str(job.get("id") or "").strip()
        if not job_id:
            raise RuntimeError("DJB 创建任务未返回 id")
        _update_extract(account_id, nonce, {
            "ok": False, "status": "running", "job_id": job_id,
            "link_type": link_type, "message": "DJB 任务已创建，等待结果",
        })
        deadline = time.time() + djb_client.task_timeout_seconds()
        while True:
            snapshot = djb_client.get_task(job_id)
            item = _djb_item_for_email(snapshot, email)
            item_status = str(item.get("status") or snapshot.get("status") or "running").lower()
            progress = snapshot.get("progress") if isinstance(snapshot, dict) else {}
            if item_status not in djb_client.DJB_TERMINAL_STATUSES:
                task_status = str(snapshot.get("status") or "running").lower()
                message = f"DJB 提链中：{task_status}"
                if isinstance(progress, dict):
                    done = progress.get("done")
                    total = progress.get("total")
                    if done is not None and total is not None:
                        message += f"（{done}/{total}）"
                _update_extract(account_id, nonce, {
                    "ok": False, "status": "running", "job_id": job_id,
                    "link_type": link_type, "message": message,
                })
            elif item_status in djb_client.DJB_SUCCESS_STATUSES and item.get("link"):
                payload = _map_djb_result(item, link_type)
                final = {
                    "ok": True, "status": "success", "job_id": job_id,
                    "link_type": link_type, "result": payload,
                    "message": "DJB 提链成功",
                }
                _update_extract(account_id, nonce, final)
                logger.info("[提链] DJB 成功: %s type=%s job=%s", email, link_type, job_id)
                return final
            else:
                reason = str(item.get("error") or snapshot.get("error") or f"DJB 任务状态：{item_status}")[:500]
                raise RuntimeError(reason)
            if time.time() >= deadline:
                raise RuntimeError("DJB 提链任务超时")
            time.sleep(djb_client.poll_interval_seconds())
    except Exception as exc:
        reason = _format_failure_reason(exc)
        result = {
            "ok": False, "status": "failed",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": reason, "message": reason,
        }
        try:
            _update_extract(account_id, nonce, result)
        except Exception:
            logger.exception("[提链] 写入失败状态异常: account_id=%s", account_id)
        logger.exception("[提链] DJB 失败: %s", email)
        return result


def _run_pay153_extract(*, account_id: int, email: str, access_token: str, link_type: str, cdk: str, trigger: str, nonce: str) -> dict:
    logs: list[str] = []
    try:
        if not db.mark_account_extract_running(account_id, nonce=nonce):
            return {"ok": False, "error": "账号已删除或提链状态已被重置"}
        job = _create_pay153_job(token=access_token, link_type=link_type)
        job_id = str(job.get("job_id") or "")
        _update_extract(account_id, nonce, {
            "ok": False,
            "status": "running",
            "job_id": job_id,
            "link_type": link_type,
            "message": "提链任务已创建，等待结果",
        })
        poll_interval = max(0.5, _int_setting("PAY153_POLL_INTERVAL_MS", 1200, 500, 10000) / 1000.0)
        poll_timeout = _int_setting("PAY153_POLL_TIMEOUT", 900, 60, 7200)
        deadline = time.time() + poll_timeout
        while True:
            if time.time() > deadline:
                raise RuntimeError("pay153 提链任务超时")
            data = _pay153_progress(job_id)
            status = str((data or {}).get("status") or "running").lower()
            text = str((data or {}).get("text") or "")
            if text and text not in logs[-1:]:
                logs.append(text[:300])
                _update_extract(account_id, nonce, {
                    "ok": False,
                    "status": "running",
                    "job_id": job_id,
                    "link_type": link_type,
                    "message": text[:300],
                })
            for item in (data or {}).get("logs") or []:
                msg = str((item or {}).get("message") or "")[:300]
                if msg and msg not in logs[-1:]:
                    logs.append(msg)
            if status == "done":
                result = (data or {}).get("result") or {}
                if not isinstance(result, dict):
                    result = {}
                payload = _map_pay153_result(result, link_type)
                final = {
                    "ok": True, "status": "success", "job_id": job_id,
                    "link_type": payload.get("payment_link_type") or link_type,
                    "result": payload, "logs": logs,
                }
                _update_extract(account_id, nonce, final)
                logger.info("[提链] pay153 成功: %s type=%s job=%s", email, link_type, job_id)
                return final
            if status in {"error", "cancelled"}:
                reason = str((data or {}).get("error") or text or "提链任务失败")
                raise RuntimeError(reason)
            time.sleep(poll_interval)
    except Exception as exc:
        reason = _format_failure_reason(exc, logs=logs)
        result = {
            "ok": False,
            "status": "failed",
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "error": reason,
            "message": reason,
        }
        try:
            _update_extract(account_id, nonce, result)
        except Exception:
            logger.exception("[提链] 写入失败状态异常: account_id=%s", account_id)
        logger.exception("[提链] pay153 失败: %s", email)
        return result
    finally:
        _QUEUE_SLOTS.release()


def enqueue_account_extract(*, account_id: int, email: str, access_token: str, trigger: str = "manual", link_type: str | None = None, cdk: str | None = None) -> dict:
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "error": "提链队列已满"}
    try:
        backend = _backend()
        lt = _link_type(link_type, backend=backend)
        code = _cdk(cdk)
        djb_config = None
        if backend == "djbnb":
            from core import djb_client
            djb_config = djb_client.capture_config(card_code=code)
        nonce = db.claim_account_extract(account_id, trigger=trigger, link_type=lt)
        if not nonce:
            _QUEUE_SLOTS.release()
            return {"accepted": False, "busy": True, "error": "该账号正在提链中"}
        fut = _EXECUTOR.submit(
            _run_extract,
            account_id=account_id, email=email, access_token=access_token,
            link_type=lt, cdk=code, trigger=trigger, nonce=str(nonce),
            backend=backend, djb_config=djb_config,
        )
        return {"accepted": True, "busy": False, "future": fut, "link_type": lt}
    except Exception:
        _QUEUE_SLOTS.release()
        raise
