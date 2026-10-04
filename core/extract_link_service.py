# -*- coding: utf-8 -*-
"""Background extraction for legacy, Lumen Flow and UPI-GIT5 providers."""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from datetime import datetime
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

try:
    from curl_cffi import requests as curl_requests
except Exception:
    curl_requests = None

from config import extract_link as cfg
from core import db, operation_log, task_control
from core import extract_provider_store as provider_store
from core.lumen_flow_client import LumenClient, LumenError
from core.upi_git5_client import UpiGit5Client, UpiGit5Error, PAYMENT_PROVIDERS, _safe_message

logger = logging.getLogger(__name__)
SUPPORTED_LINK_TYPES = {"pix", "upi", "kakao_pay", "ideal"}
LUMEN_LINK_TYPES = {"ideal", "upi", "pix", "paypal", "kakao_pay", "momo", "blik", "twint", "gcash", "gopay"}
UPI_GIT5_LINK_TYPES = {"upi"}
_LEGACY_REJECTION_CODES = {400, 401, 402, 403, 404, 422, 429}


class _LegacyRejected(RuntimeError):
    """The extractor explicitly rejected the request or reported task failure."""


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
    choices = {
        "lumen": LUMEN_LINK_TYPES,
        "upi_git5": UPI_GIT5_LINK_TYPES,
        "extract": SUPPORTED_LINK_TYPES,
    }.get(provider_type, SUPPORTED_LINK_TYPES)
    default = {
        "lumen": "ideal",
        "upi_git5": "upi",
    }.get(provider_type, _runtime_setting("EXTRACT_LINK_TYPE", "pix"))
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


KIND = "extract_link"
_MAX_WORKERS = task_control.MAX_WORKERS
_WORKERS = _int_setting("EXTRACT_LINK_WORKERS", 3, 1, _MAX_WORKERS)
_QUEUE_LIMIT = _int_setting("EXTRACT_LINK_QUEUE_LIMIT", 500, _WORKERS, 5000)
_EXECUTOR = task_control.register_pool(KIND, _WORKERS, max_workers=_MAX_WORKERS)
_QUEUE_SLOTS = threading.BoundedSemaphore(_QUEUE_LIMIT)


def apply_settings() -> dict:
    """热加载提链并发数并立即对运行中的批次生效。"""
    global _WORKERS, _QUEUE_LIMIT
    _WORKERS = _int_setting("EXTRACT_LINK_WORKERS", 3, 1, _MAX_WORKERS)
    _QUEUE_LIMIT = _int_setting("EXTRACT_LINK_QUEUE_LIMIT", 500, _WORKERS, 5000)
    _EXECUTOR.set_workers(_WORKERS)
    return queue_settings()


def queue_settings() -> dict:
    return {"workers": _WORKERS, "queue_limit": _QUEUE_LIMIT}


def _cancelled_state(message: str = "用户取消提链") -> dict:
    return {"ok": False, "status": "cancelled", "error": message, "message": message,
            "checked_at": datetime.now().isoformat(timespec="seconds")}


def _cancel_pending_extract(account_id: int) -> None:
    """排队中的提链任务被取消：释放队列槽位并写回取消状态。"""
    try:
        _QUEUE_SLOTS.release()
    except ValueError:
        pass
    try:
        db.update_account_extract(int(account_id), _cancelled_state())
    except Exception:
        logger.exception("写入提链取消状态异常: account_id=%s", account_id)
    task_control.release(KIND, account_id)


def _session():
    return curl_requests.Session() if curl_requests is not None else None


