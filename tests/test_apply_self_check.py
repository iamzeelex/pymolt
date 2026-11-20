"""apply checks its own write: a compile on the target (and the base) before it, and (unless
``--no-smoke``) an import of the touched modules in the target environment around it."""
from __future__ import annotations

import json
import subprocess
import sys

import pytest
from _apply import apply_migration_plan
from typer.testing import CliRunner

from pymolt.apply_check import SmokeCheck, module_for, smoke_verdict
from pymolt.codemods.models import DependencyMigration, FilePreview
from pymolt.ingestion.config import EnvConfig, ToolChoice
from pymolt.interfaces.cli.commands import app
from pymolt.migration_plan import (
    MigrationCheckError,
    MigrationPlan,
    create_migration_plan,
)
from pymolt.migration_state import MigrationReceipt
from pymolt.status import build_status
from pymolt.verify import service
from pymolt.verify.models import CaptureMode

GOOD_OLD = "import json\nVALUE = json.dumps(1)\n"
GOOD_NEW = "import json\nVALUE = json.dumps(2)\n"


def _project(tmp_path, *, target_env: bool = False):
    (tmp_path / "requirements.txt").write_text("flask==2.0.3\n", encoding="utf-8")
    (tmp_path / "requirements-target.txt").write_text("flask==3.0.0\n", encoding="utf-8")
    env = None
    if target_env:
        # A stand-in target environment: its bin/python is this interpreter.
        (tmp_path / "target-env" / "bin").mkdir(parents=True)
        (tmp_path / "target-env" / "bin" / "python").symlink_to(sys.executable)
        env = "target-env"
    EnvConfig(
        selected_manifest="requirements.txt", selected_tool=ToolChoice.UV,
        base_python="3.11", target_python="3.12", target_env_path=env,
    ).save(tmp_path / ".pymolt" / "env_config.json")
    script = tmp_path / "baseline_run.py"
    script.write_text("import json\njson.loads('{}')\n", encoding="utf-8")
    service.capture_named_trace(tmp_path, "baseline", CaptureMode.LIVE_COMMAND,
                                command=[sys.executable, str(script)], target="json")


def _plan(tmp_path, old: str, new: str, *, name="app0.py", runs_on_base=None,
          target_env=False) -> MigrationPlan:
    _project(tmp_path, target_env=target_env)
    path = tmp_path / name
    path.write_text(old, encoding="utf-8")
    plan = create_migration_plan(
        tmp_path, [FilePreview(path=str(path), old_source=old, new_source=new, sites=1)],
        [DependencyMigration("flask", "2.0.3", "3.0.0")], target_python="3.12",
        source_manifest="requirements.txt",
        target_manifest_path=tmp_path / "requirements-target.txt", files_scanned=1,
        predict=False)
    if runs_on_base is not None:
        plan.changes[0].runs_on_base = runs_on_base
    plan.save(tmp_path)
    return plan


# ── compile, before writing ──────────────────────────────────────────────────


def test_a_rewrite_that_does_not_compile_is_refused_before_any_write(tmp_path):
    plan = _plan(tmp_path, GOOD_OLD, "import json\nVALUE = json.dumps(1\n")

    with pytest.raises(MigrationCheckError, match="does not compile") as caught:
        apply_migration_plan(tmp_path, plan)

    assert "app0.py on the target (SyntaxError" in str(caught.value)
    assert caught.value.check.compile[0].status == "failed"
    assert (tmp_path / "app0.py").read_text(encoding="utf-8") == GOOD_OLD
    assert not (tmp_path / ".pymolt" / "migrations").exists()
    assert MigrationReceipt.load(tmp_path) is None


def test_the_receipt_records_the_compile_and_a_failure_that_predates_the_plan(tmp_path):
    """A file that already fails on the target is not the rewrite's doing: recorded, not
    refused (the plan's compile findings report it)."""
    old = "def legacy(a, a):\n    pass\nimport json\n"   # parses; never compiled on 3.x
    plan = _plan(tmp_path, old, old.replace("import json", "import json  # moved"))

    receipt = apply_migration_plan(tmp_path, plan)

    [target] = receipt.self_check.compile
    assert target.side == "target" and target.status == "passed"
    assert target.python == "3.12" and target.files == ["app0.py"]
    assert "SyntaxError" in target.pre_existing["app0.py"]
    smoke = receipt.self_check.smoke              # not asked for: the receipt says so
    assert (smoke.status, smoke.reason) == ("skipped", "turned off")
    assert MigrationReceipt.load(tmp_path).self_check == receipt.self_check
    assert "already failing before" in receipt.self_check.summary()


