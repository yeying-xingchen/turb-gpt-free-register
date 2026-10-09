"""Regression tests for the Cloak Codex OAuth state transitions."""
import pytest

from core import roxy_codex_oauth as oauth


class _Driver:
    def __init__(self, url):
        self.current_url = url

    def get(self, url):
        self.current_url = url


def test_direct_login_password_page_completes_before_callback_wait(monkeypatch):
    driver = _Driver("https://auth.openai.com/log-in/password")
    monkeypatch.setattr(driver, "get", lambda url: None)
    monkeypatch.setattr(oauth, "human_delay", lambda *args, **kwargs: None)
    monkeypatch.setattr(oauth, "_maybe_accept", lambda driver: None)
    monkeypatch.setattr(oauth, "_handle_login_password_route", lambda *args, **kwargs: "next_step")
    monkeypatch.setattr(
        oauth,
        "_type_email_address",
        lambda *args, **kwargs: pytest.fail("password page must not search for an email input"),
    )

    oauth._fill_email_and_otp(driver, "user@example.test", lambda *args, **kwargs: "123456", "https://auth.test/authorize")


def test_unrelated_email_interaction_error_is_not_disguised_as_advanced_state(monkeypatch):
    driver = _Driver("https://auth.openai.com/log-in")
    monkeypatch.setattr(oauth, "human_delay", lambda *args, **kwargs: None)
    monkeypatch.setattr(oauth, "_maybe_accept", lambda driver: None)
    monkeypatch.setattr(oauth, "_type_email_address", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("scroll failure")))

    with pytest.raises(RuntimeError, match="scroll failure"):
        oauth._fill_email_and_otp(driver, "user@example.test", lambda *args, **kwargs: "123456", "https://auth.test/authorize")
