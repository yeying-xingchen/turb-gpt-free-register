# -*- coding: utf-8 -*-
"""seashore 发布者 API：CDK 即账号，直接放在 Authorization: Bearer 头里。

与其它支付平台的三点差异：
* 没有幂等键——重复 POST 会重复建单并重复暂扣次数，因此只依赖本地提交前快照
  （``scan_payment_store.reserve``）阻止重放，而不是上游幂等。
* 拒绝码 ``paylink_ttl_untrusted`` / ``paylink_amount_rejected`` 会扣除 1 次但
  不创建任务，必须标记 ``charged=True``，否则回退候选会重复扣次。
* 查询按 ``public_id``（形如 ``ORD-1A2B3C4D``）单条读取，返回上游状态码与中文
  ``status_text``；本模块把状态归一到本站既有词汇，并保留 ``providerStatus``。
"""
from __future__ import annotations

import math
import re
import time

import requests

from config import scan_api as cfg
from core import operation_log
from core.scan_api_client import (
    ScanApiError, _CONTROL, _ID, _credential, _safe_message, _secret_values,
    validate_base, validate_payment_link,
)

# 提交时就扣次、但不创建任务的拒绝码（见《发布者 API》第 3 节）。
CHARGED_REJECTIONS = frozenset({"paylink_ttl_untrusted", "paylink_amount_rejected"})

# 上游状态 -> 本站既有状态词汇；``providerStatus`` 始终保留原文。
STATUS_ALIASES = {
    "pending": "queued",
    "processing": "processing",
    "submitted": "verifying",
    "completed": "completed",
    "timeout": "expired",
    "failed": "failed",
    "not_activated": "not_activated",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "link_rejected": "rejected",
}
VERIFYING_STATUSES = frozenset({"verifying"})
_PUBLIC_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")


def _status_text(value, secrets=()):
    text = _safe_message(value, secrets)
    return text[:120]


