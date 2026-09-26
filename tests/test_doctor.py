from __future__ import annotations

import json

from typer.testing import CliRunner

from pymolt.doctor import detect_workload_command, run_doctor
from pymolt.ingestion.config import EnvConfig, ToolChoice
from pymolt.interfaces.cli.commands import app
from pymolt.interfaces.cli.output import EXIT_INCONCLUSIVE

runner = CliRunner()


def test_detects_pytest_workload_without_running_it(tmp_path):
    (tmp_path / "tests").mkdir()

    assert detect_workload_command(tmp_path) == ["python", "-m", "pytest"]


def test_doctor_reports_missing_prerequisites(tmp_path):
    report = run_doctor(tmp_path)

    assert report.ready is False
    assert any(check.name == "manifest" and check.state == "error" for check in report.checks)
    assert any(check.name == "configuration" and check.state == "error" for check in report.checks)


def test_doctor_cli_json_is_machine_readable_and_nonzero_when_unready(tmp_path):
    result = runner.invoke(app, ["doctor", str(tmp_path), "--json"])

    assert result.exit_code == EXIT_INCONCLUSIVE
    payload = json.loads(result.stdout)
    assert payload["ready"] is False
    assert payload["checks"]


def test_configured_project_exposes_exact_workload(tmp_path):
    (tmp_path / "requirements.txt").write_text("flask==2.0.3\n")
    (tmp_path / "tests").mkdir()
    EnvConfig(
        selected_manifest="requirements.txt",
        selected_tool=ToolChoice.UV,
        base_python="3.11",
        target_python="3.13",
    ).save(tmp_path / ".pymolt" / "env_config.json")

    report = run_doctor(tmp_path)

    assert report.workload_command == ["python", "-m", "pytest"]
    assert any(check.name == "configuration" and check.state == "ok" for check in report.checks)
