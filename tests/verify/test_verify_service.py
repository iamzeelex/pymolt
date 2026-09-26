"""pymolt.verify.service — capture orchestration + named contract state.

Covers: capture_trace (parity with the old CLI-embedded _trace_command
behavior, plus cancellation), capture_named_trace's state bookkeeping and
overwrite semantics, the attach/collect flow, ContractState round-tripping,
and build_contract_report_from_state's trace/against resolution rules.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

from pymolt.verify import service
from pymolt.verify.models import CaptureMode, CaptureValidity, ContractState


def _write_script(tmp_path, body: str, name: str = "app.py"):
    p = tmp_path / name
    p.write_text(body)
    return p


class TestCaptureTrace:
    def test_local_capture_produces_events_and_merges_to_out_path(self, tmp_path):
        script = _write_script(tmp_path, "import json\njson.loads('{\"a\": 1}')\n")
        out = tmp_path / "trace.jsonl"
        result = service.capture_trace("json", [sys.executable, str(script)], out, "auto")
        assert result.out_path == str(out)
        assert result.events >= 1
        assert result.processes == 1
        assert result.where == "local"
        assert out.is_file()
        lines = [json.loads(line) for line in out.read_text().splitlines() if line.strip()]
        assert lines  # at least one captured call record

    def test_cancel_event_terminates_a_long_running_command(self, tmp_path):
        script = _write_script(tmp_path, (
            "import json, time\n"
            "while True:\n"
            "    json.loads('{}')\n"
            "    time.sleep(0.05)\n"
        ))
        out = tmp_path / "trace.jsonl"
        cancel = threading.Event()
        holder = {}

        def _run():
            holder["result"] = service.capture_trace(
                "json", [sys.executable, str(script)], out, "auto", cancel_event=cancel,
            )

        t = threading.Thread(target=_run)
        t.start()
        time.sleep(0.5)
        cancel.set()
        t.join(timeout=10)

        assert not t.is_alive()
        assert holder["result"].events >= 1

    def test_uncancelled_command_that_never_exits_times_out(self, tmp_path, monkeypatch):
        """No cancel_event (the MCP server's contract_capture path) + a command that
        never exits used to hang forever — _CAPTURE_TIMEOUT bounds it instead."""
        monkeypatch.setattr(service, "_CAPTURE_TIMEOUT", 0.2)
        script = _write_script(tmp_path, "import time\ntime.sleep(30)\n")
        out = tmp_path / "trace.jsonl"
        start = time.monotonic()
        try:
            service.capture_trace("json", [sys.executable, str(script)], out, "auto")
        except ValueError as exc:
            assert "timed out" in str(exc)
        else:
            raise AssertionError("expected ValueError")
        assert time.monotonic() - start < 10  # bounded, not left hanging

    def test_container_capture_returns_child_exit_code(self, tmp_path, monkeypatch):
        def fake_docker(args, check=True):
            returncode = 7 if args[:1] == ["exec"] and "pytest" in args else 0
            return subprocess.CompletedProcess(args, returncode, "", "")

        monkeypatch.setattr(service, "_docker", fake_docker)
        monkeypatch.setattr(service.glob, "glob", lambda *_a, **_kw: ["trace-1.jsonl"])

        produced, returncode = service.capture_trace_in_container(
            "flask", ["pytest"], "auto", tmp_path / "bundle", tmp_path / "work",
            "container-id", None, "", "", False,
        )

        assert produced == ["trace-1.jsonl"]
        assert returncode == 7


class TestCaptureNamedTrace:
    def test_writes_slot_and_persists_state(self, tmp_path):
        script = _write_script(tmp_path, "import json\njson.loads('{}')\n")
        slot = service.capture_named_trace(
            tmp_path, "baseline", CaptureMode.TEST_SUITE,
            command=[sys.executable, str(script)], target="json",
        )
        assert slot.mode == CaptureMode.TEST_SUITE
        assert slot.command == [sys.executable, str(script)]
        assert slot.events >= 1
        assert slot.captured_at  # non-empty ISO timestamp
        assert slot.privacy_profile == "values"

        state = service.load_contract_state(tmp_path)
        assert state.baseline == slot
        assert state.post_migration is None

    def test_live_capture_defaults_to_shape_privacy(self, tmp_path):
        script = _write_script(tmp_path, "import json\njson.loads('secret')\n")

        slot = service.capture_named_trace(
            tmp_path, "baseline", CaptureMode.LIVE_COMMAND,
            command=[sys.executable, str(script)], target="json",
        )

        assert slot.privacy_profile == "shape"
        assert "secret" not in Path(slot.trace_path).read_text()

    def test_failed_command_is_diagnostic_and_not_active_evidence(self, tmp_path):
        script = _write_script(
            tmp_path, "import json\njson.loads('{}')\nraise SystemExit(7)\n"
        )

        slot = service.capture_named_trace(
            tmp_path, "baseline", CaptureMode.LIVE_COMMAND,
            command=[sys.executable, str(script)], target="json",
        )

        state = service.load_contract_state(tmp_path)
        assert slot.validity is CaptureValidity.COMMAND_FAILED
        assert state.baseline is None
        assert state.diagnostic_captures == [slot]
        assert "diagnostics" in slot.trace_path

    def test_invalid_recapture_preserves_the_active_baseline(self, tmp_path):
        good = _write_script(tmp_path, "import json\njson.loads('{}')\n", "good.py")
        active = service.capture_named_trace(
            tmp_path, "baseline", CaptureMode.LIVE_COMMAND,
            command=[sys.executable, str(good)], target="json",
        )
        bad = _write_script(
            tmp_path, "import json\njson.loads('{}')\nraise SystemExit(9)\n", "bad.py"
        )

        diagnostic = service.capture_named_trace(
            tmp_path, "baseline", CaptureMode.LIVE_COMMAND,
            command=[sys.executable, str(bad)], target="json",
        )

        state = service.load_contract_state(tmp_path)
        assert state.baseline == active
        assert diagnostic.validity is CaptureValidity.COMMAND_FAILED
        assert state.diagnostic_captures[-1] == diagnostic
        assert Path(active.trace_path).is_file()

    def test_recapture_overwrites_in_place_no_history(self, tmp_path):
        script = _write_script(tmp_path, "import json\njson.loads('{}')\n")
        first = service.capture_named_trace(
            tmp_path, "baseline", CaptureMode.TEST_SUITE,
            command=[sys.executable, str(script)], target="json",
        )
        second = service.capture_named_trace(
            tmp_path, "baseline", CaptureMode.LIVE_COMMAND,
            command=[sys.executable, str(script)], target="json",
        )
        state = service.load_contract_state(tmp_path)
        assert state.baseline == second
        assert state.baseline != first
        assert state.baseline.mode == CaptureMode.LIVE_COMMAND

    def test_rejects_unknown_when(self, tmp_path):
        try:
            service.capture_named_trace(
                tmp_path, "sideways", CaptureMode.TEST_SUITE, command=["true"],
            )
        except ValueError as exc:
            assert "when" in str(exc)
        else:
            raise AssertionError("expected ValueError")

    def test_rejects_missing_command(self, tmp_path):
        try:
            service.capture_named_trace(tmp_path, "baseline", CaptureMode.TEST_SUITE, command=None)
        except ValueError as exc:
            assert "command" in str(exc)
        else:
            raise AssertionError("expected ValueError")

    def test_rejects_live_attach(self, tmp_path):
        try:
            service.capture_named_trace(
                tmp_path, "baseline", CaptureMode.LIVE_ATTACH, command=["true"],
            )
        except ValueError as exc:
            assert "LIVE_ATTACH" in str(exc) or "start_attached_capture" in str(exc)
        else:
            raise AssertionError("expected ValueError")


class TestCoverageGapSummary:
    """capture_named_trace's TEST_SUITE-only coverage-based gap summary. All real
    `coverage run` invocations are avoided — `_build_coverage_data` is monkeypatched to
    inject a fake report, mirroring how coverage.py's build_coverage_map takes an
    injectable `runner`."""

    def _write_dependency_usage(self, tmp_path):
        pkg = tmp_path / "pkgx"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("import flask\n", encoding="utf-8")

    def test_test_suite_capture_populates_coverage_summary(self, tmp_path, monkeypatch):
        self._write_dependency_usage(tmp_path)
        script = _write_script(tmp_path, "import json\njson.loads('{}')\n")
        monkeypatch.setattr(service, "_coverage_available", lambda: True)
        monkeypatch.setattr(
            service, "_build_coverage_data",
            lambda project_dir, pytest_args: {"files": {"pkgx/__init__.py": {
                "executed_lines": [1],
            }}},
        )

        slot = service.capture_named_trace(
            tmp_path, "baseline", CaptureMode.TEST_SUITE,
            command=[sys.executable, str(script)], target="json",
        )

        assert slot.coverage_pct == 100.0
        assert slot.covered_sites == 1
        assert slot.blind_sites == 0

    def test_live_command_capture_never_computes_coverage(self, tmp_path, monkeypatch):
        """Coverage summarization is TEST_SUITE-only — LIVE_COMMAND must not even attempt it."""
        script = _write_script(tmp_path, "import json\njson.loads('{}')\n")

        def _boom(*a, **kw):
            raise AssertionError("_coverage_gap_summary should not be called for LIVE_COMMAND")

        monkeypatch.setattr(service, "_coverage_gap_summary", _boom)

        slot = service.capture_named_trace(
            tmp_path, "baseline", CaptureMode.LIVE_COMMAND,
            command=[sys.executable, str(script)], target="json",
        )
        assert slot.coverage_pct is None
        assert slot.covered_sites == 0
        assert slot.blind_sites == 0

    def test_missing_coverage_package_degrades_without_raising(self, tmp_path, monkeypatch):
        script = _write_script(tmp_path, "import json\njson.loads('{}')\n")
        monkeypatch.setattr(service, "_coverage_available", lambda: False)

        slot = service.capture_named_trace(
            tmp_path, "baseline", CaptureMode.TEST_SUITE,
            command=[sys.executable, str(script)], target="json",
        )

        assert slot.coverage_pct is None
        assert slot.covered_sites == 0
        assert slot.blind_sites == 0

    def test_gap_summary_helper_degrades_on_unexpected_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(service, "_coverage_available", lambda: True)

        def _raise(*a, **kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(service, "_build_coverage_data", _raise)

        pct, covered, blind, notes = service._coverage_gap_summary(
            tmp_path, [sys.executable, "-m", "pytest"],
        )

        assert pct is None
        assert covered == 0
        assert blind == 0
        assert notes and "boom" in notes[0]


class TestAttachedCapture:
    def test_start_returns_hint_and_deterministic_path(self, tmp_path):
        instr = service.start_attached_capture(tmp_path, "post_migration", target="requests")
        assert "PYMOLT_TRACE_TARGET=requests" in instr.command_hint
        expected = service.pending_attach_path(tmp_path, "post_migration")
        assert instr.out_path == str(expected)

    def test_poll_counts_lines_zero_when_absent(self, tmp_path):
        missing = tmp_path / "nope.jsonl"
        assert service.poll_attached_capture(missing) == 0

    def test_poll_and_finalize_round_trip(self, tmp_path):
        instr = service.start_attached_capture(tmp_path, "baseline", target="requests")
        with open(instr.out_path, "w") as f:
            f.write('{"q": "requests.get"}\n')
            f.write('{"q": "requests.post"}\n')

        assert service.poll_attached_capture(instr.out_path) == 2
        slot = service.finalize_attached_capture(
            tmp_path, "baseline", instr.out_path, target="requests"
        )
        assert slot.mode == CaptureMode.LIVE_ATTACH
        assert slot.events == 2
        assert slot.command == []

        state = service.load_contract_state(tmp_path)
        assert state.baseline == slot
        assert slot.validity is CaptureValidity.VALID


class TestContractStateRoundTrip:
    def test_load_missing_file_returns_none(self, tmp_path):
        assert ContractState.load(tmp_path / "contract_state.json") is None

    def test_save_then_load_round_trips(self, tmp_path):
        from pymolt.verify.models import ContractSlot

        path = tmp_path / ".pymolt" / "contract_state.json"
        state = ContractState(baseline=ContractSlot(
            trace_path="/tmp/x.jsonl", captured_at="2026-07-03T00:00:00+00:00",
            mode=CaptureMode.TEST_SUITE, command=["pytest"], events=3, processes=1,
        ))
        state.save(path)
        loaded = ContractState.load(path)
        assert loaded == state

    def test_load_corrupt_file_returns_none(self, tmp_path):
        path = tmp_path / ".pymolt" / "contract_state.json"
        path.parent.mkdir(parents=True)
        path.write_text("not json{{{")
        assert ContractState.load(path) is None

    def test_load_contract_state_defaults_to_empty(self, tmp_path):
        state = service.load_contract_state(tmp_path)
        assert state == ContractState()


class TestBuildContractReportFromState:
    def _app_with_dependency(self, tmp_path):
        return _write_script(tmp_path, (
            "import requests\n"
            "def fetch():\n"
            "    try:\n"
            "        requests.get('http://example.invalid', timeout=0.01)\n"
            "    except Exception:\n"
            "        pass\n"
            "fetch()\n"
        ))

    def _trace_file(self, tmp_path, name, qualnames):
        p = tmp_path / name
        with p.open("w") as f:
            for q in qualnames:
                f.write(json.dumps({"q": q}) + "\n")
        return str(p)

    def test_no_captures_falls_back_to_static_only(self, tmp_path):
        self._app_with_dependency(tmp_path)
        rep = service.build_contract_report_from_state(tmp_path)
        assert rep.static_targets >= 1
        assert rep.confirmed == 0
        assert any("no trace given" in n for n in rep.notes)

    def test_baseline_only_used_as_trace(self, tmp_path):
        self._app_with_dependency(tmp_path)
        service.capture_named_trace(
            tmp_path, "baseline", CaptureMode.TEST_SUITE,
            command=[sys.executable, str(tmp_path / "app.py")], target="requests",
        )
        rep = service.build_contract_report_from_state(tmp_path)
        assert rep.diff is None  # only one side captured -> no version diff

    def test_both_captures_diff_post_migration_against_baseline(self, tmp_path):
        self._app_with_dependency(tmp_path)
        baseline = self._trace_file(tmp_path, "baseline.jsonl", ["requests.api.get"])
        post = self._trace_file(tmp_path, "post.jsonl", ["requests.api.get", "requests.api.post"])
        state = ContractState(
            baseline={"trace_path": baseline, "captured_at": "t1", "mode": "test_suite"},
            post_migration={"trace_path": post, "captured_at": "t2", "mode": "test_suite"},
        )
        state.save(tmp_path / ".pymolt" / "contract_state.json")

        rep = service.build_contract_report_from_state(tmp_path)
        assert rep.diff is not None  # both slots present -> version diff attached
        assert rep.observed_targets >= 2

    def test_explicit_override_wins_over_state(self, tmp_path):
        self._app_with_dependency(tmp_path)
        service.capture_named_trace(
            tmp_path, "baseline", CaptureMode.TEST_SUITE,
            command=[sys.executable, str(tmp_path / "app.py")], target="requests",
        )
        empty = tmp_path / "empty.jsonl"
        empty.write_text("")
        rep = service.build_contract_report_from_state(tmp_path, trace_override=str(empty))
        assert rep.confirmed == 0  # explicit (empty) trace used, not the captured baseline
