# -*- coding: utf-8 -*-
"""本地 Playwright ChatGPT 注册与查活驱动。

与 ``core.openai_auth`` 的纯协议链路不同，本模块让 Chromium 自己访问
``chatgpt.com/auth/login``、``auth.openai.com`` 和验证码页面。这样 authorize
阶段的 Cookie、JS challenge、导航头和页面状态由真实浏览器维护，避免 curl
协议会话反复收到 403。

模块只在选择 ``playwright`` 驱动时导入 Playwright；没有安装浏览器时给出明确
错误，不影响项目的 protocol/Roxy/Cloak/Cloud 驱动。
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable, Any
from urllib.parse import urlsplit, urlunsplit

from config import playwright as _cfg
from config.proxy import normalize_proxy_url, redact_proxy_url
from core.account_export import save_account_data
from core.email_provider import acquire_email_after_input, resolve_email_source, wait_for_otp
from core.profile_utils import generate_random_birthday

logger = logging.getLogger(__name__)

_PROFILE_LOCKS: dict[str, threading.Lock] = {}
_PROFILE_LOCKS_GUARD = threading.Lock()
_HELD_PROFILE_LOCKS: dict[int, tuple[str, threading.Lock]] = {}
_PERSISTENT_CONTEXTS: set[int] = set()


def _account_registration_password(email: str) -> str:
    # 延迟导入，避免 account_liveness -> playwright_auth -> account_liveness 循环导入。
    from core.codex_oauth import _account_registration_password as reader
    return str(reader(email) or "").strip()


def _account_totp_code(email: str) -> str:
    from core.codex_oauth import _account_totp_code as reader
    return str(reader(email) or "").strip()


class PlaywrightAuthError(RuntimeError):
    """Playwright 注册/查活失败，并携带可供查活路由判断的结构化信息。"""

    def __init__(
        self,
        message: str,
        *,
        code: str = "playwright_error",
        http_status: int | None = None,
        phase: str | None = None,
    ) -> None:
        self.code = str(code or "playwright_error")
        self.http_status = int(http_status) if http_status else None
        self.phase = str(phase or "") or None
        super().__init__(message)


_SENSITIVE_VALUE_RE = re.compile(
    r"(?i)(['\"]?(?:access[_-]?token|refresh[_-]?token|id[_-]?token|csrf(?:token)?|password|secret|code|otp)['\"]?\s*[:=]\s*['\"]?)[^,}\s'\"]+"
)
_URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)


def _safe_url_for_log(value: object, *, max_length: int = 240) -> str:
    """只保留 URL 的 scheme/host/path/查询键，避免日志写入 OAuth/OTP 值。"""
    text = str(value or "")
    if not text:
        return "-"
    try:
        parsed = urlsplit(text)
        netloc = parsed.netloc.rsplit("@", 1)[-1]
        keys = []
        for item in str(parsed.query or "").split("&"):
            if item:
                keys.append(item.split("=", 1)[0] + "=<redacted>")
        safe = urlunsplit((parsed.scheme, netloc, parsed.path, "&".join(keys), ""))
    except Exception:
        safe = text.split("?", 1)[0]
    safe = re.sub(r"[\r\n\x00-\x1f\x7f]", " ", safe)
    return safe[:max_length] + ("…" if len(safe) > max_length else "")


def _safe_error_text(value: object, *, max_length: int = 500) -> str:
    text = str(value or "")
    text = _URL_RE.sub(lambda match: _safe_url_for_log(match.group(0)), text)
    text = _SENSITIVE_VALUE_RE.sub(r"\1<redacted>", text)
    text = re.sub(r"[\r\n\x00-\x1f\x7f]", " ", text)
    text = " ".join(text.split())
    return text[:max_length] + ("…" if len(text) > max_length else "")


_EDGE_RETRY_CODES = {"edge_http", "challenge_required"}
_EDGE_DEAD_CODES = {"account_deactivated", "account_deleted", "account_banned"}
_EDGE_URL_MARKERS = (
    "/cdn-cgi/challenge-platform",
    "challenge-platform",
    "/cdn-cgi/challenge",
    "cf-chl-",
)
_EDGE_TEXT_MARKERS = (
    "just a moment",
    "checking your browser",
    "verify you are human",
    "enable javascript and cookies",
    "performing security verification",
    "security verification",
    "cloudflare ray id",
)


def _bounded_edge_retry_attempts(value: int | None = None) -> int:
    """Return a small, deterministic retry budget for browser edge responses."""
    raw = value if value is not None else getattr(_cfg, "PLAYWRIGHT_EDGE_RETRY_ATTEMPTS", 3)
    try:
        attempts = int(raw)
    except (TypeError, ValueError):
        attempts = 3
    return max(1, min(4, attempts))


def _edge_challenge_wait_seconds(value: float | int | None = None) -> float:
    """Return the bounded time given to Chromium's own challenge scripts."""
    raw = value if value is not None else getattr(_cfg, "PLAYWRIGHT_EDGE_CHALLENGE_WAIT", 5.0)
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        seconds = 5.0
    return max(0.0, min(15.0, seconds))


def _response_headers(response) -> dict[str, str]:
    """Read Playwright/requests-style response headers without exposing values."""
    try:
        headers = getattr(response, "headers", {})
        headers = headers() if callable(headers) else headers
    except Exception:
        return {}
    if not isinstance(headers, Mapping):
        return {}
    return {str(key).strip().lower(): str(value).strip().lower() for key, value in headers.items()}


def _response_has_edge_challenge(response) -> bool:
    """Detect the explicit edge challenge signal, including HTTP 200 responses."""
    return _response_headers(response).get("cf-mitigated", "") == "challenge"


def _text_has_edge_challenge(value: object) -> bool:
    text = str(value or "").lower()
    return any(marker in text for marker in _EDGE_TEXT_MARKERS)


def _response_edge_code(response) -> str:
    """Classify a first-party response without reading auth-bearing headers."""
    dead_code = _response_dead_code(response)
    if dead_code in _EDGE_DEAD_CODES:
        return dead_code
    if _response_has_edge_challenge(response):
        return "challenge_required"
    try:
        status = int(getattr(response, "status", 0) or 0)
    except (TypeError, ValueError):
        status = 0
    if status in {403, 408, 425, 429} or status >= 500:
        return "edge_http"
    return ""


def _auth_response_url(url: object) -> bool:
    try:
        parsed = urlsplit(str(url or ""))
        host = (parsed.hostname or "").lower().rstrip(".")
        path = (parsed.path or "").lower()
    except Exception:
        return False
    if host == "chatgpt.com" or host.endswith(".chatgpt.com"):
        return any(marker in path for marker in ("/api/", "/auth/", "/backend-api/", "/login"))
    if host in {"auth.openai.com", "www.auth.openai.com"}:
        return any(marker in path for marker in ("/api/", "/authorize", "/login", "/callback", "/mfa"))
    return False


