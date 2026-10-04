"""Client for the public Lumen Flow checkout API.

The submit call deliberately has no automatic retry: a transport failure can
mean that the upstream accepted the task already.
"""
from __future__ import annotations

import re
import time
from urllib.parse import urlsplit
from uuid import UUID

import requests

from core import operation_log

PAYMENT_METHODS = ("IDEAL", "UPI", "PIX", "PAYPAL", "KAKAO_PAY", "MOMO", "BLIK", "TWINT", "GCASH", "GOPAY")
TASK_STATES = {"QUEUED", "RUNNING", "AWAITING_BLIK_CODE", "SUCCEEDED", "FAILED", "CANCELED"}
FLOW_PATH = "/public/api/checkout/flow"


def validate_base(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("提供方地址必须是 HTTP(S) URL")
    base = value.strip().rstrip("/")
    try:
        parts = urlsplit(base)
        _ = parts.port
    except ValueError:
        raise ValueError("提供方地址无效") from None
    if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username
            or parts.password or parts.query or parts.fragment or any(c.isspace() for c in base)):
        raise ValueError("提供方地址必须是 HTTP(S) URL，不能包含凭据、查询参数或片段")
    return base


def _safe_message(value, secrets=()) -> str:
    text = str(value or "服务请求失败")
    for secret in secrets:
        if isinstance(secret, str) and secret:
            text = re.sub(re.escape(secret), "[已隐藏]", text, flags=re.IGNORECASE)
    text = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)?", "[令牌已隐藏]", text)
    text = re.sub(r"https?://[^\s/@]+:[^\s/@]+@", "http://[凭据已隐藏]@", text)
    return text[:500]


class LumenError(RuntimeError):
    def __init__(self, message: str, *, status: int = 502, uncertain: bool = False):
        super().__init__(message)
        self.status = int(status)
        self.uncertain = bool(uncertain)


