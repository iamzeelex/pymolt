"""Agents running the plan (design 3.6, variant A): a person's approval per plan, voided by a
re-plan or any change to the plan; ``apply --agent`` and ``rollback --agent --step`` on an
approved plan; the full apply view with the next brief. The MCP ``apply`` and ``rollback``
tools are in test_mcp_write_tools.py."""
from __future__ import annotations

import json

import pytest
from _stepwise import APP, RECIPES, manual_step, plan_for, plan_next, read, rename, with_steps
from _verifying import person
from typer.testing import CliRunner

from pymolt.attest import Attester
from pymolt.interfaces.cli.commands import app
from pymolt.migration_plan import MigrationConflictError
from pymolt.migration_state import MigrationReceipt
from pymolt.workorder.apply import StepRefusal, apply_steps
from pymolt.workorder.approval import approval_path, approve, check_approval
from pymolt.workorder.rollback import rollback_steps

M = "app/m.py"
runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_compile(monkeypatch):
    monkeypatch.setattr("pymolt.apply_check.compile_changes", lambda root, plan: [])


def _plan(root):
    plan = plan_for(root, {M: (APP, RECIPES)})
    return with_steps(root, plan, manual_step(root, M, 7, sid="sig-1", text=APP,
                                              symbol=("a.k", None)))


def _approved(root):
    plan = _plan(root)
    approve(root, plan, attester=person(root))
    return plan


# ── the approval ──────────────────────────────────────────────────────────────────────────


def test_an_agent_applies_pymolt_steps_only_on_an_approved_plan(tmp_path):
    plan = _plan(tmp_path)
    with pytest.raises(StepRefusal, match="approved for agents, and no person approved"):
        apply_steps(tmp_path, plan, agent=True)
    assert apply_steps(tmp_path, plan, agent=True, dry_run=True).outcome == "dry_run"

    approval = approve(tmp_path, plan, attester=person(tmp_path))

    assert approval["scope"] == ["pymolt-steps", "rollback-step"]
    saved = json.loads(approval_path(tmp_path, plan.plan_id).read_text())
    assert saved["attestation"].startswith("att-") and saved["plan_id"] == plan.plan_id
    result = apply_steps(tmp_path, plan, agent=True)
    assert result.outcome == "applied" and result.applied == ["mech-1", "mech-2", "mech-3"]


def test_no_person_no_approval(tmp_path):
    plan = _plan(tmp_path)
    with pytest.raises(MigrationConflictError, match="person's attested act"):
        approve(tmp_path, plan, attester=Attester(prompter=None))
    with pytest.raises(MigrationConflictError, match="nonce was not typed back"):
        approve(tmp_path, plan, attester=person(tmp_path, types=False))
    assert not approval_path(tmp_path, plan.plan_id).exists()


def test_a_re_plan_voids_the_approval(tmp_path):
    plan = _approved(tmp_path)
    apply_steps(tmp_path, plan, agent=True, step="mech-1")
    replanned = plan_next(tmp_path, {M: [rename("a.h", "a.k")]})
    with pytest.raises(StepRefusal, match="no person approved"):
        apply_steps(tmp_path, replanned, agent=True)


def test_any_change_to_the_plan_voids_the_approval(tmp_path):
    plan = _approved(tmp_path)
    changed = plan.model_copy(update={"steps": [
        s.model_copy(update={"title": s.title + "!"}) for s in plan.steps]})
    changed.save(tmp_path)
    assert check_approval(tmp_path, changed, plan.plan_id, "pymolt-steps") == (
        "the plan changed since it was approved")
    with pytest.raises(StepRefusal, match="the plan changed since it was approved"):
        apply_steps(tmp_path, changed, agent=True)


def test_an_approval_written_by_hand_does_not_hold(tmp_path):
    plan = _plan(tmp_path)
    from pymolt.workorder.approval import SCOPE, plan_digest

    path = approval_path(tmp_path, plan.plan_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"plan_id": plan.plan_id, "plan_sha256": plan_digest(plan),
                                "run_id": plan.plan_id, "scope": list(SCOPE),
                                "attestation": "att-0123456789abcdef"}))
    with pytest.raises(StepRefusal, match="attestation does not hold"):
        apply_steps(tmp_path, plan, agent=True)


def test_cli_approve_agent_is_a_person_s(tmp_path):
    _plan(tmp_path)
    agent = runner.invoke(app, ["apply", str(tmp_path), "--approve-agent", "--agent"])
    assert agent.exit_code == 2 and "refused under --agent" in agent.stdout
    plain = runner.invoke(app, ["apply", str(tmp_path), "--approve-agent"])
    assert plain.exit_code == 2 and "not a terminal" in plain.output
    refused = runner.invoke(app, ["apply", str(tmp_path), "--agent"])
    assert refused.exit_code == 2 and "--approve-agent" in refused.stdout


# ── rollback --agent ──────────────────────────────────────────────────────────────────────


