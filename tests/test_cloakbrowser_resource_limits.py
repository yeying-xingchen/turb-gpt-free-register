"""Offline resource admission and cleanup contracts; never launch real browsers."""
import sys
import threading
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from core import cloakbrowser_driver as cloak


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.setattr(cloak, "_cfg", SimpleNamespace(
        CLOAK_MAX_CONCURRENT=1, CLOAK_KEEP_BROWSER_OPEN=True,
        CLOAK_USE_PROXY=False, CLOAK_GEOIP=False, CLOAK_MEMORY_SAVER=False,
        CLOAK_USER_DATA_DIR="/unused/shared-profile",
    ))
    gate = cloak._BrowserGate()
    monkeypatch.setattr(cloak, "_BROWSER_GATE", gate)
    monkeypatch.setattr(cloak, "_check_registration_stop", lambda: None)
    monkeypatch.setattr(cloak, "_build_cloak_locale_options", lambda proxy: {})
    page = Mock(spec=["set_default_navigation_timeout", "set_default_timeout"])
    context = Mock(spec=["new_page", "close", "browser"])
    browser = Mock(spec=["new_context", "close"])
    browser.new_context.return_value = context
    context.browser = browser
    context.new_page.return_value = page
    package = ModuleType("cloakbrowser")
    package.launch = Mock(return_value=browser)
    package.launch_persistent_context = Mock(return_value=context)
    monkeypatch.setitem(sys.modules, "cloakbrowser", package)
    return SimpleNamespace(gate=gate, package=package, browser=browser,
                           context=context, page=page)


def test_isolated_browser_uses_slot_despite_keep_open(runtime):
    driver, _ = cloak.build_cloak_driver("", isolated=True)
    assert runtime.gate.snapshot()["in_use"] == 1
    runtime.package.launch.assert_called_once()
    runtime.package.launch_persistent_context.assert_not_called()
    driver.quit()
    assert runtime.gate.snapshot()["in_use"] == 0


def test_fail_closed_timeout_preserves_slot_count_and_allows_retry(runtime):
    gate = runtime.gate
    assert gate.acquire(timeout=0)
    errors = []
    depths = []

    def waiter():
        try:
            gate.acquire(timeout=0, fail_open=False)
        except Exception as exc:
            errors.append(exc)
        depths.append(getattr(gate._local, "depth", 0))

    worker = threading.Thread(target=waiter)
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], TimeoutError)
    assert depths == [0]
    assert gate.snapshot()["in_use"] == 1
    gate.release()
    assert gate.acquire(timeout=0, fail_open=False)
    gate.release()
    assert gate.snapshot()["in_use"] == 0


def test_isolated_timeout_does_not_launch_and_closes_prepared_relay(runtime, monkeypatch):
    from config import proxy as proxy_cfg
    from core import proxy_chain

    relay = Mock(spec=["close"])
    monkeypatch.setattr(proxy_cfg, "pick_proxy", lambda: "http://pool.example:8080")
    monkeypatch.setattr(proxy_chain, "open_proxy_pool_proxy",
                        lambda proxy: ("http://127.0.0.1:18080", relay))
    acquire = Mock(side_effect=TimeoutError("capacity exhausted"))
    monkeypatch.setattr(runtime.gate, "acquire", acquire)
    with pytest.raises(TimeoutError, match="capacity exhausted"):
        cloak.build_cloak_driver(isolated=True, force_proxy=True)
    acquire.assert_called_once_with(fail_open=False)
    runtime.package.launch.assert_not_called()
    runtime.package.launch_persistent_context.assert_not_called()
    relay.close.assert_called_once()
    assert runtime.gate.snapshot()["in_use"] == 0


def test_relay_close_error_still_releases_slot_exactly_once(runtime):
    assert runtime.gate.acquire(timeout=0)
    relay = Mock(spec=["close"])
    relay.close.side_effect = RuntimeError("relay close failed")
    driver = cloak.CloakSeleniumDriver(
        runtime.browser, runtime.context, runtime.page,
        proxy_relay=relay, gate_slot=True,
    )
    with pytest.raises(RuntimeError, match="relay close failed"):
        driver.quit()
    assert runtime.gate.snapshot()["in_use"] == 0
    runtime.context.close.assert_called_once()
    runtime.browser.close.assert_called_once()
    # A repeated quit must not release another browser's new slot.
    assert runtime.gate.acquire(timeout=0)
    driver.quit()
    assert runtime.gate.snapshot()["in_use"] == 1
    relay.close.assert_called_once()
    runtime.gate.release()
