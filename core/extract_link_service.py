# -*- coding: utf-8 -*-
"""Background extraction service for the legacy extractor and Lumen Flow."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

try:
    from curl_cffi import requests as curl_requests
except Exception:
    curl_requests = None

from config import extract_link as cfg
from core import db
from core import extract_provider_store as provider_store
from core.lumen_flow_client import LumenClient, LumenError

logger = logging.getLogger(__name__)
SUPPORTED_LINK_TYPES = {"pix", "upi", "kakao_pay", "ideal"}
LUMEN_LINK_TYPES = {"ideal", "upi", "pix", "paypal", "kakao_pay", "momo", "blik", "twint", "gcash", "gopay"}


def _runtime_setting(name: str, default=None):
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


def _link_type(value: str | None = None, *, provider_type: str = "extract") -> str:
    choices = LUMEN_LINK_TYPES if provider_type == "lumen" else SUPPORTED_LINK_TYPES
    default = "ideal" if provider_type == "lumen" else _runtime_setting("EXTRACT_LINK_TYPE", "pix")
    value = str(value or default or "pix").strip().lower()
    if value not in choices:
        raise ValueError("提链类型无效")
    return value


def _api_base() -> str:
    base = str(_runtime_setting("EXTRACT_LINK_API_BASE", "") or "").strip().rstrip("/")
    if not base:
        raise ValueError("EXTRACT_LINK_API_BASE 为空")
    return base


def _cdk(value: str | None = None) -> str:
    code = str(value or _runtime_setting("EXTRACT_LINK_CDK", "") or "").strip()
    if not code:
        raise ValueError("EXTRACT_LINK_CDK/CDK 为空")
    return code


_WORKERS = _int_setting("EXTRACT_LINK_WORKERS", 3, 1, 16)
_QUEUE_LIMIT = _int_setting("EXTRACT_LINK_QUEUE_LIMIT", 500, _WORKERS, 5000)
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="extract-link")
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)


def queue_settings() -> dict:
    return {"workers": _WORKERS, "queue_limit": _QUEUE_LIMIT}


def _session():
    return curl_requests.Session() if curl_requests is not None else None


def _legacy_query_cdk(code: str) -> dict:
    base = _api_base()
    timeout = _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300)
    session = _session()
    try:
        if session is None:
            req = Request(f"{base}/api/cdk?{urlencode({'code': code})}", headers={"Accept": "application/json"})
            with urlopen(req, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8", "replace") or "{}")
        else:
            response = session.get(f"{base}/api/cdk?{urlencode({'code': code})}", timeout=timeout)
            try:
                payload = response.json()
            except Exception:
                payload = {"error": (response.text or "")[:300]}
            if response.status_code < 200 or response.status_code >= 300:
                raise RuntimeError(payload.get("error") or f"HTTP {response.status_code}")
        return payload if isinstance(payload, dict) else {}
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass


def _legacy_create_job(*, token: str, link_type: str, cdk: str) -> dict:
    base = _api_base()
    timeout = _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300)
    payload = {"link_type": _link_type(link_type), "cdk": cdk, "token": token}
    session = _session()
    try:
        if session is None:
            request = Request(f"{base}/api/extract", data=json.dumps(payload).encode(),
                              headers={"Accept": "application/json", "Content-Type": "application/json"}, method="POST")
            with urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8", "replace") or "{}")
        else:
            response = session.post(f"{base}/api/extract", json=payload, timeout=timeout)
            try:
                data = response.json()
            except Exception:
                data = {"error": (response.text or "")[:300]}
            if response.status_code < 200 or response.status_code >= 300:
                raise RuntimeError(data.get("error") or f"HTTP {response.status_code}")
        if not isinstance(data, dict) or not data.get("job_id"):
            raise RuntimeError("提链服务未返回 job_id")
        return data
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass


def _iter_sse_events(*, job_id: str, cdk: str):
    base = _api_base()
    timeout = _int_setting("EXTRACT_LINK_EVENT_TIMEOUT", 180, 30, 900)
    url = f"{base}/api/jobs/{quote(job_id, safe='')}/events?{urlencode({'cdk': cdk})}"
    session = _session()
    try:
        if session is None:
            request = Request(url, headers={"Accept": "text/event-stream"})
            response_context = urlopen(request, timeout=timeout)
        else:
            response_context = session.get(url, headers={"Accept": "text/event-stream"}, timeout=timeout, stream=True)
        with response_context as response:
            if session is not None and (response.status_code < 200 or response.status_code >= 300):
                raise RuntimeError(f"HTTP {response.status_code}")
            event, data_lines = "message", []
            for raw in response:
                line = raw.decode("utf-8", "replace").rstrip("\r\n") if isinstance(raw, bytes) else str(raw).rstrip("\r\n")
                if line == "":
                    if data_lines:
                        text = "\n".join(data_lines)
                        try:
                            data = json.loads(text)
                        except Exception:
                            data = {"raw": text}
                        yield event, data
                    event, data_lines = "message", []
                elif line.startswith("event:"):
                    event = line[6:].strip() or "message"
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            if data_lines:
                try:
                    yield event, json.loads("\n".join(data_lines))
                except Exception:
                    yield event, {"raw": "\n".join(data_lines)}
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass


def _extract_error_message(data) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data.strip()
    if not isinstance(data, dict):
        return str(data)
    error = data.get("error")
    if isinstance(error, dict):
        for key in ("message", "detail", "reason", "error", "msg", "description"):
            if error.get(key):
                return str(error[key]).strip()
    if error:
        return str(error).strip()
    for key in ("message", "detail", "reason", "msg", "description", "raw"):
        if data.get(key):
            return str(data[key]).strip()
    return json.dumps(data, ensure_ascii=False)[:500]


def _format_failure_reason(exc: Exception, logs=None, last_event=None) -> str:
    reason = f"{type(exc).__name__}: {str(exc)}".strip()
    if not str(exc).strip() and logs:
        reason = str(logs[-1])
    if last_event and "事件流结束" in reason:
        extracted = _extract_error_message(last_event.get("data"))
        if extracted:
            reason = f"事件流结束；最后事件 {last_event.get('event')}: {extracted}"
    return reason[:500]


def _legacy_run_extract(*, account_id, email, access_token, link_type, cdk, trigger, metadata=None):
    logs, last_event = [], None
    try:
        if not db.mark_account_extract_running(account_id):
            return {"ok": False, "error": "账号已删除或提链状态已被重置"}
        job = _legacy_create_job(token=access_token, link_type=link_type, cdk=cdk)
        job_id = str(job.get("job_id") or "")
        db.update_account_extract(account_id, {"ok": False, "status": "running", "job_id": job_id,
            "link_type": link_type, "message": "提链任务已创建，等待结果", "cdk_remaining": job.get("cdk_remaining"), **(metadata or {})})
        for event, data in _iter_sse_events(job_id=job_id, cdk=cdk):
            last_event = {"event": event, "data": data}
            if event == "log":
                msg = str((data or {}).get("message") or "")[:300]
                if msg:
                    logs.append(msg)
                    db.update_account_extract(account_id, {"ok": False, "status": "running", "job_id": job_id, "link_type": link_type, "message": msg})
            elif event == "result":
                result = data.get("result") if isinstance(data, dict) else {}
                result = result if isinstance(result, dict) else {}
                final = {"ok": True, "status": "success", "job_id": job_id, "link_type": link_type, "result": result, "logs": logs, **(metadata or {})}
                db.update_account_extract(account_id, final)
                return final
            elif event == "error":
                raise RuntimeError(_extract_error_message(data) or "提链任务失败")
            elif event == "done":
                break
        raise RuntimeError(f"提链事件流结束但未返回 result: {last_event}")
    except Exception as exc:
        reason = _format_failure_reason(exc, logs, last_event)
        result = {"ok": False, "status": "failed", "checked_at": datetime.now().isoformat(timespec="seconds"), "error": reason, "message": reason, **(metadata or {})}
        try:
            db.update_account_extract(account_id, result)
        except Exception:
            logger.exception("写入提链失败状态异常: account_id=%s", account_id)
        return result
    finally:
        _QUEUE_SLOTS.release()


def _lumen_result(task: dict, link_type: str) -> dict:
    artifact = task.get("paymentArtifact") if isinstance(task.get("paymentArtifact"), dict) else {}
    result = task.get("result") if isinstance(task.get("result"), dict) else {}
    nested = result.get("paymentArtifact") if isinstance(result.get("paymentArtifact"), dict) else {}
    if not artifact:
        artifact = nested or result
    def first(*keys):
        for source in (artifact, result, task):
            for key in keys:
                if source.get(key) is not None:
                    return source.get(key)
        return None
    return {
        "long_url": first("url", "processorUrl", "hostedUrl", "longUrl"),
        "copy_paste": first("data", "copyPaste", "copy_paste", "url", "hostedUrl"),
        "image_url_png": first("imageUrlPng", "image_url_png", "pngUrl"),
        "image_url_svg": first("imageUrlSvg", "image_url_svg", "svgUrl"),
        "payment_method": first("paymentMethod", "paymentMethodType") or link_type,
        "payment_link_type": first("paymentLinkType", "linkType"),
        "expires_at": first("expiresAt", "expires_at"),
        "payment_status": first("paymentStatus", "payment_state", "paymentState"),
        "raw": task,
    }


def _lumen_state(task: dict, link_type: str, *, provider_meta=None) -> dict:
    upstream = str(task.get("status") or "").upper()
    statuses = {"QUEUED": "queued", "RUNNING": "running", "AWAITING_BLIK_CODE": "awaiting_blik", "SUCCEEDED": "success", "FAILED": "failed", "CANCELED": "stopped"}
    status = statuses.get(upstream, "unknown")
    progress = task.get("progress")
    if isinstance(progress, dict):
        progress = progress.get("percent") or progress.get("percentage")
    try:
        progress = int(progress) if progress is not None else None
    except (TypeError, ValueError):
        progress = None
    result = {"ok": status == "success", "status": status, "task_id": task.get("taskId"),
              "job_id": task.get("taskId"), "link_type": link_type, "awaiting_blik": status == "awaiting_blik",
              "progress": progress, "message": task.get("message") or task.get("error") or ("等待 BLIK 验证码" if status == "awaiting_blik" else None),
              "error": task.get("error") if status in {"failed", "unknown"} else None,
              "payment_status": task.get("paymentStatus") or task.get("payment_state"), **(provider_meta or {})}
    if status == "success":
        result["result"] = _lumen_result(task, link_type)
    return result


def _run_lumen_extract(*, account_id, email, access_token, link_type, cdk, trigger, provider, cdk_id=None, proxy_url=None, payment_amount=0):
    meta = {"provider_id": provider["id"], "provider_type": "lumen", "provider_name": provider["name"], "cdk_id": cdk_id, "cdk_suffix": cdk[-4:] if len(cdk) > 4 else ""}
    client = LumenClient(provider["api_base"], _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300))
    try:
        if not db.mark_account_extract_running(account_id):
            return {"accepted": False, "ok": False, "error": "账号已删除或提链状态已被重置"}
        options = {"maxAmountCents": int(payment_amount or 0)}
        if proxy_url:
            options["proxyUrl"] = str(proxy_url).strip()
        task = client.submit(cdk=cdk, access_token=access_token, payment_method=link_type.upper(), options=options)
        task_id = str(task.get("taskId"))
        state = _lumen_state(task, link_type, provider_meta=meta)
        db.update_account_extract(account_id, state)
        interval = max(1, min(60, float(_runtime_setting("EXTRACT_LINK_POLL_INTERVAL", 5) or 5)))
        deadline = time.monotonic() + _int_setting("EXTRACT_LINK_FLOW_TIMEOUT", 1800, 60, 86400)
        while state["status"] not in {"success", "failed", "stopped"}:
            if time.monotonic() >= deadline:
                raise LumenError("Lumen 任务轮询超时；请刷新原任务核对，不要直接重提", uncertain=True)
            time.sleep(interval)
            task = client.get_task(task_id)
            state = _lumen_state(task, link_type, provider_meta=meta)
            db.update_account_extract(account_id, state)
        return state
    except LumenError as exc:
        status = "unknown" if exc.uncertain else "failed"
        result = {"ok": False, "status": status, "error": str(exc), "message": str(exc), **meta}
        db.update_account_extract(account_id, result)
        return result
    except Exception as exc:
        result = {"ok": False, "status": "unknown", "error": _format_failure_reason(exc), "message": "Lumen 请求异常，请先刷新核对原任务", **meta}
        db.update_account_extract(account_id, result)
        return result
    finally:
        _QUEUE_SLOTS.release()


def _legacy_provider() -> dict:
    base = _api_base()
    code = _runtime_setting("EXTRACT_LINK_CDK", "")
    return {"id": 0, "name": "Legacy Extractor", "provider_type": "extract", "api_base": base,
            "default_link_type": _link_type(), "enabled": True, "is_default": False, "note": "由旧版环境变量配置",
            "has_configured_cdk": bool(str(code or "").strip()), "cdks": []}


def list_providers() -> list[dict]:
    provider_store.ensure_defaults()
    items = provider_store.list_providers()
    try:
        legacy = _legacy_provider()
    except ValueError:
        legacy = {"id": 0, "name": "Legacy Extractor", "provider_type": "extract", "api_base": "", "default_link_type": "pix", "enabled": False, "is_default": False, "note": "未配置旧版环境变量", "has_configured_cdk": False, "cdks": []}
    if legacy["enabled"] or legacy.get("has_configured_cdk"):
        items.insert(0, legacy)
    return items


def _resolve_provider(provider_id=None) -> dict:
    if provider_id in (None, 0, "0"):
        return _legacy_provider()
    try:
        provider_id = int(provider_id)
    except (TypeError, ValueError):
        raise ValueError("provider_id 无效") from None
    provider = provider_store.get_provider(provider_id)
    if not provider:
        raise LookupError("供应商不存在")
    if not provider["enabled"]:
        raise ValueError("供应商已停用")
    return provider


def _resolve_cdk(provider: dict, *, cdk=None, cdk_id=None) -> tuple[str, int | None]:
    if cdk_id is not None:
        try:
            raw = provider_store.get_cdk(int(cdk_id))
        except (TypeError, ValueError):
            raise ValueError("cdk_id 无效") from None
        if not raw or int(raw["provider_id"]) != int(provider.get("id") or -1):
            raise LookupError("CDK 不存在或不属于该供应商")
        if not raw["enabled"]:
            raise ValueError("CDK 已停用")
        return raw["cdk"], raw["id"]
    if cdk is not None and str(cdk).strip():
        return str(cdk).strip(), None
    if provider.get("id") == 0:
        return _cdk(), None
    raise ValueError("请选择一个启用的 CDK")


def query_cdk(*, provider_id=None, cdk_id=None, cdk=None) -> dict:
    provider = _resolve_provider(provider_id)
    code, _ = _resolve_cdk(provider, cdk=cdk, cdk_id=cdk_id)
    if provider["provider_type"] == "lumen":
        return LumenClient(provider["api_base"], _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300)).validate_cdk(code)
    return _legacy_query_cdk(code)


def enqueue_account_extract(*, account_id: int, email: str, access_token: str, trigger: str = "manual",
                             link_type: str | None = None, cdk: str | None = None, provider_id=None,
                             cdk_id=None, proxy_url: str | None = None, payment_amount: int = 0) -> dict:
    if not _QUEUE_SLOTS.acquire(blocking=False):
        return {"accepted": False, "busy": False, "error": "提链队列已满"}
    try:
        provider = _resolve_provider(provider_id)
        lt = _link_type(link_type or provider.get("default_link_type"), provider_type=provider["provider_type"])
        code, resolved_cdk_id = _resolve_cdk(provider, cdk=cdk, cdk_id=cdk_id)
        try:
            amount = max(0, int(payment_amount or 0))
        except (TypeError, ValueError):
            raise ValueError("payment_amount 必须是非负整数") from None
        suffix = code[-4:] if len(code) > 4 else ""
        if not db.claim_account_extract(account_id, trigger=trigger, link_type=lt, provider_id=provider["id"], provider_type=provider["provider_type"], provider_name=provider["name"], cdk_id=resolved_cdk_id, cdk_suffix=suffix):
            _QUEUE_SLOTS.release()
            return {"accepted": False, "busy": True, "error": "该账号正在提链中"}
        meta = {"provider_id": provider["id"], "provider_type": provider["provider_type"], "provider_name": provider["name"], "cdk_id": resolved_cdk_id, "cdk_suffix": suffix}
        if provider["provider_type"] == "lumen":
            future = _EXECUTOR.submit(_run_lumen_extract, account_id=account_id, email=email, access_token=access_token, link_type=lt, cdk=code, trigger=trigger, provider=provider, cdk_id=resolved_cdk_id, proxy_url=proxy_url, payment_amount=amount)
        else:
            future = _EXECUTOR.submit(_legacy_run_extract, account_id=account_id, email=email, access_token=access_token, link_type=lt, cdk=code, trigger=trigger, metadata=meta)
        return {"accepted": True, "busy": False, "future": future, "link_type": lt, **meta}
    except Exception:
        _QUEUE_SLOTS.release()
        raise


def task_action(account_id: int, action: str, *, blik_code: str | None = None) -> dict:
    account = db.get_account(int(account_id))
    if not account:
        raise LookupError("账号不存在")
    provider_type = str(account.get("extract_link_provider_type") or "").lower()
    if provider_type != "lumen":
        raise RuntimeError("旧版提链任务不支持该操作")
    task_id = account.get("extract_link_task_id") or account.get("extract_link_job_id")
    if not task_id:
        raise RuntimeError("任务没有可核对的 Lumen taskId")
    provider = _resolve_provider(account.get("extract_link_provider_id"))
    client = LumenClient(provider["api_base"], _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300))
    link_type = str(account.get("extract_link_type") or provider.get("default_link_type") or "ideal").lower()
    if action == "refresh":
        task = client.get_task(task_id)
    elif action == "cancel":
        task = client.cancel(task_id)
    elif action == "blik-code":
        task = client.blik_code(task_id, str(blik_code or ""))
    else:
        raise ValueError("操作无效")
    state = _lumen_state(task, link_type, provider_meta={"provider_id": provider["id"], "provider_type": "lumen", "provider_name": provider["name"]})
    db.update_account_extract(account_id, state)
    return state
