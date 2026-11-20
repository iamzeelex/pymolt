"""Damaged or newer state files (``pymolt.state_io``): the command that reads one refuses with
exit 2 and leaves it byte-identical; ``status`` reports it and exits normally; the capture
state's schema 2."""
from __future__ import annotations

import json
import sys

import pytest
from _stepwise import APP, RECIPES, plan_for
from typer.testing import CliRunner

from pymolt.interfaces.cli.commands import app
from pymolt.state_io import StateFileError
from pymolt.verify import service
from pymolt.verify.models import CONTRACT_STATE_SCHEMA_VERSION, CaptureMode, ContractState
from pymolt.workorder.apply import apply_steps

runner = CliRunner()
M = "app/m.py"
DAMAGED = b'{"schema_version": 2, "trunc'


@pytest.fixture(autouse=True)
def _no_compile(monkeypatch):
    monkeypatch.setattr("pymolt.apply_check.compile_changes", lambda root, plan: [])


@pytest.fixture
def applied(tmp_path):
    """A project with a baseline, a saved plan applied in full (receipt, ledger, run files)."""
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    apply_steps(tmp_path, plan)
    return tmp_path, plan


def _ran(root) -> bool:
    return (root / "RAN").exists()


def _flat(text: str) -> str:
    """Terminal output with rich's line wrapping undone."""
    return " ".join(text.split())


# file (relative to .pymolt/, ``{run}`` for the run's id) -> the command that reads it.
CASES = {
    "env_config.json": lambda d: [
        "setup", d, "--manifest", "requirements.txt", "--tool", "system",
        "--base-python", "3.11", "--target-python", "3.12", "--json"],
    "contract_state.json": lambda d: [
        "capture", "post-migration", "--project-dir", d, "--mode", "command", "--json", "--",
        sys.executable, "-c", "open('RAN', 'w')"],
    "migration_plan.json": lambda d: ["plan", d, "--json"],
    "migration_state.json": lambda d: ["apply", d, "--json"],
    "apply_ledger.json": lambda d: ["rollback", d, "--json"],
    "migrations/{run}/state.json": lambda d: ["rollback", d, "--json"],
    "migrations/{run}/plan.json": lambda d: ["rollback", d, "--json"],
    "migrations/{run}/approval.json": lambda d: ["apply", d, "--agent"],
    "verification.json": lambda d: ["verify", d, "--json"],
}


