"""The liveness browser choice is independent and editable through the WebUI."""
from pathlib import Path
import runpy

import pytest

import config
from config import env_loader, live_check
from webui import config_editor


@pytest.mark.parametrize("value, expected", [(None, "protocol"), ("", "protocol"), ("cloak", "cloak")])
def test_live_driver_defaults_and_env_override(monkeypatch, value, expected):
    monkeypatch.delenv("LIVE_CHECK_DRIVER", raising=False)
    if value is not None:
        monkeypatch.setenv("LIVE_CHECK_DRIVER", value)
    monkeypatch.setenv("REGISTRATION_DRIVER", "skyvern")
    monkeypatch.setenv("CODEX_OAUTH_DRIVER", "roxy")
    settings = runpy.run_path(str(Path(live_check.__file__)))
    assert settings["LIVE_CHECK_DRIVER"] == expected


def test_live_driver_is_exported_and_reloaded():
    assert "LIVE_CHECK_DRIVER" in config.__all__
    assert "config.live_check" in config._RELOADABLE_SUBMODULES
    assert config.LIVE_CHECK_DRIVER == live_check.LIVE_CHECK_DRIVER


def test_live_driver_webui_round_trip(monkeypatch):
    monkeypatch.delenv("LIVE_CHECK_DRIVER", raising=False)
    fields = {item["key"]: item for item in config_editor.get_config()}
    field = fields["LIVE_CHECK_DRIVER"]
    assert field["group"] == "账号查活"
    assert field["storage"] == "env"
    assert {item["value"] for item in field["choices"]} == {"protocol", "cloak"}
    result = config_editor.update_config({"LIVE_CHECK_DRIVER": "cloak"})
    assert "LIVE_CHECK_DRIVER" in result["env_updated"]
    assert env_loader.read_env_file()["LIVE_CHECK_DRIVER"] == "cloak"
    assert runpy.run_path(str(Path(live_check.__file__)))["LIVE_CHECK_DRIVER"] == "cloak"
    with pytest.raises(ValueError):
        config_editor.update_config({"LIVE_CHECK_DRIVER": "same_as_registration"})
