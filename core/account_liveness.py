# -*- coding: utf-8 -*-
"""已注册账号查活：优先复用已有 AT 预热后走密码/TOTP 或邮箱 OTP。"""
import logging
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from core import db
from core.session import BrowserSession
from core.codex_oauth import _account_registration_password, _account_totp_secret, _account_totp_code
from core.humanize import delay as human_delay
from core.chatgpt_auth import get_csrf_token, signin_openai
from core.openai_auth import (
    follow_authorize,
    send_email_otp,
    validate_email_otp,
    EmailOtpInvalidError,
    AccountUnusableError,
    detect_account_unusable_text,
)
from core.account_export import (
    _follow_reauth,
    _trigger_reauth,
    _validate_reauth_otp,
    fetch_session,
    follow_oauth_callback,
)
from core.email_provider import wait_for_otp

logger = logging.getLogger(__name__)
_LOG_DIR = Path(__file__).resolve().parent.parent / "注册日志"
_RUNNING: set[str] = set()
_RUNNING_LOCK = threading.Lock()

# 查活网络预检失败（403/429/代理/超时等）多为出口 IP 被 CF 标记或代理池抖动，
# 视为可换新 IP 重试；账号本身问题（废号/邮箱错误等）不重试。
_RETRYABLE_NETWORK_HINTS = (
    "403", "429", "502", "503", "504",
    "proxy", "socks", "timeout", "timed out",
    "connection", "closed", "reset",
)

_SESSION_FINGERPRINT_KEYS = {
    "device_id", "sentinel_sid", "oai_session_id", "auth_session_logging_id",
    "datadog_trace_id", "datadog_parent_id", "react_listening_key",
    "react_container_key", "react_resources_key",
}


def _is_retryable_network_error(exc: BaseException) -> bool:
    if isinstance(exc, AccountUnusableError):
        return False
    text = str(exc or "").lower()
    return any(h in text for h in _RETRYABLE_NETWORK_HINTS)


def _network_preflight_with_retry(email: str, proxy: str | None, max_attempts: int = 4) -> tuple[BrowserSession, str]:
    """CSRF → Signin 预检，保留 None/空字符串的代理语义并关闭失败会话。"""
    session: BrowserSession | None = None
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        if session is not None:
            try:
                session.session.close()
            except Exception:
                pass
        session = BrowserSession(proxy=proxy, fingerprint_seed=f"account:{email.lower()}")
        logger.info(
            "[查活] 会话创建完成：proxy=%s device_id=%s（网络预检第 %s/%s 次）",
            session.proxy or "配置随机/直连", session.device_id, attempt, max_attempts,
        )
        logger.info("[查活] 指纹摘要：%s", session.fingerprint_summary_text())
        try:
            csrf = get_csrf_token(session)
            authorize_url = signin_openai(session, csrf, email)
            return session, authorize_url
        except Exception as exc:
            last_exc = exc
            if attempt >= max_attempts or not _is_retryable_network_error(exc):
                try:
                    session.session.close()
                except Exception:
                    pass
                raise
            logger.warning(
                "[查活] 网络预检失败（%s/%s），重新建立会话重试：%s",
                attempt, max_attempts, str(exc)[:200],
            )
            time.sleep(2)
    raise RuntimeError(f"网络预检多次失败：{last_exc}")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe_fingerprint_for_account(session: BrowserSession) -> dict:
    fp = session.fingerprint_summary()
    return {k: v for k, v in fp.items() if k not in _SESSION_FINGERPRINT_KEYS}


def _safe_fingerprint_text_for_account(session: BrowserSession) -> str:
    fp = _safe_fingerprint_for_account(session)
    short = getattr(BrowserSession, "_short_value", lambda value, limit=80: str(value or "")[:limit])
    return " ".join([
        f"proxy={short(fp.get('proxy') or 'direct', 36)}",
        f"ua={short(fp.get('user_agent'), 72)}",
        f"lang={fp.get('accept_language')}",
        f"tz={fp.get('timezone_iana')}({fp.get('timezone_offset_minutes')})",
        f"screen={fp.get('screen_width')}x{fp.get('screen_height')}@{fp.get('device_pixel_ratio')}",
        f"cpu={fp.get('hardware_concurrency')}",
        f"mem={fp.get('device_memory')}",
        f"geo={fp.get('geo_country') or '?'}:{fp.get('geo_city') or '?'}",
    ])


