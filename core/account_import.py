# -*- coding: utf-8 -*-
"""解析 WebUI 中“已有账号”批量导入的文本格式。"""
import re


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_FIELD_SEPARATOR_RE = re.compile(r"-{3,}")


def parse_existing_account_text(text: str, *, max_lines: int = 5000) -> tuple[list[dict], list[dict]]:
    """解析 ``邮箱---密码---2FA---AT``，返回 (有效记录, 无效行详情)。

    兼容四个短横线的旧粘贴习惯；只按前三个分隔符切分，因此 AT 中的
    普通短横线不会影响解析。错误详情只返回行号和原因，避免把凭据回显到页面。
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
            errors.append({"line": line_number, "reason": "格式错误，需要邮箱---密码---2FA---AT 四段"})
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