def _legacy_query_cdk(code: str, *, api_base: str | None = None) -> dict:
    base = api_base or _api_base()
    timeout = _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300)
    url = f"{base}/api/cdk?{urlencode({'code': code})}"
    session = _session()
    started = time.monotonic()
    operation_log.request(method="GET", url=url, timeout=timeout, note="Legacy 提链 CDK 校验", secrets=(code,))
    try:
        if session is None:
            req = Request(url, headers={"Accept": "application/json"})
            with urlopen(req, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8", "replace") or "{}")
                status = getattr(response, "status", 200)
        else:
            response = session.get(url, timeout=timeout)
            status = response.status_code
            try:
                payload = response.json()
            except Exception:
                payload = {"error": (response.text or "")[:300]}
            if response.status_code < 200 or response.status_code >= 300:
                operation_log.response(status=status, headers=dict(response.headers),
                                       elapsed=time.monotonic() - started, body=payload, secrets=(code,))
                error = RuntimeError(payload.get("error") or f"HTTP {response.status_code}")
                operation_log.failure(error, note="Legacy CDK 校验被拒绝", secrets=(code,))
                raise error
        operation_log.response(status=status, elapsed=time.monotonic() - started, body=payload, secrets=(code,))
        return payload if isinstance(payload, dict) else {}
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass


def _legacy_create_job(*, token: str, link_type: str, cdk: str, api_base: str | None = None) -> dict:
    base = api_base or _api_base()
    timeout = _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300)
    payload = {"link_type": _link_type(link_type), "cdk": cdk, "token": token}
    url = f"{base}/api/extract"
    session = _session()
    started = time.monotonic()
    operation_log.request(method="POST", url=url, headers={"Accept": "application/json", "Content-Type": "application/json"},
                          body=payload, timeout=timeout, note="Legacy 创建提链任务", secrets=(cdk, token))
    try:
        if session is None:
            request = Request(url, data=json.dumps(payload).encode(),
                              headers={"Accept": "application/json", "Content-Type": "application/json"}, method="POST")
            with urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode("utf-8", "replace") or "{}")
                status = getattr(response, "status", 200)
        else:
            response = session.post(url, json=payload, timeout=timeout)
            status = response.status_code
            try:
                data = response.json()
            except Exception:
                data = {"error": (response.text or "")[:300]}
            if response.status_code < 200 or response.status_code >= 300:
                operation_log.response(status=status, headers=dict(response.headers),
                                       elapsed=time.monotonic() - started, body=data, secrets=(cdk, token))
                error_type = _LegacyRejected if response.status_code in _LEGACY_REJECTION_CODES else RuntimeError
                error = error_type(data.get("error") or f"HTTP {response.status_code}")
                operation_log.failure(error, note="Legacy 提链任务被拒绝", secrets=(cdk, token))
                raise error
        operation_log.response(status=status, elapsed=time.monotonic() - started, body=data, secrets=(cdk, token))
        if not isinstance(data, dict) or not data.get("job_id"):
            error = RuntimeError("提链服务未返回 job_id")
            operation_log.failure(error, note="提链服务返回缺少 job_id", secrets=(cdk, token))
            raise error
        return data
    except HTTPError as exc:
        operation_log.failure(exc, note=f"Legacy 提链 HTTP {exc.code}", secrets=(cdk, token))
        if exc.code in _LEGACY_REJECTION_CODES:
            raise _LegacyRejected(f"HTTP {exc.code}") from exc
        raise
    finally:
        if session is not None:
            try:
                session.close()
            except Exception:
                pass


def _iter_sse_events(*, job_id: str, cdk: str, api_base: str | None = None):
    base = api_base or _api_base()
    timeout = _int_setting("EXTRACT_LINK_EVENT_TIMEOUT", 180, 30, 900)
    url = f"{base}/api/jobs/{quote(job_id, safe='')}/events?{urlencode({'cdk': cdk})}"
    session = _session()
    operation_log.request(method="GET", url=url, headers={"Accept": "text/event-stream"}, timeout=timeout,
                          note="Legacy 提链事件流", secrets=(cdk,))
    try:
        if session is None:
            request = Request(url, headers={"Accept": "text/event-stream"})
            response_context = urlopen(request, timeout=timeout)
        else:
            response_context = session.get(url, headers={"Accept": "text/event-stream"}, timeout=timeout, stream=True)
        with response_context as response:
            if session is not None and (response.status_code < 200 or response.status_code >= 300):
                operation_log.failure(RuntimeError(f"HTTP {response.status_code}"), note="提链事件流建立失败", secrets=(cdk,))
                raise RuntimeError(f"HTTP {response.status_code}")
            operation_log.response(status=getattr(response, "status_code", 200), note="提链事件流已建立", secrets=(cdk,))
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
                        operation_log.detail("提链事件", event=event, data=data, secrets=(cdk,))
                        yield event, data
                    event, data_lines = "message", []
                elif line.startswith("event:"):
                    event = line[6:].strip() or "message"
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            if data_lines:
                try:
                    data = json.loads("\n".join(data_lines))
                except Exception:
                    data = {"raw": "\n".join(data_lines)}
                operation_log.detail("提链事件", event=event, data=data, secrets=(cdk,))
                yield event, data
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


