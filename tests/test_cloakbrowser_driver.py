"""Offline launch/cleanup contract for the CloakBrowser adapter."""
from types import ModuleType, SimpleNamespace
import sys
from unittest.mock import Mock

import pytest

from core import cloakbrowser_driver as cloak


def _browser_resources():
    page = Mock(spec=["set_default_navigation_timeout", "set_default_timeout"])
    context = Mock(spec=["new_page", "close", "browser"])
    browser = Mock(spec=["new_context", "close"])
    browser.new_context.return_value = context
    context.browser = browser
    context.new_page.return_value = page
    return browser, context, page


@pytest.fixture
def runtime(monkeypatch):
    cfg = SimpleNamespace(
        CLOAK_USE_PROXY=True,
        CLOAK_USER_DATA_DIR="",
        CLOAK_HEADLESS=True,
        CLOAK_HUMANIZE=False,
        CLOAK_GEOIP=True,
        CLOAK_EXTRA_ARGS=["--disable-dev-shm-usage"],
        CLOAK_FINGERPRINT_SEED="12345",
        CLOAK_LICENSE_KEY="test-license",
        CLOAK_SELENIUM_TIMEOUT=37,
    )
    monkeypatch.setattr(cloak, "_cfg", cfg)
    locale = Mock(return_value={
        "locale": "ja-JP",
        "timezone": "Asia/Tokyo",
        "accept_language": "ja-JP,ja;q=0.9",
    })
    monkeypatch.setattr(cloak, "_build_cloak_locale_options", locale)

    browser, context, page = _browser_resources()
    # Replace the package itself so the real browser cannot import or download.
    package = ModuleType("cloakbrowser")
    package.launch = Mock(return_value=browser)
    package.launch_persistent_context = Mock(return_value=context)
    monkeypatch.setitem(sys.modules, "cloakbrowser", package)

    from config import proxy as proxy_cfg
    from core import proxy_chain

    pick_proxy = Mock(return_value="http://pool.example:8080")
    relay = Mock(spec=["close"])
    open_proxy = Mock(return_value=("http://127.0.0.1:18080", relay))
    monkeypatch.setattr(proxy_cfg, "pick_proxy", pick_proxy)
    monkeypatch.setattr(proxy_chain, "open_proxy_pool_proxy", open_proxy)
    return SimpleNamespace(
        cfg=cfg, locale=locale, package=package,
        browser=browser, context=context, page=page,
        pick_proxy=pick_proxy, open_proxy=open_proxy, relay=relay,
    )


def test_isolated_ignores_persistent_config_and_creates_independent_browsers(runtime):
    runtime.cfg.CLOAK_USER_DATA_DIR = "/shared/registration-profile"
    second_browser, second_context, second_page = _browser_resources()
    runtime.package.launch.side_effect = [runtime.browser, second_browser]

    first, _ = cloak.build_cloak_driver("", isolated=True)
    second, _ = cloak.build_cloak_driver("", isolated=True)

    assert runtime.package.launch.call_count == 2
    runtime.package.launch_persistent_context.assert_not_called()
    assert first.browser is runtime.browser
    assert second.browser is second_browser
    assert first.context is runtime.context
    assert second.context is second_context
    assert first.page is runtime.page
    assert second.page is second_page
    assert runtime.cfg.CLOAK_USER_DATA_DIR == "/shared/registration-profile"
    for browser in (runtime.browser, second_browser):
        browser.new_context.assert_called_once_with(
            locale="ja-JP", timezone_id="Asia/Tokyo",
            extra_http_headers={"Accept-Language": "ja-JP,ja;q=0.9"},
        )
    first.quit()
    second_browser.close.assert_not_called()
    second_context.close.assert_not_called()
    second.quit()


@pytest.mark.parametrize("configured_proxy", [False, True])
@pytest.mark.parametrize("force_proxy", [False, True])
@pytest.mark.parametrize("proxy", [None, "", " socks5h://user:pass@proxy.example:1080 "])
def test_proxy_selection_respects_force_and_explicit_direct(
    runtime, configured_proxy, force_proxy, proxy,
):
    runtime.cfg.CLOAK_USE_PROXY = configured_proxy
    driver, opened = cloak.build_cloak_driver(proxy, force_proxy=force_proxy)

    enabled = configured_proxy or force_proxy
    from_pool = enabled and proxy is None
    expected_proxy = (
        "http://127.0.0.1:18080" if from_pool
        else "socks5://user:pass@proxy.example:1080" if enabled and proxy
        else None
    )
    if from_pool:
        runtime.pick_proxy.assert_called_once_with()
        runtime.open_proxy.assert_called_once_with("http://pool.example:8080")
        assert driver._proxy_relay is runtime.relay
    else:
        runtime.pick_proxy.assert_not_called()
        runtime.open_proxy.assert_not_called()
        assert driver._proxy_relay is None
    options = runtime.package.launch.call_args.kwargs
    assert options.get("proxy") == expected_proxy
    if expected_proxy is None:
        assert "proxy" not in options
    assert opened.raw["proxy"] == expected_proxy
    assert opened.raw["proxy_pool_target"] == (
        "http://pool.example:8080" if from_pool else expected_proxy
    )
    runtime.locale.assert_called_once_with(expected_proxy)
    runtime.relay.close.assert_not_called()
    driver.quit()
    if from_pool:
        runtime.relay.close.assert_called_once_with()
    else:
        runtime.relay.close.assert_not_called()


