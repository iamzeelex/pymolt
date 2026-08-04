"""pymolt.status — the funnel projected, never enforced.

The value of this module is the *next* command: one, not a menu. These tests
pin the progression through a real migration, and the two states that are worse
than "not done yet" — evidence that is empty, and evidence that no longer
describes the project.
"""

from __future__ import annotations

import json
import sys

from typer.testing import CliRunner

from pymolt.ingestion.config import EnvConfig, ToolChoice
from pymolt.interfaces.cli.commands import app
from pymolt.status import build_status
from pymolt.verify import service
from pymolt.verify.models import CaptureMode

runner = CliRunner()


def _manifest(project, contents="flask==2.0.3\n"):
    (project / "requirements.txt").write_text(contents, encoding="utf-8")


def _configure(project, *, target="3.12"):
    EnvConfig(
        selected_manifest="requirements.txt", selected_tool=ToolChoice.UV,
        base_python="3.11", target_python=target,
    ).save(project / ".pymolt" / "env_config.json")


def _capture(project, when, *, body="import json\njson.loads('{}')\n", target="json"):
    script = project / f"{when}.py"
    script.write_text(body, encoding="utf-8")
    return service.capture_named_trace(
        project, when, CaptureMode.LIVE_COMMAND,
        command=[sys.executable, str(script)], target=target,
    )


def _state(status, phase):
    found = status.phase(phase)
    assert found is not None, f"no {phase} phase in {[p.phase for p in status.phases]}"
    return found.state


class TestProgression:
    def test_empty_directory_has_nothing_to_migrate(self, tmp_path):
        status = build_status(tmp_path)
        assert _state(status, "scan") == "todo"
        assert status.next_command is None      # no command can help here
        assert "manifest" in status.next_reason

    def test_manifest_only_points_at_setup(self, tmp_path):
        _manifest(tmp_path)
        status = build_status(tmp_path)
        assert _state(status, "scan") == "done"
        assert _state(status, "setup") == "todo"
        assert _state(status, "assess") == "blocked"   # no target chosen yet
        assert status.next_command == "pymolt setup ."

    def test_configured_points_at_assess_with_the_chosen_target(self, tmp_path):
        _manifest(tmp_path)
        _configure(tmp_path, target="3.13")
        status = build_status(tmp_path)
        assert _state(status, "setup") == "done"
        assert _state(status, "assess") == "todo"
        assert status.next_command == "pymolt assess . --target-python 3.13"

    def test_assessed_points_at_the_baseline_capture(self, tmp_path):
        _manifest(tmp_path)
        _configure(tmp_path)
        (tmp_path / "requirements-target.txt").write_text("flask==3.0.0\n", encoding="utf-8")
        status = build_status(tmp_path)
        assert _state(status, "assess") == "done"
        assert _state(status, "baseline") == "todo"
        assert "capture --when baseline" in status.next_command
        assert "cannot be done later" in status.next_reason

    def test_baseline_captured_points_at_post_migration(self, tmp_path):
        _manifest(tmp_path)
        _configure(tmp_path)
        (tmp_path / "requirements-target.txt").write_text("flask==3.0.0\n", encoding="utf-8")
        _capture(tmp_path, "baseline")
        status = build_status(tmp_path)
        assert _state(status, "baseline") == "done"
        assert "capture --when post-migration" in status.next_command

    def test_both_captured_points_at_the_report(self, tmp_path):
        _manifest(tmp_path)
        _configure(tmp_path)
        (tmp_path / "requirements-target.txt").write_text("flask==3.0.0\n", encoding="utf-8")
        _capture(tmp_path, "baseline")
        _capture(tmp_path, "post_migration")
        status = build_status(tmp_path)
        assert status.next_command == "pymolt contract report ."
        assert _state(status, "report") == "todo"


class TestWorseThanNotDone:
    def test_an_empty_capture_is_stale_not_done(self, tmp_path):
        """0 events would later diff as 'everything disappeared' — a finding-shaped
        artifact of a missing input. Written straight to the state file: this is a
        test of the projection, not of what the tracer happens to record."""
        from pymolt.verify.models import ContractSlot, ContractState

        _manifest(tmp_path)
        _configure(tmp_path)
        trace = tmp_path / "empty.jsonl"
        trace.write_text("", encoding="utf-8")
        ContractState(baseline=ContractSlot(
            trace_path=str(trace), captured_at="2026-08-01T00:00:00+00:00",
            mode=CaptureMode.LIVE_COMMAND, command=["python", "-c", "pass"], events=0,
            env_fingerprint=service.environment_fingerprint(tmp_path),
        )).save(tmp_path / ".pymolt" / "contract_state.json")

        status = build_status(tmp_path)

        baseline = status.phase("baseline")
        assert baseline.state == "stale"
        assert "EMPTY" in baseline.detail

    def test_a_capture_from_another_world_is_stale(self, tmp_path):
        _manifest(tmp_path)
        _configure(tmp_path)
        (tmp_path / "requirements-target.txt").write_text("flask==3.0.0\n", encoding="utf-8")
        _capture(tmp_path, "baseline")
        _manifest(tmp_path, "flask==3.0.0\n")   # the world moved

        status = build_status(tmp_path)

        assert _state(status, "baseline") == "stale"
        assert "capture --when baseline" in status.next_command
        assert "no longer describes" in status.next_reason


class TestStatusCommand:
    def test_json_is_the_whole_projection(self, tmp_path):
        _manifest(tmp_path)
        _configure(tmp_path)
        result = runner.invoke(app, ["status", str(tmp_path), "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert [p["phase"] for p in payload["phases"]] == [
            "scan", "setup", "assess", "baseline", "post-migration", "report",
        ]
        assert payload["next_command"].startswith("pymolt assess")

    def test_human_render_shows_the_next_command(self, tmp_path):
        _manifest(tmp_path)
        result = runner.invoke(app, ["status", str(tmp_path)])
        assert result.exit_code == 0
        assert "pymolt setup ." in result.stdout

    def test_writes_nothing(self, tmp_path):
        """status is a projection: it must never create the state it reports on."""
        _manifest(tmp_path)
        before = {p.name for p in tmp_path.iterdir()}
        runner.invoke(app, ["status", str(tmp_path)])
        assert {p.name for p in tmp_path.iterdir()} == before

    def test_missing_directory_is_a_usage_error(self, tmp_path):
        from pymolt.interfaces.cli.output import EXIT_USAGE

        result = runner.invoke(app, ["status", str(tmp_path / "nope"), "--json"])
        assert result.exit_code == EXIT_USAGE
        assert json.loads(result.stdout)["ok"] is False
