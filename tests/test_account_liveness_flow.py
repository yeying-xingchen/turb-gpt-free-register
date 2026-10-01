"""Offline regressions for login routing and authentication failure boundaries."""
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock, call
from urllib.parse import urlparse

import pyotp
import pytest

from core import account_liveness as liveness
from core import chatgpt_bootstrap, openai_auth


EMAIL = "flow-test@example.invalid"
# Public fixture material; never read an account or secret from disk.
SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
CALLBACK_URL = "https://chatgpt.com/api/auth/callback/openai?code=fixture"
CALLBACK = {"continue_url": CALLBACK_URL}
PASSWORD = {"page": {"type": "login_password"}}
EMAIL_OTP = {"page": {"type": "email_otp_verification"}}
MFA = {"page": {"type": "mfa_challenge", "payload": {
    "factors": [{"id": "totp-fixture", "factor_type": "totp"}],
}}}
EMPTY = object()


class HttpFailure(RuntimeError):
    def __init__(self, response):
        super().__init__(f"HTTP {response.status_code}")
        self.response = response


class Response:
    def __init__(self, payload=EMPTY, status=200, text=None):
        self.payload = payload
        self.status_code = status
        self.text = text if text is not None else ("" if payload is EMPTY else json.dumps(payload))

    def json(self):
        if self.payload is EMPTY:
            raise ValueError("empty or non-JSON response")
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise HttpFailure(self)


class Clock:
    def __init__(self, now=60.0):
        self.now = now
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        assert seconds >= 0
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    account = {
        "email": EMAIL,
        "registration_password": "fixture-password",
        "totp_secret": SECRET,
        "access_token": "fixture-old-token",
    }
    monkeypatch.setattr(liveness.db, "get_account_by_email", Mock(return_value=account))
    monkeypatch.setattr(liveness.db, "_sqlite_conn", Mock(side_effect=AssertionError("real DB forbidden")))
    monkeypatch.setattr(liveness.BrowserSession, "__init__", Mock(side_effect=AssertionError("real network session forbidden")))
    monkeypatch.setattr(liveness, "_LOG_DIR", tmp_path)
    monkeypatch.setattr(liveness, "human_delay", Mock())
    monkeypatch.setattr(openai_auth, "request_sentinel_token", Mock(return_value={}))
    monkeypatch.setattr(openai_auth, "build_sentinel_header", Mock(return_value=("fixture-sentinel", "")))
    monkeypatch.setattr(chatgpt_bootstrap, "anonymous_bootstrap", Mock())
    boundaries = {
        "wait_for_otp": "654321",
        "validate_email_otp": CALLBACK,
        "send_email_otp": None,
        "follow_oauth_callback": "https://chatgpt.com/",
        "fetch_session": {"accessToken": "fixture-new-token"},
        "get_providers": {},
        "probe_auth_session": {},
        "get_csrf_token": "fixture-csrf",
        "signin_openai": "https://auth.openai.com/authorize?state=fixture",
    }
    mocks = {}
    for name, result in boundaries.items():
        mocks[name] = Mock(return_value=result)
        monkeypatch.setattr(liveness, name, mocks[name])
    return SimpleNamespace(account=account, **mocks)


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    # Replace this module's clock, so pytest/logging retain a real clock.
    monkeypatch.setattr(liveness, "time", clock)
    return clock


@pytest.fixture
def session():
    return SimpleNamespace(
        proxy="",
        device_id="fixture-device",
        oai_session_id="fixture-session",
        session=SimpleNamespace(close=Mock()),
        post=Mock(side_effect=AssertionError("unexpected auth request")),
        get=Mock(return_value=Response({})),
        get_auth_headers=Mock(return_value={}),
        get_auth_navigate_headers=Mock(return_value={}),
        get_chatgpt_navigate_headers=Mock(return_value={}),
        observe_chatgpt_document=Mock(),
        reset_circuit_breaker=Mock(),
        fingerprint_summary=Mock(return_value={"user_agent": "fixture"}),
        fingerprint_summary_text=Mock(return_value="fixture"),
    )


def post_paths(session):
    return [urlparse(entry.args[0]).path for entry in session.post.call_args_list]


def test_existing_at_and_password_use_full_login_without_reauth(monkeypatch, offline, session):
    offline.account["totp_secret"] = ""
    full = Mock(return_value=(session, {"accessToken": "fixture-new-token"}))
    reauth = Mock(side_effect=AssertionError("password account must not start email reauth"))
    monkeypatch.setattr(liveness, "_login_via_full_web_flow", full)
    monkeypatch.setattr(liveness, "_login_via_reauth", reauth)

    result = liveness.check_account_liveness(EMAIL, proxy="")

    assert result["status"] == "live"
    assert result["access_token"] == "fixture-new-token"
    full.assert_called_once()
    reauth.assert_not_called()
    offline.wait_for_otp.assert_not_called()
    session.session.close.assert_called_once()


