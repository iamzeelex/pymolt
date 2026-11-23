"""Tests for the optional stdio MCP adapter (pymolt/interfaces/mcp_server.py).

The whole module is skipped when the `mcp` extra isn't installed (CI installs it). The tools
are methods of ``Tools``, bound to the person's grants, so we call them directly. The parity
tests at the bottom are the adapter's contract: every tool returns exactly what
`pymolt <command> --agent` prints for the same project. The write tools (apply, rollback) are
tested in tests/workorder/test_mcp_write_tools.py.
"""
from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

pytest.importorskip("mcp")

from pymolt.interfaces import mcp_server  # noqa: E402
from pymolt.interfaces.cli.commands import app  # noqa: E402
from pymolt.interfaces.mcp_server import Grants, Tools, build_server  # noqa: E402
from pymolt.migration_plan import MigrationPlan  # noqa: E402

_DEFAULT_TOOLS = {"status", "scan", "setup", "assess", "plan", "verify", "apply", "rollback"}
_ALL_TOOLS = _DEFAULT_TOOLS | {"capture"}
runner = CliRunner()


def _make_project(tmp_path):
    """A minimal but real project root: one manifest + one source file."""
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\n", encoding="utf-8")
    (tmp_path / "app.py").write_text(
        "import requests\n\n\ndef fetch():\n    return requests.get('http://x')\n",
        encoding="utf-8",
    )
    return tmp_path


def _configured_project(tmp_path):
    from pymolt.ingestion.config import EnvConfig, ToolChoice

    project = _make_project(tmp_path)
    (project / "requirements-target.txt").write_text("requests==2.32.0\n", encoding="utf-8")
    EnvConfig(
        selected_manifest="requirements.txt", selected_tool=ToolChoice.UV,
        base_python="3.11", base_python_source="declared", target_python="3.12",
    ).save(project / ".pymolt" / "env_config.json")
    return project


@pytest.fixture
def tools(tmp_path):
    """The tools of a server rooted at this test's tmp_path, started with --allow-run."""
    return Tools(Grants(root=tmp_path.resolve(), allow_run=True))


def _names(server) -> set[str]:
    return {t.name for t in asyncio.run(server.list_tools())}


# ── what the person grants ────────────────────────────────────────────────────


def test_without_allow_run_the_agent_has_no_capture_tool(tmp_path):
    server = build_server(Grants(root=tmp_path))
    assert _names(server) == _DEFAULT_TOOLS
    assert "--allow-run" in server.instructions


def test_allow_run_adds_capture(tmp_path):
    server = build_server(Grants(root=tmp_path, allow_run=True))
    assert _names(server) == _ALL_TOOLS
    assert "--allow-run" not in server.instructions


def test_the_tools_that_change_things_say_so(tmp_path):
    """Annotations let a client ask the person before a call that runs programs or
    changes source."""
    listed = asyncio.run(build_server(Grants(root=tmp_path, allow_run=True)).list_tools())
    destructive = {t.name for t in listed if t.annotations.destructiveHint}
    assert destructive == {"capture", "apply", "rollback"}


def test_every_tool_refuses_an_argument_it_does_not_take(tmp_path):
    server = build_server(Grants(root=tmp_path, allow_run=True))
    for name in _ALL_TOOLS:
        with pytest.raises(Exception, match="Extra inputs are not permitted"):
            asyncio.run(server.call_tool(name, {"project_dir": str(tmp_path), "force_it": 1}))


