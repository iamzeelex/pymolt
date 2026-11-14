"""Provenance for witnessed state: archiving, fingerprints, staleness.

Most of what pymolt produces is derived and can simply be recomputed. A capture
cannot: it records an environment that stops existing the moment you migrate. So
two invariants get their own tests here —

* overwriting a capture **displaces** it, never destroys it, and
* a capture that no longer describes the project **says so**, rather than
  quietly backing a confident-looking verdict.
"""

from __future__ import annotations

import json
import sys

from pymolt.ingestion.config import EnvConfig, ToolChoice
from pymolt.migration_plan import MigrationPlan, apply_migration_plan, create_migration_plan
from pymolt.migration_state import MigrationReceipt
from pymolt.verify import service
from pymolt.verify.models import CaptureMode, ContractSlot, ContractState


def _configure(project_dir, *, manifest="requirements.txt", base="3.11", contents="flask==2.0.3\n"):
    (project_dir / manifest).write_text(contents, encoding="utf-8")
    EnvConfig(
        selected_manifest=manifest, selected_tool=ToolChoice.UV, base_python=base,
    ).save(project_dir / ".pymolt" / "env_config.json")


def _capture(project_dir, when="baseline", body="import json\njson.loads('{}')\n"):
    script = project_dir / f"{when}_script.py"
    script.write_text(body, encoding="utf-8")
    return service.capture_named_trace(
        project_dir, when, CaptureMode.LIVE_COMMAND,
        command=[sys.executable, str(script)], target="json",
    )


# ── archiving ─────────────────────────────────────────────────────────────────

class TestArchiveNeverDestroys:
    def test_recapture_moves_the_previous_recording_aside(self, tmp_path):
        first = _capture(tmp_path)
        first_bytes = (tmp_path / ".pymolt" / "contract_traces" / "baseline.jsonl").read_bytes()
        assert first.archived_previous is None  # nothing to displace on a first capture

        second = _capture(tmp_path, body="import json\njson.dumps({'a': 1})\n")

        assert second.archived_previous is not None
        archived = tmp_path / ".pymolt" / "contract_traces" / "archive"
        kept = list(archived.glob("baseline-*.jsonl"))
        assert len(kept) == 1
        # The displaced recording survived byte-for-byte.
        assert kept[0].read_bytes() == first_bytes
        assert kept[0].name == service.Path(second.archived_previous).name

    def test_archive_is_named_for_when_the_capture_was_taken(self, tmp_path):
        first = _capture(tmp_path)
        _capture(tmp_path)
        archived = next((tmp_path / ".pymolt" / "contract_traces" / "archive").glob("*.jsonl"))
        # The stamp identifies the world it recorded, not the moment it was moved.
        assert first.captured_at[:10].replace("-", "") in archived.name.replace("-", "")

    def test_repeated_overwrites_never_collide(self, tmp_path):
        for _ in range(3):
            _capture(tmp_path)
        kept = list((tmp_path / ".pymolt" / "contract_traces" / "archive").glob("*.jsonl"))
        assert len(kept) == 2  # three captures displaced two predecessors

    def test_attach_keeps_active_capture_until_pending_trace_is_valid(self, tmp_path):
        """Starting attach must not displace the last valid evidence."""
        _capture(tmp_path)
        original = (tmp_path / ".pymolt" / "contract_traces" / "baseline.jsonl").read_bytes()

        instructions = service.start_attached_capture(tmp_path, "baseline", target="json")

        active = tmp_path / ".pymolt" / "contract_traces" / "baseline.jsonl"
        assert active.read_bytes() == original
        assert not (tmp_path / ".pymolt" / "contract_traces" / "archive").exists()

        pending = service.Path(instructions.out_path)
        pending.write_text('{"q":"json.loads"}\n', encoding="utf-8")
        service.finalize_attached_capture(tmp_path, "baseline", pending, target="json")

        kept = list((tmp_path / ".pymolt" / "contract_traces" / "archive").glob("*.jsonl"))
        assert len(kept) == 1 and kept[0].read_bytes() == original

    def test_missing_trace_file_is_not_an_error(self, tmp_path):
        state = ContractState(baseline=ContractSlot(
            trace_path=str(tmp_path / "gone.jsonl"), captured_at="2026-01-01T00:00:00+00:00",
            mode=CaptureMode.TEST_SUITE,
        ))
        state.save(tmp_path / ".pymolt" / "contract_state.json")
        assert service.archive_existing_capture(tmp_path, "baseline") is None


