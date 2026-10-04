"""Authenticated payment routes; upstream credentials never enter browser storage."""
from flask import jsonify, request, session

from core import scan_api_service as service, orderhub_client, plus_activation_service
from core.scan_api_client import ScanApiError, _safe_message

_SESSION_KEY = "orderhub_session_handle"


def _json_object():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError("JSON 请求体必须是对象")
    return data


def _no_store(response):
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


def _error(exc, secret=None):
    if isinstance(exc, (ValueError, LookupError, ScanApiError)):
        text = _safe_message(str(exc), (secret,))
        status = 400 if isinstance(exc, ValueError) else 404 if isinstance(exc, LookupError) else exc.status
        # 上游 401 表示支付 CDK 失效，不是本站会话失效；返回 401 会让前端退出登录。
        status = 400 if status == 401 else status
        if status < 400 or status > 599:
            status = 502
    else:
        text, status = "支付操作异常，请查询原记录核对受理情况", 500
    return _no_store(jsonify({"ok": False, "error": text, "code": getattr(exc, "code", None)})), status


def _auth_session(data):
    mode = data.get("auth_mode", "key")
    if not isinstance(mode, str) or mode not in {"key", "session"}:
        raise ValueError("支付验证方式无效")
    if mode == "session":
        return orderhub_client.get_session(session.get(_SESSION_KEY))
    return None


def register_scan_routes(app):
    @app.post("/api/accounts/activate-plus")
    def activate_plus():
        data, payment, extract = {}, {}, {}
        try:
            data = _json_object()
            payment, extract = data.get("payment"), data.get("extraction")
            payments = plus_activation_service.option_list(payment, "payment")
            plus_activation_service.option_list(extract, "extraction")
            auth_sessions = [_auth_session(item) for item in payments]
            result = plus_activation_service.enqueue_accounts(
                account_ids=data.get("account_ids"), extraction_options=extract,
                payment_options=payment, auth_session=auth_sessions,
                success_group=data.get("success_group"),
            )
            return _no_store(jsonify(result)), 202
        except Exception as exc:
            # Never include upstream credentials echoed by validation errors.
            if isinstance(exc, (ValueError, LookupError)):
                objects = [obj for value in (payment, extract)
                           for obj in (value if isinstance(value, list) else [value]) if isinstance(obj, dict)]
                secrets = [obj.get("cdk") for obj in objects]
                for obj in objects:
                    secrets.append(obj.get("proxy_url"))
                    if isinstance(obj.get("entry_proxies"), list):
                        secrets.extend(p for p in obj["entry_proxies"] if isinstance(p, str))
                error = _safe_message(str(exc), secrets)
                return _no_store(jsonify({"ok": False, "error": error})), 400
            return _no_store(jsonify({"ok": False, "error": "Plus 开通提交异常，请刷新账号核对任务状态"})), 500

    @app.post("/api/accounts/scan-requests")
    def submit_scan_requests():
        data = {}
        try:
            data = _json_object()
            result = service.submit_accounts(
                account_ids=data.get("account_ids"), cdk=data.get("cdk"), cdk_id=data.get("cdk_id"),
                provider=data.get("provider", "v1"), idempotency_key=data.get("idempotency_key"),
                auth_session=_auth_session(data),
            )
            return _no_store(jsonify(result))
        except Exception as exc:
            return _error(exc, data.get("cdk"))

    @app.post("/api/accounts/scan-requests/query")
    def query_scan_requests():
        data = {}
        try:
            data = _json_object()
            result = service.query_accounts(account_ids=data.get("account_ids"), cdk=data.get("cdk"),
                                            cdk_id=data.get("cdk_id"), auth_session=_auth_session(data))
            return _no_store(jsonify(result))
        except Exception as exc:
            return _error(exc, data.get("cdk"))

    @app.get("/api/accounts/<int:account_id>/scan-request")
    def local_scan_request(account_id):
        try:
            return _no_store(jsonify({"ok": True, "item": service.local_status(account_id)}))
        except Exception as exc:
            return _error(exc)

    @app.post("/api/payments/verify")
    def verify_payment_cdk():
        data = {}
        try:
            data = _json_object()
            result = service.verify_cdk(provider=data.get("provider", "v1"), cdk=data.get("cdk"),
                                        cdk_id=data.get("cdk_id"), auth_session=_auth_session(data))
            return _no_store(jsonify({"ok": True, **result}))
        except Exception as exc:
            return _error(exc, data.get("cdk"))

    @app.post("/api/payments/orderhub/login")
    def payment_employer_login():
        data = {}
        try:
            data = _json_object()
            handle, user = orderhub_client.login(data.get("username"), data.get("password"))
            old = session.get(_SESSION_KEY)
            session[_SESSION_KEY] = handle  # Only opaque ID in the HttpOnly Flask cookie.
            if old:
                orderhub_client.logout(old)
            return _no_store(jsonify({"ok": True, "user": user}))
        except Exception as exc:
            return _error(exc, data.get("password"))

    @app.get("/api/payments/orderhub/session")
    def payment_employer_session():
        try:
            saved = orderhub_client.get_session(session.get(_SESSION_KEY))
            return _no_store(jsonify({"ok": True, "logged_in": True, "user": saved["user"]}))
        except ValueError:
            session.pop(_SESSION_KEY, None)
            return _no_store(jsonify({"ok": True, "logged_in": False}))

    @app.post("/api/payments/orderhub/logout")
    def payment_employer_logout():
        orderhub_client.logout(session.pop(_SESSION_KEY, None))
        return _no_store(jsonify({"ok": True}))

    return app
