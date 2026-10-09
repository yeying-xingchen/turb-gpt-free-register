"""真实浏览器回归：Cloak humanize 开启时查活交互仍可用。

Cloak 的 humanize 层会全局改写 ``Locator.fill/click``，把选择器交给隔离世界解析；
该解析器只支持 CSS/text=/xpath=/get_by_* 与末尾的 ``.first``/``.nth()``/``.last``，
遇到 ``:visible`` 或 ``>>`` 链式定位会抛 ``UnsupportedHumanizeSelectorError``，线上
表现为“Cloak 页面交互失败”。这里用真实 Chromium + humanize 补丁跑完整登录状态机，
确保交互 helper 不再触发该限制。

需要本机有可用的 Chromium 与 cloakbrowser；默认跳过：
    RUN_CLOAK_BROWSER_TESTS=1 PLAYWRIGHT_CHROMIUM_EXECUTABLE=... \
        .venv/bin/python -m pytest tests/test_cloakbrowser_humanize.py -q
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

playwright = pytest.importorskip("playwright.sync_api")
pytest.importorskip("cloakbrowser.human")

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_CLOAK_BROWSER_TESTS") != "1",
    reason="Set RUN_CLOAK_BROWSER_TESTS=1 to run the real-browser Cloak check",
)

_CHATGPT = "<!doctype html><html><body><h1>ChatGPT</h1></body></html>"


def _login_page(next_url: str) -> str:
    return (
        "<!doctype html><html><body><form "
        f"onsubmit=\"event.preventDefault();location.href='{next_url}';return false;\">"
        "<input type='email' name='email' autocomplete='email'/>"
        "<button type='submit'>Continue</button></form></body></html>"
    )


def _password_page(next_url: str) -> str:
    return (
        "<!doctype html><html><body><form "
        f"onsubmit=\"event.preventDefault();location.href='{next_url}';return false;\">"
        "<input type='password' name='password' autocomplete='current-password'/>"
        "<button type='submit'>Continue</button></form></body></html>"
    )


def _totp_page(next_url: str) -> str:
    return (
        "<!doctype html><html><body><form "
        f"onsubmit=\"event.preventDefault();location.href='{next_url}';return false;\">"
        "<input type='text' name='code' inputmode='numeric' autocomplete='one-time-code'/>"
        "<button type='submit'>Continue</button></form></body></html>"
    )


_PAGES = {
    ("chatgpt.com", "/auth/login"): _login_page("https://auth.openai.com/log-in/password"),
    ("auth.openai.com", "/log-in/password"): _password_page("https://auth.openai.com/mfa-challenge/abc"),
    ("auth.openai.com", "/mfa-challenge/abc"): _totp_page("https://chatgpt.com/"),
    ("chatgpt.com", "/"): _CHATGPT,
}


def _serve(route) -> None:
    parsed = urlparse(route.request.url)
    if parsed.path == "/api/auth/session":
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "accessToken": "AT-humanize", "user": {"email": "user@example.com"},
        }))
        return
    body = _PAGES.get((parsed.hostname, parsed.path))
    if body is None:
        route.fulfill(status=204, body="")
        return
    route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)


@pytest.mark.parametrize("session_failure", [None, 503, "network"])
def test_cloak_liveness_survives_humanized_locators(monkeypatch, session_failure):
    from cloakbrowser.human import patch_browser
    from cloakbrowser.human.config import resolve_config
    from core import cloakbrowser_liveness as cloak
    from core.cloakbrowser_driver import CloakSeleniumDriver

    options = {"headless": True}
    if executable := os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"):
        options["executable_path"] = executable

    with playwright.sync_playwright() as driver:
        try:
            browser = driver.chromium.launch(**options)
        except Exception as exc:  # pragma: no cover - environment dependent
            pytest.skip(f"Chromium 不可用：{type(exc).__name__}")
        # 与生产一致：humanize 在 new_context 之前打补丁。
        patch_browser(browser, resolve_config("default", None))
        context = browser.new_context(service_workers="block")
        page = context.new_page()
        session_requests = []

        def serve(route):
            if urlparse(route.request.url).path == "/api/auth/session":
                session_requests.append(route.request.url)
                if len(session_requests) == 1 and session_failure is not None:
                    if session_failure == "network":
                        route.abort("failed")
                    else:
                        route.fulfill(status=session_failure, content_type="application/json", body="{}")
                    return
            _serve(route)

        # Page 路由优先于查活安装的 context 省流量路由，所有请求始终本地响应。
        page.route("**/*", serve)
        page.set_default_timeout(15000)
        cloak_driver = CloakSeleniumDriver(browser=browser, context=context, page=page)
        cloak_driver.set_page_load_timeout(30)

        import core.account_liveness as account_liveness

        monkeypatch.setattr(cloak, "build_cloak_driver", lambda *a, **k: (cloak_driver, SimpleNamespace(raw={"locale": {}})))
        monkeypatch.setattr(account_liveness, "_account_registration_password", lambda email: "secret-password")
        monkeypatch.setattr(account_liveness, "_totp_for_account", lambda email: "JBSWY3DPEHPK3PXP")
        monkeypatch.setattr(account_liveness, "_fresh_totp_code", lambda secret, used: "123456")
        monkeypatch.setattr(cloak, "wait_for_otp", lambda *a, **k: "654321")

        session, details = cloak.login_with_cloak("user@example.com")

    assert session["accessToken"] == "AT-humanize"
    assert details["driver"] == "cloak"
    assert len(session_requests) == (1 if session_failure is None else 2)
