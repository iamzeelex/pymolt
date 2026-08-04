import subprocess

import pymolt.ingestion.uv_runner as uv_runner
from pymolt.core.enums import Mode, ResolutionQuality, SourceFixation
from pymolt.ingestion.detect import DiscoveredSource
from pymolt.ingestion.lock_parsers import (
    parse_uv_lock,
    parse_poetry_lock,
    parse_pipfile_lock,
    parse_pypi_lock,
)


def test_parse_uv_lock_direct_and_transitive(tmp_path):
    lock = tmp_path / "uv.lock"
    lock.write_text(
        """
version = 1
requires-python = ">=3.12"

[[package]]
name = "myproject"
version = "0.1.0"
source = { editable = "." }
dependencies = [
    { name = "httpx" },
]

[[package]]
name = "httpx"
version = "0.27.0"
source = { registry = "https://pypi.org/simple" }
dependencies = [
    { name = "anyio" },
]

[[package]]
name = "anyio"
version = "4.3.0"
source = { registry = "https://pypi.org/simple" }
""",
        encoding="utf-8",
    )

    graph = parse_uv_lock(lock)

    # Root project package is not a dependency node.
    assert "myproject" not in graph.nodes
    assert graph.nodes["httpx"].version == "0.27.0"
    assert graph.nodes["httpx"].direct is True
    assert graph.nodes["anyio"].direct is False
    assert graph.nodes["httpx"].mode == Mode.PYPI

    # Edge convention: source requires target. httpx -> anyio.
    assert any(e.source == "httpx" and e.target == "anyio" for e in graph.edges)
    assert graph.resolution_quality == ResolutionQuality.LOCK_PARSED
    assert graph.source_fixation == SourceFixation.PINNED


def test_parse_poetry_lock_uses_pyproject_for_direct(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        """
[tool.poetry]
name = "proj"

[tool.poetry.dependencies]
python = "^3.12"
httpx = "^0.27.0"
""",
        encoding="utf-8",
    )
    lock = tmp_path / "poetry.lock"
    lock.write_text(
        """
[[package]]
name = "httpx"
version = "0.27.0"
category = "main"

[package.dependencies]
anyio = ">=3.0"

[[package]]
name = "anyio"
version = "4.3.0"
category = "main"
""",
        encoding="utf-8",
    )

    graph = parse_poetry_lock(lock)
    assert graph.nodes["httpx"].direct is True
    assert graph.nodes["anyio"].direct is False  # not in pyproject
    assert any(e.source == "httpx" and e.target == "anyio" for e in graph.edges)


def test_parse_pipfile_lock_uses_pipfile_for_direct(tmp_path):
    (tmp_path / "Pipfile").write_text(
        """
[packages]
httpx = "*"
""",
        encoding="utf-8",
    )
    lock = tmp_path / "Pipfile.lock"
    lock.write_text(
        """
{
  "default": {
    "httpx": {"version": "==0.27.0"},
    "anyio": {"version": "==4.3.0"}
  },
  "develop": {}
}
""",
        encoding="utf-8",
    )

    graph = parse_pipfile_lock(lock)
    assert graph.nodes["httpx"].version == "0.27.0"
    assert graph.nodes["httpx"].direct is True
    assert graph.nodes["anyio"].direct is False
    assert graph.edges == []  # Pipfile.lock carries no per-package edges


def test_parse_pypi_lock_empty_returns_none(tmp_path):
    lock = tmp_path / "uv.lock"
    lock.write_text("", encoding="utf-8")
    assert parse_pypi_lock(lock) is None


def test_parse_pypi_lock_unsupported_returns_none(tmp_path):
    lock = tmp_path / "something.lock"
    lock.write_text("name = 'x'", encoding="utf-8")
    assert parse_pypi_lock(lock) is None


def test_uv_runner_parses_lock_directly_without_recompiling(tmp_path, monkeypatch):
    """A populated lock must be read verbatim, never recompiled."""
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'proj'\ndependencies = ['httpx']", encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text(
        """
version = 1

[[package]]
name = "proj"
version = "0.1.0"
source = { editable = "." }
dependencies = [{ name = "httpx" }]

[[package]]
name = "httpx"
version = "0.27.0"
source = { registry = "https://pypi.org/simple" }
""",
        encoding="utf-8",
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("run_command should not be called when a lock is parsed directly")

    monkeypatch.setattr(uv_runner, "run_command", fail_if_called)

    source = DiscoveredSource(
        path=tmp_path / "uv.lock",
        mode=Mode.PYPI,
        is_lock=True,
        fixation=SourceFixation.PINNED,
    )
    graph = uv_runner.ingest(source, current_python="3.12")
    assert graph.nodes["httpx"].version == "0.27.0"
    assert graph.resolution_quality == ResolutionQuality.LOCK_PARSED


def test_uv_runner_force_recompile_ignores_lock(tmp_path, monkeypatch):
    """force_recompile re-resolves the sibling manifest instead of the lock."""
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'proj'\ndependencies = ['httpx']", encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text(
        """
version = 1

[[package]]
name = "proj"
version = "0.1.0"
source = { editable = "." }
dependencies = [{ name = "httpx" }]

[[package]]
name = "httpx"
version = "0.27.0"
source = { registry = "https://pypi.org/simple" }
""",
        encoding="utf-8",
    )

    captured = {}

    def mock_run_command(cmd, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="httpx==0.28.0\n    # via -r pyproject.toml\n", stderr=""
        )

    monkeypatch.setattr(uv_runner, "run_command", mock_run_command)

    source = DiscoveredSource(
        path=tmp_path / "uv.lock",
        mode=Mode.PYPI,
        is_lock=True,
        fixation=SourceFixation.PINNED,
    )
    graph = uv_runner.ingest(source, current_python="3.12", force_recompile=True)
    assert "pyproject.toml" in captured["cmd"]
    assert "uv.lock" not in captured["cmd"]
    assert graph.nodes["httpx"].version == "0.28.0"
