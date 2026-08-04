import subprocess
from pathlib import Path

import pymolt.ingestion.uv_runner as uv_runner
from pymolt.core.enums import Mode, SourceFixation
from pymolt.ingestion.detect import DiscoveredSource


def _src(path, lock=False, fixation=SourceFixation.PINNED):
    return DiscoveredSource(path=path, mode=Mode.PYPI, is_lock=lock, fixation=fixation)


def test_system_tool_resolves_with_modern_pip_tools(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("anyio==4.3.0\n", encoding="utf-8")
    import pymolt.ingestion.fallback_compiler as fc

    captured = {}

    def fake_compile(manifest_path, version, container_id=None, constraint_file=None,
                     pip_tools_spec="pip-tools<6.0", env_prefix="legacy_env"):
        captured["spec"] = pip_tools_spec
        captured["prefix"] = env_prefix
        return "anyio==4.3.0\n    # via -r requirements.txt\n"

    monkeypatch.setattr(fc, "compile_legacy", fake_compile)

    graph = uv_runner.ingest(_src(tmp_path / "requirements.txt"), current_python="3.12", tool="system")
    assert captured["spec"] == "pip-tools"   # modern, not the legacy <6.0
    assert captured["prefix"] == "tools_env"
    assert graph.nodes["anyio"].version == "4.3.0"


def test_default_tool_uses_uv(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("anyio==4.3.0\n", encoding="utf-8")
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "anyio==4.3.0\n    # via -r requirements.txt\n", "")

    monkeypatch.setattr(uv_runner, "run_command", fake_run)

    graph = uv_runner.ingest(_src(tmp_path / "requirements.txt"), current_python="3.12", tool="uv")
    assert seen["cmd"][:3] == ["uv", "pip", "compile"]
    assert graph.nodes["anyio"].version == "4.3.0"


def test_poetry_tool_resolves_via_poetry_lock(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.poetry]\nname = 'p'\n\n[tool.poetry.dependencies]\npython = '^3.12'\nhttpx = '*'\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(uv_runner.shutil, "which", lambda x: "/usr/bin/poetry" if x == "poetry" else None)

    def fake_run(cmd, **kwargs):
        # Emulate `poetry lock` writing the lock into the temp working copy.
        cwd = Path(kwargs["cwd"])
        (cwd / "poetry.lock").write_text(
            '[[package]]\nname = "httpx"\nversion = "0.27.0"\ncategory = "main"\n\n'
            '[package.dependencies]\nanyio = ">=3.0"\n\n'
            '[[package]]\nname = "anyio"\nversion = "4.3.0"\ncategory = "main"\n',
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(uv_runner, "run_command", fake_run)

    graph = uv_runner.ingest(_src(tmp_path / "pyproject.toml", fixation=SourceFixation.INTENT), current_python="3.12", tool="poetry")
    assert graph.nodes["httpx"].version == "0.27.0"
    assert graph.nodes["httpx"].direct is True   # declared in [tool.poetry.dependencies]
    assert graph.nodes["anyio"].direct is False  # transitive


def test_poetry_tool_falls_back_to_uv_when_not_a_poetry_project(tmp_path, monkeypatch):
    # requirements.txt is not a poetry project -> poetry strategy declines -> uv.
    (tmp_path / "requirements.txt").write_text("anyio==4.3.0\n", encoding="utf-8")
    monkeypatch.setattr(uv_runner.shutil, "which", lambda x: "/usr/bin/poetry")

    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "anyio==4.3.0\n    # via -r requirements.txt\n", "")

    monkeypatch.setattr(uv_runner, "run_command", fake_run)

    graph = uv_runner.ingest(_src(tmp_path / "requirements.txt"), current_python="3.12", tool="poetry")
    assert seen["cmd"][:3] == ["uv", "pip", "compile"]
    assert graph.nodes["anyio"].version == "4.3.0"