@pytest.mark.parametrize("name", list(CASES))
def test_a_damaged_state_file_refuses_the_command_that_reads_it_and_stays_as_it_was(
        applied, name):
    root, plan = applied
    path = root / ".pymolt" / name.format(run=plan.plan_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(DAMAGED)

    result = runner.invoke(app, CASES[name](str(root)))

    assert result.exit_code == 2, result.output
    failure = json.loads(result.stdout)
    assert failure["ok"] is False and failure["problem"] == "damaged"
    assert failure["path"] == str(path)
    assert failure["error"].startswith(f".pymolt/{name.format(run=plan.plan_id)} (")
    assert "pymolt will not overwrite it" in failure["error"]
    assert failure["remedy"] and failure["hint"] == failure["remedy"]
    assert path.read_bytes() == DAMAGED
    assert not _ran(root)


@pytest.mark.parametrize("name", list(CASES))
def test_status_reports_a_damaged_state_file_as_a_problem_of_its_phase(applied, name):
    root, plan = applied
    path = root / ".pymolt" / name.format(run=plan.plan_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(DAMAGED)

    result = runner.invoke(app, ["status", str(root), "--json"])

    assert result.exit_code == 0, result.output
    status = json.loads(result.stdout)
    assert [p["path"] for p in status["problems"]] == [str(path)]
    problem = status["problems"][0]
    phase = next(p for p in status["phases"] if p["phase"] == problem["phase"])
    assert phase["state"] == "blocked" and phase["problem"]["path"] == str(path)
    assert status["next_command"] is None
    assert status["next_reason"] == problem["remedy"]
    assert path.read_bytes() == DAMAGED


def test_the_agent_status_view_carries_the_problem_and_its_remedy(applied):
    root, _plan = applied
    path = root / ".pymolt" / "migration_state.json"
    path.write_bytes(DAMAGED)

    result = runner.invoke(app, ["status", str(root), "--agent"])

    assert result.exit_code == 0, result.output
    view = json.loads(result.stdout)
    assert view["ok"] is True
    assert view["data"]["problems"][0]["kind"] == "apply receipt"
    assert view["data"]["next_command"] is None
    assert view["hint"] == view["data"]["problems"][0]["remedy"]
    assert "PROBLEM .pymolt/migration_state.json (apply receipt) is damaged" in view["summary"]


def test_status_reports_a_file_that_fails_validation(applied):
    root, _plan = applied
    path = root / ".pymolt" / "contract_state.json"
    path.write_text(json.dumps({"schema_version": 2, "baseline": {"events": "many"}}))

    status = json.loads(runner.invoke(app, ["status", str(root), "--json"]).stdout)

    assert status["problems"][0]["phase"] == "baseline"
    assert status["problems"][0]["detail"].startswith("baseline.")


def test_status_reports_a_large_file_it_scans_as_json_only_once_reading_it_fails(applied):
    root, _plan = applied
    path = root / ".pymolt" / "migration_plan.json"
    path.write_text(json.dumps({"schema_version": 2, "target_python": 5}))

    status = json.loads(runner.invoke(app, ["status", str(root), "--json"]).stdout)

    assert [(p["phase"], p["path"]) for p in status["problems"]] == [("plan", str(path))]
    assert status["next_command"] is None


def test_the_mcp_tools_return_the_cli_envelope_and_status_view(applied):
    pytest.importorskip("mcp")
    from pymolt.interfaces.mcp_server import Grants, Tools

    root, _plan = applied
    path = root / ".pymolt" / "verification.json"
    path.write_bytes(DAMAGED)
    tools = Tools(Grants(root=root.resolve()))

    failure = tools.verify(project_dir=str(root))
    view = tools.status(project_dir=str(root))

    assert failure["ok"] is False and failure["problem"] == "damaged"
    assert failure["kind"] == "saved verdict" and failure["hint"] == failure["remedy"]
    assert view["ok"] is True and view["data"]["problems"][0]["kind"] == "saved verdict"
    assert path.read_bytes() == DAMAGED


def test_a_newer_state_file_is_refused_with_upgrade_as_the_remedy(applied):
    root, _plan = applied
    path = root / ".pymolt" / "migration_state.json"
    data = json.loads(path.read_text())
    path.write_text(json.dumps({**data, "schema_version": 3}))
    before = path.read_bytes()

    result = runner.invoke(app, ["rollback", str(root)])

    assert result.exit_code == 2
    assert ("was written by a newer pymolt (schema 3; this pymolt reads up to 2). Upgrade "
            "pymolt.") in _flat(result.output)
    assert path.read_bytes() == before


def test_capture_refuses_before_running_the_command(tmp_path):
    plan_for(tmp_path, {M: (APP, RECIPES)}, save=False)
    state = tmp_path / ".pymolt" / "contract_state.json"
    state.write_bytes(DAMAGED)

    with pytest.raises(StateFileError):
        service.capture_named_trace(
            tmp_path, "post_migration", CaptureMode.LIVE_COMMAND,
            command=[sys.executable, "-c", "open('RAN', 'w')"], cwd=tmp_path)

    assert not _ran(tmp_path) and state.read_bytes() == DAMAGED


def test_a_human_capture_prints_the_file_and_the_remedy_and_exits_2(tmp_path):
    plan_for(tmp_path, {M: (APP, RECIPES)}, save=False)
    (tmp_path / ".pymolt" / "contract_state.json").write_bytes(DAMAGED)

    result = runner.invoke(app, ["capture", "baseline", "--project-dir", str(tmp_path),
                                 "--force", "--", sys.executable, "-c", "open('RAN', 'w')"])

    assert result.exit_code == 2
    assert ".pymolt/contract_state.json (capture state) is damaged" in _flat(result.output)
    assert "capture again" in _flat(result.output)
    assert not _ran(tmp_path)


# ── the capture state's schema 2 ─────────────────────────────────────────────────────────


def test_a_version_1_capture_state_loads_and_is_saved_as_version_2(tmp_path):
    plan_for(tmp_path, {M: (APP, RECIPES)}, save=False)
    path = tmp_path / ".pymolt" / "contract_state.json"
    data = json.loads(path.read_text())
    trace = (tmp_path / data["baseline"]["trace_path"]).resolve()
    data.update(schema_version=1)
    data["baseline"]["trace_path"] = str(trace)          # version 1: absolute paths
    path.write_text(json.dumps(data))

    state = service.load_contract_state(tmp_path)
    assert state.baseline is not None and state.baseline.trace_path == str(trace)
    state.save(path)

    saved = json.loads(path.read_text())
    assert CONTRACT_STATE_SCHEMA_VERSION == 2 and saved["schema_version"] == 2
    assert saved["baseline"]["trace_path"] == trace.relative_to(tmp_path.resolve()).as_posix()


def test_a_version_3_capture_state_is_refused_as_newer(tmp_path):
    path = tmp_path / ".pymolt" / "contract_state.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"schema_version": 3}))

    with pytest.raises(StateFileError) as info:
        ContractState.load(path)
    assert info.value.problem == "newer"
    with pytest.raises(StateFileError):
        service.load_contract_state(tmp_path)
