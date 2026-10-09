"""Cloak 查活的内存与速度优化：并发额度、地理缓存、低内存参数、单次 DOM 扫描。"""
from __future__ import annotations

import threading
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from core import cloakbrowser_driver as driver_mod
from core import cloakbrowser_liveness as cloak


# --------------------------------------------------------------------------
# 低内存启动参数
# --------------------------------------------------------------------------

def test_memory_saver_args_defaults_to_bounded_v8_heap(monkeypatch):
    monkeypatch.setattr(driver_mod, "_cfg", SimpleNamespace(CLOAK_MEMORY_SAVER=True, CLOAK_JS_HEAP_MB=512))
    assert driver_mod._memory_saver_args() == ["--js-flags=--max-old-space-size=512"]


def test_memory_saver_args_can_be_disabled_or_unbounded(monkeypatch):
    monkeypatch.setattr(driver_mod, "_cfg", SimpleNamespace(CLOAK_MEMORY_SAVER=False, CLOAK_JS_HEAP_MB=512))
    assert driver_mod._memory_saver_args() == []
    monkeypatch.setattr(driver_mod, "_cfg", SimpleNamespace(CLOAK_MEMORY_SAVER=True, CLOAK_JS_HEAP_MB=0))
    assert driver_mod._memory_saver_args() == []


@pytest.mark.parametrize("raw,expected", [(1, 64), (128, 128), (99999, 4096), ("bad", 512)])
def test_memory_saver_args_clamp_invalid_values(monkeypatch, raw, expected):
    monkeypatch.setattr(driver_mod, "_cfg", SimpleNamespace(CLOAK_MEMORY_SAVER=True, CLOAK_JS_HEAP_MB=raw))
    assert driver_mod._memory_saver_args() == [f"--js-flags=--max-old-space-size={expected}"]


# --------------------------------------------------------------------------
# 浏览器并发额度
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_shared_state(monkeypatch):
    monkeypatch.setattr(driver_mod, "_BROWSER_GATE", driver_mod._BrowserGate())
    monkeypatch.setattr(driver_mod, "_GEO_CACHE", {})


def test_gate_uses_configured_limit(monkeypatch):
    monkeypatch.setattr(driver_mod, "_cfg", SimpleNamespace(CLOAK_MAX_CONCURRENT=3))
    assert driver_mod._BrowserGate().limit() == 3


@pytest.mark.parametrize("available,expected", [(600, 1), (1500, 2), (7000, 10), (0, 1)])
def test_gate_auto_limit_follows_available_memory(monkeypatch, available, expected):
    monkeypatch.setattr(driver_mod, "_cfg", SimpleNamespace(CLOAK_MAX_CONCURRENT=0))
    monkeypatch.setattr(driver_mod, "_available_memory_mb", lambda: float(available))
    assert driver_mod._BrowserGate().limit() == expected


def test_gate_is_unlimited_when_memory_is_unknown(monkeypatch):
    monkeypatch.setattr(driver_mod, "_cfg", SimpleNamespace(CLOAK_MAX_CONCURRENT=0))
    monkeypatch.setattr(driver_mod, "_available_memory_mb", lambda: None)
    gate = driver_mod._BrowserGate()
    assert gate.limit() is None
    assert gate.acquire(timeout=0.1) is True


def test_gate_blocks_second_browser_until_release(monkeypatch):
    monkeypatch.setattr(driver_mod, "_cfg", SimpleNamespace(CLOAK_MAX_CONCURRENT=1))
    gate = driver_mod._BrowserGate()
    assert gate.acquire(timeout=0.1) is True
    results: list[bool] = []

    def worker():
        started = time.monotonic()
        results.append(gate.acquire(timeout=5.0))
        results.append(time.monotonic() - started >= 0.2)

    thread = threading.Thread(target=worker)
    thread.start()
    time.sleep(0.3)
    assert results == []  # 仍被额度挡住
    gate.release()
    thread.join(timeout=5.0)
    assert results == [True, True]
    assert gate.snapshot() == {"in_use": 1, "limit": 1}
    gate.release()
    assert gate.snapshot()["in_use"] == 0