def _install_edge_response_tracker(page) -> None:
    """Record transient first-party edge responses for post-submit state waits."""
    on = getattr(page, "on", None)
    if not callable(on):
        return

    def _observe(response) -> None:
        try:
            url = getattr(response, "url", "")
            url = url() if callable(url) else url
            if not _auth_response_url(url):
                return
            code = _response_edge_code(response)
            if code:
                setattr(page, "_dsh_edge_response_code", code)
                setattr(page, "_dsh_edge_response_status", getattr(response, "status", None))
            elif getattr(response, "status", 0) and int(getattr(response, "status", 0) or 0) < 400:
                setattr(page, "_dsh_edge_response_code", "")
                setattr(page, "_dsh_edge_response_status", None)
        except Exception:
            return

    try:
        setattr(page, "_dsh_edge_response_code", "")
        setattr(page, "_dsh_edge_response_status", None)
        on("response", _observe)
    except Exception:
        return


def _page_edge_response_code(page) -> str:
    try:
        return str(getattr(page, "_dsh_edge_response_code", "") or "").strip().lower()
    except Exception:
        return ""


def _clear_page_edge_response(page) -> None:
    try:
        setattr(page, "_dsh_edge_response_code", "")
        setattr(page, "_dsh_edge_response_status", None)
    except Exception:
        return


def _reload_page_after_edge(page) -> None:
    """Reload the current document after a bounded challenge wait."""
    reload = getattr(page, "reload", None)
    if not callable(reload):
        return
    _clear_page_edge_response(page)
    try:
        response = reload(wait_until="domcontentloaded")
    except Exception as exc:
        logger.debug("[Playwright] 边缘挑战后的页面重载失败：%s", _safe_error_text(exc, max_length=180))
        return
    code = _response_edge_code(response)
    if code in _EDGE_DEAD_CODES:
        raise PlaywrightAuthError("页面响应包含账号状态错误", code=code, phase="state")
    if code in _EDGE_RETRY_CODES or _page_has_edge_challenge(page):
        try:
            setattr(page, "_dsh_edge_response_code", "challenge_required")
        except Exception:
            pass


def _page_has_edge_challenge(page) -> bool:
    """Observe challenge-page markers only; never click or manufacture a proof."""
    url = _page_url(page).lower()
    if any(marker in url for marker in _EDGE_URL_MARKERS):
        return True
    try:
        title = page.title()
    except Exception:
        title = ""
    try:
        body = page.locator("body").inner_text(timeout=500)
    except Exception:
        body = ""
    if _text_has_edge_challenge(f"{title} {body}"):
        return True
    for selector in (
        "#challenge-form",
        "iframe[src*='challenges.cloudflare.com']",
        "[data-cf-chl-t]",
        "input[name='cf-turnstile-response']",
    ):
        try:
            if page.locator(selector).count() > 0:
                return True
        except Exception:
            continue
    return False


def _response_text(response) -> str:
    try:
        text = getattr(response, "text", "")
        text = text() if callable(text) else text
        return str(text or "")
    except Exception:
        return ""


def _response_dead_code(response) -> str:
    """Read only a response body needed to preserve dead-account classification."""
    body = _response_text(response)
    if not body:
        return ""
    try:
        from core.openai_auth import (
            detect_account_unusable_response_body,
            detect_account_unusable_text,
        )
        return str(
            detect_account_unusable_response_body(body)
            or detect_account_unusable_text(body)
            or ""
        )
    except Exception:
        return ""


def _validated_start_url() -> str:
    raw = str(getattr(_cfg, "PLAYWRIGHT_START_URL", "https://chatgpt.com/auth/login") or "").strip()
    parsed = urlsplit(raw)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or host not in {"chatgpt.com", "www.chatgpt.com"}:
        raise PlaywrightAuthError("PLAYWRIGHT_START_URL 必须是 chatgpt.com HTTPS 地址", code="url_config", phase="launch")
    if parsed.username or parsed.password or parsed.fragment or parsed.query:
        raise PlaywrightAuthError("PLAYWRIGHT_START_URL 不得包含认证信息、query 或 fragment", code="url_config", phase="launch")
    return raw


def _timeout_ms(value: int | float | None = None, default: int = 45) -> int:
    try:
        seconds = float(value if value is not None else getattr(_cfg, "PLAYWRIGHT_TIMEOUT", default))
    except (TypeError, ValueError):
        seconds = float(default)
    return max(1000, int(max(1.0, seconds) * 1000))


def _navigation_timeout_ms() -> int:
    return _timeout_ms(getattr(_cfg, "PLAYWRIGHT_NAVIGATION_TIMEOUT", 90), 90)


def _page_url(page) -> str:
    try:
        return str(page.url or "")
    except Exception:
        return ""


def _is_closed(page) -> bool:
    try:
        return bool(page.is_closed())
    except Exception:
        return False


def _visible(page, selector: str, timeout: int = 700):
    try:
        locator = page.locator(selector).first
        if locator.is_visible(timeout=timeout):
            return locator
    except Exception:
        pass
    return None


def _first_visible(page, selectors: list[str], timeout: int = 700):
    for selector in selectors:
        loc = _visible(page, selector, timeout=timeout)
        if loc is not None:
            return loc
    return None


def _safe_click(locator, timeout: int = 5000) -> bool:
    try:
        locator.scroll_into_view_if_needed(timeout=timeout)
    except Exception:
        pass
    try:
        locator.click(timeout=timeout)
        return True
    except Exception:
        try:
            locator.click(timeout=timeout, force=True)
            return True
        except Exception:
            return False


def _human_fill(locator, value: str, *, delay: int = 55) -> None:
    value = str(value or "")
    try:
        locator.click(timeout=4000)
        locator.fill("")
        locator.press_sequentially(value, delay=max(0, int(delay)))
    except Exception:
        locator.fill(value)


def _accept_cookies(page) -> None:
    _safe_click(_first_visible(page, [
        "button:has-text('Accept all')", "button:has-text('Accept')",
        "button:has-text('I agree')", "button:has-text('同意')", "button:has-text('接受')",
    ], timeout=400), timeout=1200)


def _email_input(page):
    return _first_visible(page, [
        "input[type='email']", "input[name='email']", "input[name='username']",
        "input[autocomplete='email']", "input[autocomplete='username']",
        "input[id*='email' i]", "input[placeholder*='email' i]",
        "input[aria-label*='email' i]", "input[placeholder*='邮箱']",
        "input[aria-label*='邮箱']",
    ], timeout=600)


