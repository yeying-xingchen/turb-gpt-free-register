# -*- coding: utf-8 -*-
"""在独立 CloakBrowser 上重新登录已有账号，获取 ChatGPT Web Session。"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from config import cloakbrowser as cfg
from core.cloakbrowser_driver import build_cloak_driver
from core.email_provider import wait_for_otp
from core.openai_auth import (
    AccountUnusableError, detect_account_unusable_text,
    detect_account_unusable_response_body,
)

logger = logging.getLogger(__name__)
_EMAIL_SELECTORS = ["input[type='email']", "input[name='email']", "input[name='username']"]
_PASSWORD_SELECTORS = ["input[type='password']", "input[autocomplete='current-password']"]
_INVALID_CODE_HINTS = (
    "invalid_otp", "invalid_totp", "invalid_code", "incorrect_code", "expired_code", "otp_expired",
    "invalid code", "incorrect code", "expired code", "code is invalid", "code is incorrect",
    "code has expired", "验证码错误", "验证码无效", "验证码已过期",
)

# 在页面内一次判定所有候选元素是否可见可用；返回布尔数组，顺序与 locator 一致。
_VISIBLE_JS = """(elements) => elements.map(el => {
  const box = el.getBoundingClientRect();
  const shown = !!(box.width || box.height) && getComputedStyle(el).visibility !== 'hidden';
  return shown && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
})"""

# 一次抓取登录页上所有可选按钮的可见性、文案与关键属性，替代逐元素
# get_attribute/inner_text（原来每个候选 7 次 CDP 往返）。
_CHOICE_SCAN_JS = """(elements) => elements.map(el => {
  const box = el.getBoundingClientRect();
  const shown = !!(box.width || box.height) && getComputedStyle(el).visibility !== 'hidden'
    && !el.disabled && el.getAttribute('aria-disabled') !== 'true';
  if (!shown) return {visible: false};
  const pick = (name) => el.getAttribute(name) || '';
  const text = (el.innerText || '').trim().slice(0, 200);
  return {
    visible: true,
    text: text,
    attrs: [pick('name'), pick('value'), pick('data-testid'), pick('data-dd-action-name')].join(' '),
    details: [pick('name'), pick('value'), pick('href'), pick('formaction'),
              pick('data-testid'), pick('data-dd-action-name'), text].join(' '),
  };
})"""

_CHOICE_SELECTOR = "button,a,[role='button'],[role='link'],input[type='submit']"

# 登录状态机轮询间隔（毫秒）：提交动作后等待跳转要快，空闲等待可以慢。
_POLL_FAST_MS = 150
_POLL_IDLE_MS = 400


def _page_state(driver) -> dict:
    # 只读取状态，不把密码、验证码、Cookie 或完整回调 URL 写入日志。
    # 这里刻意不读 document.body.innerText：innerText 会强制整页布局，在
    # chatgpt.com 这种 React 页面上单次可达几十到上百毫秒，而登录状态机每轮
    # 都要调用一次。需要正文时用 _page_text() 按需读取。
    return driver.page.evaluate(r"""() => {
      const visible = el => !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
        && getComputedStyle(el).visibility !== 'hidden' && !el.disabled;
      const has = sel => [...document.querySelectorAll(sel)].some(visible);
      const errors = [...document.querySelectorAll('[role="alert"],.react-aria-FieldError,[slot="errorMessage"],[id$="-error"]')]
        .filter(visible).map(el => el.innerText || '').filter(Boolean);
      return {url: location.href, errors,
        email: has('input[type="email"],input[name="email"],input[name="username"]'),
        password: has('input[type="password"],input[autocomplete="current-password"]'),
        code: has('input[autocomplete="one-time-code"],input[name="code"],input[inputmode="numeric"]'),
        invalid: has('input[aria-invalid="true"]'),
        mfa: [...document.querySelectorAll('form')].some(f => /mfa|totp|two-factor/i.test(f.action || ''))};
    }""") or {}


def _page_text(driver) -> str:
    """按需读取可见正文，仅用于认证错误页的废号判定。"""
    try:
        return str(driver.page.evaluate(
            "() => (document.body?.innerText || '').slice(0, 4000)"
        ) or "")
    except Exception as exc:
        if _during_navigation(exc):
            return ""
        raise


def _step(state: dict) -> str:
    parsed = urlparse(str(state.get("url") or ""))
    path = parsed.path.lower()
    # 先辨别手机与 MFA，不能把它们的 code 输入框误判成邮箱验证码。
    if parsed.hostname == "auth.openai.com":
        if "phone" in path:
            return "phone"
        if any(s in path for s in ("create-account", "about-you", "complete-profile")):
            return "incomplete"
        if state.get("mfa") or any(s in path for s in ("mfa", "totp", "two-factor")):
            return "mfa"
        if state.get("password"):
            return "password"
        if state.get("code") or "email-verification" in path:
            return "email_otp"
    if parsed.hostname not in {"chatgpt.com", "auth.openai.com"}:
        return "unknown"
    if state.get("email"):
        return "email"
    if parsed.hostname == "chatgpt.com" and not path.startswith(("/auth/", "/api/auth/callback")):
        return "session"
    return "waiting"


class _BrowserAuthError(RuntimeError):
    def __init__(self, status: int, code: str):
        super().__init__(f"浏览器登录失败：HTTP {status} {code}".strip())
        self.response = SimpleNamespace(
            status_code=status, text=json.dumps({"error": {"code": code}}),
        )


class _AuthResponses:
    """只保留认证接口的错误码，浏览器导航后也可识别明确废号。"""
    def __init__(self):
        self.dead_code = ""
        self.error = ""
        self.status = 0
        self.code = ""

    def __call__(self, response):
        parsed = urlparse(response.url)
        is_auth_api = parsed.hostname == "auth.openai.com" and parsed.path.startswith("/api/accounts/")
        is_web_auth = parsed.hostname == "chatgpt.com" and parsed.path.startswith((
            "/api/auth/csrf", "/api/auth/signin/", "/api/auth/callback/",
        ))
        if not (is_auth_api or is_web_auth) or response.status < 400:
            return
        try:
            self.dead_code = detect_account_unusable_response_body(response.text()) or self.dead_code
            data = response.json()
            error = data.get("error") if isinstance(data, dict) else None
            code = str(error.get("code") or "") if isinstance(error, dict) else ""
            # 只允许错误标识符，不记录远端返回的凭据或回调。
            code = code if code.replace("_", "").isalnum() else ""
        except Exception:
            code = ""
        self.status, self.code = response.status, code
        self.error = f"HTTP {response.status}" + (f" {code}" if code else "")


def _check_error(state: dict, responses: _AuthResponses, driver=None) -> None:
    dead = responses.dead_code
    parsed = urlparse(str(state.get("url") or ""))
    auth_page = parsed.hostname == "auth.openai.com"
    web_auth_page = parsed.hostname == "chatgpt.com" and parsed.path.startswith(("/auth/", "/api/auth/"))
    if not dead and (auth_page or web_auth_page):
        query = parse_qs(parsed.query)
        for key in ("error", "error_code", "code"):
            for value in query.get(key, []):
                dead = detect_account_unusable_response_body(json.dumps({"error": {"code": value}}))
                if dead:
                    break
            if dead:
                break
    if not dead and auth_page:
        # 只读认证错误提示；邮箱、ChatGPT 聊天标题和普通正文不是账号状态证据。
        errors = " ".join(state.get("errors") or [])
        if parsed.path.rstrip("/").endswith("/error"):
            text = state.get("text")
            if text is None and driver is not None:
                # 正文只在认证错误页按需读取，避免登录轮询每轮都触发布局。
                try:
                    text = _page_text(driver)
                except Exception:
                    text = ""
            errors += " " + str(text or "")
        errors = re.sub(r"\S+@\S+", "", errors)
        dead = detect_account_unusable_text(errors)
    if dead:
        raise AccountUnusableError(f"账号已废（{dead}）", error_code=dead)


def _read_session(driver) -> dict | None:
    try:
        result = driver.page.evaluate("""async () => {
          if (location.hostname !== 'chatgpt.com') return null;
          const response = await fetch('/api/auth/session', {
            credentials: 'include', cache: 'no-store', signal: AbortSignal.timeout(10000)
          });
          if (!response.ok) throw new Error('Session HTTP ' + response.status);
          return await response.json();
        }""")
    except Exception as exc:
        if _during_navigation(exc):
            return None
        raise
    return result if isinstance(result, dict) and result.get("accessToken") else None


def _during_navigation(exc: Exception) -> bool:
    return any(hint in str(exc).lower() for hint in (
        "execution context was destroyed", "cannot find context with specified id",
        "most likely because of a navigation",
    ))


def _interaction_error(exc: Exception) -> RuntimeError:
    # Playwright 的 fill call log 含原文密码/OTP，绝不向日志或任务结果透传。
    text = str(exc).lower()
    if _during_navigation(exc):
        return RuntimeError("Cloak execution context was destroyed during navigation")
    if "timeout" in text or "timed out" in text or isinstance(exc, TimeoutError):
        return RuntimeError("Cloak 页面交互 timeout")
    if "closed" in text or "connection" in text:
        return RuntimeError("Cloak 浏览器连接 closed")
    # 只记录异常类型名，便于定位 humanize/选择器类问题且不会带出凭据。
    logger.warning("[Cloak查活] 未分类的页面交互异常：%s", type(exc).__name__)
    return RuntimeError("Cloak 页面交互失败")


def _visible(scope, selector: str) -> list:
    """返回可见且可用的元素。

    逐个 ``is_visible()/is_enabled()/get_attribute()`` 会产生 3N 次 CDP 往返：
    登录页上 ``button,a,[role=button],[role=link]`` 这类宽选择器动辄上百个节点，
    一次调用就要上千次往返（重发验证码按钮的轮询循环里尤其明显）。这里先用一次
    ``evaluate_all`` 在页面内批量判定，只有命中的元素才创建 Locator。
    """
    candidates = scope.locator(selector)
    try:
        flags = candidates.evaluate_all(_VISIBLE_JS)
    except Exception:
        flags = None
    if isinstance(flags, list):
        return [candidates.nth(index) for index, ok in enumerate(flags) if ok]
    # 回退路径：驱动或页面不支持 evaluate_all 时，保持逐元素判定。
    return [el for el in (candidates.nth(i) for i in range(candidates.count()))
            if el.is_visible() and el.is_enabled() and el.get_attribute("aria-disabled") != "true"]


def _wait_visible(scope, selector: str, timeout: float) -> list:
    """轮询等待可见元素，替代 Playwright 的 ``:visible`` 伪类。

    Cloak 的 humanize 层（默认开启）会把 ``Locator.fill/click`` 转发到隔离世界
    解析选择器，那里只支持 CSS/text=/xpath=/get_by_* 以及末尾的 ``.first``/
    ``.nth()``/``.last``，明确拒绝 ``:visible`` 与 ``>>`` 链式定位，命中时抛
    ``UnsupportedHumanizeSelectorError``，最终表现为“Cloak 页面交互失败”。
    这里改用原生 ``is_visible()`` 轮询，既避开该限制，又保留超时等待语义。
    """
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        elements = _visible(scope, selector)
        if elements:
            return elements
        if time.monotonic() >= deadline:
            return []
        scope.wait_for_timeout(100)


def _pin(driver, locator):
    # Locator 会在导航后重新定位；唯一 DOM 标记使它只指向本次页面的元素。
    token = uuid.uuid4().hex
    locator.evaluate("(el, token) => el.setAttribute('data-cloak-live-target', token)", token)
    return driver.page.locator(f'[data-cloak-live-target="{token}"]')


def _input_form(driver, selectors: list[str]):
    inputs = _visible(driver.page, ",".join(selectors))
    if not inputs:
        return None
    form = inputs[0].locator("xpath=ancestor::form[1]")
    return _pin(driver, form) if form.count() else None


def _type_any(driver, selectors: list[str], value: str, timeout: int | None = None, clear: bool = True) -> bool:
    wait = timeout or 30
    try:
        inputs = _wait_visible(driver.page, ",".join(selectors), wait)
        if not inputs:
            raise TimeoutError(f"未找到可输入元素：{','.join(selectors)}")
        # 查活只做完整输入，不沿用 Selenium send_keys 的逐段追加语义。
        inputs[0].fill(value, timeout=wait * 1000)
        return True
    except Exception as exc:
        if _during_navigation(exc):
            return False
        raise _interaction_error(exc) from None


def _maybe_accept(driver) -> None:
    try:
        buttons = _visible(driver.page, "button#onetrust-accept-btn-handler,button[data-testid='cookie-accept'],button[data-testid='accept-cookies']")
        if buttons:
            buttons[0].click(timeout=1000)
    except Exception:
        pass


def _button_details(button) -> str:
    return " ".join(str(button.get_attribute(name) or "") for name in
                    ("name", "value", "href", "formaction", "data-testid", "data-dd-action-name")) + " " + button.inner_text()


def _registration_target(text: str) -> bool:
    return bool(re.search(r"sign[\s_-]*up|register|registration|create[\s/_-]*account|注册|註冊", text, re.I))


def _submit_auth_form(driver, selectors: list[str], *, form=None, expected_url: str | None = None) -> bool:
    try:
        state = _page_state(driver)
        if _step(state) not in {"email", "password", "email_otp", "mfa"}:
            return False
        if expected_url is not None and state.get("url") != expected_url:
            return False
        form = form if form is not None else _input_form(driver, selectors)
        if form is None or not form.count():
            return False
        action = form.get_attribute("action") or ""
        if _registration_target(action):
            return False
        selector = "button[type='submit'],input[type='submit'],button:not([type])"
        candidates = form.locator(selector)
        try:
            scanned = candidates.evaluate_all(_CHOICE_SCAN_JS)
        except Exception:
            scanned = None
        if isinstance(scanned, list):
            buttons = ((candidates.nth(i), str(item.get("details") or ""))
                       for i, item in enumerate(scanned)
                       if isinstance(item, dict) and item.get("visible"))
        else:
            buttons = ((button, _button_details(button)) for button in _visible(form, selector))
        for button, details in buttons:
            if _registration_target(details) or re.search(r"resend|passwordless|重新发送|重发", details, re.I):
                continue
            _pin(driver, button).click(timeout=3000)
            return True
        return False
    except Exception as exc:
        if _during_navigation(exc):
            return False
        raise _interaction_error(exc) from None


def _submit_email_step(driver, email: str | None = None) -> None:
    if not _submit_auth_form(driver, _EMAIL_SELECTORS):
        if _step(_page_state(driver)) != "email":
            return
        raise RuntimeError("找不到邮箱登录提交按钮")


def _choice_patterns(resend: bool) -> tuple[str, str]:
    text_pattern = (r"resend|send\s+(?:a\s+)?new\s+code|send\s+again|重新发送|重发|再送信" if resend else
                    r"(?:use|continue with|log\s?in with).{0,12}one[- ]time code|使用一次性验证码|使用一次性驗證碼|メールでコード|ワンタイムコード")
    attr_pattern = r"resend|send_new_code" if resend else r"passwordless_login_send_otp|passwordless_send_otp"
    return text_pattern, attr_pattern


def _pick_choice_index(scanned: list, text_pattern: str, attr_pattern: str) -> int | None:
    """按原语义挑选候选：优先属性命中（多个命中取最后一个），否则取第一个文案命中。"""
    attr_hit: int | None = None
    text_hit: int | None = None
    for index, item in enumerate(scanned):
        if not isinstance(item, dict) or not item.get("visible"):
            continue
        if _registration_target(str(item.get("details") or "")):
            continue
        if re.search(attr_pattern, str(item.get("attrs") or ""), re.I):
            attr_hit = index
        elif text_hit is None and re.search(text_pattern, str(item.get("text") or ""), re.I):
            text_hit = index
    return attr_hit if attr_hit is not None else text_hit


def _click_auth_choice(driver, *, resend: bool) -> bool:
    state = _page_state(driver)
    if _step(state) != ("email_otp" if resend else "password"):
        return False
    text_pattern, attr_pattern = _choice_patterns(resend)
    candidates = driver.page.locator(_CHOICE_SELECTOR)
    try:
        scanned = candidates.evaluate_all(_CHOICE_SCAN_JS)
    except Exception:
        scanned = None
    if isinstance(scanned, list):
        picked = _pick_choice_index(scanned, text_pattern, attr_pattern)
        if picked is None:
            return False
        _pin(driver, candidates.nth(picked)).click(timeout=3000)
        return True
    # 回退路径：保持原逐元素实现，兼容不支持 evaluate_all 的驱动。
    choices = []
    for button in _visible(driver.page, _CHOICE_SELECTOR):
        details = _button_details(button)
        if _registration_target(details):
            continue
        attrs = " ".join(str(button.get_attribute(k) or "") for k in ("name", "value", "data-testid", "data-dd-action-name"))
        if re.search(attr_pattern, attrs, re.I):
            choices.insert(0, button)
        elif re.search(text_pattern, button.inner_text(), re.I):
            choices.append(button)
    if not choices:
        return False
    _pin(driver, choices[0]).click(timeout=3000)
    return True


def _click_passwordless_signup_if_present(driver) -> dict:
    # 保留调用名兼容流程测试，但只允许登录 OTP，明确拒绝注册按钮。
    try:
        return {"ok": _click_auth_choice(driver, resend=False)}
    except Exception as exc:
        raise _interaction_error(exc) from None


def _click_resend_email_otp(driver, timeout: int = 20) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if _click_auth_choice(driver, resend=True):
                return {"ok": True}
        except Exception as exc:
            raise _interaction_error(exc) from None
        driver.page.wait_for_timeout(250)
    raise RuntimeError("找不到可点击的重新发送验证码按钮")


_CODE_SELECTORS = ["input[autocomplete='one-time-code']", "input[name='code']", "input[inputmode='numeric']", "input[type='tel']"]


def _submit_code(driver, code: str) -> None:
    state = _page_state(driver)
    if _step(state) not in {"email_otp", "mfa"}:
        return
    try:
        if not _wait_visible(driver.page, ",".join(_CODE_SELECTORS), 10):
            raise TimeoutError("验证码输入框未出现")
        current = _page_state(driver)
        if current.get("url") != state.get("url") or _step(current) != _step(state):
            return
        form = _input_form(driver, _CODE_SELECTORS)
        inputs = _visible(form if form is not None else driver.page, ",".join(_CODE_SELECTORS))
        inputs = [_pin(driver, el) for el in inputs]
        if len(inputs) == 1:
            inputs[0].fill(code, timeout=10000)
        elif len(inputs) == len(code):
            for el in inputs:
                el.fill("", timeout=10000)
            for el, digit in zip(inputs, code):
                el.fill(digit, timeout=10000)
        else:
            raise RuntimeError("找不到匹配的验证码输入框")
        current = _page_state(driver)
        if current.get("url") != state.get("url") or _step(current) != _step(state):
            return
        # 自动提交后只允许操作原来的 form；不能重新定位到 MFA/注册资料页。
        if form is not None:
            _submit_auth_form(driver, _CODE_SELECTORS, form=form, expected_url=state.get("url"))
    except Exception as exc:
        if _during_navigation(exc):
            return
        raise _interaction_error(exc) from None


def _login(driver, email: str, *, email_source: str | None, responses: _AuthResponses) -> dict:
    from core.account_liveness import _account_registration_password, _totp_for_account, _fresh_totp_code

    timeout = max(30, int(getattr(cfg, "CLOAK_SELENIUM_TIMEOUT", 90) or 90))
    driver.get("https://chatgpt.com/auth/login")
    _maybe_accept(driver)
    otp_after_ts = time.time()
    deadline = time.monotonic() + max(300, timeout * 3)
    last_step = active_step = ""
    submitted = False
    step_deadline = time.monotonic() + timeout
    email_attempts = mfa_attempts = email_submits = 0
    used_email_codes: set[str] = set()
    used_totp_codes: set[str] = set()
    next_session_read = 0.0
    session_retry_delay = 0.5
    while time.monotonic() < deadline:
        try:
            state = _page_state(driver)
        except Exception as exc:
            if not _during_navigation(exc):
                raise
            driver.page.wait_for_timeout(500)
            continue
        _check_error(state, responses, driver)
        step = _step(state)
        if step != last_step:
            logger.info("[Cloak查活] 登录阶段：%s", step)
            last_step = step
            # 重新进入 Session 阶段意味着登录态可能已变化，立即尝试读取。
            next_session_read, session_retry_delay = 0.0, 0.5
        if step not in {"waiting", "unknown"} and step != active_step:
            active_step, submitted = step, False
            step_deadline = time.monotonic() + timeout
        if step == "phone":
            raise RuntimeError("账号要求手机号验证，请先完成手机号验证后再查活")
        if step == "incomplete":
            raise RuntimeError("账号要求注册或资料补全，请先完成后再查活")

        error_text = responses.error + " " + " ".join(state.get("errors") or [])
        invalid_code = any(hint in error_text.lower() for hint in _INVALID_CODE_HINTS)
        if submitted and step in {"email_otp", "mfa"} and (invalid_code or state.get("invalid")):
            if step == "email_otp":
                if email_attempts >= 3:
                    raise RuntimeError("邮箱验证码连续错误/过期，已达到最大重试次数")
                otp_after_ts = time.time()
                responses.error = ""
                _click_resend_email_otp(driver, timeout=min(timeout, 20))
            elif mfa_attempts >= 2:
                raise RuntimeError("TOTP 验证失败，请检查 2FA 密钥和系统时间")
            submitted = False
            responses.error = ""
        elif responses.error:
            raise _BrowserAuthError(responses.status, responses.code)
        elif submitted and (state.get("errors") or state.get("invalid")):
            raise RuntimeError("登录凭据验证失败，请检查账号密码或验证码")

        if step == "session":
            if time.monotonic() >= next_session_read:
                session_info = _read_session(driver)
                if session_info:
                    return session_info
                # 页面状态仍持续轮询，但空 Session 不随每轮 DOM 扫描重复请求。
                # 从请求完成时计时，慢请求也不会在返回后立即再次发出。
                next_session_read = time.monotonic() + session_retry_delay
                session_retry_delay = min(2.0, session_retry_delay * 2)
        elif not submitted and step == "email":
            email_submits += 1
            if email_submits > 3:
                raise RuntimeError("邮箱登录步骤反复跳转，未完成登录")
            if _type_any(driver, _EMAIL_SELECTORS, email, timeout=timeout) is False:
                continue
            otp_after_ts = time.time()
            _submit_email_step(driver, email)
            submitted = True
        elif not submitted and step == "password":
            password = _account_registration_password(email)
            otp_after_ts = time.time()
            if password:
                if _type_any(driver, _PASSWORD_SELECTORS, password, timeout=timeout) is False:
                    continue
                if not _submit_auth_form(driver, _PASSWORD_SELECTORS):
                    try:
                        if _step(_page_state(driver)) != "password":
                            continue
                    except Exception as exc:
                        if _during_navigation(exc):
                            continue
                        raise
                    raise RuntimeError("找不到登录密码提交按钮")
            elif not _click_passwordless_signup_if_present(driver).get("ok"):
                raise RuntimeError("账号没有保存密码，且页面没有可用的一次性验证码登录入口")
            submitted = True
        elif not submitted and step == "email_otp":
            if email_attempts >= 3:
                raise RuntimeError("邮箱验证码已达到最大尝试次数")
            email_attempts += 1
            logger.info("[Cloak查活] 等待邮箱验证码（第 %s/3 次）", email_attempts)
            code = str(wait_for_otp(email, after_ts=otp_after_ts, email_source=email_source) or "").strip()
            if not code or code in used_email_codes:
                raise RuntimeError("邮箱未返回新的验证码，请稍后重试")
            used_email_codes.add(code)
            _submit_code(driver, code)
            submitted = True
            step_deadline = time.monotonic() + timeout
        elif not submitted and step == "mfa":
            if mfa_attempts >= 2:
                raise RuntimeError("TOTP 验证已达到最大尝试次数")
            mfa_attempts += 1
            code = _fresh_totp_code(_totp_for_account(email), used_totp_codes)
            used_totp_codes.add(code)
            logger.info("[Cloak查活] 验证 TOTP（第 %s/2 次）", mfa_attempts)
            _submit_code(driver, code)
            submitted = True
            step_deadline = time.monotonic() + timeout
        if time.monotonic() >= step_deadline:
            raise RuntimeError(f"Cloak 登录 timeout：停留在 {step} 阶段，未取得 Session/AT")
        # 刚提交过表单时页面正在跳转，缩短轮询尽快进入下一阶段；空闲等待（等验证码/
        # 等跳转）时拉长间隔，减少 CDP 与页面求值次数。_page_state 已不再触发布局，
        # 所以 150ms 的轮询成本很低。
        driver.page.wait_for_timeout(_POLL_FAST_MS if submitted else _POLL_IDLE_MS)
    raise RuntimeError("Cloak 登录 timeout：未取得 Session/AT")


def _install_live_check_data_saver(driver) -> None:
    """查活页面的省流量拦截：只拦可选资源，页面更小、渲染更快、内存更低。

    与注册共用同一套规则（``BROWSER_DATA_SAVER_BLOCKED_*``），但由
    ``LIVE_CHECK_DATA_SAVER`` 独立开关，不受 ``BROWSER_DATA_SAVER_MODE`` 影响；
    验证码/challenge 相关 URL 由拦截器自动放行。
    """
    try:
        from config import live_check as live_cfg
        if not bool(getattr(live_cfg, "LIVE_CHECK_DATA_SAVER", True)):
            return
        from core.browser_data_saver import (
            BrowserDataSaver, configured_resource_types, configured_url_patterns,
        )
        saver = BrowserDataSaver(label="Cloak查活")
        if not saver.enabled:
            saver.enabled = True
            saver.resource_types = configured_resource_types()
            saver.url_patterns = configured_url_patterns()
        saver.install_playwright(driver.context)
    except Exception:
        # 拦截器只是优化项，装不上就按完整页面继续查活。
        logger.warning("[Cloak查活] 安装省流量拦截失败，继续完整加载页面", exc_info=True)


def login_with_cloak(email: str, proxy: str | None = None, *, email_source: str | None = None) -> tuple[dict, dict]:
    """生命周期归当前工作线程；队列的每次换出口都会创建新浏览器。"""
    driver = None
    started_at = time.monotonic()
    try:
        driver, opened = build_cloak_driver(proxy=proxy, isolated=True, force_proxy=True)
        driver._registration_log_prefix = "[Cloak查活]"
        launched_at = time.monotonic()
        logger.info("[Cloak查活] 浏览器就绪，耗时 %.2fs", launched_at - started_at)
        _install_live_check_data_saver(driver)
        responses = _AuthResponses()
        driver.context.on("response", responses)
        logger.info("[Cloak查活] 已创建独立浏览器，开始重新登录：%s", email)
        session_info = _login(driver, email, email_source=email_source, responses=responses)
        logger.info(
            "[Cloak查活] 重新登录完成：登录耗时 %.2fs，浏览器启动+登录共 %.2fs",
            time.monotonic() - launched_at, time.monotonic() - started_at,
        )
        # 独立环境应有明确的账号身份，禁止把其它账号的浏览器状态写回。
        returned_email = str((session_info.get("user") or {}).get("email") or "").strip()
        if returned_email.casefold() != email.casefold():
            raise RuntimeError("登录会话邮箱缺失或与查活账号不一致，未写入凭据")
        raw = opened.raw or {}
        locale = raw.get("locale") or {}
        fingerprint = {
            "driver": "cloak", "locale": locale.get("locale"),
            "timezone_iana": locale.get("timezone"),
        }
        return session_info, {
            "driver": "cloak", "proxy_used": raw.get("proxy_pool_target") or None,
            "fingerprint": fingerprint,
            "fingerprint_text": "CloakBrowser 独立会话 " + " ".join(
                str(v) for k, v in fingerprint.items() if k != "driver" and v
            ),
        }
    finally:
        if driver is not None:
            # 后台批量任务不受注册调试保留开关影响，防止浏览器资源持续累积。
            try:
                driver.quit()
            except Exception:
                logger.warning("[Cloak查活] 浏览器退出失败，保留查活结果")
