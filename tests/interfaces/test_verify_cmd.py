"""CLI tests for `pymolt capture` and the report `pymolt verify` renders.

Drives the actual Typer CliRunner invocation, not just the underlying
service/report functions — an import or wiring bug in the CLI's own
rendering code (e.g. a stdlib name used only inside a render helper) does
not show up in tests that call build_contract_report()/build_boundary_diff()
directly, only in one that actually runs the command end-to-end.
"""
from __future__ import annotations

import json
import re
import sys

import pytest
from typer.testing import CliRunner

from pymolt.ingestion.config import EnvConfig, ToolChoice
from pymolt.interfaces.cli.commands import app as cli
from pymolt.interfaces.cli.output import EXIT_FINDING, EXIT_INCONCLUSIVE, EXIT_OK, EXIT_USAGE
from pymolt.verify import service
from pymolt.verify.models import CaptureMode, CaptureValidity, ContractState
from pymolt.verify.unified import UnifiedVerificationReport

runner = CliRunner()


@pytest.fixture(autouse=True)
def _configured(tmp_path):
    """Captures need a setup: it is the environment they record."""
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\n", encoding="utf-8")
    EnvConfig(
        selected_manifest="requirements.txt", selected_tool=ToolChoice.UV,
        base_python="3.11", target_python="3.12",
    ).save(tmp_path / ".pymolt" / "env_config.json")


def test_capture_before_setup_is_refused(tmp_path):
    (tmp_path / ".pymolt" / "env_config.json").unlink()
    app_py = _write_app(tmp_path, third_party=False)

    result = runner.invoke(cli, [
        "capture", "baseline", "--mode", "tests",
        "--project-dir", str(tmp_path), "--target", "json",
        "--", sys.executable, str(app_py),
    ])

    assert result.exit_code == EXIT_USAGE
    assert "not configured" in result.output
    assert "pymolt setup" in result.output
    assert not (tmp_path / ".pymolt" / "contract_state.json").exists()


def test_capture_hints_name_the_project_dir(tmp_path, monkeypatch):
    """QA H1: the commands capture prints carry the dir the user gave."""
    monkeypatch.chdir(tmp_path.parent)
    rel = tmp_path.name
    started = runner.invoke(cli, ["capture", "post-migration", "--mode", "attach",
                                  "--project-dir", rel])
    assert started.exit_code == 0, started.output
    shown = " ".join(re.sub("[│╭╮╰╯─]", " ", started.output).split())
    assert f"pymolt capture post-migration --project-dir {rel} --mode attach --collect" in shown
    missing = runner.invoke(cli, ["capture", "baseline", "--mode", "attach",
                                  "--project-dir", rel, "--collect"])
    assert f"pymolt capture baseline --project-dir {rel} --mode attach" in " ".join(
        missing.output.split())
    (tmp_path / ".pymolt" / "env_config.json").unlink()
    refused = runner.invoke(cli, ["capture", "baseline", "--mode", "tests",
                                  "--project-dir", rel, "--", "python", "-c", "1"])
    assert f"pymolt setup {rel}" in " ".join(refused.output.split())


def _write_app(tmp_path, *, third_party: bool):
    p = tmp_path / "app.py"
    if third_party:
        p.write_text(
            "import requests\n"
            "def fetch():\n"
            "    try:\n"
            "        requests.get('http://example.invalid', timeout=0.01)\n"
            "    except Exception:\n"
            "        pass\n"
            "fetch()\n"
        )
    else:
        p.write_text("import json\njson.loads('{}')\n")
    return p


def test_capture_writes_named_state(tmp_path):
    app_py = _write_app(tmp_path, third_party=False)
    result = runner.invoke(cli, [
        "capture", "baseline", "--mode", "tests",
        "--project-dir", str(tmp_path), "--target", "json",
        "--", sys.executable, str(app_py),
    ])
    assert result.exit_code == 0, result.output
    assert "captured" in result.output.lower()

    state = ContractState.load(tmp_path / ".pymolt" / "contract_state.json")
    assert state is not None and state.baseline is not None
    assert state.baseline.mode == CaptureMode.TEST_SUITE
    assert state.baseline.validity is CaptureValidity.VALID