def _click_email_entry(page) -> None:
    if _email_input(page) is not None:
        return
    loc = _first_visible(page, [
        "button[data-testid*='email' i]", "button[data-provider='email']",
        "button[data-auth-provider='email']", "button:has-text('Continue with email')",
        "button:has-text('Sign up with email')", "button:has-text('Log in with email')",
        "button:has-text('Email')", "button:has-text('邮箱')", "button:has-text('電子郵件')",
        "a:has-text('Continue with email')", "[role='button']:has-text('Email')",
    ], timeout=700)
    if loc is not None and _safe_click(loc):
        time.sleep(0.8)


def _click_submit(page) -> bool:
    loc = _first_visible(page, [
        "button[type='submit']", "button[data-dd-action-name='Continue']",
        "button[data-login-web-auth-control='true']", "button:has-text('Continue')",
        "button:has-text('Next')", "button:has-text('继续')", "form button",
        "input[type='submit']",
    ], timeout=500)
    if loc is not None and _safe_click(loc):
        return True
    try:
        page.keyboard.press("Enter")
        return True
    except Exception:
        return False


def _auth_state(page) -> str:
    """返回 email/password/otp/mfa/profile/chatgpt/other。"""
    url = _page_url(page).lower()
    try:
        text = (page.locator("body").inner_text(timeout=700) or "").lower()
    except Exception:
        text = ""
    try:
        from core.openai_auth import detect_account_unusable_text
        dead_code = detect_account_unusable_text(text)
    except Exception:
        dead_code = ""
    if dead_code:
        return dead_code
    response_code = _page_edge_response_code(page)
    if response_code in _EDGE_DEAD_CODES:
        return response_code
    # Challenge pages can still be hosted on chatgpt.com and must be classified
    # before the generic chatgpt URL branch below.
    if response_code in _EDGE_RETRY_CODES or _page_has_edge_challenge(page):
        return "edge_challenge"
    if "/log-in/password" in url:
        return "login_password"
    if any(x in url for x in ("mfa", "totp", "two-factor", "two_factor")):
        return "mfa"
    if any(x in url for x in ("email-verification", "email_otp", "verify-email")):
        return "otp"
    if any(x in url for x in ("about-you", "create-account/about", "/profile")):
        return "profile"
    if "chatgpt.com" in url and "/auth/" not in url and "/login" not in url:
        return "chatgpt"
    if _first_visible(page, [
        "input[autocomplete='one-time-code']", "input[name='code']", "input[name='otp']",
        "input[inputmode='numeric']", "input[maxlength='1']", "input[aria-label*='code' i]",
    ], timeout=300) is not None:
        return "otp"
    if _first_visible(page, [
        "input[type='password']", "input[autocomplete='new-password']", "input[name='password']",
    ], timeout=300) is not None:
        return "password"
    if any(marker in text for marker in ("authenticator", "verification code", "two-factor", "two factor", "一次性密码")):
        if _first_visible(page, ["input[inputmode='numeric']", "input[autocomplete='one-time-code']"], timeout=300):
            return "mfa"
    if _email_input(page) is not None:
        return "email"
    return "other"


def _wait_state(page, wanted: set[str], timeout: float = 30) -> str:
    end = time.time() + max(1.0, timeout)
    last = "other"
    challenge_seen = False
    challenge_started = 0.0
    challenge_retries = 0
    max_challenge_retries = max(0, _bounded_edge_retry_attempts() - 1)
    while time.time() < end:
        if _is_closed(page):
            raise PlaywrightAuthError("Playwright 页面已关闭", code="page_closed", phase="state")
        last = _auth_state(page)
        if last in wanted:
            return last
        if last in _EDGE_DEAD_CODES:
            raise PlaywrightAuthError(
                "页面检测到账号状态错误",
                code=last,
                phase="state",
            )
        if last == "edge_challenge":
            if not challenge_seen:
                challenge_seen = True
                challenge_started = time.time()
                logger.info("[Playwright] 检测到页面边缘挑战，等待 Chromium 页面脚本完成")
            # Wait for the browser to settle the page, but do not click a
            # challenge widget or synthesize a challenge token.
            if time.time() - challenge_started >= _edge_challenge_wait_seconds():
                if challenge_retries < max_challenge_retries:
                    challenge_retries += 1
                    challenge_seen = False
                    challenge_started = 0.0
                    logger.info(
                        "[Playwright] 边缘挑战仍在页面上，执行同一 context 重载 (%s/%s)",
                        challenge_retries,
                        max_challenge_retries,
                    )
                    _reload_page_after_edge(page)
                    continue
                raise PlaywrightAuthError(
                    "页面仍处于边缘挑战状态",
                    code="challenge_required",
                    phase="state",
                )
        time.sleep(0.25)
    if challenge_seen or challenge_retries:
        raise PlaywrightAuthError(
            "等待页面状态时遇到边缘挑战",
            code="challenge_required",
            phase="state",
        )
    raise PlaywrightAuthError(
        f"等待页面状态超时 state={last} wanted={','.join(sorted(wanted))}",
        code="state_timeout",
        phase="state",
    )


def _otp_inputs(page):
    """优先返回最具体的 OTP 输入框，避免同一单框被多个 selector 重复收集。"""
    for selector in (
        "input[autocomplete='one-time-code']",
        "input[name='code']",
        "input[name='otp']",
        "input[aria-label*='code' i]",
    ):
        try:
            loc = page.locator(selector)
            visible = [
                loc.nth(i) for i in range(min(loc.count(), 8))
                if loc.nth(i).is_visible(timeout=150)
            ]
            if visible:
                if len(visible) == 1:
                    return visible
        except Exception:
            continue

    for selector in (
        "input[maxlength='1']",
        "input[data-index]",
        "input[aria-label*='digit' i]",
        "input[inputmode='numeric']",
        "input[type='tel']",
    ):
        try:
            loc = page.locator(selector)
            visible = [
                loc.nth(i) for i in range(min(loc.count(), 8))
                if loc.nth(i).is_visible(timeout=150)
            ]
            if visible:
                return visible
        except Exception:
            continue
    return []


def _fill_otp(page, code: str) -> None:
    code = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(code) != 6:
        raise PlaywrightAuthError("OTP 必须是 6 位数字", code="otp_invalid", phase="otp")
    boxes = _otp_inputs(page)
    if not boxes:
        raise PlaywrightAuthError(f"找不到 OTP 输入框，url={_page_url(page)}")
    if len(boxes) == 1:
        _human_fill(boxes[0], code, delay=70)
        return
    if len(boxes) < len(code):
        raise PlaywrightAuthError("OTP 输入框数量不足 6 个", code="otp_input", phase="otp")
    for box, char in zip(boxes[:6], code):
        try:
            box.fill(char)
        except Exception:
            box.click()
            page.keyboard.type(char, delay=70)