def test_session_for_another_email_is_rejected_without_exposing_token(monkeypatch, session):
    wrong_session = {
        "accessToken": "wrong-account-token",
        "user": {"email": "another-user@example.invalid"},
    }
    monkeypatch.setattr(liveness, "_login_via_full_web_flow", Mock(return_value=(session, wrong_session)))

    result = liveness.check_account_liveness(EMAIL, proxy="")

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert "access_token" not in result
    assert "session" not in result
    assert "wrong-account-token" not in json.dumps(result)
    session.session.close.assert_called_once()


@pytest.mark.parametrize("page_type", ["email_verification", "email_otp_verification", "contact_verification"])
def test_server_email_page_does_not_submit_saved_password(page_type, offline, session, clock):
    result = liveness._complete_login_steps(session, EMAIL, {"page": {"type": page_type}}, 1.0)

    assert result["accessToken"] == "fixture-new-token"
    session.post.assert_not_called()
    offline.wait_for_otp.assert_called_once()
    offline.follow_oauth_callback.assert_called_once()


@pytest.mark.parametrize(
    "initial,post_results,email_result,expected_paths,email_count",
    [
        (PASSWORD, [Response(MFA), Response(status=204), Response(CALLBACK)], CALLBACK,
         ["password/verify", "mfa/issue_challenge", "mfa/verify"], 0),
        (EMAIL_OTP, [Response(status=204), Response(CALLBACK)], MFA,
         ["mfa/issue_challenge", "mfa/verify"], 1),
        (MFA, [Response(status=204), Response(EMAIL_OTP)], CALLBACK,
         ["mfa/issue_challenge", "mfa/verify"], 1),
    ],
    ids=["password-mfa-callback", "email-mfa-callback", "mfa-email-callback"],
)
def test_login_challenges_continue_until_web_callback(
    initial, post_results, email_result, expected_paths, email_count, offline, session, clock,
):
    session.post.side_effect = post_results
    offline.validate_email_otp.return_value = email_result

    result = liveness._complete_login_steps(session, EMAIL, initial, 1.0, email_source="fixture-mail")

    assert result["accessToken"] == "fixture-new-token"
    assert post_paths(session) == [f"/api/accounts/{path}" for path in expected_paths]
    assert offline.wait_for_otp.call_count == email_count
    if email_count:
        assert offline.wait_for_otp.call_args.kwargs["email_source"] == "fixture-mail"
    offline.follow_oauth_callback.assert_called_once()
    assert offline.follow_oauth_callback.call_args.args == (session, CALLBACK_URL)
    offline.fetch_session.assert_called_once_with(session)
    verify_body = json.loads(session.post.call_args_list[-1].kwargs["data"])
    assert verify_body["id"] == "totp-fixture"
    assert verify_body["code"] == pyotp.TOTP(SECRET).at(clock.now)


@pytest.mark.parametrize("next_result", [MFA, {}, {"page": {"type": "unknown"}}], ids=["still-mfa", "empty", "no-next-step"])
def test_mfa_without_progress_never_fetches_a_token(next_result, offline, session, clock):
    session.post.side_effect = [Response(status=204), Response(next_result)]

    with pytest.raises(RuntimeError):
        liveness._complete_login_steps(session, EMAIL, MFA, 1.0)

    offline.follow_oauth_callback.assert_not_called()
    offline.fetch_session.assert_not_called()
    assert post_paths(session).count("/api/accounts/mfa/verify") == 1


@pytest.mark.parametrize("collection", ["factors", "mfa_factors"])
@pytest.mark.parametrize("type_key", ["factor_type", "type"])
def test_nested_factors_choose_totp_instead_of_first_sms_factor(collection, type_key):
    payload = {"data": [{"page": {"payload": {collection: [
        {"id": "sms-id", type_key: "sms"},
        {"id": "totp-id", type_key: "totp"},
    ]}}}]}
    assert liveness._extract_factor_id(payload, "/mfa-challenge/unrelated") == "totp-id"


def test_sms_factor_cannot_be_reinterpreted_as_totp_using_url_fallback():
    with pytest.raises(RuntimeError, match="TOTP"):
        liveness._extract_factor_id({"factors": [{"id": "sms-id", "type": "sms"}]}, "/mfa-challenge/sms-id")


def test_factor_url_excludes_query_and_fragment_and_ignores_untyped_id():
    result = {"id": "unrelated-page-id"}
    assert liveness._extract_factor_id(result, "https://auth.openai.com/mfa-challenge/factor-1?next=/wrong#fragment") == "factor-1"
    assert liveness._extract_factor_id(result, "https://auth.openai.com/mfa-challenge") == ""