def test_failed_test_capture_is_diagnostic_and_fails_cli(tmp_path):
    app_py = tmp_path / "fails.py"
    app_py.write_text("import json\njson.loads('{}')\nraise SystemExit(5)\n")

    result = runner.invoke(cli, [
        "capture", "baseline", "--mode", "tests",
        "--project-dir", str(tmp_path), "--target", "json",
        "--", sys.executable, str(app_py),
    ])

    assert result.exit_code == EXIT_FINDING
    state = ContractState.load(tmp_path / ".pymolt" / "contract_state.json")
    assert state is not None and state.baseline is None
    assert state.diagnostic_captures[-1].validity is CaptureValidity.COMMAND_FAILED


def test_accept_failing_keeps_a_red_baseline(tmp_path):
    app_py = tmp_path / "red.py"
    app_py.write_text("import json\njson.loads('{}')\nprint('1 failed, 4 passed')\n"
                      "raise SystemExit(1)\n")
    result = runner.invoke(cli, [
        "capture", "baseline", "--mode", "tests", "--accept-failing",
        "--project-dir", str(tmp_path), "--target", "json",
        "--", sys.executable, str(app_py),
    ])
    assert result.exit_code == 0, result.output
    assert "RED" in result.output
    state = ContractState.load(tmp_path / ".pymolt" / "contract_state.json")
    assert state.baseline.validity is CaptureValidity.VALID
    assert state.baseline.red["failed"] == 1 and state.baseline.red["returncode"] == 1


def test_accept_failing_still_refuses_a_run_that_recorded_nothing(tmp_path):
    app_py = tmp_path / "dead.py"
    app_py.write_text("raise SystemExit(4)\n")
    result = runner.invoke(cli, [
        "capture", "baseline", "--mode", "tests", "--accept-failing", "--agent",
        "--project-dir", str(tmp_path), "--target", "json",
        "--", sys.executable, str(app_py),
    ])
    assert result.exit_code == EXIT_FINDING
    view = json.loads(result.output.strip().splitlines()[-1])
    assert view["ok"] is False and view["data"]["accepted"] is False


def test_zero_event_capture_is_inconclusive_and_not_active(tmp_path):
    app_py = _write_app(tmp_path, third_party=False)
    result = runner.invoke(cli, [
        "capture", "baseline", "--mode", "command",
        "--project-dir", str(tmp_path), "--target", "definitely_missing_dependency",
        "--", sys.executable, str(app_py),
    ])

    assert result.exit_code == EXIT_INCONCLUSIVE
    state = ContractState.load(tmp_path / ".pymolt" / "contract_state.json")
    assert state is not None and state.baseline is None
    assert state.diagnostic_captures[-1].validity is CaptureValidity.EMPTY


def test_capture_overwrite_requires_confirmation_unless_forced(tmp_path):
    app_py = _write_app(tmp_path, third_party=False)
    args = ["capture", "baseline", "--mode", "tests",
            "--project-dir", str(tmp_path), "--target", "json",
            "--", sys.executable, str(app_py)]

    first = runner.invoke(cli, args)
    assert first.exit_code == 0, first.output

    declined = runner.invoke(cli, args, input="n\n")
    assert declined.exit_code == 1

    forced_args = ["capture", "baseline", "--mode", "tests",
                   "--project-dir", str(tmp_path), "--target", "json", "--force",
                   "--", sys.executable, str(app_py)]
    forced = runner.invoke(cli, forced_args)
    assert forced.exit_code == 0, forced.output