def test_gate_returns_false_after_timeout_and_still_counts(monkeypatch):
    monkeypatch.setattr(driver_mod, "_cfg", SimpleNamespace(CLOAK_MAX_CONCURRENT=1))
    gate = driver_mod._BrowserGate()
    gate.acquire(timeout=0.1)
    outcome: list[bool] = []

    def worker():
        # 额度已被主线程占用，等待超时后仍放行，保证队列不会被一次泄漏卡死。
        outcome.append(gate.acquire(timeout=0.05))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=5.0)
    assert outcome == [False]
    assert gate.snapshot()["in_use"] == 2
    gate.release()
    gate.release()
    assert gate.snapshot()["in_use"] == 0


def test_gate_is_reentrant_for_nested_same_thread_launch(monkeypatch):
    monkeypatch.setattr(driver_mod, "_cfg", SimpleNamespace(CLOAK_MAX_CONCURRENT=1))
    gate = driver_mod._BrowserGate()
    assert gate.acquire(timeout=0.1) is True
    # 同线程嵌套（例如注册→授权复用流程）不再排队。
    assert gate.acquire(timeout=0.1) is True
    assert gate.snapshot()["in_use"] == 1
    gate.release()
    assert gate.snapshot()["in_use"] == 1
    gate.release()
    assert gate.snapshot()["in_use"] == 0


def test_build_cloak_driver_releases_slot_when_startup_fails(monkeypatch):
    monkeypatch.setattr(driver_mod, "_cfg", SimpleNamespace(
        CLOAK_USE_PROXY=False, CLOAK_USER_DATA_DIR="", CLOAK_HEADLESS=True,
        CLOAK_HUMANIZE=False, CLOAK_GEOIP=False, CLOAK_EXTRA_ARGS=[],
        CLOAK_FINGERPRINT_SEED="", CLOAK_LICENSE_KEY="", CLOAK_SELENIUM_TIMEOUT=30,
        CLOAK_KEEP_BROWSER_OPEN=False, CLOAK_MEMORY_SAVER=False, CLOAK_JS_HEAP_MB=0,
        CLOAK_MAX_CONCURRENT=1,
    ))
    monkeypatch.setattr(driver_mod, "_build_cloak_locale_options", Mock(return_value={}))
    package = ModuleType("cloakbrowser")
    package.launch = Mock(side_effect=RuntimeError("launch failed"))
    package.launch_persistent_context = Mock()
    monkeypatch.setitem(__import__("sys").modules, "cloakbrowser", package)

    with pytest.raises(RuntimeError, match="launch failed"):
        driver_mod.build_cloak_driver("")
    assert driver_mod._BROWSER_GATE.snapshot()["in_use"] == 0


def test_keep_browser_open_skips_concurrency_slot(monkeypatch):
    monkeypatch.setattr(driver_mod, "_cfg", SimpleNamespace(
        CLOAK_USE_PROXY=False, CLOAK_USER_DATA_DIR="", CLOAK_HEADLESS=True,
        CLOAK_HUMANIZE=False, CLOAK_GEOIP=False, CLOAK_EXTRA_ARGS=[],
        CLOAK_FINGERPRINT_SEED="", CLOAK_LICENSE_KEY="", CLOAK_SELENIUM_TIMEOUT=30,
        CLOAK_KEEP_BROWSER_OPEN=True, CLOAK_MEMORY_SAVER=False, CLOAK_JS_HEAP_MB=0,
        CLOAK_MAX_CONCURRENT=1,
    ))
    monkeypatch.setattr(driver_mod, "_build_cloak_locale_options", Mock(return_value={}))
    page = Mock(spec=["set_default_navigation_timeout", "set_default_timeout"])
    context = Mock(spec=["new_page", "close", "browser"])
    browser = Mock(spec=["new_context", "close"])
    browser.new_context.return_value = context
    context.browser = browser
    context.new_page.return_value = page
    package = ModuleType("cloakbrowser")
    package.launch = Mock(return_value=browser)
    package.launch_persistent_context = Mock()
    monkeypatch.setitem(__import__("sys").modules, "cloakbrowser", package)

    driver, _ = driver_mod.build_cloak_driver("")
    assert driver_mod._BROWSER_GATE.snapshot()["in_use"] == 0


