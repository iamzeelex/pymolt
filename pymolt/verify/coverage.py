"""Coverage map -> blind spots.

Coverage is the **honesty marker for tests**. A green suite proves nothing about an
unexercised path, so the cascade must distinguish "tests cover this node" from "tests are
silent here" instead of trusting a pass blindly. That distinction is what makes
``TraceScope.BLIND_SPOTS`` meaningful: a node confidently settled *and exercised* is not
traced; a node that passed but was never exercised is a blind spot and descends.

A dependency node counts as *exercised* when the suite executed at least one line under its
import prefix. Running coverage requires the target environment (the devcontainer), so
``build_coverage_map`` shells out behind ``adapters/subprocess_runner``; the parsing
(``parse_coverage_json``) and the blind-spot logic are pure and unit-tested directly.
"""
from collections.abc import Callable, Iterable
from typing import Any

from pydantic import BaseModel, Field

from pymolt.adapters.subprocess_runner import run_command
from pymolt.verify.models import NodeRef


class CoverageMap(BaseModel):
    """Which dependency import prefixes the suite actually exercised."""

    exercised_prefixes: set[str] = Field(default_factory=set)

    def is_exercised(self, prefix: str) -> bool:
        """True if any exercised module is the prefix itself or lives under it."""
        return any(p == prefix or p.startswith(prefix + ".") for p in self.exercised_prefixes)

    def is_node_exercised(self, node: NodeRef) -> bool:
        return self.is_exercised(node.trace_prefix)


def _module_of(filepath: str) -> str | None:
    """Best-effort module dotted-name from a coverage file path.

    Maps ``.../site-packages/flask/views.py`` -> ``flask.views``,
    ``.../flask/__init__.py`` -> ``flask``. Returns None for paths with no package anchor.
    """
    norm = filepath.replace("\\", "/")
    anchor = None
    for marker in ("/site-packages/", "/dist-packages/"):
        idx = norm.rfind(marker)
        if idx != -1:
            anchor = norm[idx + len(marker):]
            break
    if anchor is None:
        return None
    if anchor.endswith(".py"):
        anchor = anchor[:-3]
    if anchor.endswith("/__init__"):
        anchor = anchor[: -len("/__init__")]
    parts = [p for p in anchor.split("/") if p]
    return ".".join(parts) if parts else None


def parse_coverage_json(data: dict[str, Any]) -> set[str]:
    """Extract executed module dotted-names from a coverage.py JSON report.

    A file is counted only if it has executed lines (``summary.covered_lines > 0``), so files
    that were imported but never run do not count as exercised.
    """
    prefixes: set[str] = set()
    for filepath, info in (data.get("files") or {}).items():
        summary = info.get("summary", {}) if isinstance(info, dict) else {}
        if summary.get("covered_lines", 0) <= 0:
            continue
        module = _module_of(filepath)
        if module:
            prefixes.add(module)
    return prefixes


def run_coverage_json(
    project_dir: str,
    pytest_args: Iterable[str] = (),
    json_out: str = "coverage.json",
    runner: Callable[..., Any] = run_command,
) -> dict[str, Any]:
    """Run the suite under coverage in ``project_dir`` and return the raw coverage.py JSON
    report (the ``{"files": {...}}`` shape).

    System-touching, so it goes through the subprocess adapter and is exercised inside the
    devcontainer (not the host unit suite). ``runner`` is injectable for testing the
    orchestration without a real suite. Shared by ``build_coverage_map`` (the prefix-only
    view below) and callers that need the raw per-file report (e.g. ``gaps.classify_gaps``).
    """
    args = list(pytest_args)
    runner(["coverage", "run", "-m", "pytest", *args], cwd=project_dir, check=False)
    runner(["coverage", "json", "-o", json_out], cwd=project_dir, check=False)
    import json
    import os

    path = json_out if os.path.isabs(json_out) else os.path.join(project_dir, json_out)
    with open(path) as f:
        return json.load(f)


def build_coverage_map(
    project_dir: str,
    pytest_args: Iterable[str] = (),
    json_out: str = "coverage.json",
    runner: Callable[..., Any] = run_command,
) -> CoverageMap:
    """Run the suite under coverage in ``project_dir`` and build the map.

    System-touching, so it goes through the subprocess adapter and is exercised inside the
    devcontainer (not the host unit suite). ``runner`` is injectable for testing the
    orchestration without a real suite.
    """
    data = run_coverage_json(project_dir, pytest_args, json_out, runner)
    return CoverageMap(exercised_prefixes=parse_coverage_json(data))


def blind_spots(coverage_map: CoverageMap, nodes: Iterable[NodeRef]) -> list[NodeRef]:
    """Nodes whose boundary with a dependency is never hit by the suite."""
    return [n for n in nodes if not coverage_map.is_node_exercised(n)]