def test_no_target_interpreter_skips_the_compile_and_says_why(tmp_path, monkeypatch):
    monkeypatch.setattr("pymolt.pyversion.facts.find_target_interpreter", lambda *a: None)
    plan = _plan(tmp_path, GOOD_OLD, GOOD_NEW)

    receipt = apply_migration_plan(tmp_path, plan)

    [target] = receipt.self_check.compile
    assert (target.status, target.reason) == ("skipped", "no interpreter for Python 3.12")
    assert "compile on the target not checked" in receipt.self_check.summary()


def test_a_change_that_claims_to_run_on_the_base_is_compiled_there_through_its_runner(
        tmp_path, monkeypatch):
    """A container base is reached through the runner adapter (``docker exec``)."""
    from pymolt.adapters import python_runner
    from pymolt.adapters.python_runner import RunnerSpec

    spec = RunnerSpec(name="legacy", container_id="c0ffee")
    monkeypatch.setattr("pymolt.pyversion.plan._base_side", lambda root, base: ("python", spec))
    calls = []

    def docker(args, *, stdin=None, timeout=None):
        calls.append(args[:3])
        exe, *rest = args[3:]
        return subprocess.run([sys.executable, *rest], input=stdin, capture_output=True,
                              text=True)

    monkeypatch.setattr(python_runner, "_docker", docker)
    plan = _plan(tmp_path, GOOD_OLD, "import json\nVALUE = f'{json.dumps(1)}'\n",
                 runs_on_base=True)

    receipt = apply_migration_plan(tmp_path, plan)

    target, base = receipt.self_check.compile
    assert base.side == "base" and base.status == "passed"
    assert base.executable == "python in container legacy"
    assert calls == [["exec", "-i", "c0ffee"]]


def test_a_rewrite_that_breaks_the_base_where_it_claims_to_run_is_refused(tmp_path,
                                                                          monkeypatch):
    from pymolt.pyversion import fixes

    real = fixes.recompile_detailed

    def base_rejects_walrus(files, executable, *, runner=None, timeout=120.0):
        if executable == "base-python":
            bad = {p: "SyntaxError: invalid syntax (line 2)" for p, s in files.items()
                   if ":=" in s}
            return bad, None, "3.6"
        return real(files, executable, runner=runner, timeout=timeout)

    monkeypatch.setattr(fixes, "recompile_detailed", base_rejects_walrus)
    monkeypatch.setattr("pymolt.pyversion.plan._base_side",
                        lambda root, base: ("base-python", None))
    plan = _plan(tmp_path, GOOD_OLD, "import json\nif (v := json.dumps(1)):\n    pass\n",
                 runs_on_base=True)

    with pytest.raises(MigrationCheckError, match="app0.py on the base"):
        apply_migration_plan(tmp_path, plan)

    assert (tmp_path / "app0.py").read_text(encoding="utf-8") == GOOD_OLD


# ── smoke, around the write ──────────────────────────────────────────────────


def test_smoke_imports_the_touched_modules_in_the_target_environment(tmp_path):
    plan = _plan(tmp_path, GOOD_OLD, GOOD_NEW, target_env=True)

    receipt = apply_migration_plan(tmp_path, plan, smoke=True)

    smoke = receipt.self_check.smoke
    assert (smoke.status, smoke.modules, smoke.failed) == ("passed", ["app0"], {})
    assert smoke.executable.endswith("target-env/bin/python")
    assert (tmp_path / "app0.py").read_text(encoding="utf-8") == GOOD_NEW


def test_a_smoke_failure_the_write_caused_restores_the_files(tmp_path):
    broken = GOOD_NEW + "raise RuntimeError('broken by the rewrite')\n"
    plan = _plan(tmp_path, GOOD_OLD, broken, target_env=True)

    with pytest.raises(MigrationCheckError, match="smoke import failed") as caught:
        apply_migration_plan(tmp_path, plan, smoke=True)

    smoke = caught.value.check.smoke
    assert smoke.status == "failed"
    assert smoke.failed == {"app0": "RuntimeError: broken by the rewrite"}
    assert (tmp_path / "app0.py").read_text(encoding="utf-8") == GOOD_OLD
    # Design 3.2 step 8: the write was recorded, then rolled back: the ledger says both, and
    # the step's receipt says why.
    receipt = MigrationReceipt.load(tmp_path)
    pin, step = receipt.steps.values()           # the manifest pin and the code step
    assert (step.status, step.epoch, step.files_restored) == ("rolled_back", None, ["app0.py"])
    assert step.checks["smoke_import"] == "failed: app0: RuntimeError: broken by the rewrite"
    assert (pin.status, pin.files_restored) == ("rolled_back", ["requirements.txt"])
    assert (receipt.status, receipt.in_effect) == ("rolled_back", False)
    assert (tmp_path / "requirements.txt").read_text() == "flask==2.0.3\n"
    ledger = json.loads((tmp_path / ".pymolt" / "apply_ledger.json").read_text())
    assert [(e["event"], e.get("restored")) for e in ledger["events"]] == [
        ("apply", None), ("apply", None), ("rollback", True), ("rollback", True)]
    state = json.loads((tmp_path / ".pymolt" / "migrations" / plan.plan_id / "state.json")
                       .read_text())
    assert state["status"] == "rolled_back" and "smoke import failed" in state["error"]
    # Without the smoke import the same plan applies again in the same run, with a new epoch.
    again = apply_migration_plan(tmp_path, plan)
    assert (again.status, again.run_id) == ("applied", plan.plan_id)
    assert [s.epoch for s in again.steps.values()] == [5, 6]