# --------------------------------------------------------------------------
# 出口地理信息缓存
# --------------------------------------------------------------------------

def _geo_runtime(monkeypatch, **overrides):
    from config import browser as browser_cfg
    for key, value in {
        "IP_GEO_ENDPOINTS": ["https://geo.example/json"],
        "IP_GEO_TIMEOUT": 1.0,
        "IP_GEO_CACHE_TTL": 1800.0,
        "IP_GEO_CACHE_SIZE": 8,
        "IP_GEO_FAILURE_CACHE_TTL": 60.0,
        **overrides,
    }.items():
        monkeypatch.setattr(browser_cfg, key, value)
    response = Mock(status_code=200)
    response.json.return_value = {
        "ip": "203.0.113.9", "country": "JP", "city": "Tokyo", "timezone": "Asia/Tokyo",
    }
    get = Mock(return_value=response)
    import requests
    monkeypatch.setattr(requests, "get", get)
    return get


def test_exit_geo_is_looked_up_once_per_proxy(monkeypatch):
    get = _geo_runtime(monkeypatch)
    first = driver_mod._detect_cloak_exit_geo("socks5://proxy.example:1080")
    second = driver_mod._detect_cloak_exit_geo("socks5://proxy.example:1080")

    assert first == second
    assert first["timezone"] == "Asia/Tokyo"
    assert get.call_count == 1
    # 不同出口各自缓存，不会互相串用。
    driver_mod._detect_cloak_exit_geo("socks5://other.example:1080")
    assert get.call_count == 2


def test_exit_geo_cache_can_be_disabled(monkeypatch):
    get = _geo_runtime(monkeypatch, IP_GEO_CACHE_SIZE=0)
    driver_mod._detect_cloak_exit_geo("socks5://proxy.example:1080")
    driver_mod._detect_cloak_exit_geo("socks5://proxy.example:1080")
    assert get.call_count == 2


def test_exit_geo_failure_is_short_cached(monkeypatch):
    get = _geo_runtime(monkeypatch)
    get.return_value = Mock(status_code=500)
    assert driver_mod._detect_cloak_exit_geo("socks5://proxy.example:1080") == {}
    assert driver_mod._detect_cloak_exit_geo("socks5://proxy.example:1080") == {}
    assert get.call_count == 1


def test_exit_geo_cache_expires(monkeypatch):
    get = _geo_runtime(monkeypatch, IP_GEO_CACHE_TTL=0.05)
    driver_mod._detect_cloak_exit_geo("socks5://proxy.example:1080")
    time.sleep(0.08)
    driver_mod._detect_cloak_exit_geo("socks5://proxy.example:1080")
    assert get.call_count == 2


# --------------------------------------------------------------------------
# 页面扫描：单次 evaluate_all 取代逐元素往返
# --------------------------------------------------------------------------

def test_visible_filters_with_single_evaluate_all_call():
    elements = [Mock(name=f"el{i}") for i in range(4)]
    locator = Mock()
    locator.evaluate_all.return_value = [True, False, True, False]
    locator.nth.side_effect = elements.__getitem__
    scope = SimpleNamespace(locator=Mock(return_value=locator))

    visible = cloak._visible(scope, "button")

    locator.evaluate_all.assert_called_once()
    assert [elements[0], elements[2]] == visible
    for element in elements:
        element.is_visible.assert_not_called()


def test_visible_falls_back_when_evaluate_all_is_unsupported():
    node = Mock()
    node.is_visible.return_value = True
    node.is_enabled.return_value = True
    node.get_attribute.return_value = None
    locator = Mock()
    locator.evaluate_all.side_effect = RuntimeError("unsupported")
    locator.count.return_value = 1
    locator.nth.return_value = node
    scope = SimpleNamespace(locator=Mock(return_value=locator))

    assert cloak._visible(scope, "button") == [node]
    node.is_visible.assert_called_once()