def test_capture_attach_then_collect(tmp_path):
    started = runner.invoke(cli, [
        "capture", "post-migration", "--mode", "attach",
        "--project-dir", str(tmp_path), "--target", "requests",
    ])
    assert started.exit_code == 0, started.output
    assert "run this yourself" in started.output.lower()

    out_path = service.pending_attach_path(tmp_path, "post_migration")
    assert out_path.parent.is_dir()
    out_path.write_text('{"q": "requests.get"}\n')

    collected = runner.invoke(cli, [
        "capture", "post-migration", "--mode", "attach",
        "--project-dir", str(tmp_path), "--collect",
    ])
    assert collected.exit_code == 0, collected.output
    assert "collected" in collected.output.lower()

    state = ContractState.load(tmp_path / ".pymolt" / "contract_state.json")
    assert state.post_migration.events == 1
    assert state.post_migration.mode == CaptureMode.LIVE_ATTACH


def test_collect_without_a_pending_capture_errors(tmp_path):
    result = runner.invoke(cli, [
        "capture", "baseline", "--mode", "attach",
        "--project-dir", str(tmp_path), "--collect",
    ])
    assert result.exit_code == 2
    assert "no pending" in result.output.lower()


def test_report_auto_sources_from_captured_state_and_explicit_override_wins(tmp_path):
    app_py = _write_app(tmp_path, third_party=True)
    cap = runner.invoke(cli, [
        "capture", "baseline", "--mode", "tests",
        "--project-dir", str(tmp_path), "--target", "requests",
        "--", sys.executable, str(app_py),
    ])
    assert cap.exit_code == 0, cap.output

    rep = runner.invoke(cli, ["verify", str(tmp_path), "--json"])
    assert rep.exit_code == EXIT_INCONCLUSIVE, rep.output
    data = json.loads(rep.output)["contract"]
    assert data["static_targets"] >= 1
    # the captured baseline trace was picked up (auto-sourced from state, no --trace given):
    assert data["observed_targets"] >= 1
    assert not any("no trace given" in n for n in data["notes"])

    empty_trace = tmp_path / "empty.jsonl"
    empty_trace.write_text("")
    overridden = runner.invoke(
        cli, ["verify", str(tmp_path), "--post-migration", str(empty_trace), "--json"]
    )
    overridden_data = json.loads(overridden.output)["contract"]
    assert overridden_data["observed_targets"] == 0  # explicit (empty) trace wins over state


def test_verify_on_explicit_recordings_renders_and_is_not_saved(tmp_path):
    """Two recordings from elsewhere (what `contract diff` took): rendered in
    human mode without crashing, and never saved as this project's verdict."""
    app_py = _write_app(tmp_path, third_party=True)
    old = tmp_path / "old.jsonl"
    new = tmp_path / "new.jsonl"
    for out in (old, new):
        service.capture_trace("requests", [sys.executable, str(app_py)], out, "auto")

    result = runner.invoke(cli, [
        "verify", str(tmp_path), "--baseline", str(old), "--post-migration", str(new),
    ])
    # the same run recorded twice, its one call site covered: nothing changed
    assert result.exit_code == EXIT_OK, result.output
    assert "Traceback" not in result.output
    assert "Not saved" in result.output
    assert not (tmp_path / ".pymolt" / "verification.json").exists()


