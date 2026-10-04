# -*- coding: utf-8 -*-
"""解析已有账号导入文本，并使用 AT 补全最新用户名。"""
import math
import re
from concurrent.futures import ThreadPoolExecutor

from core import db
from core.chatgpt_plan import normalize_token, open_plan_check_proxy, resolve_plan_check_route
from core.session import BrowserSession, close_browser_session


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# 分隔符兼容 ``--``、``---``、``----`` 三种样式；量词贪婪，连续的短横线会
# 视为同一个分隔符（``----`` 不会拆成两个 ``--``）。
_FIELD_SEPARATOR_RE = re.compile(r"-{2,}")


def parse_existing_account_text(text: str, *, max_lines: int = 5000) -> tuple[list[dict], list[dict]]:
    """解析 ``邮箱--密码--2FA--AT``，返回 (有效记录, 无效行详情)。

    分隔符兼容 ``--``、``---``、``----`` 三种样式；只按前三个分隔符切分，
    因此 AT 中的普通短横线不会影响解析。错误详情只返回行号和原因，避免把
    凭据回显到页面。
    """
    records: list[dict] = []
    errors: list[dict] = []
    seen_emails: set[str] = set()
    lines = str(text or "").splitlines()

    for line_number, raw_line in enumerate(lines, 1):
        line = raw_line.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        if len(records) + len(errors) >= max_lines:
            errors.append({"line": line_number, "reason": f"单次最多导入 {max_lines} 行"})
            break

        parts = [part.strip() for part in _FIELD_SEPARATOR_RE.split(line, maxsplit=3)]
        if len(parts) != 4:
            errors.append({
                "line": line_number,
                "reason": "格式错误，需要邮箱、密码、2FA、AT 四段（分隔符可用 --、--- 或 ----）",
            })
            continue

        email, password, totp_secret, access_token = parts
        if not email or not password or not totp_secret or not access_token:
            errors.append({"line": line_number, "reason": "邮箱、密码、2FA 和 AT 都不能为空"})
            continue
        if not _EMAIL_RE.fullmatch(email):
            errors.append({"line": line_number, "reason": "邮箱格式无效"})
            continue

        email_key = email.casefold()
        if email_key in seen_emails:
            errors.append({"line": line_number, "email": email, "reason": "本次内容中邮箱重复"})
            continue
        seen_emails.add(email_key)
        records.append({
            "email": email,
            "password": password,
            "totp_secret": totp_secret.replace(" ", ""),
            "access_token": access_token,
        })

    return records, errors


_PROFILE_REQUEST_ERROR = "用户名查询请求失败，请稍后重试"


def fetch_account_user_name(access_token: str, *, email: str = "", timeout: float = 15.0) -> dict:
    """使用 AT 查询当前用户名；错误只返回固定消息，不回显凭据或响应。"""
    token = normalize_token(access_token) if isinstance(access_token, str) else ""
    if not token:
        return {"ok": False, "error": "账号缺少 access_token"}
    try:
        timeout_seconds = float(timeout)
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError
        timeout_seconds = max(1.0, min(60.0, timeout_seconds))
    except (TypeError, ValueError, OverflowError):
        return {"ok": False, "error": "用户名查询超时配置无效"}

    session = None
    relay = None
    try:
        route = resolve_plan_check_route()
        effective_proxy, relay = open_plan_check_proxy(route, route["proxy"], timeout=timeout_seconds)
        session = BrowserSession(proxy=effective_proxy, detect_exit_geo=False)
        headers = session.get_chatgpt_headers(referer="https://chatgpt.com/")
        headers.pop("content-type", None)
        headers["authorization"] = f"Bearer {token}"
        response = session.get(
            "https://chatgpt.com/backend-api/me", headers=headers,
            timeout=timeout_seconds, allow_redirects=False,
        )
        status = int(response.status_code)
        if not 200 <= status < 300:
            error = {
                401: "AT 已过期或无效，请刷新后重试",
                403: "用户名查询被拒绝，请检查网络后重试",
                429: "用户名查询请求过于频繁，请稍后重试",
            }.get(status, _PROFILE_REQUEST_ERROR)
            return {"ok": False, "error": error}
        try:
            data = response.json()
        except Exception:
            return {"ok": False, "error": "用户名查询响应格式无效"}
        if not isinstance(data, dict):
            return {"ok": False, "error": "用户名查询响应格式无效"}
        expected_email = str(email or "").strip().casefold()
        if expected_email and "email" in data:
            actual_email = data["email"]
            if not isinstance(actual_email, str) or actual_email.strip().casefold() != expected_email:
                return {"ok": False, "error": "AT 对应邮箱与导入邮箱不一致"}
        name = data.get("name")
        if not isinstance(name, str) or not name.strip():
            return {"ok": False, "error": "用户名查询响应缺少有效姓名"}
        return {"ok": True, "user_name": name.strip()}
    except Exception:
        return {"ok": False, "error": _PROFILE_REQUEST_ERROR}
    finally:
        if session is not None:
            try:
                close_browser_session(session)
            except Exception:
                pass
        if relay is not None:
            try:
                relay.close()
            except Exception:
                pass


def import_existing_accounts(records: list[dict]) -> tuple[int, list[dict], list[dict]]:
    """对解析器验证过的唯一邮箱记录补充姓名，再交由 DB 原子导入。"""
    candidates = []
    skipped_details = []
    for record in records:
        email = record["email"]
        if db.get_account_by_email(email) is not None:
            skipped_details.append({"email": email, "reason": "账号已存在"})
        else:
            candidates.append(dict(record))
    if not candidates:
        return 0, skipped_details, []

    name_details = []
    # 网络查询在 DB 导入锁外执行，单条查询失败仍允许导入账户。
    with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as executor:
        futures = [executor.submit(fetch_account_user_name, record["access_token"], email=record["email"])
                   for record in candidates]
        for record, future in zip(candidates, futures):
            try:
                result = future.result()
            except Exception:
                result = {"ok": False, "error": _PROFILE_REQUEST_ERROR}
            if result.get("ok"):
                record["user_name"] = result["user_name"]
                name_details.append({"email": record["email"], "ok": True, "user_name": result["user_name"]})
            else:
                name_details.append({"email": record["email"], "ok": False, "error": result["error"]})

    inserted, db_skipped = db.import_existing_accounts(candidates)
    skipped_details.extend(db_skipped)
    skipped_emails = {str(item.get("email") or "").strip().casefold() for item in db_skipped}
    name_details = [item for item in name_details if item["email"].strip().casefold() not in skipped_emails]
    return inserted, skipped_details, name_details