def _extract_continue_url(result: dict | None) -> str:
    if not isinstance(result, dict):
        return ""
    page = result.get("page") or {}
    page = page if isinstance(page, dict) else {}
    return str(result.get("continue_url") or result.get("external_url") or result.get("url")
               or page.get("continue_url") or page.get("external_url") or page.get("url") or "").strip()


def _extract_factor_id(result: dict | None, continue_url: str) -> str:
    """Extract the MFA factor identifier from all known response shapes."""
    if isinstance(result, dict):
        page = result.get("page") or {}
        page = page if isinstance(page, dict) else {}
        candidates = [
            page.get("factor_id"),
            page.get("factorId"),
            result.get("factor_id"),
            result.get("factorId"),
        ]
        payload = page.get("payload")
        if isinstance(payload, dict):
            candidates.extend([
                payload.get("factor_id"), payload.get("factorId"),
                payload.get("id") if str(payload.get("type") or "").lower() in {"totp", "mfa"} else None,
            ])
        factors = page.get("factors") or result.get("factors")
        if isinstance(factors, list):
            for factor in factors:
                if isinstance(factor, dict) and str(factor.get("type") or "").lower() in {"totp", "mfa"}:
                    candidates.extend([factor.get("id"), factor.get("factor_id"), factor.get("factorId")])
        for candidate in candidates:
            value = str(candidate or "").strip()
            if value:
                return value
    try:
        parsed = urlparse(str(continue_url or ""))
        path_parts = [part for part in parsed.path.split("/") if part]
        for marker in ("mfa-challenge", "mfa", "factor"):
            if marker in path_parts:
                index = path_parts.index(marker)
                if index + 1 < len(path_parts):
                    return path_parts[index + 1]
        query = parse_qs(parsed.query)
        for key in ("factor_id", "factorId", "id"):
            if query.get(key) and str(query[key][0]).strip():
                return str(query[key][0]).strip()
    except Exception:
        pass
    return ""


def _is_mfa_challenge(result: dict | None, continue_url: str) -> bool:
    """Detect MFA without relying on one exact page type or URL spelling."""
    url = str(continue_url or "").lower()
    if any(marker in url for marker in ("mfa", "multi-factor", "multifactor", "two-factor", "two_factor", "totp")):
        return True
    if not isinstance(result, dict):
        return False
    page = result.get("page") if isinstance(result.get("page"), dict) else {}
    page_type = str(page.get("type") or result.get("type") or "").lower().replace("-", "_")
    if page_type in {"mfa", "mfa_challenge", "totp", "totp_challenge", "two_factor", "two_factor_challenge"}:
        return True
    # Keep the text fallback narrow: generic occurrences of ``mfa: false``
    # in an account/session payload must not send us down the MFA branch.
    text = json.dumps(result, ensure_ascii=False, separators=(",", ":")).lower()
    return any(marker in text for marker in (
        '"mfa_required":true', '"requires_mfa":true', '"factor_id":',
        '"factorid":', '"factor_type":"totp"', '"challenge_type":"totp"',
        '"challenge_type":"mfa"', 'multi-factor', 'multifactor',
        'two-factor', 'two_factor',
    ))


def _password_verify(session: BrowserSession, password: str) -> dict:
    from core.openai_auth import build_sentinel_header, request_sentinel_token
    sentinel_resp = request_sentinel_token(session, "password_verify")
    sentinel_header, so_header = build_sentinel_header(session, sentinel_resp, "password_verify")
    headers = session.get_auth_headers(referer="https://auth.openai.com/log-in/password")
    headers["openai-sentinel-token"] = sentinel_header
    if so_header:
        headers["openai-sentinel-so-token"] = so_header
    resp = session.post(
        "https://auth.openai.com/api/accounts/password/verify",
        headers=headers,
        data=json.dumps({"password": password}),
        allow_redirects=False,
    )
    resp.raise_for_status()
    return resp.json()


def _mfa_issue_challenge(session: BrowserSession, factor_id: str) -> dict:
    headers = session.get_auth_headers(referer="https://auth.openai.com/mfa-challenge")
    headers.pop("openai-sentinel-token", None)
    headers.pop("openai-sentinel-so-token", None)
    resp = session.post(
        "https://auth.openai.com/api/accounts/mfa/issue_challenge",
        headers=headers,
        data=json.dumps({"id": factor_id, "type": "totp", "force_fresh_challenge": False}),
        allow_redirects=False,
    )
    resp.raise_for_status()
    return resp.json()


