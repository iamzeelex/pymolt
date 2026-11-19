"""The MCP ``apply`` and ``rollback`` tools (design 3.6, variant A): they write only on a plan a
person approved for agents, take none of the parameters an agent never sets, run the import
check only when the person started the server with ``--allow-run``, and return what
``pymolt apply --agent`` / ``pymolt rollback --agent`` print."""
from __future__ import annotations

import inspect
import json

import pytest
from _stepwise import APP, RECIPES, manual_step, plan_for, read, with_steps
from _verifying import person
from typer.testing import CliRunner

from pymolt.interfaces.cli.commands import app
from pymolt.migration_state import MigrationReceipt
from pymolt.workorder.apply import apply_steps
from pymolt.workorder.approval import approve

pytest.importorskip("mcp")

from pymolt.interfaces.mcp_server import Grants, Tools, build_server  # noqa: E402

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


# ── MCP ───────────────────────────────────────────────────────────────────────────────────


FORBIDDEN = ("force", "cascade", "approve_agent", "claim", "expect", "pair",
             "allow_no_baseline")


@pytest.fixture
def mcp(tmp_path):
    """The tools of a server rooted at tmp_path, started without --allow-run."""
    return Tools(Grants(root=tmp_path.resolve()))


def test_the_write_tools_never_take_the_forbidden_parameters(tmp_path):
    import asyncio

    for name in ("apply", "rollback"):
        params = set(inspect.signature(getattr(Tools, name)).parameters)
        assert not params & set(FORBIDDEN), name
    server = build_server(Grants(root=tmp_path.resolve()))
    for name, bad in (("apply", {"claim": "intended"}), ("apply", {"expect": "raise:X"}),
                      ("apply", {"force": True}), ("rollback", {"force": True}),
                      ("rollback", {"cascade": True}), ("apply", {"approve_agent": True})):
        with pytest.raises(Exception, match="Extra inputs are not permitted"):
            asyncio.run(server.call_tool(name, {"project_dir": str(tmp_path), **bad}))


def test_without_a_persons_approval_apply_changes_nothing(mcp, tmp_path):
    _plan(tmp_path)
    view = mcp.apply(project_dir=str(tmp_path))
    assert view["ok"] is False and "approved for agents" in view["error"]
    # Reading is not writing: a dry run and a brief need no approval.
    assert mcp.apply(project_dir=str(tmp_path), dry_run=True)["ok"] is True
    assert mcp.apply(project_dir=str(tmp_path), step="sig-1", brief=True)["step"] == "sig-1"
    assert read(tmp_path, M) == APP


def test_without_a_persons_approval_rollback_changes_nothing(mcp, tmp_path):
    plan = _plan(tmp_path)
    apply_steps(tmp_path, plan, step="mech-1")  # a person applied it
    applied = read(tmp_path, M)
    refused = mcp.rollback(project_dir=str(tmp_path), step="mech-1")
    assert refused["ok"] is False and "approved for agents" in refused["error"]
    assert read(tmp_path, M) == applied != APP


def test_without_allow_run_apply_runs_no_import_check_and_says_why(mcp, tmp_path):
    plan = _approved(tmp_path)
    view = mcp.apply(project_dir=str(tmp_path))
    assert view["ok"] is True, view
    note = "not checked: MCP server started without --allow-run"
    assert view["data"]["checks"]["mech-1"]["smoke_import"] == note
    receipt = MigrationReceipt.load(tmp_path)
    assert receipt.steps[f"{plan.plan_id}:mech-1"].checks["smoke_import"] == note


def test_with_allow_run_apply_asks_for_the_import_check(tmp_path, monkeypatch):
    _approved(tmp_path)
    seen = {}

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return apply_steps(*args, **{**kwargs, "smoke": False})

    monkeypatch.setattr("pymolt.workorder.apply.apply_steps", spy)
    view = Tools(Grants(root=tmp_path.resolve(), allow_run=True)).apply(project_dir=str(tmp_path))
    assert view["ok"] is True, view
    assert seen["smoke"] is True and seen["smoke_reason"] is None and seen["agent"] is True


def _normalize(view, root, plan_id):
    import re

    text = json.dumps(view, sort_keys=True).replace(
        "not checked: MCP server started without --allow-run", "not checked: not requested")
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
