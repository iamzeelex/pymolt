"""CLI tests for pymolt/interfaces/cli/verify_cmd.py (the `contract` sub-app).

Drives the actual Typer CliRunner invocation, not just the underlying
service/report functions — an import or wiring bug in the CLI's own
rendering code (e.g. a stdlib name used only inside a render helper) does
not show up in tests that call build_contract_report()/build_boundary_diff()
directly, only in one that actually runs the command end-to-end.
"""
from __future__ import annotations

import json
import sys

from typer.testing import CliRunner

from pymolt.interfaces.cli.verify_cmd import app
from pymolt.verify.models import CaptureMode, ContractState

runner = CliRunner()


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
    result = runner.invoke(app, [
        "capture", "--when", "baseline", "--mode", "tests",
        "--project-dir", str(tmp_path), "--target", "json",
        "--", sys.executable, str(app_py),
    ])
    assert result.exit_code == 0, result.output
    assert "captured" in result.output.lower()

    state = ContractState.load(tmp_path / ".pymolt" / "contract_state.json")
    assert state is not None and state.baseline is not None
    assert state.baseline.mode == CaptureMode.TEST_SUITE


def test_capture_overwrite_requires_confirmation_unless_forced(tmp_path):
    app_py = _write_app(tmp_path, third_party=False)
    args = ["capture", "--when", "baseline", "--mode", "tests",
            "--project-dir", str(tmp_path), "--", sys.executable, str(app_py)]

    first = runner.invoke(app, args)
    assert first.exit_code == 0, first.output

    declined = runner.invoke(app, args, input="n\n")
    assert declined.exit_code == 1

    forced_args = ["capture", "--when", "baseline", "--mode", "tests",
                   "--project-dir", str(tmp_path), "--force",
                   "--", sys.executable, str(app_py)]
    forced = runner.invoke(app, forced_args)
    assert forced.exit_code == 0, forced.output


def test_capture_attach_then_collect(tmp_path):
    started = runner.invoke(app, [
        "capture", "--when", "post-migration", "--mode", "attach",
        "--project-dir", str(tmp_path), "--target", "requests",
    ])
    assert started.exit_code == 0, started.output
    assert "run this yourself" in started.output.lower()

    out_path = tmp_path / ".pymolt" / "contract_traces" / "post_migration.jsonl"
    assert out_path.parent.is_dir()
    out_path.write_text('{"q": "requests.get"}\n')

    collected = runner.invoke(app, [
        "capture", "--when", "post-migration", "--mode", "attach",
        "--project-dir", str(tmp_path), "--collect",
    ])
    assert collected.exit_code == 0, collected.output
    assert "collected" in collected.output.lower()

    state = ContractState.load(tmp_path / ".pymolt" / "contract_state.json")
    assert state.post_migration.events == 1
    assert state.post_migration.mode == CaptureMode.LIVE_ATTACH


def test_collect_without_a_pending_capture_errors(tmp_path):
    result = runner.invoke(app, [
        "capture", "--when", "baseline", "--mode", "attach",
        "--project-dir", str(tmp_path), "--collect",
    ])
    assert result.exit_code == 2
    assert "no pending" in result.output.lower()


def test_report_auto_sources_from_captured_state_and_explicit_override_wins(tmp_path):
    app_py = _write_app(tmp_path, third_party=True)
    cap = runner.invoke(app, [
        "capture", "--when", "baseline", "--mode", "tests",
        "--project-dir", str(tmp_path), "--target", "requests",
        "--", sys.executable, str(app_py),
    ])
    assert cap.exit_code == 0, cap.output

    rep = runner.invoke(app, ["report", str(tmp_path), "--json"])
    assert rep.exit_code == 0, rep.output
    data = json.loads(rep.output)
    assert data["static_targets"] >= 1
    # the captured baseline trace was picked up (auto-sourced from state, no --trace given):
    assert data["observed_targets"] >= 1
    assert not any("no trace given" in n for n in data["notes"])

    empty_trace = tmp_path / "empty.jsonl"
    empty_trace.write_text("")
    overridden = runner.invoke(
        app, ["report", str(tmp_path), "--trace", str(empty_trace), "--json"]
    )
    overridden_data = json.loads(overridden.output)
    assert overridden_data["observed_targets"] == 0  # explicit (empty) trace wins over state


def test_diff_human_rendering_does_not_crash_on_skipped_opaque_rows(tmp_path):
    """Regression: render_boundary_diff's _cell() calls os.path.basename — this
    only crashes when the CLI actually renders (not --json), which is why a
    prior refactor that dropped `import os` from verify_cmd.py slipped past
    every test that called build_boundary_diff() directly."""
    app_py = _write_app(tmp_path, third_party=True)
    old = tmp_path / "old.jsonl"
    new = tmp_path / "new.jsonl"
    for out in (old, new):
        traced = runner.invoke(app, [
            "trace", "--target", "requests", "--out", str(out), "--", sys.executable, str(app_py),
        ])
        assert traced.exit_code == 0, traced.output

    result = runner.invoke(app, ["diff", str(old), str(new)])
    assert result.exit_code in (0, 1)
    assert "Traceback" not in result.output
    assert "NameError" not in result.output


def test_boundary_interactive_flow_does_not_crash(tmp_path):
    """Regression: boundary_cmd's work directory uses `tempfile.mkdtemp` — the
    same class of missing-import bug as the diff test above, on a different
    code path (interactive prompting, not `--out`/`--json`)."""
    old = tmp_path / "old.jsonl"
    old.write_text('{"q": "requests.get"}\n')
    new = tmp_path / "new.jsonl"
    new.write_text('{"q": "requests.get"}\n')

    stdin = f"requests\n\n\np\n{old}\np\n{new}\n"
    result = runner.invoke(app, ["boundary"], input=stdin)
    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
