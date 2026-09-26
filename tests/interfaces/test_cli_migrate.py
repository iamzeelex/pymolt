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
from pymolt.codemods.client import DependencyMigration
from pymolt.codemods.models import CodemodPattern, FilePreview
from pymolt.interfaces.cli.commands import app
from pymolt.migration_state import MigrationReceipt
from pymolt.verify import service as verify_service
from pymolt.verify.models import CaptureMode, CaptureValidity, ContractSlot, ContractState

runner = CliRunner()


def test_migrate_help() -> None:
    result = runner.invoke(app, ["migrate", "--help"])
    assert result.exit_code == 0, result.output
    assert "Run the complete end-to-end Python migration workflow" in result.output
    assert "--target-python" in result.output
    assert "--endpoint" in result.output
    assert "--patch" in result.output


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
    source = "from flask.helpers import safe_join\n"
    (tmp_path / "app.py").write_text(source, encoding="utf-8")
    pattern = CodemodPattern(
        old_qualname="flask.helpers.safe_join",
        new_qualname="werkzeug.utils.safe_join",
        kind="rewrite-import",
        confidence="high",
    )
    preview = FilePreview(
        path=str(tmp_path / "app.py"),
        old_source=source,
        new_source="from werkzeug.utils import safe_join\n",
        sites=1,
        patterns=[pattern],
    )
    patch_path = tmp_path / "migration.patch"

    # Dry-run uses the exact per-file preview returned by the codemod service.
    with patch("pymolt.assess.service.run_assess", return_value=fake_assess_result), \
         patch("pymolt.codemods.service.resolve_codemod_migrations", return_value=([MagicMock(name="flask")], "flask 2.0.3 -> 3.0.0")), \
         patch("pymolt.codemods.service.preview_codemods", return_value=({"flask": [pattern]}, [preview])):

        result = runner.invoke(app, [
            "migrate", str(tmp_path), "--target-python", "3.12",
            "--patch", str(patch_path),
        ])
        assert result.exit_code == 0
        assert "Starting PyMolt End-to-End Migration" in result.output
        assert "Migration pipeline finished!" in result.output
        assert "DRY-RUN PREVIEW" in result.output
        assert "werkzeug.utils" in result.output
        assert "Matched rewrite sites" in result.output
        assert (tmp_path / "app.py").read_text(encoding="utf-8") == source
        assert "--- a/app.py" in patch_path.read_text(encoding="utf-8")


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


def test_migrate_write_requires_valid_baseline(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("flask==2.0.3\n", encoding="utf-8")

    result = runner.invoke(app, ["migrate", str(tmp_path), "--write"])

    assert result.exit_code != 0
    assert "without a valid baseline" in result.output


def test_migrate_write_records_receipt_and_recommends_post_capture(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("flask==2.0.3\n", encoding="utf-8")
    (tmp_path / "requirements-target.txt").write_text("flask==3.0.0\n", encoding="utf-8")
    pymolt_dir = tmp_path / ".pymolt"
    pymolt_dir.mkdir()
    (pymolt_dir / "env_config.json").write_text(json.dumps({
        "target_python": "3.12",
        "selected_manifest": "requirements.txt",
        "selected_tool": "uv",
    }), encoding="utf-8")
    trace = pymolt_dir / "baseline.jsonl"
    trace.write_text('{"q":"flask.safe_join"}\n', encoding="utf-8")
    baseline = ContractSlot(
        trace_path=str(trace),
        captured_at="2026-09-09T12:00:00+00:00",
        mode=CaptureMode.TEST_SUITE,
        command=["pytest"],
        target="flask",
        events=1,
        validity=CaptureValidity.VALID,
        env_fingerprint=verify_service.environment_fingerprint(tmp_path),
    )
    ContractState(baseline=baseline).save(pymolt_dir / "contract_state.json")

    fake_assess_result = AssessResult(
        project_dir=str(tmp_path), source_manifest="requirements.txt",
        target_python="3.12", target_resolved=True,
        target_manifest_path=str(tmp_path / "requirements-target.txt"),
        rows=[{"package": "flask", "legacy_version": "2.0.3", "target_version": "3.0.0", "status": "upgrade"}],
    )
    migration = DependencyMigration("flask", "2.0.3", "3.0.0")
    with patch("pymolt.assess.service.run_assess", return_value=fake_assess_result), \
         patch("pymolt.codemods.service.resolve_codemod_migrations", return_value=([migration], "assess")), \
         patch("pymolt.codemods.service.preview_codemods", return_value=({"flask": []}, [])):
        result = runner.invoke(app, ["migrate", str(tmp_path), "--write"])

    assert result.exit_code == 0, result.output
    receipt = MigrationReceipt.load(tmp_path)
    assert receipt is not None
    assert receipt.baseline_trace_path == baseline.trace_path
    assert "capture --when post-migration" in result.output