def test_the_cli_passes_the_grants_to_the_server(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(mcp_server, "run", lambda **kw: seen.update(kw))
    result = runner.invoke(app, ["mcp", "--serve", "--root", str(tmp_path), "--allow-run"])
    assert result.exit_code == 0, result.output
    assert seen == {"root": str(tmp_path), "allow_run": True}
    seen.clear()
    runner.invoke(app, ["mcp", "--serve"])
    assert seen == {"root": None, "allow_run": False}


def test_dir_taking_tools_use_project_dir_param():
    """The directory arg is named `project_dir`, as in the CLI (regression guard for `dir`)."""
    for name in _ALL_TOOLS:
        params = inspect.signature(getattr(Tools, name)).parameters
        assert "project_dir" in params, f"{name} lost its project_dir param"
        assert "dir" not in params


def test_scan_description_tells_the_agent_to_expect_a_monorepo():
    """A tool description is the only text that reaches the model (measured: the
    server `instructions` moved no request tokens). When it stopped naming the
    monorepo case, agents dropped nested requirements-only roots."""
    doc = Tools.scan.__doc__ or ""
    assert "monorepo" in doc.lower()
    assert "requirements.txt" in doc
    assert "EVERY project root" in doc


# ── shapes and failures ──────────────────────────────────────────────────────


def test_scan_returns_contract_shape_and_writes_report(tmp_path, tools):
    result = tools.scan(project_dir=str(_make_project(tmp_path)))
    assert set(result) == {"ok", "summary", "data", "full_report_path", "hint"}
    assert result["ok"] is True and result["data"]["root_count"] >= 1
    written = Path(result["full_report_path"])
    assert written.parts[-3:] == (".pymolt", "agent", "scan.json")
    json.loads(written.read_text(encoding="utf-8"))
    json.dumps(result)


def test_scan_reports_the_call_sites_into_dependencies(tmp_path, tools):
    result = tools.scan(project_dir=str(_make_project(tmp_path)))
    assert result["data"]["contacts"]["total"] >= 1
    assert any(row.startswith("requests ") for row in result["data"]["contacts"]["per_dependency"])
    assert "call site" in result["summary"]


def test_scan_finds_a_nested_requirements_only_root(tmp_path, tools):
    (tmp_path / "setup.py").write_text("from setuptools import setup\nsetup(name='top')\n")
    (tmp_path / "requirements.txt").write_text("flask>=1.0\n")
    demo = tmp_path / "demo_app"
    demo.mkdir()
    (demo / "requirements.txt").write_text("flask>=2.0\n")
    (demo / "app.py").write_text("import flask\n")
    res = tools.scan(project_dir=str(tmp_path))
    assert "demo_app" in {r["path"] for r in res["data"]["roots"]}


def test_scan_nonexistent_dir_returns_error(tmp_path, tools):
    result = tools.scan(project_dir=str(tmp_path / "does-not-exist"))
    assert result["ok"] is False
    assert "does not exist" in result["error"].lower()
    assert "hint" in result and "summary" not in result


def test_every_tool_handles_nonexistent_dir(tmp_path, tools):
    missing = str(tmp_path / "nope")
    for name in _ALL_TOOLS:
        kwargs = {"command": ["pytest"]} if name == "capture" else {}
        result = getattr(tools, name)(project_dir=missing, **kwargs)
        assert result["ok"] is False and result["error"], name


def test_every_tool_stays_inside_the_root_the_person_gave(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    (root / "sub").mkdir(parents=True)
    outside.mkdir()
    _make_project(outside)
    confined = Tools(Grants(root=root.resolve(), allow_run=True))
    assert confined.scan(project_dir=str(root / "sub"))["ok"] is True
    for name in _ALL_TOOLS:
        kwargs = {"command": ["pytest"]} if name == "capture" else {}
        result = getattr(confined, name)(project_dir=str(outside), **kwargs)
        assert result["ok"] is False and "outside the workspace root" in result["error"], name
        assert "--root" in result["hint"], name
    assert not (outside / ".pymolt").exists()


def test_capture_before_setup_is_refused(tmp_path, tools):
    project = _make_project(tmp_path)
    result = tools.capture(project_dir=str(project), command=["pytest"])
    assert result["ok"] is False and "not configured" in result["error"]
    assert not (project / ".pymolt" / "contract_state.json").exists()


def test_setup_lists_then_saves(tmp_path, tools):
    project = _make_project(tmp_path)
    listing = tools.setup(project_dir=str(project))
    options = listing["data"]["options"]
    assert listing["ok"] is True and "requirements.txt" in options["manifests"]
    assert not (project / ".pymolt" / "env_config.json").exists()

    saved = tools.setup(
        project_dir=str(project), manifest="requirements.txt", tool=options["tools"][0],
        base_python="3.8", target_python=options["target_python_options"][0],
        target_env=".venv-target",
    )
    assert saved["ok"] is True, saved
    config = json.loads((project / ".pymolt" / "env_config.json").read_text(encoding="utf-8"))
    assert config["target_env_path"] == ".venv-target"


def test_setup_rejects_invalid_or_partial_choices(tmp_path, tools):
    project = str(_make_project(tmp_path))
    bad = tools.setup(project_dir=project, manifest="bogus.txt", tool="uv",
                           base_python="3.8", target_python="3.12")
    assert bad["ok"] is False and "requirements.txt" in bad["hint"]
    partial = tools.setup(project_dir=project, manifest="requirements.txt")
    assert "tool" in partial["error"] and "target_python" in partial["error"]


def test_plan_before_setup_says_what_to_run(tmp_path, tools):
    result = tools.plan(project_dir=str(_make_project(tmp_path)))
    assert result["ok"] is False and "not configured" in result["error"]
    assert "setup" in result["hint"]


# ── fakes for the slow or external parts (resolve, run, codemods) ────────────


def _fake_resolution(monkeypatch):
    from pymolt.core.enums import Mode, Provenance, ResolutionQuality, SourceFixation
    from pymolt.core.graph import DependencyGraph, Node
    from pymolt.core.layers import IngestionReport

    def graph(version):
        node = Node(name="requests", version=version, mode=Mode.PYPI,
                    provenance=Provenance.PYPI, direct=True)
        return DependencyGraph(nodes={"requests": node},
                               resolution_quality=ResolutionQuality.RESOLVED,
                               source_fixation=SourceFixation.PINNED)

    def baseline(*_a, **_k):
        return graph("2.31.0"), IngestionReport(
            resolution_quality="resolved", source_fixation="pinned", manual_zone=[],
            warnings=["a warning the view must keep"], detected_python="3.11")

    def target(*_a, **_k):
        return graph("2.32.0"), None

    monkeypatch.setattr("pymolt.assess.service.orchestrate_ingestion", baseline)
    monkeypatch.setattr("pymolt.assess.service.resolve_target_graph", target)
    monkeypatch.setattr("pymolt.interfaces.cli.commands._resolve_target_graph", target)
    monkeypatch.setattr("pymolt.assess.service.fetch_pypi_hashes", lambda *a, **k: [])
    monkeypatch.setattr("pymolt.interfaces.cli.commands.get_eol_versions", lambda: {})


def _fake_capture(monkeypatch):
    """Persist a fixed, valid slot instead of running the command."""
    from pymolt.verify import service
    from pymolt.verify.models import CaptureValidity, ContractSlot

    def capture(project_dir, when, mode, **kwargs):
        trace = Path(project_dir) / ".pymolt" / f"{when}.jsonl"
        trace.parent.mkdir(parents=True, exist_ok=True)
        trace.write_text("", encoding="utf-8")
        slot = ContractSlot(trace_path=str(trace), captured_at="2026-11-14T12:00:00+00:00",
                            mode=mode, command=list(kwargs["command"]), events=3, processes=1,
                            returncode=0, validity=CaptureValidity.VALID, env_note="local")
        state = service.load_contract_state(project_dir)
        setattr(state, when, slot)
        state.save(service._state_path(project_dir))
        return slot

    monkeypatch.setattr(service, "capture_named_trace", capture)
    return capture


def _fake_codemods(monkeypatch, project):
    from pymolt.codemods import service as codemod_service
    from pymolt.codemods.models import CodemodPattern, DependencyMigration, FilePreview

    source = (project / "app.py").read_text(encoding="utf-8")
    pattern = CodemodPattern(old_qualname="requests.get", new_qualname="requests.api.get",
                             kind="rewrite-import")
    preview = FilePreview(path=str(project / "app.py"), old_source=source,
                          new_source=source.replace("requests.get", "requests.api.get"),
                          sites=1, patterns=[pattern])
    monkeypatch.setattr(
        codemod_service, "resolve_codemod_migrations",
        lambda *a, **k: ([DependencyMigration("requests", "2.31.0", "2.32.0")], "target"))
    monkeypatch.setattr(codemod_service, "preview_codemods",
                        lambda *a, **k: ({"requests": [pattern]}, [preview]))
    return source


def test_assess_never_says_feasible_and_keeps_warnings(tmp_path, monkeypatch, tools):
    _fake_resolution(monkeypatch)
    project = _configured_project(tmp_path)
    (project / "Dockerfile").write_text("FROM python:3.11\n", encoding="utf-8")
    result = tools.assess(project_dir=str(project))
    assert result["ok"] is True, result
    data = result["data"]
    assert data["changes"] == ["requests 2.31.0>2.32.0 upgrade"]
    assert "a warning the view must keep" in data["warnings"]
    assert data["verdict"]["title"].startswith("GOLD base")
    assert "feasible" not in result["summary"].lower()
    assert "python:3.12" in data["target_env"]["suggested_dockerfile"]


def test_plan_saves_the_plan_and_writes_no_source(tmp_path, monkeypatch, tools):
    project = _configured_project(tmp_path)
    source = _fake_codemods(monkeypatch, project)
    result = tools.plan(project_dir=str(project))
    assert result["ok"] is True, result
    plan = MigrationPlan.load(project)
    assert (project / ".pymolt" / "plans" / plan.name / "plan.json").is_file()
    assert (project / ".pymolt" / "plans" / plan.name / "PLAN.md").is_file()
    assert not (project / ".pymolt" / "migration_plan.json").exists()
    assert result["data"]["files_to_change"] == ["app.py"]
    assert "requests.api.get" in result["data"]["diffs"][0]["diff"]
    assert (project / "app.py").read_text(encoding="utf-8") == source
    assert "pymolt apply" in result["hint"]


# ── parity: MCP tool == `pymolt <command> --agent` ────────────────────────────


def _normalize(view: dict) -> dict:
    """Plan ids are random per plan; everything else must match byte for byte."""
    text = json.dumps(view, sort_keys=True)
    plan_id = (view.get("data") or {}).get("plan_id")
    if plan_id:
        text = text.replace(plan_id, "<plan-id>").replace(plan_id[:8], "<plan-id8>")
    return json.loads(text)


def _parity_cases(tools, p: str):
    """(CLI argv, MCP call) per tool; the CLI side gets --agent."""
    return {
        "status": (["status", p], lambda: tools.status(project_dir=p)),
        "scan": (["scan", p], lambda: tools.scan(project_dir=p)),
        "setup": (["setup", p], lambda: tools.setup(project_dir=p)),
        "setup-save": (
            ["setup", p, "--manifest", "requirements.txt", "--tool", "system",
             "--base-python", "3.11", "--target-python", "3.12"],
            lambda: tools.setup(project_dir=p, manifest="requirements.txt", tool="system",
                                     base_python="3.11", target_python="3.12")),
        "assess": (["assess", p], lambda: tools.assess(project_dir=p)),
        "capture": (
            ["capture", "baseline", "--mode", "tests", "--project-dir", p, "--force",
             "--agent", "--", "pytest"],
            lambda: tools.capture(project_dir=p, when="baseline", mode="tests",
                                       command=["pytest"], force=True)),
        "plan": (["plan", p], lambda: tools.plan(project_dir=p)),
        "verify": (["verify", p], lambda: tools.verify(project_dir=p)),
    }


@pytest.mark.parametrize("case", list(_parity_cases(None, ".")))
def test_mcp_tool_returns_the_cli_agent_view(case, tmp_path, monkeypatch, tools):
    project = _configured_project(tmp_path)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    _fake_resolution(monkeypatch)
    capture = _fake_capture(monkeypatch)
    _fake_codemods(monkeypatch, project)
    if case == "verify":
        from pymolt.verify.models import CaptureMode

        for when in ("baseline", "post_migration"):
            capture(project, when, CaptureMode.TEST_SUITE, command=["pytest"])

    argv, call = _parity_cases(tools, str(project))[case]
    if "--agent" not in argv:
        argv = [*argv, "--agent"]
    result = runner.invoke(app, argv)
    cli_view = json.loads(result.stdout)
    mcp_view = call()

    assert cli_view["ok"] is True, cli_view
    assert _normalize(mcp_view) == _normalize(cli_view)


def test_cli_agent_capture_never_prompts_over_an_existing_capture(tmp_path, monkeypatch):
    """A prompt on stdout would corrupt the JSON: machine output fails and names --force."""
    project = _configured_project(tmp_path)
    capture = _fake_capture(monkeypatch)
    from pymolt.verify.models import CaptureMode

    capture(project, "baseline", CaptureMode.TEST_SUITE, command=["pytest"])
    result = runner.invoke(app, ["capture", "baseline", "--mode", "tests", "--project-dir",
                                 str(project), "--agent", "--", "pytest"])
    payload = json.loads(result.stdout)
    assert result.exit_code == 2
    assert payload["ok"] is False and "--force" in payload["hint"]


def test_cli_setup_json_is_the_full_payload(tmp_path):
    project = _make_project(tmp_path)
    result = runner.invoke(app, ["setup", str(project), "--json"])
    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload["saved"] is False and payload["config"] is None
    assert payload["options"]["manifests"][0]["name"] == "requirements.txt"
    assert not (project / ".pymolt" / "env_config.json").exists()


def test_capture_passes_accept_failing_through(tmp_path, monkeypatch, tools):
    project = _configured_project(tmp_path)
    seen = {}
    fake = _fake_capture(monkeypatch)

    def spy(project_dir, when, mode, **kwargs):
        seen.update(kwargs)
        return fake(project_dir, when, mode, **kwargs)

    monkeypatch.setattr("pymolt.verify.service.capture_named_trace", spy)
    view = tools.capture(project_dir=str(project), command=["pytest"], accept_failing=True)
    assert view["ok"] is True and seen["accept_failing"] is True