class LumenClient:
    def __init__(self, api_base: str = "https://api.int31.space", timeout: int = 30):
        self.api_base = validate_base(api_base)
        self.timeout = max(5, min(300, int(timeout)))

    def _request(self, method: str, path: str, *, body=None, params=None, submitting=False):
        secrets = []
        def collect(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in {"cdk", "accessToken", "proxyUrl", "blikCode"}:
                        secrets.append(item)
                    else:
                        collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)
        collect(body)
        collect(params)
        known = tuple(item for item in secrets if isinstance(item, str) and item)
        url = self.api_base + path
        started = time.monotonic()
        operation_log.request(method=method, url=url, headers={"Accept": "application/json"}, params=params,
                              body=body, timeout=self.timeout,
                              note="Lumen Flow 提交提链" if submitting else "Lumen Flow 请求",
                              secrets=known)
        try:
            with requests.Session() as session:
                response = session.request(
                    method, url, json=body, params=params,
                    timeout=self.timeout, allow_redirects=False,
                    headers={"Accept": "application/json"},
                )
                try:
                    payload = response.json()
                except ValueError:
                    operation_log.response(status=response.status_code, headers=dict(response.headers),
                                           elapsed=time.monotonic() - started, body=getattr(response, "text", ""),
                                           note="Lumen 未返回有效 JSON", secrets=known)
                    raise LumenError(
                        "提供方未返回有效 JSON；请核对任务受理情况" if submitting else "提供方未返回有效 JSON",
                        uncertain=submitting,
                    ) from None
                operation_log.response(status=response.status_code, headers=dict(response.headers),
                                       elapsed=time.monotonic() - started, body=payload, secrets=known)
                if not 200 <= response.status_code < 300:
                    detail = None
                    if isinstance(payload, dict):
                        detail = payload.get("detail") or payload.get("title") or payload.get("error")
                    uncertain = submitting and response.status_code >= 500
                    error = LumenError(
                        _safe_message(detail or f"提供方 HTTP {response.status_code}", known),
                        status=response.status_code,
                        uncertain=uncertain,
                    )
                    operation_log.failure(error, note="Lumen 拒绝该请求", secrets=known)
                    raise error
                return payload
        except LumenError:
            raise
        except requests.RequestException as exc:
            operation_log.failure(exc, note="Lumen 网络异常，未取得响应", secrets=known)
            raise LumenError(
                "网络异常，受理情况未确认，请先核对，勿直接重提" if submitting else "查询提供方失败，请稍后刷新原任务",
                uncertain=submitting,
            ) from None

    @staticmethod
    def _task_path(task_id: str) -> str:
        try:
            normalized = str(UUID(str(task_id)))
        except (ValueError, TypeError, AttributeError):
            raise ValueError("Lumen 任务 ID 无效") from None
        return f"{FLOW_PATH}/{normalized}"

    @staticmethod
    def check_task(task, expected_id: str | None = None) -> dict:
        if not isinstance(task, dict) or task.get("status") not in TASK_STATES:
            raise LumenError("任务快照格式无效")
        task_id = str(task.get("taskId") or "")
        LumenClient._task_path(task_id)
        if expected_id and task_id != str(expected_id):
            raise LumenError("提供方返回的任务 ID 不匹配")
        return task

    def validate_cdk(self, cdk: str) -> dict:
        result = self._request("POST", "/public/api/checkout/cdk/validate", body={"cdk": cdk})
        if not isinstance(result, dict) or result.get("valid") is not True:
            raise LumenError("CDK 校验响应无效")
        keys = ("valid", "remainingUses", "reservedUses", "expiresAt", "flowUses", "momoPaymentUses", "automaticMomoUses")
        return {key: result[key] for key in keys if key in result}

    def submit(self, *, cdk: str, access_token: str, payment_method: str, options: dict | None = None) -> dict:
        entry = {
            "cdk": cdk,
            "accessToken": access_token,
            "paymentMethodType": payment_method,
            "maxAmountCents": 0,
        }
        if options:
            entry.update(options)
        payload = self._request("POST", FLOW_PATH + "/submit-batch", body={"requests": [entry]}, submitting=True)
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
            raise LumenError("提交响应不完整，受理情况未确认，请先核对", uncertain=True)
        item = results[0]
        outcome = item.get("outcome")
        if outcome == "REJECTED":
            raise LumenError(_safe_message(item.get("error") or "提链请求被拒绝", (cdk, access_token, entry.get("proxyUrl"))), status=item.get("status") or 400)
        if outcome not in {"ACCEPTED", "REUSED"}:
            raise LumenError("提供方返回 UNKNOWN，受理情况未确认，请先核对，勿直接重提", uncertain=True)
        return self.check_task(item.get("task"))

    def get_task(self, task_id: str) -> dict:
        return self.check_task(self._request("GET", self._task_path(task_id)), task_id)

    def batch_tasks(self, task_ids: list[str]) -> dict:
        if not 1 <= len(task_ids) <= 500:
            raise ValueError("每次查询须包含 1–500 个任务")
        for task_id in task_ids:
            self._task_path(task_id)
        result = self._request("POST", FLOW_PATH + "/batch", body={"taskIds": task_ids})
        if not isinstance(result, dict) or not isinstance(result.get("tasks"), list) or not isinstance(result.get("missingTaskIds"), list):
            raise LumenError("批量查询响应无效")
        return result

    def cancel(self, task_id: str) -> dict:
        return self.check_task(self._request("POST", self._task_path(task_id) + "/cancel"), task_id)

    def blik_code(self, task_id: str, code: str) -> dict:
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
            raise ValueError("BLIK 验证码必须是六位数字")
        return self.check_task(self._request("POST", self._task_path(task_id) + "/blik-code", body={"blikCode": code}), task_id)

    def sync_payment(self, task_id: str) -> dict:
        return self.check_task(self._request("POST", self._task_path(task_id) + "/sync-payment-state"), task_id)

    def sync_plus(self, task_id: str, access_token: str) -> dict:
        return self.check_task(self._request("POST", self._task_path(task_id) + "/sync-plus-verification", body={"accessToken": access_token}), task_id)

    def momo_payment(self, task_id: str) -> dict:
        return self._request("GET", self._task_path(task_id) + "/momo-payment")

    def results(self, cdk: str):
        return self._request("GET", FLOW_PATH + "/results", params={"cdk": cdk})

    def status(self) -> dict:
        return self._request("GET", FLOW_PATH + "/status")

    def stats(self) -> dict:
        return self._request("GET", FLOW_PATH + "/stats")

    def merge_cdks(self, cdks: list[str]) -> dict:
        if not 2 <= len(cdks) <= 100:
            raise ValueError("合并 CDK 数量必须在 2–100 之间")
        return self._request("POST", "/public/api/checkout/cdk/merge", body={"cdks": cdks})
