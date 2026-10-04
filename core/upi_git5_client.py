# -*- coding: utf-8 -*-
"""Client for the public UPI-GIT5 checkout-link API.

The service is documented by its OpenAPI 3.1 spec at
https://upi.agiapi.top (production, HTTPS/443).  The checkout flow is:

1. ``POST /api/link-cdk/session`` — exchange a Link CDK code for a tenant
   session token (``X-Link-CDK-Session`` header for all later calls).
2. ``POST /api/upi-git5/batch`` — create a batch that walks the account ATs
   through a checkout link; the response contains a ``batch_id`` and one
   ``job`` per token with a per-account ``job_id``.
3. ``GET /api/upi-git5/batch/{batch_id}`` (or ``/progress``) — poll until every
   job reaches a terminal state (``done`` / ``error`` / ``cancelled``).

Per-job ops: ``GET /api/checkout-progress?job_id=``, ``GET /api/checkout-qr?job_id=``
(PNG bytes), ``POST /api/checkout-cancel``.

The submit call deliberately has no automatic retry: a transport failure can
mean that the upstream accepted the batch already.
"""
from __future__ import annotations

import base64
import binascii
import re
import time
from urllib.parse import quote, urlsplit

import requests

from core import operation_log

DEFAULT_API_BASE = "https://upi.agiapi.top"
SESSION_HEADER = "X-Link-CDK-Session"
PAYMENT_PROVIDERS = ("foarge", "xxsyun", "astrascan")
BATCH_JOB_TERMINAL = {"done", "error", "cancelled"}

# Job.status values observed in BatchSnapshot.
JOB_STATUSES = {"queued", "running", "done", "error", "cancelled"}
PAYMENT_STATUSES = {
    "not_submitted", "awaiting_worker", "queued", "submission_waiting", "submitting",
    "submit_failed", "tracking_retrying", "tracking_failed", "quota_insufficient",
    "completed", "failed", "expired", "cancelled",
}


def validate_base(value: str) -> str:
    """Accept only clean HTTP(S) origins; identical rules to LumenClient."""
    if not isinstance(value, str):
        raise ValueError("UPI-GIT5 地址必须是 HTTP(S) URL")
    base = value.strip().rstrip("/")
    try:
        parts = urlsplit(base)
        if parts.port is not None and parts.port <= 0:
            raise ValueError
    except ValueError:
        raise ValueError("UPI-GIT5 地址无效") from None
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or "?" in base or "#" in base or "\\" in base
        or any(char.isspace() or ord(char) < 32 for char in base)
    ):
        raise ValueError("UPI-GIT5 地址必须是 HTTP(S) URL，不能包含凭据、查询参数或片段")
    return base


def _safe_message(value, secrets=()) -> str:
    text = str(value or "UPI-GIT5 服务请求失败")
    for secret in secrets:
        if isinstance(secret, str) and secret:
            text = re.sub(re.escape(secret), "[已隐藏]", text, flags=re.IGNORECASE)
    # AT-like JWTs should never be copied into a local error message.
    text = re.sub(
        r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)?",
        "[令牌已隐藏]",
        text,
    )
    return text[:500]


def _error_message(payload: dict) -> tuple[str, str | None]:
    """Extract (message, code) from the ErrorEnvelope shape."""
    if not isinstance(payload, dict):
        return "请求被拒绝", None
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or "请求被拒绝"), str(error.get("code") or "") or None
    if error:
        return str(error), None
    return str(payload.get("message") or "请求被拒绝"), str(payload.get("code") or "") or None


class UpiGit5Error(RuntimeError):
    """An upstream API rejection or an uncertain upstream result."""

    def __init__(self, message: str, *, status: int = 502, code: str | None = None,
                 uncertain: bool = False, batch_id: str | None = None, retry_after: float | None = None):
        super().__init__(message)
        self.status = int(status)
        self.code = str(code or "") or None
        self.uncertain = bool(uncertain)
        self.batch_id = batch_id
        self.retry_after = retry_after


