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

import pytest

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
        """No cancel_event (the MCP server's capture path) + a command that
        never exits used to hang forever — _CAPTURE_TIMEOUT bounds it instead."""
        monkeypatch.setattr(service, "_CAPTURE_TIMEOUT", 0.2)
        # A timed-out capture deliberately keeps raw traces it could not merge; keep that
        # work dir inside tmp_path instead of leaking it into the real $TMPDIR.
        monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
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

    def test_container_capture_keeps_the_command_output(self, tmp_path, monkeypatch):
        """A run in a container leaves its output next to the trace, as a local run does:
        without it a failed run (a replicate, say) shows nothing of which test failed."""
        def fake_docker(args, check=True):
            if args[:1] == ["exec"] and "pytest" in args:
                return subprocess.CompletedProcess(
                    args, 1, "FAILED tests/test_a.py::test_x - assert 0\n=== 1 failed ===",
                    "DeprecationWarning: old\n")
            return subprocess.CompletedProcess(args, 0, "", "")

        monkeypatch.setattr(service, "_docker", fake_docker)

        result = service.capture_trace(
            "json", ["pytest"], tmp_path / "trace.jsonl", "auto", container="legacy")

        assert result.returncode == 1
        assert result.command_log == str(tmp_path / "trace.command.log")
        assert Path(result.command_log).read_text() == (
            "FAILED tests/test_a.py::test_x - assert 0\n=== 1 failed ===\n"
            "DeprecationWarning: old\n")
        assert service.failed_tests(result.command_log) == (["tests/test_a.py::test_x"], 1)


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

    def test_a_failed_container_run_keeps_its_log_with_the_diagnostic(self, tmp_path,
                                                                        monkeypatch):
        def fake_docker(args, check=True):
            if args[:1] == ["exec"] and "pytest" in args:
                return subprocess.CompletedProcess(args, 1, "=== 2 failed ===\n", "")
            return subprocess.CompletedProcess(args, 0, "", "")

        monkeypatch.setattr(service, "_docker", fake_docker)
        monkeypatch.setattr(service, "_container_runner_spec", lambda container: None)

        slot = service.capture_named_trace(
            tmp_path, "baseline", CaptureMode.TEST_SUITE, command=["pytest"], target="json",
            container="legacy",
        )

        assert slot.validity is CaptureValidity.COMMAND_FAILED
        assert slot.command_log == str(Path(slot.trace_path).with_suffix(".command.log"))
        assert Path(slot.command_log).read_text() == "=== 2 failed ===\n"
        assert not list((tmp_path / ".pymolt" / "contract_traces").glob(".*pending*"))

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

    def test_accept_failing_keeps_a_red_run_that_recorded_events(self, tmp_path):
        script = _write_script(
            tmp_path,
            "import json\njson.loads('{}')\nprint('=== 2 failed, 5 passed in 0.1s ===')\n"
            "raise SystemExit(1)\n",
        )
        refused = service.capture_named_trace(
            tmp_path, "baseline", CaptureMode.LIVE_COMMAND,
            command=[sys.executable, str(script)], target="json",
        )
        assert refused.validity is CaptureValidity.COMMAND_FAILED and refused.red is None

        slot = service.capture_named_trace(
            tmp_path, "baseline", CaptureMode.LIVE_COMMAND,
            command=[sys.executable, str(script)], target="json", accept_failing=True,
        )
        assert slot.validity is CaptureValidity.VALID
        assert slot.red == {"returncode": 1, "failed": 2, "summary": "2 failed, 5 passed in 0.1s"}
        assert service.load_contract_state(tmp_path).baseline == slot

    def test_accept_failing_still_refuses_a_run_with_no_events(self, tmp_path):
        script = _write_script(tmp_path, "raise SystemExit(3)\n")
        slot = service.capture_named_trace(
            tmp_path, "baseline", CaptureMode.LIVE_COMMAND,
            command=[sys.executable, str(script)], target="json", accept_failing=True,
        )
        assert slot.validity is CaptureValidity.COMMAND_FAILED and slot.red is None
        assert service.load_contract_state(tmp_path).baseline is None

    def test_slot_paths_are_stored_relative_and_survive_a_changed_cwd(
            self, tmp_path, monkeypatch):
        script = _write_script(tmp_path, "import json\njson.loads('{}')\n")
        monkeypatch.chdir(tmp_path.parent)
        slot = service.capture_named_trace(
            Path(tmp_path.name), "baseline", CaptureMode.TEST_SUITE,
            command=[sys.executable, str(script)], target="json",
        )
        assert Path(slot.trace_path).is_absolute()
        raw = json.loads((tmp_path / ".pymolt" / "contract_state.json").read_text())
        assert raw["baseline"]["trace_path"] == ".pymolt/contract_traces/baseline.jsonl"

        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        loaded = service.load_contract_state(tmp_path).baseline
        assert Path(loaded.trace_path).is_file()

    def test_legacy_cwd_relative_path_is_accepted_with_a_warning(
            self, tmp_path, monkeypatch, caplog):
        script = _write_script(tmp_path, "import json\njson.loads('{}')\n")
        service.capture_named_trace(
            tmp_path, "baseline", CaptureMode.TEST_SUITE,
            command=[sys.executable, str(script)], target="json",
        )
        state_file = tmp_path / ".pymolt" / "contract_state.json"
        raw = json.loads(state_file.read_text())
        raw["baseline"]["trace_path"] = "proj/.pymolt/contract_traces/baseline.jsonl"
        state_file.write_text(json.dumps(raw))
        (tmp_path.parent / "proj").symlink_to(tmp_path)
        monkeypatch.chdir(tmp_path.parent)
        with caplog.at_level("WARNING"):
            loaded = service.load_contract_state(tmp_path).baseline
        assert Path(loaded.trace_path).is_file()
        assert "relative to the current directory" in caplog.text

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

    def test_recapture_replaces_the_active_slot(self, tmp_path):
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


