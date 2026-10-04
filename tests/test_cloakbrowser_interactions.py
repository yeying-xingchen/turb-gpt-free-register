"""Exercise native Playwright helpers without importing a real browser package."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from core import cloakbrowser_liveness as cloak


EMAIL_OTP = {"url": "https://auth.openai.com/email-verification", "code": True}
PASSWORD = {"url": "https://auth.openai.com/log-in/password", "password": True}


def group(nodes):
    result = Mock()
    result.count.return_value = len(nodes)
    result.nth.side_effect = nodes.__getitem__
    result.first = nodes[0] if nodes else Mock()
    return result


@pytest.fixture
def ui(monkeypatch):
    pinned = {}
    page = Mock()
    driver = SimpleNamespace(page=page)

    def node(text="", **attrs):
        el = Mock()
        el.count.return_value = 1
        el.is_visible.return_value = el.is_enabled.return_value = True
        el.get_attribute.side_effect = attrs.get
        el.inner_text.return_value = text
        el.evaluate.side_effect = lambda script, token: pinned.update({token: el})
        return el

    form = node(action="/api/accounts/email-otp/validate")
    submit = node("Continue", type="submit")
    outside = node("Create account", type="submit")
    inputs = [node()]
    choices = []
    buttons = [submit]

    def page_locator(selector):
        if ":visible" in selector:
            # Cloak humanize 的隔离世界解析器不支持 :visible/链式定位，命中会抛
            # UnsupportedHumanizeSelectorError，线上表现为“Cloak 页面交互失败”。
            raise RuntimeError(f"UnsupportedHumanizeSelectorError: {selector}")
        if selector.startswith('[data-cloak-live-target="'):
            return pinned[selector.split('"')[1]]
        if selector.startswith("button,a"):
            return group(choices)
        return group(inputs)

    page.locator.side_effect = page_locator
    form.locator.side_effect = lambda selector: group(buttons if selector.startswith("button") else inputs)
    inputs[0].locator.return_value = form
    state = Mock(return_value=EMAIL_OTP)
    monkeypatch.setattr(cloak, "_page_state", state)
    return SimpleNamespace(
        driver=driver, page=page, form=form, submit=submit, outside=outside,
        inputs=inputs, choices=choices, buttons=buttons, state=state, node=node,
    )


def test_native_fill_preserves_complete_password(ui):
    assert cloak._type_any(ui.driver, cloak._PASSWORD_SELECTORS, "full-secret", timeout=7)
    ui.inputs[0].fill.assert_called_once_with("full-secret", timeout=7000)
    ui.inputs[0].press_sequentially.assert_not_called()


def test_type_any_polls_until_input_appears(ui, monkeypatch):
    real_visible = cloak._visible
    calls = []

    def delayed(scope, selector):
        calls.append(selector)
        return [] if len(calls) == 1 else real_visible(scope, selector)

    monkeypatch.setattr(cloak, "_visible", delayed)
    assert cloak._type_any(ui.driver, cloak._PASSWORD_SELECTORS, "late-secret", timeout=5)
    assert len(calls) == 2
    ui.inputs[0].fill.assert_called_once_with("late-secret", timeout=5000)


def test_type_any_missing_input_reports_timeout(ui, monkeypatch):
    import itertools

    ticks = itertools.count(0, 100)
    monkeypatch.setattr(cloak, "_visible", lambda scope, selector: [])
    # 只替换本模块的 time 引用，避免影响全局 time.monotonic。
    monkeypatch.setattr(cloak, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    with pytest.raises(RuntimeError, match="timeout") as caught:
        cloak._type_any(ui.driver, cloak._PASSWORD_SELECTORS, "private-secret")
    assert "private-secret" not in str(caught.value)


@pytest.mark.parametrize("message,expected", [
    ("Timeout 30000ms: locator.fill('private-secret')", "timeout"),
    ("Target page closed while fill('private-secret')", "closed"),
    ("Invalid value private-secret", "交互失败"),
])
def test_input_failures_never_expose_credentials(ui, message, expected):
    ui.inputs[0].fill.side_effect = RuntimeError(message)
    with pytest.raises(RuntimeError) as caught:
        cloak._type_any(ui.driver, cloak._PASSWORD_SELECTORS, "private-secret")
    assert expected in str(caught.value)
    assert "private-secret" not in str(caught.value)
    assert caught.value.__suppress_context__


def test_input_navigation_returns_control_to_state_machine(ui):
    ui.inputs[0].fill.side_effect = RuntimeError("Execution context was destroyed: fill('private-secret')")
    assert cloak._type_any(ui.driver, cloak._PASSWORD_SELECTORS, "private-secret") is False


def test_submit_stays_inside_input_form_and_skips_resend(ui):
    resend = ui.node("Resend", type="submit", name="intent", value="resend")
    ui.buttons.insert(0, resend)
    assert cloak._submit_auth_form(ui.driver, cloak._CODE_SELECTORS)
    ui.submit.click.assert_called_once()
    resend.click.assert_not_called()
    ui.outside.click.assert_not_called()
    ui.inputs[0].locator.assert_called_once_with("xpath=ancestor::form[1]")


@pytest.mark.parametrize("boxes", [1, 6])
def test_code_fills_whole_value_or_clears_then_fills_each_box(ui, boxes):
    if boxes == 6:
        ui.inputs.extend(ui.node() for _ in range(5))
    calls = []
    for index, el in enumerate(ui.inputs):
        el.fill.side_effect = lambda value, *, timeout, i=index: calls.append((i, value))
    cloak._submit_code(ui.driver, "123456")
    expected = [(0, "123456")] if boxes == 1 else (
        [(i, "") for i in range(6)] + list(enumerate("123456"))
    )
    assert calls == expected
    ui.submit.click.assert_called_once()


@pytest.mark.parametrize("next_state", [
    {"url": "https://auth.openai.com/mfa-challenge/totp", "code": True},
    {"url": "https://auth.openai.com/create-account/password", "password": True},
    {"url": "https://chatgpt.com/"},
])
def test_code_auto_navigation_does_not_submit_next_form(ui, next_state):
    ui.state.side_effect = [EMAIL_OTP, EMAIL_OTP, next_state]
    cloak._submit_code(ui.driver, "123456")
    ui.inputs[0].fill.assert_called_once_with("123456", timeout=10000)
    ui.submit.click.assert_not_called()


def test_code_does_not_retarget_replaced_form_at_same_url(ui):
    ui.inputs[0].fill.side_effect = lambda *a, **k: setattr(ui.form.count, "return_value", 0)
    cloak._submit_code(ui.driver, "123456")
    ui.submit.click.assert_not_called()


def test_code_fill_error_is_redacted(ui):
    ui.inputs[0].fill.side_effect = RuntimeError("Timeout while fill('123456')")
    with pytest.raises(RuntimeError, match="timeout") as caught:
        cloak._submit_code(ui.driver, "123456")
    assert "123456" not in str(caught.value)


def test_passwordless_uses_login_attribute_and_refuses_signup(ui):
    ui.state.return_value = PASSWORD
    signup = ui.node("Use a one-time code", name="intent", value="passwordless_signup_send_otp")
    fallback = ui.node("Continue with a one-time code")
    login = ui.node("Email code", name="intent", value="passwordless_login_send_otp")
    ui.choices.extend([signup, fallback, login])
    assert cloak._click_passwordless_signup_if_present(ui.driver) == {"ok": True}
    login.click.assert_called_once()
    signup.click.assert_not_called()
    fallback.click.assert_not_called()


def test_registration_only_passwordless_button_is_not_clicked(ui):
    ui.state.return_value = PASSWORD
    signup = ui.node("Use a one-time code to sign up", value="passwordless_signup_send_otp")
    ui.choices.append(signup)
    assert cloak._click_passwordless_signup_if_present(ui.driver) == {"ok": False}
    signup.click.assert_not_called()


def test_resend_clicks_native_locator_without_selenium_text(ui):
    button = ui.node("Resend code", name="intent", value="resend")
    ui.choices.append(button)
    assert cloak._click_resend_email_otp(ui.driver) == {"ok": True}
    button.click.assert_called_once()


@pytest.mark.parametrize("state", [
    {"url": "https://chatgpt.com/auth/login?email=account_deleted%40example.com", "text": "account_deleted@example.com"},
    {"url": "https://chatgpt.com/", "text": "Recent chats: account deleted", "errors": ["account_banned"]},
    {"url": "https://auth.openai.com/log-in", "errors": ["Invalid email account_deleted@example.com"]},
    {"url": "https://other.example/?error=account_deleted", "errors": ["account_deleted"]},
])
def test_email_and_chat_content_do_not_mark_account_dead(state):
    cloak._check_error(state, cloak._AuthResponses())


@pytest.mark.parametrize("state", [
    {"url": "https://auth.openai.com/error?error=account_deleted"},
    {"url": "https://auth.openai.com/log-in/password", "errors": ["Your account has been deactivated"]},
])
def test_explicit_auth_errors_still_mark_account_dead(state):
    with pytest.raises(cloak.AccountUnusableError):
        cloak._check_error(state, cloak._AuthResponses())


@pytest.mark.parametrize("path", ["/phone-otp", "/verify-phone"])
def test_other_phone_routes_are_not_treated_as_email_otp(path):
    assert cloak._step({"url": "https://auth.openai.com" + path, "code": True}) == "phone"


def test_session_navigation_is_retried(ui):
    ui.page.evaluate.side_effect = RuntimeError("Execution context was destroyed")
    assert cloak._read_session(ui.driver) is None


@pytest.mark.parametrize("login_error", [None, RuntimeError("original login failure")])
def test_quit_error_preserves_result_or_original_failure(monkeypatch, login_error):
    driver = Mock()
    driver.quit.side_effect = RuntimeError("close failed")
    session = {"accessToken": "token", "user": {"email": "user@example.com"}}
    monkeypatch.setattr(cloak, "build_cloak_driver", Mock(return_value=(driver, SimpleNamespace(raw={}))))
    monkeypatch.setattr(cloak, "_login", Mock(return_value=session, side_effect=login_error))
    if login_error:
        with pytest.raises(RuntimeError) as caught:
            cloak.login_with_cloak("user@example.com")
        assert caught.value is login_error
    else:
        assert cloak.login_with_cloak("user@example.com")[0] is session
    driver.quit.assert_called_once()