def _legacy_run_extract(*, account_id, email, access_token, link_type, cdk, trigger, metadata=None, api_base=None):
    logs, last_event, job_id = [], None, ""
    with operation_log.operation(
            operation_log.EXTRACT, account_id, truncate=False, header="提链（Legacy）",
            secrets=(cdk, access_token), trigger=trigger, provider_type="extract", provider_name=(metadata or {}).get("provider_name"),
            api_base=api_base or "", link_type=link_type, cdk=cdk, email=email,
            access_token=access_token) as log:
        try:
            task_control.checkpoint(KIND, account_id)
            if not db.mark_account_extract_running(account_id):
                log.warn("账号已删除或提链状态已被重置，未执行提链")
                return {"ok": False, "error": "账号已删除或提链状态已被重置"}
            job = _legacy_create_job(token=access_token, link_type=link_type, cdk=cdk, api_base=api_base)
            job_id = str(job.get("job_id") or "")
            log.step("提链任务已创建", job_id=job_id, cdk_remaining=job.get("cdk_remaining"))
            db.update_account_extract(account_id, {"ok": False, "status": "running", "job_id": job_id,
                "link_type": link_type, "message": "提链任务已创建，等待结果", "cdk_remaining": job.get("cdk_remaining"), **(metadata or {})})
            for event, data in _iter_sse_events(job_id=job_id, cdk=cdk, api_base=api_base):
                task_control.checkpoint(KIND, account_id)
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
                    log.step("提链成功", job_id=job_id, link_type=link_type, result=result, logs=logs)
                    db.update_account_extract(account_id, final)
                    return final
                elif event == "error":
                    error = _LegacyRejected(_extract_error_message(data) or "提链任务失败")
                    log.failure(error, note="提链事件流返回失败", data=data, logs=logs)
                    raise error
                elif event == "done":
                    log.detail("提链事件流结束", last_event=last_event, logs=logs)
                    break
            error = RuntimeError(f"提链事件流结束但未返回 result: {last_event}")
            log.failure(error, note="事件流结束但没有结果")
            raise error
        except task_control.TaskCancelled:
            state = {**_cancelled_state(), **(metadata or {})}
            log.warn("用户取消提链，已停止本地跟踪", job_id=job_id)
            try:
                db.update_account_extract(account_id, state)
            except Exception:
                logger.exception("写入提链取消状态异常: account_id=%s", account_id)
            return state
        except Exception as exc:
            reason = _format_failure_reason(exc, logs, last_event)
            status = "failed" if isinstance(exc, _LegacyRejected) else "unknown"
            result = {"ok": False, "status": status, "checked_at": datetime.now().isoformat(timespec="seconds"), "error": reason, "message": reason, **(metadata or {})}
            log.failure(exc, note=f"提链结束（{status}）", reason=reason, job_id=job_id, logs=logs)
            try:
                db.update_account_extract(account_id, result)
            except Exception:
                log.failure(RuntimeError("写入提链状态失败"), note="本地状态写入异常")
                logger.exception("写入提链失败状态异常: account_id=%s", account_id)
            return result
        finally:
            _QUEUE_SLOTS.release()
            task_control.release(KIND, account_id)


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
        "long_url": first("hostedInstructionsUrl", "url", "processorUrl", "hostedUrl", "longUrl"),
        "hosted_instructions_url": first("hostedInstructionsUrl", "hosted_instructions_url"),
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
    with operation_log.operation(
            operation_log.EXTRACT, account_id, truncate=False, header="提链（Lumen Flow）",
            secrets=(cdk, access_token, proxy_url), trigger=trigger, provider_name=provider["name"], api_base=provider["api_base"],
            link_type=link_type, cdk=cdk, cdk_id=cdk_id, proxy_url=proxy_url,
            payment_amount=payment_amount, email=email, access_token=access_token) as log:
        client = LumenClient(provider["api_base"], _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300))
        task_id = ""
        try:
            task_control.checkpoint(KIND, account_id)
            if not db.mark_account_extract_running(account_id):
                log.warn("账号已删除或提链状态已被重置，未执行提链")
                return {"accepted": False, "ok": False, "error": "账号已删除或提链状态已被重置"}
            options = {"maxAmountCents": int(payment_amount or 0)}
            if proxy_url:
                options["proxyUrl"] = str(proxy_url).strip()
            log.step("提交 Lumen Flow 提链请求", payment_method=link_type.upper(), options=options)
            task = client.submit(cdk=cdk, access_token=access_token, payment_method=link_type.upper(), options=options)
            task_id = str(task.get("taskId"))
            state = _lumen_state(task, link_type, provider_meta=meta)
            log.step("Lumen Flow 已受理", task_id=task_id, status=state.get("status"),
                     message=state.get("message"), progress=state.get("progress"))
            db.update_account_extract(account_id, state)
            interval = max(1, min(60, float(_runtime_setting("EXTRACT_LINK_POLL_INTERVAL", 5) or 5)))
            deadline = time.monotonic() + _int_setting("EXTRACT_LINK_FLOW_TIMEOUT", 1800, 60, 86400)
            while state["status"] not in {"success", "failed", "stopped"}:
                task_control.checkpoint(KIND, account_id)
                if time.monotonic() >= deadline:
                    error = LumenError("Lumen 任务轮询超时；请刷新原任务核对，不要直接重提", uncertain=True)
                    log.failure(error, note="轮询超时", task_id=task_id, last_state=state)
                    raise error
                task_control.sleep(KIND, account_id, interval)
                task_control.checkpoint(KIND, account_id)
                task = client.get_task(task_id)
                state = _lumen_state(task, link_type, provider_meta=meta)
                log.detail("Lumen Flow 轮询", task_id=task_id, status=state.get("status"),
                           progress=state.get("progress"), message=state.get("message"),
                           payment_status=state.get("payment_status"))
                db.update_account_extract(account_id, state)
            log.step("提链结束", task_id=task_id, status=state.get("status"),
                     result=state.get("result"), error=state.get("error"))
            return state
        except task_control.TaskCancelled:
            state = _cancelled_state()
            log.warn("用户取消提链，已停止本地跟踪", task_id=task_id)
            db.update_account_extract(account_id, {**state, **meta})
            return {**state, **meta}
        except LumenError as exc:
            status = "unknown" if exc.uncertain else "failed"
            result = {"ok": False, "status": status, "error": str(exc), "message": str(exc), **meta}
            log.failure(exc, note=f"Lumen 提链结束（{status}）")
            db.update_account_extract(account_id, result)
            return result
        except Exception as exc:
            result = {"ok": False, "status": "unknown", "error": _format_failure_reason(exc), "message": "Lumen 请求异常，请先刷新核对原任务", **meta}
            log.failure(exc, note="Lumen 请求异常")
            db.update_account_extract(account_id, result)
            return result
        finally:
            _QUEUE_SLOTS.release()
            task_control.release(KIND, account_id)


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