def _mfa_verify(session: BrowserSession, factor_id: str, code: str) -> dict:
    headers = session.get_auth_headers(referer="https://auth.openai.com/mfa-challenge")
    headers.pop("openai-sentinel-token", None)
    headers.pop("openai-sentinel-so-token", None)
    resp = session.post(
        "https://auth.openai.com/api/accounts/mfa/verify",
        headers=headers,
        data=json.dumps({"id": factor_id, "type": "totp", "code": code}),
        allow_redirects=False,
    )
    resp.raise_for_status()
    return resp.json()


def _follow_continue_and_fetch(session: BrowserSession, continue_url: str, *, referer: str) -> dict:
    """Follow an auth continuation URL and load the resulting ChatGPT session."""
    follow_oauth_callback(session, continue_url, referer=referer)
    return fetch_session(session)


def _complete_totp_challenge(session: BrowserSession, email: str, result: dict, continue_url: str) -> dict:
    """Finish a server-selected TOTP challenge and fetch the authenticated session."""
    factor_id = _extract_factor_id(result, continue_url)
    secret = _account_totp_secret(email)
    if not factor_id or not secret:
        raise RuntimeError(f"登录进入 MFA，但缺少 factor_id 或 totp_secret：{email}")
    logger.info("[查活] 自动识别为 TOTP/MFA 登录：%s factor_id=%s", email, factor_id)
    _mfa_issue_challenge(session, factor_id)
    code = _account_totp_code(email)
    if not code:
        raise RuntimeError(f"无法生成 TOTP 验证码：{email}")
    mfa_result = _mfa_verify(session, factor_id, code)
    mfa_continue_url = _extract_continue_url(mfa_result) or continue_url
    if not mfa_continue_url:
        raise RuntimeError(f"MFA 验证成功但没有 continue_url: {mfa_result}")
    return _follow_continue_and_fetch(
        session,
        mfa_continue_url,
        referer=f"https://auth.openai.com/mfa-challenge/{factor_id}",
    )


def _stored_access_token(email: str) -> str:
    try:
        account = db.get_account_by_email(email)
        return str((account or {}).get("access_token") or "").strip()
    except Exception:
        return ""


def _clear_optional_bootstrap_circuit(session: BrowserSession) -> None:
    reset = getattr(session, "reset_circuit_breaker", None)
    if callable(reset):
        reset()
    else:
        session.blocked_until = 0.0
        session.blocked_reason = ""


def _warm_authenticated_session(session: BrowserSession, access_token: str) -> None:
    if not access_token:
        return
    from core.chatgpt_bootstrap import authenticated_bootstrap
    try:
        authenticated_bootstrap(session, access_token, strict=False)
    except Exception as exc:
        logger.warning("[查活] accessToken 预热失败，继续 reauth：%s", str(exc)[:180])
    finally:
        _clear_optional_bootstrap_circuit(session)