def _submit_otp(page) -> None:
    _click_submit(page)


def _wait_mail_otp(email: str, after_ts: float, email_source: str | None = None) -> str:
    total = int(getattr(_cfg, "PLAYWRIGHT_OTP_TIMEOUT", 180) or 180)
    try:
        from config import email as email_cfg
        configured = int(getattr(email_cfg, "OTP_MAX_WAIT", total) or total)
        if configured > 0:
            total = min(total, configured)
    except Exception:
        pass
    return wait_for_otp(email, after_ts=after_ts, max_wait=total, email_source=email_source)


def _click_passwordless_if_present(page) -> bool:
    loc = _first_visible(page, [
        "button[name='intent'][value*='passwordless' i]",
        "input[name='intent'][value*='passwordless' i]",
        "button:has-text('one-time code')", "a:has-text('one-time code')",
        "button:has-text('一次性验证码')", "a:has-text('一次性验证码')",
        "button:has-text('ワンタイムコード')", "a:has-text('ワンタイムコード')",
    ], timeout=600)
    return bool(loc is not None and _safe_click(loc))


def _fill_password(page, password: str) -> bool:
    loc = _first_visible(page, [
        "input[type='password']", "input[autocomplete='current-password']",
        "input[name='password']",
    ], timeout=700)
    if loc is None:
        return False
    _human_fill(loc, password, delay=60)
    _click_submit(page)
    return True


def _fill_profile(page, name: str, birthday: str) -> bool:
    filled = False
    name_loc = _first_visible(page, [
        "input[name='name']", "input[name='fullName']", "input[autocomplete='name']",
        "input[placeholder*='name' i]", "input[aria-label*='name' i]",
    ], timeout=600)
    if name_loc is not None:
        _human_fill(name_loc, name, delay=75)
        filled = True
    try:
        year, month, day = birthday.split("-")
    except ValueError as exc:
        raise PlaywrightAuthError(f"生日格式无效: {birthday}") from exc
    selectors = [
        (["input[name*='year' i]", "input[aria-label*='year' i]", "[data-type='year'] input"], year),
        (["input[name*='month' i]", "input[aria-label*='month' i]", "[data-type='month'] input"], str(int(month))),
        (["input[name*='day' i]", "input[aria-label*='day' i]", "[data-type='day'] input"], str(int(day))),
    ]
    for candidates, value in selectors:
        loc = _first_visible(page, candidates, timeout=350)
        if loc is not None:
            _human_fill(loc, value, delay=70)
            filled = True
    if not filled:
        age = max(18, min(70, datetime.now().year - int(year)))
        age_loc = _first_visible(page, ["input[name='age']", "input[id*='age' i]", "input[type='number']"], timeout=500)
        if age_loc is not None:
            _human_fill(age_loc, str(age), delay=70)
            filled = True
    # consent 复选框由页面默认值决定；未勾选的 visible checkbox 全部勾上。
    try:
        checks = page.locator("input[type='checkbox']")
        for i in range(checks.count()):
            checkbox = checks.nth(i)
            if checkbox.is_visible(timeout=150) and not checkbox.is_checked():
                checkbox.check(force=True)
                filled = True
    except Exception:
        pass
    if filled:
        _click_submit(page)
    return filled


def _page_is_chatgpt(page) -> bool:
    try:
        host = (urlsplit(_page_url(page)).hostname or "").lower().rstrip(".")
    except Exception:
        return False
    return host == "chatgpt.com" or host.endswith(".chatgpt.com")


def _goto_page(page, url: str, *, phase: str) -> object:
    """导航并把真实 HTTP 403/5xx 转成可路由的结构化错误。"""
    safe_url = _safe_url_for_log(url)
    _clear_page_edge_response(page)
    try:
        response = page.goto(url, wait_until="domcontentloaded")
    except Exception as exc:
        raise PlaywrightAuthError(
            f"页面导航失败 phase={phase} url={safe_url}: {_safe_error_text(exc, max_length=260)}",
            code="network",
            phase=phase,
        ) from exc
    try:
        status = int(getattr(response, "status", 0) or 0)
    except (TypeError, ValueError):
        status = 0
    dead_code = _response_dead_code(response)
    if dead_code:
        raise PlaywrightAuthError(
            f"页面响应包含账号状态错误 code={dead_code} phase={phase} url={safe_url}",
            code=dead_code,
            http_status=status or None,
            phase=phase,
        )
    if _response_has_edge_challenge(response) or _page_has_edge_challenge(page):
        raise PlaywrightAuthError(
            f"页面导航需要边缘挑战 phase={phase} url={safe_url}",
            code="challenge_required",
            http_status=status or None,
            phase=phase,
        )
    if status >= 400:
        code = "edge_http" if status in {403, 408, 425, 429} or status >= 500 else "http_error"
        raise PlaywrightAuthError(
            f"页面导航返回 HTTP {status} phase={phase} url={safe_url}",
            code=code,
            http_status=status,
            phase=phase,
        )
    return response


def _settle_edge_challenge(page, seconds: float) -> None:
    """给 Chromium 一小段时间执行 CF challenge 并写入 clearance cookie。"""
    delay = max(0.0, min(15.0, float(seconds or 0.0)))
    if delay <= 0:
        return
    waiter = getattr(page, "wait_for_timeout", None)
    if callable(waiter):
        try:
            waiter(int(delay * 1000))
            return
        except Exception:
            pass
    time.sleep(delay)


def _goto_page_with_edge_retry(
    page,
    url: str,
    *,
    phase: str,
    attempts: int | None = None,
) -> object:
    """重试初始页面导航上的 CF/边缘 403，保留同一浏览器上下文。"""
    total_attempts = _bounded_edge_retry_attempts(attempts)
    last_exc: PlaywrightAuthError | None = None
    for attempt in range(1, total_attempts + 1):
        try:
            response = _goto_page(page, url, phase=phase)
            if attempt > 1:
                logger.info(
                    "[Playwright] %s 页面边缘重试成功：attempt=%s/%s url=%s",
                    phase, attempt, total_attempts, _safe_url_for_log(url),
                )
            return response
        except PlaywrightAuthError as exc:
            last_exc = exc
            if exc.code not in _EDGE_RETRY_CODES or attempt >= total_attempts:
                raise
            logger.warning(
                "[Playwright] %s 页面收到边缘响应 code=%s status=%s，等待页面脚本后重试 (%s/%s)：%s",
                phase, exc.code, exc.http_status or "?", attempt, total_attempts,
                _safe_url_for_log(url),
            )
            _settle_edge_challenge(page, _edge_challenge_wait_seconds())
    raise last_exc or PlaywrightAuthError(
        f"页面边缘重试失败 phase={phase} url={_safe_url_for_log(url)}",
        code="edge_http", phase=phase,
    )