def test_verify_on_another_post_migration_recording_saves_what_it_compared(tmp_path):
    """`--post-migration PATH` alone compares PATH with the baseline capture: saved like a
    plain verify (the legacy harness reads .pymolt/verification.json), naming both sides."""
    app_py = _write_app(tmp_path, third_party=True)
    cap = runner.invoke(cli, [
        "capture", "baseline", "--mode", "tests", "--project-dir", str(tmp_path),
        "--target", "requests", "--", sys.executable, str(app_py),
    ])
    assert cap.exit_code == 0, cap.output
    baseline = service.load_contract_state(tmp_path).baseline
    other = tmp_path / "other.jsonl"
    service.capture_trace("requests", [sys.executable, str(app_py)], other, "auto")

    result = runner.invoke(cli, ["verify", str(tmp_path), "--post-migration", str(other),
                                 "--agent"])

    view = json.loads(result.output)
    saved = UnifiedVerificationReport.load(tmp_path)
    assert saved is not None
    assert view["data"]["saved_path"] == str(tmp_path / ".pymolt" / "verification.json")
    compared = saved.contract.compared
    assert compared.after == str(other) and compared.after_capture is None
    assert service.same_slot_path(tmp_path, compared.before, baseline.trace_path)
    assert compared.before_capture == "baseline"
    assert view["data"]["compared_recordings"]["after"] == str(other)
    assert "does not count it as the migration's verdict" in view["summary"]

    # The post-migration capture's own file is the capture: nothing to disclaim.
    post = service.capture_named_trace(
        tmp_path, "post_migration", CaptureMode.TEST_SUITE,
        command=[sys.executable, str(app_py)], target="requests")
    result = runner.invoke(cli, ["verify", str(tmp_path), "--post-migration",
                                 post.trace_path, "--agent"])
    assert UnifiedVerificationReport.load(tmp_path).contract.compared.captures
    assert "does not count" not in json.loads(result.output)["summary"]


def test_verify_with_opaque_values_is_inconclusive(tmp_path):
    event = {
        "q": "pkg.call", "in": {"bound": {}}, "t": "return",
        "result": {"__opaque__": "Thing"},
    }
    old = tmp_path / "old.jsonl"
    new = tmp_path / "new.jsonl"
    old.write_text(json.dumps(event) + "\n")
    new.write_text(json.dumps(event) + "\n")

    result = runner.invoke(cli, [
        "verify", str(tmp_path), "--baseline", str(old), "--post-migration", str(new), "--json",
    ])

    assert result.exit_code == EXIT_INCONCLUSIVE
    assert json.loads(result.output)["verdict"] == "inconclusive"


def test_verify_without_comparable_pair_is_inconclusive(tmp_path):
    _write_app(tmp_path, third_party=True)

    result = runner.invoke(cli, ["verify", str(tmp_path), "--json"])

    assert result.exit_code == EXIT_INCONCLUSIVE
    payload = json.loads(result.output)
    assert payload["verdict"] == "inconclusive"
    assert payload["contract"]["verdict_reasons"]


def _probe_project(tmp_path, result):
    """TESTING §D: a hand-written one-call trace of yaml.safe_load, replayed by the probe."""
    project = tmp_path / "proj"
    (project / "app").mkdir(parents=True)
    (project / "app" / "__init__.py").write_text("")
    (project / "app" / "m.py").write_text(
        "import yaml\ndef load():\n    return yaml.safe_load(\"a: 1\")\n"
        "def dump():\n    return yaml.dump({})\n")
    trace = tmp_path / "trace.jsonl"
    trace.write_text(json.dumps({"q": "yaml.safe_load", "in": {"bound": {"stream": "a: 1"}},
                                 "result": result, "t": "return"}) + "\n")
    return project, trace