def test_page_state_does_not_read_body_text(monkeypatch):
    page = Mock()
    page.evaluate.return_value = {"url": "https://chatgpt.com/", "email": True}
    state = cloak._page_state(SimpleNamespace(page=page))

    script = page.evaluate.call_args.args[0]
    assert "document.body" not in script  # innerText 会强制整页布局
    assert "text" not in state


def test_page_text_is_read_on_demand():
    page = Mock()
    page.evaluate.return_value = "  account disabled  "
    assert cloak._page_text(SimpleNamespace(page=page)) == "  account disabled  "


def test_auth_error_page_reads_body_text_lazily(monkeypatch):
    page_text = Mock(return_value="Your account has been deactivated")
    monkeypatch.setattr(cloak, "_page_text", page_text)

    with pytest.raises(cloak.AccountUnusableError):
        cloak._check_error(
            {"url": "https://auth.openai.com/error", "errors": []},
            cloak._AuthResponses(),
            SimpleNamespace(page=Mock()),
        )
    page_text.assert_called_once()


def test_non_error_page_never_reads_body_text(monkeypatch):
    page_text = Mock(return_value="account deleted")
    monkeypatch.setattr(cloak, "_page_text", page_text)

    cloak._check_error(
        {"url": "https://auth.openai.com/log-in/password", "errors": []},
        cloak._AuthResponses(),
        SimpleNamespace(page=Mock()),
    )
    page_text.assert_not_called()


# --------------------------------------------------------------------------
# 密码/重发按钮选择：一次扫描 + 本地判定
# --------------------------------------------------------------------------

def test_submit_form_scans_once_and_skips_non_login_buttons(monkeypatch):
    form = Mock()
    form.get_attribute.return_value = "/log-in/password"
    candidates = form.locator.return_value
    elements = [Mock() for _ in range(5)]
    candidates.nth.side_effect = elements.__getitem__
    candidates.evaluate_all.return_value = [
        {"visible": False, "details": "Continue"},
        {"visible": True, "details": "Create account"},
        {"visible": True, "details": "passwordless_login_send_otp"},
        {"visible": True, "details": "Resend code"},
        {"visible": True, "details": "Continue"},
    ]
    pinned = Mock()
    pin = Mock(return_value=pinned)
    monkeypatch.setattr(cloak, "_pin", pin)
    monkeypatch.setattr(cloak, "_page_state", Mock(return_value={
        "url": "https://auth.openai.com/log-in/password", "password": True,
    }))
    details = Mock(side_effect=AssertionError("unexpected per-button CDP reads"))
    monkeypatch.setattr(cloak, "_button_details", details)

    assert cloak._submit_auth_form(SimpleNamespace(page=Mock()), [], form=form)
    candidates.evaluate_all.assert_called_once()
    assert pin.call_args.args[1] == candidates.nth(4)
    pinned.click.assert_called_once_with(timeout=3000)
    details.assert_not_called()


def test_submit_form_scan_falls_back_when_unsupported(monkeypatch):
    form = Mock()
    form.get_attribute.return_value = "/log-in/password"
    form.locator.return_value.evaluate_all.side_effect = RuntimeError("unsupported")
    button = Mock()
    monkeypatch.setattr(cloak, "_visible", Mock(return_value=[button]))
    monkeypatch.setattr(cloak, "_button_details", Mock(return_value="Continue"))
    monkeypatch.setattr(cloak, "_pin", Mock(return_value=button))
    monkeypatch.setattr(cloak, "_page_state", Mock(return_value={
        "url": "https://auth.openai.com/log-in/password", "password": True,
    }))

    assert cloak._submit_auth_form(SimpleNamespace(page=Mock()), [], form=form)
    button.click.assert_called_once_with(timeout=3000)


