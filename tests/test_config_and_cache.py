"""
tests/test_config_and_cache.py

Unit and integration tests for PyMolt configuration, endpoint resolution (no
default endpoint: a remote Axiom is used only when explicitly configured), the
local cache of remote bundles, and the removed config/auth/axiom commands.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from pymolt.codemods.client import AxiomGraphClient, DependencyMigration
from pymolt.config import (
    ENV_ENDPOINT,
    ENV_TOKEN,
    _write,
    cache_stats,
    clear_delta_cache,
    clear_token,
    config_show,
    get_cached_delta,
    load_endpoint,
    load_token,
    save_cached_delta,
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


def test_endpoint_resolution_has_no_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. No endpoint is the default state: pymolt computes API changes locally.
    assert load_endpoint() is None

    # 2. An endpoint an older release saved is reported, never silently used.
    _write({"endpoint": "https://custom.axiom.corp"})
    assert load_endpoint() is None
    assert config_show()["stored_endpoint_ignored"] == "https://custom.axiom.corp"

    # 3. The environment variable configures one explicitly.
    monkeypatch.setenv(ENV_ENDPOINT, "http://localhost:9000/")
    assert load_endpoint() == "http://localhost:9000"

    # 4. The explicit --endpoint argument wins over the environment.
    assert load_endpoint("http://override:5000") == "http://override:5000"
    assert config_show()["remote"] is True


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


def test_cli_axiom_group_is_a_hidden_stub_naming_the_replacement() -> None:
    for argv, new in (
        (["axiom", "status", "--json"], "pymolt plan"),
        (["axiom", "endpoint", "http://localhost:8000"], "--endpoint URL"),
        (["axiom", "login", "--token", "pmk_test_auth_123456789"], "PYMOLT_API_TOKEN"),
        (["axiom", "logout"], "unset PYMOLT_API_TOKEN"),
        (["axiom", "clear-cache"], ".pymolt/cache/delta"),
        (["axiom"], "pymolt plan"),
    ):
        result = runner.invoke(app, argv)
        assert result.exit_code == 2, argv
        assert "was removed" in result.output and new in result.output, (argv, result.output)
    assert load_token() is None  # the stub never stores anything
    assert "axiom" not in runner.invoke(app, ["--help"]).output


def test_old_config_and_auth_commands_name_their_replacements() -> None:
    for argv, new in (
        (["config", "set-endpoint", "http://x"], "PYMOLT_AXIOM_ENDPOINT"),
        (["config", "show"], "pymolt plan"),
        (["auth", "login", "--token", "pmk_x"], "PYMOLT_API_TOKEN"),
        (["login", "--token", "pmk_x"], "PYMOLT_API_TOKEN"),
        (["logout"], "unset PYMOLT_API_TOKEN"),
    ):
        result = runner.invoke(app, argv)
        assert result.exit_code == 2, argv
        assert new in result.output, argv
    assert load_token() is None
