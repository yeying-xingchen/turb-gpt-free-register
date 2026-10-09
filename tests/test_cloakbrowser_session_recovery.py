"""Cloak 查活低占用轮询与同会话恢复的离线回归。"""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core import account_liveness, cloakbrowser_liveness as cloak

EMAIL = "check@example.test"
SESSION = {"accessToken": "test-token", "user": {"email": EMAIL}}
PASSWORD = {"url": "https://auth.openai.com/log-in/password", "password": True}
SESSION_PAGE = {"url": "https://chatgpt.com/"}


@pytest.fixture
def runtime(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(cloak, "time", SimpleNamespace(
        monotonic=lambda: clock[0], time=lambda: clock[0],
    ))
    driver = Mock()
    driver.page.wait_for_timeout.side_effect = lambda ms: clock.__setitem__(0, clock[0] + ms / 1000)
    monkeypatch.setattr(cloak, "_maybe_accept", Mock())
    monkeypatch.setattr(cloak, "_type_any", Mock(return_value=True))
    monkeypatch.setattr(cloak, "_submit_auth_form", Mock(return_value=True))
    monkeypatch.setattr(account_liveness, "_account_registration_password", Mock(return_value="password"))
    monkeypatch.setattr(cloak, "_page_state", Mock(return_value=SESSION_PAGE))
    monkeypatch.setattr(cloak, "build_cloak_driver", Mock(return_value=(driver, SimpleNamespace(raw={}))))
    monkeypatch.setattr(cloak, "_install_live_check_data_saver", Mock(return_value=None))
    return driver, clock


def test_stable_submitted_page_reduces_scans_without_resubmitting(runtime, monkeypatch):
    driver, clock = runtime
    scans = Mock(side_effect=lambda _: PASSWORD if clock[0] < 30 else SESSION_PAGE)
    monkeypatch.setattr(cloak, "_page_state", scans)
    monkeypatch.setattr(cloak, "_read_session", Mock(return_value=SESSION))

    assert cloak.login_with_cloak(EMAIL)[0] == SESSION
    # 固定150ms轮询需约200次；稳定3秒后1秒一次，保持跳转发现延迟<=1秒。
    assert scans.call_count < 45
    assert 30 <= clock[0] <= 31
    cloak._type_any.assert_called_once()
    cloak._submit_auth_form.assert_called_once()
    driver.quit.assert_called_once()


def test_page_changes_restore_fast_polling(runtime):
    _, clock = runtime
    cadence = cloak._PollCadence()
    assert cadence.delay(PASSWORD, True) == 150
    clock[0] = 1.5
    assert cadence.delay(PASSWORD, True) == 400
    clock[0] = 4
    assert cadence.delay(PASSWORD, True) == 1000
    assert cadence.delay({**PASSWORD, "errors": ["invalid code"]}, True) == 150


@pytest.mark.parametrize("result", [
    {"status": 503}, {"status": 429, "retryAfter": "3"},
    {"failure": "network"}, {"failure": "timeout"}, {"status": 200, "data": None},
])
def test_transient_session_error_recovers_in_same_browser(runtime, monkeypatch, result):
    driver, clock = runtime
    reads_at = []
    responses = iter([result, {"status": 200, "data": SESSION}])

    def evaluate(_script):
        reads_at.append(clock[0])
        return next(responses)

    driver.page.evaluate.side_effect = evaluate
    assert cloak.login_with_cloak(EMAIL)[0] == SESSION
    assert len(reads_at) == 2
    assert reads_at[1] - reads_at[0] >= (3 if result.get("status") == 429 else 0.5)
    cloak.build_cloak_driver.assert_called_once()
    cloak._submit_auth_form.assert_not_called()
    driver.get.assert_called_once()
    driver.quit.assert_called_once()


@pytest.mark.parametrize("status", [400, 401, 403])
def test_session_permanent_http_error_does_not_retry_locally(runtime, status):
    driver, _ = runtime
    driver.page.evaluate.return_value = {"status": status, "data": {"error": "credentials"}}
    with pytest.raises(cloak._BrowserAuthError) as caught:
        cloak.login_with_cloak(EMAIL)
    assert caught.value.response.status_code == status
    driver.page.evaluate.assert_called_once()
    driver.quit.assert_called_once()


def test_transient_session_retry_is_bounded_and_outer_retryable(runtime):
    driver, clock = runtime
    driver.page.evaluate.return_value = {"failure": "network"}
    with pytest.raises(cloak._SessionReadTransient) as caught:
        cloak.login_with_cloak(EMAIL)
    assert driver.page.evaluate.call_count == 3
    assert account_liveness._is_retryable_network_error(caught.value)
    assert clock[0] == pytest.approx(1.5)
    driver.quit.assert_called_once()


def test_session_retry_keeps_detecting_dead_account(runtime, monkeypatch):
    driver, _ = runtime
    driver.page.evaluate.return_value = {"status": 503}
    monkeypatch.setattr(cloak, "_page_state", Mock(side_effect=[
        SESSION_PAGE, {"url": "https://auth.openai.com/error?error=account_deleted"},
    ]))
    with pytest.raises(cloak.AccountUnusableError):
        cloak.login_with_cloak(EMAIL)
    driver.page.evaluate.assert_called_once()
    driver.quit.assert_called_once()


def test_session_dead_response_is_not_retried(runtime):
    driver, _ = runtime
    driver.page.evaluate.return_value = {"status": 403, "data": {"error": {"code": "account_deleted"}}}
    with pytest.raises(cloak.AccountUnusableError):
        cloak.login_with_cloak(EMAIL)
    driver.page.evaluate.assert_called_once()


def test_session_unknown_browser_error_is_not_swallowed(runtime):
    driver, _ = runtime
    driver.page.evaluate.side_effect = RuntimeError("Target page closed")
    with pytest.raises(RuntimeError, match="closed"):
        cloak.login_with_cloak(EMAIL)
    driver.page.evaluate.assert_called_once()


def test_session_retry_respects_deadline(runtime, monkeypatch):
    driver, clock = runtime
    monkeypatch.setattr(cloak.cfg, "CLOAK_SELENIUM_TIMEOUT", 30)
    driver.page.evaluate.return_value = {"status": 429, "retryAfter": "30"}
    with pytest.raises(RuntimeError, match="timeout"):
        cloak.login_with_cloak(EMAIL)
    assert clock[0] == pytest.approx(30)
    driver.page.evaluate.assert_called_once()


@pytest.mark.parametrize("login_error", [None, RuntimeError("login failed")])
@pytest.mark.parametrize("stop_error", [None, RuntimeError("stop failed")])
def test_saver_cleanup_always_precedes_browser_quit(runtime, monkeypatch, login_error, stop_error):
    driver, _ = runtime
    saver = Mock()
    order = []

    def stop():
        order.append("stop")
        if stop_error:
            raise stop_error

    saver.stop.side_effect = stop
    driver.quit.side_effect = lambda: order.append("quit")
    monkeypatch.setattr(cloak, "_install_live_check_data_saver", Mock(return_value=saver))
    monkeypatch.setattr(cloak, "_login", Mock(return_value=SESSION, side_effect=login_error))
    if login_error:
        with pytest.raises(RuntimeError) as caught:
            cloak.login_with_cloak(EMAIL)
        assert caught.value is login_error
    else:
        assert cloak.login_with_cloak(EMAIL)[0] == SESSION
    assert order == ["stop", "quit"]
