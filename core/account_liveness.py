# -*- coding: utf-8 -*-
"""已注册账号查活：按密码、邮箱 OTP、MFA 状态重登，刷新 Web AT 后确认正常。"""
import logging
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import pyotp

from core import db
from core.session import BrowserSession, close_browser_session
from core.codex_oauth import _account_registration_password, _account_totp_secret
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
    _follow_reauth_with_retry,
    _trigger_reauth_with_retry,
    _validate_reauth_otp,
    fetch_session,
    SessionNotReadyError,
    follow_oauth_callback,
)
from core.email_provider import wait_for_otp

logger = logging.getLogger(__name__)
_LOG_DIR: Path | None = None  # None follows the process-bound database log directory.
_RUNNING: set[str] = set()
_RUNNING_LOCK = threading.Lock()

# 查活网络预检失败（403/429/代理/超时等）多为出口 IP 被 CF 标记或代理池抖动，
# 视为可换新 IP 重试；账号本身问题（废号/邮箱错误等）不重试。
_RETRYABLE_NETWORK_HINTS = (
    "403", "408", "425", "429", "500", "502", "503", "504",
    "proxy", "socks", "timeout", "timed out", "ssl", "tls",
    "connection", "closed", "reset", "temporarily unavailable",
)

_SESSION_FINGERPRINT_KEYS = {
    "device_id",
    "sentinel_sid",
    "oai_session_id",
    "auth_session_logging_id",
    "datadog_trace_id",
    "datadog_parent_id",
    "react_listening_key",
    "react_container_key",
    "react_resources_key",
}


def _is_retryable_network_error(exc: BaseException) -> bool:
    if isinstance(exc, AccountUnusableError) or _is_cloudflare_challenge(exc):
        return False
    if detect_account_unusable_text(_exception_response_text(exc)) or detect_account_unusable_text(str(exc)):
        return False
    status = _exception_status_code(exc)
    if status is not None:
        return status in {403, 408, 425, 429} or 500 <= status <= 599
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    text = str(exc or "").lower()
    return any(h in text for h in _RETRYABLE_NETWORK_HINTS)


def _new_fingerprint_pinned_session(
    email: str,
    proxy: str | None,
    fingerprint_state: dict | None = None,
    *,
    defer_identity_cookies: bool = False,
) -> BrowserSession:
    """创建任务独占账号会话；同一路由尝试内固定完整身份与浏览器画像。"""
    state = fingerprint_state if fingerprint_state is not None else {}
    saved_profile = state.get("browser_profile")
    identity = str(email).strip().lower()
    # 和协议注册使用同一套生命周期配置：开启“同邮箱保持协议指纹”时，
    # 查活可重新构造注册阶段的 device/session/硬件画像；关闭时每次查活
    # 生成独立 seed。无论哪种模式，同一任务内部的所有阶段/重试都固定。
    fingerprint_seed = str(state.get("fingerprint_seed") or "").strip() or None
    if not state.get("fingerprint_initialized"):
        from config import register as register_cfg
        reuse_by_email = bool(
            getattr(register_cfg, "PROTOCOL_REUSE_FINGERPRINT_BY_EMAIL", False)
        ) and not bool(state.get("force_fresh"))
        # “每次重新创建”必须和协议注册完全一致：不传 seed，让 BrowserSession
        # 生成真实的随机 UUID4。旧实现先生成随机 seed 再派生 UUID5，虽然值也
        # 随机，但 UUID version 位与注册时不同，属于可观测的指纹差异。
        fingerprint_seed = f"registration:{identity}" if reuse_by_email else None
        state["fingerprint_seed"] = fingerprint_seed or ""
        state["fingerprint_mode"] = (
            "registration_email_stable" if reuse_by_email else "fresh_per_check"
        )
        state["fingerprint_initialized"] = True
    session = BrowserSession(
        proxy=proxy,
        # 首次按当前出口生成地区画像；同一路由内部如需重建则原样复用。
        detect_exit_geo=not bool(saved_profile),
        browser_profile=dict(saved_profile) if isinstance(saved_profile, dict) else None,
        fingerprint_seed=fingerprint_seed,
        device_id=state.get("device_id"),
        auth_session_logging_id=state.get("auth_session_logging_id"),
        oai_session_id=state.get("oai_session_id"),
        sentinel_sid=state.get("sentinel_sid"),
        defer_identity_cookies=defer_identity_cookies,
    )
    for key in (
        "device_id", "auth_session_logging_id", "oai_session_id", "sentinel_sid",
    ):
        state.setdefault(key, str(getattr(session, key, "") or ""))
    if not saved_profile:
        generated_profile = getattr(session, "browser_profile", None)
        if isinstance(generated_profile, dict):
            state["browser_profile"] = dict(generated_profile)
    return session


def _fingerprint_mode_label(state: dict) -> str:
    mode = str(state.get("fingerprint_mode") or "")
    if mode == "registration_email_stable":
        return "同邮箱复用协议注册指纹"
    return "每次查活重新创建"