class SeashorePublisherClient:
    """普通（非一条龙）发布者 CDK 的提交、查询与额度核对。"""

    provider = "seashore"
    auth_scheme = "Bearer"

    def __init__(self, api_base=None, timeout=None):
        self.api_base = validate_base(api_base if api_base is not None else cfg.SEASHORE_API_BASE)
        self.timeout = max(5, min(300, int(timeout if timeout is not None else cfg.SCAN_API_TIMEOUT)))
        ua = _CONTROL.sub(" ", str(cfg.SCAN_API_USER_AGENT or ""))
        self.user_agent = ua if ua and ua.isascii() else "turb-gpt-free-register/1.0"

    # ------------------------------------------------------------------ HTTP

    def _headers(self, cdk, body=None):
        headers = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
            "Authorization": f"{self.auth_scheme} {cdk}",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        return headers

    def _request(self, method, path, *, cdk, body=None, submitting=False):
        cdk = _credential(cdk)
        secrets = [cdk, *_secret_values(body)]
        headers = self._headers(cdk, body)
        url = self.api_base + path
        started = time.monotonic()
        operation_log.request(method=method, url=url, headers=headers, body=body, timeout=self.timeout,
                              note="提交支付请求" if submitting else "查询支付任务", secrets=tuple(secrets))
        payload, raw_text = None, None
        try:
            with requests.Session() as session:
                response = session.request(method, url, json=body, headers=headers,
                                           timeout=self.timeout, allow_redirects=False)
                try:
                    payload = response.json()
                except ValueError:
                    payload = None
                    raw_text = getattr(response, "text", "") or ""
        except requests.RequestException as exc:
            operation_log.failure(exc, note="支付平台网络异常，未取得响应", secrets=tuple(secrets))
            # 没有上游任务 ID，重提可能重复建单并重复扣次：保持“待核实”。
            raise ScanApiError("网络结果未确认，请先核对原任务，不要换 CDK 重提",
                               code="TRANSPORT_ERROR", uncertain=True) from None
        status = response.status_code
        operation_log.response(status=status, headers=dict(response.headers), elapsed=time.monotonic() - started,
                               body=payload if isinstance(payload, dict) else raw_text, secrets=tuple(secrets))
        if not isinstance(payload, dict):
            error = ScanApiError("发布者 API 响应格式无效，请先核对原任务", status=status, code="INVALID_RESPONSE",
                                 uncertain=status >= 500 or (submitting and not 400 <= status < 500))
            operation_log.failure(error, note="发布者 API 响应不是 JSON 对象", secrets=tuple(secrets))
            raise error from None
        secrets.extend(_secret_values(payload))
        if not 200 <= status < 300:
            raise self._rejection(status, payload, submitting, secrets)
        return status, payload, secrets

    def _rejection(self, status, payload, submitting, secrets):
        """把 ``{"error": "...", "code": "..."}`` 转成带扣次语义的错误。"""
        error = payload.get("error")
        detail = error if isinstance(error, dict) else {}
        code = detail.get("code") or payload.get("code")
        message = (detail.get("message") or (error if isinstance(error, str) else None)
                   or payload.get("message") or "发布者平台拒绝了该请求")
        code = _safe_message(code, secrets) or None
        charged = code in CHARGED_REJECTIONS or payload.get("charged") is True
        # 4xx 明确拒绝即没有建单；5xx / 跳转 / 提交期的其它意外状态保持未知。
        uncertain = status >= 500 or (submitting and (300 <= status < 400 or payload.get("created") is True))
        if type(payload.get("created")) is bool:
            created = payload["created"]
        elif status < 500 and not uncertain:
            created = False
        else:
            created = None
        retryable = payload.get("retryable")
        if not isinstance(retryable, bool):
            retryable = status in (429, 502, 503, 504)
        retry_after = payload.get("retry_after_seconds", payload.get("retry_after"))
        try:
            retry_after = max(0, int(retry_after)) if retry_after is not None else None
        except (TypeError, ValueError):
            retry_after = None
        hint = ""
        if code == "paylink_ttl_untrusted" or code == "paylink_amount_rejected":
            hint = "（已扣 1 次，未创建任务）"
        elif code == "email_blocked" or code == "paylink_email_mismatch":
            hint = "（该 CDK 已被平台自动停用）"
        elif code == "insufficient_uses":
            hint = "（读取接口不受影响，请改看 remaining_uses）"
        rejection = ScanApiError(_safe_message(message, secrets) + hint, status=status, code=code,
                                 uncertain=uncertain, retryable=retryable, created=created,
                                 charged=charged, retry_after_seconds=retry_after)
        operation_log.failure(rejection, note="发布者平台拒绝该请求", secrets=tuple(secrets))
        return rejection

    # ------------------------------------------------------------- 结果归一

    def _task(self, raw, secrets, *, expected_id=None, submitting=False):
        if not isinstance(raw, dict):
            raw = {}
        public_id = raw.get("public_id") or raw.get("publicId")
        if not isinstance(public_id, str) or not _PUBLIC_ID.fullmatch(public_id) \
                or _safe_message(public_id, secrets) != public_id:
            public_id = None
        if expected_id is not None and public_id != expected_id:
            raise ScanApiError("返回任务 ID 与查询不匹配，保留原任务状态", code="TASK_ID_MISMATCH")
        if expected_id is None and not public_id:
            raise ScanApiError("平台未返回有效任务 ID，请先核对原任务", code="MISSING_TASK_ID",
                               uncertain=submitting)
        upstream_status = raw.get("status")
        upstream_status = upstream_status.strip().lower() if isinstance(upstream_status, str) else ""
        if expected_id is not None and not upstream_status:
            raise ScanApiError("平台未返回任务状态，保留原任务", code="MISSING_TASK_STATUS", uncertain=True)
        status = STATUS_ALIASES.get(upstream_status, "unknown" if not upstream_status else "processing")
        status_text = _status_text(raw.get("status_text") or raw.get("statusText"), secrets)
        task = {
            "id": public_id, "status": status, "providerStatus": upstream_status or "unknown",
            "statusText": status_text, "message": status_text or _status_text(raw.get("message"), secrets),
            "canCancel": bool(raw.get("can_cancel")) if isinstance(raw.get("can_cancel"), bool) else False,
        }
        if status in VERIFYING_STATUSES:
            task["verifying"] = True
        resolved_at = raw.get("resolved_at")
        if type(resolved_at) in (int, float):
            task["resolvedAt"] = resolved_at
        created_at = raw.get("created_at")
        if type(created_at) in (int, float):
            task["createdAt"] = created_at
        if status == "completed":
            task["message"] = "发布者平台已完成支付结算"
        return task

    def _result(self, status, payload, secrets, *, submitting=False, expected_id=None):
        task = self._task(payload.get("task"), secrets, expected_id=expected_id, submitting=submitting)
        if not task.get("id"):
            raise ScanApiError("平台未返回有效任务 ID，请先核对原任务", code="MISSING_TASK_ID", uncertain=True)
        return {"task": task, "task_id": task.get("id"), "request_id": None, "http_status": status,
                "duplicate": False, "uncertain": False}

    # ---------------------------------------------------------------- 接口

    def submit_upi(self, *, cdk, link, email=None, access_token=None, idempotency_key=None):
        """POST /tasks：邮箱 + 完整 AT + 支付链。上游没有幂等键，只提交一次。"""
        token = _credential(access_token)
        if not token.startswith("eyJ"):
            raise ValueError("发布者 API 需要所选账号的完整 eyJ 开头 access_token")
        body = {"email": str(email or "").strip(), "access_token": token,
                "pay_link": validate_payment_link(link)}
        if not body["email"]:
            raise ValueError("发布者 API 需要所选账号的邮箱")
        status, payload, secrets = self._request("POST", "/tasks", cdk=cdk, body=body, submitting=True)
        return self._result(status, payload, secrets, submitting=True)

    def get_task(self, *, cdk, task_id):
        if not isinstance(task_id, str) or not _PUBLIC_ID.fullmatch(task_id):
            raise ValueError("支付任务 ID 无效")
        from urllib.parse import quote
        status, payload, secrets = self._request("GET", "/tasks/" + quote(task_id, safe=""), cdk=cdk)
        return self._result(status, payload, secrets, expected_id=task_id)

    def verify_cdk(self, cdk):
        """GET /me：只回传额度、并发与站点聚合数字，不含任何身份信息。"""
        status, payload, secrets = self._request("GET", "/me", cdk=cdk)
        data = {}
        for key in ("remaining_uses", "uses_total", "uses_used", "uses_held", "uses_consumed",
                    "success_count", "failed_count", "pending_orders"):
            value = payload.get(key)
            if type(value) in (int, float) and not isinstance(value, bool) and math.isfinite(value):
                data[key] = value
        capacity = payload.get("capacity")
        if isinstance(capacity, dict):
            safe = {}
            for key in ("accept_per_minute", "complete_per_minute", "link_budget_seconds",
                        "recommended", "expected_wait_seconds"):
                value = capacity.get(key)
                if type(value) in (int, float) and not isinstance(value, bool) and math.isfinite(value):
                    safe[key] = value
            if isinstance(capacity.get("idle"), bool):
                safe["idle"] = capacity["idle"]
            if safe:
                data["capacity"] = safe
        site = payload.get("site_stats")
        if isinstance(site, dict):
            safe = {key: value for key, value in site.items()
                    if type(value) in (int, float) and not isinstance(value, bool) and math.isfinite(value)}
            if safe:
                data["site_stats"] = safe
        if "remaining_uses" in data:
            data["balance"] = data["remaining_uses"]
        return {"data": data, "request_id": None, "http_status": status}