def _read_session(context, page, timeout_ms: int = 6000) -> dict:
    """使用同一 BrowserContext 的 cookie 读取 NextAuth session，不跨到外部 host。"""
    try:
        response = context.request.get(
            "https://chatgpt.com/api/auth/session",
            timeout=timeout_ms,
            headers={"accept": "application/json", "referer": "https://chatgpt.com/", "cache-control": "no-cache"},
        )
        try:
            status = int(getattr(response, "status", 0) or 0)
        except (TypeError, ValueError):
            status = 0
        dead_code = _response_dead_code(response)
        if dead_code:
            return {"_http_status": status, "_error_code": dead_code, "_phase": "session"}
        if _response_has_edge_challenge(response) or _text_has_edge_challenge(_response_text(response)):
            return {
                "_http_status": status,
                "_error_code": "challenge_required",
                "_phase": "session",
            }
        if status >= 400:
            return {"_http_status": status, "_error_code": "edge_http", "_phase": "session"}
        data = response.json()
        if isinstance(data, dict):
            data["_http_status"] = status
            return data
        return {"_http_status": status, "data": data}
    except Exception as exc:
        # context.request 失败时只允许在 ChatGPT 页面上使用相对 fetch；
        # 在 auth.openai.com 等重定向页上 fetch('/api/auth/session') 没有意义。
        if not _page_is_chatgpt(page):
            return {"_error_code": "network", "_phase": "session", "_error": _safe_error_text(exc, max_length=220)}
        try:
            data = page.evaluate("""async () => {
                const r = await fetch('https://chatgpt.com/api/auth/session', {credentials:'include', cache:'no-store'});
                const text = await r.text();
                let j = {};
                try { j = JSON.parse(text); } catch (_) { j = {}; }
                if (j && typeof j === 'object') {
                    j._http_status = r.status;
                    j._cf_mitigated = r.headers.get('cf-mitigated') || '';
                    j._body_text = text.slice(0, 4000);
                }
                return j;
            }""")
            if not isinstance(data, dict):
                return {"_error_code": "session_invalid", "_phase": "session"}
            try:
                status = int(data.get("_http_status") or 0)
            except (TypeError, ValueError):
                status = 0
            body_text = str(data.get("_body_text") or "")
            try:
                from core.openai_auth import detect_account_unusable_response_body
                dead_code = detect_account_unusable_response_body(body_text)
            except Exception:
                dead_code = ""
            data.pop("_body_text", None)
            if dead_code:
                return {"_http_status": status, "_error_code": dead_code, "_phase": "session"}
            if (
                str(data.get("_cf_mitigated") or "").strip().lower() == "challenge"
                or _text_has_edge_challenge(body_text)
            ):
                return {"_http_status": status, "_error_code": "challenge_required", "_phase": "session"}
            data.pop("_cf_mitigated", None)
            if status >= 400:
                return {"_http_status": status, "_error_code": "edge_http", "_phase": "session"}
            return data
        except Exception as page_exc:
            return {
                "_error_code": "network",
                "_phase": "session",
                "_error": _safe_error_text(f"{type(exc).__name__}; page={type(page_exc).__name__}"),
            }


def _recover_session_edge(page) -> None:
    """让真实 Chromium 重新执行一个 document 导航，再读 session。"""
    if not _page_is_chatgpt(page):
        return
    _settle_edge_challenge(page, _edge_challenge_wait_seconds())
    goto = getattr(page, "goto", None)
    if not callable(goto):
        return
    try:
        # This is a normal first-party document navigation. It gives the page's
        # own scripts a chance to establish clearance state; no token or widget
        # response is injected by this code.
        _goto_page_with_edge_retry(
            page,
            "https://chatgpt.com/",
            phase="session",
            attempts=2,
        )
    except PlaywrightAuthError as exc:
        if exc.code in _EDGE_DEAD_CODES:
            raise
        if exc.code not in _EDGE_RETRY_CODES:
            logger.debug("[Playwright] session 边缘恢复导航失败：%s", _safe_error_text(exc, max_length=180))


def _wait_session(context, page, timeout: int | None = None) -> dict:
    seconds = int(timeout or getattr(_cfg, "PLAYWRIGHT_LIVE_CHECK_TIMEOUT", 180) or 180)
    end = time.time() + max(5, seconds)
    last: dict = {}
    edge_retries = 0
    max_edge_retries = max(0, _bounded_edge_retry_attempts() - 1)
    while time.time() < end:
        last = _read_session(context, page, timeout_ms=min(8000, _timeout_ms(8, 8)))
        if isinstance(last, dict) and last.get("accessToken"):
            return last
        code = str(last.get("_error_code") or "") if isinstance(last, dict) else ""
        if code in _EDGE_DEAD_CODES:
            raise PlaywrightAuthError(
                "session endpoint 返回账号状态错误",
                code=code,
                http_status=last.get("_http_status"),
                phase="session",
            )
        if code in _EDGE_RETRY_CODES:
            if edge_retries >= max_edge_retries:
                raise PlaywrightAuthError(
                    "session endpoint 多次返回边缘挑战/错误",
                    code=code,
                    http_status=last.get("_http_status"),
                    phase="session",
                )
            edge_retries += 1
            logger.warning(
                "[Playwright] session endpoint 边缘响应 code=%s status=%s，执行页面等待并重试 (%s/%s)",
                code,
                last.get("_http_status") or "?",
                edge_retries,
                max_edge_retries,
            )
            _recover_session_edge(page)
            continue
        if _page_is_chatgpt(page):
            time.sleep(0.7)
        else:
            time.sleep(0.4)
    status = last.get("_http_status") if isinstance(last, dict) else None
    code = str(last.get("_error_code") or "session_timeout") if isinstance(last, dict) else "session_timeout"
    try:
        status = int(status) if status else None
    except (TypeError, ValueError):
        status = None
    raise PlaywrightAuthError(
        f"等待 /api/auth/session accessToken 超时 code={code}",
        code=code,
        http_status=status,
        phase="session",
    )


def _effective_proxy(proxy: str | None) -> str:
    """统一 Playwright 与 BrowserSession 的代理语义：None=代理池，空串=直连。"""
    if proxy is None:
        try:
            from config import proxy as proxy_cfg
            return str(proxy_cfg.pick_proxy() or "").strip()
        except Exception as exc:
            raise PlaywrightAuthError(
                f"无法从代理池选择代理：{_safe_error_text(exc)}",
                code="proxy_config",
                phase="proxy",
            ) from exc
    raw = str(proxy or "").strip()
    if not raw:
        return ""
    try:
        return normalize_proxy_url(raw)
    except Exception as exc:
        raise PlaywrightAuthError(
            f"代理格式无效：{redact_proxy_url(raw)}",
            code="proxy_config",
            phase="proxy",
        ) from exc