def _warm_login_fingerprint_context(session: BrowserSession) -> None:
    """参考协议登录：先取首页 Cookie，再建立设备身份并进入登录页。"""
    observe = getattr(session, "observe_chatgpt_document", None)
    if not getattr(session, "_login_home_ready", False):
        logger.info("[查活] 登录初始化：首页 → 设备 Cookie → /auth/login → CSRF")
        home = session.get(
            "https://chatgpt.com/",
            headers=session.get_chatgpt_navigate_headers(referer="", user_initiated=True),
            allow_redirects=True,
            timeout=12,
        )
        home.raise_for_status()
        session.prime_identity_cookies()
        session._login_home_ready = True
        if callable(observe):
            observe(home)
    # 同会话重试保留 CF/OAuth Cookie，也不再带新设备 Cookie 重访首页。
    nav = session.get(
        "https://chatgpt.com/auth/login",
        headers=session.get_chatgpt_navigate_headers(
            referer="https://chatgpt.com/", user_initiated=True,
        ),
        allow_redirects=True,
        timeout=12,
    )
    nav.raise_for_status()
    if callable(observe):
        observe(nav)


def _network_preflight_with_retry(
    email: str,
    proxy: str | None,
    max_attempts: int = 4,
    fingerprint_state: dict | None = None,
) -> tuple[BrowserSession, str]:
    """CSRF → Signin 备用预检；失败时保留同一会话重试。

    `/api/auth/providers` 只是 NextAuth 的发现接口，signin 端点并不依赖它返回的
    内容。实际运行中该接口很容易先被 Cloudflare 拦截，如果把它作为硬门槛，后续
    本来可用的 CSRF/授权链永远不会执行。因此查活备用链不再把 providers 当作
    必经步骤。

    这里必须原样传递 ``proxy``：``None`` 表示按配置选代理，空字符串表示明确
    直连。之前用 ``proxy if proxy else None`` 把直连兜底误变成了再次抽取代理。
    """
    session: BrowserSession | None = None
    last_exc: BaseException | None = None
    state = fingerprint_state if fingerprint_state is not None else {}
    # 一次网络预检只创建一个 BrowserSession。403 响应下发的新 __cf_bm、
    # OAuth/设备上下文都保留在同一 Cookie Jar 中供下一轮使用。
    session = _new_fingerprint_pinned_session(
        email, proxy, state, defer_identity_cookies=True,
    )
    logger.info("[查活] 指纹生命周期：%s", _fingerprint_mode_label(state))
    for attempt in range(1, max_attempts + 1):
        logger.info(
            "[查活] 复用统一会话：proxy=%s device_id=%s oai_session_id=%s（网络预检第 %s/%s 次）",
            session.proxy or "配置随机/直连", session.device_id,
            str(getattr(session, "oai_session_id", "") or "")[:12] + "...",
            attempt, max_attempts,
        )
        logger.info("[查活] 指纹摘要：%s", session.fingerprint_summary_text())
        try:
            _warm_login_fingerprint_context(session)
            csrf = get_csrf_token(session)
            authorize_url = signin_openai(session, csrf, email, login_only=True)
            return session, authorize_url
        except Exception as exc:
            last_exc = exc
            if attempt >= max_attempts or not _is_retryable_network_error(exc):
                try:
                    close_browser_session(session)
                except Exception:
                    pass
                raise
            _clear_optional_bootstrap_circuit(session)
            logger.warning(
                "[查活] 网络预检失败（%s/%s），保留当前 session/deviceId/CF Cookie 重试：%s",
                attempt, max_attempts, str(exc)[:200],
            )
            time.sleep(2)
    raise RuntimeError(f"网络预检多次失败：{last_exc}")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _safe_fingerprint_for_account(session: BrowserSession) -> dict:
    """账号里只记录运行环境画像，不保存会话/设备标识。"""
    fp = session.fingerprint_summary()
    return {k: v for k, v in fp.items() if k not in _SESSION_FINGERPRINT_KEYS}


def _safe_fingerprint_text_for_account(session: BrowserSession) -> str:
    fp = _safe_fingerprint_for_account(session)
    parts = [
        f"proxy={BrowserSession._short_value(fp.get('proxy') or 'direct', 36)}",
        f"ua={BrowserSession._short_value(fp.get('user_agent'), 72)}",
        f"lang={fp.get('accept_language')}",
        f"tz={fp.get('timezone_iana')}({fp.get('timezone_offset_minutes')})",
        f"screen={fp.get('screen_width')}x{fp.get('screen_height')}@{fp.get('device_pixel_ratio')}",
        f"cpu={fp.get('hardware_concurrency')}",
        f"mem={fp.get('device_memory')}",
        f"geo={fp.get('geo_country') or '?'}:{fp.get('geo_city') or '?'}",
    ]
    return " ".join(parts)


def _resolve_continue_url(value: str) -> str:
    """Auth 步骤留在 auth 域，ChatGPT callback 相对路径回到 chatgpt 域。"""
    parsed = urlparse(value)
    path = "/" + parsed.path.lstrip("/")
    is_web_callback = any(
        path == prefix or path.startswith(prefix + "/")
        for prefix in ("/api/auth/callback", "/auth/callback")
    )
    base = "https://chatgpt.com/" if is_web_callback else "https://auth.openai.com/"
    return urljoin(base, value)


def _extract_continue_url(result: dict | None) -> str:
    if not isinstance(result, dict):
        return ""
    page = result.get("page") or {}
    page = page if isinstance(page, dict) else {}
    for container in (result, page):
        for key in ("continue_url", "external_url", "redirect_url", "url"):
            value = str(container.get(key) or "").strip()
            if value:
                return _resolve_continue_url(value)
    return ""


