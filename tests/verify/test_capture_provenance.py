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

    def test_attach_archives_before_the_engineer_starts_appending(self, tmp_path):
        """The attach flow writes into the slot path itself, so the displacement
        has to happen at start — by --collect time the two runs are one file."""
        _capture(tmp_path)
        original = (tmp_path / ".pymolt" / "contract_traces" / "baseline.jsonl").read_bytes()

        service.start_attached_capture(tmp_path, "baseline", target="json")

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
