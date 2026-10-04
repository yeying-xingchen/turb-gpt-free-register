# -*- coding: utf-8 -*-
"""已保存的支付平台与支付 CDK 管理路由。

与提链 CDK 管理对称：列表只返回掩码，明文凭据永不进入响应体。明文只在
``/validate`` 与提交/查询时由服务端内部读取。父应用负责登录鉴权中间件。
"""
from __future__ import annotations

from typing import Any

from flask import jsonify, request

from core import payment_provider_store as store
from core import scan_api_service as service

_SECRET_KEYS = {
    "cdk", "code", "secret", "token", "access_token", "refresh_token", "password",
    "authorization", "api_key", "apikey", "client_secret",
}


def _json_object() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError("JSON 请求体必须是对象")
    return data


def _redact(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    needles = tuple(item for item in secrets if isinstance(item, str) and item)
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            result[key] = "[redacted]" if str(key).lower() in _SECRET_KEYS else _redact(item, needles)
        return result
    if isinstance(value, list):
        return [_redact(item, needles) for item in value]
    if isinstance(value, str):
        result = value
        for needle in needles:
            result = result.replace(needle, "[redacted]")
        return result
    return value


def _message(exc: Exception, secrets: tuple[str, ...] = ()) -> str:
    return _redact(str(exc).strip() or "请求失败", secrets)[:500]


def _error(exc: Exception, *, upstream=False, secrets: tuple[str, ...] = ()):
    if isinstance(exc, ValueError):
        status = 400
    elif isinstance(exc, LookupError):
        status = 404
    elif upstream:
        status = getattr(exc, "status", 502)
        # 上游 401 是支付 CDK 失效，不是本站会话失效；返回 401 会让前端退出登录。
        status = 400 if status == 401 else status
        if not 400 <= int(status) <= 599:
            status = 502
    else:
        status = 500
    return jsonify({"ok": False, "error": _message(exc, secrets),
                    "code": getattr(exc, "code", None)}), status


def _no_store(response):
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


def register_payment_provider_routes(app):
    """平台与 CDK 的增删改查，以及用已保存凭据查询额度。"""

    @app.get("/api/payment-providers")
    def payment_providers_list():
        try:
            store.ensure_defaults()
            providers = [_redact(item) for item in store.list_providers()]
            return _no_store(jsonify({"ok": True, "items": providers}))
        except Exception as exc:
            return _error(exc)

    @app.get("/api/payment-providers/<provider_type>/credentials")
    def payment_provider_credentials(provider_type: str):
        """提交/开通窗口用：某平台的已保存凭据概览（无明文）。"""
        try:
            store.ensure_defaults()
            return _no_store(jsonify({"ok": True, "item": _redact(store.credential_summary(provider_type))}))
        except Exception as exc:
            return _error(exc)

    @app.post("/api/payment-providers")
    def payment_provider_create():
        try:
            item = store.save_provider(_json_object())
            return jsonify({"ok": True, "item": _redact(item)}), 201
        except Exception as exc:
            return _error(exc)

    @app.put("/api/payment-providers/<int:provider_id>")
    def payment_provider_update(provider_id: int):
        try:
            item = store.save_provider(_json_object(), provider_id=provider_id)
            return jsonify({"ok": True, "item": _redact(item)})
        except Exception as exc:
            return _error(exc)

    @app.delete("/api/payment-providers/<int:provider_id>")
    def payment_provider_delete(provider_id: int):
        try:
            store.delete_provider(provider_id)
            return jsonify({"ok": True})
        except Exception as exc:
            return _error(exc)

    @app.post("/api/payment-providers/<int:provider_id>/cdks")
    def payment_cdk_create(provider_id: int):
        data = None
        try:
            data = _json_object()
            item = store.save_cdk(provider_id, data)
            return jsonify({"ok": True, "item": _redact(item)}), 201
        except Exception as exc:
            secret = ((data or {}).get("cdk"),) if isinstance(data, dict) else ()
            return _error(exc, secrets=secret)

    @app.put("/api/payment-cdks/<int:cdk_id>")
    def payment_cdk_update(cdk_id: int):
        data = None
        secrets: tuple[str, ...] = ()
        try:
            data = _json_object()
            raw = store.get_cdk(cdk_id)
            if raw is None:
                raise LookupError("CDK 不存在")
            provider_id = data.get("provider_id", raw["provider_id"])
            if isinstance(data.get("cdk"), str):
                secrets = (data["cdk"],)
            item = store.save_cdk(provider_id, data, cdk_id=cdk_id)
            return jsonify({"ok": True, "item": _redact(item)})
        except Exception as exc:
            return _error(exc, secrets=secrets)

    @app.delete("/api/payment-cdks/<int:cdk_id>")
    def payment_cdk_delete(cdk_id: int):
        try:
            store.delete_cdk(cdk_id)
            return jsonify({"ok": True})
        except Exception as exc:
            return _error(exc)

    @app.post("/api/payment-cdks/<int:cdk_id>/validate")
    def payment_cdk_validate(cdk_id: int):
        """用已保存的凭据查询额度；只回传数字额度，不含身份信息。"""
        raw = None
        try:
            raw = store.get_cdk(cdk_id)
            if raw is None:
                raise LookupError("CDK 不存在")
            saved = store.resolve_saved(cdk_id)
            result = service.verify_cdk(provider=saved["provider_type"], cdk=saved["cdk"])
            return _no_store(jsonify({"ok": True, "result": _redact(result, (saved["cdk"],))}))
        except Exception as exc:
            secret = ((raw or {}).get("cdk"),) if isinstance(raw, dict) else ()
            return _error(exc, upstream=True, secrets=secret)

    return app
