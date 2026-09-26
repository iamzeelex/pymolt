"""
tests/test_config_and_cache.py

Unit and integration tests for PyMolt configuration, endpoint resolution,
local offline delta cache, and CLI config/auth commands.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from pymolt.codemods.client import AxiomGraphClient, DependencyMigration
from pymolt.codemods.models import CodemodBundle, CodemodPattern
from pymolt.config import (
    DEFAULT_ENDPOINT,
    ENV_ENDPOINT,
    ENV_TOKEN,
    cache_stats,
    clear_delta_cache,
    clear_endpoint,
    clear_token,
    config_show,
    get_cached_delta,
    load_endpoint,
    load_token,
    save_cached_delta,
    save_endpoint,
    save_token,
)
from pymolt.interfaces.cli.commands import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_config_and_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate config and cache dirs per test."""
    cfg_dir = tmp_path / "config"
    cache_dir = tmp_path / "cache"
    cfg_dir.mkdir()
    cache_dir.mkdir()

    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_dir))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_dir))
    monkeypatch.delenv(ENV_TOKEN, raising=False)
    monkeypatch.delenv(ENV_ENDPOINT, raising=False)


def test_endpoint_resolution_hierarchy(monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. Default when nothing is set
    assert load_endpoint() == DEFAULT_ENDPOINT

    # 2. Config file
    save_endpoint("https://custom.axiom.corp")
    assert load_endpoint() == "https://custom.axiom.corp"

    # 3. Environment variable overrides config file
    monkeypatch.setenv(ENV_ENDPOINT, "http://localhost:9000")
    assert load_endpoint() == "http://localhost:9000"

    # 4. Explicit override argument wins over env var
    assert load_endpoint("http://override:5000") == "http://override:5000"

    # 5. Clear endpoint resets back to env / default
    monkeypatch.delenv(ENV_ENDPOINT, raising=False)
    clear_endpoint()
    assert load_endpoint() == DEFAULT_ENDPOINT


def test_token_save_and_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    assert load_token() is None

    save_token("pmk_secret_test_token_12345678")
    assert load_token() == "pmk_secret_test_token_12345678"

    # Env var overrides stored token
    monkeypatch.setenv(ENV_TOKEN, "pmk_env_override")
    assert load_token() == "pmk_env_override"

    monkeypatch.delenv(ENV_TOKEN, raising=False)
    assert clear_token() is True
    assert load_token() is None


def test_local_delta_cache_lifecycle() -> None:
    assert get_cached_delta("pydantic", "1.10.0", "2.0.0") is None

    bundle = {
        "patterns": [
            {
                "old_qualname": "pydantic.BaseModel.dict",
                "new_qualname": "model_dump",
                "kind": "rename-call",
                "confidence": "high",
                "evidence": ["pydantic_v2_migration"],
            }
        ],
        "rules": [],
        "downgraded": [],
    }

    path = save_cached_delta("pydantic", "1.10.0", "2.0.0", bundle)
    assert path.exists()

    cached = get_cached_delta("pydantic", "1.10.0", "2.0.0")
    assert cached is not None
    assert cached["patterns"][0]["new_qualname"] == "model_dump"

    stats = cache_stats()
    assert stats["packages"] == 1
    assert stats["total_files"] == 1
    assert stats["total_size_bytes"] > 0

    count = clear_delta_cache()
    assert count == 1
    assert get_cached_delta("pydantic", "1.10.0", "2.0.0") is None


def test_client_local_cache_hit_bypasses_http() -> None:
    bundle = {
        "patterns": [
            {
                "old_qualname": "pandas.DataFrame.append",
                "new_qualname": "pandas.concat",
                "kind": "rename-call",
                "confidence": "verified",
                "evidence": ["pandas_2_migration"],
            }
        ],
        "rules": [],
        "downgraded": [],
    }
    save_cached_delta("pandas", "1.5.0", "2.0.0", bundle)

    # Instantiate client with unreachable mock URL
    client = AxiomGraphClient(base_url="http://invalid-unreachable-host:9999")

    # Fetch bundle should hit local cache without raising network error
    mig = DependencyMigration("pandas", "1.5.0", "2.0.0")
    res = client.fetch_bundle([mig])

    assert "pandas" in res
    assert len(res["pandas"].patterns) == 1
    assert res["pandas"].patterns[0].new_qualname == "pandas.concat"


def test_cli_config_commands() -> None:
    # 1. Config show
    result = runner.invoke(app, ["config", "show", "--json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["endpoint"] == DEFAULT_ENDPOINT
    assert data["token_configured"] is False

    # 2. Config set-endpoint
    result = runner.invoke(app, ["config", "set-endpoint", "http://localhost:8000"])
    assert result.exit_code == 0
    assert "Set Axiom endpoint to http://localhost:8000" in result.output
    assert load_endpoint() == "http://localhost:8000"

    # 3. Config reset-endpoint
    result = runner.invoke(app, ["config", "reset-endpoint"])
    assert result.exit_code == 0
    assert DEFAULT_ENDPOINT in result.output
    assert load_endpoint() == DEFAULT_ENDPOINT

    # 4. Config clear-cache
    result = runner.invoke(app, ["config", "clear-cache"])
    assert result.exit_code == 0
    assert "Cleared local delta cache" in result.output


def test_cli_auth_commands() -> None:
    # 1. Status when logged out
    result = runner.invoke(app, ["auth", "status"])
    assert result.exit_code == 0
    assert "Anonymous / Community" in result.output

    # 2. Login with token
    result = runner.invoke(app, ["auth", "login", "--token", "pmk_test_auth_123456789"])
    assert result.exit_code == 0
    assert "Successfully authenticated" in result.output
    assert load_token() == "pmk_test_auth_123456789"

    # 3. Status when logged in
    result = runner.invoke(app, ["auth", "status"])
    assert result.exit_code == 0
    assert "Logged in" in result.output

    # 4. Logout
    result = runner.invoke(app, ["auth", "logout"])
    assert result.exit_code == 0
    assert "Logged out" in result.output
    assert load_token() is None