def _log_enqueue(account_id, provider, link_type, cdk, *, accepted, message="", access_token="",
                 proxy_url=None, trigger="", cdk_id=None):
    """入队/拒绝也写入账号日志，避免「点了没反应」时无迹可查。"""
    with operation_log.operation(
            operation_log.EXTRACT, account_id, truncate=False,
            header="提链任务已入队" if accepted else "提链任务未入队",
            secrets=(cdk, access_token),
            trigger=trigger, provider_id=provider.get("id"), provider_name=provider.get("name"),
            provider_type=provider.get("provider_type"), api_base=provider.get("api_base"),
            link_type=link_type, cdk=cdk, cdk_id=cdk_id, access_token=access_token or None,
            proxy_url=proxy_url) as log:
        if accepted:
            log.step("本地提链队列已接受，后台任务开始执行")
        else:
            log.warn("未入队", reason=message or "账号正在提链或原任务待核对")


def query_cdk(*, provider_id=None, cdk_id=None, cdk=None) -> dict:
    provider = _resolve_provider(provider_id)
    code, _ = _resolve_cdk(provider, cdk=cdk, cdk_id=cdk_id)
    if provider["provider_type"] == "upi_git5":
        from core.upi_git5_extract import redact
        return redact(_upi_git5_client(provider).cdk_status(code), (code,))
    if provider["provider_type"] == "lumen":
        return LumenClient(provider["api_base"], _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300)).validate_cdk(code)
    return _legacy_query_cdk(code, api_base=provider["api_base"])


def enqueue_account_extract(*, account_id: int, email: str, access_token: str, trigger: str = "manual",
                             link_type: str | None = None, cdk: str | None = None, provider_id=None,
                             cdk_id=None, proxy_url: str | None = None, payment_amount: int = 0,
                             payment_provider_id: str | None = None, entry_proxies=None,
                             use_promo=None, promo_campaign=None) -> dict:
    provider = _resolve_provider(provider_id)
    if provider["provider_type"] == "upi_git5":
        result = enqueue_account_extract_bulk(
            accounts=[{"account_id": account_id, "email": email, "access_token": access_token,
                       "proxy_url": proxy_url}], trigger=trigger, link_type=link_type, cdk=cdk,
            provider_id=provider_id, cdk_id=cdk_id, entry_proxies=entry_proxies,
            payment_provider_id=payment_provider_id, use_promo=use_promo, promo_campaign=promo_campaign)
        if result["started"]:
            return {"accepted": True, "busy": False, **result["started"][0]}
        return {"accepted": False, "busy": bool(result["busy"]),
                "error": (result["busy"] or result["failed"] or [{"error": "账号未入队"}])[0]["error"]}
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
            _log_enqueue(account_id, provider, lt, code, accepted=False, trigger=trigger,
                         access_token=access_token, proxy_url=proxy_url, cdk_id=resolved_cdk_id)
            return {"accepted": False, "busy": True, "error": "该账号正在提链中"}
        _log_enqueue(account_id, provider, lt, code, accepted=True, trigger=trigger,
                     access_token=access_token, proxy_url=proxy_url, cdk_id=resolved_cdk_id)
        meta = {"provider_id": provider["id"], "provider_type": provider["provider_type"], "provider_name": provider["name"], "cdk_id": resolved_cdk_id, "cdk_suffix": suffix}
        handle = task_control.control(KIND, account_id)
        on_cancel = lambda: _cancel_pending_extract(account_id)  # noqa: E731
        if provider["provider_type"] == "lumen":
            future = _EXECUTOR.submit(task_control.gated(_run_lumen_extract, handle, on_cancel=on_cancel), account_id=account_id, email=email, access_token=access_token, link_type=lt, cdk=code, trigger=trigger, provider=provider, cdk_id=resolved_cdk_id, proxy_url=proxy_url, payment_amount=amount)
        else:
            future = _EXECUTOR.submit(task_control.gated(_legacy_run_extract, handle, on_cancel=on_cancel), account_id=account_id, email=email, access_token=access_token, link_type=lt, cdk=code, trigger=trigger, metadata=meta, api_base=provider["api_base"])
        if future is False:
            raise RuntimeError("提链队列已关闭")
        return {"accepted": True, "busy": False, "future": future, "link_type": lt, **meta}
    except Exception:
        task_control.release(KIND, account_id)
        _QUEUE_SLOTS.release()
        raise