# ── fingerprints ──────────────────────────────────────────────────────────────

class TestEnvironmentFingerprint:
    def test_no_config_is_unknown_not_a_value(self, tmp_path):
        assert service.environment_fingerprint(tmp_path) is None

    def test_stable_across_calls(self, tmp_path):
        _configure(tmp_path)
        first = service.environment_fingerprint(tmp_path)
        assert first == service.environment_fingerprint(tmp_path)

    def test_manifest_contents_move_it(self, tmp_path):
        _configure(tmp_path)
        before = service.environment_fingerprint(tmp_path)
        (tmp_path / "requirements.txt").write_text("flask==3.0.0\n", encoding="utf-8")
        assert service.environment_fingerprint(tmp_path) != before

    def test_base_python_moves_it(self, tmp_path):
        _configure(tmp_path, base="3.11")
        before = service.environment_fingerprint(tmp_path)
        _configure(tmp_path, base="3.12")
        assert service.environment_fingerprint(tmp_path) != before

    def test_capture_is_stamped_with_it(self, tmp_path):
        _configure(tmp_path)
        slot = _capture(tmp_path)
        assert slot.env_fingerprint == service.environment_fingerprint(tmp_path)


# ── staleness surfaces in the report ──────────────────────────────────────────

class TestStalenessInTheReport:
    def _project(self, tmp_path):
        (tmp_path / "app.py").write_text("import json\njson.loads('{}')\n", encoding="utf-8")
        _configure(tmp_path)

    def test_unchanged_project_is_not_stale(self, tmp_path):
        self._project(tmp_path)
        _capture(tmp_path)
        report = service.build_contract_report_from_state(tmp_path)
        assert report.baseline_stale is False

    def test_changed_manifest_marks_the_evidence_stale(self, tmp_path):
        self._project(tmp_path)
        _capture(tmp_path)
        (tmp_path / "requirements.txt").write_text("flask==3.0.0\n", encoding="utf-8")

        report = service.build_contract_report_from_state(tmp_path)

        assert report.baseline_stale is True
        assert any("STALE EVIDENCE" in n for n in report.notes)

    def test_capture_without_a_fingerprint_is_unknown_never_fresh(self, tmp_path):
        """State written before fingerprints existed must not be vouched for."""
        self._project(tmp_path)
        trace = tmp_path / "old.jsonl"
        trace.write_text(json.dumps({"q": "json.loads"}) + "\n", encoding="utf-8")
        ContractState(baseline=ContractSlot(
            trace_path=str(trace), captured_at="2026-01-01T00:00:00+00:00",
            mode=CaptureMode.TEST_SUITE, events=1,
        )).save(tmp_path / ".pymolt" / "contract_state.json")

        report = service.build_contract_report_from_state(tmp_path)

        assert report.baseline_stale is None  # neither confirmed fresh nor stale
        assert any("staleness unknown" in n for n in report.notes)

    def test_no_config_reports_the_check_could_not_run(self, tmp_path):
        (tmp_path / "app.py").write_text("import json\n", encoding="utf-8")
        _capture(tmp_path)  # captured with no EnvConfig -> no fingerprint to stamp
        report = service.build_contract_report_from_state(tmp_path)
        assert report.baseline_stale is None
        assert any("staleness unchecked" in n for n in report.notes)

    def test_dropped_events_are_never_silently_treated_as_complete(self, tmp_path):
        self._project(tmp_path)
        slot = _capture(tmp_path)
        state = service.load_contract_state(tmp_path)
        state.baseline = slot.model_copy(update={"dropped_events": 3})
        state.save(tmp_path / ".pymolt" / "contract_state.json")

        report = service.build_contract_report_from_state(tmp_path)

        assert any("dropped 3 event" in reason for reason in report.verdict_reasons)


