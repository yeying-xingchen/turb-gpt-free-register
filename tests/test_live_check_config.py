"""Offline checks for the independent liveness retry configuration."""
from pathlib import Path
import runpy

import pytest

import config
from config import env_loader, proxy
from webui import config_editor


_RETRY_KEYS = ("LIVE_CHECK_MAX_ATTEMPTS", "LIVE_CHECK_RETRY_DELAY")


@pytest.fixture(autouse=True)
def clean_live_check_env(monkeypatch, isolated_runtime):
    # conftest.py supplies a temporary .env and blocks outbound HTTP.
    # Track these keys before the editor loads saved values into os.environ.
    for key in _RETRY_KEYS:
        monkeypatch.delenv(key, raising=False)


def _load_proxy_settings():
    # Execute a fresh namespace without changing the shared config.proxy module.
    return runpy.run_path(str(Path(proxy.__file__)))


@pytest.mark.parametrize("raw", [None, "", "invalid"])
def test_live_check_retry_defaults_survive_missing_or_invalid_env(monkeypatch, raw):
    if raw is not None:
        for key in _RETRY_KEYS:
            monkeypatch.setenv(key, raw)

    settings = _load_proxy_settings()

    assert settings["LIVE_CHECK_MAX_ATTEMPTS"] == 3
    assert settings["LIVE_CHECK_RETRY_DELAY"] == 2.0


def test_live_check_retry_env_overrides_are_typed_and_independent(monkeypatch):
    monkeypatch.setenv("LIVE_CHECK_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("LIVE_CHECK_RETRY_DELAY", "0.25")
    monkeypatch.setenv("PLAN_CHECK_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("PLAN_CHECK_RETRY_DELAY", "17.5")

    settings = _load_proxy_settings()

    assert settings["LIVE_CHECK_MAX_ATTEMPTS"] == 5
    assert isinstance(settings["LIVE_CHECK_MAX_ATTEMPTS"], int)
    assert settings["LIVE_CHECK_RETRY_DELAY"] == 0.25
    assert isinstance(settings["LIVE_CHECK_RETRY_DELAY"], float)
    assert settings["PLAN_CHECK_MAX_ATTEMPTS"] == 2
    assert settings["PLAN_CHECK_RETRY_DELAY"] == 17.5


def test_live_check_retry_settings_are_exported_from_config():
    for key in _RETRY_KEYS:
        assert key in config.__all__
        assert getattr(config, key) == getattr(proxy, key)


@pytest.mark.parametrize("attempts, delay", [(1, 0.0), (3, 2.0), (5, 60.0)])
def test_webui_live_check_retry_settings_round_trip(monkeypatch, attempts, delay):
    monkeypatch.setenv("PLAN_CHECK_MAX_ATTEMPTS", "4")
    env_path = env_loader.env_path()
    env_path.write_text('# keep existing settings\nPLAN_CHECK_MAX_ATTEMPTS="4"\n', encoding="utf-8")

    fields = {item["key"]: item for item in config_editor.get_config()}
    for key, value_type, default in (
        ("LIVE_CHECK_MAX_ATTEMPTS", "int", 3),
        ("LIVE_CHECK_RETRY_DELAY", "float", 2.0),
    ):
        assert fields[key]["group"] == "账号查活"
        assert fields[key]["file"] == "proxy.py"
        assert fields[key]["type"] == value_type
        assert fields[key]["storage"] == "env"
        assert fields[key]["value"] == default

    updates = {"LIVE_CHECK_MAX_ATTEMPTS": attempts, "LIVE_CHECK_RETRY_DELAY": delay}
    result = config_editor.update_config(updates)

    assert set(result["updated"]) == set(updates)
    assert set(result["env_updated"]) == set(updates)
    assert result["ignored"] == []
    saved = env_loader.read_env_file()
    assert saved["PLAN_CHECK_MAX_ATTEMPTS"] == "4"
    assert saved["LIVE_CHECK_MAX_ATTEMPTS"] == str(attempts)
    assert saved["LIVE_CHECK_RETRY_DELAY"] == str(delay)
    assert "# keep existing settings" in env_path.read_text(encoding="utf-8")
    fields = {item["key"]: item for item in config_editor.get_config()}
    settings = _load_proxy_settings()
    for key, expected in updates.items():
        assert fields[key]["value"] == expected
        assert settings[key] == expected