def task_action(account_id: int, action: str, *, blik_code: str | None = None, cdk=None) -> dict:
    account = db.get_account(int(account_id))
    if not account:
        raise LookupError("账号不存在")
    provider_type = str(account.get("extract_link_provider_type") or "").lower()
    with operation_log.operation(
            operation_log.EXTRACT, account_id, truncate=False, header=f"提链任务操作（{action}）",
            secrets=(cdk, blik_code), provider_type=provider_type, task_id=account.get("extract_link_task_id") or "",
            job_id=account.get("extract_link_job_id") or "",
            link_type=account.get("extract_link_type") or "", cdk=cdk, blik_code=blik_code) as log:
        if provider_type == "upi_git5":
            state = _task_action_upi_git5(account, action, cdk=cdk)
            if action != "qr":
                db.update_account_extract(account_id, state, expected_task_id=account.get("extract_link_task_id"))
            log.step("任务操作完成", action=action, state=_log_safe_state(state))
            return state
        if provider_type != "lumen":
            log.warn("旧版提链任务不支持该操作", action=action)
            raise RuntimeError("旧版提链任务不支持该操作")
        task_id = account.get("extract_link_task_id") or account.get("extract_link_job_id")
        if not task_id:
            log.warn("任务没有可核对的 Lumen taskId")
            raise RuntimeError("任务没有可核对的 Lumen taskId")
        provider = _resolve_provider(account.get("extract_link_provider_id"))
        client = LumenClient(provider["api_base"], _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300))
        link_type = str(account.get("extract_link_type") or provider.get("default_link_type") or "ideal").lower()
        log.step("执行 Lumen 任务操作", action=action, task_id=task_id, api_base=provider["api_base"])
        if action == "refresh":
            task = client.get_task(task_id)
        elif action == "cancel":
            task = client.cancel(task_id)
        elif action == "blik-code":
            task = client.blik_code(task_id, str(blik_code or ""))
        else:
            log.warn("操作无效", action=action)
            raise ValueError("操作无效")
        state = _lumen_state(task, link_type, provider_meta={"provider_id": provider["id"], "provider_type": "lumen", "provider_name": provider["name"]})
        log.step("任务操作完成", action=action, status=state.get("status"), message=state.get("message"),
                 result=state.get("result"))
        db.update_account_extract(account_id, state)
        return state


def _log_safe_state(state):
    """二维码等大字段只记录体积，避免日志被 base64 淹没。"""
    if not isinstance(state, dict):
        return state
    result = {key: value for key, value in state.items() if key != "image_url_png"}
    if isinstance(state.get("image_url_png"), str):
        result["image_url_png_bytes"] = len(state["image_url_png"])
    return result


# UPI-GIT5 batches share one worker and one upstream submission.
def _upi_git5_client(provider):
    return UpiGit5Client(provider["api_base"], _int_setting("EXTRACT_LINK_REQUEST_TIMEOUT", 30, 5, 300))


def _upi_git5_read(client, code, session, method, *args):
    """Only reads may renew a rejected session. Never replay a batch POST."""
    try:
        return getattr(client, method)(session["token"], *args)
    except UpiGit5Error as exc:
        if exc.status != 401:
            raise
        session.update(client.create_session(code))
        return getattr(client, method)(session["token"], *args)


def _upi_git5_state(job, link_type="upi", *, batch_id="", secrets=()):
    from core.upi_git5_extract import state
    return state(job, batch_id=batch_id or job.get("batch_id", ""), secrets=secrets)