class TestTestSuiteCaptureRunsOnce:
    """A test-suite capture runs the suite once: no second run under ``coverage``, and nothing
    written into the project (docs/verify.md: nothing is added to your project)."""

    def test_runs_the_command_once_and_adds_nothing_with_coverage_importable(
            self, tmp_path, monkeypatch):
        import importlib.util
        import os

        if importlib.util.find_spec("coverage") is None:     # a stand-in when not installed
            site = tmp_path / "site"
            (site / "coverage").mkdir(parents=True)
            (site / "coverage" / "__init__.py").write_text("")
            monkeypatch.syspath_prepend(str(site))
        assert importlib.util.find_spec("coverage") is not None
        runs = tmp_path / "runs.log"
        # A ``coverage`` on PATH that logs each call and writes what coverage.py would.
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        shim = bin_dir / "coverage"
        shim.write_text(f"#!/bin/sh\necho \"coverage $*\" >> '{runs}'\n: > .coverage\n"
                        "echo '{\"files\": {}}' > coverage.json\n")
        shim.chmod(0o755)
        monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
        project = tmp_path / "proj"
        (project / "pkgx").mkdir(parents=True)
        (project / "pkgx" / "__init__.py").write_text("import json\n")
        suite = _write_script(tmp_path, (
            "import json\n"
            "json.loads('{}')\n"
            f"open({str(runs)!r}, 'a').write('suite\\n')\n"), name="suite.py")

        slot = service.capture_named_trace(
            project, "baseline", CaptureMode.TEST_SUITE,
            command=[sys.executable, str(suite)], target="json",
        )

        assert slot.validity is CaptureValidity.VALID
        assert runs.read_text().splitlines() == ["suite"]
        assert not (project / ".coverage").exists()
        assert not (project / "coverage.json").exists()

    def test_a_state_file_with_the_old_coverage_fields_still_loads(self, tmp_path):
        """``coverage_pct``/``covered_sites``/``blind_sites`` were slot fields of schema 2."""
        state_file = tmp_path / ".pymolt" / "contract_state.json"
        state_file.parent.mkdir()
        trace = tmp_path / ".pymolt" / "contract_traces" / "baseline.jsonl"
        state_file.write_text(json.dumps({"schema_version": 2, "baseline": {
            "trace_path": str(trace), "captured_at": "2026-11-18T10:00:00+00:00",
            "mode": "test_suite", "command": ["pytest"], "events": 3,
            "coverage_pct": 87.5, "covered_sites": 7, "blind_sites": 1}}))

        state = service.load_contract_state(tmp_path)

        assert state.baseline is not None and state.baseline.events == 3
        assert "coverage_pct" not in state.baseline.model_dump()
        state.save(state_file)
        assert "coverage_pct" not in state_file.read_text()


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

    def test_load_corrupt_file_raises(self, tmp_path):
        from pymolt.state_io import StateFileError

        path = tmp_path / ".pymolt" / "contract_state.json"
        path.parent.mkdir(parents=True)
        path.write_text("not json{{{")
        with pytest.raises(StateFileError, match="capture state"):
            ContractState.load(path)
        with pytest.raises(StateFileError):            # never read as an empty state
            service.load_contract_state(tmp_path)

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
                f.write(json.dumps({"q": q, "t": "return", "in": {"bound": {}},
                                    "result": None}) + "\n")
        return str(p)

    def test_no_captures_falls_back_to_static_only(self, tmp_path):
        self._app_with_dependency(tmp_path)
        rep = service.build_contract_report_from_state(tmp_path)
        assert rep.static_targets == 1  # requests.get
        assert rep.confirmed == 0
        assert any("no trace given" in n for n in rep.notes)

    def test_baseline_only_used_as_trace(self, tmp_path):
        self._app_with_dependency(tmp_path)
        service.capture_named_trace(
            tmp_path, "baseline", CaptureMode.TEST_SUITE,
            command=[sys.executable, str(tmp_path / "app.py")], target="requests",
        )
        rep = service.build_contract_report_from_state(tmp_path)
        assert rep.confirmed == 1  # the baseline's requests.get call is the trace
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
        # post-migration against the baseline: the extra post call appeared (not disappeared)
        assert (rep.diff["appeared"], rep.diff["disappeared"]) == (1, 0)
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


