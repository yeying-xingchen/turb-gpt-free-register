"""Cloak 查活的离线流程回归：不得访问真实账号或邮件服务。"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from config import live_check
from core import account_liveness as liveness
from core import cloakbrowser_liveness as cloak

EMAIL = "user@example.com"
SESSION = {"accessToken": "fresh-token", "user": {"email": EMAIL, "id": "user-1"}}


@pytest.fixture
def browser(monkeypatch):
    driver = MagicMock()
    opened = SimpleNamespace(raw={"proxy_pool_target": "socks5://proxy:1080", "locale": {"locale": "en-US"}})
    factory = MagicMock(return_value=(driver, opened))
    monkeypatch.setattr(cloak, "build_cloak_driver", factory)
    monkeypatch.setattr(cloak, "_maybe_accept", MagicMock())
    for name in ("_type_any", "_submit_email_step", "_submit_code", "_click_resend_email_otp"):
        monkeypatch.setattr(cloak, name, MagicMock())
    monkeypatch.setattr(cloak, "_submit_auth_form", MagicMock(return_value=True))
    monkeypatch.setattr(cloak, "_click_passwordless_signup_if_present", MagicMock(return_value={"ok": True}))
    monkeypatch.setattr(cloak, "_read_session", MagicMock(return_value=SESSION))
    monkeypatch.setattr(cloak, "wait_for_otp", MagicMock(return_value="123456"))
    monkeypatch.setattr(liveness, "_account_registration_password", MagicMock(return_value="saved-password"))
    monkeypatch.setattr(liveness, "_totp_for_account", MagicMock())
    monkeypatch.setattr(liveness, "_fresh_totp_code", MagicMock(return_value="654321"))
    monkeypatch.setattr(live_check, "LIVE_CHECK_DRIVER", "cloak")
    return driver, factory


def page(step, **overrides):
    states = {
        "email": {"url": "https://chatgpt.com/auth/login", "email": True},
        "password": {"url": "https://auth.openai.com/log-in/password", "password": True},
        "email_otp": {"url": "https://auth.openai.com/email-verification", "code": True},
        "mfa": {"url": "https://auth.openai.com/mfa-challenge/totp", "code": True},
        "session": {"url": "https://chatgpt.com/"},
    }
    return {**states[step], **overrides}


def states(monkeypatch, *values):
    monkeypatch.setattr(cloak, "_page_state", MagicMock(side_effect=list(values)))


def test_complete_login_refreshes_token_and_keeps_email_source(browser, monkeypatch):
    driver, factory = browser
    states(monkeypatch, *(page(x) for x in ("email", "password", "email_otp", "mfa", "session")))
    result = liveness.check_account_liveness(EMAIL, proxy="", email_source="remail")
    assert result["status"] == "live"
    assert result["access_token"] == "fresh-token"
    assert result["fingerprint"]["driver"] == "cloak"
    factory.assert_called_once_with(proxy="", isolated=True, force_proxy=True)
    cloak.wait_for_otp.assert_called_once()
    assert cloak.wait_for_otp.call_args.kwargs["email_source"] == "remail"
    assert [call.args[1] for call in cloak._submit_code.call_args_list] == ["123456", "654321"]
    assert cloak._type_any.call_args_list[1].args[2] == "saved-password"
    driver.quit.assert_called_once()
    assert not liveness.is_checking(EMAIL)


def test_no_password_uses_passwordless_login(browser, monkeypatch):
    liveness._account_registration_password.return_value = ""
    states(monkeypatch, *(page(x) for x in ("email", "password", "email_otp", "session")))
    result = liveness.check_account_liveness(EMAIL)
    assert result["ok"]
    cloak._click_passwordless_signup_if_present.assert_called_once_with(browser[0])
    assert cloak._type_any.call_count == 1


@pytest.mark.parametrize("session", [
    {"accessToken": "wrong-token", "user": {"email": "other@example.com"}},
    {"accessToken": "unknown-token", "user": {}},
    {"user": {"email": EMAIL}},
])
def test_does_not_accept_incomplete_or_wrong_account_session(browser, monkeypatch, session):
    monkeypatch.setattr(cloak, "_login", MagicMock(return_value=session))
    result = liveness.check_account_liveness(EMAIL)
    assert result["status"] == "failed"
    assert "access_token" not in result
    browser[0].quit.assert_called_once()


@pytest.mark.parametrize("error,status", [
    (cloak._BrowserAuthError(403, "account_deactivated"), "deactivated"),
    (cloak._BrowserAuthError(401, "invalid_password"), "failed"),
    (cloak._BrowserAuthError(429, "rate_limit_exceeded"), "failed"),
    (RuntimeError("HTTP 403 blocked"), "failed"),
    (RuntimeError("browser timeout"), "failed"),
])
def test_failures_are_classified_and_always_close_browser(browser, monkeypatch, error, status):
    monkeypatch.setattr(cloak, "_login", MagicMock(side_effect=error))
    result = liveness.check_account_liveness(EMAIL)
    assert result["status"] == status
    if isinstance(error, cloak._BrowserAuthError) and error.response.status_code == 401:
        assert result["retryable"] is False
        assert "密码不正确" in result["error"]
    browser[0].quit.assert_called_once()


@pytest.mark.parametrize("path,expected", [
    ("/add-phone", "手机号验证"),
    ("/phone-verification", "手机号验证"),
    ("/create-account/password", "资料补全"),
    ("/about-you", "资料补全"),
])
def test_manual_steps_fail_without_registering_or_sending_sms(browser, monkeypatch, path, expected):
    states(monkeypatch, {"url": "https://auth.openai.com" + path, "code": True})
    result = liveness.check_account_liveness(EMAIL)
    assert result["status"] == "failed"
    assert expected in result["error"]
    cloak._submit_code.assert_not_called()
    cloak.wait_for_otp.assert_not_called()


def test_waits_for_navigation_without_resubmitting_password(browser, monkeypatch):
    states(monkeypatch, page("password"), page("password"), page("password"), page("session"))
    assert liveness.check_account_liveness(EMAIL)["ok"]
    cloak._type_any.assert_called_once()
    cloak._submit_auth_form.assert_called_once()


def test_invalid_email_code_resends_and_uses_fresh_code(browser, monkeypatch):
    states(monkeypatch, page("email_otp"), page("email_otp", errors=["invalid code"]), page("session"))
    cloak.wait_for_otp.side_effect = ["111111", "222222"]
    assert liveness.check_account_liveness(EMAIL, email_source="outlook")["ok"]
    cloak._click_resend_email_otp.assert_called_once()
    assert [call.args[1] for call in cloak._submit_code.call_args_list] == ["111111", "222222"]
    assert all(call.kwargs["email_source"] == "outlook" for call in cloak.wait_for_otp.call_args_list)


def test_repeated_email_code_is_not_resubmitted(browser, monkeypatch):
    states(monkeypatch, page("email_otp"), page("email_otp", invalid=True))
    result = liveness.check_account_liveness(EMAIL)
    assert result["status"] == "failed"
    assert "新的验证码" in result["error"]
    cloak._submit_code.assert_called_once()


def test_mfa_retries_only_once_on_explicit_invalid_code(browser, monkeypatch):
    states(monkeypatch, page("mfa"), page("mfa", errors=["invalid_totp"]), page("mfa", invalid=True))
    liveness._fresh_totp_code.side_effect = ["111111", "222222"]
    result = liveness.check_account_liveness(EMAIL)
    assert result["status"] == "failed"
    assert "TOTP 验证失败" in result["error"]
    assert cloak._submit_code.call_count == 2
    cloak.wait_for_otp.assert_not_called()


def test_missing_totp_secret_fails_without_email_fallback(browser, monkeypatch):
    states(monkeypatch, page("mfa"))
    liveness._totp_for_account.side_effect = RuntimeError("账号要求 MFA，但没有保存 2FA 密钥")
    result = liveness.check_account_liveness(EMAIL)
    assert result["status"] == "failed"
    assert "没有保存 2FA" in result["error"]
    cloak.wait_for_otp.assert_not_called()


def test_dead_api_response_is_detected_after_navigation(browser, monkeypatch):
    observer = cloak._AuthResponses()
    response = MagicMock(url="https://auth.openai.com/api/accounts/email-otp/validate", status=403)
    response.text.return_value = '{"error":{"code":"account_deleted"}}'
    response.json.return_value = {"error": {"code": "account_deleted"}}
    observer(response)
    with pytest.raises(cloak.AccountUnusableError):
        cloak._check_error(page("session"), observer)


@pytest.mark.parametrize("url", [
    "https://analytics.example/api/accounts/login", "https://auth.openai.com/favicon.ico",
])
def test_non_auth_network_errors_do_not_poison_login(url):
    observer = cloak._AuthResponses()
    observer(MagicMock(url=url, status=403))
    assert observer.error == ""


def test_mfa_precedes_email_code_and_foreign_domains_are_ignored():
    assert cloak._step(page("mfa")) == "mfa"
    assert cloak._step({"url": "https://other.example/log-in", "email": True}) == "unknown"
    assert cloak._step({"url": "https://chatgpt.com.evil.example/"}) == "unknown"


def test_transient_empty_page_does_not_resubmit_password(browser, monkeypatch):
    states(monkeypatch, page("password"), {"url": "https://auth.openai.com/log-in/password"}, page("password"), page("session"))
    assert liveness.check_account_liveness(EMAIL)["ok"]
    cloak._type_any.assert_called_once()
    cloak._submit_auth_form.assert_called_once()


def test_queue_runs_cloak_and_writes_fresh_token(browser, monkeypatch):
    from core import live_check_service as service
    from config import proxy as proxy_cfg

    monkeypatch.setattr(cloak, "_login", MagicMock(return_value=SESSION))
    monkeypatch.setattr(service, "_QUEUE_SLOTS", MagicMock())
    monkeypatch.setattr(service, "_append_log", MagicMock())
    monkeypatch.setattr(service.db, "mark_account_live_check_running", MagicMock(return_value=True))
    monkeypatch.setattr(service.db, "get_account", MagicMock(return_value={"email_source": "remail"}))
    update = MagicMock()
    monkeypatch.setattr(service.db, "update_account_liveness", update)
    monkeypatch.setattr(proxy_cfg, "PLAN_CHECK_PROXY", [])
    monkeypatch.setattr(proxy_cfg, "PROXY_POOL", [])
    monkeypatch.setattr(service, "resolve_plan_check_route", MagicMock(return_value={"proxy": "", "network_route": "direct"}))
    monkeypatch.setattr(service, "open_plan_check_proxy", MagicMock(return_value=("", None)))
    result = service._run_live_check(account_id=123, email=EMAIL, proxy="", trigger="manual")
    assert result["status"] == "live"
    assert update.call_args.args[0] == 123
    assert update.call_args.args[1]["access_token"] == "fresh-token"
    assert cloak._login.call_args.kwargs["email_source"] == "remail"
    service._QUEUE_SLOTS.release.assert_called_once()


def test_unknown_driver_fails_without_starting_browser(browser, monkeypatch):
    monkeypatch.setattr(live_check, "LIVE_CHECK_DRIVER", "typo")
    result = liveness.check_account_liveness(EMAIL)
    assert result["status"] == "failed"
    assert "LIVE_CHECK_DRIVER" in result["error"]
    browser[1].assert_not_called()


def test_cloak_failure_does_not_fall_back_to_protocol(browser, monkeypatch):
    protocol = MagicMock()
    monkeypatch.setattr(liveness, "_login_via_full_web_flow", protocol)
    browser[1].side_effect = RuntimeError("未安装 cloakbrowser")
    result = liveness.check_account_liveness(EMAIL)
    assert result["status"] == "failed"
    assert "未安装 cloakbrowser" in result["error"]
    protocol.assert_not_called()
