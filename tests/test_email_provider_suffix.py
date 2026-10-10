"""Per-request mailbox suffix selection, allocation and compatibility contracts."""
from concurrent.futures import ThreadPoolExecutor
import importlib
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from config import email as email_config
from core import db, email_provider

_CLIENTS = {
    "outlook": "outlook_client", "generic_api": "generic_api_mail_client",
    "imap": "imap_mail_client", "cloudflare_domain": "qqmail_client",
    "cloudflare": "cf_temp_mail_client", "gptmail": "gptmail_client",
    "mailnest": "mailnest_client", "cloudmail": "cloudmail_client",
    "remail": "remail_client",
}


@pytest.mark.parametrize("value, expected", [
    (None, ""), ("", ""), ("   ", ""), ("Example.COM", "example.com"),
    (" @Sub.Example.COM ", "sub.example.com"), ("xn--bcher-kva.example", "xn--bcher-kva.example"),
])
def test_normalize_suffix(value, expected):
    assert email_provider.normalize_email_suffix(value) == expected


@pytest.mark.parametrize("value", [
    "user@example.com", "https://example.com", "example.com/path", "@@example.com", "@",
    "example", ".example.com", "example..com", "example.com.", "-bad.example", "bad-.example",
    "bad_domain.example", "*.example.com", "example.com:443", "exa mple.com", "127.0.0.1",
    "a" * 64 + ".com", "a." * 126 + "com", 1, [],
])
def test_normalize_rejects_invalid_suffix(value):
    with pytest.raises(ValueError, match="email_suffix"):
        email_provider.normalize_email_suffix(value)


@pytest.mark.parametrize("source", email_provider._VALID_SOURCES)
@pytest.mark.parametrize("suffix", [None, "", "   "])
def test_no_suffix_keeps_zero_argument_client_call(monkeypatch, source, suffix):
    client = importlib.import_module("core." + _CLIENTS[source])
    pick_name = "pick_domain_email" if source == "cloudflare_domain" else "pick_account"
    result = "fresh@example.com" if source == "cloudflare_domain" else SimpleNamespace(email="fresh@example.com")
    pick = Mock(return_value=result)
    monkeypatch.setattr(client, pick_name, pick)
    assert email_provider.acquire_email_from_source(source, email_suffix=suffix) == "fresh@example.com"
    pick.assert_called_once_with()


@pytest.mark.parametrize("source", email_provider._VALID_SOURCES)
def test_invalid_suffix_rejected_before_allocation(monkeypatch, source):
    client = importlib.import_module("core." + _CLIENTS[source])
    pick_name = "pick_domain_email" if source == "cloudflare_domain" else "pick_account"
    pick = Mock()
    monkeypatch.setattr(client, pick_name, pick)
    with pytest.raises(ValueError):
        email_provider.acquire_email_from_source(source, "user@example.com")
    pick.assert_not_called()


@pytest.mark.parametrize("source", ["gptmail", "mailnest"])
def test_unsupported_suffix_rejected_without_api_call(monkeypatch, source):
    client = importlib.import_module("core." + _CLIENTS[source])
    request = Mock()
    monkeypatch.setattr(client, "_get" if source == "gptmail" else "_request", request)
    with pytest.raises(RuntimeError, match="不支持显式 email_suffix"):
        email_provider.acquire_email_from_source(source, "example.com")
    request.assert_not_called()


def _import_pool(source, emails):
    if source == "outlook":
        return db.import_outlook_accounts([
            {"email": address, "password": "pw", "client_id": "client", "refresh_token": "token"}
            for address in emails
        ])
    if source == "generic_api":
        return db.import_generic_api_emails([
            {"email": address, "code_url": "https://mail.example/code"} for address in emails
        ])
    return db.import_imap_emails([
        {"email": address, "imap_password": "pw", "imap_server": "imap.example"} for address in emails
    ])


@pytest.mark.parametrize("source", ["outlook", "generic_api", "imap"])
def test_pool_exact_filter_does_not_consume_nonmatching_addresses(source):
    emails = ["first@other.example", "sub@sub.example.com", "last@EXAMPLE.COM"]
    _import_pool(source, emails)
    assert email_provider.acquire_email_from_source(source, " @Example.COM ") == emails[-1]
    rows = {row["email"]: row for row in db.list_email_pool_page(source=source)["items"]}
    assert rows[emails[0]]["status"] == rows[emails[1]]["status"] == "available"
    assert rows[emails[2]]["status"] == "used"
    with pytest.raises(RuntimeError, match="@missing\\.example"):
        email_provider.acquire_email_from_source(source, "missing.example")
    rows = db.list_email_pool_page(source=source)["items"]
    assert sum(row["status"] == "available" for row in rows) == 2


@pytest.mark.parametrize("source", ["outlook", "generic_api", "imap"])
def test_concurrent_suffix_claims_are_unique_and_leave_other_domain_untouched(source):
    wanted = [f"user{i}@example.com" for i in range(6)]
    _import_pool(source, ["untouched@other.example", *wanted])

    def acquire(_):
        try:
            return email_provider.acquire_email_from_source(source, "example.com")
        except RuntimeError:
            return None

    with ThreadPoolExecutor(max_workers=8) as workers:
        claimed = list(workers.map(acquire, range(10)))
    assert sorted(address for address in claimed if address) == sorted(wanted)
    assert claimed.count(None) == 4
    rows = db.list_email_pool_page(source=source)["items"]
    assert next(row for row in rows if row["email"] == "untouched@other.example")["status"] == "available"


