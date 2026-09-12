# -*- coding: utf-8 -*-
"""已注册账号导入的解析与格式归一化。"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse


MAX_ACCOUNT_IMPORT_BYTES = 5 * 1024 * 1024
MAX_ACCOUNT_IMPORT_RECORDS = 5000


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _first(*values: Any) -> str:
    for value in values:
        result = _text(value)
        if result:
            return result
    return ""


def _strip_bearer(value: Any) -> str:
    result = _text(value)
    if result.lower().startswith("bearer "):
        return result[7:].strip()
    return result


def _valid_email(value: str) -> bool:
    return bool(value) and "@" in value and not any(ch.isspace() for ch in value)


def _looks_like_http_url(value: Any) -> bool:
    """Return whether a value looks like an absolute HTTP(S) URL."""
    try:
        parsed = urlparse(_text(value))
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _parse_extra(raw: dict, nested: dict) -> dict:
    value = raw.get("extra")
    if not isinstance(value, dict):
        value = nested.get("extra")
    if not isinstance(value, dict):
        value = raw.get("extra_json")
    if not isinstance(value, dict):
        value = nested.get("extra_json")
    if isinstance(value, str) and value.strip():
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = {}
    return dict(value) if isinstance(value, dict) else {}


def _normalise_record(raw: Any, index: int) -> tuple[dict | None, dict | None]:
    if not isinstance(raw, dict):
        return None, {"index": index, "reason": "记录必须是 JSON 对象"}

    nested = _as_dict(raw.get("row"))
    extra = _parse_extra(raw, nested)
    extra_user = _as_dict(extra.get("user"))
    extra_account = _as_dict(extra.get("account"))

    email = _first(
        raw.get("email"), nested.get("email"), raw.get("mail"), nested.get("mail"),
        extra_user.get("email"),
    )
    token = _strip_bearer(_first(
        raw.get("access_token"), raw.get("accessToken"), raw.get("token"),
        nested.get("access_token"), nested.get("accessToken"), nested.get("token"),
    ))
    if not _valid_email(email):
        return None, {"index": index, "email": email, "reason": "缺少有效邮箱"}
    if not token:
        return None, {"index": index, "email": email, "reason": "缺少 access_token/token"}

    result: dict[str, Any] = {"email": email, "access_token": token}
    totp = _first(
        raw.get("totp_secret"), raw.get("totp"), raw.get("totpSecret"),
        nested.get("totp_secret"), nested.get("totp"), nested.get("totpSecret"),
    )
    if totp:
        result["totp_secret"] = totp

    metadata_sources = (raw, nested)
    aliases = {
        # 邮箱素材字段：扩展文本格式为
        # email----ChatGPT_password----2OTP_secret----code_url----access_token。
        "password": ("password", "passwd"),
        # 明确的 ChatGPT 登录密码；普通 password 字段仍兼容邮箱素材密码。
        "registration_password": ("registration_password", "registrationPassword", "openai_password", "openaiPassword", "chatgpt_password", "chatgptPassword", "login_password", "loginPassword"),
        "client_id": ("client_id", "clientId"),
        "refresh_token": ("refresh_token", "refreshToken"),
        "code_url": ("code_url", "codeUrl", "mail_url", "mailUrl", "url"),
        "account_line_format": ("account_line_format", "accountLineFormat"),
        "user_id": ("user_id", "userId"),
        "user_name": ("user_name", "userName", "name"),
        "plan_type": ("plan_type", "planType"),
        "expires_at": ("expires_at", "expiresAt", "expires"),
        "device_id": ("device_id", "deviceId"),
        "proxy_used": ("proxy_used", "proxyUsed"),
        "email_source": ("email_source", "emailSource", "source"),
        "account_id": ("account_id", "accountId"),
        "current_plan_type": ("current_plan_type", "currentPlanType"),
        "subscription_plan": ("subscription_plan", "subscriptionPlan"),
        "plus_trial_eligible": ("plus_trial_eligible", "plusTrialEligible"),
        "codex_status": ("codex_status", "codexStatus"),
        "codex_error": ("codex_error", "codexError"),
        "note": ("note",),
        "archived": ("archived",),
        "created_at": ("created_at", "createdAt", "saved_at", "savedAt"),
        "updated_at": ("updated_at", "updatedAt"),
    }
    for target, names in aliases.items():
        value: Any = None
        found = False
        for source in metadata_sources:
            for name in names:
                if name in source and source.get(name) is not None:
                    value = source.get(name)
                    found = True
                    break
            if found:
                break
        if not found:
            if target == "user_id":
                value = extra_user.get("id")
            elif target == "user_name":
                value = extra_user.get("name")
            elif target == "plan_type":
                value = extra_account.get("planType") or extra_account.get("plan_type")
            elif target == "expires_at":
                value = extra.get("expires")
            elif target == "device_id":
                value = extra.get("device_id")
        if value is not None and value != "":
            result[target] = value

    # 仅接收明确的“邮箱素材”字段，不能把 copy_line 当作 material_line，
    # 否则生成的整行会重复 access token。
    material_line = _first(
        raw.get("material_line"), raw.get("original_email_line"),
        nested.get("material_line"), nested.get("original_email_line"),
    )
    if material_line:
        result["material_line"] = material_line
    # Generic API account exports use ``password`` for the ChatGPT login
    # password; unlike Outlook rows it is safe to promote it to the explicit
    # field used by the liveness password/TOTP flow.
    if (
        result.get("email_source") == "generic_api"
        and result.get("password")
        and not result.get("registration_password")
    ):
        result["registration_password"] = result["password"]
    if extra:
        result["extra"] = extra
    return result, None


def parse_account_records(records: Any, *, max_records: int = MAX_ACCOUNT_IMPORT_RECORDS) -> tuple[list[dict], list[dict]]:
    """归一化 JSON 记录，返回 (有效记录, 无敏感值的错误列表)。"""
    if not isinstance(records, list):
        return [], [{"index": 0, "reason": "records 必须是数组"}]
    if len(records) > max_records:
        return [], [{"index": max_records, "reason": f"单次最多导入 {max_records} 条记录"}]
    valid: list[dict] = []
    errors: list[dict] = []
    for index, raw in enumerate(records):
        record, error = _normalise_record(raw, index)
        if record is not None:
            valid.append(record)
        if error is not None:
            errors.append(error)
    return valid, errors


def _json_items(payload: Any) -> tuple[list[Any], str | None]:
    if isinstance(payload, list):
        return payload, None
    if isinstance(payload, dict):
        for key in ("accounts", "registered_accounts", "credentials", "records", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value, None
        if any(key in payload for key in ("email", "mail", "access_token", "accessToken", "token")):
            return [payload], None
    return [], "JSON 须为账号数组、单个账号对象或包含 accounts/credentials/records 的对象"


def parse_account_json(text: str, *, max_records: int = MAX_ACCOUNT_IMPORT_RECORDS) -> tuple[list[dict], list[dict]]:
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as exc:
        return [], [{"index": 0, "reason": f"JSON 解析失败：{type(exc).__name__}"}]
    items, root_error = _json_items(payload)
    if root_error:
        return [], [{"index": 0, "reason": root_error}]
    return parse_account_records(items, max_records=max_records)


def _split_line(line: str) -> list[str]:
    if "----" in line:
        return [part.strip() for part in line.split("----")]
    if "====" in line:
        return [part.strip() for part in line.split("====")]
    if "\t" in line:
        return [part.strip() for part in line.split("\t")]
    return [line.strip()]


def parse_account_text(text: str, *, max_records: int = MAX_ACCOUNT_IMPORT_RECORDS) -> tuple[list[dict], list[dict]]:
    """解析文本账号行。

    支持的常见形式：

    * ``email----access_token[----totp_secret]``
    * ``email----code_url----access_token[----totp_secret]``
    * ``email----ChatGPT_password----2OTP_secret----code_url----access_token``
    * Outlook 素材整行 ``email----password----clientId----refreshToken----access_token``

    五段通用 API 格式中，第 2 段是 ChatGPT 账号密码，第 3 段是 2OTP/TOTP
    密钥，第 4 段是取码地址，最后一段是 access token；取码地址会保存到通用
    API 邮箱池，方便后续查活自动收取 OTP。
    """
    valid: list[dict] = []
    errors: list[dict] = []
    meaningful = 0
    for line_number, raw_line in enumerate(str(text or "").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        meaningful += 1
        if meaningful > max_records:
            errors.append({"line": line_number, "reason": f"单次最多导入 {max_records} 条记录"})
            break
        parts = _split_line(line)
        if len(parts) < 2:
            errors.append({"line": line_number, "reason": "格式错误：至少需要 email----access_token"})
            continue

        email = parts[0]
        token_index = 1
        totp = ""
        material_line = email
        fields: dict[str, str] = {}

        if len(parts) >= 5:
            # 通用 API 账号：邮箱、ChatGPT 密码、2OTP/TOTP 密钥、取码地址、AT。
            # Outlook 旧整行没有 URL，仍按 email/password/clientId/refreshToken/AT
            # 兼容解析。
            token_index = 4
            material_line = "----".join(parts[:4])
            fields["password"] = parts[1]
            if _looks_like_http_url(parts[3]):
                fields["registration_password"] = parts[1]
                fields["totp_secret"] = parts[2]
                fields["code_url"] = parts[3]
                fields["email_source"] = "generic_api"
                fields["account_line_format"] = "chatgpt_api"
            else:
                fields["client_id"] = parts[2]
                fields["refresh_token"] = parts[3]
                fields["email_source"] = "outlook"
            # 兼容旧版六段导入行：仅当新格式第三段为空时使用末段 TOTP。
            if len(parts) >= 6 and not fields.get("totp_secret"):
                totp = parts[5]
        elif len(parts) == 4 and _looks_like_http_url(parts[1]):
            # 通用 API 账号：email----code_url----access_token----totp_secret。
            fields["code_url"] = parts[1]
            fields["email_source"] = "generic_api"
            token_index = 2
            totp = parts[3]
            material_line = "----".join(parts[:2])
        elif len(parts) == 3 and _looks_like_http_url(parts[1]):
            # 通用 API 账号：email----code_url----access_token。
            fields["code_url"] = parts[1]
            fields["email_source"] = "generic_api"
            token_index = 2
            material_line = "----".join(parts[:2])
        elif len(parts) == 3:
            # 简写：email----access_token----totp_secret。
            totp = parts[2]
        elif len(parts) == 4:
            # 兼容旧的 email----token----totp----source 写法；source 不参与账号凭证。
            totp = parts[2]

        token = _strip_bearer(parts[token_index] if token_index < len(parts) else "")
        record = {"email": email, "access_token": token, "material_line": material_line, **fields}
        if totp and not record.get("totp_secret"):
            record["totp_secret"] = totp
        normalised, error = _normalise_record(record, line_number - 1)
        if normalised is not None:
            valid.append(normalised)
        if error is not None:
            error["line"] = line_number
            error.pop("index", None)
            errors.append(error)
    return valid, errors


def parse_account_content(
    content: str | bytes,
    *,
    format: str = "auto",
    filename: str = "",
    max_records: int = MAX_ACCOUNT_IMPORT_RECORDS,
) -> tuple[list[dict], list[dict], str]:
    """解析 JSON/TXT 导入内容，返回 (记录, 错误, 实际格式)。"""
    if isinstance(content, bytes):
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            return [], [{"index": 0, "reason": "文件必须使用 UTF-8 编码"}], "text"
    else:
        text = str(content or "")
    selected = _text(format).lower() or "auto"
    if selected not in {"auto", "json", "text"}:
        return [], [{"index": 0, "reason": "format 仅支持 auto/json/text"}], selected
    if selected == "auto":
        suffix = str(filename or "").lower().rsplit(".", 1)[-1] if "." in str(filename or "") else ""
        stripped = text.lstrip()
        selected = "json" if suffix == "json" or stripped.startswith("[") or stripped.startswith("{") else "text"
    if selected == "json":
        records, errors = parse_account_json(text, max_records=max_records)
    else:
        records, errors = parse_account_text(text, max_records=max_records)
    return records, errors, selected