def _extract_factor_id(result: dict | None, continue_url: str) -> str:
    """优先选明确的 TOTP 因子，避免把短信因子或 URL 查询串当作 ID。"""
    factors = []

    def collect(value, depth=0):
        if depth > 8:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"factors", "mfa_factors"} and isinstance(child, list):
                    factors.extend(item for item in child if isinstance(item, dict))
                elif isinstance(child, (dict, list)):
                    collect(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                collect(child, depth + 1)

    collect(result)
    for factor in factors:
        if str(factor.get("factor_type") or factor.get("type") or "").lower() == "totp":
            factor_id = str(factor.get("id") or factor.get("factor_id") or "").strip()
            if factor_id:
                return factor_id
    if factors:
        raise RuntimeError("账号没有可用的 TOTP 验证器，请确认 2FA 设置")
    if isinstance(result, dict):
        page = result.get("page")
        page = page if isinstance(page, dict) else {}
        payload = page.get("payload")
        for container in (result, page, payload):
            if isinstance(container, dict):
                factor_id = str(container.get("factor_id") or "").strip()
                if factor_id:
                    return factor_id
                # 仅在明确声明类型的因子对象中接受 id。
                if str(container.get("factor_type") or container.get("type") or "").lower() == "totp":
                    return str(container.get("id") or "").strip()
    path = urlparse(continue_url).path
    if "/mfa-challenge/" in path:
        return path.split("/mfa-challenge/", 1)[1].split("/", 1)[0]
    return ""


def _login_step(result: dict | None) -> str:
    result = result if isinstance(result, dict) else {}
    page = result.get("page")
    page_type = str(page.get("type") if isinstance(page, dict) else page or "").strip().lower()
    path = urlparse(_extract_continue_url(result)).path.lower()
    if page_type in {
        "mfa", "mfa_challenge", "totp", "totp_verification", "mfa_totp",
        "two_factor", "two_factor_totp", "otp_totp",
    } or "/mfa-challenge" in path:
        return "mfa"
    if page_type == "email_otp_send" or path.rstrip("/").endswith("/email-otp/send"):
        return "email_otp_send"
    if page_type in {
        "email_verification", "email_otp",
        "email_otp_verification", "contact_verification",
    } or any(part in path for part in ("email-verification", "email-otp", "contact-verification")):
        return "email_otp"
    if page_type in {"password", "login_password", "password_verify"} or path.endswith(("/log-in/password", "/login/password")):
        return "password"
    if page_type in {
        "add_phone", "phone_verification", "phone_otp", "phone_verification_required",
    } or any(part in path for part in ("add-phone", "phone-verification")):
        return "phone"
    if page_type in {
        "about_you", "about-you", "create_account", "create_password",
    } or any(part in path for part in ("about-you", "create-account", "create-password")):
        return "incomplete"
    return "continue" if _extract_continue_url(result) else "unknown"


def _auth_response_json(resp, stage: str, *, allow_empty: bool = False) -> dict:
    """保留 HTTP 异常上下文，明确账号停用才判废；挑战接口允许 204。"""
    try:
        data = resp.json()
    except ValueError:
        data = None
    body = str(getattr(resp, "text", "") or "")
    has_error = resp.status_code >= 400 or (isinstance(data, dict) and bool(data.get("error")))
    code = detect_account_unusable_text(body) if has_error else ""
    if code:
        raise AccountUnusableError(f"{stage}：账号已停用（{code}）", error_code=code)
    resp.raise_for_status()
    if allow_empty and resp.status_code == 204:
        return {}
    if isinstance(data, dict) and data.get("error"):
        # 不把服务端原始内容（可能包含敏感数据）写入日志。
        raise RuntimeError(f"{stage}未通过，请检查凭据后重试")
    location = (getattr(resp, "headers", None) or {}).get("location", "")
    if resp.status_code in (301, 302, 303, 307, 308) and location:
        result = dict(data) if isinstance(data, dict) else {}
        if not _extract_continue_url(result):
            result["continue_url"] = _resolve_continue_url(location)
        return result
    if not isinstance(data, dict):
        raise RuntimeError(f"{stage}返回非 JSON 对象，无法继续认证")
    return data


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
    return _auth_response_json(resp, "密码验证")


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
    return _auth_response_json(resp, "MFA challenge", allow_empty=True)


def _mfa_verify(session: BrowserSession, factor_id: str, code: str) -> dict:
    headers = session.get_auth_headers(referer=f"https://auth.openai.com/mfa-challenge/{factor_id}")
    headers.pop("openai-sentinel-token", None)
    headers.pop("openai-sentinel-so-token", None)
    resp = session.post(
        "https://auth.openai.com/api/accounts/mfa/verify",
        headers=headers,
        data=json.dumps({"id": factor_id, "type": "totp", "code": code}),
        allow_redirects=False,
    )
    return _auth_response_json(resp, "TOTP 验证")


def _totp_for_account(email: str) -> pyotp.TOTP:
    secret = _account_totp_secret(email)
    if not secret:
        raise RuntimeError("账号要求 MFA，但没有保存 2FA 密钥")
    try:
        if secret.lower().startswith("otpauth://"):
            totp = pyotp.parse_uri(secret)
            if not isinstance(totp, pyotp.TOTP):
                raise ValueError("not TOTP")
        else:
            normalized = "".join(secret.split()).replace("-", "").upper()
            totp = pyotp.TOTP(normalized)
        if not totp.byte_secret():
            raise ValueError("empty secret")
        if not 5 <= totp.interval <= 60:
            raise ValueError("unsupported interval")
        return totp
    except Exception:
        raise RuntimeError("2FA 密钥格式无效，请使用 Base32 密钥或有效的 TOTP URI（周期 5–60 秒）") from None


def _fresh_totp_code(totp: pyotp.TOTP, tried_codes: set[str]) -> str:
    """challenge 发起后才生成码，避免过期边界与重复提交同一码。"""
    for _ in range(3):
        now = time.time()
        remaining = totp.interval - now % totp.interval
        code = totp.at(now)
        if remaining > 4 and code not in tried_codes:
            return code
        time.sleep(remaining + 0.1)
    raise RuntimeError("无法取得新的 TOTP 动态码，请稍后重试")


def _complete_totp(session: BrowserSession, email: str, result: dict) -> dict:
    factor_id = _extract_factor_id(result, _extract_continue_url(result))
    if not factor_id:
        raise RuntimeError("账号要求 MFA，但响应中没有可用的 TOTP factor_id")
    totp = _totp_for_account(email)
    _mfa_issue_challenge(session, factor_id)
    tried_codes: set[str] = set()
    for attempt in range(2):
        code = _fresh_totp_code(totp, tried_codes)
        tried_codes.add(code)
        logger.info("[查活] 正在验证 TOTP（第 %s/2 次）", attempt + 1)
        try:
            next_result = _mfa_verify(session, factor_id, code)
        except Exception as exc:
            body = _exception_response_text(exc).lower()
            invalid_code = _exception_status_code(exc) in {400, 401, 422} and any(
                hint in body for hint in ("invalid_otp", "invalid_totp", "invalid_code", "incorrect_code", "expired_code", "otp_expired", "invalid code", "incorrect code", "expired code")
            )
            if isinstance(exc, AccountUnusableError) or not invalid_code or attempt:
                raise
            logger.warning("[查活] TOTP 无效或过期，等待下一时间窗口重试")
            continue
        if _login_step(next_result) == "mfa":
            raise RuntimeError("TOTP 验证后仍停留在 MFA 页面，请检查 2FA 密钥")
        return next_result
    raise RuntimeError("TOTP 验证未通过")


def _start_passwordless_login(session: BrowserSession) -> dict:
    resp = session.post(
        "https://auth.openai.com/api/accounts/passwordless/send-otp",
        headers=session.get_auth_headers(referer="https://auth.openai.com/log-in/password"),
        data="{}",
        allow_redirects=False,
    )
    result = _auth_response_json(resp, "发送登录验证码", allow_empty=True)
    return result or {"page": {"type": "email_otp_verification"}}


def _raise_auth_redirect_error(url: str) -> None:
    code = detect_account_unusable_text(url)
    if code:
        raise AccountUnusableError(f"账号已废弃（{code}）", error_code=code)
    parsed = urlparse(url)
    keys = {key.lower() for key in parse_qs(parsed.query, keep_blank_values=True)}
    if parsed.path.rstrip("/") in {"/auth/error", "/api/auth/error"} or "error" in keys:
        raise RuntimeError("OAuth 回调返回认证错误，未建立有效登录态")


def _follow_continue_and_fetch(session: BrowserSession, continue_url: str, *, referer: str) -> dict:
    """完成 callback/session，并对 403 保留同会话 Cookie 做阶段内重试。

    callback 与 session 分开重试：callback 一旦成功就不重复消费 OAuth code；
    只有 callback 本身失败时才重放 continue_url。重试耗尽后抛给上层，由
    live_check_service 按既有代理策略执行下一次完整登录。
    """
    _raise_auth_redirect_error(continue_url)
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            final_url = follow_oauth_callback(session, continue_url, referer=referer)
            _raise_auth_redirect_error(final_url)
            break
        except Exception as exc:
            if attempt >= max_attempts or not _is_retryable_network_error(exc):
                raise
            _clear_optional_bootstrap_circuit(session)
            delay = float(2 ** (attempt - 1))
            logger.warning(
                "[查活] OAuth callback 临时失败（%s/%s），保留当前 "
                "session/deviceId/CF Cookie，%.1fs 后重试：%s",
                attempt, max_attempts, delay, str(exc)[:200],
            )
            time.sleep(delay)

    return _fetch_live_session_with_retry(session)


def _fetch_live_session_with_retry(session: BrowserSession) -> dict:
    """回调后最多轮询八次；空 session 短等，网络错误最多重试三次。"""
    network_failures = 0
    for attempt in range(1, 9):
        try:
            return fetch_session(session)
        except SessionNotReadyError:
            if attempt >= 8:
                raise
            logger.info("[查活] Session/AT 尚未就绪（%s/8），稍后读取同一登录态", attempt)
            time.sleep(0.35)
        except Exception as exc:
            network_failures += 1
            if attempt >= 8 or network_failures >= 3 or not _is_retryable_network_error(exc):
                raise
            _clear_optional_bootstrap_circuit(session)
            delay = float(2 ** (network_failures - 1))
            logger.warning(
                "[查活] Session/AT 拉取临时失败（%s/3），保留当前 "
                "session/deviceId/CF Cookie，%.1fs 后重试：%s",
                network_failures, delay, str(exc)[:200],
            )
            time.sleep(delay)
    raise RuntimeError("查活 Session/AT 拉取重试耗尽")


def _stored_access_token(email: str) -> str:
    """读取本地账号已有 AT，用于先预热登录态再走稳定的 reauth 链。"""
    try:
        account = db.get_account_by_email(email)
        return str((account or {}).get("access_token") or "").strip()
    except Exception as exc:
        logger.debug("[查活] 读取已有 accessToken 失败，改走备用登录链：%s: %s", type(exc).__name__, exc)
        return ""


def _clear_optional_bootstrap_circuit(session: BrowserSession) -> None:
    """清理可选登录态预热造成的本地熔断，不影响后续正式认证请求。

    authenticated_bootstrap 是 best-effort 预热，其中个别旧接口返回 403 不等于
    reauth 链不可用；BrowserSession 的通用熔断器若保留该状态，会直接拦截后续
    `/api/auth/csrf`，导致稳定的 2FA 链也无法开始。
    """
    reset = getattr(session, "reset_circuit_breaker", None)
    if callable(reset):
        reset()
        return
    if getattr(session, "blocked_until", 0.0):
        session.blocked_until = 0.0
        session.blocked_reason = ""


def _warm_authenticated_session(session: BrowserSession, access_token: str) -> None:
    """复用 2FA 已验证的登录态预热流程。预热失败不直接判定账号死亡。"""
    if not access_token:
        return
    from core.chatgpt_bootstrap import authenticated_bootstrap

    try:
        logger.info("[查活] 使用已有 accessToken 预热登录态...")
        authenticated_bootstrap(session, access_token, strict=False)
        logger.info("[查活] accessToken 预热完成，继续走 reauth OTP")
    except Exception as exc:
        # strict=False 已经会吞掉大部分单接口错误；这里仅兜住初始化异常。
        logger.warning("[查活] accessToken 预热失败，继续走 reauth OTP：%s: %s", type(exc).__name__, str(exc)[:180])
    finally:
        _clear_optional_bootstrap_circuit(session)


def _exception_response_text(exc: BaseException) -> str:
    response = getattr(exc, "response", None)
    return str(getattr(response, "text", "") or "")


def _exception_response_headers(exc: BaseException) -> dict:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    return dict(headers or {}) if headers is not None else {}


def _is_cloudflare_challenge(exc: BaseException) -> bool:
    """识别 Cloudflare 明确返回的挑战页，不把它误判成账号失效。"""
    headers = {str(key).lower(): str(value).lower() for key, value in _exception_response_headers(exc).items()}
    return headers.get("cf-mitigated") == "challenge"


def _exception_status_code(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    try:
        status = int(getattr(response, "status_code", 0) or 0)
    except (TypeError, ValueError):
        return None
    return status or None


def _failure_result(exc: BaseException, checked_at: str) -> dict:
    status = _exception_status_code(exc)
    body = _exception_response_text(exc)
    error_code = ""
    try:
        payload = json.loads(body)
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            error_code = str(error.get("code") or error.get("type") or "")
        elif isinstance(error, str):
            error_code = error
    except (ValueError, TypeError):
        pass
    session_pending = isinstance(exc, SessionNotReadyError)
    challenge_pending = _is_cloudflare_challenge(exc)
    if session_pending:
        error_code = "session_not_ready"
    elif challenge_pending:
        error_code = "cloudflare_challenge"
    hints = {
        "invalid_username_or_password": "邮箱或密码不正确，请检查保存的登录凭据",
        "invalid_password": "密码不正确，请更新保存的登录密码",
        "invalid_otp": "验证码无效，请检查验证码或 2FA 密钥后重试",
        "invalid_totp": "TOTP 验证失败，请检查 2FA 密钥和系统时间",
        "invalid_code": "验证码无效，请检查验证码或 2FA 密钥后重试",
        "rate_limit_exceeded": "登录请求被限流，请稍后重试",
    }
    message = hints.get(error_code)
    if message is None and status == 429:
        message = "登录请求被限流，请稍后重试"
    if message is None and challenge_pending:
        message = "检测到 Cloudflare 人机验证，请在可见窗口完成后恢复任务"
    if message is None and status == 403:
        message = "认证请求被拒绝，请检查网络出口或稍后重试"
    error_text = f"{message}（HTTP {status}）" if message and status else message
    credential_error = error_code in {
        "invalid_username_or_password", "invalid_password", "wrong_password",
        "incorrect_password", "password_mismatch", "invalid_otp", "invalid_totp",
        "invalid_code", "incorrect_code", "expired_code", "otp_expired",
    }
    return {
        "ok": False,
        "status": "failed",
        "checked_at": checked_at,
        "error": error_text or f"{type(exc).__name__}: {str(exc)[:500]}",
        "http_status": status,
        "error_code": error_code,
        "retryable": getattr(exc, "retryable", True) is not False and not challenge_pending and not credential_error and (session_pending or _is_retryable_network_error(exc)),
        "challenge_waiting": challenge_pending,
    }


def _validate_reauth_with_retry(
    session: BrowserSession,
    email: str,
    otp_after_ts: float,
    max_otp_attempts: int = 3,
    email_source: str | None = None,
) -> str:
    """提交 reauth OTP；验证码错误时重新发送并重新取码。"""
    current_otp: str | None = None
    last_exc: Exception | None = None
    for attempt in range(1, max_otp_attempts + 1):
        try:
            if current_otp is None:
                logger.info("[查活] 等待重认证 OTP：%s（第 %s/%s 次）", email, attempt, max_otp_attempts)
                current_otp = wait_for_otp(
                    email,
                    after_ts=otp_after_ts,
                    email_source=email_source,
                )
            human_delay("otp_input")
            continue_url = _validate_reauth_otp(session, current_otp)
            if not continue_url:
                raise RuntimeError("重认证 OTP 验证响应缺少 continue_url")
            return str(continue_url)
        except AccountUnusableError:
            raise
        except Exception as exc:
            last_exc = exc
            body = _exception_response_text(exc)
            dead_code = detect_account_unusable_text(body) or detect_account_unusable_text(str(exc))
            if dead_code:
                raise AccountUnusableError(
                    f"账号已废弃（{dead_code}），邮箱不可再用",
                    error_code=dead_code,
                ) from exc

            status = _exception_status_code(exc)
            # reauth validate 的 403 可能是 Cloudflare/出口拦截；不要在同一已熔断
            # 会话上反复发送 OTP，交给上层的直连兜底处理。
            retryable_otp = status in (400, 401, 422)
            if attempt >= max_otp_attempts or not retryable_otp:
                raise
            logger.warning(
                "[查活] 重认证 OTP 无效/过期，重新发送后再取（%s/%s）：%s",
                attempt,
                max_otp_attempts,
                str(exc)[:180],
            )
            send_email_otp(session)
            otp_after_ts = time.time()
            current_otp = None
            time.sleep(1)
    raise last_exc if last_exc else RuntimeError("重认证 OTP 验证失败")


def _login_via_reauth(
    session: BrowserSession,
    email: str,
    otp_after_ts: float,
    email_source: str | None = None,
) -> dict:
    """按 2FA 已验证链路重新认证并刷新 ChatGPT session。"""
    auth_url = _trigger_reauth_with_retry(session, email)
    logger.info("[查活] reauth authorize URL 已获取")
    human_delay("api")
    final_url = _follow_reauth_with_retry(session, auth_url)
    _raise_auth_redirect_error(final_url)
    step = _login_step({"continue_url": final_url})
    if step == "continue" and urlparse(final_url).hostname == "chatgpt.com":
        # authorize 已自动跟随到 ChatGPT，不能再消费同一个回调 code。
        return _fetch_live_session_with_retry(session)
    if step in {"password", "mfa", "email_otp_send", "phone", "incomplete", "continue"}:
        return _login_via_password_or_otp(
            session, email, otp_after_ts, email_source=email_source, initial_url=final_url,
        )
    human_delay("navigate")
    logger.info("[查活] 已跟随 reauth authorize URL，开始等待邮箱 OTP")
    continue_url = _validate_reauth_with_retry(
        session,
        email,
        otp_after_ts,
        email_source=email_source,
    )
    logger.info("[查活] reauth OTP 验证通过，开始交换新 token")
    human_delay("api")
    return _complete_login_steps(
        session, email, {"continue_url": continue_url}, otp_after_ts,
        email_source=email_source,
        referer="https://auth.openai.com/email-verification",
    )


def _complete_login_steps(
    session: BrowserSession,
    email: str,
    result: dict,
    otp_after_ts: float,
    *,
    email_source: str | None = None,
    referer: str = "https://auth.openai.com/",
) -> dict:
    """参考 reauth-web 的状态分流；每种凭据最多提交一次，避免认证循环。"""
    visited: set[str] = set()
    for _ in range(6):
        step = _login_step(result)
        logger.info("[查活] 认证阶段：%s", step)
        if step == "phone":
            raise RuntimeError("账号要求手机号验证，请先完成验证后再查活")
        if step == "incomplete":
            raise RuntimeError("账号进入注册资料补全页面，尚未完成注册")
        if step == "unknown":
            raise RuntimeError("认证响应缺少下一步骤或 continue_url，无法确认登录成功")
        if step == "continue":
            return _follow_continue_and_fetch(session, _extract_continue_url(result), referer=referer)
        if step in visited:
            raise RuntimeError(f"认证重复进入 {step}，未完成登录，请检查账号凭据")
        visited.add(step)
        if step == "password":
            password = _account_registration_password(email)
            otp_after_ts = time.time()
            if password:
                result = _password_verify(session, password)
            else:
                logger.info("[查活] 未保存密码，先请求发送登录验证码")
                result = _start_passwordless_login(session)
            referer = "https://auth.openai.com/log-in/password"
        elif step == "email_otp_send":
            otp_after_ts = time.time()
            send_url = _extract_continue_url(result) or "https://auth.openai.com/api/accounts/email-otp/send"
            headers = session.get_auth_navigate_headers(referer=referer)
            response = session.get(send_url, headers=headers, allow_redirects=True)
            response.raise_for_status()
            rotate = getattr(session, "rotate_document_navigation_id", None)
            if callable(rotate):
                rotate()
            final_url = str(getattr(response, "url", "") or "")
            result = {"continue_url": final_url} if final_url else {"page": {"type": "email_verification"}}
            referer = send_url
        elif step == "email_otp":
            result = _validate_with_retry(session, email, otp_after_ts, email_source=email_source)
            referer = "https://auth.openai.com/email-verification"
        elif step == "mfa":
            otp_after_ts = time.time()
            result = _complete_totp(session, email, result)
            referer = "https://auth.openai.com/mfa-challenge"
    raise RuntimeError("认证步骤超过上限，未完成登录")


def _login_via_email_otp(
    session: BrowserSession,
    email: str,
    otp_after_ts: float,
    email_source: str | None = None,
) -> dict:
    return _complete_login_steps(
        session, email, {"page": {"type": "email_verification"}}, otp_after_ts,
        email_source=email_source,
    )


def _login_via_password_or_otp(
    session: BrowserSession,
    email: str,
    otp_after_ts: float,
    email_source: str | None = None,
    *,
    initial_url: str = "",
) -> dict:
    """遵从 authorize 落点；普通登录页先导航到密码页再提交凭据。"""
    if initial_url:
        _raise_auth_redirect_error(initial_url)
        if urlparse(initial_url).hostname == "chatgpt.com":
            return _fetch_live_session_with_retry(session)
    initial_result = {"continue_url": initial_url} if initial_url else {}
    if not initial_url or urlparse(initial_url).path.rstrip("/") in {"/log-in", "/login"}:
        if _account_registration_password(email):
            password_url = "https://auth.openai.com/log-in/password"
            response = session.get(
                password_url,
                headers=session.get_auth_navigate_headers(
                    referer=initial_url or "https://auth.openai.com/log-in",
                ),
                allow_redirects=True,
            )
            response.raise_for_status()
            rotate = getattr(session, "rotate_document_navigation_id", None)
            if callable(rotate):
                rotate()
            landed = str(getattr(response, "url", "") or password_url)
            _raise_auth_redirect_error(landed)
            if urlparse(landed).hostname == "chatgpt.com":
                return _fetch_live_session_with_retry(session)
            initial_result = {"continue_url": landed}
            if _login_step(initial_result) not in {"password", "email_otp", "email_otp_send", "mfa", "phone", "incomplete"}:
                raise RuntimeError("未能进入密码或验证码页面，未提交登录凭据")
        else:
            # 没有密码也应先请求发码，不能在普通登录页直接等待不存在的邮件。
            initial_result = {"page": {"type": "login_password"}}
    return _complete_login_steps(
        session, email, initial_result, otp_after_ts, email_source=email_source,
    )


def _login_via_full_web_flow(
    email: str,
    proxy: str | None,
    *,
    email_source: str | None,
    fingerprint_state: dict,
) -> tuple[BrowserSession, dict]:
    """通过首页、登录页和 NextAuth 登录入口建立全新登录态。"""
    session, authorize_url = _network_preflight_with_retry(
        email,
        proxy,
        fingerprint_state=fingerprint_state,
    )
    try:
        otp_after_ts = time.time()
        final_url = follow_authorize(session, authorize_url)
        dead_code = detect_account_unusable_text(final_url)
        if dead_code:
            raise AccountUnusableError(
                f"账号已废弃（{dead_code}）",
                error_code=dead_code,
            )
        session_info = _login_via_password_or_otp(
            session,
            email,
            otp_after_ts,
            email_source=email_source,
            initial_url=final_url,
        )
        return session, session_info
    except BaseException:
        # 返回前会话归此函数所有；上层尚未取得引用，必须在这里回收。
        try:
            close_browser_session(session)
        except Exception:
            pass
        raise


def log_path(email: str) -> Path:
    safe = str(email or "").replace("/", "_").replace("\\", "_").replace(":", "_")
    return (_LOG_DIR or db._LOG_DIR) / f"live-check-{safe}.log"


def is_checking(email: str) -> bool:
    key = str(email or "").strip().lower()
    with _RUNNING_LOCK:
        return key in _RUNNING


def _validate_with_retry(
    session: BrowserSession,
    email: str,
    otp_after_ts: float,
    max_otp_attempts: int = 3,
    email_source: str | None = None,
) -> dict:
    current_otp = None
    last_exc: Exception | None = None
    for attempt in range(1, max_otp_attempts + 1):
        try:
            if current_otp is None:
                logger.info("[查活] 等待登录 OTP：%s（第 %s/%s 次）", email, attempt, max_otp_attempts)
                current_otp = wait_for_otp(
                    email,
                    after_ts=otp_after_ts,
                    email_source=email_source,
                )
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


def _live_result(email: str, session_info: dict, checked_at: str, **details) -> dict:
    """协议和浏览器查活使用相同的凭据完整性与账号校验。"""
    access_token = str(session_info.get("accessToken") or "").strip()
    if not access_token:
        raise RuntimeError("重新登录后未拿到 accessToken")
    user = session_info.get("user") or {}
    returned_email = str(user.get("email") or "").strip()
    if returned_email and returned_email.casefold() != email.casefold():
        raise RuntimeError("登录会话邮箱与查活账号不一致，未写入凭据")
    account = session_info.get("account") or {}
    logger.info("[查活] 正常：%s user_id=%s plan=%s", email, user.get("id"), account.get("planType"))
    return {
        "ok": True,
        "status": "live",
        "checked_at": checked_at,
        "access_token": access_token,
        "session": session_info,
        **details,
    }


def check_account_liveness(
    email: str,
    proxy: str | None = None,
    *,
    clear_log: bool = True,
    email_source: str | None = None,
    fingerprint_state: dict | None = None,
) -> dict:
    """
    重新登录账号并刷新最新 accessToken。

    返回：
      {
        ok: bool,
        status: live/deactivated/failed,
        access_token: str?,
        session: dict?,
        checked_at: ISO,
        error: str?
      }
    """
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
    task_fingerprint_state = fingerprint_state if fingerprint_state is not None else {}
    root_logger = logging.getLogger()
    thread_name = threading.current_thread().name
    with _RUNNING_LOCK:
        _RUNNING.add(key)
    try:
        fh = logging.FileHandler(str(path), encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
        ))
        fh.addFilter(lambda record: record.threadName == thread_name)
        root_logger.addHandler(fh)

        logger.info("[查活] 日志文件：%s", path)
        logger.info("[查活] 开始重新登录：%s", email)
        from config import live_check as live_cfg
        driver_mode = str(live_cfg.LIVE_CHECK_DRIVER or "protocol").strip().lower()
        logger.info("[查活] 查活驱动：%s", driver_mode)
        if driver_mode == "cloak":
            from core.cloakbrowser_liveness import login_with_cloak
            session_info, details = login_with_cloak(email, proxy=proxy, email_source=email_source)
            return _live_result(email, session_info, checked_at, **details)
        if driver_mode != "protocol":
            raise ValueError(f"不支持的 LIVE_CHECK_DRIVER={driver_mode!r}，可选 protocol / cloak")
        existing_access_token = _stored_access_token(email)
        has_password = bool(_account_registration_password(email))
        has_totp = bool(_account_totp_secret(email))
        if existing_access_token and not has_password and not has_totp:
            # 无密码/2FA 的旧记录保留已验证的 AT reauth 邮箱登录链。
            # 有密码则走完整登录，并遵从服务端要求处理 OTP/MFA。
            logger.info("[查活] 流程：登录态预热 → CSRF → Reauth Signin → Authorize → 邮箱 OTP → OAuth callback → Session/AT")
            session = _new_fingerprint_pinned_session(email, proxy, task_fingerprint_state)
            logger.info(
                "[查活] 指纹生命周期：%s",
                _fingerprint_mode_label(task_fingerprint_state),
            )
            logger.info(
                "[查活] 会话创建完成：proxy=%s device_id=%s（复用2FA稳定链路）",
                session.proxy or "直连/配置随机",
                session.device_id,
            )
            logger.info("[查活] 指纹摘要：%s", session.fingerprint_summary_text())
            _warm_authenticated_session(session, existing_access_token)
            human_delay("navigate")
            try:
                session_info = _login_via_reauth(
                    session,
                    email,
                    time.time(),
                    email_source=email_source,
                )
            except Exception as reauth_exc:
                if not _is_retryable_network_error(reauth_exc):
                    raise
                # reauth/callback 的同会话阶段重试已耗尽，建立干净会话并
                # 按首页 → 登录页 → CSRF → signin 完整重登。
                failed_proxy = session.proxy if proxy is None else proxy
                logger.warning(
                    "[查活] AT reauth 链临时失败，切换干净会话执行纯协议完整登录：%s",
                    str(reauth_exc)[:240],
                )
                try:
                    close_browser_session(session)
                except Exception:
                    pass
                session, session_info = _login_via_full_web_flow(
                    email,
                    failed_proxy,
                    email_source=email_source,
                    fingerprint_state=task_fingerprint_state,
                )
        else:
            # 有密码/2FA 或没有 AT 时，使用已有账号登录入口并按服务端要求验证。
            logger.info(
                "[查活] 流程：首页 → 设备 Cookie → 登录页 → CSRF → Signin(login) → "
                "Authorize → 密码/邮箱 OTP → MFA(如有) → OAuth callback → Session/AT"
            )
            session, session_info = _login_via_full_web_flow(
                email,
                proxy,
                email_source=email_source,
                fingerprint_state=task_fingerprint_state,
            )
        return _live_result(
            email, session_info, checked_at,
            proxy_used=session.proxy or None,
            fingerprint=_safe_fingerprint_for_account(session),
            fingerprint_text=_safe_fingerprint_text_for_account(session),
        )
    except AccountUnusableError as exc:
        code = getattr(exc, "error_code", "") or detect_account_unusable_text(str(exc)) or "account_deactivated"
        logger.warning("[查活] 已废号：%s %s", email, code)
        return {"ok": False, "status": "deactivated", "checked_at": checked_at, "error": code}
    except Exception as exc:
        from core.task_control import TaskCancelled
        if isinstance(exc, TaskCancelled):
            raise
        code = detect_account_unusable_text(_exception_response_text(exc)) or detect_account_unusable_text(str(exc))
        if code:
            logger.warning("[查活] 已废号：%s %s", email, code)
            return {"ok": False, "status": "deactivated", "checked_at": checked_at, "error": code}
        result = _failure_result(exc, checked_at)
        logger.warning("[查活] 失败：%s %s", email, result["error"])
        return result
    finally:
        try:
            logger.info("[查活] 结束：%s", email)
            if session is not None:
                try:
                    close_browser_session(session)
                except Exception:
                    pass
            if fh is not None:
                root_logger.removeHandler(fh)
                fh.close()
        finally:
            with _RUNNING_LOCK:
                _RUNNING.discard(key)