def _run_upi_git5_batch(*, entries, cdk, provider, meta, entry_proxies,
                        payment_provider_id=None, use_promo=None, promo_campaign=None):
    from core.upi_git5_extract import bind_jobs, identifier, TERMINAL
    session, batch_id, submitted, batch_saved = {}, "", False, False
    original_ids = [e["account_id"] for e in entries]
    pending = {e["account_id"]: e for e in entries}
    secrets = (cdk, *entry_proxies, *(e["access_token"] for e in entries))
    client = None
    with operation_log.batch_operation(
            operation_log.EXTRACT, [e["account_id"] for e in entries], truncate=False,
            header="批量提链（UPI-GIT5）", secrets=(cdk, *entry_proxies, *(e["access_token"] for e in entries)), provider_name=provider["name"], api_base=provider["api_base"],
            link_type=meta.get("link_type"), cdk=cdk, entry_proxies=list(entry_proxies),
            payment_provider_id=payment_provider_id, use_promo=use_promo, promo_campaign=promo_campaign,
            accounts=[{"id": e["account_id"], "email": e.get("email"), "access_token": e["access_token"]}
                      for e in entries]) as log:
        try:
            client = _upi_git5_client(provider)
            # 任务中心的取消/暂停：批次提交前先剔除已取消的账号。
            cancelled = [e["account_id"] for e in entries
                         if task_control.control_state(KIND, e["account_id"]) == "cancelled"]
            if cancelled:
                for account_id in cancelled:
                    db.update_account_extract(account_id, _cancelled_state("用户取消提链"), preserve_terminal=True)
                    log.warn("账号提链已被用户取消，不再提交批次", account_id=account_id)
                    task_control.release(KIND, account_id)
                entries = [e for e in entries if e["account_id"] not in set(cancelled)]
            entries = [e for e in entries if db.mark_account_extract_running(e["account_id"])]
            pending = {e["account_id"]: e for e in entries}
            if not entries:
                log.warn("所有账号已删除或提链状态已被重置，未提交批次")
                return
            session.update(client.create_session(cdk))
            submitted = True
            log.step("已取得 UPI-GIT5 租户会话", api_base=provider["api_base"],
                     session_token=session.get("token"))
            batch = client.create_batch(
                session_token=session["token"], tokens=[e["access_token"] for e in entries],
                entry_proxies=entry_proxies, link_cdk=cdk, payment_provider_id=payment_provider_id,
                use_promo=use_promo, promo_campaign=promo_campaign)
            batch_id = identifier(batch.get("batch_id"))
            log.step("批次已受理", batch_id=batch_id, account_count=len(entries),
                     jobs_count=len(batch.get("jobs") or []))
            # Save the batch before mapping jobs so malformed responses remain recoverable.
            for entry in entries:
                db.update_account_extract(entry["account_id"], {
                    "status": "running", "task_id": batch_id, "message": "批次已受理，等待提链", **meta})
            batch_saved = True
            binding = bind_jobs(entries, batch.get("jobs"))
            for job_id, entry in binding.items():
                log.step("账号已绑定批次任务", batch_id=batch_id, job_id=job_id,
                         account_id=entry["account_id"], email=entry.get("email"))
                db.update_account_extract(entry["account_id"], {
                    "status": "running", "job_id": job_id, "task_id": batch_id, **meta})
            interval = _int_setting("EXTRACT_LINK_POLL_INTERVAL", 5, 1, 60)
            deadline = time.monotonic() + _int_setting("EXTRACT_LINK_FLOW_TIMEOUT", 1800, 60, 86400)
            while pending:
                if all(task_control.control_state(KIND, account_id) == "paused" for account_id in pending):
                    # 剩余账号全部暂停：保持批次上下文，等待恢复，不计入轮询超时。
                    time.sleep(min(interval, 2))
                    continue
                if time.monotonic() >= deadline:
                    error = UpiGit5Error("批次轮询超时，请刷新原任务核对", uncertain=True)
                    log.failure(error, note="批次轮询超时", batch_id=batch_id, pending=sorted(pending))
                    raise error
                try:
                    snap = _upi_git5_read(client, cdk, session, "get_batch_progress", batch_id)
                except UpiGit5Error as exc:
                    if exc.status == 429 or exc.status >= 500:
                        delay = min(60, max(interval, getattr(exc, "retry_after", None) or interval))
                        log.warn("批次进度读取失败，等待后重试", status=exc.status, delay=delay,
                                 uncertain=exc.uncertain)
                        time.sleep(max(0, min(delay, deadline - time.monotonic())))
                        continue
                    raise
                if not isinstance(snap, dict) or snap.get("batch_id") != batch_id or not isinstance(snap.get("jobs"), list):
                    error = UpiGit5Error("批次进度响应不匹配，请核对原任务", uncertain=True)
                    log.failure(error, note="批次进度响应不匹配", batch_id=batch_id, snapshot=snap)
                    raise error
                jobs = snap["jobs"]
                log.detail("批次进度", batch_id=batch_id, running=snap.get("running"),
                           jobs=[{"id": j.get("id"), "status": j.get("status")} for j in jobs if isinstance(j, dict)])
                # Lightweight progress can omit artifacts. Read full detail before storing done.
                if any(isinstance(j, dict) and j.get("id") in binding and j.get("status") == "done"
                       and not j.get("result") for j in jobs):
                    full = _upi_git5_read(client, cdk, session, "get_batch", batch_id)
                    if full.get("batch_id") != batch_id or not isinstance(full.get("jobs"), list):
                        error = UpiGit5Error("批次详情响应不匹配", uncertain=True)
                        log.failure(error, note="批次详情响应不匹配", batch_id=batch_id)
                        raise error
                    jobs = full["jobs"]
                seen = set()
                for job in jobs:
                    if not isinstance(job, dict):
                        error = UpiGit5Error("批次任务格式无效", uncertain=True)
                        log.failure(error, note="批次任务格式无效", job=job)
                        raise error
                    job_id = job.get("id")
                    if job_id not in binding:
                        continue
                    if job_id in seen or (job.get("batch_id") and job["batch_id"] != batch_id):
                        error = UpiGit5Error("批次任务 ID 冲突", uncertain=True)
                        log.failure(error, note="批次任务 ID 冲突", job_id=job_id, batch_id=batch_id)
                        raise error
                    seen.add(job_id)
                    entry = binding[job_id]
                    if entry["account_id"] not in pending:
                        continue
                    control_state = task_control.control_state(KIND, entry["account_id"])
                    if control_state == "cancelled":
                        state = {**_cancelled_state(), **meta, "task_id": batch_id}
                        db.update_account_extract(entry["account_id"], state,
                                                  expected_task_id=batch_id, preserve_terminal=True)
                        log.warn("账号提链已被用户取消，停止本地跟踪", account_id=entry["account_id"],
                                 job_id=job_id)
                        pending.pop(entry["account_id"], None)
                        task_control.release(KIND, entry["account_id"])
                        continue
                    if control_state == "paused":
                        # 暂停期间不写回状态；恢复后继续跟进同一批次。
                        continue
                    state = _upi_git5_state(job, batch_id=batch_id, secrets=(*secrets, session["token"]))
                    log.step("账号提链状态更新", account_id=entry["account_id"], job_id=job_id,
                             status=state.get("status"), message=state.get("message"),
                             payment_status=state.get("payment_status"), result=state.get("result"))
                    updated = db.update_account_extract(entry["account_id"], {**state, **meta},
                                                        expected_task_id=batch_id, preserve_terminal=True)
                    if not updated or state["status"] in TERMINAL:
                        pending.pop(entry["account_id"], None)
                if not pending:
                    break
                if all(task_control.control_state(KIND, account_id) == "paused" for account_id in pending):
                    # 剩余账号全部暂停：保持批次上下文，等待恢复，不计入轮询超时。
                    time.sleep(min(interval, 2))
                    continue
                if snap.get("running") is False:
                    error = UpiGit5Error("批次已结束，但部分账号结果未确认，请刷新原任务", uncertain=True)
                    log.failure(error, note="批次结束但结果缺失", batch_id=batch_id, pending=sorted(pending))
                    raise error
                time.sleep(min(interval, max(0, deadline - time.monotonic())))
            log.step("批次提链结束", batch_id=batch_id)
        except Exception as exc:
            batch_id = batch_id or getattr(exc, "batch_id", None) or ""
            # Once accepted, a failed read says nothing about the upstream job outcome.
            uncertain = bool(batch_id) or (submitted and (not isinstance(exc, UpiGit5Error) or exc.uncertain))
            message = (_safe_message(str(exc), (*secrets, session.get("token", "")))
                       if isinstance(exc, (UpiGit5Error, ValueError)) else "UPI-GIT5 处理异常，请核对原任务")
            state = {"ok": False, "status": "unknown" if uncertain else "failed",
                     "error": message, "message": message, **meta}
            if batch_id:
                state["task_id"] = batch_id
            log.failure(exc, note=f"批次提链结束（{state['status']}）", batch_id=batch_id,
                        uncertain=uncertain, pending=sorted(pending))
            for account_id in pending:
                db.update_account_extract(account_id, state, preserve_terminal=True,
                                          expected_task_id=batch_id if batch_saved else None)
        finally:
            try:
                if client and session.get("token"):
                    client.logout_session(session["token"])
            except Exception:
                pass
            _QUEUE_SLOTS.release()
            for entry in entries:
                task_control.release(KIND, entry["account_id"])
            for account_id in original_ids:
                task_control.release(KIND, account_id)