def test_empty_204_is_allowed_for_challenge_but_not_verification(session):
    session.post.side_effect = [Response(status=204), Response(status=204)]
    assert liveness._mfa_issue_challenge(session, "totp-fixture") == {}
    with pytest.raises(RuntimeError):
        liveness._mfa_verify(session, "totp-fixture", "123456")


def test_totp_uri_preserves_algorithm_digits_and_period(offline):
    offline.account["totp_secret"] = (
        f"otpauth://totp/Fixture:test?secret={SECRET}&algorithm=SHA256&digits=8&period=45"
    )
    totp = liveness._totp_for_account(EMAIL)
    assert totp.interval == 45
    assert totp.digits == 8
    assert totp.at(123456) == pyotp.TOTP(SECRET, digest=hashlib.sha256, digits=8, interval=45).at(123456)


def test_base32_secret_accepts_lowercase_spaces_and_separators(offline):
    offline.account["totp_secret"] = " ".join([SECRET[:16].lower(), SECRET[16:].lower()]) + "\n"
    assert liveness._totp_for_account(EMAIL).at(123456) == pyotp.TOTP(SECRET).at(123456)
    offline.account["totp_secret"] = SECRET[:16] + "-" + SECRET[16:]
    assert liveness._totp_for_account(EMAIL).at(123456) == pyotp.TOTP(SECRET).at(123456)


@pytest.mark.parametrize("secret", [
    "", "not-a-base32-secret!", "otpauth://totp/Fixture?secret=INVALID!",
    f"otpauth://hotp/Fixture?secret={SECRET}&counter=1",
    f"otpauth://totp/Fixture?secret={SECRET}&period=1",
    f"otpauth://totp/Fixture?secret={SECRET}&period=120",
])
def test_invalid_totp_inputs_fail_before_any_auth_request(secret, offline, session):
    offline.account["totp_secret"] = secret
    with pytest.raises(RuntimeError) as error:
        liveness._complete_totp(session, EMAIL, MFA)
    assert not isinstance(error.value, liveness.AccountUnusableError)
    session.post.assert_not_called()


def test_totp_boundary_waits_for_next_window_without_wall_clock_delay(clock):
    clock.now = 58.0
    totp = pyotp.TOTP(SECRET)
    stale = totp.at(clock.now)
    code = liveness._fresh_totp_code(totp, set())
    assert clock.sleeps == pytest.approx([2.1])
    assert code == totp.at(60.1)
    assert code != stale


def test_totp_repeated_code_waits_for_a_new_window(clock):
    totp = pyotp.TOTP(SECRET)
    tried = {totp.at(clock.now)}
    code = liveness._fresh_totp_code(totp, tried)
    assert len(clock.sleeps) == 1
    assert code not in tried
    assert code == totp.at(clock.now)


