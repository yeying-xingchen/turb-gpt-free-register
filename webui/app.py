# -*- coding: utf-8 -*-
"""
Flask 本地控制台。

复用现有后端：
    core.db                     —— 账号 / 邮箱池 / 任务的 SQLite 持久化与查询
    core.registration_service   —— 线程池批量注册 + 任务日志
    webui.config_editor         —— 安全读写 config/*.py

所有接口返回 JSON；前端是单文件 templates/index.html（原生 JS + fetch）。
默认绑定 127.0.0.1，仅本地访问。
"""
import logging
import gzip
import json
import math
import threading
import time
import uuid
from datetime import datetime as _datetime, timezone as _timezone
from urllib.parse import urlparse, urlunparse

from flask import Flask, Response, jsonify, make_response, render_template, request
import pyotp

from core import codex_retry_service, db, plan_check_service, payment_method_service, extract_link_service, codex_agent_service, live_check_service, momo_activation_service
from webui.auth import init_auth, register_auth_routes
from core import registration_service as svc
from core import account_import
from webui import config_editor

logger = logging.getLogger(__name__)


def _safe_public_url(value: object) -> str:
    """Return a sub2api URL without embedded credentials or query secrets."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
    except Exception:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    # Keep only scheme/host/port/path.  Userinfo and query/fragment commonly
    # carry admin credentials or one-time tokens and must not enter list JSON.
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunparse((parsed.scheme, netloc, parsed.path or "", "", "", ""))


_POOL_SOURCE_VALUES = frozenset(("all", "outlook", "generic_api", "imap", "cloudflare_domain"))


def _pool_source_arg(default: str = "outlook") -> str:
    src = str(request.args.get("source") or "").strip().lower()
    if not src and request.method == "POST":
        data = request.get_json(silent=True) or {}
        src = str(data.get("source") or data.get("type") or "").strip().lower()
    return src if src in _POOL_SOURCE_VALUES else default


def _with_pool_source(rows: list[dict], source: str) -> list[dict]:
    out = []
    for r in rows:
        x = dict(r)
        x["source"] = source
        if not x.get("copy_line"):
            x["copy_line"] = x.get("email") or ""
        out.append(x)
    return out




def _matches_query(row: dict, q: str | None) -> bool:
    q = str(q or "").strip().lower()
    if not q:
        return True
    try:
        return q in "\n".join(str(v) for v in row.values()).lower()
    except Exception:
        return False


def _paginate_items(items: list[dict], *, page: int, page_size: int) -> dict:
    page = max(1, int(page or 1))
    page_size = max(1, min(500, int(page_size or 50)))
    total = len(items)
    offset = (page - 1) * page_size
    return {
        "ok": True,
        "items": items[offset:offset + page_size],
        "total": total,
        "page": page,
        "page_size": page_size,
        "offset": offset,
        "limit": page_size,
    }


def _compact_account_for_list(row: dict) -> dict:
    """账号列表轻量对象：只返回当前表格渲染和按钮判断必需字段。

    原则：
    - 不返回完整 Token / Token 预览 / TOTP Secret / Agent Token。
    - 时间戳、错误原因、提链详情等只在前端确实要展示时返回；空值不返回。
    - 复制/下载敏感内容时再通过 /secret 接口按需读取。
    """
    out = {
        "id": row.get("id"),
        "email": row.get("email"),
        "has_access_token": bool(str(row.get("access_token") or "").strip()),
        "totp_enabled": bool(row.get("totp_secret")),
        "codex_agent_has_token": bool(str(row.get("codex_agent_token") or "").strip()),
    }

    # 密码、extra_json、registration_password 都属于敏感凭证；账号列表不返回
    # 这些字段，复制/查阅敏感值时统一通过受控 secret 接口按需读取。
    # 这些是列表固定列直接展示字段。
    for key in (
        "user_name", "email_source", "original_email", "note", "archived", "created_at",
        "plan_type", "current_plan_type", "plus_trial_eligible",
        "plan_check_status", "payment_method_check_status", "codex_status", "codex_agent_status",
        "totp_setup_status", "momo_activation_status",
    ):
        if key in row:
            out[key] = row.get(key)

    if row.get("plan_check_status") in ("queued", "running") or row.get("plan_check_ok") is False:
        out["plan_check_ok"] = row.get("plan_check_ok")

    # 下面字段仅在有值时返回，避免每行堆满 null/空字符串/内部状态。
    optional_keys = (
        # 套餐展示补充：付费到期/折扣/失败原因。
        "plan_check_error", "plan_expires_at", "plan_renews_at", "renews_at",
        "payment_method_check_ok", "payment_method_check_error", "payment_method_check_trigger",
        "payment_method_check_queued_at", "payment_method_check_started_at",
        "payment_method_check_completed_at", "payment_method_checked_at",
        "payment_method_check_proxy_used", "payment_method_available_channels",
        "payment_method_channel_availability", "payment_method_regions",
        "payment_method_region_errors", "payment_method_last_success_at",
        "billing_period", "billing_currency", "discount_amount", "discount_type",
        "discount_expires_at", "discount_promo_campaign_id",
        "token_expired", "token_expires_at",
        # 查活状态。
        "live_check_status", "live_check_error", "live_checked_at",
        "live_check_proxy_used", "live_check_fingerprint_text",
        # 提链成功/失败时才需要。
        "extract_link_status", "extract_link_type", "extract_link_message", "extract_link_error",
        "extract_link_long_url", "extract_link_copy_paste", "extract_link_image_url_png",
        "extract_link_image_url_svg", "extract_link_expires_at",
        # Codex / Agent 状态提示。
        "codex_error", "codex_agent_message", "codex_agent_runtime_id",
        "codex_agent_sub2api_url", "codex_agent_sub2api_mode", "codex_agent_sub2api_total",
        "totp_setup_error", "totp_setup_message", "totp_setup_started_at", "totp_setup_completed_at",
        "email_change_status", "email_change_error", "email_change_new_email",
        "email_change_started_at", "email_change_completed_at",
        "momo_activation_ok", "momo_activation_trigger", "momo_activation_queued_at",
        "momo_activation_started_at", "momo_activation_completed_at", "momo_activation_checked_at",
        "momo_activation_error", "momo_activation_message", "momo_activation_job_id",
        "momo_activation_remote_status", "momo_activation_result_summary",
    )
    for key in optional_keys:
        value = row.get(key)
        if value is not None and value != "":
            out[key] = _safe_public_url(value) if key == "codex_agent_sub2api_url" else value
    plan = str(row.get("current_plan_type") or row.get("plan_type") or "").lower()
    if any(x in plan for x in ("plus", "pro", "team", "go")):
        expire = row.get("expires_at")
        if expire:
            out["expires_at"] = expire
    return out


ACCOUNT_EXPORT_FIELDS = (
    "email", "email_password", "password", "totp_secret", "totp_code",
    "id", "email_source", "user_name", "note", "plan_type", "current_plan_type",
    "live_check_status", "codex_status", "created_at", "updated_at",
    "access_token", "codex_agent_token", "copy_line",
)
_ACCOUNT_EXPORT_FIELD_LABELS = {
    "email": "邮箱",
    "email_password": "邮箱密码",
    "password": "密码（账号登录）",
    "totp_secret": "2FA 密钥",
    "totp_code": "当前 2FA 验证码",
    "id": "账号 ID",
    "email_source": "邮箱来源",
    "user_name": "用户名",
    "note": "备注",
    "plan_type": "套餐",
    "current_plan_type": "当前套餐",
    "live_check_status": "查活状态",
    "codex_status": "Codex 状态",
    "created_at": "创建时间",
    "updated_at": "更新时间",
    "access_token": "access_token（AT）",
    "codex_agent_token": "Codex Agent Token",
    "copy_line": "完整整行（含 AT）",
}
_ACCOUNT_EXPORT_SENSITIVE_FIELDS = frozenset({
    "password", "totp_secret", "totp_code", "email_password", "access_token",
    "codex_agent_token", "copy_line",
})


def _coerce_export_account_id(raw: object) -> int:
    """Accept only integer IDs, rejecting bools, floats and non-positive values."""
    if isinstance(raw, bool):
        raise ValueError("ID 非法")
    if isinstance(raw, int):
        acc_id = raw
    elif isinstance(raw, str) and raw.strip():
        text = raw.strip()
        if text.startswith(("+", "-")):
            digits = text[1:]
        else:
            digits = text
        if not digits.isdigit():
            raise ValueError("ID 非法")
        acc_id = int(text, 10)
    else:
        raise ValueError("ID 非法")
    if acc_id <= 0:
        raise ValueError("ID 必须是正整数")
    return acc_id


def _parse_export_filter_date(value: object, *, end: bool = False) -> tuple[str | None, _datetime | None]:
    """Validate an ISO date/date-time filter and return its DB value and UTC-naive value."""
    text = str(value or "").strip()
    if not text:
        return None, None
    try:
        if len(text) == 10:
            parsed = _datetime.strptime(text, "%Y-%m-%d")
            if end:
                parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
            return text, parsed
        parsed = _datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValueError("日期必须是 YYYY-MM-DD 或 ISO 日期时间") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(_timezone.utc).replace(tzinfo=None)
    return parsed.isoformat(timespec="microseconds"), parsed


def _normalise_account_export_filters(raw: object) -> dict:
    """Normalize account-list filters before using them for an export scope."""
    filters = raw if isinstance(raw, dict) else {}
    archived = str(filters.get("archived", "0") or "0").strip().lower()
    if archived not in {"0", "1", "true", "false", "yes", "no", "only", "all", "include"}:
        raise ValueError("archived 仅支持 0/1/only/all")
    status_filter = str(filters.get("status", "") or "").strip().lower()
    allowed_status_filters = {
        "", "all", "plus", "live", "alive", "failed", "live_failed",
        "liveness_failed", "trial", "free_trial", "plus_trial",
        "deactivated", "dead", "invalid",
    }
    if status_filter not in allowed_status_filters:
        raise ValueError("status 仅支持 all/plus/live/failed/trial/deactivated")
    date_from, parsed_from = _parse_export_filter_date(filters.get("date_from"), end=False)
    date_to, parsed_to = _parse_export_filter_date(filters.get("date_to"), end=True)
    if parsed_from is not None and parsed_to is not None and parsed_from > parsed_to:
        raise ValueError("date_from 不能晚于 date_to")
    return {
        "archived": archived,
        "plan_filter": str(filters.get("plan", "") or "").strip().lower(),
        "codex_filter": str(filters.get("codex_status", "") or "").strip().lower(),
        "totp_filter": str(
            filters.get("totp_status") or filters.get("totp_filter")
            or filters.get("twofa_status") or ""
        ).strip().lower(),
        "status_filter": status_filter,
        "q": str(filters.get("q", "") or "").strip(),
        "date_from": date_from,
        "date_to": date_to,
    }


def _csv_safe_cell(value: object) -> str:
    """Prevent spreadsheet formula execution for user-controlled CSV values."""
    text = str(value or "")
    return "'" + text if text[:1] in {"=", "+", "-", "@"} else text


def _txt_safe_cell(value: object, delimiter: str) -> str:
    """Quote TXT cells that contain separators/newlines while keeping simple lines readable."""
    text = str(value or "")
    if delimiter in text or "\n" in text or "\r" in text or '"' in text:
        return '"' + text.replace('"', '""') + '"'
    return text


def _account_login_password(row: dict) -> str:
    """返回 ChatGPT 登录密码，不把邮箱素材密码误当成登录密码。"""
    extra_raw = row.get("extra_json")
    extra = {}
    if isinstance(extra_raw, str) and extra_raw.strip():
        try:
            parsed = json.loads(extra_raw)
            extra = parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            extra = {}
    elif isinstance(extra_raw, dict):
        extra = extra_raw
    explicit = extra.get("registration_password") or row.get("registration_password")
    if explicit:
        return str(explicit).strip()
    line_format = str(row.get("account_line_format") or "").strip().lower().replace("-", "_")
    email_source = str(row.get("email_source") or "").strip().lower()
    is_chatgpt_material = line_format in {"chatgpt_api", "chatgpt_api_no_code_url", "chatgpt_no_code_url"} or (
        email_source == "generic_api" and bool(str(row.get("code_url") or "").strip())
    )
    return str(row.get("password") or "").strip() if is_chatgpt_material else ""


def _account_email_password(row: dict) -> str:
    """返回邮箱素材密码；generic_api 的 password 是 ChatGPT 密码，不能回退使用。"""
    explicit = str(row.get("email_password") or "").strip()
    if explicit:
        return explicit
    source = str(row.get("email_source") or "").strip().lower()
    if source == "imap":
        value = str(row.get("imap_password") or row.get("password") or "").strip()
        if value:
            return value
        try:
            from core import db as _db
            pool_row = _db.get_imap_email_by_email(str(row.get("email") or ""))
            return str((pool_row or {}).get("imap_password") or (pool_row or {}).get("password") or "").strip()
        except Exception:
            return ""
    if source == "outlook":
        return str(row.get("password") or "").strip()
    return ""


def _account_totp_code(row: dict) -> str:
    secret = str(row.get("totp_secret") or "").strip()
    if not secret:
        return ""
    try:
        return pyotp.TOTP(secret).now()
    except Exception:
        return ""


def _account_export_password(row: dict) -> str:
    return _account_login_password(row)


def _account_export_field_value(row: dict, field: str) -> str:
    if field == "password":
        return _account_export_password(row)
    if field == "email_password":
        return _account_email_password(row)
    if field == "totp_secret":
        return str(row.get("totp_secret") or "").strip()
    if field == "totp_code":
        return _account_totp_code(row)
    if field == "copy_line":
        try:
            from core.db import _account_line
            return str(_account_line(row) or "")
        except Exception:
            return str(row.get("copy_line") or "")
    if field == "access_token":
        return str(row.get("access_token") or "")
    if field == "codex_agent_token":
        return str(row.get("codex_agent_token") or "")
    if field == "id":
        return str(row.get("id") or "")
    if field == "email":
        return str(row.get("email") or "")
    value = row.get(field)
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value or "")


def _account_secret_value(row: dict, field: str) -> str:
    field = (field or "").strip()
    if field == "email":
        # 邮箱是非敏感元数据，但通过统一 secret-bulk 返回可以保证跨页复制时
        # 不依赖当前页列表，也不会把 access_token/整行凭证一起带出。
        return str(row.get("email") or "")
    if field == "access_token":
        return str(row.get("access_token") or "")
    if field == "email_password":
        return _account_email_password(row)
    if field == "copy_line":
        try:
            from core.db import _account_line

            return str(_account_line(row) or "")
        except Exception:
            return str(row.get("copy_line") or "")
    if field == "codex_agent_token":
        return str(row.get("codex_agent_token") or "")
    if field == "totp_secret":
        return str(row.get("totp_secret") or "").strip()
    if field == "totp_code":
        return _account_totp_code(row)
    if field == "password":
        return _account_login_password(row)
    raise ValueError("field 仅支持 email/access_token/copy_line/codex_agent_token/totp_secret/totp_code/password/email_password")


def _compact_job_for_list(row: dict) -> dict:
    """注册任务列表轻量对象：只返回表格展示和按钮判断需要的字段。"""
    out = {
        "id": row.get("id"),
        "status": row.get("status"),
    }
    for key in (
        "parent_job_id", "retry_attempt", "email", "started_at", "completed_at",
        "display_status", "retryable", "retry_action", "retry_label",
        "manual_otp_required",
    ):
        value = row.get(key)
        if value is not None and value != "" and value is not False:
            out[key] = value
    err = str(row.get("error_message") or "").strip()
    if err:
        # 列表只需要摘要；完整错误和堆栈看“任务日志”。
        out["error_message"] = err[:240] + ("…" if len(err) > 240 else "")
    traffic = row.get("network_traffic")
    if isinstance(traffic, dict) and traffic.get("available"):
        # 流量统计只包含字节计数，不带 URL/Header/请求体，可直接随任务列表返回。
        out["network_traffic"] = traffic
    return out


def _job_status_counts(rows: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    counts["active"] = sum(int(counts.get(s, 0) or 0) for s in ("pending", "running", "stopping"))
    return counts


def _read_log_tail(path, *, max_bytes: int, default_running: bool = False, running_fn=None) -> dict:
    if not path.exists():
        return {"ok": True, "log": "", "running": bool(default_running)}
    size = path.stat().st_size
    with path.open("rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
        content = f.read().decode("utf-8", errors="replace")
    running = bool(default_running)
    if callable(running_fn):
        try:
            running = bool(running_fn())
        except Exception:
            pass
    return {"ok": True, "log": content, "running": running}

def _accepts_gzip(accept_encoding: str | None) -> bool:
    """Return whether the client explicitly accepts gzip with a valid q value."""
    text = str(accept_encoding or "").strip()
    if not text:
        return False
    explicit: float | None = None
    wildcard: float | None = None
    for raw_item in text.split(","):
        parts = [part.strip() for part in raw_item.split(";")]
        coding = parts[0].lower()
        if not coding:
            continue
        quality = 1.0
        for parameter in parts[1:]:
            key, separator, value = parameter.partition("=")
            if key.strip().lower() != "q" or not separator:
                continue
            try:
                parsed_quality = float(value.strip())
                quality = parsed_quality if math.isfinite(parsed_quality) and 0.0 <= parsed_quality <= 1.0 else 0.0
            except (TypeError, ValueError):
                quality = 0.0
            break
        if coding == "gzip":
            # If a client repeats a coding, the last explicit declaration is
            # the one most HTTP parsers apply; an explicit q=0 still overrides *.
            explicit = quality
        elif coding == "*":
            wildcard = quality
    accepted_quality = explicit if explicit is not None else (wildcard or 0.0)
    return accepted_quality > 0.0


def create_app(auth_code: str | None = None) -> Flask:
    app = Flask(__name__, template_folder="templates")
    _prepared_downloads: dict[str, dict] = {}

    @app.after_request
    def _compress_json_response(response: Response):
        """默认对 JSON API 响应启用 gzip，减少本地前端拉取大列表的传输体积。"""
        accept_encoding = request.headers.get("Accept-Encoding")
        gzip_allowed = _accepts_gzip(accept_encoding)
        if (
            response.direct_passthrough
            or response.headers.get("Content-Encoding")
            or not gzip_allowed
        ):
            return response
        mimetype = (response.mimetype or "").lower()
        if mimetype != "application/json":
            return response
        data = response.get_data()
        if not data or len(data) < 1024:
            return response
        compressed = gzip.compress(data, compresslevel=6)
        if len(compressed) >= len(data):
            return response
        response.set_data(compressed)
        response.headers["Content-Encoding"] = "gzip"
        response.headers["Content-Length"] = str(len(compressed))
        vary = response.headers.get("Vary")
        response.headers["Vary"] = "Accept-Encoding" if not vary else f"{vary}, Accept-Encoding"
        return response

    def _put_prepared_download(content: bytes, filename: str, mimetype: str = "application/zip") -> str:
        now = time.time()
        # 顺手清理 10 分钟前的临时下载，避免内存堆积。
        for k, v in list(_prepared_downloads.items()):
            if now - float(v.get("created_at") or 0) > 600:
                _prepared_downloads.pop(k, None)
        download_id = uuid.uuid4().hex
        _prepared_downloads[download_id] = {
            "content": bytes(content),
            "filename": filename,
            "mimetype": mimetype,
            "created_at": now,
        }
        return download_id

    @app.get("/api/downloads/<download_id>")
    def api_prepared_download(download_id: str):
        item = _prepared_downloads.pop(str(download_id or ""), None)
        if not item:
            return jsonify({"ok": False, "error": "下载已过期或不存在，请重新生成"}), 404
        content = item.get("content") or b""
        filename = item.get("filename") or "download.zip"
        mimetype = item.get("mimetype") or "application/octet-stream"
        return Response(
            content,
            mimetype=mimetype,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(content)),
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
                "X-Download-Options": "noopen",
            },
        )

    init_auth(app, auth_code=auth_code)
    register_auth_routes(app)
    recovered_plan_checks = db.recover_interrupted_plan_checks()
    if recovered_plan_checks:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的套餐查询状态", recovered_plan_checks)
    recovered_payment_checks = db.recover_interrupted_payment_method_checks()
    if recovered_payment_checks:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的支付方式查询状态", recovered_payment_checks)
    recovered_extract_links = db.recover_interrupted_extract_links()
    if recovered_extract_links:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的提链状态", recovered_extract_links)
    recovered_live_checks = db.recover_interrupted_live_checks()
    if recovered_live_checks:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的查活状态", recovered_live_checks)
    recovered_codex_agents = db.recover_interrupted_codex_agents()
    if recovered_codex_agents:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的 Codex Agent Token 状态", recovered_codex_agents)
    recovered_totp_setups = db.recover_interrupted_totp_setups()
    if recovered_totp_setups:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的 2FA 状态", recovered_totp_setups)
    recovered_email_changes = db.recover_interrupted_email_changes()
    if recovered_email_changes:
        logger.warning("已恢复 %s 个因 WebUI 重启中断的邮箱换绑状态", recovered_email_changes)
    recover_activations = getattr(db, "recover_interrupted_activations", None)
    if callable(recover_activations):
        recovered_momo = recover_activations()
        if recovered_momo:
            logger.warning("已恢复 %s 个因 WebUI 重启中断的 MoMo 开通状态", recovered_momo)

    # ----------------------------------------------------------
    # 页面
    # ----------------------------------------------------------
    @app.get("/")
    def index():
        requested_ui = (request.args.get("ui") or "").strip().lower()
        if requested_ui in {"legacy", "modern"}:
            ui_mode = requested_ui
        else:
            ui_mode = (request.cookies.get("ui_mode") or "modern").strip().lower()
            if ui_mode not in {"legacy", "modern"}:
                ui_mode = "modern"

        template_name = "index_legacy.html" if ui_mode == "legacy" else "index.html"
        resp = make_response(render_template(template_name))
        if requested_ui in {"legacy", "modern"}:
            resp.set_cookie("ui_mode", ui_mode, max_age=60 * 60 * 24 * 365, samesite="Lax")
        return resp

    # ----------------------------------------------------------
    # 统计概览
    # ----------------------------------------------------------
    @app.get("/api/summary")
    def api_summary():
        from config import email as _email_cfg
        from core.email_provider import parse_email_sources
        pool = {"total": 0, "available": 0, "used": 0, "failed": 0}
        for src in parse_email_sources(_email_cfg.EMAIL_SOURCE):
            # 动态服务地址按需生成，不属于本地邮箱池。
            if src in ("gptmail", "mailnest", "cloudmail", "cloudflare", "djbnb", "remail"):
                continue
            one = (
                db.generic_api_email_pool_summary() if src == "generic_api"
                else db.imap_email_pool_summary() if src == "imap"
                else db.domain_email_pool_summary() if src == "cloudflare_domain"
                else db.outlook_pool_summary()
            )
            for k in pool:
                pool[k] += int(one.get(k, 0) or 0)
        domain_pool = db.domain_email_pool_summary()
        return jsonify({
            "accounts": db.count_accounts(),
            "outlook_total": pool.get("total", 0),
            "outlook_available": pool.get("available", 0),
            "outlook_used": pool.get("used", 0),
            "outlook_failed": pool.get("failed", 0),
            "domain_total": domain_pool.get("total", 0),
            "domain_available": domain_pool.get("available", 0),
            "domain_used": domain_pool.get("used", 0),
            "domain_failed": domain_pool.get("failed", 0),
        })

    # ----------------------------------------------------------
    # 已注册账号
    # ----------------------------------------------------------
    @app.get("/api/accounts")
    def api_accounts():
        limit = request.args.get("limit", default=500, type=int)
        archived = str(request.args.get("archived", default="0") or "0").lower()
        plan_filter = str(request.args.get("plan", default="") or "").lower()
        codex_filter = str(request.args.get("codex_status", default="") or "").strip().lower()
        totp_filter = str(
            request.args.get("totp_status")
            or request.args.get("totp_filter")
            or request.args.get("twofa_status")
            or ""
        ).strip().lower()
        status_filter = str(request.args.get("status", default="") or "").strip().lower()
        allowed_status_filters = {
            "", "all", "plus", "live", "alive", "failed", "live_failed",
            "liveness_failed", "trial", "free_trial", "plus_trial",
            "deactivated", "dead", "invalid",
        }
        if status_filter not in allowed_status_filters:
            return jsonify({"ok": False, "error": "status 仅支持 all/plus/live/failed/trial/deactivated"}), 400
        q = str(request.args.get("q", default="") or "").strip()
        date_from = str(request.args.get("date_from", default="") or "").strip() or None
        date_to = str(request.args.get("date_to", default="") or "").strip() or None
        # 新分页接口：传 page/page_size 或 paged=1 时返回 {items,total,page,page_size,...}
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        if paged or page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            offset = (page - 1) * page_size
            result = db.list_accounts_page(limit=page_size, offset=offset, archived=archived, plan_filter=plan_filter, codex_filter=codex_filter, q=q, date_from=date_from, date_to=date_to, totp_filter=totp_filter, status_filter=status_filter)
            result["items"] = [_compact_account_for_list(r) for r in (result.get("items") or [])]
            result.update({"ok": True, "page": page, "page_size": page_size, "compact": True})
            return jsonify(result)
        rows = db.list_accounts(limit=limit, archived=archived, plan_filter=plan_filter, codex_filter=codex_filter, q=q, date_from=date_from, date_to=date_to, totp_filter=totp_filter, status_filter=status_filter)
        return jsonify([_compact_account_for_list(row) for row in rows])

    @app.post("/api/accounts/import")
    def api_accounts_import():
        """导入已有账号；支持 JSON/TXT、multipart 文件及扩展整行格式。"""
        duplicate_mode = "skip"
        selected_format = "auto"
        filename = ""
        content: str | bytes = b""

        if request.files:
            upload = request.files.get("file") or next(iter(request.files.values()), None)
            if upload is None:
                return jsonify({"ok": False, "error": "请选择要导入的文件"}), 400
            filename = upload.filename or ""
            content = upload.read(account_import.MAX_ACCOUNT_IMPORT_BYTES + 1)
            duplicate_mode = request.form.get("duplicate_mode", "skip")
            selected_format = request.form.get("format", "auto")
        else:
            data = request.get_json(silent=True)
            if not isinstance(data, dict):
                return jsonify({"ok": False, "error": "请求体必须是 JSON 对象或 multipart 文件"}), 400
            duplicate_mode = data.get("duplicate_mode", "skip")
            selected_format = data.get("format", "auto")
            if "records" in data:
                records, errors = account_import.parse_account_records(data.get("records"))
                actual_format = "json"
            else:
                content = data.get("text", "")
                if not isinstance(content, (str, bytes)):
                    return jsonify({"ok": False, "error": "text 必须是字符串"}), 400
                if isinstance(content, str) and len(content.encode("utf-8")) > account_import.MAX_ACCOUNT_IMPORT_BYTES:
                    return jsonify({"ok": False, "error": "导入内容不能超过 5 MB"}), 413
                records, errors, actual_format = account_import.parse_account_content(
                    content, format=selected_format, filename=str(data.get("filename") or ""),
                )
        if isinstance(content, bytes) and len(content) > account_import.MAX_ACCOUNT_IMPORT_BYTES:
            return jsonify({"ok": False, "error": "导入文件不能超过 5 MB"}), 413
        if request.files:
            records, errors, actual_format = account_import.parse_account_content(
                content, format=selected_format, filename=filename,
            )
        mode = str(duplicate_mode or "skip").strip().lower()
        if mode not in ("skip", "update"):
            return jsonify({"ok": False, "error": "duplicate_mode 仅支持 skip/update"}), 400
        if not records and not errors:
            return jsonify({"ok": False, "error": "没有可导入的账号记录"}), 400
        result = db.import_registered_accounts(records, duplicate_mode=mode)
        result.update({
            "ok": True,
            "format": actual_format,
            "parsed": len(records) + len(errors),
            "errors": errors,
            "error_count": len(errors),
        })
        return jsonify(result)

    @app.get("/api/accounts/plan-check-status")
    def api_account_plan_check_status():
        """套餐查询轻量状态，不返回 Token、邮箱密码等敏感字段。"""
        limit = request.args.get("limit", default=5000, type=int)
        archived = str(request.args.get("archived", default="0") or "0").lower()
        plan_filter = str(request.args.get("plan", default="") or "").lower()
        codex_filter = str(request.args.get("codex_status", default="") or "").strip().lower()
        totp_filter = str(
            request.args.get("totp_status")
            or request.args.get("totp_filter")
            or request.args.get("twofa_status")
            or ""
        ).strip().lower()
        status_filter = str(request.args.get("status", default="") or "").strip().lower()
        allowed_status_filters = {
            "", "all", "plus", "live", "alive", "failed", "live_failed",
            "liveness_failed", "trial", "free_trial", "plus_trial",
            "deactivated", "dead", "invalid",
        }
        if status_filter not in allowed_status_filters:
            return jsonify({"ok": False, "error": "status 仅支持 all/plus/live/failed/trial/deactivated"}), 400
        q = str(request.args.get("q", default="") or "").strip()
        date_from = str(request.args.get("date_from", default="") or "").strip() or None
        date_to = str(request.args.get("date_to", default="") or "").strip() or None
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        if page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            offset = (page - 1) * page_size
            snapshot = db.list_account_plan_check_statuses(limit=page_size, offset=offset, archived=archived, plan_filter=plan_filter, codex_filter=codex_filter, q=q, date_from=date_from, date_to=date_to, totp_filter=totp_filter, status_filter=status_filter)
            snapshot.update({"page": page, "page_size": page_size})
        else:
            snapshot = db.list_account_plan_check_statuses(limit=max(1, min(5000, limit)), archived=archived, plan_filter=plan_filter, codex_filter=codex_filter, q=q, date_from=date_from, date_to=date_to, totp_filter=totp_filter, status_filter=status_filter)
        snapshot["queue"] = plan_check_service.queue_settings()
        return jsonify(snapshot)


    @app.get("/api/accounts/<int:acc_id>/secret")
    def api_account_secret(acc_id: int):
        """按需读取单账号敏感值，避免账号列表一次性下发完整 Token/整行。"""
        field = str(request.args.get("field") or "").strip()
        acc = db.get_account(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        try:
            value = _account_secret_value(acc, field)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "id": acc_id, "field": field, "value": value})

    @app.post("/api/accounts/secret-bulk")
    def api_accounts_secret_bulk():
        """按需批量读取账号敏感值。Body {account_ids:[...], field}."""
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400
        ids = data.get("account_ids") if "account_ids" in data else data.get("ids")
        field = str(data.get("field") or "").strip()
        allowed_fields = {"email", "access_token", "copy_line", "codex_agent_token", "totp_secret", "totp_code", "password", "email_password"}
        if field not in allowed_fields:
            return jsonify({"ok": False, "error": "field 仅支持 email/access_token/copy_line/codex_agent_token/totp_secret/totp_code/password/email_password"}), 400
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多读取 5000 个账号"}), 400
        values = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = _coerce_export_account_id(raw)
            except ValueError as exc:
                skipped.append({"id": raw, "reason": str(exc)})
                continue
            if acc_id in seen:
                skipped.append({"id": acc_id, "reason": "ID 重复"})
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            value = _account_secret_value(acc, field)
            if value:
                values.append({"id": acc_id, "email": acc.get("email"), "value": value})
            else:
                skipped.append({"id": acc_id, "email": acc.get("email"), "reason": "值为空"})
        return jsonify({"ok": True, "field": field, "values": values, "count": len(values), "skipped": skipped})

    @app.get("/api/accounts/export-fields")
    def api_accounts_export_fields():
        """返回账号批量导出的字段清单，供前端展示自定义导出选项。"""
        return jsonify({
            "ok": True,
            "fields": [
                {"key": field, "label": _ACCOUNT_EXPORT_FIELD_LABELS[field], "sensitive": field in _ACCOUNT_EXPORT_SENSITIVE_FIELDS}
                for field in ACCOUNT_EXPORT_FIELDS
            ],
        })

    @app.post("/api/accounts/export")
    def api_accounts_export():
        """批量导出账号；支持选中账号/当前页/当前筛选结果与自定义字段。"""
        import csv
        import io
        from datetime import datetime as _dt

        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400

        # fields 缺省时保持便捷的邮箱导出默认值；显式空数组/空字符串必须拒绝，
        # 不能静默扩大为 email 导出。
        raw_fields = data["fields"] if "fields" in data else ["email"]
        if isinstance(raw_fields, str):
            raw_fields = [part.strip() for part in raw_fields.split(",") if part.strip()]
        if not isinstance(raw_fields, list) or not raw_fields:
            return jsonify({"ok": False, "error": "fields 必须是非空数组"}), 400
        aliases = {
            "mail": "email", "mail_password": "email_password", "2fa": "totp_secret",
            "totp": "totp_secret", "at": "access_token", "token": "access_token",
            "line": "copy_line",
        }
        fields = []
        for raw in raw_fields:
            field = aliases.get(str(raw or "").strip().lower(), str(raw or "").strip().lower())
            if field not in ACCOUNT_EXPORT_FIELDS:
                return jsonify({"ok": False, "error": f"不支持的导出字段：{raw}"}), 400
            if field not in fields:
                fields.append(field)
        if not fields:
            return jsonify({"ok": False, "error": "fields 必须包含至少一个有效字段"}), 400

        output_format = str(data.get("format") or "txt").strip().lower()
        if output_format == "text":
            output_format = "txt"
        if output_format not in {"txt", "csv", "json"}:
            return jsonify({"ok": False, "error": "format 仅支持 txt/csv/json"}), 400
        sensitive = [field for field in fields if field in _ACCOUNT_EXPORT_SENSITIVE_FIELDS]
        if sensitive and data.get("confirm_sensitive") is not True:
            labels = "、".join(_ACCOUNT_EXPORT_FIELD_LABELS[field] for field in sensitive)
            return jsonify({"ok": False, "error": f"导出包含敏感字段（{labels}），请确认后重试", "sensitive_fields": sensitive}), 400

        scope = str(data.get("scope") or "selected").strip().lower()
        if scope in {"filter", "all_filtered", "all"}:
            scope = "filtered"
        if scope not in {"selected", "current_page", "filtered"}:
            return jsonify({"ok": False, "error": "scope 仅支持 selected/current_page/filtered"}), 400

        raw_ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(raw_ids, list):
            return jsonify({"ok": False, "error": "account_ids 必须是数组"}), 400
        if len(raw_ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多导出 5000 个账号"}), 400

        skipped = []
        ids = []
        seen = set()
        if scope == "selected":
            if not raw_ids:
                return jsonify({"ok": False, "error": "selected 导出需要提供 account_ids"}), 400
            for raw in raw_ids:
                try:
                    acc_id = _coerce_export_account_id(raw)
                except ValueError as exc:
                    skipped.append({"id": raw, "reason": str(exc)})
                    continue
                if acc_id in seen:
                    skipped.append({"id": acc_id, "reason": "ID 重复"})
                    continue
                seen.add(acc_id)
                ids.append(acc_id)
            rows = db.get_accounts_by_ids(ids)
            found = {int(row.get("id") or 0) for row in rows}
            skipped.extend({"id": acc_id, "reason": "账号不存在"} for acc_id in ids if acc_id not in found)
        else:
            filters = data.get("filters") if isinstance(data.get("filters"), dict) else data
            try:
                parsed_filters = _normalise_account_export_filters(filters)
            except ValueError as exc:
                return jsonify({"ok": False, "error": str(exc)}), 400
            page = data.get("page", 1)
            page_size = data.get("page_size", 50)
            try:
                page = int(page)
                page_size = int(page_size)
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "page/page_size 必须是整数"}), 400
            if page < 1 or page_size < 1 or page_size > 500:
                return jsonify({"ok": False, "error": "page 必须 >=1，page_size 必须为 1-500"}), 400
            result = db.list_accounts_page(
                limit=5001 if scope == "filtered" else page_size,
                offset=0 if scope == "filtered" else (page - 1) * page_size,
                archived=parsed_filters["archived"], plan_filter=parsed_filters["plan_filter"],
                codex_filter=parsed_filters["codex_filter"], q=parsed_filters["q"],
                date_from=parsed_filters["date_from"], date_to=parsed_filters["date_to"],
                totp_filter=parsed_filters["totp_filter"], status_filter=parsed_filters["status_filter"],
            )
            if scope == "filtered" and int(result.get("total") or 0) > 5000:
                return jsonify({"ok": False, "error": "当前筛选结果超过 5000 个账号，请缩小范围后导出"}), 400
            rows = result.get("items") or []
            if scope == "current_page":
                raw_page_ids = data.get("account_ids") if "account_ids" in data else data.get("ids")
                if raw_page_ids is not None:
                    requested = []
                    for raw in raw_page_ids if isinstance(raw_page_ids, list) else []:
                        try:
                            requested.append(_coerce_export_account_id(raw))
                        except ValueError:
                            skipped.append({"id": raw, "reason": "ID 非法"})
                    actual = {int(row.get("id") or 0) for row in rows}
                    if requested and set(requested) != actual:
                        return jsonify({"ok": False, "error": "current_page 不接受与服务端页码不一致的 account_ids"}), 400

        if not rows:
            return jsonify({"ok": False, "error": "没有可导出的账号", "skipped": skipped}), 404

        include_header = data.get("include_header") if "include_header" in data else output_format == "csv"
        include_header = bool(include_header)
        delimiter = data.get("delimiter", "----")
        if delimiter == "\\t":
            delimiter = "\t"
        if not isinstance(delimiter, str) or not delimiter or len(delimiter) > 8:
            return jsonify({"ok": False, "error": "delimiter 必须是 1-8 个字符"}), 400
        if any(ord(char) < 32 and char != "\t" for char in delimiter) or "\n" in delimiter or "\r" in delimiter:
            return jsonify({"ok": False, "error": "delimiter 不能包含控制字符或换行"}), 400
        if output_format == "csv" and (len(delimiter) != 1 or delimiter in {'"', "\x00"}):
            return jsonify({"ok": False, "error": "CSV 分隔符必须是单个字符且不能是引号/NUL"}), 400

        matrix = [[_account_export_field_value(row, field) for field in fields] for row in rows]
        if output_format == "json":
            payload = [dict(zip(fields, values)) for values in matrix]
            text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            mimetype = "application/json"
            extension = "json"
            encoded = text.encode("utf-8")
        elif output_format == "csv":
            out = io.StringIO(newline="")
            writer = csv.writer(out, delimiter=delimiter, lineterminator="\n")
            if include_header:
                writer.writerow([_csv_safe_cell(_ACCOUNT_EXPORT_FIELD_LABELS[field]) for field in fields])
            writer.writerows([[_csv_safe_cell(value) for value in values] for values in matrix])
            encoded = out.getvalue().encode("utf-8-sig")
            mimetype = "text/csv"
            extension = "csv"
        else:
            lines = []
            if include_header:
                lines.append(delimiter.join(_txt_safe_cell(_ACCOUNT_EXPORT_FIELD_LABELS[field], delimiter) for field in fields))
            lines.extend(delimiter.join(_txt_safe_cell(value, delimiter) for value in values) for values in matrix)
            encoded = ("\ufeff" + "\n".join(lines) + "\n").encode("utf-8")
            mimetype = "text/plain"
            extension = "txt"

        filename = f"accounts-export-{_dt.now().strftime('%Y%m%d-%H%M%S')}.{extension}"
        response_meta = {
            "ok": True, "scope": scope, "fields": fields, "count": len(rows),
            "skipped": skipped, "skipped_count": len(skipped), "filename": filename,
        }
        if data.get("prepare"):
            download_id = _put_prepared_download(encoded, filename, mimetype)
            response_meta.update({"prepared": True, "download_id": download_id, "download_url": f"/api/downloads/{download_id}"})
            return jsonify(response_meta)
        response = Response(
            encoded,
            mimetype=mimetype,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(encoded)),
                "Cache-Control": "no-store, max-age=0",
                "X-Content-Type-Options": "nosniff",
                "X-Export-Count": str(len(rows)),
                "X-Export-Skipped": str(len(skipped)),
            },
        )
        return response

    @app.post("/api/accounts/<int:acc_id>/archive")
    def api_account_archive(acc_id: int):
        """归档/取消归档一个账号。Body {archived: true|false}。"""
        data = request.get_json(silent=True) or {}
        archived = bool(data.get("archived", True))
        updated = db.archive_account(acc_id=acc_id, archived=archived)
        if not updated:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        return jsonify({"ok": True, "updated": True, "id": acc_id, "archived": archived})

    @app.post("/api/accounts/archive-bulk")
    def api_accounts_archive_bulk():
        """批量归档/取消归档账号。Body {account_ids:[...], archived:true|false}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        archived = bool(data.get("archived", True))
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多归档 5000 个账号"}), 400
        account_ids = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            account_ids.append(acc_id)
        updated, db_skipped = db.archive_accounts(account_ids=account_ids, archived=archived)
        skipped.extend(db_skipped)
        return jsonify({"ok": True, "updated": updated, "updated_count": len(updated), "archived": archived, "skipped": skipped})

    @app.post("/api/accounts/<int:acc_id>/delete")
    def api_account_delete(acc_id: int):
        """删除一个已注册账号记录。只删除本地保存的账号/token记录，不改邮箱池状态。"""
        deleted = db.delete_account(acc_id=acc_id)
        if not deleted:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        return jsonify({"ok": True, "deleted": True})

    @app.post("/api/accounts/delete-bulk")
    def api_accounts_delete_bulk():
        """批量删除已注册账号记录。Body {account_ids: [...]} 或 {ids: [...]}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多删除 5000 个账号"}), 400
        account_ids = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            account_ids.append(acc_id)
        deleted, db_skipped = db.delete_accounts(account_ids=account_ids)
        skipped.extend(db_skipped)
        return jsonify({
            "ok": True,
            "deleted": deleted,
            "deleted_count": len(deleted),
            "skipped": skipped,
        })

    @app.post("/api/accounts/<int:acc_id>/note")
    def api_account_note(acc_id: int):
        """更新单个已注册账号备注。Body {note: "..."}，空字符串表示清空。"""
        data = request.get_json(silent=True) or {}
        note = str(data.get("note") or "")
        if len(note) > 2000:
            return jsonify({"ok": False, "error": "备注最多 2000 个字符"}), 400
        updated = db.update_account_note(acc_id=acc_id, note=note)
        if not updated:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        return jsonify({"ok": True, "updated": True, "id": acc_id, "note": note})

    @app.post("/api/accounts/<int:acc_id>/totp-setup")
    def api_account_totp_setup(acc_id: int):
        """为单个账号开启 2FA/TOTP，成功后自动把 secret 写回账号记录。"""
        acc = db.get_account(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        token = str(acc.get("access_token") or "").strip()
        if not token:
            return jsonify({"ok": False, "error": "该账号没有 access_token"}), 400
        if bool(acc.get("totp_secret")):
            return jsonify({"ok": False, "error": "该账号已经开启 2FA"}), 400

        try:
            from core import twofa_service
        except Exception as exc:
            return jsonify({"ok": False, "error": f"2FA 服务加载失败：{type(exc).__name__}: {exc}"}), 503

        queued = twofa_service.enqueue_account_totp_setup(
            account_id=acc_id,
            email=str(acc.get("email") or ""),
            access_token=token,
            trigger="manual",
            proxy=str(acc.get("proxy_used") or "") or None,
        )
        queued_payload = {k: v for k, v in queued.items() if k != "future"}
        if queued.get("busy"):
            return jsonify({"ok": False, **queued_payload}), 409
        if not queued.get("accepted"):
            return jsonify({"ok": False, **queued_payload}), 503
        return jsonify({
            "ok": True,
            "started": True,
            "queue": twofa_service.queue_settings(),
            **queued_payload,
        }), 202

    @app.post("/api/accounts/<int:acc_id>/change-email")
    def api_account_change_email(acc_id: int):
        """给单个账号排队换绑邮箱。Body {source}."""
        data = request.get_json(silent=True) or {}
        source = str(data.get("source") or "").strip().lower()
        allowed = {"outlook", "generic_api", "imap", "cloudflare_domain", "cloudflare", "gptmail", "mailnest", "cloudmail", "remail"}
        if source not in allowed:
            return jsonify({"ok": False, "error": "请选择有效的邮箱来源"}), 400
        acc = db.get_account(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        if not str(acc.get("access_token") or "").strip():
            return jsonify({"ok": False, "error": "账号缺少 access_token，请先查活刷新 AT"}), 400
        from core import email_change_service
        result = email_change_service.enqueue(acc_id, source, trigger="manual")
        public = {k: v for k, v in result.items() if k != "future"}
        return jsonify({"ok": bool(result.get("accepted")), **public}), (202 if result.get("accepted") else 409)

    @app.post("/api/accounts/change-email-bulk")
    def api_accounts_change_email_bulk():
        """批量换绑邮箱。Body {account_ids:[...], source}."""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        source = str(data.get("source") or "").strip().lower()
        allowed = {"outlook", "generic_api", "imap", "cloudflare_domain", "cloudflare", "gptmail", "mailnest", "cloudmail", "remail"}
        if source not in allowed:
            return jsonify({"ok": False, "error": "请选择有效的邮箱来源"}), 400
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多提交 500 个账号"}), 400
        from core import email_change_service
        started, skipped = [], []
        seen_ids: set[int] = set()
        for raw_id in ids:
            try:
                acc_id = int(raw_id)
            except (TypeError, ValueError):
                skipped.append({"id": raw_id, "reason": "ID 非法"})
                continue
            if acc_id in seen_ids:
                continue
            seen_ids.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            if not str(acc.get("access_token") or "").strip():
                skipped.append({"id": acc_id, "email": acc.get("email"), "reason": "缺少 access_token"})
                continue
            result = email_change_service.enqueue(acc_id, source, trigger="manual_bulk")
            if result.get("accepted"):
                started.append({"id": acc_id, "email": acc.get("email"), "status": "queued"})
            else:
                skipped.append({"id": acc_id, "email": acc.get("email"), "reason": result.get("error")})
        return jsonify({"ok": True, "started": started, "started_count": len(started), "skipped": skipped}), 202

    @app.post("/api/accounts/totp-setup-bulk")
    def api_accounts_totp_setup_bulk():
        """批量把账号 2FA/TOTP 设置任务加入后台队列。Body {account_ids:[...]}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多提交 500 个账号"}), 400

        account_ids = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            account_ids.append(acc_id)

        accounts = []
        for acc_id in account_ids:
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = str(acc.get("email") or "").strip()
            token = str(acc.get("access_token") or "").strip()
            if not token:
                skipped.append({"id": acc_id, "email": email, "reason": "缺少 access_token"})
                continue
            if str(acc.get("totp_secret") or "").strip():
                skipped.append({"id": acc_id, "email": email, "reason": "该账号已经开启 2FA"})
                continue
            if not email:
                skipped.append({"id": acc_id, "reason": "邮箱为空"})
                continue
            accounts.append(acc)

        try:
            from core import twofa_service
        except Exception as exc:
            return jsonify({"ok": False, "error": f"2FA 服务加载失败：{type(exc).__name__}: {exc}"}), 503

        started = []
        busy = []
        failed = []
        for acc in accounts:
            acc_id = int(acc.get("id") or 0)
            email = str(acc.get("email") or "").strip()
            try:
                queued = twofa_service.enqueue_account_totp_setup(
                    account_id=acc_id,
                    email=email,
                    access_token=str(acc.get("access_token") or "").strip(),
                    trigger="manual_bulk",
                    proxy=str(acc.get("proxy_used") or "") or None,
                )
            except Exception as exc:
                failed.append({
                    "id": acc_id,
                    "email": email,
                    "error": f"{type(exc).__name__}: {exc}",
                })
                continue

            # Future 对象不可 JSON 序列化；批量接口只返回队列结果摘要。
            public_result = {k: v for k, v in queued.items() if k != "future"}
            item = {"id": acc_id, "email": email, **public_result}
            if queued.get("accepted"):
                item["status"] = "queued"
                started.append(item)
            elif queued.get("busy"):
                busy.append(item)
            else:
                failed.append(item)

        return jsonify({
            "ok": True,
            "message": f"已入队 {len(started)} 个 2FA 设置任务",
            "started": started,
            "started_count": len(started),
            "busy": busy,
            "busy_count": len(busy),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
            "queue": twofa_service.queue_settings(),
        }), 202

    @app.post("/api/accounts/note-bulk")
    def api_accounts_note_bulk():
        """批量更新已注册账号备注。Body {account_ids: [...], note: "..."}，空字符串表示清空。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        note = str(data.get("note") or "")
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 5000:
            return jsonify({"ok": False, "error": "单次最多备注 5000 个账号"}), 400
        if len(note) > 2000:
            return jsonify({"ok": False, "error": "备注最多 2000 个字符"}), 400

        account_ids = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            account_ids.append(acc_id)
        updated, db_skipped = db.update_accounts_note(account_ids=account_ids, note=note)
        skipped.extend(db_skipped)
        return jsonify({
            "ok": True,
            "updated": updated,
            "updated_count": len(updated),
            "skipped": skipped,
            "skipped_count": len(skipped),
        })

    @app.post("/api/accounts/check-live-bulk")
    def api_accounts_check_live_bulk():
        """批量查活：加入后台队列；协议 BrowserSession 指纹环境重新登录并刷新最新 AT。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多查活 500 个账号"}), 400

        account_ids: list[int] = []
        skipped: list[dict] = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            account_ids.append(acc_id)

        accounts = []
        for acc_id in account_ids:
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = str(acc.get("email") or "").strip()
            if not email:
                skipped.append({"id": acc_id, "reason": "邮箱为空"})
                continue
            accounts.append(acc)

        started = []
        busy_count = 0
        failed = []
        for acc in accounts:
            acc_id = int(acc.get("id") or 0)
            email = str(acc.get("email") or "")
            queued = live_check_service.enqueue_account_live_check(
                account_id=acc_id,
                email=email,
                trigger="manual",
                # 查活按“查套餐”同一套网络选路：
                # PLAN_CHECK_PROXY_MODE / PLAN_CHECK_PROXY / PROXY_POOL。
                # 不复用账号注册时的 proxy_used，避免旧注册出口被 CF 403 后一直失败。
                proxy=None,
            )
            if queued.get("accepted"):
                started.append({"id": acc_id, "email": email, "status": "queued"})
            elif queued.get("busy"):
                busy_count += 1
                skipped.append({"id": acc_id, "email": email, "reason": queued.get("error") or "正在查活"})
            else:
                failed.append({"id": acc_id, "email": email, "error": queued.get("error") or "入队失败"})

        return jsonify({
            "ok": True,
            "message": f"已入队 {len(started)} 个查活任务",
            "started": started,
            "started_count": len(started),
            "busy_count": busy_count,
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "queue": live_check_service.queue_settings(),
        }), 202


    @app.post("/api/accounts/check-plan")
    def api_account_check_plan():
        """把单账号套餐查询加入后台队列。Body {account_id|email, proxy?, timezone_offset_min?}"""
        data = request.get_json(silent=True) or {}
        acc_id = data.get("account_id") or data.get("id")
        email = (data.get("email") or "").strip()
        acc = None
        if acc_id is not None:
            try:
                acc = db.get_account(int(acc_id))
            except Exception:
                acc = None
        if acc is None and email:
            acc = db.get_account_by_email(email)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        token = (acc.get("access_token") or "").strip()
        if not token:
            return jsonify({"ok": False, "error": "该账号没有 access_token"}), 400
        account_id = int(acc.get("id"))
        queued = plan_check_service.enqueue_account_plan_check(
            account_id=account_id,
            email=acc.get("email") or "",
            access_token=token,
            trigger="manual",
            proxy=data.get("proxy") if "proxy" in data else None,
            timezone_offset_min=str(data.get("timezone_offset_min") or "-"),
        )
        if queued.get("busy"):
            return jsonify({"ok": False, **queued}), 409
        if not queued.get("accepted"):
            return jsonify({"ok": False, **queued}), 503
        return jsonify({"ok": True, "started": True, **queued}), 202

    @app.post("/api/accounts/check-plan-bulk")
    def api_accounts_check_plan_bulk():
        """批量把套餐查询加入统一后台队列。Body {account_ids:[...], proxy?, timezone_offset_min?}"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多查询 500 个账号"}), 400
        # 与单账号查询保持一致：未传时使用独立网络策略。
        proxy = data.get("proxy") if "proxy" in data else None
        timezone_offset_min = str(data.get("timezone_offset_min") or "-")

        items = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except Exception:
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            if not (acc.get("access_token") or "").strip():
                skipped.append({"id": acc_id, "email": acc.get("email"), "reason": "缺少 access_token"})
                continue
            items.append(acc)

        started = []
        busy = []
        failed = []
        for acc in items:
            queued = plan_check_service.enqueue_account_plan_check(
                account_id=int(acc.get("id")),
                email=acc.get("email") or "",
                access_token=acc.get("access_token") or "",
                trigger="manual_bulk",
                proxy=proxy,
                timezone_offset_min=timezone_offset_min,
            )
            item = {"id": acc.get("id"), "email": acc.get("email"), **queued}
            if queued.get("accepted"):
                started.append(item)
            elif queued.get("busy"):
                busy.append(item)
            else:
                failed.append(item)
        return jsonify({
            "ok": True,
            "started": started,
            "started_count": len(started),
            "busy": busy,
            "busy_count": len(busy),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
        }), 202

    def _payment_account_from_payload(data: dict):
        acc_id = data.get("account_id") or data.get("id")
        if acc_id is None:
            return None
        # Account IDs are the only browser-supplied selector; never accept an
        # email, token, password, or arbitrary checker input as an account key.
        try:
            if isinstance(acc_id, bool) or not isinstance(acc_id, (str, int)) or (isinstance(acc_id, str) and not acc_id.strip().isdigit()):
                return None
            parsed_id = int(acc_id)
            if parsed_id <= 0:
                return None
            return db.get_account(parsed_id)
        except (TypeError, ValueError, OverflowError):
            return None

    def _payment_regions_from_payload(data: dict):
        value = data.get("regions")
        if value is None:
            return None, None
        if not isinstance(value, list) or not value or len(value) > 20 or any(not isinstance(item, str) for item in value):
            return None, "regions 必须是 1-20 个地区预设名称的数组"
        regions = [item.strip().lower() for item in value if item.strip()]
        invalid = [item for item in regions if item not in payment_method_service.available_region_names()]
        if not regions or invalid:
            return None, f"不支持的支付方式地区预设: {', '.join(invalid or regions)}"
        return regions, None

    @app.post("/api/accounts/check-payment")
    def api_account_check_payment():
        """把单账号支付方式资格查询加入后台队列。仅使用服务端保存的 Token。"""
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400
        regions, region_error = _payment_regions_from_payload(data)
        if region_error:
            return jsonify({"ok": False, "error": region_error, "supported_regions": payment_method_service.available_region_names()}), 400
        acc = _payment_account_from_payload(data)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        token = str(acc.get("access_token") or "").strip()
        if not token:
            return jsonify({"ok": False, "error": "该账号没有 access_token"}), 400
        queued = payment_method_service.enqueue_account_payment_method_check(
            account_id=int(acc.get("id")),
            email=acc.get("email") or "",
            access_token=token,
            trigger="manual",
            regions=regions,
        )
        if queued.get("busy"):
            return jsonify({"ok": False, **queued}), 409
        if not queued.get("accepted"):
            return jsonify({"ok": False, **queued}), 503
        return jsonify({"ok": True, "started": True, **queued}), 202

    @app.post("/api/accounts/check-payment-bulk")
    def api_accounts_check_payment_bulk():
        """批量查询账号在配置地区发布的支付方式。"""
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400
        regions, region_error = _payment_regions_from_payload(data)
        if region_error:
            return jsonify({"ok": False, "error": region_error, "supported_regions": payment_method_service.available_region_names()}), 400
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多查询 500 个账号"}), 400
        started, busy, failed, skipped = [], [], [], []
        seen = set()
        for raw in ids:
            try:
                if isinstance(raw, bool) or not isinstance(raw, (str, int)) or (isinstance(raw, str) and not raw.strip().isdigit()):
                    raise ValueError("invalid id")
                acc_id = int(raw)
                if acc_id <= 0:
                    raise ValueError("invalid id")
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            token = str(acc.get("access_token") or "").strip()
            if not token:
                skipped.append({"id": acc_id, "email": acc.get("email"), "reason": "缺少 access_token"})
                continue
            queued = payment_method_service.enqueue_account_payment_method_check(
                account_id=acc_id,
                email=acc.get("email") or "",
                access_token=token,
                trigger="manual_bulk",
                regions=regions,
            )
            item = {"id": acc_id, "email": acc.get("email"), **queued}
            if queued.get("accepted"):
                started.append(item)
            elif queued.get("busy"):
                busy.append(item)
            else:
                failed.append(item)
        return jsonify({
            "ok": True,
            "started": started, "started_count": len(started),
            "busy": busy, "busy_count": len(busy),
            "failed": failed, "failed_count": len(failed),
            "skipped": skipped, "skipped_count": len(skipped),
            "queue": payment_method_service.queue_settings(),
        }), 202

    @app.get("/api/extract-link/cdk")
    def api_extract_link_cdk():
        """查询当前配置或传入 CDK 的剩余次数/服务状态。"""
        code = (request.args.get("code") or "").strip() or None
        try:
            result = extract_link_service.query_cdk(cdk=code)
            if isinstance(result, dict) and str(result.get("backend") or "") == "djbnb":
                result = {k: v for k, v in result.items() if k not in {"code", "cardCode"}}
            return jsonify({"ok": True, **result})
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    @app.get("/api/extract-link/djbnb/meta")
    def api_extract_link_djbnb_meta():
        """获取 DJB 可用通道、国家目录和引擎限制。"""
        try:
            from core import djb_client
            return jsonify({"ok": True, **djb_client.get_meta()})
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    @app.post("/api/extract-link/djbnb/card-check")
    def api_extract_link_djbnb_card_check():
        """校验 DJB 卡密，不消耗次数。"""
        data = request.get_json(silent=True) or {}
        code = str(data.get("code") or "").strip() or None
        try:
            from core import djb_client
            result = djb_client.check_card(code)
            safe = {k: v for k, v in result.items() if k not in {"code", "cardCode"}}
            return jsonify({"ok": True, **safe})
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    def _is_extract_eligible(acc: dict) -> bool:
        plan = str(acc.get("current_plan_type") or acc.get("plan_type") or "").lower()
        return plan == "free" and bool(acc.get("plus_trial_eligible"))

    def _is_activation_eligible(acc: dict) -> bool:
        """Require a known free/trial-eligible account before auto-pay."""
        if not isinstance(acc, dict):
            return False
        plan = str(acc.get("current_plan_type") or acc.get("plan_type") or "").strip().lower()
        if plan != "free" or not bool(acc.get("plus_trial_eligible")):
            return False
        if str(acc.get("live_check_status") or "").strip().lower() == "deactivated":
            return False
        return bool(str(acc.get("access_token") or "").strip())

    def _activation_account_id(raw: object) -> int:
        if isinstance(raw, bool) or not isinstance(raw, (str, int)):
            raise ValueError("ID 非法")
        text = str(raw).strip()
        if not text or not text.isdigit():
            raise ValueError("ID 非法")
        value = int(text, 10)
        if value <= 0:
            raise ValueError("ID 必须是正整数")
        return value

    @app.post("/api/accounts/extract-link")
    def api_account_extract_link():
        """单账号提链。Body {account_id|id, link_type?, cdk?}。"""
        data = request.get_json(silent=True) or {}
        acc_id = data.get("account_id") or data.get("id")
        try:
            acc = db.get_account(int(acc_id))
        except Exception:
            acc = None
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        if not _is_extract_eligible(acc):
            return jsonify({"ok": False, "error": "仅支持 free(可Plus试用) 账号提链；请先查询套餐确认资格"}), 400
        token = (acc.get("access_token") or "").strip()
        if not token:
            return jsonify({"ok": False, "error": "该账号没有 access_token"}), 400
        try:
            queued = extract_link_service.enqueue_account_extract(
                account_id=int(acc.get("id")),
                email=acc.get("email") or "",
                access_token=token,
                trigger="manual",
                link_type=data.get("link_type"),
                cdk=data.get("cdk"),
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400
        if queued.get("busy"):
            return jsonify({"ok": False, **queued}), 409
        if not queued.get("accepted"):
            return jsonify({"ok": False, **queued}), 503
        return jsonify({"ok": True, "started": True, **{k: v for k, v in queued.items() if k != "future"}}), 202

    @app.post("/api/accounts/extract-link-bulk")
    def api_accounts_extract_link_bulk():
        """批量提链。Body {account_ids:[...], link_type?, cdk?}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多提链 500 个账号"}), 400

        started = []
        busy = []
        failed = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except Exception:
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = acc.get("email")
            if not _is_extract_eligible(acc):
                skipped.append({"id": acc_id, "email": email, "reason": "不是 free(可Plus试用)"})
                continue
            token = (acc.get("access_token") or "").strip()
            if not token:
                skipped.append({"id": acc_id, "email": email, "reason": "缺少 access_token"})
                continue
            try:
                queued = extract_link_service.enqueue_account_extract(
                    account_id=acc_id,
                    email=email or "",
                    access_token=token,
                    trigger="manual_bulk",
                    link_type=data.get("link_type"),
                    cdk=data.get("cdk"),
                )
            except Exception as exc:
                failed.append({"id": acc_id, "email": email, "error": f"{type(exc).__name__}: {exc}"})
                continue
            item = {"id": acc_id, "email": email, **{k: v for k, v in queued.items() if k != "future"}}
            if queued.get("accepted"):
                started.append(item)
            elif queued.get("busy"):
                busy.append(item)
            else:
                failed.append(item)
        return jsonify({
            "ok": True,
            "started": started,
            "started_count": len(started),
            "busy": busy,
            "busy_count": len(busy),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
        }), 202

    def _activation_response_item(queued: dict, acc: dict) -> dict:
        safe = {k: v for k, v in (queued or {}).items() if k not in {"future", "job_secret", "access_token", "session"}}
        safe.setdefault("id", int(acc.get("id") or 0))
        safe.setdefault("email", acc.get("email") or "")
        return safe

    @app.post("/api/accounts/activate")
    def api_account_activate():
        """通过公开 MoMo v1 API 自动提交并等待 Plus 开通任务。"""
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400
        try:
            acc_id = _activation_account_id(data.get("account_id") if "account_id" in data else data.get("id"))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "account_id 必须是正整数"}), 400
        acc = db.get_account(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        if not _is_activation_eligible(acc):
            return jsonify({"ok": False, "error": "仅支持 free 且可免费试用 Plus、未废号且有 access_token 的账号"}), 400
        try:
            queued = momo_activation_service.enqueue_account_activation(
                account_id=acc_id,
                email=acc.get("email") or "",
                access_token=str(acc.get("access_token") or "").strip(),
                trigger="manual",
                trial_days=data.get("trial_days"),
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:240]}"}), 400
        if queued.get("busy"):
            return jsonify({"ok": False, **_activation_response_item(queued, acc)}), 409
        if not queued.get("accepted"):
            return jsonify({"ok": False, **_activation_response_item(queued, acc)}), 503
        return jsonify({"ok": True, "started": True, **_activation_response_item(queued, acc)}), 202

    @app.post("/api/accounts/activate-bulk")
    def api_accounts_activate_bulk():
        """批量提交 MoMo 自动开通任务；仅接受账号 ID，Token 由服务端读取。"""
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400
        ids = data.get("account_ids") if "account_ids" in data else data.get("ids")
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 100:
            return jsonify({"ok": False, "error": "单次最多开通 100 个账号"}), 400
        started, busy, failed, skipped = [], [], [], []
        seen = set()
        for raw in ids:
            try:
                acc_id = _activation_account_id(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            if not _is_activation_eligible(acc):
                skipped.append({"id": acc_id, "email": acc.get("email") or "", "reason": "不是 free 可试用、已废号或缺少 access_token"})
                continue
            try:
                queued = momo_activation_service.enqueue_account_activation(
                    account_id=acc_id,
                    email=acc.get("email") or "",
                    access_token=str(acc.get("access_token") or "").strip(),
                    trigger="manual_bulk",
                    trial_days=data.get("trial_days"),
                )
            except Exception as exc:
                failed.append({"id": acc_id, "email": acc.get("email") or "", "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
                continue
            item = _activation_response_item(queued, acc)
            if queued.get("accepted"):
                started.append(item)
            elif queued.get("busy"):
                busy.append(item)
            else:
                failed.append(item)
        return jsonify({
            "ok": True,
            "started": started, "started_count": len(started),
            "busy": busy, "busy_count": len(busy),
            "failed": failed, "failed_count": len(failed),
            "skipped": skipped, "skipped_count": len(skipped),
            "queue": momo_activation_service.queue_settings(),
        }), 202

    @app.post("/api/accounts/codex-agent")
    def api_account_codex_agent():
        """单账号生成 Codex Agent Token。Body {account_id|id, verify_task?}。"""
        data = request.get_json(silent=True) or {}
        acc_id = data.get("account_id") or data.get("id")
        try:
            acc = db.get_account(int(acc_id))
        except Exception:
            acc = None
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        token = (acc.get("access_token") or "").strip()
        if not token:
            return jsonify({"ok": False, "error": "该账号没有 access_token"}), 400
        try:
            queued = codex_agent_service.enqueue_account_codex_agent(
                account_id=int(acc.get("id")),
                email=acc.get("email") or "",
                access_token=token,
                trigger="manual",
                verify_task=bool(data.get("verify_task", True)),
            )
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400
        if queued.get("busy"):
            return jsonify({"ok": False, **queued}), 409
        if not queued.get("accepted"):
            return jsonify({"ok": False, **queued}), 503
        return jsonify({"ok": True, "started": True, **{k: v for k, v in queued.items() if k != "future"}}), 202

    @app.post("/api/accounts/codex-agent-bulk")
    def api_accounts_codex_agent_bulk():
        """批量生成 Codex Agent Token。Body {account_ids:[...], verify_task?}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多提交 500 个账号"}), 400

        started = []
        busy = []
        failed = []
        skipped = []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except Exception:
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = acc.get("email")
            token = (acc.get("access_token") or "").strip()
            if not token:
                skipped.append({"id": acc_id, "email": email, "reason": "缺少 access_token"})
                continue
            try:
                queued = codex_agent_service.enqueue_account_codex_agent(
                    account_id=acc_id,
                    email=email or "",
                    access_token=token,
                    trigger="manual_bulk",
                    verify_task=bool(data.get("verify_task", True)),
                )
            except Exception as exc:
                failed.append({"id": acc_id, "email": email, "error": f"{type(exc).__name__}: {exc}"})
                continue
            item = {"id": acc_id, "email": email, **{k: v for k, v in queued.items() if k != "future"}}
            if queued.get("accepted"):
                started.append(item)
            elif queued.get("busy"):
                busy.append(item)
            else:
                failed.append(item)
        return jsonify({
            "ok": True,
            "started": started,
            "started_count": len(started),
            "busy": busy,
            "busy_count": len(busy),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
        }), 202

    def _codex_agent_auth_for_account(acc: dict) -> tuple[str, str]:
        """从 SQLite 返回账号已生成的 Codex Agent auth.json 文本与下载文件名。"""
        import json as _json

        email = str(acc.get("email") or "").strip()
        safe_email = "".join(ch if ch.isalnum() or ch in ("@", ".", "-", "_") else "_" for ch in (email or f"account-{acc.get('id')}"))
        filename = f"codex-agent-{safe_email}.json"
        token_text = str(acc.get("codex_agent_token") or "").strip()
        if token_text:
            try:
                payload = _json.loads(token_text)
                token_text = _json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            except Exception:
                token_text = token_text + ("\n" if not token_text.endswith("\n") else "")
            return token_text, filename

        stored = db.get_codex_agent_credential(int(acc.get("id") or 0))
        if stored:
            return stored

        raise RuntimeError("该账号还没有生成 Codex Agent Token")

    def _join_sub2_url(base: str, path: str) -> str:
        base = str(base or "").strip().rstrip("/")
        path = str(path or "").strip()
        if not base or not path:
            return ""
        parsed = urlparse(path)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return path
        return f"{base}/{path.lstrip('/')}"

    def _sub2_codex_session_import_url() -> str:
        from config import sub2api as sub2api_cfg
        api_base = str(getattr(sub2api_cfg, "SUB2API_API_BASE", "") or "").strip()
        if api_base:
            return _join_sub2_url(api_base, "/api/v1/admin/accounts/import/codex-session")
        # 兼容旧配置：之前 SUB2API_API_URL 是完整上传接口 URL。
        return str(getattr(sub2api_cfg, "SUB2API_API_URL", "") or "").strip()

    def _upload_account_codex_agent_to_sub2(acc: dict) -> dict:
        """把账号已生成的 Codex Agent auth.json 上传到 sub2api。"""
        import json as _json
        from config import sub2api as sub2api_cfg
        from core.codex_agent import upload_sub2api_account

        text, _filename = _codex_agent_auth_for_account(acc)
        try:
            auth_json = _json.loads(text)
        except Exception as exc:
            raise RuntimeError(f"Agent Token JSON 无效: {exc}") from exc

        api_url = _sub2_codex_session_import_url()
        api_token = str(getattr(sub2api_cfg, "SUB2API_API_KEY", "") or getattr(sub2api_cfg, "SUB2API_API_TOKEN", "") or "").strip()
        auth_header = str(getattr(sub2api_cfg, "SUB2API_API_AUTH_HEADER", "x-api-key") or "x-api-key").strip()
        auth_prefix = str(getattr(sub2api_cfg, "SUB2API_API_AUTH_PREFIX", "") or "").strip()
        payload_mode = "codex_session_import"
        proxy_key = str(getattr(sub2api_cfg, "SUB2API_PROXY_KEY", "") or "").strip() or None
        timeout = float(getattr(sub2api_cfg, "SUB2API_API_TIMEOUT", 20) or 20)

        result = upload_sub2api_account(
            auth_json,
            api_url,
            api_token=api_token,
            auth_header=auth_header,
            auth_prefix=auth_prefix,
            payload_mode=payload_mode,
            proxy_key=proxy_key,
            timeout=timeout,
        )
        try:
            db.update_account_codex_agent(int(acc.get("id")), {
                "ok": True,
                "status": "success",
                "message": "Agent Token 已上传 sub2api",
                "sub2api_url": result.get("url"),
                "sub2api_mode": result.get("payload_mode"),
                "sub2api_total": result.get("total"),
            })
        except Exception:
            logger.exception("更新账号 sub2api 上传状态失败: account_id=%s", acc.get("id"))
        return result

    @app.post("/api/accounts/<int:acc_id>/codex-agent/upload-sub2")
    def api_account_codex_agent_upload_sub2(acc_id: int):
        """单账号把已生成的 Codex Agent Token 上传到 sub2api。"""
        acc = db.get_account(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        try:
            result = _upload_account_codex_agent_to_sub2(acc)
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400
        return jsonify({"ok": True, "account_id": acc_id, "email": acc.get("email"), "result": result})

    @app.post("/api/accounts/codex-agent/upload-sub2-bulk")
    def api_accounts_codex_agent_upload_sub2_bulk():
        """批量把已生成的 Codex Agent Token 上传到 sub2api。Body {account_ids:[...]}。"""
        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多提交 500 个账号"}), 400

        uploaded, failed, skipped = [], [], []
        seen = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except Exception:
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen:
                continue
            seen.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = acc.get("email")
            if (acc.get("codex_agent_status") or "") != "success" and not (acc.get("codex_agent_token") or acc.get("codex_agent_auth_path")):
                skipped.append({"id": acc_id, "email": email, "reason": "未生成 Agent Token"})
                continue
            try:
                result = _upload_account_codex_agent_to_sub2(acc)
                uploaded.append({"id": acc_id, "email": email, "url": result.get("url"), "status_code": result.get("status_code")})
            except Exception as exc:
                failed.append({"id": acc_id, "email": email, "error": f"{type(exc).__name__}: {exc}"})
        return jsonify({
            "ok": True,
            "uploaded": uploaded,
            "uploaded_count": len(uploaded),
            "failed": failed,
            "failed_count": len(failed),
            "skipped": skipped,
            "skipped_count": len(skipped),
        })

    @app.get("/api/accounts/<int:acc_id>/codex-agent/download")
    def api_account_codex_agent_download(acc_id: int):
        """下载单个账号的 Codex Agent auth.json。"""
        acc = db.get_account(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        try:
            content, filename = _codex_agent_auth_for_account(acc)
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 404
        data = content.encode("utf-8")
        return Response(
            data,
            mimetype="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(data)),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/accounts/codex-agent/download-bulk")
    def api_accounts_codex_agent_download_bulk():
        """下载选中账号已生成的 Codex Agent Token，打包 ZIP。"""
        import io
        import json as _json
        import zipfile
        from datetime import datetime as _dt

        data = request.get_json(silent=True) or {}
        if not data and request.form:
            ids_text = (request.form.get("account_ids") or request.form.get("ids") or "").strip()
            try:
                ids = _json.loads(ids_text) if ids_text else []
            except Exception:
                ids = [x.strip() for x in ids_text.split(",") if x.strip()]
        else:
            ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 1000:
            return jsonify({"ok": False, "error": "单次最多下载 1000 个账号"}), 400

        added = []
        errors = []
        used_names = set()
        seen = set()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for raw in ids:
                try:
                    acc_id = int(raw)
                except Exception:
                    errors.append({"id": raw, "error": "ID 非法"})
                    continue
                if acc_id in seen:
                    continue
                seen.add(acc_id)
                acc = db.get_account(acc_id)
                if not acc:
                    errors.append({"id": acc_id, "error": "账号不存在"})
                    continue
                try:
                    content, filename = _codex_agent_auth_for_account(acc)
                    arcname = filename
                    if arcname in used_names:
                        stem, dot, ext = arcname.rpartition(".")
                        arcname = f"{stem or arcname}-{len(used_names)+1}{dot}{ext}" if dot else f"{arcname}-{len(used_names)+1}"
                    used_names.add(arcname)
                    zf.writestr(arcname, content)
                    added.append({"id": acc_id, "email": acc.get("email"), "filename": arcname})
                except Exception as exc:
                    errors.append({"id": acc_id, "email": acc.get("email"), "error": f"{type(exc).__name__}: {exc}"})
            manifest = {
                "exported_at": _dt.now().isoformat(timespec="seconds"),
                "source": "accounts-codex-agent",
                "count": len(added),
                "files": added,
                "errors": errors,
            }
            zf.writestr("manifest.json", _json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

        if not added:
            return jsonify({"ok": False, "error": "没有可下载的 Codex Agent Token", "errors": errors}), 404
        now = _dt.now()
        dl_name = f"accounts-codex-agent-{now.strftime('%Y%m%d-%H%M%S')}.zip"
        buf.seek(0)
        zip_bytes = buf.getvalue()
        return Response(
            zip_bytes,
            mimetype="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{dl_name}"',
                "Content-Length": str(len(zip_bytes)),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.post("/api/accounts/download-cpa-bulk")
    def api_accounts_download_cpa_bulk():
        """
        从账号列表选中的账号直接到 CPA auth-files 下载 Codex CPA JSON，并打包为 ZIP。
        Body: {"account_ids": [1,2,...]} 或 {"ids": [...]}
        """
        import io
        import json as _json
        import zipfile
        from datetime import datetime as _dt
        from core.codex_oauth import download_cpa_codex_auth_text, list_cpa_codex_auth_files

        data = request.get_json(silent=True) or {}
        if not data and request.form:
            ids_text = (request.form.get("account_ids") or request.form.get("ids") or "").strip()
            try:
                ids = _json.loads(ids_text) if ids_text else []
            except Exception:
                ids = [x.strip() for x in ids_text.split(",") if x.strip()]
        else:
            ids = data.get("account_ids") or data.get("ids") or []
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        if len(ids) > 1000:
            return jsonify({"ok": False, "error": "单次最多下载 1000 个账号"}), 400

        try:
            cpa_files = list_cpa_codex_auth_files()
        except Exception as exc:
            return jsonify({"ok": False, "error": f"读取 CPA auth-files 失败: {type(exc).__name__}: {exc}"}), 502

        def _match_cpa_file(email: str, local_filename: str = "") -> dict | None:
            """在已缓存的 CPA 文件列表中匹配，避免每个账号都重新请求 auth-files。"""
            email_l = str(email or "").strip().lower()
            local_name_l = str(local_filename or "").strip().lower()
            local_stem_l = local_name_l[:-5] if local_name_l.endswith(".json") else local_name_l

            def score(item: dict) -> int:
                name_l = str(item.get("name") or "").lower()
                item_email_l = str(item.get("email") or "").lower()
                s = 0
                if local_name_l and name_l == local_name_l:
                    s = max(s, 100)
                if local_stem_l and name_l.startswith(local_stem_l):
                    s = max(s, 80)
                if email_l and item_email_l == email_l:
                    s = max(s, 70)
                if email_l and email_l in name_l:
                    s = max(s, 60)
                if local_stem_l.endswith("-cpa-callback"):
                    base = local_stem_l[:-len("-cpa-callback")]
                    if base and name_l.startswith(base + "-"):
                        s = max(s, 75)
                return s

            ranked = sorted(((score(item), item) for item in cpa_files), key=lambda x: x[0], reverse=True)
            return ranked[0][1] if ranked and ranked[0][0] > 0 else None

        # 建立 email -> 本地 codex 文件名索引；有本地文件名时传给 CPA 匹配逻辑可提升命中率。
        local_by_email: dict[str, str] = {}
        try:
            for item in db.list_codex_accounts():
                email_key = str(item.get("email") or "").strip().lower()
                fname = str(item.get("filename") or "").strip()
                if email_key and fname and email_key not in local_by_email:
                    local_by_email[email_key] = fname
        except Exception:
            local_by_email = {}

        errors = []
        added = []
        used_names = set()
        seen_ids = set()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for raw_id in ids:
                try:
                    acc_id = int(raw_id)
                except (TypeError, ValueError):
                    errors.append({"id": raw_id, "error": "ID 非法"})
                    continue
                if acc_id in seen_ids:
                    continue
                seen_ids.add(acc_id)

                acc = db.get_account(acc_id)
                if not acc:
                    errors.append({"id": acc_id, "error": "账号不存在"})
                    continue
                email = str(acc.get("email") or "").strip()
                if not email:
                    errors.append({"id": acc_id, "error": "账号缺少 email"})
                    continue

                local_filename = local_by_email.get(email.lower(), "")
                try:
                    meta = _match_cpa_file(email=email, local_filename=local_filename)
                    cpa_name_hint = str((meta or {}).get("name") or "").strip()
                    if not cpa_name_hint:
                        raise RuntimeError(f"[Codex][CPA] 未在 CPA auth-files 中找到匹配的 Codex 凭证: {email}")
                    cpa_text, cpa_name, meta = download_cpa_codex_auth_text(
                        cpa_name=cpa_name_hint,
                    )
                    arcname = cpa_name
                    if arcname in used_names:
                        stem, dot, ext = arcname.rpartition(".")
                        arcname = f"{stem or arcname}-{len(used_names)+1}{dot}{ext}" if dot else f"{arcname}-{len(used_names)+1}"
                    used_names.add(arcname)
                    zf.writestr(arcname, cpa_text)
                    added.append({
                        "id": acc_id,
                        "email": email,
                        "local_filename": local_filename,
                        "cpa_filename": cpa_name,
                        "cpa_meta": meta,
                    })
                    if local_filename:
                        try:
                            db.mark_codex_exported(local_filename)
                        except Exception:
                            pass
                except Exception as exc:
                    errors.append({"id": acc_id, "email": email, "error": f"{type(exc).__name__}: {exc}"})

            manifest = {
                "exported_at": _dt.now().isoformat(timespec="seconds"),
                "source": "accounts-cpa",
                "count": len(added),
                "files": added,
                "errors": errors,
            }
            zf.writestr("manifest.json", _json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

        if not added:
            return jsonify({"ok": False, "error": "没有成功从 CPA 下载任何凭证", "errors": errors}), 502
        now = _dt.now()
        dl_name = f"accounts-cpa-bulk-{now.strftime('%Y%m%d-%H%M%S')}.zip"
        buf.seek(0)
        zip_bytes = buf.getvalue()
        if isinstance(data, dict) and data.get("prepare"):
            download_id = _put_prepared_download(zip_bytes, dl_name, "application/zip")
            return jsonify({
                "ok": True,
                "prepared": True,
                "download_id": download_id,
                "download_url": f"/api/downloads/{download_id}",
                "filename": dl_name,
                "added_count": len(added),
                "error_count": len(errors),
            })
        return Response(
            zip_bytes,
            mimetype="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{dl_name}"',
                "Content-Length": str(len(zip_bytes)),
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
                "X-Content-Type-Options": "nosniff",
                "X-Download-Options": "noopen",
            },
        )

    # ----------------------------------------------------------
    # 邮箱池
    # ----------------------------------------------------------
    @app.get("/api/outlook")
    def api_outlook():
        status = request.args.get("status") or None
        limit = request.args.get("limit", default=500, type=int)
        source = _pool_source_arg()
        q = str(request.args.get("q", default="") or "").strip()
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        if paged or page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            offset = (page - 1) * page_size
            result = db.list_email_pool_page(
                source=source, status=status, q=q, limit=page_size, offset=offset
            )
            result.update({"ok": True, "page": page, "page_size": page_size})
            return jsonify(result)
        # 兼容旧接口仍返回数组，但查询本身也只从 SQLite 读取 limit 条。
        result = db.list_email_pool_page(
            source=source, status=status, q=q, limit=max(1, int(limit or 1)), offset=0
        )
        return jsonify(result["items"])

    @app.post("/api/outlook/import")
    def api_outlook_import():
        """
        粘贴文本导入邮箱素材。
        Outlook：email----password----clientId----refreshToken
        通用 API：email----code_url
        通用 IMAP：email----password 或 email:password；服务器/端口/SSL 单独传入
        分隔符兼容 ---- 与 ====。
        """
        data = request.get_json(silent=True) or {}
        source = (data.get("source") or data.get("type") or "").strip()
        if source not in ("outlook", "generic_api", "imap"):
            return jsonify({"ok": False, "error": "导入时请选择具体类型：Outlook、通用 API 或通用 IMAP"}), 400
        text = data.get("text") or ""
        as_registered = bool(data.get("as_registered", False))
        imap_server = str(data.get("imap_server") or "").strip()
        try:
            imap_port = int(data.get("imap_port") or 993)
        except (TypeError, ValueError):
            imap_port = 0
        imap_ssl_raw = data.get("imap_ssl", True)
        imap_ssl = imap_ssl_raw if isinstance(imap_ssl_raw, bool) else str(imap_ssl_raw).strip().lower() not in {"0", "false", "no", "off"}
        if source == "imap" and (not imap_server or not (1 <= imap_port <= 65535)):
            return jsonify({"ok": False, "error": "通用 IMAP 导入必须填写有效的服务器和端口"}), 400
        records = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if source == "imap":
                if "----" in line:
                    parts = line.split("----", 1)
                elif "====" in line:
                    parts = line.split("====", 1)
                elif ":" in line:
                    parts = line.split(":", 1)
                else:
                    continue
            else:
                parts = line.split("----") if "----" in line else line.split("====")
            parts = [p.strip() for p in parts]
            if source == "generic_api":
                if len(parts) < 2:
                    continue
                records.append({
                    "email": parts[0],
                    "code_url": parts[1],
                    "access_token": parts[2] if len(parts) > 2 else "",
                    "totp_secret": parts[3] if len(parts) > 3 else "",
                })
                continue
            if source == "imap":
                if len(parts) < 2 or not parts[0] or not parts[1]:
                    continue
                records.append({
                    "email": parts[0], "imap_password": parts[1],
                    "imap_server": imap_server, "imap_port": imap_port,
                    "imap_ssl": imap_ssl, "imap_username": "",
                })
                continue
            if len(parts) < 4:
                continue
            records.append({
                "email": parts[0],
                "password": parts[1],
                "client_id": parts[2],
                "refresh_token": parts[3],
                "access_token": parts[4] if len(parts) > 4 else "",
                "totp_secret": parts[5] if len(parts) > 5 else "",
            })
        if not records:
            need = ("2 段：邮箱----取码地址" if source == "generic_api" else
                    "邮箱----IMAP密码 或 邮箱:IMAP密码" if source == "imap" else
                    "4 段：email----password----clientId----refreshToken")
            return jsonify({"ok": False, "error": f"未解析到有效邮箱行（需 {need}，---- 或 ==== 分隔）"}), 400
        if as_registered:
            inserted, skipped = db.import_registered_email_accounts(records, source=source)
        elif source == "generic_api":
            inserted, skipped = db.import_generic_api_emails(records)
        elif source == "imap":
            inserted, skipped = db.import_imap_emails(records)
        else:
            inserted, skipped = db.import_outlook_accounts(records)
        return jsonify({
            "ok": True,
            "inserted": inserted,
            "skipped": skipped,
            "parsed": len(records),
            "as_registered": as_registered,
        })

    @app.post("/api/outlook/status")
    def api_outlook_status():
        """手动改邮箱状态：body {email, status, note?, source?}。status ∈ available/used/failed/disabled。"""
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        status = (data.get("status") or "").strip()
        if not email or status not in ("available", "used", "failed", "disabled"):
            return jsonify({"ok": False, "error": "email 或 status 非法"}), 400
        source = (data.get("source") or _pool_source_arg()).strip()
        if source == "all":
            source = "outlook"
        if source == "generic_api":
            db.release_generic_api_email(email, status=status, note=data.get("note"))
        elif source == "imap":
            db.release_imap_email(email, status=status, note=data.get("note"))
        elif source == "cloudflare_domain":
            db.release_domain_email(email, status=status, note=data.get("note"))
        else:
            db.release_outlook(email, status=status, note=data.get("note"))
        return jsonify({"ok": True})

    @app.post("/api/outlook/status-bulk")
    def api_outlook_status_bulk():
        """批量修改邮箱状态。Body {items:[{email,source}], status, note?}。"""
        data = request.get_json(silent=True) or {}
        items = data.get("items") or data.get("emails") or []
        status = (data.get("status") or "").strip()
        note = data.get("note")
        default_source = (data.get("source") or _pool_source_arg()).strip()
        if status not in ("available", "used", "failed", "disabled"):
            return jsonify({"ok": False, "error": "status 非法"}), 400
        if not isinstance(items, list) or not items:
            return jsonify({"ok": False, "error": "items/emails 必须是非空数组"}), 400
        if len(items) > 5000:
            return jsonify({"ok": False, "error": "单次最多操作 5000 个邮箱"}), 400

        updated = []
        skipped = []
        seen = set()
        for raw_item in items:
            if isinstance(raw_item, dict):
                email = (str(raw_item.get("email") or "")).strip()
                item_source = (raw_item.get("source") or default_source or "outlook").strip()
            else:
                email = (str(raw_item or "")).strip()
                item_source = default_source
            if item_source == "all":
                item_source = "outlook"
            key = f"{item_source}:{email.lower()}"
            if not email:
                skipped.append({"email": raw_item, "reason": "邮箱为空"})
                continue
            if key in seen:
                continue
            seen.add(key)
            try:
                if item_source == "generic_api":
                    db.release_generic_api_email(email, status=status, note=note)
                elif item_source == "imap":
                    db.release_imap_email(email, status=status, note=note)
                elif item_source == "cloudflare_domain":
                    db.release_domain_email(email, status=status, note=note)
                else:
                    db.release_outlook(email, status=status, note=note)
                updated.append({"email": email, "source": item_source, "status": status})
            except Exception as exc:
                skipped.append({"email": email, "source": item_source, "reason": f"{type(exc).__name__}: {exc}"})
        return jsonify({
            "ok": True,
            "updated": updated,
            "updated_count": len(updated),
            "skipped": skipped,
        })

    @app.post("/api/outlook/delete")
    def api_outlook_delete():
        """从邮箱池彻底删除一个邮箱：body {email, source?}。"""
        data = request.get_json(silent=True) or {}
        email = str(data.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        raw_source = data.get("source") or data.get("type")
        source = (
            _pool_source_arg()
            if not str(raw_source or "").strip()
            else str(raw_source).strip().lower()
        )
        if source not in _POOL_SOURCE_VALUES:
            return jsonify({"ok": False, "error": "邮箱来源非法"}), 400
        deleted = db.delete_email_pool(email, source=source)
        return jsonify({"ok": True, "deleted": deleted})

    @app.post("/api/outlook/delete-bulk")
    def api_outlook_delete_bulk():
        """从邮箱池批量彻底删除邮箱：body {items/emails: [...], source?}。"""
        data = request.get_json(silent=True) or {}
        raw_source = data.get("source") or data.get("type")
        source = (
            _pool_source_arg()
            if not str(raw_source or "").strip()
            else str(raw_source).strip().lower()
        )
        if source not in _POOL_SOURCE_VALUES:
            return jsonify({"ok": False, "error": "邮箱来源非法"}), 400
        emails = data.get("items") or data.get("emails") or []
        if not isinstance(emails, list) or not emails:
            return jsonify({"ok": False, "error": "emails/items 必须是非空数组"}), 400
        if len(emails) > 5000:
            return jsonify({"ok": False, "error": "单次最多删除 5000 个邮箱"}), 400

        deleted: list[dict] = []
        skipped: list[dict] = []
        seen: set[str] = set()
        for raw_item in emails:
            if isinstance(raw_item, dict):
                email = str(raw_item.get("email") or "").strip()
                raw_item_source = raw_item.get("source") or raw_item.get("type")
                item_source = (
                    source
                    if not str(raw_item_source or "").strip()
                    else str(raw_item_source).strip().lower()
                )
            else:
                email = (str(raw_item or "")).strip()
                item_source = source
            if not email:
                skipped.append({"email": raw_item, "reason": "邮箱为空"})
                continue
            if item_source not in _POOL_SOURCE_VALUES:
                skipped.append({"email": email, "source": item_source, "reason": "邮箱来源非法"})
                continue
            key = f"{item_source}:{email.casefold()}"
            if key in seen:
                continue
            seen.add(key)
            try:
                deleted_ok = db.delete_email_pool(email, source=item_source)
            except Exception as exc:
                skipped.append({
                    "email": email,
                    "source": item_source,
                    "reason": f"{type(exc).__name__}: {exc}",
                })
                continue
            if deleted_ok:
                deleted.append({"email": email, "source": item_source})
            else:
                skipped.append({"email": email, "reason": "邮箱不存在"})

        return jsonify({
            "ok": True,
            "deleted": deleted,
            "deleted_count": len(deleted),
            "skipped": skipped,
        })

    # ----------------------------------------------------------
    # 域名邮箱池（Cloudflare 域名邮箱模式）
    # ----------------------------------------------------------
    @app.get("/api/domain-pool")
    def api_domain_pool():
        status = request.args.get("status") or None
        limit = request.args.get("limit", default=500, type=int)
        return jsonify(db.list_domain_email_pool(status=status, limit=limit))

    @app.post("/api/domain-pool/status")
    def api_domain_pool_status():
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        status = (data.get("status") or "").strip()
        if not email or status not in ("available", "used", "failed"):
            return jsonify({"ok": False, "error": "email 或 status 非法"}), 400
        db.release_domain_email(email, status=status, note=data.get("note"))
        return jsonify({"ok": True})

    @app.post("/api/domain-pool/delete")
    def api_domain_pool_delete():
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        deleted = db.delete_domain_email(email)
        return jsonify({"ok": True, "deleted": deleted})

    # ----------------------------------------------------------
    # Codex 授权账号（CPA 兼容凭证）
    # ----------------------------------------------------------
    @app.get("/api/codex")
    def api_codex_list():
        q = str(request.args.get("q", default="") or "").strip()
        archived = str(request.args.get("archived", default="0") or "0").lower()
        date_from = str(request.args.get("date_from", default="") or "").strip() or None
        date_to = str(request.args.get("date_to", default="") or "").strip() or None
        limit = request.args.get("limit", default=500, type=int)
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        if paged or page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            result = db.list_codex_accounts_page(
                archived=archived,
                date_from=date_from,
                date_to=date_to,
                q=q,
                limit=page_size,
                offset=(page - 1) * page_size,
            )
            result.update({"ok": True, "page": page, "page_size": page_size})
            result["accounts"] = result.pop("items")
            result["summary"] = db.codex_accounts_summary()
            return jsonify(result)
        result = db.list_codex_accounts_page(
            archived=archived,
            date_from=date_from,
            date_to=date_to,
            q=q,
            limit=max(1, int(limit or 1)),
            offset=0,
        )
        return jsonify({
            "summary": db.codex_accounts_summary(),
            "accounts": result["items"],
        })

    @app.post("/api/codex/archive")
    def api_codex_archive():
        """归档/取消归档一条 Codex 授权凭证。Body {filename, archived}。"""
        data = request.get_json(silent=True) or {}
        filename = str(data.get("filename") or "").strip()
        archived = bool(data.get("archived", True))
        if not filename:
            return jsonify({"ok": False, "error": "filename 必填"}), 400
        try:
            rec = db.archive_codex(filename=filename, archived=archived)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if rec is None:
            return jsonify({"ok": False, "error": f"凭证不存在: {filename}"}), 404
        return jsonify({"ok": True, "filename": filename, "archived": archived, "record": rec})

    @app.post("/api/codex/archive-bulk")
    def api_codex_archive_bulk():
        """批量归档/取消归档 Codex 授权凭证。Body {filenames:[...], archived}。"""
        data = request.get_json(silent=True) or {}
        filenames = data.get("filenames") or []
        archived = bool(data.get("archived", True))
        if not isinstance(filenames, list) or not filenames:
            return jsonify({"ok": False, "error": "filenames 必须是非空数组"}), 400
        if len(filenames) > 1000:
            return jsonify({"ok": False, "error": "单次最多 1000 个"}), 400
        updated = []
        skipped = []
        seen = set()
        for fname in filenames:
            if not isinstance(fname, str) or not fname:
                skipped.append({"filename": str(fname), "reason": "非法文件名"})
                continue
            if fname in seen:
                continue
            seen.add(fname)
            try:
                rec = db.archive_codex(filename=fname, archived=archived)
            except ValueError as exc:
                skipped.append({"filename": fname, "reason": str(exc)})
                continue
            if rec is None:
                skipped.append({"filename": fname, "reason": "凭证不存在"})
            else:
                updated.append({"filename": fname, "archived": archived})
        return jsonify({"ok": True, "updated": updated, "updated_count": len(updated), "archived": archived, "skipped": skipped})

    @app.get("/api/codex/download/<path:filename>")
    def api_codex_download(filename: str):
        """
        下载一个 CPA 兼容的 codex-*.json 文件，下载即标记为已导出（计数+1）。
        前端通过浏览器原生下载触发（a 标签 / window.location）。
        """
        try:
            content, fname = db.read_codex_credential(filename)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        db.mark_codex_exported(fname)
        return Response(
            content,
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    @app.get("/api/codex/download-from-cpa/<path:filename>")
    def api_codex_download_from_cpa(filename: str):
        """按本地 codex 文件/回执匹配 CPA auth-files，并从 CPA 下载实际 Codex JSON。"""
        try:
            content, fname = db.read_codex_credential(filename)
            import json as _json
            try:
                local = _json.loads(content)
            except Exception:
                local = {}
            email = str(local.get("email") or "").strip()
            from core.codex_oauth import download_cpa_codex_auth_text
            cpa_text, cpa_name, _meta = download_cpa_codex_auth_text(email=email, local_filename=fname)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 404
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 502
        db.mark_codex_exported(fname)
        return Response(
            cpa_text,
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{cpa_name}"'},
        )

    @app.post("/api/codex/download-bulk-from-cpa")
    def api_codex_download_bulk_from_cpa():
        """
        批量从 CPA 下载选中的 Codex 凭证，打包成 zip；zip 内每个文件都是 CPA 原始 JSON。
        Body: {"filenames": ["codex-xxx-cpa-callback.json", ...]}
        """
        import io
        import json as _json
        import zipfile
        from datetime import datetime as _dt
        from core.codex_oauth import download_cpa_codex_auth_text

        data = request.get_json(silent=True) or {}
        filenames = data.get("filenames") or []
        if not isinstance(filenames, list) or not filenames:
            return jsonify({"ok": False, "error": "filenames 必须是非空数组"}), 400
        if len(filenames) > 1000:
            return jsonify({"ok": False, "error": "单次最多 1000 个"}), 400

        errors = []
        added = []
        used_names = set()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for fname in filenames:
                if not isinstance(fname, str):
                    errors.append({"filename": str(fname), "error": "非字符串"})
                    continue
                try:
                    content, real_fname = db.read_codex_credential(fname)
                    try:
                        local = _json.loads(content)
                    except Exception:
                        local = {}
                    email = str(local.get("email") or "").strip()
                    cpa_text, cpa_name, _meta = download_cpa_codex_auth_text(email=email, local_filename=real_fname)
                    arcname = cpa_name
                    if arcname in used_names:
                        stem, dot, ext = arcname.rpartition(".")
                        arcname = f"{stem or arcname}-{len(used_names)+1}{dot}{ext}" if dot else f"{arcname}-{len(used_names)+1}"
                    used_names.add(arcname)
                    zf.writestr(arcname, cpa_text)
                    added.append({"local_filename": real_fname, "cpa_filename": cpa_name})
                    db.mark_codex_exported(real_fname)
                except Exception as exc:
                    errors.append({"filename": fname, "error": f"{type(exc).__name__}: {exc}"})
            manifest = {
                "exported_at": _dt.now().isoformat(timespec="seconds"),
                "source": "cpa",
                "count": len(added),
                "files": added,
                "errors": errors,
            }
            zf.writestr("manifest.json", _json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")

        if not added:
            return jsonify({"ok": False, "error": "没有成功从 CPA 下载任何凭证", "errors": errors}), 502
        now = _dt.now()
        dl_name = f"codex-cpa-bulk-{now.strftime('%Y%m%d-%H%M%S')}.zip"
        buf.seek(0)
        return Response(
            buf.getvalue(),
            mimetype="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{dl_name}"'},
        )

    @app.post("/api/codex/download-bulk")
    def api_codex_download_bulk():
        """
        批量下载选中的 codex 凭证，打包到一个 JSON 文件里。

        Body: {"filenames": ["codex-xxx.json", ...]}
        响应：聚合 JSON（attachment 触发浏览器下载），结构：
            {
              "exported_at": "...",
              "count": N,
              "credentials": [{"filename": "...", "data": {...原始凭证内容...}}, ...],
              "errors": [...]   // 仅当部分失败时出现
            }
        注意：聚合格式**不能直接被 CPA 读**，CPA 是按单文件加载 auths/ 目录的。
              本接口主要用途是备份 / 跨机迁移 / 二次处理。
        每个成功的凭证会自动标记 mark_exported（计数+1）。
        """
        import json as _json
        from datetime import datetime as _dt

        data = request.get_json(silent=True) or {}
        filenames = data.get("filenames") or []
        if not isinstance(filenames, list) or not filenames:
            return jsonify({"ok": False, "error": "filenames 必须是非空数组"}), 400
        if len(filenames) > 1000:
            return jsonify({"ok": False, "error": "单次最多 1000 个"}), 400

        bundle = []
        errors = []
        for fname in filenames:
            if not isinstance(fname, str):
                errors.append({"filename": str(fname), "error": "非字符串"})
                continue
            try:
                content, real_fname = db.read_codex_credential(fname)
                parsed = _json.loads(content)
                bundle.append({"filename": real_fname, "data": parsed})
                db.mark_codex_exported(real_fname)
            except Exception as exc:
                errors.append({"filename": fname, "error": f"{type(exc).__name__}: {exc}"})

        now = _dt.now()
        result = {
            "exported_at": now.isoformat(timespec="seconds"),
            "count": len(bundle),
            "credentials": bundle,
        }
        if errors:
            result["errors"] = errors

        dl_name = f"codex-bulk-{now.strftime('%Y%m%d-%H%M%S')}.json"
        return Response(
            _json.dumps(result, ensure_ascii=False, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": f'attachment; filename="{dl_name}"'},
        )

    @app.post("/api/codex/reset-export")
    def api_codex_reset_export():
        """清掉某个 codex 凭证的导出状态（重新标为未导出）。body {filename}。"""
        data = request.get_json(silent=True) or {}
        fname = (data.get("filename") or "").strip()
        if not fname:
            return jsonify({"ok": False, "error": "filename 为空"}), 400
        try:
            db.reset_codex_exported(fname)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True})

    @app.post("/api/codex/delete")
    def api_codex_delete():
        """删除一个 codex 凭证文件。body {filename}。"""
        data = request.get_json(silent=True) or {}
        fname = (data.get("filename") or "").strip()
        if not fname:
            return jsonify({"ok": False, "error": "filename 为空"}), 400
        try:
            deleted = db.delete_codex_credential(fname)
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        if not deleted:
            return jsonify({"ok": False, "error": "凭证文件不存在"}), 404
        return jsonify({"ok": True, "deleted": fname})

    @app.post("/api/codex/delete-bulk")
    def api_codex_delete_bulk():
        """批量删除 codex 凭证文件。body {filenames:[...]}。"""
        data = request.get_json(silent=True) or {}
        filenames = data.get("filenames") or []
        if not isinstance(filenames, list) or not filenames:
            return jsonify({"ok": False, "error": "filenames 必须是非空数组"}), 400
        if len(filenames) > 1000:
            return jsonify({"ok": False, "error": "单次最多删除 1000 个"}), 400
        deleted = []
        skipped = []
        seen = set()
        for fname in filenames:
            fname = str(fname or "").strip()
            if not fname or fname in seen:
                continue
            seen.add(fname)
            try:
                ok = db.delete_codex_credential(fname)
                if ok:
                    deleted.append(fname)
                else:
                    skipped.append({"filename": fname, "reason": "文件不存在"})
            except Exception as exc:
                skipped.append({"filename": fname, "reason": f"{type(exc).__name__}: {exc}"})
        return jsonify({"ok": True, "deleted": deleted, "deleted_count": len(deleted), "skipped": skipped})

    def _reserve_codex_retry(email: str) -> bool:
        """进程内防重复占位；成功返回 True。"""
        return codex_retry_service.reserve(email)

    def _release_codex_retry(email: str) -> None:
        codex_retry_service.release(email)

    def _run_codex_retry_worker(email: str, *, batch_label: str | None = None, clear_log: bool = True) -> None:
        """执行一个账号的 Codex 补跑。调用前必须已经 reserve。"""
        codex_retry_service.run_worker(email, batch_label=batch_label, clear_log=clear_log)


    @app.post("/api/codex/stop")
    def api_codex_stop():
        """停止单个 Codex 补跑。Body {email}。"""
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        acc = db.get_account_by_email(email)
        if acc is None:
            return jsonify({"ok": False, "error": f"账号不存在: {email}"}), 404
        result = codex_retry_service.request_stop(email)
        status = int(result.pop("status", 200) or 200)
        return jsonify(result), status

    @app.post("/api/codex/stop-bulk")
    def api_codex_stop_bulk():
        """批量停止 Codex 补跑。Body {emails:[...]} 或 {account_ids:[...]}。"""
        data = request.get_json(silent=True) or {}
        emails = data.get("emails") or []
        ids = data.get("account_ids") or data.get("ids") or []
        targets = []
        if isinstance(emails, list) and emails:
            targets = [str(x or "").strip() for x in emails]
        elif isinstance(ids, list) and ids:
            for raw in ids:
                try:
                    acc = db.get_account(int(raw))
                except Exception:
                    acc = None
                if acc and acc.get("email"):
                    targets.append(str(acc.get("email") or "").strip())
        else:
            return jsonify({"ok": False, "error": "emails 或 account_ids 必须是非空数组"}), 400
        if len(targets) > 500:
            return jsonify({"ok": False, "error": "单次最多停止 500 个"}), 400
        stopped = []
        skipped = []
        seen = set()
        for email in targets:
            key = email.lower()
            if not email or key in seen:
                continue
            seen.add(key)
            acc = db.get_account_by_email(email)
            if acc is None:
                skipped.append({"email": email, "reason": "账号不存在"})
                continue
            if (acc.get("codex_status") or "") != "retrying" and not codex_retry_service.is_retrying(email):
                skipped.append({"email": email, "reason": "未处于补跑中"})
                continue
            r = codex_retry_service.request_stop(email)
            if r.get("ok"):
                stopped.append({"email": email, "injected": r.get("injected"), "running": r.get("running")})
            else:
                skipped.append({"email": email, "reason": r.get("error") or "停止失败"})
        return jsonify({"ok": True, "stopped": stopped, "stopped_count": len(stopped), "skipped": skipped})

    @app.post("/api/codex/reset-retrying")
    def api_codex_reset_retrying():
        """手动重置某账号的 Codex 补跑中状态。Body {email, status?}。"""
        from datetime import datetime as _dt

        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        raw_status = (data.get("status") or "failed").strip().lower()
        if raw_status in ("", "none", "null", "clear"):
            raw_status = "empty"
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        if raw_status not in ("failed", "skipped", "empty"):
            return jsonify({"ok": False, "error": "status 仅支持 failed/skipped/empty"}), 400

        acc = db.get_account_by_email(email)
        if acc is None:
            return jsonify({"ok": False, "error": f"账号不存在: {email}"}), 404

        new_status = "" if raw_status == "empty" else raw_status
        err = None if raw_status == "empty" else "用户手动重置补跑中状态"
        ok = db.update_account_codex_status(email, new_status, err)
        if not ok:
            return jsonify({"ok": False, "error": f"账号不存在: {email}"}), 404

        _release_codex_retry(email)

        try:
            log_path = codex_retry_service.log_path(email)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as f:
                ts = _dt.now().strftime("%H:%M:%S")
                shown = new_status or "空"
                f.write(f"{ts} [WARNING] [Codex 补跑] 用户手动重置补跑中状态，当前状态={shown}\n")
        except Exception:
            logger.exception("写入 Codex 补跑重置日志失败")

        return jsonify({"ok": True, "message": "已重置补跑中状态", "status": new_status})

    @app.post("/api/codex/retry")
    def api_codex_retry():
        """手动补跑某账号的 Codex 授权。Body {email}。"""
        data = request.get_json(silent=True) or {}
        email = (data.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        acc = db.get_account_by_email(email)
        if acc is None:
            return jsonify({"ok": False, "error": f"账号不存在: {email}"}), 404
        if (acc.get("live_check_status") or "") == "deactivated":
            return jsonify({"ok": False, "error": "账号已废号，不能补跑 Codex"}), 409
        if not _reserve_codex_retry(email):
            return jsonify({"ok": False, "error": "该账号正在补跑中，请稍候"}), 409

        db.update_account_codex_status(email, "retrying", None)
        threading.Thread(
            target=_run_codex_retry_worker,
            kwargs={"email": email, "clear_log": True},
            name=f"codex-retry-{email}",
            daemon=True,
        ).start()
        return jsonify({"ok": True, "message": "已在后台开始补跑，~1-2 分钟后刷新查看"})

    @app.post("/api/codex/retry-bulk")
    def api_codex_retry_bulk():
        """批量补跑 Codex。Body {account_ids:[...], workers: 1-16}。"""
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from datetime import datetime as _dt

        data = request.get_json(silent=True) or {}
        ids = data.get("account_ids") or data.get("ids") or []
        workers = data.get("workers", 1)
        if not isinstance(ids, list) or not ids:
            return jsonify({"ok": False, "error": "account_ids 必须是非空数组"}), 400
        try:
            workers = max(1, min(16, int(workers)))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 必须是数字"}), 400
        if len(ids) > 500:
            return jsonify({"ok": False, "error": "单次最多选择 500 个账号"}), 400

        selected = []
        skipped = []
        seen_ids = set()
        for raw in ids:
            try:
                acc_id = int(raw)
            except (TypeError, ValueError):
                skipped.append({"id": raw, "reason": "ID 非法"})
                continue
            if acc_id in seen_ids:
                continue
            seen_ids.add(acc_id)
            acc = db.get_account(acc_id)
            if not acc:
                skipped.append({"id": acc_id, "reason": "账号不存在"})
                continue
            email = (acc.get("email") or "").strip()
            if not email:
                skipped.append({"id": acc_id, "reason": "邮箱为空"})
                continue
            if (acc.get("live_check_status") or "") == "deactivated":
                skipped.append({"id": acc_id, "email": email, "reason": "账号已废号"})
                continue
            if not _reserve_codex_retry(email):
                skipped.append({"id": acc_id, "email": email, "reason": "正在补跑中"})
                continue
            selected.append({"id": acc_id, "email": email})

        if not selected:
            return jsonify({"ok": False, "error": "没有可补跑的账号", "skipped": skipped}), 409

        batch_id = _dt.now().strftime("%Y%m%d-%H%M%S")
        for item in selected:
            email = item["email"]
            db.update_account_codex_status(email, "retrying", None)
            log_path = codex_retry_service.log_path(email)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(
                f"{_dt.now().strftime('%H:%M:%S')} [INFO] [Codex 批量补跑] 已加入批量任务 batch={batch_id} workers={workers}，等待线程执行\n",
                encoding="utf-8",
            )

        def _bulk_runner(items: list[dict], max_workers: int, batch: str):
            logger.info(f"[Codex 批量补跑] 启动 batch={batch} count={len(items)} workers={max_workers}")
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=f"codex-bulk-{batch}") as ex:
                futures = [ex.submit(_run_codex_retry_worker, it["email"], batch_label=f"{batch} #{idx}/{len(items)}", clear_log=False) for idx, it in enumerate(items, 1)]
                for fut in as_completed(futures):
                    try:
                        fut.result()
                    except Exception:
                        logger.exception(f"[Codex 批量补跑] 子任务异常 batch={batch}")
            logger.info(f"[Codex 批量补跑] 完成 batch={batch}")

        threading.Thread(
            target=_bulk_runner,
            args=(selected, workers, batch_id),
            name=f"codex-bulk-dispatch-{batch_id}",
            daemon=True,
        ).start()
        return jsonify({
            "ok": True,
            "message": f"已开始批量补跑 {len(selected)} 个账号，并发 {workers}",
            "started": selected,
            "started_count": len(selected),
            "skipped": skipped,
            "batch_id": batch_id,
        })

    @app.get("/api/codex/retry-log")
    def api_codex_retry_log():
        """读取某邮箱最近一次补跑的日志。?email=xxx"""
        email = (request.args.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        p = codex_retry_service.log_path(email)
        if not p.exists():
            return jsonify({"ok": True, "log": "", "running": False})
        max_bytes = 50_000
        size = p.stat().st_size
        with p.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            content = f.read().decode("utf-8", errors="replace")
        return jsonify({
            "ok": True,
            "log": content,
            "running": codex_retry_service.is_retrying(email),
        })

    @app.get("/api/accounts/live-check-log")
    def api_account_live_check_log():
        """读取某邮箱最近一次查活日志。?email=xxx"""
        from core import account_liveness
        email = (request.args.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        p = account_liveness.log_path(email)
        data = _read_log_tail(p, max_bytes=80_000, running_fn=lambda: live_check_service.is_checking(email))
        return jsonify(data)

    @app.get("/api/accounts/totp-setup-log")
    def api_account_totp_setup_log():
        """读取某邮箱最近一次 2FA 设置日志。?email=xxx"""
        from core import twofa_service
        email = (request.args.get("email") or "").strip()
        if not email:
            return jsonify({"ok": False, "error": "email 为空"}), 400
        p = twofa_service.log_path(email)
        data = _read_log_tail(p, max_bytes=80_000, running_fn=lambda: False)
        try:
            acc = db.get_account_by_email(email) or {}
            data["running"] = bool(str(acc.get("totp_setup_status") or "") in {"queued", "running"}) or twofa_service.is_running(int(acc.get("id") or 0))
        except Exception:
            pass
        return jsonify(data)

    @app.get("/api/accounts/<int:acc_id>/change-email-log")
    def api_account_change_email_log(acc_id: int):
        """读取账号最近一次邮箱换绑日志。"""
        from core import email_change_service
        acc = db.get_account(acc_id)
        if not acc:
            return jsonify({"ok": False, "error": "账号不存在"}), 404
        data = _read_log_tail(
            email_change_service.log_path(acc_id), max_bytes=80_000,
            running_fn=lambda: email_change_service.is_running(acc_id),
        )
        data["account_id"] = acc_id
        data["email"] = acc.get("email")
        data["running"] = bool(data.get("running") or str(acc.get("email_change_status") or "") in {"queued", "running"})
        return jsonify(data)

    # ----------------------------------------------------------
    # 注册任务
    # ----------------------------------------------------------
    @app.get("/api/jobs")
    def api_jobs():
        limit = request.args.get("limit", default=100, type=int)
        paged = str(request.args.get("paged", default="") or "").lower() in {"1", "true", "yes"}
        page_arg = request.args.get("page", default=None, type=int)
        page_size_arg = request.args.get("page_size", default=None, type=int)
        from config import email as _email_cfg
        manual_otp_required = not bool(getattr(_email_cfg, "USE_EMAIL_SERVICE", True))
        if paged or page_arg is not None or page_size_arg is not None:
            page = max(1, int(page_arg or 1))
            page_size = max(1, min(500, int(page_size_arg or limit or 50)))
            result = db.list_jobs_page(
                limit=page_size, offset=(page - 1) * page_size
            )
            rows = result.get("items") or []
            for row in rows:
                row["manual_otp_required"] = manual_otp_required
                row.update(svc.get_retry_info(row))
            result.update({"ok": True, "page": page, "page_size": page_size})
            result["items"] = [_compact_job_for_list(r) for r in rows]
            result["status_counts"] = db.job_status_counts()
            result["compact"] = True
            return jsonify(result)
        rows = db.list_jobs(limit=max(1, int(limit or 1)))
        for row in rows:
            row["manual_otp_required"] = manual_otp_required
            row.update(svc.get_retry_info(row))
        return jsonify(rows)

    @app.post("/api/jobs")
    def api_jobs_create():
        """启动批量注册：body {count, workers}。"""
        data = request.get_json(silent=True) or {}
        try:
            count = int(data.get("count", 1))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "count 非法"}), 400
        if count < 1 or count > 200:
            return jsonify({"ok": False, "error": "count 需在 1~200 之间"}), 400

        # workers 控制本次新提交任务使用的线程池；若和上次不同，服务层会为新任务切换到新池。
        try:
            workers = max(1, min(16, int(data.get("workers", 3))))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 非法"}), 400

        # 提交前先确认池里有足够可用邮箱，给前端一个温和提示（不阻断）
        from config import email as _email_cfg
        from config import register as _register_cfg
        from core.email_provider import parse_email_sources
        if not bool(getattr(_email_cfg, "USE_EMAIL_SERVICE", True)):
            reg_email = str(getattr(_register_cfg, "REGISTER_EMAIL", "") or "").strip()
            if not reg_email:
                return jsonify({
                    "ok": False,
                    "error": "手动模式未配置 REGISTER_EMAIL。请到配置页填写「手动注册邮箱」，或开启自动取邮箱+收码。",
                }), 400
            if count > 1:
                return jsonify({
                    "ok": False,
                    "error": "手动模式建议每次只跑 1 个任务（同一 REGISTER_EMAIL）。请把数量设为 1。",
                }), 400
            jobs = svc.submit_registration(count=count, workers=workers)
            return jsonify({
                "ok": True,
                "submitted": len(jobs),
                "jobs": jobs,
                "warning": f"手动 OTP 模式：将使用 {reg_email}；验证码请在任务页提交",
                "workers": workers,
            })
        sources = parse_email_sources(_email_cfg.EMAIL_SOURCE)
        if "gptmail" in sources:
            api_key = str(getattr(_email_cfg, "GPTMAIL_API_KEY", "") or "").strip()
            if not api_key:
                return jsonify({
                    "ok": False,
                    "error": "已选择 gptmail 邮箱来源，请填写 GPTMail API Key（配置 → 邮箱 / OTP）。",
                }), 400
        if "cloudflare" in sources:
            api_base = str(getattr(_email_cfg, "CLOUDFLARE_API_BASE", "") or "").strip()
            if not api_base:
                return jsonify({
                    "ok": False,
                    "error": "已选择 cloudflare 邮箱来源，请填写 Cloudflare API 地址（配置 → 邮箱 / OTP）。",
                }), 400
            auth_mode = str(getattr(_email_cfg, "CLOUDFLARE_AUTH_MODE", "none") or "none").strip().lower()
            accounts_path = str(getattr(_email_cfg, "CLOUDFLARE_PATH_ACCOUNTS", "/api/new_address") or "").strip().lower()
            api_key = str(getattr(_email_cfg, "CLOUDFLARE_API_KEY", "") or "").strip()
            needs_key = auth_mode in ("x-admin-auth", "bearer", "x-api-key", "query-key") or accounts_path.rstrip("/").endswith("/admin/new_address")
            if needs_key and not api_key:
                return jsonify({
                    "ok": False,
                    "error": "Cloudflare admin/鉴权模式需要填写 Cloudflare API Key（配置 → 邮箱 / OTP）。",
                }), 400
        if "mailnest" in sources:
            api_key = str(getattr(_email_cfg, "MAIL_NEST_API_KEY", "") or "").strip()
            project_code = str(getattr(_email_cfg, "MAIL_NEST_PROJECT_CODE", "") or "").strip()
            if not api_key:
                return jsonify({
                    "ok": False,
                    "error": "已选择 mailnest 邮箱来源，请填写 MailNest API Key（配置 → 邮箱 / OTP）。",
                }), 400
            if not project_code:
                return jsonify({
                    "ok": False,
                    "error": "已选择 mailnest 邮箱来源，请填写 MailNest 项目代码（配置 → 邮箱 / OTP）。",
                }), 400
        if "cloudmail" in sources:
            api_base = str(getattr(_email_cfg, "CLOUDMAIL_API_BASE", "") or "").strip()
            token = str(getattr(_email_cfg, "CLOUDMAIL_AUTH_TOKEN", "") or "").strip()
            if not api_base:
                return jsonify({
                    "ok": False,
                    "error": "已选择 cloudmail 邮箱来源，请填写 CloudMail API 地址（配置 → 邮箱 / OTP）。",
                }), 400
            if not token:
                return jsonify({
                    "ok": False,
                    "error": "已选择 cloudmail 邮箱来源，请填写 CloudMail Token（配置 → 邮箱 / OTP）。",
                }), 400
        if "remail" in sources:
            api_base = str(getattr(_email_cfg, "REMAIL_API_BASE", "") or "").strip()
            api_key = str(getattr(_email_cfg, "REMAIL_API_KEY", "") or "").strip()
            try:
                project_id = int(getattr(_email_cfg, "REMAIL_PROJECT_ID", 2) or 0)
            except (TypeError, ValueError):
                project_id = 0
            suffix = str(getattr(_email_cfg, "REMAIL_EMAIL_SUFFIX", "") or "").strip()
            service_mode = str(getattr(_email_cfg, "REMAIL_SERVICE_MODE", "purchase") or "purchase").strip().lower()
            if not api_base:
                return jsonify({
                    "ok": False,
                    "error": "已选择 remail 邮箱来源，请填写 Remail API 地址（配置 → 邮箱 / OTP）。",
                }), 400
            if not api_key:
                return jsonify({
                    "ok": False,
                    "error": "已选择 remail 邮箱来源，请填写 Remail API Key（配置 → 邮箱 / OTP）。",
                }), 400
            if project_id <= 0:
                return jsonify({
                    "ok": False,
                    "error": "已选择 remail 邮箱来源，请填写 Remail 项目 ID（配置 → 邮箱 / OTP）。",
                }), 400
            if not suffix:
                return jsonify({
                    "ok": False,
                    "error": "已选择 remail 邮箱来源，请填写 Remail 邮箱后缀（例如 outlook.com）。",
                }), 400
            if service_mode not in ("code", "purchase"):
                return jsonify({
                    "ok": False,
                    "error": "Remail 服务模式只能填写 code 或 purchase（配置 → 邮箱 / OTP）。",
                }), 400
        if "gptmail" in sources or "mailnest" in sources or "cloudmail" in sources or "remail" in sources or "cloudflare" in sources:
            # 临时邮箱在任务开始时动态生成，不需要本地邮箱池容量提示。
            warning = ""
        elif "cloudflare_domain" in sources:
            pool = db.domain_email_pool_summary()
            warning = ""
            if sources == ["cloudflare_domain"] and pool.get("available", 0) < count:
                warning = f"域名邮箱池仅 {pool.get('available', 0)} 个可用，少于任务数 {count}，不足的会自动生成"
        elif sources == ["generic_api"]:
            pool = db.generic_api_email_pool_summary()
            warning = ""
            if pool.get("available", 0) < count:
                warning = f"通用 API 邮箱池仅 {pool.get('available', 0)} 个可用，少于任务数 {count}，不足的会失败"
        elif sources == ["imap"]:
            pool = db.imap_email_pool_summary()
            warning = ""
            if pool.get("available", 0) < count:
                warning = f"通用 IMAP 邮箱池仅 {pool.get('available', 0)} 个可用，少于任务数 {count}，不足的会失败"
        elif len(sources) > 1:
            available = 0
            if "outlook" in sources:
                available += db.outlook_pool_summary().get("available", 0)
            if "generic_api" in sources:
                available += db.generic_api_email_pool_summary().get("available", 0)
            if "imap" in sources:
                available += db.imap_email_pool_summary().get("available", 0)
            warning = ""
            if available < count:
                warning = f"多个邮箱池合计仅 {available} 个可用，少于任务数 {count}，不足的会失败"
        else:
            pool = db.outlook_pool_summary()
            warning = ""
            if pool.get("available", 0) < count:
                warning = f"可用邮箱仅 {pool.get('available', 0)} 个，少于任务数 {count}，不足的会失败"
        jobs = svc.submit_registration(count=count, workers=workers)
        return jsonify({"ok": True, "submitted": len(jobs), "jobs": jobs, "warning": warning, "workers": workers})

    @app.get("/api/manual-otp/waiting")
    def api_manual_otp_waiting():
        """列出当前正在等待手动验证码的邮箱。"""
        from core.manual_otp import list_waiting
        return jsonify({"ok": True, "waiting": list_waiting()})

    @app.post("/api/manual-otp")
    def api_manual_otp_submit():
        """提交手动邮箱验证码。Body: {email, code} 或 {job_id, code}。"""
        from core.manual_otp import submit_manual_otp
        data = request.get_json(silent=True) or {}
        code = (data.get("code") or data.get("otp") or "").strip()
        email = (data.get("email") or "").strip()
        job_id = data.get("job_id")
        if not email and job_id is not None:
            job = db.get_job(int(job_id))
            email = (job or {}).get("email") or ""
        if not email:
            return jsonify({"ok": False, "error": "email/job_id 缺失"}), 400
        try:
            result = submit_manual_otp(email, code)
            return jsonify(result)
        except Exception as exc:
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    @app.post("/api/jobs/cancel-pending")
    def api_jobs_cancel_pending():
        """取消所有还在排队（status=pending）的任务。已在 running 的不动。"""
        cancelled = svc.cancel_pending_jobs()
        return jsonify({"ok": True, "cancelled": cancelled})

    @app.post("/api/jobs/<int:job_id>/stop")
    def api_job_stop(job_id: int):
        """手动停止单个注册任务。pending 取消；running 发送停止信号。"""
        result = svc.request_stop_job(job_id)
        if not result.get("ok"):
            return jsonify({"ok": False, "error": result.get("error") or "停止失败"}), int(result.get("status") or 400)
        return jsonify(result)

    @app.post("/api/jobs/<int:job_id>/retry")
    def api_job_retry(job_id: int):
        """重试失败/停止/取消任务；服务端自动判断完整注册或 Codex 补跑。"""
        data = request.get_json(silent=True) or {}
        try:
            workers = max(1, min(16, int(data.get("workers", svc.get_executor_workers()))))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 非法"}), 400
        result = svc.retry_job(job_id, workers=workers)
        if not result.get("ok"):
            return jsonify(result), int(result.get("status") or 400)
        return jsonify(result)

    @app.post("/api/jobs/retry-bulk")
    def api_jobs_retry_bulk():
        """批量重试任务；不支持项逐条跳过并返回原因。"""
        data = request.get_json(silent=True) or {}
        job_ids = data.get("job_ids") or data.get("ids") or []
        if not isinstance(job_ids, list) or not job_ids:
            return jsonify({"ok": False, "error": "job_ids 必须是非空数组"}), 400
        if len(job_ids) > 500:
            return jsonify({"ok": False, "error": "单次最多重试 500 个任务"}), 400
        try:
            workers = max(1, min(16, int(data.get("workers", svc.get_executor_workers()))))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "workers 非法"}), 400

        started: list[dict] = []
        reused: list[dict] = []
        skipped: list[dict] = []
        seen: set[int] = set()
        for raw_id in job_ids:
            try:
                one_id = int(raw_id)
            except (TypeError, ValueError):
                skipped.append({"id": raw_id, "reason": "ID 非法"})
                continue
            if one_id in seen:
                continue
            seen.add(one_id)
            result = svc.retry_job(one_id, workers=workers)
            if not result.get("ok"):
                skipped.append({"id": one_id, "reason": result.get("error") or "不能重试"})
            elif result.get("reused"):
                reused.append(result)
            else:
                started.append(result)
        return jsonify({
            "ok": True,
            "started": started,
            "started_count": len(started),
            "reused": reused,
            "reused_count": len(reused),
            "skipped": skipped,
            "skipped_count": len(skipped),
            "workers": workers,
        })

    @app.post("/api/jobs/<int:job_id>/delete")
    def api_job_delete(job_id: int):
        """删除一个任务记录。运行中的任务不允许删除；排队任务删除后执行前会自动跳过。"""
        job = db.get_job(job_id)
        if not job:
            return jsonify({"ok": False, "error": "任务不存在"}), 404
        if job.get("status") in ("running", "stopping"):
            return jsonify({"ok": False, "error": "运行中的任务不能删除，请等待完成后再删"}), 409
        deleted = db.delete_job(job_id, delete_log=True, allow_running=False)
        if not deleted:
            return jsonify({"ok": False, "error": "任务不存在或已开始运行"}), 409
        return jsonify({"ok": True, "deleted": deleted})

    @app.post("/api/jobs/delete-bulk")
    def api_jobs_delete_bulk():
        """批量删除任务记录。running 任务跳过，其它任务删除记录和日志。"""
        data = request.get_json(silent=True) or {}
        job_ids = data.get("job_ids") or data.get("ids") or []
        if not isinstance(job_ids, list) or not job_ids:
            return jsonify({"ok": False, "error": "job_ids 必须是非空数组"}), 400
        if len(job_ids) > 1000:
            return jsonify({"ok": False, "error": "单次最多删除 1000 个任务"}), 400

        deleted: list[int] = []
        skipped: list[dict] = []
        seen: set[int] = set()
        for raw_id in job_ids:
            try:
                job_id = int(raw_id)
            except (TypeError, ValueError):
                skipped.append({"id": raw_id, "reason": "ID 非法"})
                continue
            if job_id in seen:
                continue
            seen.add(job_id)

            job = db.get_job(job_id)
            if not job:
                skipped.append({"id": job_id, "reason": "任务不存在"})
                continue
            if job.get("status") in ("running", "stopping"):
                skipped.append({"id": job_id, "reason": "运行中，不能删除"})
                continue
            if db.delete_job(job_id, delete_log=True, allow_running=False):
                deleted.append(job_id)
            else:
                skipped.append({"id": job_id, "reason": "任务不存在或已开始运行"})

        return jsonify({"ok": True, "deleted": deleted, "deleted_count": len(deleted), "skipped": skipped})

    @app.get("/api/jobs/<int:job_id>/log")
    def api_job_log(job_id: int):
        job = db.get_job(job_id)
        if not job:
            return jsonify({"ok": False, "error": "任务不存在"}), 404
        return jsonify({
            "ok": True,
            "job": job,
            "log": svc.read_job_log(job_id),
        })

    # ----------------------------------------------------------
    # RoxyBrowser 辅助接口
    # ----------------------------------------------------------
    @app.get("/api/roxy/workspaces")
    def api_roxy_workspaces():
        try:
            from core.roxybrowser_client import RoxyBrowserClient
            result = RoxyBrowserClient().list_workspaces()
            return jsonify(result)
        except Exception as exc:
            logger.exception("获取 Roxy 团队/工作区失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500

    # ----------------------------------------------------------
    # 配置读写
    # ----------------------------------------------------------
    @app.get("/api/config")
    def api_config_get():
        return jsonify(config_editor.get_config())

    @app.post("/api/cloudmail/gen-token")
    def api_cloudmail_gen_token():
        """手动生成 CloudMail Authorization Token，并把本次填写的 CloudMail 配置一并写入 .env。"""
        data = request.get_json(silent=True) or {}
        try:
            from core.cloudmail_client import gen_token
            from config.env_loader import write_env_values

            api_base = (data.get("api_base") or "").strip()
            admin_email = (data.get("email") or data.get("admin_email") or "").strip()
            password = (data.get("password") or "").strip()
            path = (data.get("path") or "/api/public/genToken").strip() or "/api/public/genToken"
            token = gen_token(
                email=admin_email,
                password=password,
                path=path,
                base_url=api_base,
            )
            updates = {"CLOUDMAIL_AUTH_TOKEN": token}
            # 生成 Token 时用户通常尚未点“保存配置”；这里同步保存本次填写的字段，
            # 避免 loadConfig() 后 API 地址/账号/密码被旧 .env 值覆盖。
            if api_base:
                updates["CLOUDMAIL_API_BASE"] = api_base
            if admin_email:
                updates["CLOUDMAIL_ADMIN_EMAIL"] = admin_email
            if password:
                updates["CLOUDMAIL_PASSWORD"] = password
            if path:
                updates["CLOUDMAIL_TOKEN_PATH"] = path
            written = write_env_values(updates)
            try:
                import config as _config_pkg
                _config_pkg.reload_all()
            except Exception:
                logger.exception("CloudMail Token 写入后热加载失败")
            return jsonify({
                "ok": True,
                "token": token,
                "written": written,
                "message": "CloudMail Token 已生成，且当前 CloudMail 配置已保存",
            })
        except Exception as exc:
            logger.exception("生成 CloudMail Token 失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    @app.post("/api/cloudmail/domains")
    def api_cloudmail_domains():
        """从 CloudMail 平台获取域名列表，并可写入 .env 作为本地缓存。"""
        data = request.get_json(silent=True) or {}
        try:
            from core.cloudmail_client import fetch_domains
            from config.env_loader import write_env_values

            updates = {}
            api_base = (data.get("api_base") or "").strip()
            admin_email = (data.get("email") or data.get("admin_email") or "").strip()
            password = (data.get("password") or "").strip()
            token = (data.get("token") or "").strip()
            if api_base:
                updates["CLOUDMAIL_API_BASE"] = api_base
            if admin_email:
                updates["CLOUDMAIL_ADMIN_EMAIL"] = admin_email
            if password:
                updates["CLOUDMAIL_PASSWORD"] = password
            if token:
                updates["CLOUDMAIL_AUTH_TOKEN"] = token
            if updates:
                write_env_values(updates)
                import config as _config_pkg
                _config_pkg.reload_all()

            domains = fetch_domains(force=True)
            written = write_env_values({"CLOUDMAIL_DOMAINS": "\n".join(domains)})
            try:
                import config as _config_pkg
                _config_pkg.reload_all()
            except Exception:
                logger.exception("CloudMail 域名写入后热加载失败")
            return jsonify({
                "ok": True,
                "domains": domains,
                "count": len(domains),
                "written": written,
                "message": f"已获取 {len(domains)} 个 CloudMail 可用域名并保存",
            })
        except Exception as exc:
            logger.exception("获取 CloudMail 域名失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400

    @app.post("/api/config")
    def api_config_set():
        data = request.get_json(silent=True) or {}
        updates = data.get("updates") if isinstance(data.get("updates"), dict) else data
        if not isinstance(updates, dict) or not updates:
            return jsonify({"ok": False, "error": "无更新内容"}), 400
        try:
            result = config_editor.update_config(updates)
        except ValueError as exc:
            # 表单数字为空/非法时返回可读的 400，而不是把底层 TypeError
            # 或 ValueError 当成服务器故障；空数字由 config_editor 写空 .env，
            # 这里主要处理非数字文本、NaN 等非法输入。
            logger.warning("配置值无效: %s", exc)
            return jsonify({"ok": False, "error": f"ValueError: {exc}"}), 400
        except Exception as exc:
            logger.exception("配置写入失败")
            return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 500

        # 写盘成功后立即热加载所有 config 子模块，让运行时代码看到新值。
        reload_ok = True
        reload_err = ""
        try:
            import config as _config_pkg
            _config_pkg.reload_all()
        except Exception as exc:
            reload_ok = False
            reload_err = f"{type(exc).__name__}: {exc}"
            logger.exception("配置热加载失败")

        return jsonify({
            "ok": True,
            "updated": result["updated"],
            "ignored": result["ignored"],
            "reloaded": reload_ok,
            "note": (
                "✅ 已保存并热加载，新值立即生效"
                if reload_ok
                else f"⚠️ 已写入文件但热加载失败（{reload_err}），需重启 Web 服务才能生效"
            ),
        })

    return app