def test_a_changed_probe_replay_fails_with_one_capture(tmp_path):
    """QA V4: the replay compares with the recording it replays, not with the missing side."""
    project, trace = _probe_project(tmp_path, {"__dict__": [["a", 2]]})
    args = ["verify", str(project), "--post-migration", str(trace),
            "--probe-python", sys.executable, "--json"]
    result = runner.invoke(cli, args)
    assert result.exit_code == EXIT_FINDING, result.output
    payload = json.loads(result.output)
    assert payload["verdict"] == "fail"
    assert payload["contract"]["verdict_reasons"] == ["sandbox replay changed behavior"]
    assert any("FAIL rests on the probe replay alone" in n for n in payload["contract"]["notes"])

    # The hand-written recording carries no event-loss counters (QA V2-1): that blocks a
    # version diff, not the replay, which compares each call with its own recorded outcome.
    assert any("trace.jsonl has no event-loss counters" in n
               for n in payload["contract"]["notes"])

    stable, trace = _probe_project(tmp_path / "stable", {"__dict__": [["a", 1]]})
    result = runner.invoke(cli, ["verify", str(stable), "--post-migration", str(trace),
                                 "--probe-python", sys.executable, "--json"])
    assert result.exit_code == EXIT_INCONCLUSIVE    # a stable replay proves no PASS alone
    assert json.loads(result.output)["contract"]["verdict_reasons"] == [
        "no baseline trace is available for a version diff",
        "the post-migration recording trace.jsonl has no event-loss counters"]


_LOSSLESS = {"events_seen": 1, "events_written": 1, "sampling_dropped": 0,
             "backpressure_dropped": 0, "write_failures": 0, "sink_failures": 0,
             "shutdown_dropped": 0}


def test_explicit_recordings_need_their_event_loss_counters(tmp_path):
    """QA V2-1: two identical hand-written recordings passed PASS (and a changed pair FAIL)
    with no counters to say they lost no event; a capture without them is INCONCLUSIVE. The
    watcher's own sidecar, one process's or several gathered under ``processes``, carries
    them; a recording that dropped events stays INCONCLUSIVE."""
    project, old = _probe_project(tmp_path, {"__dict__": [["a", 1]]})
    (project / "app" / "m.py").write_text(
        "import yaml\ndef load():\n    return yaml.safe_load(\"a: 1\")\n")
    old.write_text(old.read_text().replace(
        '"t": "return"', '"t": "return", "where": {"file": "app/m.py", "line": 3}'))
    new = tmp_path / "new.jsonl"
    new.write_text(old.read_text())
    changed = tmp_path / "changed.jsonl"
    changed.write_text(old.read_text().replace('["a", 1]', '["a", 2]'))

    def verify(after):
        result = runner.invoke(cli, ["verify", str(project), "--baseline", str(old),
                                     "--post-migration", str(after), "--json"])
        return result.exit_code, json.loads(result.output)

    for after in (new, changed):
        code, payload = verify(after)
        assert code == EXIT_INCONCLUSIVE, payload["contract"]["verdict_reasons"]
        assert payload["contract"]["verdict_reasons"] == [
            f"the post-migration recording {after.name} has no event-loss counters",
            "the baseline recording trace.jsonl has no event-loss counters"]
        assert all(c["kind"] == "needs_evidence" for c in payload["inconclusive_causes"])
        assert "`.meta.json` sidecar" in payload["inconclusive_causes"][0]["settle"]

    # one process's own sidecar, and a merged recording's sidecars gathered
    (tmp_path / "trace.jsonl.meta.json").write_text(json.dumps({"counters": _LOSSLESS}))
    for after in (new, changed):
        (tmp_path / f"{after.name}.meta.json").write_text(json.dumps(
            {"processes": [{"counters": _LOSSLESS}, {"counters": _LOSSLESS}]}))
    code, payload = verify(new)
    assert (code, payload["verdict"]) == (EXIT_OK, "pass"), payload["contract"]["verdict_reasons"]
    code, payload = verify(changed)
    assert (code, payload["verdict"]) == (EXIT_FINDING, "fail")

    # a process without complete counters, or one that dropped events
    (tmp_path / "new.jsonl.meta.json").write_text(json.dumps(
        {"processes": [{"counters": _LOSSLESS}, {"pid": 7}]}))
    assert verify(new)[1]["contract"]["verdict_reasons"] == [
        "the post-migration recording new.jsonl has no event-loss counters"]
    (tmp_path / "new.jsonl.meta.json").write_text(json.dumps(
        {"counters": {**_LOSSLESS, "backpressure_dropped": 3}}))
    code, payload = verify(new)
    assert code == EXIT_INCONCLUSIVE
    assert payload["contract"]["verdict_reasons"] == [
        "the post-migration recording new.jsonl dropped 3 event(s)"]


