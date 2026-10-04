"""Payment clients: Astra omits account AT; Masi sends the selected account AT.

No automatic submission retries, redirects, or credential-bearing raw responses.
"""
from __future__ import annotations

import math
import re
import time
from urllib.parse import quote, urlsplit

import requests
from config import scan_api as cfg

from core import operation_log

_KEY = re.compile(r"[A-Za-z0-9:_-]{8,128}\Z")
_ID = re.compile(r"[A-Za-z0-9_:-][A-Za-z0-9_.:-]{0,255}\Z")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _safe_message(value, secrets=()):
    text = str(value) if isinstance(value, (str, int, float)) else ""
    for secret in sorted({s for s in secrets if isinstance(s, str) and s}, key=len, reverse=True):
        text = re.sub(re.escape(secret), "[已隐藏]", text, flags=re.I)
    text = re.sub(r"eyJ[A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+){1,2}", "[令牌已隐藏]", text)
    text = re.sub(r"\b(?:authorization|access_token|order_secret|x-cdk(?:-code)?)\s*[:=]\s*[^\s,;}]+", "[凭据已隐藏]", text, flags=re.I)
    return _CONTROL.sub(" ", text)[:500]


def _secret_values(value):
    result = []
    if isinstance(value, dict):
        for key, item in value.items():
            name = re.sub(r"[^a-z]", "", str(key).lower())
            if name in {"cdk", "cdkcode", "accesstoken", "refreshtoken", "ordersecret", "authorization", "token", "secret", "at"} and isinstance(item, str):
                result.append(item)
            result.extend(_secret_values(item))
    elif isinstance(value, list):
        for item in value:
            result.extend(_secret_values(item))
    return result


class ScanApiError(RuntimeError):
    def __init__(self, message, *, status=502, code=None, request_id=None, uncertain=False,
                 retryable=None, created=None, charged=None, retry_after_seconds=None):
        super().__init__(_safe_message(message))
        self.status = int(status)
        self.code = _safe_message(code) or None
        self.request_id = _safe_message(request_id) or None
        self.uncertain = bool(uncertain)
        self.retryable = retryable if isinstance(retryable, bool) else None
        self.created = created if isinstance(created, bool) else None
        self.charged = charged if isinstance(charged, bool) else None
        self.retry_after_seconds = retry_after_seconds
        self.retry_after = retry_after_seconds


def validate_base(value):
    if not isinstance(value, str):
        raise ValueError("支付 API 地址必须是 HTTPS URL")
    base = value.strip().rstrip("/")
    try:
        parts = urlsplit(base)
        _ = parts.port
    except ValueError:
        raise ValueError("支付 API 地址无效") from None
    if (parts.scheme != "https" or not parts.hostname or parts.username is not None
            or parts.password is not None or "?" in base or "#" in base or "\\" in base
            or _CONTROL.search(value) or any(c.isspace() for c in base)):
        raise ValueError("支付 API 地址必须是 HTTPS URL，不能带凭据、查询参数或片段")
    return base


def validate_payment_link(value):
    if not isinstance(value, str):
        raise ValueError("支付链接必须是 Stripe HTTPS URL")
    link = value.strip()
    try:
        parts = urlsplit(link)
        port = parts.port
    except ValueError:
        raise ValueError("支付链接无效") from None
    if (parts.scheme != "https" or parts.hostname != "payments.stripe.com" or port not in (None, 443)
            or parts.username is not None or parts.password is not None or "\\" in link
            or _CONTROL.search(value) or any(c.isspace() for c in link)):
        raise ValueError("支付链接必须是不带用户名密码的 payments.stripe.com HTTPS URL")
    return link


def validate_idempotency_key(value):
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise ValueError("Idempotency-Key 必须是 8–128 位字母数字、冒号、下划线或连字符")
    return value


def _credential(value):
    if not isinstance(value, str) or not value.strip() or _CONTROL.search(value):
        raise ValueError("请提供完整凭据，不能包含控制字符")
    return value.strip()


