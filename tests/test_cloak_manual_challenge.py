"""Offline contracts for safe, manual Cloudflare challenge waiting."""
from contextlib import contextmanager, nullcontext
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock, Mock
import sys

import pytest

from config import live_check
from core import account_liveness, cloakbrowser_driver as adapter, cloakbrowser_liveness as cloak

EMAIL = "manual@example.test"
SESSION = {"accessToken": "offline-token", "user": {"email": EMAIL}}
PASSWORD = {"url": "https://auth.openai.com/log-in/password", "password": True}
CHALLENGE = {"url": "https://auth.openai.com/log-in/password", "challenge": True}
SESSION_PAGE = {"url": "https://chatgpt.com/"}


@pytest.fixture
def runtime(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(cloak, "time", SimpleNamespace(monotonic=lambda: clock[0], time=lambda: clock[0]))
    monkeypatch.setattr(live_check, "LIVE_CHECK_MANUAL_VERIFICATION", True)
    monkeypatch.setattr(live_check, "LIVE_CHECK_MANUAL_MODE", "web")
    monkeypatch.setattr(live_check, "LIVE_CHECK_MANUAL_TIMEOUT", 600)
    page = Mock(spec=["wait_for_timeout", "is_closed", "bring_to_front", "evaluate"])
    page.is_closed.return_value = False
    page.wait_for_timeout.side_effect = lambda ms: clock.__setitem__(0, clock[0] + ms / 1000)
    driver = SimpleNamespace(page=page, context=Mock(), get=Mock(), quit=Mock(),
                             _manual_verification_wait=nullcontext,
                             _manual_verification_checkpoint=Mock(return_value=None))
    monkeypatch.setattr(cloak, "_maybe_accept", Mock())
    monkeypatch.setattr(cloak, "_type_any", Mock(return_value=True))
    monkeypatch.setattr(cloak, "_submit_auth_form", Mock(return_value=True))
    monkeypatch.setattr(cloak, "_read_session", Mock(return_value=SESSION))
    monkeypatch.setattr(account_liveness, "_account_registration_password", Mock(return_value="offline-password"))
    return driver, clock


def response(url, headers=None, status=403, document=False, code=""):
    obj = Mock(url=url, status=status)
    obj.headers = headers if headers is not None else MagicMock()
    obj.request = SimpleNamespace(resource_type="document" if document else "fetch")
    obj.text.return_value = '{}'
    obj.json.return_value = {"error": {"code": code}}
    return obj


@pytest.mark.parametrize("url,document", [
    ("https://auth.openai.com/log-in", True),
    ("https://chatgpt.com/auth/login", True),
    ("https://auth.openai.com/api/accounts/login", False),
    ("https://chatgpt.com/api/auth/session", False),
])
def test_explicit_header_on_documents_and_auth_apis(url, document):
    observer = cloak._AuthResponses()
    obj = response(url, {"CF-Mitigated": "challenge"}, document=document)
    observer(obj)
    assert observer.challenge is True
    assert observer.challenge_headers == {"cf-mitigated": "challenge"}
    assert observer.error == ""
    obj.json.assert_not_called()


@pytest.mark.parametrize("url,document", [
    ("https://chatgpt.com/backend-api/accounts/check", False),
    ("https://chatgpt.com/backend-api/sentinel/req", False),
    ("https://auth.openai.com/favicon.ico", False),
    ("https://untrusted.example/auth", True),
])
def test_anonymous_or_foreign_response_cannot_trigger_wait(url, document):
    observer = cloak._AuthResponses()
    observer(response(url, {"cf-mitigated": "challenge"}, document=document))
    assert observer.challenge is False
    assert observer.error == ""


def test_mock_headers_do_not_trigger_challenge():
    observer = cloak._AuthResponses()
    observer(response("https://auth.openai.com/api/accounts/login", code="invalid_password"))
    assert observer.challenge is False
    assert observer.code == "invalid_password"


def test_page_scan_only_uses_title_and_visible_turnstile_for_challenge():
    page = Mock()
    page.evaluate.return_value = {"challenge": False, "mfa": True}
    assert cloak._page_state(SimpleNamespace(page=page))["challenge"] is False
    script = page.evaluate.call_args.args[0]
    assert "document.title" in script and "just a moment" in script
    assert "iframe" in script and "visible(el)" in script and "turnstile" in script
    assert "body.innerText" not in script
    assert cloak._step({"url": "https://auth.openai.com/mfa-challenge/totp", "code": True}) == "mfa"
    assert cloak._step({**PASSWORD, "challenge": True}) == "waiting"


def test_long_manual_wait_extends_login_deadlines_and_preserves_driver(runtime, monkeypatch):
    driver, clock = runtime
    events = []
    context = driver.context

    @contextmanager
    def wait():
        events.append("start")
        try:
            yield
        finally:
            events.append("end")

    driver._manual_verification_wait = wait
    driver._manual_verification_checkpoint.side_effect = lambda: events.append("checkpoint")
    # 等待超过通常300秒总deadline；验证后仍在同一driver登录。
    snapshots = iter([CHALLENGE, CHALLENGE, PASSWORD, SESSION_PAGE])
    monkeypatch.setattr(cloak, "_page_state", Mock(side_effect=lambda _: next(snapshots)))
    driver.page.wait_for_timeout.side_effect = lambda ms: clock.__setitem__(0, clock[0] + (200 if events and events[-1] == "checkpoint" else ms / 1000))
    assert cloak._login(driver, EMAIL, email_source=None, responses=cloak._AuthResponses()) == SESSION
    assert clock[0] >= 400
    assert events[:2] == ["start", "checkpoint"] and events[-1] == "end"
    assert driver.context is context
    driver.get.assert_called_once()
    cloak._type_any.assert_called_once_with(driver, cloak._PASSWORD_SELECTORS, "offline-password", timeout=90)
    cloak._submit_auth_form.assert_called_once()
    driver.quit.assert_not_called()


def test_header_challenge_clears_on_normal_actionable_page(runtime, monkeypatch):
    driver, _ = runtime
    observer = cloak._AuthResponses()
    observer(response("https://auth.openai.com/api/accounts/login", {"cf-mitigated": "challenge"}))
    monkeypatch.setattr(cloak, "_page_state", Mock(return_value=PASSWORD))
    current, elapsed = cloak._wait_manual_challenge(driver, PASSWORD, observer)
    assert current == PASSWORD and elapsed == 0.5
    assert observer.challenge is False and observer.error == ""
    cloak._submit_auth_form.assert_not_called()
    cloak._type_any.assert_not_called()


@pytest.mark.parametrize("code", ["invalid_password", "account_deleted"])
def test_challenge_cleanup_preserves_unrelated_auth_error(runtime, monkeypatch, code):
    driver, _ = runtime
    observer = cloak._AuthResponses()
    observer(response("https://auth.openai.com/api/accounts/login", code=code))
    if code == "account_deleted":
        observer.dead_code = code
    observer(response("https://auth.openai.com/api/accounts/login", {"cf-mitigated": "challenge"}))
    monkeypatch.setattr(cloak, "_page_state", Mock(return_value=PASSWORD))
    if code == "account_deleted":
        with pytest.raises(cloak.AccountUnusableError):
            cloak._wait_manual_challenge(driver, CHALLENGE, observer)
    else:
        cloak._wait_manual_challenge(driver, CHALLENGE, observer)
    assert observer.code == code and observer.error == "HTTP 403 " + code


@pytest.mark.parametrize("reason", ["disabled", "timeout", "closed", "closed_error"])
def test_manual_wait_terminal_errors_are_clear_and_nonretryable(runtime, monkeypatch, reason):
    driver, _ = runtime
    monkeypatch.setattr(cloak, "_page_state", Mock(return_value=CHALLENGE))
    if reason == "disabled":
        monkeypatch.setattr(live_check, "LIVE_CHECK_MANUAL_VERIFICATION", False)
    elif reason == "timeout":
        monkeypatch.setattr(live_check, "LIVE_CHECK_MANUAL_TIMEOUT", 1)
    elif reason == "closed":
        driver.page.is_closed.return_value = True
    else:
        driver.page.wait_for_timeout.side_effect = RuntimeError("Target page, context or browser has been closed")
    with pytest.raises(cloak._ManualVerificationError) as caught:
        cloak._wait_manual_challenge(driver, CHALLENGE, cloak._AuthResponses())
    assert caught.value.retryable is False
    assert not account_liveness._is_retryable_network_error(caught.value)
    result = account_liveness._failure_result(caught.value, "offline")
    assert result["retryable"] is False
    assert "人工" in str(caught.value)
    cloak._submit_auth_form.assert_not_called()


def test_cancel_exits_callback_context(runtime, monkeypatch):
    driver, _ = runtime
    events = []

    @contextmanager
    def wait():
        events.append("start")
        try:
            yield
        finally:
            events.append("end")

    driver._manual_verification_wait = wait
    driver._manual_verification_checkpoint.side_effect = RuntimeError("任务已取消")
    with pytest.raises(RuntimeError, match="任务已取消"):
        cloak._wait_manual_challenge(driver, CHALLENGE, cloak._AuthResponses())
    assert events == ["start", "end"]
    driver.page.wait_for_timeout.assert_not_called()


def test_focus_request_is_handled_in_waiting_browser_thread(runtime, monkeypatch):
    driver, _ = runtime
    monkeypatch.setattr(live_check, "LIVE_CHECK_MANUAL_MODE", "window")
    driver._manual_verification_checkpoint.return_value = True
    monkeypatch.setattr(cloak, "_page_state", Mock(return_value=PASSWORD))
    cloak._wait_manual_challenge(driver, CHALLENGE, cloak._AuthResponses())
    driver.page.bring_to_front.assert_called_once()


def test_session_header_is_passed_to_browser_auth_error(runtime, monkeypatch):
    driver, _ = runtime
    monkeypatch.undo()  # use the actual session reader (no real network; evaluate remains mocked)
    driver.page.evaluate.return_value = {"status": 403, "headers": {"cf-mitigated": "challenge"}}
    with pytest.raises(cloak._BrowserAuthError) as caught:
        cloak._read_session(driver)
    assert caught.value.response.headers["cf-mitigated"] == "challenge"
    assert account_liveness._is_cloudflare_challenge(caught.value)


def test_session_challenge_resumes_in_same_login(runtime, monkeypatch):
    driver, _ = runtime
    observer = cloak._AuthResponses()
    monkeypatch.setattr(cloak, "_page_state", Mock(side_effect=[SESSION_PAGE, CHALLENGE, SESSION_PAGE]))
    error = cloak._BrowserAuthError(403, "cloudflare_challenge", {"cf-mitigated": "challenge"})
    cloak._read_session.side_effect = [error, SESSION]
    assert cloak._login(driver, EMAIL, email_source=None, responses=observer) == SESSION
    assert cloak._read_session.call_count == 2
    assert observer.challenge is False
    driver.get.assert_called_once()
    cloak._type_any.assert_not_called()
    cloak._submit_auth_form.assert_not_called()


def test_challenge_page_never_submits_form_or_code(monkeypatch):
    driver = SimpleNamespace(page=Mock())
    monkeypatch.setattr(cloak, "_page_state", Mock(return_value={**PASSWORD, "code": True, "challenge": True}))
    assert cloak._submit_auth_form(driver, cloak._PASSWORD_SELECTORS) is False
    cloak._submit_code(driver, "123456")
    assert cloak._click_auth_choice(driver, resend=False) is False
    driver.page.locator.assert_not_called()


def test_wait_defaults_to_noop_callbacks(runtime, monkeypatch):
    driver, _ = runtime
    del driver._manual_verification_wait
    del driver._manual_verification_checkpoint
    monkeypatch.setattr(cloak, "_page_state", Mock(return_value=PASSWORD))
    assert cloak._wait_manual_challenge(driver, CHALLENGE, cloak._AuthResponses())[0] == PASSWORD


@pytest.mark.parametrize("enabled,headless", [(True, True), (False, None)])
def test_login_launch_override_and_callback_binding(runtime, monkeypatch, enabled, headless):
    from core.manual_verification import bind_callbacks
    driver, _ = runtime
    factory = Mock(return_value=(driver, SimpleNamespace(raw={})))
    monkeypatch.setattr(cloak, "build_cloak_driver", factory)
    monkeypatch.setattr(cloak, "_install_live_check_data_saver", Mock(return_value=None))
    monkeypatch.setattr(cloak, "_login", Mock(return_value=SESSION))
    monkeypatch.setattr(live_check, "LIVE_CHECK_MANUAL_VERIFICATION", enabled)
    wait = Mock(return_value=nullcontext())
    checkpoint = Mock()
    with bind_callbacks(wait, checkpoint):
        assert cloak.login_with_cloak(EMAIL, proxy="")[0] == SESSION
    factory.assert_called_once_with(proxy="", isolated=True, force_proxy=True, headless=headless)
    assert driver._manual_verification_wait is wait
    assert driver._manual_verification_checkpoint is checkpoint
    driver.quit.assert_called_once()


@pytest.mark.parametrize("override", [False, True, None])
def test_headless_override_does_not_mutate_global_cfg(monkeypatch, override):
    cfg = SimpleNamespace(CLOAK_HEADLESS=True, CLOAK_GEOIP=False, CLOAK_USE_PROXY=False)
    monkeypatch.setattr(adapter, "_cfg", cfg)
    monkeypatch.setattr(adapter, "_BROWSER_GATE", Mock())
    monkeypatch.setattr(adapter, "_build_cloak_locale_options", Mock(return_value={}))
    package = ModuleType("cloakbrowser")
    package.launch = Mock()
    package.launch_persistent_context = Mock()
    monkeypatch.setitem(sys.modules, "cloakbrowser", package)
    driver, _ = adapter.build_cloak_driver(proxy="", isolated=True, headless=override)
    assert package.launch.call_args.kwargs["headless"] is (True if override is None else override)
    assert cfg.CLOAK_HEADLESS is True
    driver.quit()
