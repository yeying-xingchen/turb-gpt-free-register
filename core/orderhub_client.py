"""UPI OrderHub adapter with Bearer keys or server-held employer sessions."""
from __future__ import annotations

import re
import threading
import time
from urllib.parse import quote
from uuid import uuid4

import requests

from config import scan_api as cfg
from core import operation_log
from core.scan_api_client import ScanApiError, _safe_message, validate_base, validate_payment_link, validate_idempotency_key, _ID, _secret_values

_SESSIONS = {}
_SESSION_LOCK = threading.RLock()
_SESSION_LIFETIME = 8 * 60 * 60


def _safe_user(user, secrets=()):
    if not isinstance(user, dict):
        return {}
    safe = {}
    for key in ("id", "username", "employer_status"):
        value = user.get(key)
        if type(value) not in (str, int) or _safe_message(value, secrets) != str(value):
            continue
        if key == "username" and not re.fullmatch(r"[0-9]{6,16}", str(value)):
            continue
        if key == "id" and not _ID.fullmatch(str(value)):
            continue
        safe[key] = value
    return safe


class OrderHubClient:
    def __init__(self, api_base=None, timeout=None):
        self.api_base = validate_base(api_base or cfg.ORDERHUB_API_BASE)
        self.timeout = max(5, min(300, int(timeout or cfg.SCAN_API_TIMEOUT)))
        self.session_auth = None

    def _request(self, method, path, *, cdk="", body=None, key=None, submitting=False):
        if key:
            validate_idempotency_key(key)
        headers = {"User-Agent": cfg.SCAN_API_USER_AGENT, "Content-Type": "application/json", "Accept": "application/json"}
        secrets = [cdk]
        if isinstance(body, dict):
            secrets.extend(str(item.get("at") or "") for item in body.get("items", []) if isinstance(item, dict))
        if key:
            headers["Idempotency-Key"] = key
        if not self.session_auth:
            if not isinstance(cdk, str) or not cdk or any(ord(c) < 32 for c in cdk):
                raise ValueError("请输入 OrderHub Bearer API Key 或先登录数字雇主账号")
            headers["Authorization"] = "Bearer " + cdk
        elif self.session_auth["api_base"] != self.api_base:
            raise ValueError("登录会话与订单平台地址不一致，请重新登录原平台")
        url = self.api_base + path
        started = time.monotonic()
        operation_log.request(method=method, url=url, headers=headers, body=body, timeout=self.timeout,
                              cookies=dict(self.session_auth["cookies"]) if self.session_auth else None,
                              note="提交 OrderHub 订单" if submitting else "查询 OrderHub 订单",
                              secrets=tuple(secrets))
        try:
            with requests.Session() as session:
                if self.session_auth:
                    session.cookies.update(self.session_auth["cookies"])
                    secrets.extend(cookie.value for cookie in session.cookies)
                response = session.request(method, url, headers=headers, json=body,
                                           timeout=self.timeout, allow_redirects=False)
                try:
                    data = response.json()
                except ValueError:
                    operation_log.response(status=response.status_code, headers=dict(response.headers),
                                           elapsed=time.monotonic() - started, body=getattr(response, "text", ""),
                                           note="OrderHub 返回非 JSON 响应", secrets=tuple(secrets))
                    raise ScanApiError("平台返回非 JSON 响应，请核对原任务", status=502, uncertain=submitting) from None
        except requests.RequestException as exc:
            operation_log.failure(exc, note="OrderHub 网络异常，未取得响应", secrets=tuple(secrets))
            raise ScanApiError("网络结果未确认，请查询原任务或使用原请求重试", uncertain=submitting) from None
        operation_log.response(status=response.status_code, headers=dict(response.headers),
                               elapsed=time.monotonic() - started, body=data, secrets=tuple(secrets))
        if not isinstance(data, dict):
            error = ScanApiError("平台响应格式无效，请核对原任务", uncertain=submitting)
            operation_log.failure(error, note="OrderHub 响应格式无效", secrets=tuple(secrets))
            raise error
        secrets.extend(_secret_values(data))
        if not 200 <= response.status_code < 300 or data.get("ok") is not True:
            error = data.get("error") if isinstance(data.get("error"), dict) else {}
            code = _safe_message(error.get("code") or "UPSTREAM_ERROR", secrets)
            rejection = ScanApiError(_safe_message(error.get("message") or "平台请求被拒绝", secrets),
                               status=response.status_code if response.status_code >= 400 else 400, code=code,
                               uncertain=response.status_code >= 500 or (submitting and (response.status_code == 202 or 300 <= response.status_code < 400 or code == "IDEMPOTENCY_CONFLICT")))
            operation_log.failure(rejection, note="OrderHub 拒绝该请求", secrets=tuple(secrets))
            raise rejection
        return response.status_code, data, secrets

    @staticmethod
    def _order(order, status, secrets, *, expected_id=None, duplicate=False):
        if not isinstance(order, dict) or not isinstance(order.get("id"), str) or not order["id"]:
            raise ScanApiError("平台未返回订单 ID，请查询原任务", uncertain=True)
        order_id = order["id"]
        if not _ID.fullmatch(order_id) or _safe_message(order_id, secrets) != order_id:
            raise ScanApiError("平台订单 ID 格式无效，请核对原任务", uncertain=True)
        if expected_id and expected_id != order_id:
            raise ScanApiError("平台返回的订单 ID 不匹配，保留原任务状态", uncertain=True)
        raw_status = order.get("status")
        if not isinstance(raw_status, str) or not raw_status:
            raise ScanApiError("平台未返回有效订单状态，保留原任务", uncertain=True)
        task = {"id": order_id, "status": _safe_message(raw_status, secrets).lower(), "canRedispatch": False,
                "message": _safe_message(order.get("message") or order.get("failure_reason") or "平台订单已更新", secrets)}
        aliases = {"payment_expires_at": "expiresAt", "payment_window_ends_at": "paymentWindowEndsAt",
                   "payment_expiry_kind": "paymentExpiryKind", "verification_deadline": "verificationDeadline",
                   "failure_code": "failureCode", "claim_penalty_exempt": "claimPenaltyExempt"}
        for source, target in aliases.items():
            value = order.get(source)
            if isinstance(value, str):
                task[target] = _safe_message(value, secrets)
            elif value is None or type(value) in (int, float, bool):
                task[target] = value
        if task["status"] == "completed":
            task["message"] = "已按平台支付与账号核验规则完成结算"
        return {"task": task, "task_id": order_id, "http_status": status,
                "duplicate": duplicate, "uncertain": status == 202}

    def submit_upi(self, *, cdk, link, access_token, idempotency_key, email=None):
        idempotency_key = validate_idempotency_key(idempotency_key)
        link = validate_payment_link(link)
        if not isinstance(access_token, str) or not access_token.startswith("eyJ") or len(access_token) > 16384:
            raise ValueError("OrderHub 需要所选账号的完整 AT，长度最多 16384 字符")
        if len(link) > 8192:
            raise ValueError("支付链接不能超过 8192 字符")
        status, data, secrets = self._request("POST", "/orders/batch", cdk=cdk,
            body={"items": [{"at": access_token, "link": link}], "dispatch_mode": "auto"},
            key=idempotency_key, submitting=True)
        arrays = {key: data.get(key) for key in ("created", "duplicated", "failed")}
        if any(not isinstance(value, list) for value in arrays.values()) or sum(map(len, arrays.values())) != 1:
            raise ScanApiError("批量结果未能匹配所选账号，请核对原任务", uncertain=True)
        if arrays["failed"]:
            item = arrays["failed"][0]
            if not isinstance(item, dict):
                raise ScanApiError("平台拒收结果格式无效，请核对", uncertain=True)
            error = item.get("error") if isinstance(item.get("error"), dict) else item
            code = _safe_message(error.get("code") or "SUBMIT_REJECTED", secrets)
            message = error.get("message") or (item.get("error") if isinstance(item.get("error"), str) else None) or "该账号提交被拒绝"
            raise ScanApiError(_safe_message(message, secrets), status=400, code=code,
                               uncertain=code == "IDEMPOTENCY_CONFLICT")
        duplicate = bool(arrays["duplicated"])
        item = (arrays["duplicated"] if duplicate else arrays["created"])[0]
        return self._order(item.get("order") if isinstance(item, dict) else None, status, secrets, duplicate=duplicate)

    def get_task(self, *, cdk, task_id):
        if not isinstance(task_id, str) or not _ID.fullmatch(task_id):
            raise ValueError("支付任务 ID 无效")
        status, data, secrets = self._request("GET", "/orders/" + quote(task_id, safe=""), cdk=cdk)
        return self._order(data.get("order"), status, secrets, expected_id=task_id)

    def verify_cdk(self, cdk):
        _, data, secrets = self._request("GET", "/tickets", cdk=cdk)
        summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
        return {"data": {key: value for key, value in summary.items()
                         if key in {"total", "remaining", "used", "available", "pending", "total_uses", "available_uses", "used_uses", "pending_uses"}
                         and type(value) in (int, float)}}