# ── facts sink (phase 2): defaults, host merge, sidecars ─────────────────────────────────

from pymolt.verify._sinks import FactSink  # noqa: E402
from pymolt.verify.facts import iter_snapshot, load_facts, snapshot_path  # noqa: E402

_SITE = {"file": "/proj/app.py", "line": 3, "func": "main"}


def _fake_process(work, pid, calls, backend="monitoring"):
    """What one traced process with PYMOLT_TRACE_SINK=facts leaves in the work dir."""
    sink = FactSink(str(work / f"trace-{pid}.facts.json"), flush_seconds=0,
                    metadata={"target": "m", "python": "3.12.1"})
    sink.set_metadata(backend=backend, mode="auto")
    for sym, result in calls:
        sink.on_return(sym, {"bound": {}}, result, site=_SITE, sym=sym, dur=0.001)
    sink.close()


def _fake_local(processes):
    def fake(target, command, backend, bundle, work, *args, **kwargs):
        for pid, calls in processes.items():
            _fake_process(work, pid, calls)
        return service._process_traces(work), 0
    return fake


class TestFactsCapture:
    def test_trace_env_defaults_and_caller_overrides(self, monkeypatch):
        monkeypatch.delenv("PYMOLT_TRACE_SINK", raising=False)
        monkeypatch.delenv("PYMOLT_TRACE_MODE", raising=False)
        env = service.trace_env("m", "auto", "", "", False, sink="facts", trace_mode="auto")
        assert (env["PYMOLT_TRACE_SINK"], env["PYMOLT_TRACE_MODE"]) == ("facts", "auto")
        assert "PYMOLT_TRACE_SINK" not in service.trace_env("m", "auto", "", "", False)
        # Impact targets without an explicit mode keep the targeted capture.
        env = service.trace_env("m", "auto", "", "", False, impact_targets=["m.f"],
                                sink="facts", trace_mode="auto")
        assert "PYMOLT_TRACE_MODE" not in env and env["PYMOLT_TRACE_SINK"] == "facts"
        monkeypatch.setenv("PYMOLT_TRACE_SINK", "jsonl")
        monkeypatch.setenv("PYMOLT_TRACE_MODE", "watch")
        monkeypatch.setenv("PYMOLT_TRACE_MAX_FACTS", "1000")
        env = service.trace_env("m", "auto", "", "", False, sink="facts", trace_mode="auto")
        assert (env["PYMOLT_TRACE_SINK"], env["PYMOLT_TRACE_MODE"]) == ("jsonl", "watch")
        assert env["PYMOLT_TRACE_MAX_FACTS"] == "1000"  # forwarded into a container too

    def test_capture_modes_pick_their_sink(self, tmp_path, monkeypatch):
        calls = []

        def fake_capture(target, command, out_path, backend, **kwargs):
            calls.append(kwargs)
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            Path(out_path).write_text('{"q": "m.f"}\n')
            return service.TraceCaptureResult(out_path=str(out_path), events=1, processes=1,
                                              where="local")

        monkeypatch.setattr(service, "capture_trace", fake_capture)
        service.capture_named_trace(tmp_path, "baseline", CaptureMode.TEST_SUITE,
                                    command=["pytest"])
        service.capture_named_trace(tmp_path, "post_migration", CaptureMode.LIVE_COMMAND,
                                    command=["python", "app.py"])
        assert "sink" not in calls[0] and "trace_mode" not in calls[0]  # jsonl + discover
        assert (calls[1]["sink"], calls[1]["trace_mode"]) == ("facts", "auto")

    def test_widened_impact_targets_keep_the_default_mode(self, monkeypatch):
        monkeypatch.delenv("PYMOLT_TRACE_MODE", raising=False)
        env = service.trace_env("m", "auto", "", "", False, impact_targets=["m.f"],
                                trace_mode="discover", widen_targets=True)
        assert env["PYMOLT_TRACE_MODE"] == "discover"
        assert json.loads(env["PYMOLT_TRACE_IMPACT_TARGETS"]) == ["m.f"]

    def test_post_migration_arms_the_applied_plan_on_top_of_the_usual_capture(
            self, tmp_path, monkeypatch):
        # A capture narrowed to the plan's symbols reported every other baseline call as
        # DISAPPEARED: the plan's targets widen the capture, never replace it.
        calls = []

        def fake_capture(target, command, out_path, backend, **kwargs):
            calls.append(kwargs)
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            Path(out_path).write_text('{"q": "m.f"}\n')
            return service.TraceCaptureResult(out_path=str(out_path), events=1, processes=1,
                                              where="local")

        monkeypatch.delenv("PYMOLT_TRACE_IMPACT_TARGETS", raising=False)
        monkeypatch.setattr(service, "capture_trace", fake_capture)
        monkeypatch.setattr(service, "_applied_plan_impact_targets",
                            lambda _root: ["pandas.concat"])
        service.capture_named_trace(tmp_path, "post_migration", CaptureMode.TEST_SUITE,
                                    command=["pytest"])
        service.capture_named_trace(tmp_path, "post_migration", CaptureMode.TEST_SUITE,
                                    command=["pytest"], impact_targets=["m.f"])
        assert calls[0]["impact_targets"] == ["pandas.concat"]
        assert calls[0]["widen_targets"] is True
        # Explicit targets keep meaning a targeted capture.
        assert calls[1]["impact_targets"] == ["m.f"] and "widen_targets" not in calls[1]

    def test_local_test_capture_defaults_to_jsonl_discover(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PYMOLT_TRACE_SINK", raising=False)
        monkeypatch.delenv("PYMOLT_TRACE_MODE", raising=False)
        script = _write_script(tmp_path, (
            "import json, os\n"
            "json.loads('{}')\n"
            "print(os.environ.get('PYMOLT_TRACE_SINK'), os.environ.get('PYMOLT_TRACE_MODE'))\n"
        ))
        out = tmp_path / "trace.jsonl"
        result = service.capture_trace("json", [sys.executable, str(script)], out, "auto")
        assert "jsonl discover" in Path(result.command_log).read_text()
        assert result.facts_path is None and not snapshot_path(out).exists()

    def test_capture_merges_process_snapshots_before_the_work_dir_goes(self, tmp_path,
                                                                        monkeypatch):
        monkeypatch.setattr(service, "capture_trace_local", _fake_local({
            1001: [("m.f", 1)] * 3 + [("m.g", "x")],
            1002: [("m.f", 1), ("m.f", 2)],
        }))
        out = tmp_path / "trace.jsonl"
        result = service.capture_trace("m", ["app"], out, "auto", sink="facts",
                                       trace_mode="auto")
        assert result.processes == 2
        assert result.events == 6 and result.events_seen == 6 and result.dropped_events == 0
        assert result.backend == "monitoring"
        merged = snapshot_path(out)
        assert result.facts_path == str(merged) and merged.is_file()
        header = next(obj for kind, obj in iter_snapshot(merged) if kind == "header")
        assert header["merged"] is True and header["counters"]["calls"] == 6
        meta = json.loads(Path(result.metadata_path).read_text())
        assert meta["facts_path"] == str(merged)
        assert meta["facts"]["processes"] == 2 and meta["facts"]["evicted"] == 0
        assert meta["runtime_metadata_complete"] is True
        # The JSONL holds the projection: every JSONL reader sees the calls.
        projected = [json.loads(line) for line in out.read_text().splitlines()]
        assert {(r["sym"], r["result"], r["count"]) for r in projected} == {
            ("m.f", 1, 4), ("m.f", 2, 1), ("m.g", "x", 1)}
        assert service.inspect_trace_artifact(out).validity is CaptureValidity.VALID
        fs = load_facts(out)
        assert fs.snapshot and fs.calls == 6
        assert not list(tmp_path.parent.glob("pymolt-trace-*/trace-*.facts.json"))

    def test_named_capture_promotes_and_archives_the_snapshot(self, tmp_path, monkeypatch):
        monkeypatch.setattr(service, "capture_trace_local",
                            _fake_local({7: [("m.f", 1), ("m.f", 1)]}))
        first = service.capture_named_trace(tmp_path, "baseline", CaptureMode.LIVE_COMMAND,
                                            command=["python", "app.py"])
        traces = tmp_path / ".pymolt" / "contract_traces"
        assert first.validity is CaptureValidity.VALID and first.events == 2
        final = snapshot_path(traces / "baseline.jsonl")
        assert final.is_file()
        meta = json.loads(Path(first.metadata_path).read_text())
        assert meta["facts_path"] == str(final)  # retargeted from the staged name
        assert not list(traces.glob(".*pending*"))
        second = service.capture_named_trace(tmp_path, "baseline", CaptureMode.LIVE_COMMAND,
                                             command=["python", "app.py"])
        archived = Path(second.archived_previous)
        assert snapshot_path(archived).is_file() and final.is_file()

    def test_snapshot_only_trace_is_valid_and_an_empty_one_is_not(self, tmp_path):
        trace = tmp_path / "t.jsonl"
        trace.write_text("")
        sink = FactSink(str(trace), flush_seconds=0)
        sink.close()
        assert service.inspect_trace_artifact(trace).validity is CaptureValidity.EMPTY
        sink = FactSink(str(trace), flush_seconds=0)
        sink.on_return("m.f", {"bound": {}}, 1, site=_SITE, sym="m.f")
        sink.close()
        quality = service.inspect_trace_artifact(trace)
        assert quality.validity is CaptureValidity.VALID and quality.events == 1

    def test_attach_with_the_facts_sink_round_trips(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PYMOLT_TRACE_SINK", raising=False)
        monkeypatch.delenv("PYMOLT_TRACE_MODE", raising=False)
        instr = service.start_attached_capture(tmp_path, "baseline", target="m")
        assert "PYMOLT_TRACE_SINK=facts" in instr.command_hint
        assert "PYMOLT_TRACE_MODE=auto" in instr.command_hint
        pending = Path(instr.out_path)
        assert pending.is_file()  # what --collect looks for; the sink writes no JSONL
        # The engineer's process: FactSink on PYMOLT_TRACE_OUT (no {pid} in an attach path).
        sink = FactSink(str(pending), flush_seconds=0, metadata={"target": "m"})
        for _ in range(5):
            sink.on_return("m.f", {"bound": {}}, 1, site=_SITE, sym="m.f")
        sink.close()
        assert service.poll_attached_capture(pending) == 5
        slot = service.finalize_attached_capture(tmp_path, "baseline", pending, target="m")
        assert slot.validity is CaptureValidity.VALID
        assert slot.events == 5 and slot.events_seen == 5 and slot.dropped_events == 0
        final = Path(slot.trace_path)
        assert snapshot_path(final).is_file()
        assert not list(final.parent.glob(".baseline.attach-pending*"))
        fs = load_facts(final)
        assert fs.snapshot and [f.count for f in fs.facts] == [5]


def test_attach_collects_forked_worker_snapshots(tmp_path, monkeypatch):
    monkeypatch.delenv("PYMOLT_TRACE_SINK", raising=False)
    instr = service.start_attached_capture(tmp_path, "baseline", target="m")
    pending = Path(instr.out_path)
    stem = pending.name[:-len(".jsonl")]
    for name, n in ((f"{stem}.facts.json", 2), (f"{stem}.4242.facts.json", 3)):
        sink = FactSink(str(pending.with_name(name)), flush_seconds=0)
        for _ in range(n):
            sink.on_return("m.f", {"bound": {}}, 1, site=_SITE, sym="m.f")
        sink.close()
    slot = service.finalize_attached_capture(tmp_path, "baseline", pending, target="m")
    assert slot.validity is CaptureValidity.VALID
    assert [f.count for f in load_facts(slot.trace_path).facts] == [5]
    assert not list(pending.parent.glob(f"{stem}*.facts.json"))


class TestLongRunSampling:
    def _stub(self, monkeypatch):
        calls = []

        def fake_capture(target, command, out_path, backend, **kwargs):
            calls.append(kwargs)
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            Path(out_path).write_text('{"q": "m.f"}\n')
            service._write_trace_metadata(out_path, {"sample_rate": kwargs["sample_rate"]})
            return service.TraceCaptureResult(out_path=str(out_path), events=1, processes=1,
                                              where="local")

        monkeypatch.setattr(service, "capture_trace", fake_capture)
        return calls

    def test_long_runs_default_to_novelty_first_sampling(self, tmp_path, monkeypatch):
        calls = self._stub(monkeypatch)
        tests = service.capture_named_trace(tmp_path, "baseline", CaptureMode.TEST_SUITE,
                                            command=["pytest"])
        run = service.capture_named_trace(tmp_path, "post_migration", CaptureMode.LIVE_COMMAND,
                                          command=["python", "app.py"])
        assert (calls[0]["sample_rate"], tests.sample_rate) == (1.0, 1.0)
        assert "sample_first" not in calls[0]
        assert (calls[1]["sample_rate"], calls[1]["sample_first"]) == (0.1, 32)
        assert run.sample_rate == 0.1
        meta = json.loads(Path(run.metadata_path).read_text())
        assert meta["sampling"] == {"rate": 0.1, "first": 32, "source": "default"}

    def test_an_explicit_rate_is_the_callers(self, tmp_path, monkeypatch):
        calls = self._stub(monkeypatch)
        slot = service.capture_named_trace(tmp_path, "baseline", CaptureMode.LIVE_COMMAND,
                                           command=["python", "app.py"], sample_rate=1.0)
        assert calls[0]["sample_rate"] == 1.0 and "sample_first" not in calls[0]
        meta = json.loads(Path(slot.metadata_path).read_text())
        assert meta["sampling"] == {"rate": 1.0, "first": 0, "source": "caller"}

    def test_trace_env_sets_the_novelty_count_below_rate_one(self, monkeypatch):
        monkeypatch.delenv("PYMOLT_TRACE_SAMPLE_FIRST", raising=False)
        env = service.trace_env("m", "auto", "", "", False, sample_rate=0.1)
        assert env["PYMOLT_TRACE_SAMPLE_FIRST"] == "32"
        assert "PYMOLT_TRACE_SAMPLE_FIRST" not in service.trace_env("m", "auto", "", "", False)
        monkeypatch.setenv("PYMOLT_TRACE_SAMPLE_FIRST", "8")
        env = service.trace_env("m", "auto", "", "", False, sample_rate=0.1, sample_first=16)
        assert env["PYMOLT_TRACE_SAMPLE_FIRST"] == "8"

    def test_attach_defaults_to_long_run_sampling_and_records_what_ran(self, tmp_path,
                                                                       monkeypatch):
        monkeypatch.delenv("PYMOLT_TRACE_SAMPLE_FIRST", raising=False)
        instr = service.start_attached_capture(tmp_path, "baseline", target="m")
        assert "PYMOLT_TRACE_SAMPLE_RATE=0.1" in instr.command_hint
        assert "PYMOLT_TRACE_SAMPLE_FIRST=32" in instr.command_hint
        pending = Path(instr.out_path)
        monkeypatch.setenv("PYMOLT_TRACE_SAMPLE_RATE", "0.1")
        sink = FactSink(str(pending), flush_seconds=0)
        sink.on_raise("m.f", {"bound": {}}, "E", site=_SITE, sym="m.f")
        sink.close()
        # The CLI's --collect passes its own default; the watcher's sidecar is what ran.
        slot = service.finalize_attached_capture(tmp_path, "baseline", pending, target="m",
                                                 sample_rate=1.0)
        assert slot.sample_rate == 0.1


class TestSampledVerdict:
    def _slot(self, rate):
        from pymolt.verify.models import ContractSlot

        return ContractSlot(trace_path="/x.jsonl", captured_at="2026-11-14T00:00:00+00:00",
                            mode=CaptureMode.LIVE_COMMAND, command=["python", "app.py"],
                            sample_rate=rate, validity=CaptureValidity.VALID, dropped_events=0)

    def _verdict(self, tmp_path, complete):
        from pymolt.verify.report import ContractReport

        trace = tmp_path / "t.jsonl"
        trace.write_text('{"t": "return", "q": "m.f", "result": 1}\n')
        summary = {"tallies_complete": complete}
        report = ContractReport(root=".", diff={"disappeared": 0}, diff_clean=True,
                                static_targets=1, confirmed=1, baseline_stale=False,
                                fact_diff={"verdict": "pass", "counts": {"equal": 1},
                                           "old": summary, "new": dict(summary)})
        slots = [("baseline", self._slot(0.1)), ("post-migration", self._slot(0.1))]
        service._stamp_verdict(report, str(trace), str(trace), slots)
        return report

    def test_complete_tallies_on_both_sides_lift_the_sampling_caveat(self, tmp_path):
        report = self._verdict(tmp_path, complete=True)
        assert not any("sampled captures" in r for r in report.verdict_reasons)

    def test_without_complete_tallies_sampling_still_blocks(self, tmp_path):
        report = self._verdict(tmp_path, complete=False)
        assert any("sampled captures" in r for r in report.verdict_reasons)


class TestMissingSideReason:
    def _reasons(self, tmp_path, trace, used):
        from pymolt.verify.models import ContractSlot
        from pymolt.verify.report import ContractReport

        report = ContractReport(root=".")
        slots = [(name, ContractSlot(
            trace_path="/x.jsonl", captured_at="2026-11-14T00:00:00+00:00",
            mode=CaptureMode.TEST_SUITE, validity=CaptureValidity.VALID, dropped_events=0,
            **({"red": {"returncode": 1, "failed": 3, "summary": "3 failed"}}
               if name == "baseline" else {})))
            for name in used]
        service._stamp_verdict(report, trace, None, slots)
        return report

    def test_only_the_baseline_recorded(self, tmp_path):
        trace = tmp_path / "t.jsonl"
        trace.write_text('{"t": "return", "q": "m.f", "result": 1}\n')
        report = self._reasons(tmp_path, str(trace), ["baseline"])
        assert ("no post-migration capture yet (only the baseline is recorded)"
                in report.verdict_reasons)
        assert not any("no baseline" in r for r in report.verdict_reasons)
        assert any(n.startswith("RED:") and "3 failing" in n for n in report.notes)

    def test_only_the_post_migration_capture(self, tmp_path):
        trace = tmp_path / "t.jsonl"
        trace.write_text('{"t": "return", "q": "m.f", "result": 1}\n')
        report = self._reasons(tmp_path, str(trace), ["post-migration"])
        assert "no baseline capture" in report.verdict_reasons

    def test_no_captures(self, tmp_path):
        report = self._reasons(tmp_path, None, [])
        assert "no captures" in report.verdict_reasons


class TestStdlibTracing:
    """PYMOLT_TRACE_STDLIB: stdlib modules traced as dependencies during a Python move."""

    def _project(self, tmp_path, base="3.8", target="3.12"):
        (tmp_path / ".pymolt").mkdir()
        (tmp_path / ".pymolt" / "env_config.json").write_text(json.dumps(
            {"base_python": base, "target_python": target}))
        (tmp_path / "app.py").write_text(
            "import click\nimport imp\nfrom collections import Iterable\n\n"
            "def size():\n    return click.get_terminal_size()\n")
        return tmp_path

    def test_trace_env_sets_the_variable_and_the_user_wins(self, monkeypatch):
        monkeypatch.delenv("PYMOLT_TRACE_STDLIB", raising=False)
        env = service.trace_env("m", "auto", "", "", False, trace_stdlib=["collections", "imp"])
        assert env["PYMOLT_TRACE_STDLIB"] == "collections,imp"
        assert "PYMOLT_TRACE_STDLIB" not in service.trace_env("m", "auto", "", "", False)
        monkeypatch.setenv("PYMOLT_TRACE_STDLIB", "shutil")
        env = service.trace_env("m", "auto", "", "", False, trace_stdlib=["collections"])
        assert env["PYMOLT_TRACE_STDLIB"] == "shutil"
        monkeypatch.setenv("PYMOLT_TRACE_STDLIB", "none")
        env = service.trace_env("m", "auto", "", "", False, trace_stdlib=["collections"])
        assert "PYMOLT_TRACE_STDLIB" not in env

    def test_baseline_traces_the_stdlib_sites_only_for_a_python_move(self, tmp_path):
        project = self._project(tmp_path)
        assert service.stdlib_trace_modules(project, "baseline") == ["collections", "imp"]
        (project / ".pymolt" / "env_config.json").write_text(json.dumps(
            {"base_python": "3.12.1", "target_python": "3.12"}))
        assert service.stdlib_trace_modules(project, "baseline") == []

    def test_edited_lines_add_the_stdlib_modules_they_now_use(self, tmp_path, monkeypatch):
        project = self._project(tmp_path)
        trace = project / ".pymolt" / "contract_traces" / "baseline.jsonl"
        trace.parent.mkdir(parents=True)
        trace.write_text("")
        state = ContractState(baseline=service.ContractSlot(
            trace_path=str(trace), captured_at="t", mode=CaptureMode.TEST_SUITE))
        state.save(service._state_path(project))
        from pymolt.verify.facts import store_site_text
        store_site_text(trace, {"app.py:6": "return click.get_terminal_size()"})
        assert service.stdlib_trace_modules(project, "post_migration") == ["collections", "imp"]
        (project / "app.py").write_text(
            "import shutil\nimport imp\nfrom collections import Iterable\n\n"
            "def size():\n    return shutil.get_terminal_size()\n")
        assert service.stdlib_trace_modules(project, "post_migration") == [
            "collections", "imp", "shutil"]

    def test_plan_old_source_marks_the_changed_lines(self, tmp_path):
        old = "import click\n\nx = click.get_terminal_size()\n"
        new = "import click\nimport shutil\n\nx = shutil.get_terminal_size()\n"
        changed = service._changed_lines(old, new)
        assert changed == {2, 4}
        assert service._stdlib_modules_on_lines(new, changed) == {"shutil"}
        assert service._stdlib_modules_on_lines(new, {1}) == set()

    def test_capture_passes_the_list_and_records_it_on_the_slot(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PYMOLT_TRACE_STDLIB", raising=False)
        project = self._project(tmp_path)
        calls = []

        def fake_capture(target, command, out_path, backend, **kwargs):
            calls.append(kwargs)
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            Path(out_path).write_text('{"q": "m.f"}\n')
            return service.TraceCaptureResult(
                out_path=str(out_path), events=1, processes=1, where="local",
                trace_stdlib=kwargs.get("trace_stdlib", []))

        monkeypatch.setattr(service, "capture_trace", fake_capture)
        slot = service.capture_named_trace(project, "baseline", CaptureMode.TEST_SUITE,
                                           command=["pytest"])
        assert calls[0]["trace_stdlib"] == ["collections", "imp"]
        assert slot.traced_stdlib == ["collections", "imp"]
        service.capture_named_trace(project, "baseline", CaptureMode.TEST_SUITE,
                                    command=["pytest"], stdlib="none")
        assert "trace_stdlib" not in calls[1]

    def test_the_variable_reaches_the_traced_process(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PYMOLT_TRACE_STDLIB", raising=False)
        seen = tmp_path / "seen.txt"
        script = _write_script(tmp_path, (
            f"import os\nopen({str(seen)!r}, 'w').write("
            "os.environ.get('PYMOLT_TRACE_STDLIB', '-'))\n"))
        service.capture_trace("json", [sys.executable, str(script)], tmp_path / "t.jsonl",
                              "auto", trace_stdlib=["shutil", "imp"])
        assert seen.read_text() == "shutil,imp"

    def test_site_text_fallback_follows_a_shifted_line(self, tmp_path):
        project = self._project(tmp_path)
        trace = project / ".pymolt" / "contract_traces" / "baseline.jsonl"
        trace.parent.mkdir(parents=True)
        trace.write_text("")
        ContractState(baseline=service.ContractSlot(
            trace_path=str(trace), captured_at="t", mode=CaptureMode.TEST_SUITE,
        )).save(service._state_path(project))
        from pymolt.verify.facts import store_site_text
        store_site_text(trace, {"app.py:6": "return click.get_terminal_size()"})
        # An inserted import line shifts the edited call down by two.
        (project / "app.py").write_text(
            "import shutil\nimport imp\nfrom collections import Iterable\n\n\n\n"
            "def size():\n    return shutil.get_terminal_size()\n")
        assert "shutil" in service.stdlib_trace_modules(project, "post_migration")

    def test_an_explicit_target_gets_no_automatic_stdlib(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PYMOLT_TRACE_STDLIB", raising=False)
        project = self._project(tmp_path)
        calls = []

        def fake_capture(target, command, out_path, backend, **kwargs):
            calls.append(kwargs)
            Path(out_path).parent.mkdir(parents=True, exist_ok=True)
            Path(out_path).write_text('{"q": "m.f"}\n')
            return service.TraceCaptureResult(out_path=str(out_path), events=1, processes=1,
                                              where="local")

        monkeypatch.setattr(service, "capture_trace", fake_capture)
        service.capture_named_trace(project, "baseline", CaptureMode.TEST_SUITE,
                                    command=["pytest"], target="flask")
        service.capture_named_trace(project, "baseline", CaptureMode.TEST_SUITE,
                                    command=["pytest"], target="flask", stdlib="imp")
        assert "trace_stdlib" not in calls[0]
        assert calls[1]["trace_stdlib"] == ["imp"]

    def test_every_imported_stdlib_module_is_traced_less_the_noise(self, tmp_path):
        project = self._project(tmp_path)
        (project / "more.py").write_text(
            "from __future__ import annotations\nimport asyncio, typing, abc\n"
            "import os.path as p  # comment\nfrom xml.etree import ElementTree\nimport click\n"
            "import importlib.machinery, logging\n")
        # importlib stays traced: its breakage sits on loader objects only the trace can name
        assert service.stdlib_trace_modules(project, "baseline") == [
            "asyncio", "collections", "imp", "importlib", "xml"]