class UpiGit5Client:
    def __init__(self, api_base: str = DEFAULT_API_BASE, timeout: int = 30):
        self.api_base = validate_base(api_base)
        try:
            raw_timeout = int(timeout)
        except (TypeError, ValueError):
            raw_timeout = 30
        self.timeout = max(5, min(300, raw_timeout))

    # ------------------------------------------------------------------ core

    @staticmethod
    def _text(value, label):
        if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError(f"{label} 必须是非空字符串且不能包含控制字符")
        return value.strip()

    @classmethod
    def _strings(cls, values, label):
        if not isinstance(values, list) or not 1 <= len(values) <= 5000:
            raise ValueError(f"{label} 必须是 1–5000 个非空字符串的数组")
        return [cls._text(v, label) for v in values]

    @staticmethod
    def _batch_id(payload, secrets):
        value = payload.get("batch_id") if isinstance(payload, dict) else None
        if (isinstance(value, str) and value.strip() and len(value) <= 512
                and not any(ord(c) < 32 or ord(c) == 127 for c in value)
                and not any(secret and secret in value for secret in secrets)):
            return value
        return None

    def _request(self, method, path, *, body=None, params=None, session_token=None,
                 submitting=False, secrets=(), png=False):
        headers = {"Accept": "image/png" if png else "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if session_token is not None:
            session_token = self._text(session_token, "租户会话 token")
            headers[SESSION_HEADER] = session_token
        secrets = tuple(s for s in (*secrets, session_token) if isinstance(s, str) and s)
        url = self.api_base + path
        started = time.monotonic()
        operation_log.request(method=method, url=url, headers=headers, params=params, body=body,
                              timeout=self.timeout,
                              note="UPI-GIT5 提交批次" if submitting else "UPI-GIT5 请求",
                              secrets=secrets)
        try:
            with requests.Session() as http:
                response = http.request(method, url, json=body, params=params,
                                        headers=headers, timeout=self.timeout, allow_redirects=False)
        except requests.RequestException as exc:
            operation_log.failure(exc, note="UPI-GIT5 网络异常，未取得响应", secrets=secrets)
            raise UpiGit5Error(
                "网络异常，受理情况未确认，请先核对，勿直接重提" if submitting else "请求 UPI-GIT5 失败，请稍后刷新",
                uncertain=submitting) from None
        status = response.status_code
        payload = None
        raw_text = None
        try:
            payload = response.json()
        except ValueError:
            raw_text = getattr(response, "text", "") or ""
        operation_log.response(status=status, headers=dict(response.headers),
                               elapsed=time.monotonic() - started,
                               body=payload if payload is not None else raw_text, secrets=secrets)
        if not 200 <= status < 300 or (isinstance(payload, dict) and payload.get("ok") is False):
            message, code = _error_message(payload)
            try:
                retry_after = min(60, max(0, float(response.headers.get("Retry-After", "0")))) or None
            except (ValueError, TypeError):
                retry_after = None
            batch_id = self._batch_id(payload, secrets) if submitting else None
            error = UpiGit5Error(_safe_message(message, secrets), status=status if status >= 300 else 400,
                code=_safe_message(code, secrets) if code else None,
                uncertain=submitting and (bool(batch_id) or status >= 500 or status == 408),
                batch_id=batch_id, retry_after=retry_after)
            operation_log.failure(error, note="UPI-GIT5 拒绝该请求", secrets=secrets)
            raise error
        if png:
            content = response.content
            if not content.startswith(b"\x89PNG\r\n\x1a\n"):
                # OpenAPI format:byte can be encoded base64 by some deployments.
                # This is still the image body, not an undocumented JSON envelope.
                try:
                    content = base64.b64decode(content.strip(), validate=True)
                except (ValueError, binascii.Error):
                    content = b""
            if not content.startswith(b"\x89PNG\r\n\x1a\n"):
                error = UpiGit5Error("UPI-GIT5 未返回有效 PNG 二维码")
                operation_log.failure(error, note="二维码响应无效", secrets=secrets)
                raise error
            if len(content) > 10 * 1024 * 1024:
                error = UpiGit5Error("UPI-GIT5 二维码超过大小限制")
                operation_log.failure(error, note="二维码超过大小限制", secrets=secrets)
                raise error
            operation_log.detail("收到 PNG 二维码", bytes=len(content))
            return content
        if not isinstance(payload, dict):
            error = UpiGit5Error("UPI-GIT5 未返回有效 JSON 对象；请核对原任务" if submitting else "UPI-GIT5 未返回有效 JSON 对象",
                              uncertain=submitting)
            operation_log.failure(error, note="响应不是 JSON 对象", secrets=secrets)
            raise error
        return payload

    @staticmethod
    def _require_dict(payload, label: str) -> dict:
        if not isinstance(payload, dict):
            raise UpiGit5Error(f"UPI-GIT5 {label}响应格式无效")
        return payload

    @staticmethod
    def _required(data: dict, key: str, label: str):
        value = data.get(key)
        if value is None or value == "":
            raise UpiGit5Error(f"UPI-GIT5 响应缺少 {label}（{key}）")
        return value

    # ------------------------------------------------------------- service

    def health(self) -> dict:
        return self._require_dict(self._request("GET", "/api/health"), "健康")

    def config(self) -> dict:
        return self._require_dict(self._request("GET", "/api/config"), "配置")

    def recent_results(self, hours: float = 12.0) -> dict:
        return self._require_dict(
            self._request("GET", "/api/recent-results", params={"hours": float(hours)}),
            "统计",
        )

    # ------------------------------------------------------------ link CDK

    def cdk_status(self, cdk: str) -> dict:
        code = self._text(cdk, "提链 CDK")
        return self._require_dict(
            self._request("POST", "/api/link-cdk/status", body={"code": code}, secrets=(code,)),
            "CDK 状态",
        )

    def create_session(self, cdk: str) -> dict:
        code = self._text(cdk, "提链 CDK")
        data = self._require_dict(
            self._request("POST", "/api/link-cdk/session", body={"code": code}, submitting=True, secrets=(code,)),
            "会话",
        )
        try:
            self._text(data.get("token"), "会话 token")
        except ValueError:
            raise UpiGit5Error("UPI-GIT5 会话响应缺少有效 token") from None
        return data

    def logout_session(self, session_token: str) -> dict:
        return self._require_dict(
            self._request("POST", "/api/link-cdk/session/logout", session_token=session_token),
            "注销",
        )

    # -------------------------------------------------------------- batch

    def create_batch(
        self,
        *,
        session_token: str,
        tokens: list[str],
        entry_proxies: list[str],
        link_cdk: str,
        payment_provider_id: str | None = None,
        use_promo: bool | None = None,
        promo_campaign: str | None = None,
    ) -> dict:
        tokens = self._strings(tokens, "tokens")
        entry_proxies = self._strings(entry_proxies, "entry_proxies")
        link_cdk = self._text(link_cdk, "提链 CDK")
        body = {"tokens": tokens, "entry_proxies": entry_proxies, "link_cdk": link_cdk}
        if payment_provider_id is not None:
            if payment_provider_id not in PAYMENT_PROVIDERS:
                raise ValueError("支付平台必须是 foarge、xxsyun 或 astrascan")
            body["payment_provider_id"] = payment_provider_id
        if use_promo is not None:
            if type(use_promo) is not bool:
                raise ValueError("use_promo 必须是布尔值")
            body["use_promo"] = use_promo
        if promo_campaign is not None:
            body["promo_campaign"] = self._text(promo_campaign, "promo_campaign")
        secrets = (link_cdk, session_token, *tokens, *entry_proxies)
        data = self._request("POST", "/api/upi-git5/batch", body=body,
                             session_token=session_token, submitting=True, secrets=secrets)
        batch_id = self._batch_id(data, secrets)
        jobs = data.get("jobs")
        job_ids = [j.get("job_id") for j in jobs if isinstance(j, dict)] if isinstance(jobs, list) else []
        valid_ids = all(isinstance(j, str) and j.strip() and len(j) <= 512
                        and not any(ord(c) < 32 or ord(c) == 127 for c in j)
                        and not any(secret and secret in j for secret in secrets) for j in job_ids)
        if (not batch_id or data.get("ok") is not True or type(data.get("batch_size")) is not int
                or data["batch_size"] != len(tokens) or not isinstance(jobs, list)
                or len(jobs) != len(tokens) or len(job_ids) != len(tokens) or not valid_ids
                or len(set(job_ids)) != len(tokens)):
            raise UpiGit5Error("UPI-GIT5 批次已受理但响应不完整，请核对原批次", uncertain=True, batch_id=batch_id)
        return data

    def get_batch(self, session_token: str, batch_id: str) -> dict:
        data = self._require_dict(
            self._request("GET", f"/api/upi-git5/batch/{quote(str(batch_id), safe='')}", session_token=session_token),
            "批次详情",
        )
        return self._require_dict(data, "批次详情")

    def get_batch_progress(self, session_token: str, batch_id: str) -> dict:
        data = self._require_dict(
            self._request("GET", f"/api/upi-git5/batch/{quote(str(batch_id), safe='')}/progress", session_token=session_token),
            "批次进度",
        )
        return self._require_dict(data, "批次进度")

    # ------------------------------------------------------------ checkout

    def checkout_progress(self, session_token: str, job_id: str) -> dict:
        data = self._require_dict(
            self._request(
                "GET",
                "/api/checkout-progress",
                params={"job_id": str(job_id)},
                session_token=session_token,
            ),
            "任务进度",
        )
        return self._require_dict(data, "任务进度")

    def checkout_qr(self, session_token: str, job_id: str) -> bytes:
        """Fetch the authenticated PNG body; no session token reaches the browser."""
        return self._request("GET", "/api/checkout-qr", params={"job_id": self._text(job_id, "任务 ID")},
                             session_token=session_token, png=True)

    def cancel_checkout(self, session_token: str, job_id: str) -> dict:
        return self._require_dict(
            self._request(
                "POST",
                "/api/checkout-cancel",
                body={"job_id": str(job_id)},
                session_token=session_token,
            ),
            "取消任务",
        )

    # ------------------------------------------------------------- payment

    def payment_status(self, session_token: str) -> dict:
        return self._require_dict(self._request("GET", "/api/payment/status", session_token=session_token), "支付配置")

    def configure_payment(self, session_token: str, provider_id: str, cdk: str) -> dict:
        return self._require_dict(
            self._request(
                "POST",
                "/api/payment/configure",
                body={"provider_id": str(provider_id), "cdk": str(cdk)},
                session_token=session_token,
                submitting=True,
                secrets=(str(cdk),),
            ),
            "支付配置",
        )

    def set_payment_settings(self, session_token: str, provider_id: str, enabled: bool) -> dict:
        return self._require_dict(
            self._request(
                "POST",
                "/api/payment/settings",
                body={"provider_id": str(provider_id), "enabled": bool(enabled)},
                session_token=session_token,
            ),
            "支付设置",
        )

    def clear_payment(self, session_token: str, provider_id: str) -> dict:
        return self._require_dict(
            self._request(
                "POST",
                "/api/payment/clear",
                body={"provider_id": str(provider_id)},
                session_token=session_token,
            ),
            "清空支付",
        )

    def refresh_payment_quota(self, session_token: str, provider_id: str) -> dict:
        return self._require_dict(
            self._request(
                "POST",
                "/api/payment/quota",
                body={"provider_id": str(provider_id)},
                session_token=session_token,
            ),
            "支付额度",
        )

    def manual_submit_payments(self, session_token: str, job_ids: list[str]) -> dict:
        return self._require_dict(
            self._request(
                "POST",
                "/api/payment/manual-submit",
                body={"job_ids": list(job_ids)},
                session_token=session_token,
                submitting=True,
            ),
            "手动支付",
        )