def login(username, password):
    if not isinstance(username, str) or not re.fullmatch(r"\d{6,16}", username, flags=re.ASCII):
        raise ValueError("雇主账号必须是 6–16 位纯数字，保留前导零")
    if not isinstance(password, str) or not (8 <= len(password) <= 20 and re.search(r"[A-Z]", password)
            and re.search(r"[a-z]", password) and re.search(r"[^A-Za-z0-9\s]", password)):
        raise ValueError("密码须为 8–20 位，包含大写、小写字母和特殊符号")
    client = OrderHubClient()
    url = client.api_base + "/auth/login"
    body = {"username": username, "password": password}
    started = time.monotonic()
    operation_log.request(method="POST", url=url, headers={"User-Agent": cfg.SCAN_API_USER_AGENT,
                         "Content-Type": "application/json"}, body=body, timeout=client.timeout,
                         note="OrderHub 数字雇主账号登录", secrets=(password,))
    try:
        with requests.Session() as session:
            response = session.post(url, json=body,
                                    headers={"User-Agent": cfg.SCAN_API_USER_AGENT, "Content-Type": "application/json"},
                                    timeout=client.timeout, allow_redirects=False)
            try:
                data = response.json()
            except ValueError:
                operation_log.response(status=response.status_code, headers=dict(response.headers),
                                       elapsed=time.monotonic() - started, body=getattr(response, "text", ""),
                                       note="OrderHub 登录返回非 JSON 响应", secrets=(password,))
                raise ScanApiError("登录响应无效，请在平台网页核对账号状态") from None
            if not isinstance(data, dict) or data.get("ok") is not True or response.status_code != 200:
                error = data.get("error", {}) if isinstance(data, dict) else {}
                error = error if isinstance(error, dict) else {}
                operation_log.response(status=response.status_code, headers=dict(response.headers),
                                       elapsed=time.monotonic() - started, body=data,
                                       note="OrderHub 登录被拒绝", secrets=(password,))
                rejection = ScanApiError(_safe_message(error.get("message") or "雇主账号登录失败", (password,)),
                                   status=response.status_code if response.status_code >= 400 else 400,
                                   code=_safe_message(error.get("code") or "LOGIN_FAILED", (password,)))
                operation_log.failure(rejection, note="OrderHub 登录失败", secrets=(password,))
                raise rejection
            cookies = session.cookies.copy()
            if not any(cookie.name == "token" and cookie.value for cookie in cookies):
                error = ScanApiError("登录响应缺少平台会话 Cookie，请使用平台网页核对")
                operation_log.failure(error, note="OrderHub 登录缺少会话 Cookie", secrets=(password,))
                raise error
    except requests.RequestException as exc:
        operation_log.failure(exc, note="OrderHub 登录网络异常", secrets=(password,))
        raise ScanApiError("平台登录网络异常，请稍后重试") from None
    operation_log.response(status=200, elapsed=time.monotonic() - started, note="OrderHub 登录成功",
                           body={"user": data.get("user"),
                                 "cookies": {cookie.name: cookie.value for cookie in cookies}},
                           secrets=(password,))
    user = _safe_user(data.get("user"), (password, *(cookie.value for cookie in cookies)))
    if user.get("employer_status") not in {None, "active", "activated"}:
        raise ScanApiError("雇主账号尚未激活，请先由平台管理员处理", status=403, code="EMPLOYER_PENDING")
    identity = "orderhub-user:" + str(user.get("id") or username)
    handle = uuid4().hex
    with _SESSION_LOCK:
        for key in [key for key, value in _SESSIONS.items() if value["expires_at"] <= time.time()]:
            _SESSIONS.pop(key, None)
        _SESSIONS[handle] = {"identity": identity, "api_base": client.api_base, "cookies": cookies,
                             "user": user, "expires_at": time.time() + _SESSION_LIFETIME}
    return handle, user


def get_session(handle):
    with _SESSION_LOCK:
        value = _SESSIONS.get(handle)
        if not value or value["expires_at"] <= time.time():
            _SESSIONS.pop(handle, None)
            raise ValueError("OrderHub 会话已失效，请重新登录数字雇主账号")
        return value


def logout(handle):
    with _SESSION_LOCK:
        value = _SESSIONS.pop(handle, None)
    if value:
        client = OrderHubClient(value["api_base"])
        client.session_auth = value
        try:
            client._request("POST", "/auth/logout", body={})
        except ScanApiError:
            pass  # Always discard local credentials, including on remote failure.