def test_an_agent_rolls_back_one_step_of_an_approved_plan(tmp_path):
    plan = _approved(tmp_path)
    apply_steps(tmp_path, plan, agent=True, through="mech-2")
    with pytest.raises(MigrationConflictError, match="a person rolls back a whole run"):
        rollback_steps(tmp_path, agent=True)
    with pytest.raises(MigrationConflictError, match="--force is refused"):
        rollback_steps(tmp_path, agent=True, force=True)
    with pytest.raises(MigrationConflictError, match="--cascade is refused"):
        rollback_steps(tmp_path, agent=True, step="mech-1", cascade=True)
    result = rollback_steps(tmp_path, agent=True, step="mech-2")
    assert result.steps == ["mech-2"]


def test_an_agent_cannot_roll_back_on_an_unapproved_plan(tmp_path):
    plan = _plan(tmp_path)
    apply_steps(tmp_path, plan)                                  # a person applied it
    with pytest.raises(StepRefusal, match="no person approved"):
        rollback_steps(tmp_path, agent=True, step="mech-3")
    assert MigrationReceipt.load(tmp_path).steps[f"{plan.plan_id}:mech-3"].in_effect


def test_a_whole_run_rollback_through_verify_is_refused_to_an_agent(tmp_path):
    """`verify --auto-rollback` rolls back the whole run, so under --agent it is refused
    as `rollback --agent` without --step is, before anything is compared or written."""
    plan = _approved(tmp_path)
    apply_steps(tmp_path, plan, agent=True)
    applied = read(tmp_path, M)
    (tmp_path / "policy.json").write_text("{}")
    (tmp_path / "metrics.json").write_text("{}")

    out = runner.invoke(app, ["verify", str(tmp_path), "--policy", str(tmp_path / "policy.json"),
                              "--metrics", str(tmp_path / "metrics.json"), "--auto-rollback",
                              "--agent"])

    payload = json.loads(out.stdout)
    assert out.exit_code == 2 and payload["ok"] is False
    assert "--auto-rollback is refused under --agent" in payload["error"]
    assert "--step ID --agent" in payload["hint"]
    assert read(tmp_path, M) == applied
    assert MigrationReceipt.load(tmp_path).steps[f"{plan.plan_id}:mech-1"].in_effect


def test_rollback_migration_refuses_an_agent_a_whole_run(tmp_path):
    from pymolt.migration_plan import rollback_migration

    plan = _approved(tmp_path)
    apply_steps(tmp_path, plan, agent=True)
    with pytest.raises(MigrationConflictError, match="a person rolls back a whole run"):
        rollback_migration(tmp_path, agent=True)
    assert MigrationReceipt.load(tmp_path).steps[f"{plan.plan_id}:mech-1"].in_effect


# ── the view ──────────────────────────────────────────────────────────────────────────────


def test_the_apply_view_names_the_next_command_and_brief(tmp_path):
    _approved(tmp_path)
    out = runner.invoke(app, ["apply", str(tmp_path), "--agent"])
    assert out.exit_code == 0, out.stdout
    data = json.loads(out.stdout)["data"]
    assert [a["step"] for a in data["applied"]] == ["mech-1", "mech-2", "mech-3"]
    assert data["refused"] == {}
    nxt = data["next"]
    # The project dir as given: outside the current directory, so as an absolute path.
    from pymolt.status import shown_dir

    assert nxt["command"] == f"pymolt apply {shown_dir(tmp_path)} --step env-1 --record"
    assert nxt["step"] == "env-1"
    assert nxt["brief"]["step"] == "env-1"
    # The smoke import is on by default; this project names no target environment.
    assert data["checks"]["mech-1"]["smoke_import"].startswith(
        "not checked: setup names no target environment")


def test_a_refusal_of_what_a_person_does_says_so_and_is_no_conflict(tmp_path):
    """QA H5: `--skip --agent` is refused by policy, not by the project's state: no "conflicts
    with current state" prefix, and the hint says a person runs it, not "regenerate the plan"."""
    _plan(tmp_path)
    out = runner.invoke(app, ["apply", str(tmp_path), "--step", "sig-1", "--skip",
                              "--reason", "not needed", "--agent"])
    payload = json.loads(out.stdout)
    assert out.exit_code == 2 and payload["ok"] is False
    assert payload["error"] == "--skip is refused under --agent: a person declines a step"
    assert payload["hint"] == "A person runs the same command on a terminal, without --agent."
    whole = runner.invoke(app, ["rollback", str(tmp_path), "--agent"])
    assert not json.loads(whole.stdout)["error"].startswith("migration rollback conflicts")
    usage = runner.invoke(app, ["apply", str(tmp_path), "--step", "mech-1", "--through",
                                "mech-2", "--json"])
    assert json.loads(usage.stdout)["error"] == "--step and --through cannot be combined"
    # A real conflict keeps its prefix: an unknown step is the plan's state.
    unknown = runner.invoke(app, ["apply", str(tmp_path), "--step", "nope", "--json"])
    assert json.loads(unknown.stdout)["error"].startswith(
        "migration plan conflicts with current state: unknown step nope")