def test_report_renders_coverage_recall_and_the_identity_tiers(tmp_path, monkeypatch):
    """The headline is coverage and recall over contacts, stamped with how "ran" was known,
    the BLIND split, and the symbol-matching tiers from exact-join up."""
    site = tmp_path / "site" / "fakedep"
    site.mkdir(parents=True)
    (site / "__init__.py").write_text("from fakedep._impl import fetch, other\n")
    (site / "_impl.py").write_text("def fetch(value):\n    return value\n\n"
                                   "def other():\n    return 2\n")
    monkeypatch.setenv("PYTHONPATH", str(site.parent))
    project = tmp_path / "proj"
    project.mkdir()
    (project / "app.py").write_text("from fakedep import fetch, other\n\ndef run():\n"
                                    "    fetch(1)\n\ndef never():\n    other()\n")
    trace = tmp_path / "trace.jsonl"
    trace.write_text(json.dumps({
        "t": "return", "q": "fakedep._impl.fetch", "sym": "fakedep._impl.fetch",
        "in": {"bound": {}}, "result": 1,
        "where": {"file": str(project / "app.py"), "line": 4, "func": "run"},
    }) + "\n")
    # the executed record: run() ran, never() did not
    (tmp_path / "trace.jsonl.meta.json").write_text(json.dumps({"processes": [{"executed": {
        "v": 1, "granularity": "line", "complete": True, "until": None,
        "files": {str(project / "app.py"): {"lines": "1,3,4,6"}}}}]}))

    result = runner.invoke(cli, ["verify", str(project), "--post-migration", str(trace)])
    assert "Traceback" not in result.output
    text = " ".join(re.sub(r"[│╭╮╰╯─]", " ", result.output).split())
    assert ("Coverage: 50% (1 of 2 contacts ran; granularity line) "
            "Recall: 100% (1 of 1 recorded)") in text
    assert "BLIND symbols: not run 1, ran but not recorded 0, unobservable 0" in text
    assert "Symbol matching: exact-join 0% (0/2) → canonical 50% (1/2)" in text

    data = json.loads(runner.invoke(
        cli, ["verify", str(project), "--post-migration", str(trace), "--json"]
    ).output)["contract"]
    assert [tier["name"] for tier in data["identity_tiers"]] == ["exact", "canonical"]
    assert (data["coverage"], data["recall"], data["coverage_granularity"]) == (0.5, 1.0, "line")
    assert {c["target"]: c["status"] for c in data["contacts"]} == {
        "fakedep.fetch": "recorded", "fakedep.other": "not-run"}
    assert "trust" not in data