def _proxy_for_playwright(proxy: str | None) -> dict | None:
    """将项目代理 URL 转成 Playwright launch proxy 参数。"""
    text = _effective_proxy(proxy)
    if not text:
        return None
    parsed = urlsplit(text)
    if not parsed.hostname or not parsed.port:
        raise PlaywrightAuthError("代理格式无效", code="proxy_config", phase="proxy")
    scheme = parsed.scheme.lower()
    # Chromium 接受 http/https/socks5；socks5h 的 DNS 由浏览器 SOCKS 代理解析。
    if scheme == "socks5h":
        scheme = "socks5"
    if scheme not in {"http", "https", "socks5"}:
        raise PlaywrightAuthError(f"Playwright 不支持代理协议: {parsed.scheme}", code="proxy_config", phase="proxy")
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    server = f"{scheme}://{host}:{parsed.port}"
    result: dict[str, str] = {"server": server}
    if parsed.username:
        from urllib.parse import unquote
        result["username"] = unquote(parsed.username)
    if parsed.password:
        from urllib.parse import unquote
        result["password"] = unquote(parsed.password)
    return result


def _profile_directory(profile_key: str | None) -> tuple[Path | None, str | None, threading.Lock | None]:
    """为可选持久化上下文分配按账号隔离的目录和进程内锁。"""
    root = str(getattr(_cfg, "PLAYWRIGHT_USER_DATA_DIR", "") or "").strip()
    key = str(profile_key or "").strip()
    if not root or not key:
        return None, None, None
    digest = hashlib.sha256(key.encode("utf-8", errors="replace")).hexdigest()[:24]
    directory = Path(root).expanduser().resolve() / digest
    lock_key = str(directory)
    with _PROFILE_LOCKS_GUARD:
        lock = _PROFILE_LOCKS.setdefault(lock_key, threading.Lock())
    if not lock.acquire(timeout=30):
        raise PlaywrightAuthError("Playwright 账号浏览器目录正在被其它任务使用", code="profile_busy", phase="launch")
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except Exception:
        lock.release()
        raise
    return directory, lock_key, lock


def _release_profile_lock(context=None, lock: threading.Lock | None = None) -> None:
    if context is not None:
        held = _HELD_PROFILE_LOCKS.pop(id(context), None)
        if held:
            _, lock = held
    if lock is not None and lock.locked():
        lock.release()


def _close_playwright_objects(browser=None, context=None, page=None, lock=None) -> None:
    """幂等关闭 Playwright 对象；必须在 sync_playwright manager 内调用。"""
    try:
        if page is not None and not page.is_closed():
            page.close()
    except Exception:
        pass
    try:
        if context is not None:
            context.close()
    except Exception:
        pass
    try:
        if browser is not None:
            browser.close()
    except Exception:
        pass
    if context is not None:
        _PERSISTENT_CONTEXTS.discard(id(context))
    _release_profile_lock(context=context, lock=lock)


def _launch_context(p, proxy: str | None = None, *, profile_key: str | None = None):
    browser_name = str(getattr(_cfg, "PLAYWRIGHT_BROWSER", "chromium") or "chromium").lower()
    browser_type = getattr(p, browser_name, None)
    if browser_type is None:
        raise PlaywrightAuthError(f"不支持 PLAYWRIGHT_BROWSER={browser_name!r}", code="browser_config", phase="launch")
    launch_kwargs: dict[str, Any] = {
        "headless": bool(getattr(_cfg, "PLAYWRIGHT_HEADLESS", True)),
    }
    executable = str(getattr(_cfg, "PLAYWRIGHT_EXECUTABLE_PATH", "") or "").strip()
    if executable:
        launch_kwargs["executable_path"] = executable
    proxy_config = _proxy_for_playwright(proxy)
    if proxy_config:
        launch_kwargs["proxy"] = proxy_config

    # 让本地 Chromium 至少复用协议层的语言、时区、UA 和屏幕画像；
    # Playwright_LOCALE/TIMEZONE_ID 留空时跟随 config.browser 当前画像。
    from config import browser as browser_cfg
    locale = str(getattr(_cfg, "PLAYWRIGHT_LOCALE", "") or "").strip()
    locale = locale or str(getattr(browser_cfg, "NAVIGATOR_LANGUAGE", "en-US") or "en-US")
    timezone = str(getattr(_cfg, "PLAYWRIGHT_TIMEZONE_ID", "") or "").strip()
    timezone = timezone or str(getattr(browser_cfg, "TIMEZONE_IANA", "") or "").strip()
    viewport_width = int(getattr(_cfg, "PLAYWRIGHT_VIEWPORT_WIDTH", 0) or getattr(browser_cfg, "SCREEN_WIDTH", 1440) or 1440)
    viewport_height = int(getattr(_cfg, "PLAYWRIGHT_VIEWPORT_HEIGHT", 0) or getattr(browser_cfg, "SCREEN_HEIGHT", 900) or 900)
    context_kwargs: dict[str, Any] = {
        "locale": locale,
        "viewport": {"width": viewport_width, "height": viewport_height},
        "device_scale_factor": float(getattr(_cfg, "PLAYWRIGHT_DEVICE_SCALE_FACTOR", 1.0) or 1.0),
    }
    user_agent = str(getattr(browser_cfg, "USER_AGENT", "") or "").strip()
    if user_agent:
        context_kwargs["user_agent"] = user_agent
    accept_language = str(getattr(browser_cfg, "ACCEPT_LANGUAGE", "") or "").strip()
    if accept_language:
        context_kwargs["extra_http_headers"] = {"Accept-Language": accept_language}
    if timezone:
        context_kwargs["timezone_id"] = timezone

    directory, _, profile_lock = _profile_directory(profile_key)
    browser = context = page = None
    try:
        if directory is not None:
            persistent_kwargs = dict(launch_kwargs)
            persistent_kwargs.pop("headless", None)
            persistent_kwargs["headless"] = bool(getattr(_cfg, "PLAYWRIGHT_HEADLESS", True))
            context = browser_type.launch_persistent_context(str(directory), **persistent_kwargs, **context_kwargs)
            browser = getattr(context, "browser", None)
            _PERSISTENT_CONTEXTS.add(id(context))
        else:
            browser = browser_type.launch(**launch_kwargs)
            context = browser.new_context(**context_kwargs)
        if profile_lock is not None and context is not None:
            _HELD_PROFILE_LOCKS[id(context)] = (str(directory or ""), profile_lock)
        try:
            from playwright_stealth import Stealth
            Stealth(init_scripts_only=True).apply_stealth_sync(context)
        except Exception as exc:
            logger.debug("[Playwright] stealth 初始化跳过：%s: %s", type(exc).__name__, _safe_error_text(exc, max_length=120))
        page = context.new_page()
        timeout = _timeout_ms()
        page.set_default_timeout(timeout)
        page.set_default_navigation_timeout(_navigation_timeout_ms())
        _install_edge_response_tracker(page)
        return browser, context, page
    except Exception:
        _close_playwright_objects(browser=browser, context=context, page=page, lock=profile_lock)
        if context is None and profile_lock is not None and profile_lock.locked():
            profile_lock.release()
        raise


