"""Tests for the stdio MCP server (pymolt/interfaces/mcp_server.py).

The whole module is skipped when the optional `mcp` extra isn't installed.
Tools are registered on FastMCP but kept as plain importable functions, so we
call them directly (no transport needed) and assert the universal output
contract.
"""
from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("mcp")

from pymolt.interfaces import mcp_server  # noqa: E402

# The 8 tools this server must expose.
_EXPECTED_TOOLS = {
    "scan",
    "setup_options",
    "setup_apply",
    "assess",
    "contract_map",
    "contract_capture",
    "contract_report",
    "codemods_preview",
}


def _make_project(tmp_path):
    """A minimal but real project root: one manifest + one source file."""
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\n", encoding="utf-8")
    (tmp_path / "app.py").write_text(
        "import requests\n\n\ndef fetch():\n    return requests.get('http://x')\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.fixture(autouse=True)
def _workspace_root(tmp_path, monkeypatch):
    """Confine every test's workspace root to its own tmp_path so `project_dir=str(tmp_path)`
    (or a descendant) resolves without tripping the outside-root rejection."""
    monkeypatch.setattr(mcp_server, "_WORKSPACE_ROOT", tmp_path.resolve())


@pytest.fixture(autouse=True)
def _no_exec_env(monkeypatch):
    """Default every test to the safe-by-default (exec disabled) state unless it
    opts in explicitly with monkeypatch.setenv(PYMOLT_MCP_ALLOW_EXEC, "1")."""
    monkeypatch.delenv("PYMOLT_MCP_ALLOW_EXEC", raising=False)


# ── (a) module imports and all 8 tools are registered ────────────────────────
def test_all_eight_tools_registered():
    tools = asyncio.run(mcp_server.mcp.list_tools())
    names = {t.name for t in tools}
    assert _EXPECTED_TOOLS <= names, f"missing tools: {_EXPECTED_TOOLS - names}"


def test_tool_functions_are_importable_and_callable():
    for name in _EXPECTED_TOOLS:
        fn = getattr(mcp_server, name)
        assert callable(fn)


def test_dir_taking_tools_use_project_dir_param():
    """The directory arg must be named `project_dir`, matching the CLI. A mismatch
    (the old `dir`) let an agent that used the CLI name silently hit the default '.'
    and scan the server's own cwd instead of the target — regression guard."""
    import inspect

    dir_tools = ["scan", "setup_options", "setup_apply", "assess",
                 "contract_map", "contract_capture", "contract_report", "codemods_preview"]
    for name in dir_tools:
        params = inspect.signature(getattr(mcp_server, name)).parameters
        assert "project_dir" in params, f"{name} lost its project_dir param"
        assert "dir" not in params, f"{name} still uses the CLI-inconsistent `dir` param"


# ── (b) scan happy path: contract shape + full report written ────────────────
def test_scan_returns_contract_shape_and_writes_report(tmp_path):
    project = _make_project(tmp_path)
    result = mcp_server.scan(project_dir=str(project))

    assert set(result) == {"ok", "summary", "data", "full_report_path", "hint"}
    assert result["ok"] is True
    assert isinstance(result["summary"], str) and result["summary"]
    assert result["data"]["root_count"] >= 1

    # The full payload is on disk and is valid JSON.
    full_path = result["full_report_path"]
    assert full_path is not None
    from pathlib import Path

    written = Path(full_path)
    assert written.is_file()
    assert written.parts[-3:] == (".pymolt", "mcp", "scan.json")
    json.loads(written.read_text(encoding="utf-8"))  # must parse


def test_scan_result_is_json_serializable(tmp_path):
    result = mcp_server.scan(project_dir=str(_make_project(tmp_path)))
    json.dumps(result)  # no non-serializable objects leak into the contract


# ── (c) failure path: nonexistent dir -> ok:false, error+hint, no exception ──
def test_scan_nonexistent_dir_returns_error(tmp_path):
    missing = tmp_path / "does-not-exist"
    result = mcp_server.scan(project_dir=str(missing))

    assert result["ok"] is False
    assert result["error"] and "does not exist" in result["error"].lower()
    assert "hint" in result
    assert "summary" not in result  # failure shape, not the success shape


def test_every_tool_handles_nonexistent_dir(tmp_path):
    missing = str(tmp_path / "nope")
    calls = {
        "scan": lambda: mcp_server.scan(project_dir=missing),
        "setup_options": lambda: mcp_server.setup_options(project_dir=missing),
        "setup_apply": lambda: mcp_server.setup_apply(
            project_dir=missing, manifest="requirements.txt", tool="uv",
            base_python="3.8", target_python="3.12",
        ),
        "assess": lambda: mcp_server.assess(project_dir=missing),
        "contract_map": lambda: mcp_server.contract_map(project_dir=missing),
        "contract_capture": lambda: mcp_server.contract_capture(
            project_dir=missing, when="baseline", mode="tests", command=["pytest"],
        ),
        "contract_report": lambda: mcp_server.contract_report(project_dir=missing),
        "codemods_preview": lambda: mcp_server.codemods_preview(project_dir=missing),
    }
    for name, call in calls.items():
        result = call()
        assert result["ok"] is False, f"{name} should fail on a missing dir"
        assert result.get("error"), f"{name} missing error message"


# ── (d) list-cap helper: truncates at 20 and reports the remainder ───────────
def test_cap_list_no_truncation_under_cap():
    items = list(range(mcp_server.LIST_CAP))
    capped, remaining = mcp_server._cap_list(items)
    assert capped == items
    assert remaining == 0


def test_cap_list_truncates_and_reports_remainder():
    items = list(range(mcp_server.LIST_CAP + 5))
    capped, remaining = mcp_server._cap_list(items)
    assert len(capped) == mcp_server.LIST_CAP
    assert capped == items[: mcp_server.LIST_CAP]
    assert remaining == 5


# ── contract-shape helpers ───────────────────────────────────────────────────
def test_setup_options_happy_path_lists_choices(tmp_path):
    result = mcp_server.setup_options(project_dir=str(_make_project(tmp_path)))
    assert result["ok"] is True
    assert "requirements.txt" in result["data"]["manifests"]
    assert result["data"]["default_tool"]


def test_setup_apply_rejects_invalid_manifest(tmp_path):
    project = _make_project(tmp_path)
    result = mcp_server.setup_apply(
        project_dir=str(project), manifest="bogus.txt", tool="uv",
        base_python="3.8", target_python="3.12",
    )
    assert result["ok"] is False
    assert "requirements.txt" in str(result["hint"])


# ── workspace confinement (_resolve_dir + _WORKSPACE_ROOT) ──────────────────
def test_resolve_dir_allows_root_and_descendants(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    assert mcp_server._resolve_dir(str(tmp_path)) == tmp_path.resolve()
    assert mcp_server._resolve_dir(str(sub)) == sub.resolve()


def test_resolve_dir_rejects_path_outside_root(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(mcp_server, "_WORKSPACE_ROOT", root.resolve())

    with pytest.raises(ValueError, match="outside the workspace root"):
        mcp_server._resolve_dir(str(outside))


def test_scan_rejects_dir_outside_workspace_root(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside = _make_project(outside_dir)
    monkeypatch.setattr(mcp_server, "_WORKSPACE_ROOT", root.resolve())

    result = mcp_server.scan(project_dir=str(outside))
    assert result["ok"] is False
    assert "outside the workspace root" in result["error"]


# ── exec gate (_require_exec_allowed) ────────────────────────────────────────
def test_require_exec_allowed_blocks_by_default():
    result = mcp_server._require_exec_allowed()
    assert result is not None
    assert result["ok"] is False
    assert "PYMOLT_MCP_ALLOW_EXEC" in result["hint"]


def test_require_exec_allowed_permits_when_env_var_set(monkeypatch):
    monkeypatch.setenv("PYMOLT_MCP_ALLOW_EXEC", "1")
    assert mcp_server._require_exec_allowed() is None


def test_contract_capture_blocked_without_exec_opt_in(tmp_path):
    project = _make_project(tmp_path)
    result = mcp_server.contract_capture(
        project_dir=str(project), when="baseline", mode="tests", command=["pytest"],
    )
    assert result["ok"] is False
    assert "PYMOLT_MCP_ALLOW_EXEC" in result["hint"]


def test_env_hint_not_gated_returns_hint(tmp_path):
    """env_hint runs nothing (it only suggests a Dockerfile/uv commands), so it must work
    without PYMOLT_MCP_ALLOW_EXEC set at all — unlike contract_capture."""
    project = _make_project(tmp_path)
    (project / "Dockerfile").write_text("FROM python:3.6\n", encoding="utf-8")
    result = mcp_server.env_hint(project_dir=str(project), target_python="3.13")
    assert result["ok"] is True
    assert result["data"]["suggested_dockerfile"] is not None
    assert "pymolt contract capture --when post-migration" in result["hint"]


def test_scan_not_gated_by_exec_check(tmp_path):
    """Read-only tools must work with no PYMOLT_MCP_ALLOW_EXEC set at all."""
    result = mcp_server.scan(project_dir=str(_make_project(tmp_path)))
    assert result["ok"] is True