def test_the_blind_table_says_what_settles_each_kind_and_the_verdict_agrees(tmp_path,
                                                                           monkeypatch):
    """QA V2b/V5: a probe never settles a BLIND symbol, and no test settles one no capture can
    record: the caption says per kind what does, and the verdict box and --agent agree."""
    site = tmp_path / "site" / "fakedep"
    site.mkdir(parents=True)
    (site / "__init__.py").write_text("from fakedep._impl import fetch, other, SETTING\n")
    (site / "_impl.py").write_text("SETTING = 1\n\ndef fetch(value):\n    return value\n\n"
                                   "def other():\n    return 2\n")
    monkeypatch.setenv("PYTHONPATH", str(site.parent))
    project = tmp_path / "proj"
    project.mkdir()
    (project / "app.py").write_text("import fakedep\nfrom fakedep import fetch, other\n\n"
                                    "def run():\n    fetch(fakedep.SETTING)\n\n"
                                    "def never():\n    other()\n")
    trace = tmp_path / "trace.jsonl"
    trace.write_text(json.dumps({
        "t": "return", "q": "fakedep._impl.fetch", "sym": "fakedep._impl.fetch",
        "in": {"bound": {}}, "result": 1,
        "where": {"file": str(project / "app.py"), "line": 5, "func": "run"},
    }) + "\n")
    (tmp_path / "trace.jsonl.meta.json").write_text(json.dumps({"processes": [{"executed": {
        "v": 1, "granularity": "line", "complete": True, "until": None,
        "files": {str(project / "app.py"): {"lines": "1,2,4,5,7"}}}, "counters": _LOSSLESS}]}))
    args = ["verify", str(project), "--baseline", str(trace), "--post-migration", str(trace)]

    text = " ".join(re.sub(r"[│╭╮╰╯─]", " ", runner.invoke(
        cli, args, env={"COLUMNS": "200"}).output).split())

    assert "probe-python" not in text
    assert "unobservable: no capture can record them, a person reviews each use" in text
    assert "not run: a test that reaches them settles them" in text
    # The unobservable SETTING keeps nothing from PASS (the owner's decision): the verdict
    # lists it instead of making it a reason.
    assert ("1 dependency use(s) cannot be observed by a capture, so this verdict does not "
            "cover them; a person may glance at: fakedep.SETTING") in text
    view = json.loads(runner.invoke(cli, [*args, "--agent"]).output)
    assert view["data"]["unobservable_uses"] == ["fakedep.SETTING"]
    kinds = {c["reason"]: c["kind"] for c in view["data"]["inconclusive_causes"]}
    assert not any("fakedep.SETTING" in reason for reason in kinds)
    assert kinds["contract: 1 static dependency symbol(s) were not exercised (1 not run by "
                 "the workload)"] == "needs_evidence"


def test_noise_against_another_recording_is_not_settled_by_a_replicate(tmp_path):
    """QA V2-3 (TESTING §F): verify suggested `pymolt capture baseline --replicate` for an input
    change between two recordings named on the command line, where there is no baseline
    capture to replicate (it fails: "no valid baseline to replicate"), and a replicate of one
    would not measure another recording's noise. It suggests what works instead."""
    project = tmp_path / "proj"
    (project / "app").mkdir(parents=True)
    (project / "app" / "__init__.py").write_text("")
    (project / "app" / "m.py").write_text(
        "import yaml\ndef load(s):\n    return yaml.safe_load(s)\n")
    counters = {name: 0 for name in ("events_seen", "events_written", "sampling_dropped",
                                     "backpressure_dropped", "write_failures", "sink_failures",
                                     "shutdown_dropped")}
    for name, value in (("old", 1), ("new", 2)):
        (tmp_path / f"{name}.jsonl").write_text(json.dumps({
            "q": "yaml.safe_load", "in": {"bound": {"stream": f"a: {value}"}},
            "result": {"__dict__": [["a", value]]}, "t": "return",
            "where": {"file": "app/m.py", "line": 3, "func": "load"}}) + "\n")
        (tmp_path / f"{name}.jsonl.meta.json").write_text(json.dumps({"counters": counters}))
    args = ["verify", str(project), "--baseline", str(tmp_path / "old.jsonl"),
            "--post-migration", str(tmp_path / "new.jsonl")]

    view = json.loads(runner.invoke(cli, [*args, "--agent"]).output)
    (found,) = view["data"]["inconclusive_causes"]
    assert found["cause"] == "noise" and "input changed" in found["reason"]
    assert "--replicate" not in found["reason"] + found["settle"]
    assert "--baseline RUN1 --post-migration RUN2" in found["settle"]

    text = " ".join(runner.invoke(cli, args, env={"COLUMNS": "200"}).output.split())
    assert "pymolt capture baseline --replicate" not in text
    assert "Review — input changed: 1 entry at 1 site(s). Action: record the before side" in text
    assert "noise not measured (the before side is not the baseline capture" in text
