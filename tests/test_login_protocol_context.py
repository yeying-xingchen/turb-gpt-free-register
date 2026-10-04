"""Offline checks for login-specific NextAuth and Cookie initialization."""
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

import pytest

from core.chatgpt_auth import signin_openai
from core.session import BrowserSession


AUTHORIZE = "https://auth.openai.com/api/accounts/authorize?state=a%2Bb&screen_hint=login&ccaps=server_value"


def make_session(payload=None, *, status=200, location=""):
    response = SimpleNamespace(
        status_code=status,
        headers={"location": location},
        json=Mock(return_value=payload),
        raise_for_status=Mock(),
    )
    return SimpleNamespace(
        device_id="fixture-device",
        auth_session_logging_id="fixture-logging",
        get_nextauth_headers=Mock(return_value={}),
        post=Mock(return_value=response),
    )


def test_login_preserves_server_authorize_parameters():
    session = make_session({"url": AUTHORIZE})
    assert signin_openai(session, "csrf", "fixture@example.invalid", login_only=True) == AUTHORIZE
    request = session.post.call_args
    query = parse_qs(urlparse(request.args[0]).query)
    assert query["screen_hint"] == ["login"]
    assert query["login_hint"] == ["fixture@example.invalid"]
    assert request.kwargs["allow_redirects"] is False
    assert parse_qs(request.kwargs["data"])["csrfToken"] == ["csrf"]


@pytest.mark.parametrize("status", [302, 303])
def test_login_accepts_location_without_json(status):
    session = make_session(status=status, location=AUTHORIZE)
    session.post.return_value.json.side_effect = ValueError("not JSON")
    assert signin_openai(session, "csrf", "fixture@example.invalid", login_only=True) == AUTHORIZE


def test_registration_retains_existing_signin_context():
    session = make_session({"url": "https://auth.openai.com/api/accounts/authorize?state=fixture"})
    result = signin_openai(session, "csrf", "fixture@example.invalid")
    request_query = parse_qs(urlparse(session.post.call_args.args[0]).query)
    authorize_query = parse_qs(urlparse(result).query)
    assert request_query["screen_hint"] == ["login_or_signup"]
    assert authorize_query["screen_hint"] == ["login_or_signup"]
    assert authorize_query["ccaps"] == ["login_methods chatgpt_login_finalizer_v1"]
    assert authorize_query["ext-oai-did"] == [session.device_id]
    assert "allow_redirects" not in session.post.call_args.kwargs


@pytest.mark.parametrize("payload", [{}, {"url": ""}, {"url": 123}, []])
def test_login_rejects_missing_or_invalid_authorize_url(payload):
    session = make_session(payload)
    with pytest.raises(ValueError, match="authorize URL"):
        signin_openai(session, "csrf", "fixture@example.invalid", login_only=True)


def cookie_values(session):
    return {(cookie.domain, cookie.name): cookie.value for cookie in session.session.cookies.jar}


def test_deferred_identity_cookies_preserve_server_cookies():
    with BrowserSession(proxy="", detect_exit_geo=False, defer_identity_cookies=True) as session:
        assert not cookie_values(session)
        session.session.cookies.set("__cf_bm", "fixture-cf", domain="chatgpt.com", path="/")
        session.session.cookies.set("oauth_session", "fixture-oauth", domain="auth.openai.com", path="/")
        session.prime_identity_cookies()
        cookies = cookie_values(session)
        assert cookies[("chatgpt.com", "__cf_bm")] == "fixture-cf"
        assert cookies[("auth.openai.com", "oauth_session")] == "fixture-oauth"
        for domain in ("chatgpt.com", "auth.openai.com", "sentinel.openai.com"):
            assert cookies[(domain, "oai-did")] == session.device_id
            assert cookies[(domain, "oai-locale")] == session.navigator_language()


def test_default_session_still_primes_identity_immediately():
    with BrowserSession(proxy="", detect_exit_geo=False) as session:
        cookies = cookie_values(session)
        assert cookies[("chatgpt.com", "oai-did")] == session.device_id
        assert cookies[("auth.openai.com", "oai-did")] == session.device_id
        assert cookies[("sentinel.openai.com", "oai-locale")] == session.navigator_language()
