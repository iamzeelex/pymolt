"""Agents running the plan (design 3.6, variant A): a person's approval per plan, voided by a
re-plan or any change to the plan; ``apply --agent`` and ``rollback --agent --step`` on an
approved plan; the full apply view with the next brief; and the MCP ``apply`` and
``rollback`` tools, opt-in, without the parameters an agent never sets, with the smoke-import
note on their receipts."""
from __future__ import annotations

import inspect
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


# ── the view ──────────────────────────────────────────────────────────────────────────────


def test_the_apply_view_names_the_next_command_and_brief(tmp_path):
    _approved(tmp_path)
    out = runner.invoke(app, ["apply", str(tmp_path), "--agent"])
    assert out.exit_code == 0, out.stdout
    data = json.loads(out.stdout)["data"]
    assert [a["step"] for a in data["applied"]] == ["mech-1", "mech-2", "mech-3"]
    assert data["refused"] == {}
    nxt = data["next"]
    assert nxt["command"] == "pymolt apply --step env-1 --record" and nxt["step"] == "env-1"
    assert nxt["brief"]["step"] == "env-1"
    assert data["checks"]["mech-1"]["smoke_import"] == "not checked: not requested"


# ── MCP ───────────────────────────────────────────────────────────────────────────────────


mcp_server = pytest.importorskip("pymolt.interfaces.mcp_server")
FORBIDDEN = ("force", "cascade", "approve_agent", "claim", "expect", "pair",
             "allow_no_baseline")


@pytest.fixture
def mcp(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_server, "_WORKSPACE_ROOT", tmp_path.resolve())
    monkeypatch.delenv("PYMOLT_MCP_ALLOW_EXEC", raising=False)
    monkeypatch.setenv("PYMOLT_MCP_ALLOW_WRITE", "1")
    return mcp_server


def test_the_write_tools_never_take_the_forbidden_parameters(mcp, tmp_path):
    import asyncio

    for name in ("apply", "rollback"):
        params = set(inspect.signature(getattr(mcp, name)).parameters)
        assert not params & set(FORBIDDEN), name
    for name, bad in (("apply", {"claim": "intended"}), ("apply", {"expect": "raise:X"}),
                      ("apply", {"force": True}), ("rollback", {"force": True}),
                      ("rollback", {"cascade": True}), ("apply", {"approve_agent": True})):
        with pytest.raises(Exception, match="Extra inputs are not permitted"):
            asyncio.run(mcp.mcp.call_tool(name, {"project_dir": str(tmp_path), **bad}))


def test_the_write_tools_are_opt_in(mcp, tmp_path, monkeypatch):
    _approved(tmp_path)
    monkeypatch.delenv("PYMOLT_MCP_ALLOW_WRITE")
    refused = mcp.apply(project_dir=str(tmp_path))
    assert refused["ok"] is False and "PYMOLT_MCP_ALLOW_WRITE" in refused["hint"]
    assert mcp.rollback(project_dir=str(tmp_path), step="mech-1")["ok"] is False
    # Reading is not writing: a dry run and a brief need no opt-in.
    assert mcp.apply(project_dir=str(tmp_path), dry_run=True)["ok"] is True
    assert mcp.apply(project_dir=str(tmp_path), step="sig-1", brief=True)["step"] == "sig-1"
    assert read(tmp_path, M) == APP


def test_mcp_apply_records_the_smoke_import_note(mcp, tmp_path):
    plan = _approved(tmp_path)
    view = mcp.apply(project_dir=str(tmp_path))
    assert view["ok"] is True, view
    note = "not checked: MCP without PYMOLT_MCP_ALLOW_EXEC"
    assert view["data"]["checks"]["mech-1"]["smoke_import"] == note
    receipt = MigrationReceipt.load(tmp_path)
    assert receipt.steps[f"{plan.plan_id}:mech-1"].checks["smoke_import"] == note


def test_mcp_apply_on_an_unapproved_plan_is_refused(mcp, tmp_path):
    _plan(tmp_path)
    view = mcp.apply(project_dir=str(tmp_path))
    assert view["ok"] is False and "approved for agents" in view["error"]


def _normalize(view, root, plan_id):
    import re

    text = json.dumps(view, sort_keys=True).replace(
        "not checked: MCP without PYMOLT_MCP_ALLOW_EXEC", "not checked: not requested")
    text = text.replace(str(root.resolve()), "<root>").replace(str(root), "<root>")
    text = text.replace(plan_id, "<plan>").replace(plan_id[:8], "<plan8>")
    text = re.sub(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+\-:\d]*", "<at>", text)
    return json.loads(text)


@pytest.mark.parametrize("case", ["dry_run", "record", "done", "brief", "apply", "rollback"])
def test_mcp_tool_returns_the_cli_agent_view(mcp, tmp_path, case):
    """CLI <-> MCP parity for the write tools (the smoke-import note aside, which says why
    the MCP side did not run it)."""
    plans = {}
    for side in ("a", "b"):
        (tmp_path / side).mkdir()
        plans[side] = _approved(tmp_path / side)
    calls = {
        "dry_run": (["apply", "{p}", "--dry-run"], dict(dry_run=True)),
        "record": (["apply", "{p}", "--step", "sig-1", "--record"],
                   dict(step="sig-1", record=True)),
        "done": (["apply", "{p}", "--step", "sig-1", "--record", "--done"],
                 dict(step="sig-1", record=True, done=True)),
        "brief": (["apply", "{p}", "--step", "sig-1", "--brief"],
                  dict(step="sig-1", brief=True)),
        "apply": (["apply", "{p}", "--step", "mech-1"], dict(step="mech-1")),
        "rollback": (["rollback", "{p}", "--step", "mech-1"], dict(step="mech-1")),
    }
    argv, kwargs = calls[case]
    views = []
    for side in ("a", "b"):
        root, plan = tmp_path / side, plans[side]
        if case == "done":
            from pymolt.workorder.record import record_step

            record_step(root, plan, "sig-1", agent=True)
            (root / M).write_text(APP.replace("return a.h(x)", "return a.h(x) or 0"))
        if case == "rollback":
            apply_steps(root, plan, agent=True, step="mech-1")
        if side == "a":
            out = runner.invoke(app, [x.format(p=str(root)) for x in argv] + ["--agent"])
            views.append(json.loads(out.stdout))
        else:
            tool = mcp.rollback if case == "rollback" else mcp.apply
            views.append(tool(project_dir=str(root), **kwargs))
    cli, via_mcp = (_normalize(v, tmp_path / s, plans[s].plan_id)
                    for v, s in zip(views, "ab", strict=True))
    assert cli.get("ok", True) is not False, cli
    assert via_mcp == cli