def enqueue_account_extract_bulk(*, accounts, trigger="manual_bulk", link_type=None, cdk=None,
                                 provider_id=None, cdk_id=None, entry_proxies=None, payment_amount=0,
                                 payment_provider_id=None, use_promo=None, promo_campaign=None):
    from core.upi_git5_extract import entry_proxies as validate_proxies
    out = {name: [] for name in ("started", "busy", "failed", "skipped")}
    if not isinstance(accounts, list) or not 1 <= len(accounts) <= 500:
        raise ValueError("单次需要 1–500 个账号")
    provider = _resolve_provider(provider_id)
    lt = _link_type(link_type or provider.get("default_link_type"), provider_type=provider["provider_type"])
    if provider["provider_type"] != "upi_git5":
        for a in accounts:
            item = {"id": a["account_id"], "email": a.get("email")}
            try:
                queued = enqueue_account_extract(account_id=a["account_id"], email=a.get("email") or "",
                    access_token=a["access_token"], trigger=trigger, link_type=lt, cdk=cdk,
                    provider_id=provider_id, cdk_id=cdk_id, proxy_url=a.get("proxy_url"), payment_amount=payment_amount)
                item.update({k: v for k, v in queued.items() if k != "future"})
                out["started" if queued.get("accepted") else "busy" if queued.get("busy") else "failed"].append(item)
            except Exception as exc:
                out["failed"].append({**item, "error": str(exc)})
    else:
        if cdk is not None and (not isinstance(cdk, str) or not cdk.strip() or any(c.isspace() for c in cdk.strip())):
            raise ValueError("CDK 必须是非空且不含空白的字符串")
        if cdk is not None and cdk_id is not None:
            raise ValueError("cdk 与 cdk_id 只能提供一个")
        code, resolved_cdk_id = _resolve_cdk(provider, cdk=cdk, cdk_id=cdk_id)
        proxies = validate_proxies(entry_proxies if entry_proxies is not None else
                                   list(dict.fromkeys(a["proxy_url"] for a in accounts if a.get("proxy_url"))))
        if payment_provider_id is not None and payment_provider_id not in PAYMENT_PROVIDERS:
            raise ValueError("支付平台必须是 foarge、xxsyun 或 astrascan")
        if use_promo is not None and type(use_promo) is not bool:
            raise ValueError("use_promo 必须是布尔值")
        if promo_campaign is not None and (not isinstance(promo_campaign, str) or not promo_campaign.strip()):
            raise ValueError("promo_campaign 必须是非空字符串")
        seen = set()
        entries = []
        for a in accounts:
            account_id = a.get("account_id")
            if type(account_id) is not int or account_id <= 0:
                raise ValueError("账号 ID 必须是正整数")
            token = a.get("access_token")
            if not isinstance(token, str) or not token.strip():
                raise ValueError("账号缺少 access_token")
            if account_id not in seen:
                entries.append({**a, "access_token": token.strip()})
                seen.add(account_id)
        meta = {"provider_id": provider["id"], "provider_type": "upi_git5", "provider_name": provider["name"],
                "cdk_id": resolved_cdk_id, "cdk_suffix": code[-4:] if len(code) > 4 else "", "link_type": lt}
        if not _QUEUE_SLOTS.acquire(blocking=False):
            out["failed"] = [{"id": a["account_id"], "email": a.get("email"), "error": "提链队列已满"} for a in entries]
        else:
            claimed = []
            scheduled = False
            try:
                for a in entries:
                    if db.claim_account_extract(a["account_id"], trigger=trigger, link_type=lt,
                        provider_id=provider["id"], provider_type="upi_git5", provider_name=provider["name"],
                        cdk_id=resolved_cdk_id, cdk_suffix=meta["cdk_suffix"]):
                        claimed.append(a)
                        _log_enqueue(a["account_id"], provider, lt, code, accepted=True, trigger=trigger,
                                     access_token=a["access_token"], proxy_url=a.get("proxy_url"),
                                     cdk_id=resolved_cdk_id)
                    else:
                        out["busy"].append({"id": a["account_id"], "email": a.get("email"), "error": "账号正在提链或原任务待核对"})
                        _log_enqueue(a["account_id"], provider, lt, code, accepted=False, trigger=trigger,
                                     message="账号正在提链或原任务待核对", cdk_id=resolved_cdk_id)
                if claimed:
                    for a in claimed:
                        task_control.control(KIND, a["account_id"])
                    accepted = _EXECUTOR.submit(_run_upi_git5_batch, entries=claimed, cdk=code, provider=provider, meta=meta,
                        entry_proxies=proxies, payment_provider_id=payment_provider_id,
                        use_promo=use_promo, promo_campaign=promo_campaign)
                    if accepted is False:
                        raise RuntimeError("提链队列已关闭")
                    scheduled = True
                    out["started"] = [{"id": a["account_id"], "email": a.get("email"), "accepted": True, **meta} for a in claimed]
            except Exception:
                for a in claimed:
                    db.update_account_extract(a["account_id"], {"status": "failed", "error": "本地队列提交失败，批次未创建"})
                    _log_enqueue(a["account_id"], provider, lt, code, accepted=False, trigger=trigger,
                                 message="本地队列提交失败，批次未创建", cdk_id=resolved_cdk_id)
                    task_control.release(KIND, a["account_id"])
                raise
            finally:
                if not scheduled:
                    _QUEUE_SLOTS.release()
                    for a in claimed:
                        task_control.release(KIND, a["account_id"])
    out["ok"] = True
    for name in ("started", "busy", "failed", "skipped"):
        out[name + "_count"] = len(out[name])
    return out