@contextmanager
def _managed_playwright_session(proxy: str | None = None, *, profile_key: str | None = None):
    """在同一线程内创建并关闭 Playwright 对象，且在 manager 退出前 teardown。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise PlaywrightAuthError("缺少 playwright 依赖，请安装 requirements.txt", code="dependency", phase="launch") from exc
    browser = context = page = None
    with sync_playwright() as p:
        try:
            browser, context, page = _launch_context(p, proxy=proxy, profile_key=profile_key)
            yield browser, context, page
        finally:
            # 对象必须在 sync manager 退出前 teardown。KEEP_BROWSER_OPEN 只保留
            # 页面一小段时间供人工观察，但不能让 profile lock 泄漏到后续任务。
            _close_playwright_objects(browser=browser, context=context, page=page)


def _wait_otp_or_totp(
    page,
    email: str,
    after_ts: float,
    *,
    allow_totp: bool = True,
    email_source: str | None = None,
) -> None:
    state = _wait_state(page, {"otp", "mfa", "chatgpt", "profile", "login_password", "password"}, timeout=35)
    if state in {"login_password", "password"}:
        raise PlaywrightAuthError("邮箱进入登录密码页，未找到可用的一次性验证码入口", code="password_required", phase="auth")
    if state in {"chatgpt", "profile"}:
        return
    code = _account_totp_code(email) if allow_totp and state == "mfa" else _wait_mail_otp(email, after_ts, email_source=email_source)
    _fill_otp(page, code)
    _submit_otp(page)


def check_playwright_liveness(
    email: str,
    proxy: str | None = None,
    *,
    email_source: str | None = None,
) -> dict:
    """统一返回查活结果，供后台队列调用。

    ``run_playwright_liveness`` 保留原始 session 结果契约，便于调试；后台入口
    在这里补齐 live/deactivated/failed 状态，并且不把 token 写入日志。
    """
    checked_at = datetime.now().isoformat(timespec="seconds")
    # 在代理解析失败时也不能把传入的 user:password 原样写入结果。
    effective_proxy = ""
    try:
        # 只在 wrapper 中选择一次代理；run_playwright_liveness 收到标准 URL 后
        # 不会重新抽取代理池，避免结果记录的 route 与实际出口不一致。
        effective_proxy = _effective_proxy(proxy)
        session_info = run_playwright_liveness(email, proxy=effective_proxy, email_source=email_source)
        token = str((session_info or {}).get("accessToken") or "").strip()
        if not token:
            status_code = (session_info or {}).get("_http_status") if isinstance(session_info, dict) else None
            raise PlaywrightAuthError(
                "浏览器登录完成但 session 缺少 accessToken",
                code="session_missing_token",
                http_status=status_code,
                phase="session",
            )
        user = (session_info or {}).get("user") or {}
        account = (session_info or {}).get("account") or {}
        return {
            "ok": True,
            "status": "live",
            "checked_at": checked_at,
            "access_token": token,
            "session": session_info,
            "proxy_used": redact_proxy_url(effective_proxy) or None,
            "fingerprint": {"driver": "playwright"},
            "fingerprint_text": f"driver=playwright proxy={'configured' if effective_proxy else 'direct'}",
            "driver": "playwright",
            "user": user,
            "account": account,
        }
    except Exception as exc:
        raw_text = str(exc or "")
        text = _safe_error_text(raw_text)
        lower = raw_text.lower()
        exception_code = str(getattr(exc, "code", "") or "").lower()
        dead_code = exception_code if exception_code in _EDGE_DEAD_CODES else ""
        if not dead_code:
            try:
                from core.openai_auth import detect_account_unusable_text
                dead_code = detect_account_unusable_text(raw_text)
            except Exception:
                dead_code = ""
        dead = bool(dead_code) or any(marker in lower for marker in (
            "account_deactivated", "account deleted", "account banned",
            "account has been deactivated", "账号已停用", "账号已禁用", "账号已删除",
        ))
        if dead:
            status = "deactivated"
            error_code = dead_code or "account_deactivated"
        else:
            status = "failed"
            error_code = exception_code
        http_status = getattr(exc, "http_status", None)
        phase = str(getattr(exc, "phase", "") or "") or None
        return {
            "ok": False,
            "status": status,
            "checked_at": checked_at,
            "error": f"{type(exc).__name__}: {text}",
            "error_code": error_code,
            "http_status": http_status,
            "phase": phase,
            "driver": "playwright",
            "proxy_used": redact_proxy_url(effective_proxy) or None,
        }


def run_playwright_liveness(
    email: str,
    proxy: str | None = None,
    *,
    email_source: str | None = None,
) -> dict:
    """通过独立 Chromium context 登录已注册账号并返回最新 ChatGPT session。"""
    email = str(email or "").strip()
    if not email:
        raise ValueError("email 不能为空")
    selected_proxy = _effective_proxy(proxy)
    started = time.time()
    profile_key = f"liveness:{email.lower()}"
    with _managed_playwright_session(proxy=selected_proxy, profile_key=profile_key) as (_, context, page):
        start_url = _validated_start_url()
        # 与协议查活保持一致：先让真实 Chromium 访问首页，给 CF/边缘上下文
        # 一个正常的 document 导航，再进入 auth/login。初始 403 由同一 context
        # 内有限重试，保留 challenge 产生的 cookie。
        logger.info("[Playwright查活] 首页预热：https://chatgpt.com/ email=%s", email)
        _goto_page_with_edge_retry(page, "https://chatgpt.com/", phase="homepage")
        _accept_cookies(page)
        logger.info("[Playwright查活] 打开登录页：%s email=%s", _safe_url_for_log(start_url), email)
        _goto_page_with_edge_retry(page, start_url, phase="login")
        _accept_cookies(page)
        _click_email_entry(page)
        email_loc = _email_input(page)
        if email_loc is None:
            raise PlaywrightAuthError(
                f"找不到邮箱输入框 url={_safe_url_for_log(_page_url(page))}",
                code="email_input",
                phase="email",
            )
        _human_fill(email_loc, email, delay=55)
        after_ts = time.time()
        if not _click_submit(page):
            raise PlaywrightAuthError("邮箱提交失败", code="submit_email", phase="email")
        state = _wait_state(page, {"login_password", "password", "otp", "mfa", "profile", "chatgpt"}, timeout=35)
        logger.info("[Playwright查活] 邮箱提交后状态=%s url=%s", state, _safe_url_for_log(_page_url(page)))

        password = _account_registration_password(email) if bool(getattr(_cfg, "PLAYWRIGHT_ALLOW_PASSWORD_LOGIN", True)) else ""
        if state in {"login_password", "password"}:
            if not password:
                if state == "login_password" and _click_passwordless_if_present(page):
                    state = _wait_state(page, {"otp", "mfa", "profile", "chatgpt", "login_password", "password"}, timeout=25)
                if state in {"login_password", "password"}:
                    raise PlaywrightAuthError("账号需要登录密码，但本地没有 registration_password", code="password_required", phase="password")
            elif not _fill_password(page, password):
                raise PlaywrightAuthError("找不到登录密码输入框", code="password_input", phase="password")
            else:
                state = _wait_state(page, {"otp", "mfa", "profile", "chatgpt", "login_password", "password"}, timeout=35)
                if state in {"login_password", "password"}:
                    raise PlaywrightAuthError("登录密码提交后仍停留在密码页，请检查账号密码", code="password_invalid", phase="password")
        if state in {"otp", "mfa"}:
            _wait_otp_or_totp(page, email, after_ts, allow_totp=True, email_source=email_source)
        follow_state = _wait_state(page, {"otp", "mfa", "profile", "chatgpt", "login_password", "password"}, timeout=35)
        if follow_state in {"login_password", "password"}:
            if not password or not _fill_password(page, password):
                raise PlaywrightAuthError("密码登录未完成，请检查 registration_password", code="password_invalid", phase="password")
            follow_state = _wait_state(page, {"otp", "mfa", "profile", "chatgpt"}, timeout=35)
        if follow_state in {"otp", "mfa"}:
            _wait_otp_or_totp(page, email, after_ts, allow_totp=True, email_source=email_source)
        session_info = _wait_session(context, page)
        logger.info("[Playwright查活] 成功 email=%s elapsed=%.1fs", email, time.time() - started)
        return session_info


def run_playwright_registration(
    email: str | None,
    name: str,
    birthday: str | None = None,
    proxy: str | None = None,
    otp_code: str | None = None,
    batch_dir: Path | None = None,
    on_email_acquired: Callable[[str], None] | None = None,
) -> dict:
    """通过本地 Playwright 完成邮箱 OTP 注册并落库。"""
    email = str(email or "").strip() or None
    birthday = birthday or generate_random_birthday()
    browser = context = page = None
    acknowledged = False
    current_email = email
    openai_password: str | None = None
    try:
        selected_proxy = _effective_proxy(proxy)
        profile_key = f"registration:{email or uuid.uuid4()}"
        with _managed_playwright_session(proxy=selected_proxy, profile_key=profile_key) as (_, context, page):
            start_url = _validated_start_url()
            _goto_page(page, start_url, phase="registration")
            _accept_cookies(page)
            _click_email_entry(page)
            email_loc = _email_input(page)
            if email_loc is None:
                raise PlaywrightAuthError(f"找不到邮箱输入框：url={_page_url(page)}")
            if not current_email:
                current_email = acquire_email_after_input(None)
                if on_email_acquired:
                    on_email_acquired(current_email)
            _human_fill(email_loc, current_email, delay=55)
            after_ts = time.time()
            if not _click_submit(page):
                raise PlaywrightAuthError("邮箱提交失败")
            state = _wait_state(page, {"password", "otp", "mfa", "profile", "chatgpt", "login_password"}, timeout=35)
            if state == "login_password":
                # 邮箱已存在时不继续消耗注册素材。
                raise PlaywrightAuthError("邮箱已注册，页面进入登录密码路径")
            if state == "password":
                from core.browser_use_registration import _registration_password
                openai_password = _registration_password()
                if not _fill_password(page, openai_password):
                    raise PlaywrightAuthError("找不到注册密码输入框")
                state = _wait_state(page, {"otp", "mfa", "profile", "chatgpt"}, timeout=35)
            if state == "otp":
                code = str(otp_code or "").strip() or _wait_mail_otp(current_email, after_ts, email_source=resolve_email_source(current_email))
                _fill_otp(page, code)
                _submit_otp(page)
            elif state == "mfa":
                code = _account_totp_code(current_email)
                if not code:
                    raise PlaywrightAuthError("新注册流程意外进入 MFA，但没有 TOTP secret")
                _fill_otp(page, code)
                _submit_otp(page)
            state = _wait_state(page, {"profile", "chatgpt", "otp", "mfa"}, timeout=45)
            if state in {"otp", "mfa"}:
                code = str(otp_code or "").strip() or (_account_totp_code(current_email) if state == "mfa" else _wait_mail_otp(current_email, after_ts, email_source=resolve_email_source(current_email)))
                _fill_otp(page, code)
                _submit_otp(page)
            if _auth_state(page) == "profile":
                if not _fill_profile(page, name, birthday):
                    raise PlaywrightAuthError("找不到资料页字段")
                acknowledged = True
            session_info = _wait_session(context, page, timeout=120)
            access_token = str(session_info.get("accessToken") or "")
            if not access_token:
                raise PlaywrightAuthError("注册结束但未拿到 accessToken")
            acknowledged = True
            account_id = save_account_data(
                email=current_email,
                access_token=access_token,
                totp_secret=None,
                email_source=resolve_email_source(current_email),
                proxy_used=redact_proxy_url(selected_proxy) or None,
                batch_dir=batch_dir,
                extra={
                    "user": session_info.get("user"),
                    "account": session_info.get("account"),
                    "expires": session_info.get("expires"),
                    "registration_driver": "playwright",
                    "registration_password": openai_password,
                },
            )
            return {
                "success": True,
                "email": current_email,
                "account_id": account_id,
                "access_token": access_token,
                "totp_secret": None,
                "network_traffic": None,
                "error": None,
            }
    except Exception as exc:
        logger.error("[Playwright注册] 失败：%s: %s", current_email or email, _safe_error_text(exc))
        try:
            if current_email:
                from core.email_provider import release_email
                release_email(current_email, status="failed" if acknowledged else "available", note=f"Playwright注册失败: {_safe_error_text(exc, max_length=180)}")
        except Exception:
            pass
        return {
            "success": False,
            "email": current_email,
            "network_traffic": None,
            "error": f"{type(exc).__name__}: {_safe_error_text(exc, max_length=300)}",
            "error_code": str(getattr(exc, "code", "") or ""),
            "http_status": getattr(exc, "http_status", None),
            "phase": str(getattr(exc, "phase", "") or "") or None,
        }
