# -*- coding: utf-8 -*-
"""Management routes for persisted extract-link providers and CDKs.

The parent application owns authentication middleware and service behavior.  This
module only wires JSON routes and converts expected domain errors into stable HTTP
responses.  ``register_extract_routes(app)`` is intentionally side-effect free
until called by the application factory.
"""
from __future__ import annotations

from typing import Any

from flask import jsonify, request

from core import extract_link_service as service
from core import extract_provider_store as store

_SECRET_KEYS = {
    "cdk", "code", "secret", "token", "access_token", "refresh_token", "password",
    "authorization", "api_key", "apikey", "client_secret",
}
_ACTIONS = {"refresh", "cancel", "blik-code"}


def _json_object() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError("JSON 请求体必须是对象")
    return data


def _redact(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    """Remove credential values from arbitrary upstream/service JSON."""
    needles = tuple(item for item in secrets if isinstance(item, str) and item)
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if str(key).lower() in _SECRET_KEYS:
                result[key] = "[redacted]"
            else:
                result[key] = _redact(item, needles)
        return result
    if isinstance(value, list):
        return [_redact(item, needles) for item in value]
    if isinstance(value, tuple):
        return [_redact(item, needles) for item in value]
    if isinstance(value, str):
        result = value
        for needle in needles:
            result = result.replace(needle, "[redacted]")
        return result
    return value


def _message(exc: Exception, secrets: tuple[str, ...] = ()) -> str:
    text = str(exc).strip() or "请求失败"
    return _redact(text, secrets)[:500]


def _error(exc: Exception, *, upstream=False, secrets: tuple[str, ...] = ()):
    if isinstance(exc, ValueError):
        status = 400
    elif isinstance(exc, LookupError):
        status = 404
    elif isinstance(exc, RuntimeError):
        status = 502 if upstream else 409
    elif upstream and isinstance(exc, (OSError, TimeoutError, ConnectionError)):
        status = 502
    else:
        status = 500
    return jsonify({"ok": False, "error": _message(exc, secrets)}), status


def _no_store(response):
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    return response


def _result_status(result: Any) -> int:
    if isinstance(result, dict) and (result.get("accepted") or result.get("started")):
        return 202
    return 200


def register_extract_routes(app):
    """Register provider/CDK management and account task-action endpoints."""

    @app.get("/api/extract-link/providers")
    def extract_providers_list():
        try:
            store.ensure_defaults()
            response = jsonify({"ok": True, "items": _redact(service.list_providers())})
            return _no_store(response)
        except Exception as exc:
            return _error(exc)

    @app.post("/api/extract-link/providers")
    def extract_provider_create():
        try:
            item = store.save_provider(_json_object())
            return jsonify({"ok": True, "item": _redact(item)}), 201
        except Exception as exc:
            return _error(exc)

    @app.put("/api/extract-link/providers/<int:provider_id>")
    def extract_provider_update(provider_id: int):
        try:
            item = store.save_provider(_json_object(), provider_id=provider_id)
            return jsonify({"ok": True, "item": _redact(item)})
        except Exception as exc:
            return _error(exc)

    @app.delete("/api/extract-link/providers/<int:provider_id>")
    def extract_provider_delete(provider_id: int):
        try:
            store.delete_provider(provider_id)
            return jsonify({"ok": True})
        except Exception as exc:
            return _error(exc)

    @app.post("/api/extract-link/providers/<int:provider_id>/cdks")
    def extract_cdk_create(provider_id: int):
        data = None
        try:
            data = _json_object()
            item = store.save_cdk(provider_id, data)
            return jsonify({"ok": True, "item": _redact(item)}), 201
        except Exception as exc:
            secret = ((data or {}).get("cdk"),) if isinstance(data, dict) else ()
            return _error(exc, secrets=secret)

    @app.put("/api/extract-link/cdks/<int:cdk_id>")
    def extract_cdk_update(cdk_id: int):
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

    @app.delete("/api/extract-link/cdks/<int:cdk_id>")
    def extract_cdk_delete(cdk_id: int):
        try:
            store.delete_cdk(cdk_id)
            return jsonify({"ok": True})
        except Exception as exc:
            return _error(exc)

    @app.post("/api/extract-link/cdks/<int:cdk_id>/validate")
    def extract_cdk_validate_saved(cdk_id: int):
        try:
            raw = store.get_cdk(cdk_id)
            if raw is None:
                raise LookupError("CDK 不存在")
            result = service.query_cdk(provider_id=raw["provider_id"], cdk_id=cdk_id)
            response = jsonify({"ok": True, "result": _redact(result, (raw["cdk"],))})
            return _no_store(response)
        except Exception as exc:
            raw = locals().get("raw") or {}
            return _error(exc, upstream=True, secrets=((raw.get("cdk"),) if isinstance(raw, dict) else ()))

    @app.post("/api/extract-link/cdk")
    def extract_cdk_validate_raw():
        data = None
        try:
            data = _json_object()
            provider_id = data.get("provider_id")
            cdk_id = data.get("cdk_id")
            code = data.get("cdk")
            if cdk_id is None and code is None:
                raise ValueError("必须提供 cdk_id 或 cdk")
            if cdk_id is not None and code is not None:
                raise ValueError("cdk_id 与 cdk 只能提供一个")
            if provider_id is not None and type(provider_id) is not int:
                raise ValueError("provider_id 必须是整数")
            if cdk_id is not None and type(cdk_id) is not int:
                raise ValueError("cdk_id 必须是整数")
            if code is not None and (not isinstance(code, str) or not code.strip()):
                raise ValueError("cdk 必须是非空字符串")
            result = service.query_cdk(provider_id=provider_id, cdk_id=cdk_id, cdk=code)
            response = jsonify({"ok": True, "result": _redact(result, (code,) if isinstance(code, str) else ())})
            return _no_store(response)
        except Exception as exc:
            secret = ((data or {}).get("cdk"),) if isinstance(data, dict) else ()
            return _error(exc, upstream=True, secrets=secret)

    @app.post("/api/accounts/<int:account_id>/extract-link/<action>")
    def extract_account_task_action(account_id: int, action: str):
        data = None
        try:
            if action not in _ACTIONS:
                raise LookupError("操作不存在")
            data = _json_object()
            blik_code = data.get("blik_code")
            if action == "blik-code":
                if not isinstance(blik_code, str) or not blik_code.strip():
                    raise ValueError("blik_code 必须是非空字符串")
            else:
                blik_code = None
            result = service.task_action(account_id, action, blik_code=blik_code)
            return jsonify({"ok": True, **(result if isinstance(result, dict) else {"result": result})}), _result_status(result)
        except Exception as exc:
            secret = (data.get("blik_code"),) if isinstance(data, dict) and isinstance(data.get("blik_code"), str) else ()
            return _error(exc, secrets=secret)

    return app