def test_pick_choice_prefers_last_attribute_hit():
    scanned = [
        {"visible": True, "text": "Use a one-time code", "attrs": "", "details": "sign up"},
        {"visible": True, "text": "Use a one-time code", "attrs": "", "details": "text hit"},
        {"visible": True, "text": "x", "attrs": "value=passwordless_login_send_otp", "details": "attr one"},
        {"visible": True, "text": "y", "attrs": "value=passwordless_login_send_otp", "details": "attr two"},
    ]
    text_pattern, attr_pattern = cloak._choice_patterns(resend=False)
    assert cloak._pick_choice_index(scanned, text_pattern, attr_pattern) == 3


def test_pick_choice_uses_first_text_hit_and_skips_invisible_and_signup():
    scanned = [
        {"visible": False, "text": "Resend code", "attrs": "", "details": "Resend code"},
        {"visible": True, "text": "Resend code to sign up", "attrs": "", "details": "Resend code to sign up"},
        {"visible": True, "text": "Resend code", "attrs": "", "details": "Resend code"},
        {"visible": True, "text": "Resend code", "attrs": "", "details": "Resend code"},
    ]
    text_pattern, attr_pattern = cloak._choice_patterns(resend=True)
    assert cloak._pick_choice_index(scanned, text_pattern, attr_pattern) == 2


def test_pick_choice_returns_none_when_nothing_matches():
    scanned = [{"visible": True, "text": "Continue", "attrs": "", "details": "Continue"}]
    text_pattern, attr_pattern = cloak._choice_patterns(resend=True)
    assert cloak._pick_choice_index(scanned, text_pattern, attr_pattern) is None


def test_click_auth_choice_bulk_path_clicks_scanned_candidate(monkeypatch):
    elements = [Mock(name=f"el{i}") for i in range(3)]
    pinned = Mock()
    locator = Mock()
    locator.evaluate_all.return_value = [
        {"visible": True, "text": "Continue", "attrs": "", "details": "Continue"},
        {"visible": True, "text": "Resend code", "attrs": "", "details": "Resend code"},
        {"visible": True, "text": "Sign up", "attrs": "", "details": "Sign up"},
    ]
    locator.nth.side_effect = elements.__getitem__

    page = Mock()

    def page_locator(selector):
        if selector.startswith("[data-cloak-live-target="):
            return pinned
        return locator

    page.locator.side_effect = page_locator
    driver = SimpleNamespace(page=page)

    state = {"url": "https://auth.openai.com/email-verification", "code": True}
    monkeypatch.setattr(cloak, "_page_state", Mock(return_value=state))

    assert cloak._click_auth_choice(driver, resend=True) is True
    locator.evaluate_all.assert_called_once()
    pinned.click.assert_called_once_with(timeout=3000)
    # 命中的是第二个候选（重发按钮），没有对其它候选做逐元素属性读取。
    assert elements[1].evaluate.called
    assert not elements[0].evaluate.called
    assert not elements[2].evaluate.called


def test_click_auth_choice_bulk_path_returns_false_without_match(monkeypatch):
    locator = Mock()
    locator.evaluate_all.return_value = [
        {"visible": True, "text": "Continue", "attrs": "", "details": "Continue"},
    ]
    page = Mock()
    page.locator.return_value = locator
    driver = SimpleNamespace(page=page)
    monkeypatch.setattr(cloak, "_page_state", Mock(return_value={
        "url": "https://auth.openai.com/email-verification", "code": True,
    }))

    assert cloak._click_auth_choice(driver, resend=True) is False


# --------------------------------------------------------------------------
# 查活省流量拦截
# --------------------------------------------------------------------------

def test_live_check_data_saver_can_be_disabled(monkeypatch):
    from config import live_check as live_cfg
    monkeypatch.setattr(live_cfg, "LIVE_CHECK_DATA_SAVER", False)
    install = Mock()
    monkeypatch.setattr("core.browser_data_saver.BrowserDataSaver.install_playwright", install)

    cloak._install_live_check_data_saver(SimpleNamespace(context=Mock()))
    install.assert_not_called()