# ── the baseline is judged against where the migration started ───────────────

STALE = "capture evidence is stale"
UNKNOWN = "capture freshness could not be established"


class TestBaselineIsJudgedAgainstTheMigrationStart:
    """A migration that upgrades the manifest moves "now" away from the baseline by
    design. The baseline must describe the starting point; only the post-migration
    capture must describe the project as it is now."""

    def _project(self, tmp_path):
        (tmp_path / "app.py").write_text("import json\njson.loads('{}')\n", encoding="utf-8")
        _configure(tmp_path)

    def _capture(self, tmp_path, when):
        # One workload for both sides, so only freshness can make them incomparable.
        script = tmp_path / "workload.py"
        script.write_text("import json\njson.loads('{}')\n", encoding="utf-8")
        return service.capture_named_trace(
            tmp_path, when, CaptureMode.LIVE_COMMAND,
            command=[sys.executable, str(script)], target="json",
        )

    def _record_migration(self, tmp_path, baseline, **overrides):
        """The receipt `pymolt apply` leaves, started from the world as it is now."""
        payload = {
            "target_python": "3.12",
            "source_manifest": "requirements.txt",
            "baseline_trace_path": baseline.trace_path,
            "baseline_captured_at": baseline.captured_at,
            "environment_fingerprint": service.environment_fingerprint(tmp_path),
        }
        payload.update(overrides)
        receipt = MigrationReceipt(**payload)
        receipt.save(tmp_path)
        return receipt

    def _upgrade(self, tmp_path, contents="flask==3.0.0\n", manifest="requirements.txt"):
        (tmp_path / manifest).write_text(contents, encoding="utf-8")

    def test_manifest_upgraded_by_the_migration_leaves_the_baseline_fresh(self, tmp_path):
        """The reported bug, through the real plan → apply path."""
        self._project(tmp_path)
        self._capture(tmp_path, "baseline")
        plan = create_migration_plan(
            tmp_path, [], [], target_python="3.12", source_manifest="requirements.txt",
            target_manifest_path=None, files_scanned=0,
        )
        apply_migration_plan(tmp_path, plan)
        self._upgrade(tmp_path)
        self._capture(tmp_path, "post_migration")

        report = service.build_contract_report_from_state(tmp_path)

        assert report.baseline_stale is False
        assert not any("STALE" in n or "staleness unknown" in n for n in report.notes)
        assert STALE not in report.verdict_reasons
        assert UNKNOWN not in report.verdict_reasons

    def test_post_migration_older_than_the_current_manifest_is_stale(self, tmp_path):
        self._project(tmp_path)
        baseline = self._capture(tmp_path, "baseline")
        self._record_migration(tmp_path, baseline)
        self._upgrade(tmp_path)
        self._capture(tmp_path, "post_migration")
        self._upgrade(tmp_path, "flask==3.1.0\n")  # the world moved after the post capture

        report = service.build_contract_report_from_state(tmp_path)

        assert report.baseline_stale is True
        stale = [n for n in report.notes if "STALE EVIDENCE" in n]
        assert len(stale) == 1
        assert "post-migration capture" in stale[0]
        assert "baseline" not in stale[0]  # the baseline is still the right starting point
        assert STALE in report.verdict_reasons

    def test_baseline_from_another_starting_point_is_stale(self, tmp_path):
        """Setup re-run with another manifest before migrating: the baseline recorded a
        world the migration never started from."""
        self._project(tmp_path)
        baseline = self._capture(tmp_path, "baseline")
        _configure(tmp_path, manifest="requirements-prod.txt", contents="flask==2.0.3\nrequests\n")
        receipt = self._record_migration(tmp_path, baseline)
        self._upgrade(tmp_path, "flask==3.0.0\nrequests\n", manifest="requirements-prod.txt")
        self._capture(tmp_path, "post_migration")

        report = service.build_contract_report_from_state(tmp_path)

        assert report.baseline_stale is True
        stale = [n for n in report.notes if "STALE EVIDENCE" in n]
        assert len(stale) == 1
        assert "baseline capture" in stale[0]
        assert f"migration run {receipt.run_id[:8]} started from" in stale[0]
        assert STALE in report.verdict_reasons

    def test_no_recorded_migration_is_unknown_never_fresh(self, tmp_path):
        """Without a receipt the manifest may have drifted before migrating or moved
        as part of it — the evidence cannot tell those apart, so it says neither."""
        self._project(tmp_path)
        self._capture(tmp_path, "baseline")
        self._upgrade(tmp_path)
        self._capture(tmp_path, "post_migration")

        report = service.build_contract_report_from_state(tmp_path)

        assert report.baseline_stale is None
        assert not any("STALE EVIDENCE" in n for n in report.notes)
        assert any(
            "staleness unknown for the baseline" in n and "pymolt apply" in n
            for n in report.notes
        )
        assert UNKNOWN in report.verdict_reasons

    def test_a_rolled_back_migration_pins_nothing(self, tmp_path):
        self._project(tmp_path)
        baseline = self._capture(tmp_path, "baseline")
        self._record_migration(tmp_path, baseline, status="rolled_back")
        self._upgrade(tmp_path)
        self._capture(tmp_path, "post_migration")

        report = service.build_contract_report_from_state(tmp_path)

        assert report.baseline_stale is None
        assert UNKNOWN in report.verdict_reasons

    def test_receipt_without_a_starting_point_is_unknown(self, tmp_path):
        self._project(tmp_path)
        baseline = self._capture(tmp_path, "baseline")
        receipt = self._record_migration(tmp_path, baseline, environment_fingerprint=None)
        self._upgrade(tmp_path)
        self._capture(tmp_path, "post_migration")

        report = service.build_contract_report_from_state(tmp_path)

        assert report.baseline_stale is None
        assert any(
            f"migration run {receipt.run_id[:8]} predates recording" in n
            for n in report.notes
        )

    def test_older_receipt_falls_back_to_the_frozen_plan(self, tmp_path):
        """Receipts written before the field existed: apply only ever accepted a plan
        whose fingerprint matched the project, so the frozen plan pins the start."""
        self._project(tmp_path)
        baseline = self._capture(tmp_path, "baseline")
        plan = MigrationPlan(
            target_python="3.12", source_manifest="requirements.txt",
            environment_fingerprint=service.environment_fingerprint(tmp_path),
        )
        run_dir = tmp_path / ".pymolt" / "migrations" / plan.plan_id
        frozen = plan.save(tmp_path, run_dir / "plan.json")
        self._record_migration(
            tmp_path, baseline, run_id=plan.plan_id, plan_path=str(frozen),
            environment_fingerprint=None,
        )
        self._upgrade(tmp_path)
        self._capture(tmp_path, "post_migration")

        report = service.build_contract_report_from_state(tmp_path)

        assert report.baseline_stale is False

    def test_unchanged_manifest_needs_no_receipt(self, tmp_path):
        """Nothing moved since the baseline, so it describes the start whenever that was:
        a code-only migration stays judgeable without `pymolt apply`."""
        self._project(tmp_path)
        self._capture(tmp_path, "baseline")
        self._capture(tmp_path, "post_migration")

        report = service.build_contract_report_from_state(tmp_path)

        assert report.baseline_stale is False
        assert STALE not in report.verdict_reasons
        assert UNKNOWN not in report.verdict_reasons