def _exception_response_text(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    return str(getattr(response, "text", "") or "")


def _exception_status_code(exc: BaseException) -> int | None:
    try:
        status = int(getattr(getattr(exc, "response", None), "status_code", 0) or 0)
        return status or None
    except (TypeError, ValueError):
        return None


def _validate_reauth_with_retry(session: BrowserSession, email: str, otp_after_ts: float, max_otp_attempts: int = 3, email_source: str | None = None) -> str:
    current_otp = None
    last_exc = None
    for attempt in range(1, max_otp_attempts + 1):
        try:
            if current_otp is None:
                current_otp = wait_for_otp(email, after_ts=otp_after_ts, email_source=email_source)
            continue_url = _validate_reauth_otp(session, current_otp)
            if not continue_url:
                raise RuntimeError("重认证 OTP 验证响应缺少 continue_url")
            return str(continue_url)
        except AccountUnusableError:
            raise
        except Exception as exc:
            last_exc = exc
            dead_code = detect_account_unusable_text(_exception_response_text(exc)) or detect_account_unusable_text(str(exc))
            if dead_code:
                raise AccountUnusableError(f"账号已废弃（{dead_code}）", error_code=dead_code) from exc
            if attempt >= max_otp_attempts or _exception_status_code(exc) not in (400, 401, 422):
                raise
            send_email_otp(session)
            otp_after_ts = time.time()
            current_otp = None
            time.sleep(1)
    raise last_exc if last_exc else RuntimeError("重认证 OTP 验证失败")


def _login_via_reauth(session: BrowserSession, email: str, otp_after_ts: float, email_source: str | None = None) -> dict:
    auth_url = _trigger_reauth(session, email)
    final_url = _follow_reauth(session, auth_url)
    dead_code = detect_account_unusable_text(final_url)
    if dead_code:
        raise AccountUnusableError(f"账号已废弃（{dead_code}）", error_code=dead_code)
    continue_url = _validate_reauth_with_retry(session, email, otp_after_ts, email_source=email_source)
    return _follow_continue_and_fetch(session, continue_url, referer="https://auth.openai.com/email-verification")


def _login_via_email_otp(session: BrowserSession, email: str, otp_after_ts: float, email_source: str | None = None) -> dict:
    validate_result = _validate_with_retry(session, email, otp_after_ts, email_source=email_source)
    page = validate_result.get("page") if isinstance(validate_result, dict) else {}
    page = page if isinstance(page, dict) else {}
    page_type = str(page.get("type") or "")
    continue_url = _extract_continue_url(validate_result)
    if not continue_url:
        raise RuntimeError(f"OTP 登录成功但没有 OAuth continue_url: {validate_result}")
    if "about-you" in continue_url or page_type in {"about_you", "about-you"}:
        raise RuntimeError(f"该邮箱登录后进入资料页，疑似不是完整已注册账号: page_type={page_type}, continue_url={continue_url}")
    return _follow_continue_and_fetch(session, continue_url, referer="https://auth.openai.com/email-verification")


def _login_via_password_or_otp(session: BrowserSession, email: str, otp_after_ts: float, email_source: str | None = None) -> dict:
    password = _account_registration_password(email)
    if not password:
        return _login_via_email_otp(session, email, otp_after_ts, email_source=email_source)
    password_result = _password_verify(session, password)
    continue_url = _extract_continue_url(password_result)
    page = password_result.get("page") if isinstance(password_result, dict) else {}
    page = page if isinstance(page, dict) else {}
    page_type = str(page.get("type") or "")
    if _is_mfa_challenge(password_result, continue_url):
        return _complete_totp_challenge(session, email, password_result, continue_url)
    if "email-verification" in continue_url or page_type in {"email_verification", "email_otp_send"}:
        return _login_via_email_otp(session, email, otp_after_ts, email_source=email_source)
    if continue_url:
        return _follow_continue_and_fetch(session, continue_url, referer="https://auth.openai.com/log-in/password")
    raise RuntimeError(f"密码登录成功但没有可用 continue_url: {password_result}")


def log_path(email: str) -> Path:
    safe = str(email or "").replace("/", "_").replace("\\", "_").replace(":", "_")
    return _LOG_DIR / f"live-check-{safe}.log"


def is_checking(email: str) -> bool:
    key = str(email or "").strip().lower()
    with _RUNNING_LOCK:
        return key in _RUNNING


def _validate_with_retry(session: BrowserSession, email: str, otp_after_ts: float, max_otp_attempts: int = 3, email_source: str | None = None) -> dict:
    current_otp = None
    last_exc: Exception | None = None
    for attempt in range(1, max_otp_attempts + 1):
        try:
            if current_otp is None:
                logger.info("[查活] 等待登录 OTP：%s（第 %s/%s 次）", email, attempt, max_otp_attempts)
                current_otp = wait_for_otp(email, after_ts=otp_after_ts, email_source=email_source)
            result = validate_email_otp(session, current_otp, sentinel_header=None, so_header=None)
            return result
        except EmailOtpInvalidError as exc:
            last_exc = exc
            if attempt >= max_otp_attempts:
                break
            logger.warning("[查活] OTP 无效/过期，重新发送后再取：%s", str(exc)[:180])
            send_email_otp(session)
            # 以“重新发送请求完成后”为新基准，避免刚刚失败的上一封旧码再次被 after 容忍窗口命中。
            otp_after_ts = time.time()
            current_otp = None
            time.sleep(1)
        except Exception as exc:
            # 提交 OTP 后的网络抖动（连接断开/超时/代理波动）：同一会话重发验证码再验证一次。
            if attempt >= max_otp_attempts or not _is_retryable_network_error(exc):
                raise
            last_exc = exc
            logger.warning("[查活] OTP 验证网络抖动，重新发送后再取（%s/%s）：%s", attempt, max_otp_attempts, str(exc)[:180])
            try:
                send_email_otp(session)
            except Exception:
                raise
            otp_after_ts = time.time()
            current_otp = None
            time.sleep(1)
    raise last_exc if last_exc else RuntimeError("OTP 验证失败")


def check_account_liveness(email: str, proxy: str | None = None, *, clear_log: bool = True, email_source: str | None = None) -> dict:
    """重新登录账号并刷新最新 accessToken。"""
    email = str(email or "").strip()
    if not email:
        raise ValueError("email 不能为空")

    checked_at = _now()
    key = email.lower()
    path = log_path(email)
    path.parent.mkdir(parents=True, exist_ok=True)
    if clear_log:
        path.write_text("", encoding="utf-8")

    fh: logging.FileHandler | None = None
    session: BrowserSession | None = None
    root_logger = logging.getLogger()
    thread_name = threading.current_thread().name
    with _RUNNING_LOCK:
        _RUNNING.add(key)
    try:
        fh = logging.FileHandler(str(path), encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
        fh.addFilter(lambda record: record.threadName == thread_name)
        root_logger.addHandler(fh)

        logger.info("[查活] 开始重新登录：%s", email)
        existing_access_token = _stored_access_token(email)
        has_totp = bool(_account_totp_secret(email))
        if existing_access_token and not has_totp:
            # 无 TOTP 账号优先复用现有登录态，重新认证只需邮箱 OTP。
            session = BrowserSession(proxy=proxy, fingerprint_seed=f"account:{email.lower()}")
            _warm_authenticated_session(session, existing_access_token)
            human_delay("navigate")
            session_info = _login_via_reauth(session, email, time.time(), email_source=email_source)
        else:
            # 有密码的账号在 authorize 后走 password verify，再根据响应进入 MFA/TOTP；
            # 没有密码则回退邮箱 OTP。
            session, authorize_url = _network_preflight_with_retry(email, proxy)
            otp_after_ts = time.time()
            final_url = follow_authorize(session, authorize_url)
            dead_code = detect_account_unusable_text(final_url)
            if dead_code:
                return {"ok": False, "status": "deactivated", "checked_at": checked_at, "error": dead_code}
            session_info = _login_via_password_or_otp(session, email, otp_after_ts, email_source=email_source)

        access_token = str(session_info.get("accessToken") or "")
        if not access_token:
            raise RuntimeError("重新登录后未拿到 accessToken")
        user = session_info.get("user") or {}
        account = session_info.get("account") or {}
        logger.info("[查活] 正常：%s user_id=%s plan=%s", email, user.get("id"), account.get("planType"))
        return {
            "ok": True,
            "status": "live",
            "checked_at": checked_at,
            "access_token": access_token,
            "session": session_info,
            "proxy_used": session.proxy or None,
            "fingerprint": _safe_fingerprint_for_account(session),
            "fingerprint_text": _safe_fingerprint_text_for_account(session),
        }
    except AccountUnusableError as exc:
        code = getattr(exc, "error_code", "") or detect_account_unusable_text(str(exc)) or "account_deactivated"
        logger.warning("[查活] 已废号：%s %s", email, code)
        return {"ok": False, "status": "deactivated", "checked_at": checked_at, "error": code}
    except Exception as exc:
        code = detect_account_unusable_text(_exception_response_text(exc)) or detect_account_unusable_text(str(exc))
        if code:
            logger.warning("[查活] 已废号：%s %s", email, code)
            return {"ok": False, "status": "deactivated", "checked_at": checked_at, "error": code}
        logger.warning("[查活] 失败：%s %s: %s", email, type(exc).__name__, str(exc)[:260])
        return {"ok": False, "status": "failed", "checked_at": checked_at, "error": f"{type(exc).__name__}: {str(exc)[:500]}"}
    finally:
        try:
            logger.info("[查活] 结束：%s", email)
            if session is not None:
                try:
                    session.session.close()
                except Exception:
                    pass
            if fh is not None:
                root_logger.removeHandler(fh)
                fh.close()
        finally:
            with _RUNNING_LOCK:
                _RUNNING.discard(key)