@pytest.mark.parametrize("persistent", [False, True])
def test_default_call_preserves_registration_options_and_metadata(runtime, persistent):
    if persistent:
        runtime.cfg.CLOAK_USER_DATA_DIR = " /shared/registration-profile "
    driver, opened = cloak.build_cloak_driver()

    expected_options = {
        "headless": True,
        "humanize": False,
        "geoip": True,
        "locale": "ja-JP",
        "timezone": "Asia/Tokyo",
        "proxy": "http://127.0.0.1:18080",
        "args": ["--disable-dev-shm-usage", "--fingerprint=12345"],
        "license_key": "test-license",
    }
    if persistent:
        runtime.package.launch_persistent_context.assert_called_once_with(
            "/shared/registration-profile", **expected_options,
        )
        runtime.package.launch.assert_not_called()
        runtime.browser.new_context.assert_not_called()
    else:
        runtime.package.launch.assert_called_once_with(**expected_options)
        runtime.package.launch_persistent_context.assert_not_called()
        runtime.browser.new_context.assert_called_once_with(
            locale="ja-JP", timezone_id="Asia/Tokyo",
            extra_http_headers={"Accept-Language": "ja-JP,ja;q=0.9"},
        )
    runtime.context.new_page.assert_called_once_with()
    runtime.page.set_default_navigation_timeout.assert_called_once_with(37000)
    runtime.page.set_default_timeout.assert_called_once_with(37000)
    assert driver._registration_log_prefix == "[Cloak注册]"
    assert opened.profile_id == "cloakbrowser"
    assert opened.raw == {
        "driver": "cloakbrowser",
        "proxy": "http://127.0.0.1:18080",
        "proxy_pool_target": "http://pool.example:8080",
        "locale": runtime.locale.return_value,
        "options": {k: v for k, v in expected_options.items() if k != "license_key"},
    }
    assert runtime.cfg.CLOAK_EXTRA_ARGS == ["--disable-dev-shm-usage"]
    runtime.context.close.assert_not_called()
    runtime.browser.close.assert_not_called()
    runtime.relay.close.assert_not_called()
    driver.quit()
    runtime.context.close.assert_called_once_with()
    runtime.browser.close.assert_called_once_with()
    runtime.relay.close.assert_called_once_with()


def test_missing_cloakbrowser_closes_previously_created_relay(runtime, monkeypatch):
    monkeypatch.setitem(sys.modules, "cloakbrowser", None)
    with pytest.raises(RuntimeError, match="未安装 cloakbrowser") as caught:
        cloak.build_cloak_driver()
    assert isinstance(caught.value.__cause__, ImportError)
    runtime.relay.close.assert_called_once_with()
    runtime.context.close.assert_not_called()
    runtime.browser.close.assert_not_called()


@pytest.mark.parametrize(
    "stage,persistent,close_browser,close_context",
    [
        ("locale", False, False, False),
        ("launch", False, False, False),
        ("launch", True, False, False),
        ("context", False, True, False),
        ("page", False, True, True),
        ("page", True, True, True),
        ("driver", False, True, True),
        ("timeout", False, True, True),
        ("result", False, True, True),
    ],
)
def test_startup_failure_closes_acquired_resources(
    runtime, monkeypatch, stage, persistent, close_browser, close_context,
):
    failure = RuntimeError(f"{stage} failed")
    if persistent:
        runtime.cfg.CLOAK_USER_DATA_DIR = "/shared/registration-profile"
    if stage == "locale":
        runtime.locale.side_effect = failure
    elif stage == "launch":
        launch = runtime.package.launch_persistent_context if persistent else runtime.package.launch
        launch.side_effect = failure
    elif stage == "context":
        runtime.browser.new_context.side_effect = failure
    elif stage == "page":
        runtime.context.new_page.side_effect = failure
    elif stage == "driver":
        # Invalid configuration fails inside the actual driver constructor.
        runtime.cfg.CLOAK_SELENIUM_TIMEOUT = "invalid-timeout"
    elif stage == "timeout":
        monkeypatch.setattr(cloak.CloakSeleniumDriver, "set_page_load_timeout", Mock(side_effect=failure))
    elif stage == "result":
        monkeypatch.setattr(cloak, "CloakOpenResult", Mock(side_effect=failure))

    expected_type = ValueError if stage == "driver" else RuntimeError
    with pytest.raises(expected_type) as caught:
        cloak.build_cloak_driver()
    if stage != "driver":
        assert caught.value is failure
    assert runtime.context.close.call_count == int(close_context)
    assert runtime.browser.close.call_count == int(close_browser)
    runtime.relay.close.assert_called_once_with()


def test_cleanup_errors_do_not_mask_failure_or_skip_other_resources(runtime):
    failure = RuntimeError("new page failed")
    runtime.context.new_page.side_effect = failure
    runtime.context.close.side_effect = RuntimeError("context close failed")
    runtime.browser.close.side_effect = RuntimeError("browser close failed")
    runtime.relay.close.side_effect = RuntimeError("relay close failed")

    with pytest.raises(RuntimeError) as caught:
        cloak.build_cloak_driver()
    assert caught.value is failure
    runtime.context.close.assert_called_once_with()
    runtime.browser.close.assert_called_once_with()
    runtime.relay.close.assert_called_once_with()


def test_persistent_context_without_browser_is_closed_once_on_failure(runtime):
    runtime.cfg.CLOAK_USER_DATA_DIR = "/shared/registration-profile"
    runtime.context.browser = None
    runtime.context.new_page.side_effect = RuntimeError("new page failed")

    with pytest.raises(RuntimeError, match="new page failed"):
        cloak.build_cloak_driver()
    runtime.context.close.assert_called_once_with()
    runtime.browser.close.assert_not_called()
    runtime.relay.close.assert_called_once_with()


def test_cancelled_startup_closes_resources(runtime):
    runtime.context.new_page.side_effect = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        cloak.build_cloak_driver()
    runtime.context.close.assert_called_once_with()
    runtime.browser.close.assert_called_once_with()
    runtime.relay.close.assert_called_once_with()