class _PaymentClient:
    def __init__(self, api_base=None, timeout=None):
        default = cfg.MASI_API_BASE if self.provider == "masi" else cfg.SCAN_API_BASE
        self.api_base = validate_base(api_base if api_base is not None else default)
        self.timeout = max(5, min(300, int(timeout if timeout is not None else cfg.SCAN_API_TIMEOUT)))
        ua = _CONTROL.sub(" ", str(cfg.SCAN_API_USER_AGENT or ""))
        self.user_agent = ua if ua and ua.isascii() else "turb-gpt-free-register/1.0"

    def _request(self, method, path, *, cdk, body=None, key=None, submitting=False):
        cdk = _credential(cdk)
        secrets = [cdk, *_secret_values(body)]
        headers = {"Accept": "application/json", "User-Agent": self.user_agent, self.credential_header: cdk}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if key is not None:
            headers["Idempotency-Key"] = validate_idempotency_key(key)
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
            raise ScanApiError("网络结果未确认，请先核对原任务，不要换 key 重提", code="TRANSPORT_ERROR", uncertain=True) from None
        status = response.status_code
        operation_log.response(status=status, headers=dict(response.headers), elapsed=time.monotonic() - started,
                               body=payload if isinstance(payload, dict) else raw_text, secrets=tuple(secrets))
        if not isinstance(payload, dict):
            error = ScanApiError("支付 API 响应格式无效，请先核对原任务", status=status, code="INVALID_RESPONSE",
                                 uncertain=status >= 500 or (submitting and not 400 <= status < 500))
            operation_log.failure(error, note="支付 API 响应不是 JSON 对象", secrets=tuple(secrets))
            raise error from None
        secrets.extend(_secret_values(payload))
        request_id = _safe_message(payload.get("requestId") or response.headers.get("X-Request-Id"), secrets) or None
        if not 200 <= status < 300 or (payload.get("ok") is False and status != 202):
            error = payload.get("error")
            detail = error if isinstance(error, dict) else {}
            code = detail.get("code") or payload.get("code")
            message = detail.get("message") or (error if isinstance(error, str) else None) or payload.get("message") or "平台请求被拒绝"
            metadata = {**payload, **detail}
            retry_after = next((metadata[k] for k in ("retry_after_seconds", "retry_after", "retryAfter") if k in metadata), response.headers.get("Retry-After"))
            try:
                retry_after = max(0, int(retry_after)) if retry_after is not None else None
            except (ValueError, TypeError):
                retry_after = None
            rejection = ScanApiError(_safe_message(message, secrets), status=status, code=_safe_message(code, secrets),
                               request_id=request_id, uncertain=status >= 500 or (submitting and (300 <= status < 400
                               or code == "IDEMPOTENCY_CONFLICT" or metadata.get("created") is True or metadata.get("charged") is True)),
                               retryable=detail.get("retryable", payload.get("retryable")),
                               created=metadata.get("created"), charged=metadata.get("charged"), retry_after_seconds=retry_after)
            operation_log.failure(rejection, note="支付平台拒绝该请求", secrets=tuple(secrets))
            raise rejection
        return status, payload, request_id, secrets

    def _result(self, result, *, submitting, expected_id=None):
        status, payload, request_id, secrets = result
        data = payload.get("data") if self.provider == "v1" else payload
        data = data if isinstance(data, dict) else {}
        raw = data.get("task", data) if self.provider == "v1" else data.get("order", {})
        raw = raw if isinstance(raw, dict) else {}
        task_id = raw.get("id") or (raw.get("order_id") if self.provider == "masi" else raw.get("taskId"))
        if not isinstance(task_id, str) or not _ID.fullmatch(task_id) or _safe_message(task_id, secrets) != task_id:
            task_id = None
        if not submitting and (not task_id or expected_id != task_id):
            raise ScanApiError("返回任务 ID 与查询不匹配，保留原任务状态", code="TASK_ID_MISMATCH")
        if not task_id and status != 202:
            raise ScanApiError("平台未返回有效任务 ID，请先核对原任务", code="MISSING_TASK_ID", uncertain=submitting)
        raw_status = raw.get("status")
        has_status = isinstance(raw_status, str) and bool(raw_status.strip())
        if not submitting and not has_status:
            raise ScanApiError("平台未返回任务状态，保留原任务", code="MISSING_TASK_STATUS", uncertain=True)
        uncertain = submitting and (status == 202 or not has_status)
        task = {"id": task_id, "status": _safe_message(raw_status, secrets).lower() if has_status else "unknown",
                "message": _safe_message(raw.get("message") or raw.get("error"), secrets), "canRedispatch": False}
        aliases = {"verificationPolicy": "verification_policy", "verificationReason": "verification_reason",
                   "expiresAt": "qr_expires_at", "verificationDeadline": "verification_deadline",
                   "redispatchHint": "redispatch_hint", "canCancel": "can_cancel", "verifying": "verifying"}
        for key, alias in aliases.items():
            value = raw.get(key, raw.get(alias))
            if key == "expiresAt" and value is None:
                value = raw.get("expires_at")
            if isinstance(value, str):
                task[key] = _safe_message(value, secrets)
            elif value is None or type(value) in (bool, int, float):
                task[key] = value
        if task["status"] in {"completed", "succeeded"}:
            task["message"] = "已按原有 AT 变化规则完成结算" if task.get("verificationPolicy") == "legacy" else "已按平台规则完成结算"
        if uncertain:
            task["submission_uncertain"] = True
        duplicate = payload.get("duplicate") is True or data.get("duplicate") is True
        if self.provider == "masi" and submitting and status == 200:
            duplicate = True
        return {"task": task, "task_id": task_id, "request_id": request_id, "http_status": status,
                "duplicate": bool(duplicate) and not uncertain, "uncertain": uncertain}

    def get_task(self, *, cdk, task_id):
        if not isinstance(task_id, str) or not _ID.fullmatch(task_id):
            raise ValueError("支付任务 ID 无效")
        return self._result(self._request("GET", self.collection_path + "/" + quote(task_id, safe=""), cdk=cdk),
                            submitting=False, expected_id=task_id)

    def verify_cdk(self, cdk):
        path = "/api/integration/tickets/status" if self.provider == "masi" else "/cdk/verify"
        body = {} if self.provider == "masi" else {"code": _credential(cdk)}
        status, payload, request_id, secrets = self._request("POST", path, cdk=cdk, body=body)
        data = payload.get("ticket", {}) if self.provider == "masi" else payload.get("data", {})
        data = data if isinstance(data, dict) else {}
        safe = {}
        for key in ("total_uses", "used_uses", "pending_uses", "available_uses", "amountStars", "availableStars", "reservedStars"):
            value = data.get(key)
            if type(value) in (int, float) and math.isfinite(value):
                safe[key] = value
        for key in ("id", "label", "status", "expiresAt"):
            if key in data:
                safe[key] = _safe_message(data[key], secrets)
        return {"data": safe, "request_id": request_id, "http_status": status}


class ScanApiClient(_PaymentClient):
    provider = "v1"
    credential_header = "X-CDK-Code"
    collection_path = "/scan-requests"

    def submit_upi(self, *, cdk, link, email=None, idempotency_key):
        idempotency_key = validate_idempotency_key(idempotency_key)
        body = {"channel": "upi", "inputType": "LINK", "link": validate_payment_link(link)}
        if email:
            body["email"] = email
        return self._result(self._request("POST", self.collection_path, cdk=cdk, body=body,
                                          key=idempotency_key, submitting=True), submitting=True)


class MasiClient(_PaymentClient):
    provider = "masi"
    credential_header = "X-CDK"
    collection_path = "/api/integration/orders"

    def submit_upi(self, *, cdk, link, access_token, email=None, idempotency_key=None):
        token = _credential(access_token)
        if not token.startswith("eyJ"):
            raise ValueError("masi 需要所选账号的完整 eyJ 开头 access_token")
        body = {"link": validate_payment_link(link), "access_token": token, "dispatch_mode": "capacity_priority"}
        if email:
            body["email"] = email
        # Masi has no documented idempotency header; the local journal guards replays.
        return self._result(self._request("POST", self.collection_path, cdk=cdk, body=body, submitting=True), submitting=True)
