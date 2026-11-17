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
from pymolt.interfaces.cli.output import EXIT_FINDING, EXIT_INCONCLUSIVE, EXIT_USAGE
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
    assert result.exit_code in (0, 1, EXIT_INCONCLUSIVE)
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


@pytest.mark.parametrize("old, new", [
    ("capture", "pymolt capture"), ("trace", "pymolt capture"),
    ("export-watcher", "pymolt capture"), ("boundary", "pymolt capture"),
    ("report", "pymolt verify"), ("diff", "pymolt verify --baseline"), ("map", "pymolt scan"),
])
def test_removed_contract_commands_name_their_replacement(old, new):
    result = runner.invoke(cli, ["contract", old, "--when", "baseline"])
    assert result.exit_code == EXIT_USAGE
    assert f"pymolt contract {old}` was removed" in result.output
    assert new in result.output


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