def test_an_import_that_already_failed_is_pre_existing_not_a_failure(tmp_path):
    old = "import not_installed_anywhere\n"
    plan = _plan(tmp_path, old, old + "VALUE = 1\n", target_env=True)

    receipt = apply_migration_plan(tmp_path, plan, smoke=True)

    smoke = receipt.self_check.smoke
    assert smoke.status == "passed" and smoke.failed == {}
    assert "ModuleNotFoundError" in smoke.pre_existing["app0"]


def test_smoke_without_a_target_environment_is_skipped_with_the_reason(tmp_path):
    plan = _plan(tmp_path, GOOD_OLD, GOOD_NEW)

    receipt = apply_migration_plan(tmp_path, plan, smoke=True)

    assert receipt.self_check.smoke.status == "skipped"
    assert "setup names no target environment" in receipt.self_check.smoke.reason


def test_module_names_follow_the_packages(tmp_path):
    (tmp_path / "src" / "pkg" / "sub").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("")
    (tmp_path / "src" / "pkg" / "sub" / "__init__.py").write_text("")
    assert module_for(tmp_path, "src/pkg/sub/mod.py") == (str(tmp_path / "src"),
                                                          "pkg.sub.mod")
    assert module_for(tmp_path, "src/pkg/__init__.py") == (str(tmp_path / "src"), "pkg")
    assert module_for(tmp_path, "setup.py") == "setup.py runs as a program when imported"
    assert module_for(tmp_path, "my-script.py") == "its path is not an importable module name"


def test_a_harness_the_new_code_breaks_fails_what_imported_before():
    verdict = smoke_verdict([("/p", "a"), ("/p", "b")], {"b": "ImportError: x"}, None,
                            exe="py", python="3.12", before_why=None,
                            after_why="exit 1: os._exit", left_out={})
    assert verdict.status == "failed" and list(verdict.failed) == ["a"]
    assert smoke_verdict([("/p", "a")], None, None, exe="py", python=None,
                         before_why="timeout", after_why=None, left_out={}) == SmokeCheck(
        status="skipped", executable="py", modules=["a"],
        reason="the import on the old code did not run (timeout)")


# ── where it shows ───────────────────────────────────────────────────────────


def test_status_and_the_cli_show_the_self_check(tmp_path):
    _plan(tmp_path, GOOD_OLD, GOOD_NEW, target_env=True)
    runner = CliRunner()

    applied = runner.invoke(app, ["apply", str(tmp_path)])        # the smoke import is the default

    assert applied.exit_code == 0, applied.output
    assert "Self-check: compile on the target 3.12: 1 file(s) ok" in applied.output
    assert "smoke import on 3.12: 1 module(s) ok" in " ".join(applied.output.split())
    detail = build_status(tmp_path).phase("migration").detail
    assert "self-check: compile on the target 3.12: 1 file(s) ok" in detail


def test_no_smoke_skips_the_import_and_the_receipt_says_so(tmp_path):
    _plan(tmp_path, GOOD_OLD, GOOD_NEW, target_env=True)

    applied = CliRunner().invoke(app, ["apply", str(tmp_path), "--no-smoke"])

    assert applied.exit_code == 0, applied.output
    assert "smoke import not checked (--no-smoke)" in " ".join(applied.output.split())
    steps = MigrationReceipt.load(tmp_path).steps.values()
    assert {s.checks["smoke_import"] for s in steps} == {"not checked: --no-smoke"}


def test_the_cli_refusal_carries_the_self_check_and_exits_as_a_finding(tmp_path):
    _plan(tmp_path, GOOD_OLD, "import json\nVALUE = json.dumps(1\n")

    result = CliRunner().invoke(app, ["apply", str(tmp_path), "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False and "does not compile" in payload["error"]
    assert payload["self_check"]["compile"][0]["status"] == "failed"