def test_configured_catchall_domain_only_and_no_global_mutation(monkeypatch):
    from core import qqmail_client
    monkeypatch.setattr(email_config, "EMAIL_DOMAIN", "Example.COM")
    claim = Mock()
    monkeypatch.setattr(db, "claim_next_domain_email", claim)
    address = email_provider.acquire_email_from_source("cloudflare_domain", "@example.com")
    assert address.endswith("@example.com")
    claim.assert_called_once_with(address)
    assert email_config.EMAIL_DOMAIN == "Example.COM"
    claim.reset_mock()
    with pytest.raises(qqmail_client.QQMailClientError, match="必须与配置的 EMAIL_DOMAIN 一致"):
        email_provider.acquire_email_from_source("cloudflare_domain", "other.example")
    claim.assert_not_called()


def test_cloudflare_passes_domain_to_creation_payload(monkeypatch):
    from core import cf_temp_mail_client as client
    monkeypatch.setattr(client, "_auth_mode", lambda: "none")
    monkeypatch.setattr(client, "_api_key", lambda: "")
    monkeypatch.setattr(client, "_cfg_str", lambda _key, default="": default)
    request = Mock(return_value={"address": "new@EXAMPLE.COM", "jwt": "token"})
    monkeypatch.setattr(client, "_request", request)
    assert email_provider.acquire_email_from_source("cloudflare", "@example.com") == "new@EXAMPLE.COM"
    assert request.call_args.kwargs["json_body"] == {"domain": "example.com"}
    client.release_account("new@EXAMPLE.COM")


@pytest.mark.parametrize("source", ["cloudflare", "remail"])
def test_remote_mismatch_rejected_once_and_context_recycled(monkeypatch, source):
    client = importlib.import_module("core." + _CLIENTS[source])
    if source == "cloudflare":
        request = Mock(return_value=client.CFTempMailAccount(email="wrong@sub.example.com", jwt="token"))
        monkeypatch.setattr(client, "create_address", request)
    else:
        request = Mock(return_value={"deliveryEmail": "wrong@sub.example.com", "serviceToken": "token", "orderNo": "R1"})
        monkeypatch.setattr(client, "_request", request)
        monkeypatch.setattr(client, "_project_id", lambda: 1)
    with pytest.raises(RuntimeError, match="email_suffix 后缀不匹配"):
        email_provider.acquire_email_from_source(source, "example.com")
    request.assert_called_once()
    assert client.get_account_context("wrong@sub.example.com") is None


def test_remail_order_suffix_overrides_config_without_mutation(monkeypatch):
    from core import remail_client as client
    monkeypatch.setattr(email_config, "REMAIL_EMAIL_SUFFIX", "configured.example")
    monkeypatch.setattr(client, "_project_id", lambda: 1)
    request = Mock(return_value={"deliveryEmail": "fresh@example.com", "serviceToken": "token", "orderNo": "R1"})
    monkeypatch.setattr(client, "_request", request)
    assert email_provider.acquire_email_from_source("remail", "@EXAMPLE.COM") == "fresh@example.com"
    assert request.call_args.kwargs["json_body"] == {"projectId": 1, "emailSuffix": "example.com"}
    assert email_config.REMAIL_EMAIL_SUFFIX == "configured.example"
    client.release_account("fresh@example.com")


def test_cloudmail_explicit_domain_used_in_add_user_without_config_mutation(monkeypatch):
    from core import cloudmail_client as client
    monkeypatch.setattr(email_config, "CLOUDMAIL_DOMAINS", ["configured.example"])
    monkeypatch.setattr(email_config, "CLOUDMAIL_AUTO_ADD_USER", True)
    domains = Mock(side_effect=AssertionError("explicit suffix must not select default domain"))
    monkeypatch.setattr(client, "_domains", domains)
    request = Mock()
    monkeypatch.setattr(client, "_request", request)
    address = email_provider.acquire_email_from_source("cloudmail", "@EXAMPLE.COM")
    assert address.endswith("@example.com")
    assert request.call_args.args[0] == "/api/public/addUser"
    assert request.call_args.args[1]["list"][0]["email"] == address
    assert email_config.CLOUDMAIL_DOMAINS == ["configured.example"]
    domains.assert_not_called()
    client.release_account(address)


def test_provider_recycles_mismatch_using_requested_source_not_source_guess(monkeypatch):
    from core import outlook_client
    pick = Mock(return_value=SimpleNamespace(email="wrong@sub.example.com"))
    recycle = Mock()
    monkeypatch.setattr(outlook_client, "pick_account", pick)
    monkeypatch.setattr(outlook_client, "release_account", recycle)
    monkeypatch.setattr(email_provider, "resolve_email_source", Mock(side_effect=AssertionError("must use explicit source")))
    with pytest.raises(RuntimeError, match="email_suffix 后缀不匹配"):
        email_provider.acquire_email_from_source("outlook", "example.com")
    pick.assert_called_once_with(email_suffix="example.com")
    recycle.assert_called_once()
    assert recycle.call_args.args == ("wrong@sub.example.com",)
    assert recycle.call_args.kwargs["status"] == "available"