def _task_action_upi_git5(account, action, *, cdk=None):
    from core.upi_git5_extract import identifier
    if action not in {"refresh", "cancel", "qr"}:
        raise ValueError("UPI-GIT5 不支持该操作")
    provider = _resolve_provider(account.get("extract_link_provider_id"))
    if provider["provider_type"] != "upi_git5":
        raise ValueError("原任务供应商类型已更改")
    code, _ = _resolve_cdk(provider, cdk=cdk, cdk_id=account.get("extract_link_cdk_id"))
    client = _upi_git5_client(provider)
    session = client.create_session(code)
    batch_id = str(account.get("extract_link_task_id") or "")
    job_id = account.get("extract_link_job_id")
    try:
        if not job_id:
            if not batch_id:
                raise ValueError("未取得上游批次 ID，请先在服务商核对受理情况")
            snap = _upi_git5_read(client, code, session, "get_batch", batch_id)
            if snap.get("batch_id") != batch_id:
                raise UpiGit5Error("原批次不匹配", uncertain=True)
            matches = [j for j in snap.get("jobs", []) if isinstance(j, dict) and
                       str(j.get("account_email") or "").casefold() == str(account.get("email") or "").casefold()]
            if len(matches) != 1:
                raise UpiGit5Error("原批次中无法唯一定位账号，请到服务商核对", uncertain=True)
            job_id = identifier(matches[0].get("id"))
        if action == "qr":
            png = _upi_git5_read(client, code, session, "checkout_qr", job_id)
            return {"ok": True, "image_url_png": "data:image/png;base64," + base64.b64encode(png).decode("ascii")}
        if action == "cancel":
            client.cancel_checkout(session["token"], job_id)
        job = _upi_git5_read(client, code, session, "checkout_progress", job_id)
        if job.get("id") != job_id or (batch_id and job.get("batch_id") and job["batch_id"] != batch_id):
            raise UpiGit5Error("返回任务与原任务不匹配", uncertain=True)
        return {**_upi_git5_state(job, batch_id=batch_id, secrets=(code, session["token"], account.get("access_token", ""))),
                "provider_id": provider["id"], "provider_type": "upi_git5", "provider_name": provider["name"]}
    finally:
        try:
            client.logout_session(session["token"])
        except Exception:
            pass
