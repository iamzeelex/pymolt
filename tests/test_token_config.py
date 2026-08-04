"""pymolt.config — token storage + resolution, and the login/logout CLI.

All tests isolate the config dir via XDG_CONFIG_HOME=tmp so they never touch a
real ~/.config/pymolt/config.json.
"""

from __future__ import annotations

import stat

import pytest
from typer.testing import CliRunner

from pymolt import config
from pymolt.interfaces.cli.commands import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("PYMOLT_API_TOKEN", raising=False)


def test_save_load_clear_roundtrip():
    assert config.load_token() is None
    path = config.save_token("pmk_abc123")
    assert config.load_token() == "pmk_abc123"
    assert path.exists()
    assert config.clear_token() is True
    assert config.load_token() is None
    assert config.clear_token() is False  # nothing left to clear


def test_saved_file_is_0600():
    path = config.save_token("pmk_secret")
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_env_token_overrides_saved_file(monkeypatch):
    config.save_token("pmk_from_file")
    monkeypatch.setenv("PYMOLT_API_TOKEN", "pmk_from_env")
    assert config.load_token() == "pmk_from_env"


def test_cli_login_saves_token():
    result = runner.invoke(app, ["login", "--token", "pmk_clitoken"])
    assert result.exit_code == 0
    assert config.load_token() == "pmk_clitoken"


def test_cli_logout_removes_token():
    config.save_token("pmk_toremove")
    result = runner.invoke(app, ["logout"])
    assert result.exit_code == 0
    assert config.load_token() is None