def test_explicit_invalid_code_retries_only_in_next_totp_window(session, clock):
    submitted = []

    def post(url, **kwargs):
        if url.endswith("/issue_challenge"):
            # The challenge request crosses a window; no code should be generated yet.
            clock.now = 88.0
            return Response(status=204)
        submitted.append((clock.now, json.loads(kwargs["data"])["code"]))
        if len(submitted) == 1:
            return Response({"error": {"code": "invalid_code"}}, status=401)
        return Response(CALLBACK)

    session.post.side_effect = post
    assert liveness._complete_totp(session, EMAIL, MFA) == CALLBACK
    assert len(submitted) == 2
    assert int(submitted[0][0] // 30) < int(submitted[1][0] // 30)
    assert submitted[0][1] != submitted[1][1]
    assert submitted[0][0] >= 90
    assert all(code == pyotp.TOTP(SECRET).at(now) for now, code in submitted)


@pytest.mark.parametrize("status,error_code", [(429, "invalid_code"), (403, "challenge_required"), (401, "invalid_auth_step")])
def test_non_code_errors_and_rate_limit_do_not_retry_totp(status, error_code, session, clock, offline):
    session.post.side_effect = [Response(status=204), Response({"error": {"code": error_code}}, status=status)]
    with pytest.raises(HttpFailure):
        liveness._complete_totp(session, EMAIL, MFA)
    assert post_paths(session).count("/api/accounts/mfa/verify") == 1
    assert not clock.sleeps
    offline.send_email_otp.assert_not_called()


@pytest.mark.parametrize("status,error_code", [(401, "invalid_code"), (429, "rate_limit_exceeded"), (403, "access_denied")])
def test_generic_http_error_preserves_response_without_marking_account_dead(status, error_code):
    response = Response({"error": {"code": error_code}}, status=status)
    with pytest.raises(HttpFailure) as error:
        liveness._auth_response_json(response, "fixture-stage")
    assert error.value.response is response


@pytest.mark.parametrize("code", ["account_deactivated", "account_deleted", "account_banned"])
def test_explicit_account_deactivation_is_not_retried(code):
    with pytest.raises(liveness.AccountUnusableError) as error:
        liveness._auth_response_json(Response({"error": {"code": code}}, status=403), "fixture-stage")
    assert error.value.error_code == code
    assert not liveness._is_retryable_network_error(error.value)


def test_optional_providers_403_does_not_block_csrf_or_replace_session(monkeypatch, offline, session):
    factory = Mock(return_value=session)
    monkeypatch.setattr(liveness, "_new_fingerprint_pinned_session", factory)
    offline.get_providers.side_effect = HttpFailure(Response(status=403, text="Cloudflare challenge"))
    events = Mock()
    events.attach_mock(offline.get_providers, "providers")
    events.attach_mock(offline.get_csrf_token, "csrf")
    events.attach_mock(offline.signin_openai, "signin")

    result_session, url = liveness._network_preflight_with_retry(EMAIL, "", max_attempts=1)

    assert result_session is session
    assert url == offline.signin_openai.return_value
    assert events.mock_calls == [call.providers(session), call.csrf(session), call.signin(session, "fixture-csrf", EMAIL)]
    factory.assert_called_once()
    assert session.reset_circuit_breaker.called
    session.session.close.assert_not_called()


@pytest.mark.parametrize("failure_stage", ["authorize", "credentials"])
def test_full_flow_closes_session_when_it_cannot_return_ownership(failure_stage, monkeypatch, session):
    failure = RuntimeError("fixture login failure")
    monkeypatch.setattr(liveness, "_network_preflight_with_retry", Mock(return_value=(session, "fixture-authorize")))
    authorize = Mock(return_value="https://auth.openai.com/log-in/password")
    login = Mock(return_value={"accessToken": "unused"})
    (authorize if failure_stage == "authorize" else login).side_effect = failure
    monkeypatch.setattr(liveness, "follow_authorize", authorize)
    monkeypatch.setattr(liveness, "_login_via_password_or_otp", login)

    with pytest.raises(RuntimeError) as error:
        liveness._login_via_full_web_flow(EMAIL, "", email_source="fixture-mail", fingerprint_state={})

    assert error.value is failure
    session.session.close.assert_called_once()


@pytest.mark.parametrize("initial", [PASSWORD, MFA], ids=["password", "mfa"])
def test_email_send_navigation_precedes_waiting_for_code(initial, offline, session, clock):
    send_url = "https://auth.openai.com/api/accounts/email-otp/send?state=fixture"
    send_state = {"page": {"type": "email_otp_send"}, "continue_url": send_url, "method": "GET"}
    session.post.side_effect = (
        [Response(send_state)] if initial == PASSWORD else [Response(status=204), Response(send_state)]
    )
    sent_at = clock.now

    def send(url, **kwargs):
        clock.now += 10
        response = Response({})
        response.url = "https://auth.openai.com/email-verification"
        return response

    session.get.side_effect = send
    events = Mock()
    events.attach_mock(session.get, "send")
    events.attach_mock(offline.wait_for_otp, "wait")

    result = liveness._complete_login_steps(session, EMAIL, initial, 1.0)

    assert result["accessToken"] == "fixture-new-token"
    assert [event[0] for event in events.mock_calls] == ["send", "wait"]
    assert session.get.call_args.args[0] == send_url
    assert offline.wait_for_otp.call_args.kwargs["after_ts"] == sent_at


def test_failed_email_send_does_not_wait_for_missing_mail(offline, session, clock):
    session.get.return_value = Response(status=429)
    with pytest.raises(HttpFailure):
        liveness._complete_login_steps(
            session, EMAIL, {"continue_url": "/api/accounts/email-otp/send"}, 1.0,
        )
    offline.wait_for_otp.assert_not_called()
    offline.fetch_session.assert_not_called()


def test_missing_password_explicitly_sends_otp_before_waiting(offline, session, clock):
    offline.account["registration_password"] = ""
    session.post.side_effect = [Response(status=204)]
    events = Mock()
    events.attach_mock(session.post, "send")
    events.attach_mock(offline.wait_for_otp, "wait")

    result = liveness._complete_login_steps(session, EMAIL, PASSWORD, 1.0)

    assert result["accessToken"] == "fixture-new-token"
    assert post_paths(session) == ["/api/accounts/passwordless/send-otp"]
    assert [event[0] for event in events.mock_calls] == ["send", "wait"]
