"""
tests/interfaces/test_cli_migrate.py

Tests for the modernized PyMolt CLI UX:
1. `pymolt migrate` macro command.
2. `pymolt env` direct root invocation.
3. `pymolt codemods` with unified `--endpoint` option.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from pymolt.assess.service import AssessResult
from pymolt.interfaces.cli.commands import app

runner = CliRunner()


def test_migrate_help() -> None:
    result = runner.invoke(app, ["migrate", "--help"])
    assert result.exit_code == 0
    assert "Run the complete end-to-end Python migration workflow" in result.output
    assert "--target-python" in result.output
    assert "--endpoint" in result.output


def test_codemods_help_has_endpoint() -> None:
    result = runner.invoke(app, ["codemods", "--help"])
    assert result.exit_code == 0
    assert "--endpoint" in result.output


def test_env_direct_invocation(tmp_path: Path) -> None:
    # Setup minimal env config
    pymolt_dir = tmp_path / ".pymolt"
    pymolt_dir.mkdir()
    (pymolt_dir / "env_config.json").write_text(
        json.dumps({"target_python": "3.12", "selected_manifest": "requirements.txt"}),
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text("flask==2.0.3\n", encoding="utf-8")

    # Invoke `pymolt env hint <dir>`
    result = runner.invoke(app, ["env", "hint", str(tmp_path)])
    assert result.exit_code == 0
    assert "pymolt env hint — target Python 3.12" in result.output


def test_migrate_executes_pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Setup test workspace
    pymolt_dir = tmp_path / ".pymolt"
    pymolt_dir.mkdir(exist_ok=True)
    (pymolt_dir / "env_config.json").write_text(
        json.dumps({
            "target_python": "3.12",
            "selected_manifest": "requirements.txt",
            "selected_tool": "uv",
        }),
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text("flask==2.0.3\n", encoding="utf-8")

    fake_assess_result = AssessResult(
        project_dir=str(tmp_path),
        source_manifest="requirements.txt",
        target_python="3.12",
        target_resolved=True,
        target_manifest_path=str(tmp_path / "requirements-target.txt"),
        rows=[
            {
                "package": "flask",
                "legacy_version": "2.0.3",
                "target_version": "3.0.0",
                "status": "upgrade",
                "risk_tier": "low",
            }
        ],
    )

    (tmp_path / "requirements-target.txt").write_text("flask==3.0.0\n", encoding="utf-8")

    # Mock run_assess, resolve_codemod_migrations and run_codemods
    with patch("pymolt.assess.service.run_assess", return_value=fake_assess_result), \
         patch("pymolt.codemods.service.resolve_codemod_migrations", return_value=([MagicMock(name="flask")], "flask 2.0.3 -> 3.0.0")), \
         patch("pymolt.codemods.service.run_codemods", return_value=({"flask": []}, MagicMock())):

        result = runner.invoke(app, ["migrate", str(tmp_path), "--target-python", "3.12"])
        assert result.exit_code == 0
        assert "Starting PyMolt End-to-End Migration" in result.output
        assert "Migration pipeline finished!" in result.output
        assert "DRY-RUN PREVIEW" in result.output


def test_migrate_no_sources(tmp_path: Path) -> None:
    result = runner.invoke(app, ["migrate", str(tmp_path)])
    assert result.exit_code != 0
    assert "No Python dependency sources detected" in result.output


def test_migrate_resolution_failure(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("flask==2.0.3\n", encoding="utf-8")
    fake_assess_result = AssessResult(
        project_dir=str(tmp_path),
        source_manifest="requirements.txt",
        target_python="3.12",
        target_resolved=False,
        target_error="Incompatible package version constraints",
    )

    with patch("pymolt.assess.service.run_assess", return_value=fake_assess_result):
        result = runner.invoke(app, ["migrate", str(tmp_path), "--target-python", "3.12"])
        assert result.exit_code != 0
        assert "Target dependency resolution failed" in result.output
        assert "Incompatible package version" in result.output
