"""Offline SMTP configuration, editor validation and hot-reload coverage."""
from pathlib import Path
import runpy

import pytest

import config
from config import env_loader, notifications
from webui import config_editor

DEFAULTS = {
    "SMTP_ENABLED": False, "SMTP_HOST": "", "SMTP_PORT": 465,
    "SMTP_SECURITY": "ssl", "SMTP_USERNAME": "", "SMTP_PASSWORD": "",
    "SMTP_FROM": "", "SMTP_ADMIN_EMAILS": "", "SMTP_NOTIFY_TASKS": True,
    "SMTP_NOTIFY_UPLOADS": True, "SMTP_TIMEOUT": 15,
}


@pytest.fixture(autouse=True)
def clean_smtp_env(monkeypatch, isolated_runtime):
    for key in DEFAULTS:
        monkeypatch.delenv(key, raising=False)
    yield
    # Restore module defaults without retaining this test's temporary .env values.
    for key, value in DEFAULTS.items():
        setattr(notifications, key, value)
        setattr(config, key, value)


def fresh_settings():
    return runpy.run_path(str(Path(notifications.__file__)))


def test_defaults_and_editor_metadata():
    settings = fresh_settings()
    fields = {item["key"]: item for item in config_editor.get_config()}
    for key, default in DEFAULTS.items():
        assert settings[key] == default
        assert type(settings[key]) is type(default)
        assert key in config.__all__
        assert fields[key]["value"] == default
        assert fields[key]["group"] == "SMTP 通知"
        assert fields[key]["storage"] == "env"
    assert fields["SMTP_PASSWORD"]["secret"] is True
    assert "SMTP_PASSWORD" in env_loader.SECRET_ENV_KEYS
    assert {item["value"] for item in fields["SMTP_SECURITY"]["choices"]} == {"ssl", "starttls", "plain"}


def test_round_trip_and_hot_reload(monkeypatch):
    values = {
        "SMTP_ENABLED": True, "SMTP_HOST": "smtp.example.com", "SMTP_PORT": 587,
        "SMTP_SECURITY": "starttls", "SMTP_USERNAME": "sender@example.com",
        "SMTP_PASSWORD": 'special-"password\\value', "SMTP_FROM": "",
        "SMTP_ADMIN_EMAILS": "one@example.com;two@example.com\nthree@example.com,four@example.com",
        "SMTP_NOTIFY_TASKS": False, "SMTP_NOTIFY_UPLOADS": False, "SMTP_TIMEOUT": 30,
    }
    original_source = Path(notifications.__file__).read_text()
    result = config_editor.update_config(values)
    assert set(result["env_updated"]) == set(values)
    assert Path(notifications.__file__).read_text() == original_source
    assert env_loader.read_env_file()["SMTP_PASSWORD"] == values["SMTP_PASSWORD"]
    # Exercise real reload_all, restricting its scope to avoid unrelated modules' side effects.
    monkeypatch.setattr(config, "_RELOADABLE_SUBMODULES", ("config.notifications",))
    original_module = notifications
    assert config.reload_all() == ["config.notifications"]
    assert config.notifications is original_module
    fields = {item["key"]: item for item in config_editor.get_config()}
    for key, expected in values.items():
        assert getattr(notifications, key) == expected
        assert getattr(config, key) == expected
        assert fields[key]["value"] == expected


def test_stepwise_save_then_enable_and_clear():
    for key, value in {
        "SMTP_HOST": "smtp.example.com", "SMTP_USERNAME": "sender@example.com",
        "SMTP_ADMIN_EMAILS": "admin@example.com",
    }.items():
        config_editor.update_config({key: value})
    config_editor.update_config({"SMTP_ENABLED": True})
    config_editor.update_config({"SMTP_ENABLED": False, "SMTP_USERNAME": "", "SMTP_ADMIN_EMAILS": ""})
    settings = fresh_settings()
    assert settings["SMTP_ENABLED"] is False
    assert settings["SMTP_USERNAME"] == settings["SMTP_ADMIN_EMAILS"] == ""


def test_unauthenticated_relay_with_explicit_sender():
    config_editor.update_config({
        "SMTP_ENABLED": True, "SMTP_HOST": "localhost", "SMTP_SECURITY": "plain",
        "SMTP_PORT": 25, "SMTP_FROM": "sender@internal", "SMTP_ADMIN_EMAILS": "admin@internal",
    })
    assert fresh_settings()["SMTP_ENABLED"] is True


@pytest.mark.parametrize("updates", [
    {"SMTP_PORT": 0}, {"SMTP_PORT": 65536}, {"SMTP_PORT": "not-an-int"},
    {"SMTP_PORT": 3.5}, {"SMTP_PORT": True}, {"SMTP_TIMEOUT": 0},
    {"SMTP_TIMEOUT": -1}, {"SMTP_TIMEOUT": 121}, {"SMTP_TIMEOUT": "invalid"}, {"SMTP_SECURITY": "tls"},
    {"SMTP_FROM": "not-an-email"}, {"SMTP_FROM": "a@b\nBcc:other@example.com"},
    {"SMTP_ADMIN_EMAILS": "good@example.com;bad"}, {"SMTP_ADMIN_EMAILS": ";,"},
    {"SMTP_HOST": "smtp.example.com\nheader"}, {"SMTP_ENABLED": True},
    {"SMTP_ENABLED": True, "SMTP_HOST": "smtp.example.com"},
    {"SMTP_ENABLED": True, "SMTP_HOST": "smtp.example.com", "SMTP_FROM": "sender@example.com"},
])
def test_invalid_values_rejected_before_any_write(updates):
    path = env_loader.env_path()
    path.write_text('# untouched\nSMTP_ENABLED="False"\n')
    before = path.read_text()
    with pytest.raises(ValueError):
        config_editor.update_config({"SMTP_NOTIFY_TASKS": False, **updates})
    assert path.read_text() == before


def test_incomplete_existing_configuration_does_not_block_unrelated_save(monkeypatch):
    monkeypatch.setenv("SMTP_ENABLED", "True")
    result = config_editor.update_config({"SMTP_TIMEOUT": 20})
    assert result["updated"] == ["SMTP_TIMEOUT"]


@pytest.mark.parametrize("port,timeout", [(1, 1), (65535, 120)])
def test_valid_integer_boundaries(port, timeout):
    config_editor.update_config({"SMTP_PORT": port, "SMTP_TIMEOUT": timeout})
    settings = fresh_settings()
    assert settings["SMTP_PORT"] == port
    assert settings["SMTP_TIMEOUT"] == timeout
