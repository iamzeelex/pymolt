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
from pymolt.migration_plan import create_migration_plan
from pymolt.migration_state import MigrationReceipt
from pymolt.status import build_status, detect_workload_command
from pymolt.verify import service
from pymolt.verify.models import CaptureMode, VerificationVerdict
from pymolt.verify.report import ContractReport
from pymolt.verify.unified import build_unified_verification

runner = CliRunner()


def _manifest(project, contents="flask==2.0.3\n"):
    (project / "requirements.txt").write_text(contents, encoding="utf-8")


def _configure(project, *, target="3.12", base="3.11"):
    EnvConfig(
        selected_manifest="requirements.txt", selected_tool=ToolChoice.UV,
        base_python=base, target_python=target,
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


def _receipt(project, baseline, **overrides):
    payload = {
        "target_python": "3.12",
        "source_manifest": "requirements.txt",
        "target_manifest_path": str(project / "requirements-target.txt"),
        "baseline_trace_path": baseline.trace_path,
        "baseline_captured_at": baseline.captured_at,
    }
    payload.update(overrides)
    receipt = MigrationReceipt(**payload)
    receipt.save(project)
    return receipt


def _plan(project):
    plan = create_migration_plan(
        project,
        [],
        [],
        target_python="3.12",
        source_manifest="requirements.txt",
        target_manifest_path=project / "requirements-target.txt",
        files_scanned=0,
    )
    plan.save(project)
    return plan


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
        assert "capture baseline" in status.next_command
        assert "cannot be done later" in status.next_reason

    def test_baseline_captured_points_at_plan(self, tmp_path):
        _manifest(tmp_path)
        _configure(tmp_path)
        (tmp_path / "requirements-target.txt").write_text("flask==3.0.0\n", encoding="utf-8")
        _capture(tmp_path, "baseline")
        status = build_status(tmp_path)
        assert _state(status, "baseline") == "done"
        assert _state(status, "migration") == "todo"
        assert status.next_command == "pymolt plan ."

    def test_reviewed_plan_points_at_exact_apply(self, tmp_path):
        _manifest(tmp_path)
        _configure(tmp_path)
        (tmp_path / "requirements-target.txt").write_text("flask==3.0.0\n", encoding="utf-8")
        _capture(tmp_path, "baseline")
        _plan(tmp_path)

        status = build_status(tmp_path)

        assert _state(status, "plan") == "done"
        assert status.next_command == "pymolt apply ."

    def test_migration_receipt_points_at_post_migration(self, tmp_path):
        _manifest(tmp_path)
        _configure(tmp_path)
        (tmp_path / "requirements-target.txt").write_text("flask==3.0.0\n", encoding="utf-8")
        baseline = _capture(tmp_path, "baseline")
        _receipt(tmp_path, baseline)

        status = build_status(tmp_path)

        assert _state(status, "migration") == "done"
        assert "capture post-migration" in status.next_command

    def test_both_captured_points_at_the_report(self, tmp_path):
        _manifest(tmp_path)
        _configure(tmp_path)
        (tmp_path / "requirements-target.txt").write_text("flask==3.0.0\n", encoding="utf-8")
        baseline = _capture(tmp_path, "baseline")
        _receipt(tmp_path, baseline)
        _capture(tmp_path, "post_migration")
        status = build_status(tmp_path)
        assert status.next_command == "pymolt verify ."
        assert _state(status, "report") == "todo"

    def test_saved_unified_verdict_completes_report_phase(self, tmp_path):
        _manifest(tmp_path)
        _configure(tmp_path)
        (tmp_path / "requirements-target.txt").write_text(
            "flask==3.0.0\n", encoding="utf-8"
        )
        baseline = _capture(tmp_path, "baseline")
        receipt = _receipt(tmp_path, baseline)
        _capture(tmp_path, "post_migration")
        build_unified_verification(
            ContractReport(root=str(tmp_path), verdict=VerificationVerdict.PASS),
            run_id=receipt.run_id,
        ).save(tmp_path)

        status = build_status(tmp_path)

        assert _state(status, "report") == "done"
        assert status.next_command is None
        assert "PASS" in status.next_reason

    def test_a_saved_verdict_on_a_capture_whose_code_changed_since_is_stale(self, tmp_path):
        """QA V1: a PASS saved on a post-migration capture stands as history once a file the
        capture recorded changed (here: reverted by hand), not as the verdict on the code."""
        _manifest(tmp_path)
        _configure(tmp_path)
        (tmp_path / "requirements-target.txt").write_text("flask==3.0.0\n", encoding="utf-8")
        baseline = _capture(tmp_path, "baseline")
        receipt = _receipt(tmp_path, baseline)
        _capture(tmp_path, "post_migration")
        build_unified_verification(
            ContractReport(root=str(tmp_path), verdict=VerificationVerdict.PASS),
            run_id=receipt.run_id,
        ).save(tmp_path)
        assert _state(build_status(tmp_path), "report") == "done"

        (tmp_path / "post_migration.py").write_text("import json\njson.loads('[]')\n",
                                                    encoding="utf-8")
        status = build_status(tmp_path)

        assert _state(status, "post-migration") == "stale"
        report = status.phase("report")
        assert report.state == "stale"
        assert report.detail.startswith("verdict: pass")
        assert "stale now: capture post-migration again, then verify" in report.detail
        assert "capture post-migration" in status.next_command

    def test_a_verdict_on_another_post_migration_recording_is_not_the_migrations(self,
                                                                                 tmp_path):
        """`verify --post-migration OTHER` saves its verdict, naming what it compared; status
        counts only a verdict whose post-migration side is the post-migration capture."""
        from pymolt.verify.report import ComparedRecordings

        _manifest(tmp_path)
        _configure(tmp_path)
        (tmp_path / "requirements-target.txt").write_text("flask==3.0.0\n", encoding="utf-8")
        baseline = _capture(tmp_path, "baseline")
        receipt = _receipt(tmp_path, baseline)
        post = _capture(tmp_path, "post_migration")

        def save(after: str, after_capture: str | None) -> None:
            build_unified_verification(
                ContractReport(root=str(tmp_path), verdict=VerificationVerdict.PASS,
                               compared=ComparedRecordings(
                                   after=after, after_capture=after_capture,
                                   before=baseline.trace_path, before_capture="baseline")),
                run_id=receipt.run_id,
            ).save(tmp_path)

        save(str(tmp_path / "elsewhere.jsonl"), None)
        assert _state(build_status(tmp_path), "report") == "todo"
        save(post.trace_path, "post-migration")
        assert _state(build_status(tmp_path), "report") == "done"

    def test_post_capture_without_receipt_is_a_migration_by_hand(self, tmp_path):
        """plan and apply are optional: a post-migration capture since the baseline, with no
        apply run, records a migration made by hand."""
        _manifest(tmp_path)
        _configure(tmp_path)
        (tmp_path / "requirements-target.txt").write_text("flask==3.0.0\n", encoding="utf-8")
        _capture(tmp_path, "baseline")
        _capture(tmp_path, "post_migration")

        status = build_status(tmp_path)

        assert _state(status, "migration") == "done"
        assert "made by hand" in status.phase("migration").detail
        assert _state(status, "post-migration") == "done"
        assert status.next_command == "pymolt verify ."


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
        _configure(tmp_path, base="3.10")   # the setup moved: another base Python

        status = build_status(tmp_path)

        assert _state(status, "baseline") == "stale"
        assert "capture baseline" in status.next_command
        assert "does not match" in status.next_reason

    def test_a_capture_taken_before_setup_is_stale(self, tmp_path):
        """It records no environment, so `apply` rejects it — status must not call it done."""
        _manifest(tmp_path)
        _capture(tmp_path, "baseline")          # no config yet: no fingerprint
        _configure(tmp_path)
        (tmp_path / "requirements-target.txt").write_text("flask==3.0.0\n", encoding="utf-8")

        status = build_status(tmp_path)

        baseline = status.phase("baseline")
        assert baseline.state == "stale"
        assert "before setup" in baseline.detail
        assert "capture baseline" in status.next_command


class TestManualMigration:
    """No `pymolt apply` (a migration done by hand or by an agent): bumping pins in the
    same manifest is the migration, so the baseline stays its starting point."""

    def test_only_manifest_contents_moved_keeps_the_baseline_done(self, tmp_path):
        _manifest(tmp_path)
        _configure(tmp_path)
        (tmp_path / "requirements-target.txt").write_text("flask==3.0.0\n", encoding="utf-8")
        _capture(tmp_path, "baseline")
        _manifest(tmp_path, "flask==3.0.0\n")   # pins bumped in the same file

        status = build_status(tmp_path)

        baseline = status.phase("baseline")
        assert baseline.state == "done"
        assert "taken as the migration's starting point" in baseline.detail
        assert "capture baseline" not in status.next_command


    def _hand_project(self, project):
        _manifest(project)
        _configure(project)
        (project / "requirements-target.txt").write_text("flask==3.0.0\n", encoding="utf-8")
        (project / "app.py").write_text("import json\n", encoding="utf-8")
        return _capture(project, "baseline")

    def test_code_edited_since_the_baseline_points_at_the_post_migration_capture(self,
                                                                                tmp_path):
        self._hand_project(tmp_path)
        (tmp_path / "app.py").write_text("import json  # migrated\n", encoding="utf-8")

        status = build_status(tmp_path)

        assert _state(status, "migration") == "done"
        assert "1 file(s) changed since the baseline" in status.phase("migration").detail
        assert "optional" in status.phase("plan").detail
        # The baseline's mode and workload (its script), by the target Python (QA H2).
        assert status.next_command == (
            "pymolt capture post-migration --mode command --target json -- <target-python> "
            f"{tmp_path / 'baseline.py'}")
        assert "Replace <target-python>" in status.next_reason

    def test_pins_bumped_with_no_code_change_are_a_migration_too(self, tmp_path):
        self._hand_project(tmp_path)
        _manifest(tmp_path, "flask==3.0.0\n")

        status = build_status(tmp_path)

        assert "pins changed since the baseline" in status.phase("migration").detail
        assert status.next_command.startswith("pymolt capture post-migration")

    def test_nothing_changed_since_the_baseline_still_points_at_plan(self, tmp_path):
        self._hand_project(tmp_path)

        status = build_status(tmp_path)

        assert _state(status, "migration") == "todo"
        assert status.next_command == "pymolt plan ."

    def test_a_valid_saved_plan_still_points_at_apply(self, tmp_path):
        self._hand_project(tmp_path)
        _plan(tmp_path)
        (tmp_path / "app.py").write_text("import json  # unrelated edit\n", encoding="utf-8")

        status = build_status(tmp_path)

        assert _state(status, "migration") == "todo"
        assert status.next_command == "pymolt apply ."

    def test_a_verdict_completes_a_migration_by_hand(self, tmp_path):
        self._hand_project(tmp_path)
        (tmp_path / "app.py").write_text("import json  # migrated\n", encoding="utf-8")
        _capture(tmp_path, "post_migration")

        def verdict(value):
            build_unified_verification(
                ContractReport(root=str(tmp_path), verdict=value), run_id=None,
            ).save(tmp_path)
            return build_status(tmp_path)

        passed = verdict(VerificationVerdict.PASS)
        assert _state(passed, "report") == "done"
        assert passed.next_command is None and "PASS" in passed.next_reason
        failed = verdict(VerificationVerdict.FAIL)
        assert failed.next_command is None             # no apply run to roll back
        assert "nothing to roll back" in failed.next_reason

    def test_a_hand_edit_after_the_post_capture_makes_it_stale(self, tmp_path):
        """By hand as after `apply`: the capture is stale once a file it recorded changed."""
        self._hand_project(tmp_path)
        (tmp_path / "app.py").write_text("import json  # migrated\n", encoding="utf-8")
        _capture(tmp_path, "post_migration")
        build_unified_verification(
            ContractReport(root=str(tmp_path), verdict=VerificationVerdict.PASS), run_id=None,
        ).save(tmp_path)
        (tmp_path / "post_migration.py").write_text("import json\njson.loads('[]')\n",
                                                    encoding="utf-8")

        status = build_status(tmp_path)

        assert _state(status, "post-migration") == "stale"
        assert "post_migration.py changed since it was captured" in \
            status.phase("post-migration").detail
        assert _state(status, "report") == "stale"
        assert "capture post-migration" in status.next_command

    def test_an_inconclusive_verdict_says_who_settles_it(self, tmp_path):
        """Decision 7: the next reason, the report phase and the agent view say whether the
        INCONCLUSIVE verdict needs a person or evidence, and what settles it."""
        from pymolt.interfaces import views
        from pymolt.verify.inconclusive import NEEDS_EVIDENCE, NEEDS_REVIEW, cause

        self._hand_project(tmp_path)
        (tmp_path / "app.py").write_text("import json  # migrated\n", encoding="utf-8")
        _capture(tmp_path, "post_migration")
        blind = cause("2 static dependency symbol(s) were not exercised", NEEDS_EVIDENCE,
                      "not-exercised", "add a test that reaches those sites, then capture "
                      "post-migration again")
        review = cause("1 edited call(s) need review: json.dumps", NEEDS_REVIEW, "review",
                       "a person reviews each finding")

        def verdict(*causes):
            build_unified_verification(ContractReport(
                root=str(tmp_path), verdict=VerificationVerdict.INCONCLUSIVE,
                verdict_reasons=[c.reason for c in causes], inconclusive_causes=list(causes),
            ), run_id=None).save(tmp_path)
            return build_status(tmp_path)

        evidence = verdict(blind)
        assert evidence.next_command is None and evidence.inconclusive_kind == "needs_evidence"
        assert evidence.next_reason == ("The verdict is INCONCLUSIVE and needs evidence: add a "
                                        "test that reaches those sites, then capture "
                                        "post-migration again.")
        assert evidence.phase("report").detail.startswith("verdict: inconclusive (needs "
                                                          "evidence)")
        person = verdict(blind, review)
        assert person.inconclusive_kind == "needs_review"
        assert person.next_reason.startswith("The verdict is INCONCLUSIVE and needs a person: "
                                             "a person reviews each finding")
        assert views.status_view(person)["data"]["inconclusive_kind"] == "needs_review"

    def test_a_post_capture_older_than_the_baseline_is_stale(self, tmp_path):
        from pymolt.verify.models import ContractState

        baseline = self._hand_project(tmp_path)
        post = _capture(tmp_path, "post_migration")
        state = ContractState.load(tmp_path / ".pymolt" / "contract_state.json")
        state.post_migration = post.model_copy(update={"captured_at": "2020-01-01T00:00:00+00:00"})
        state.save(tmp_path / ".pymolt" / "contract_state.json")
        assert baseline.captured_at > "2020"

        status = build_status(tmp_path)

        assert _state(status, "post-migration") == "stale"
        assert "before the migration" in status.phase("post-migration").detail


class TestBaselineAfterAMigration:
    """Once a migration is recorded the baseline describes where it started, not the
    project as it is now — an upgraded manifest is the migration, not staleness."""

    def _migrated(self, project, **receipt):
        _manifest(project)
        _configure(project)
        (project / "requirements-target.txt").write_text("flask==3.0.0\n", encoding="utf-8")
        baseline = _capture(project, "baseline")
        receipt.setdefault("environment_fingerprint", service.environment_fingerprint(project))
        return _receipt(project, baseline, **receipt)

    def test_manifest_upgraded_by_the_migration_keeps_the_baseline_done(self, tmp_path):
        self._migrated(tmp_path)
        _manifest(tmp_path, "flask==3.0.0\n")   # the migration's own upgrade
        _capture(tmp_path, "post_migration")

        status = build_status(tmp_path)

        assert _state(status, "baseline") == "done"
        assert _state(status, "migration") == "done"
        assert _state(status, "post-migration") == "done"
        assert status.next_command == "pymolt verify ."

    def test_post_capture_older_than_the_manifest_is_stale(self, tmp_path):
        self._migrated(tmp_path)
        _manifest(tmp_path, "flask==3.0.0\n")
        _capture(tmp_path, "post_migration")
        _manifest(tmp_path, "flask==3.1.0\n")   # moved again after the post capture

        status = build_status(tmp_path)

        assert _state(status, "baseline") == "done"
        assert _state(status, "post-migration") == "stale"
        assert "capture post-migration" in status.next_command

    def test_baseline_from_another_starting_point_is_stale(self, tmp_path):
        receipt = self._migrated(tmp_path, environment_fingerprint="0" * 16)

        status = build_status(tmp_path)

        baseline = status.phase("baseline")
        assert baseline.state == "stale"
        assert f"migration run {receipt.run_id[:8]} started from" in baseline.detail

    def test_unrecorded_starting_point_is_not_vouched_for(self, tmp_path):
        self._migrated(tmp_path, environment_fingerprint=None)
        _manifest(tmp_path, "flask==3.0.0\n")
        _configure(tmp_path, base="3.10")   # the setup moved too, not only the pins

        status = build_status(tmp_path)

        baseline = status.phase("baseline")
        # Not stale on the evidence — re-capturing now would record the migrated
        # world — but the detail must not read as a clean bill of health.
        assert baseline.state == "done"
        assert "cannot be confirmed" in baseline.detail


class TestStatusCommand:
    def test_json_is_the_whole_projection(self, tmp_path):
        _manifest(tmp_path)
        _configure(tmp_path)
        result = runner.invoke(app, ["status", str(tmp_path), "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert [p["phase"] for p in payload["phases"]] == [
            "scan", "setup", "assess", "baseline", "plan", "migration",
            "post-migration", "report",
        ]
        assert payload["next_command"].startswith("pymolt assess")

    def test_human_render_shows_the_next_command(self, tmp_path, monkeypatch):
        _manifest(tmp_path)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["status", "."])
        assert result.exit_code == 0
        assert "pymolt setup ." in result.stdout

    def test_the_next_command_names_the_dir_the_user_gave(self, tmp_path, monkeypatch):
        """Run from another directory, `.` would point at the wrong project (QA H1)."""
        project = tmp_path / "proj"
        project.mkdir()
        _manifest(project)
        monkeypatch.chdir(tmp_path)
        human = runner.invoke(app, ["status", "proj"])
        assert "pymolt setup proj" in human.stdout
        for flag in ("--json", "--agent"):
            payload = json.loads(runner.invoke(app, ["status", "proj", flag]).stdout)
            data = payload.get("data", payload)
            assert data["next_command"] == "pymolt setup proj", flag

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


class TestEnvironmentChecks:
    """What `doctor` used to report: whether the configured environments exist."""

    def test_detects_pytest_workload_without_running_it(self, tmp_path):
        (tmp_path / "tests").mkdir()

        assert detect_workload_command(tmp_path) == ["python", "-m", "pytest"]

    def test_checks_only_when_asked(self, tmp_path):
        _manifest(tmp_path)
        _configure(tmp_path)

        assert build_status(tmp_path).checks == []
        checks = {c.name: c for c in build_status(tmp_path, check_environments=True).checks}

        assert checks["base environment"].state == "warning"
        assert checks["target environment"].state == "warning"
        assert "setup" in checks["target environment"].hint

    def test_a_missing_target_environment_is_an_error(self, tmp_path):
        _manifest(tmp_path)
        EnvConfig(
            selected_manifest="requirements.txt", selected_tool=ToolChoice.UV,
            base_python="3.11", target_python="3.12",
            target_env_path=str(tmp_path / ".venv-target"),
        ).save(tmp_path / ".pymolt" / "env_config.json")

        checks = {c.name: c for c in build_status(tmp_path, check_environments=True).checks}

        assert checks["target environment"].state == "error"

    def test_status_command_prints_the_checks(self, tmp_path):
        _manifest(tmp_path)
        _configure(tmp_path)

        result = runner.invoke(app, ["status", str(tmp_path)])

        assert result.exit_code == 0
        assert "target environment" in result.stdout



def test_the_report_phase_labels_coverage_and_recall_as_bounds(tmp_path):
    """QA V6: a verdict whose coverage counts contacts as ran only by their function or file
    shows coverage as an upper and recall as a lower bound."""
    _manifest(tmp_path)
    _configure(tmp_path)
    (tmp_path / "requirements-target.txt").write_text("flask==3.0.0\n", encoding="utf-8")
    baseline = _capture(tmp_path, "baseline")
    receipt = _receipt(tmp_path, baseline)
    _capture(tmp_path, "post_migration")
    build_unified_verification(
        ContractReport(root=str(tmp_path), verdict=VerificationVerdict.INCONCLUSIVE,
                       coverage=0.66, recall=0.77, coverage_granularity="function",
                       coverage_bounded=True),
        run_id=receipt.run_id,
    ).save(tmp_path)
    detail = build_status(tmp_path).phase("report").detail
    assert "coverage at most 66%, recall at least 77% (granularity function)" in detail


class TestShownDir:
    """Every printed command names the project dir the way the user can run it (QA H1)."""

    def test_as_given_or_relative_to_the_current_dir(self, tmp_path):
        from pymolt.status import shown_dir

        inside = tmp_path / "a" / "b"
        assert shown_dir("tests/artifacts/flasgger/", cwd=tmp_path) == "tests/artifacts/flasgger"
        assert shown_dir("../other", cwd=tmp_path) == "../other"
        assert shown_dir(".", cwd=tmp_path) == "."
        assert shown_dir(tmp_path, cwd=tmp_path) == "."
        assert shown_dir(inside, cwd=tmp_path) == "a/b"
        assert shown_dir("/home/user/proj", cwd=tmp_path) == "/home/user/proj"
        assert shown_dir("/home/user/my proj", cwd=tmp_path) == "'/home/user/my proj'"

    def test_every_command_names_it(self, tmp_path):
        from pymolt.status import capture_command, command

        assert command("proj", "apply", "--step", "env-1") == "pymolt apply proj --step env-1"
        assert capture_command("proj", "baseline", "--replicate") == \
            "pymolt capture baseline --project-dir proj --replicate"
        assert capture_command(".", "baseline", "--replicate") == \
            "pymolt capture baseline --replicate"

    def test_status_renders_its_commands_for_the_shown_dir(self, tmp_path):
        _manifest(tmp_path)
        assert build_status(tmp_path, shown="proj").next_command == "pymolt setup proj"
        _configure(tmp_path)
        assert build_status(tmp_path, shown="proj").next_command == \
            "pymolt assess proj --target-python 3.12"
        (tmp_path / "requirements-target.txt").write_text("flask==3.0.0\n", encoding="utf-8")
        status = build_status(tmp_path, shown="proj")
        assert status.next_command.startswith(
            "pymolt capture baseline --project-dir proj --mode tests -- ")
        assert build_status(tmp_path).next_command.startswith(
            "pymolt capture baseline --mode tests -- ")

    def test_a_command_hint_after_a_phase_names_the_dir(self, tmp_path, monkeypatch):
        project = tmp_path / "proj"
        project.mkdir()
        _manifest(project)
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(app, ["scan", "proj"])
        assert result.exit_code == 0
        assert "pymolt setup proj" in result.stderr