def test_live_check_data_saver_enables_blocking_even_when_registration_mode_is_off(monkeypatch):
    from config import browser as browser_cfg
    from config import live_check as live_cfg
    monkeypatch.setattr(live_cfg, "LIVE_CHECK_DATA_SAVER", True)
    monkeypatch.setattr(browser_cfg, "BROWSER_DATA_SAVER_MODE", False)
    context = Mock()
    saver = SimpleNamespace(enabled=False, resource_types=[], url_patterns=[],
                            install_playwright=Mock())
    monkeypatch.setattr("core.browser_data_saver.BrowserDataSaver", Mock(return_value=saver))

    cloak._install_live_check_data_saver(SimpleNamespace(context=context))

    assert saver.enabled is True
    assert "image" in saver.resource_types
    saver.install_playwright.assert_called_once_with(context)


def test_live_check_data_saver_failure_does_not_break_check(monkeypatch):
    from config import live_check as live_cfg
    monkeypatch.setattr(live_cfg, "LIVE_CHECK_DATA_SAVER", True)
    monkeypatch.setattr(
        "core.browser_data_saver.BrowserDataSaver",
        Mock(side_effect=RuntimeError("no route support")),
    )
    # 拦截器失败只降级为完整加载，不抛给查活主流程。
    cloak._install_live_check_data_saver(SimpleNamespace(context=Mock()))


# --------------------------------------------------------------------------
# WebUI 配置往返
# --------------------------------------------------------------------------

_NEW_FIELDS = {
    "LIVE_CHECK_DATA_SAVER": ("live_check.py", "bool", "账号查活"),
    "CLOAK_MEMORY_SAVER": ("cloakbrowser.py", "bool", "CloakBrowser"),
    "CLOAK_JS_HEAP_MB": ("cloakbrowser.py", "int", "CloakBrowser"),
    "CLOAK_MAX_CONCURRENT": ("cloakbrowser.py", "int", "CloakBrowser"),
    "IP_GEO_CACHE_TTL": ("browser.py", "float", "浏览器画像"),
    "IP_GEO_CACHE_SIZE": ("browser.py", "int", "浏览器画像"),
}


def test_new_optimization_fields_are_editable(isolated_runtime):
    from webui import config_editor
    fields = {item["key"]: item for item in config_editor.get_config()}
    for key, (file_name, value_type, group) in _NEW_FIELDS.items():
        field = fields[key]
        assert field["file"] == file_name
        assert field["type"] == value_type
        assert field["group"] == group
        assert field["storage"] == "env"


def test_optimization_settings_round_trip_through_env(monkeypatch, isolated_runtime):
    import runpy
    from pathlib import Path
    from config import cloakbrowser, live_check
    from webui import config_editor

    for key in _NEW_FIELDS:
        monkeypatch.delenv(key, raising=False)

    updates = {
        "LIVE_CHECK_DATA_SAVER": False,
        "CLOAK_MEMORY_SAVER": False,
        "CLOAK_JS_HEAP_MB": 384,
        "CLOAK_MAX_CONCURRENT": 2,
        "IP_GEO_CACHE_TTL": 60.0,
        "IP_GEO_CACHE_SIZE": 0,
    }
    result = config_editor.update_config(updates)
    assert set(result["updated"]) == set(updates)
    assert set(result["env_updated"]) == set(updates)

    cloak_settings = runpy.run_path(str(Path(cloakbrowser.__file__)))
    live_settings = runpy.run_path(str(Path(live_check.__file__)))
    assert cloak_settings["CLOAK_MEMORY_SAVER"] is False
    assert cloak_settings["CLOAK_JS_HEAP_MB"] == 384
    assert cloak_settings["CLOAK_MAX_CONCURRENT"] == 2
    assert live_settings["LIVE_CHECK_DATA_SAVER"] is False

    from config import browser as browser_cfg
    browser_settings = runpy.run_path(str(Path(browser_cfg.__file__)))
    assert browser_settings["IP_GEO_CACHE_TTL"] == 60.0
    assert browser_settings["IP_GEO_CACHE_SIZE"] == 0
