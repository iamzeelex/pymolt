"""
axiom_graph/analyzers/call_graph.py

PyCG-based call graph builder and differ.

Uses the vendored PyCG (pymolt._vendor.pycg.CallGraphGenerator) directly —
no subprocess, in-process analysis with a safe contextmanager that:
  1. Temporarily adds source_dir to sys.path
  2. Runs PyCG analysis (static AST traversal with import hooks)
  3. Tears down import hooks via cg.tearDown()
  4. Restores sys.path

PyCG operates through install_hooks() which installs a custom import finder.
This is safe for our use case because:
  - PyCG performs static AST analysis, not actual imports
  - tearDown() removes all hooks before we proceed
  - We add/remove the source_dir from sys.path atomically via contextmanager
"""

from __future__ import annotations

import logging
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

log = logging.getLogger(__name__)

try:
    from pymolt._vendor.pycg.pycg import CallGraphGenerator
    from pymolt._vendor.pycg.utils.constants import CALL_GRAPH_OP
    _PYCG_AVAILABLE = True
except ImportError:
    _PYCG_AVAILABLE = False
    log.warning("pymolt._vendor.pycg not found — call graph analysis disabled")

from axiom_graph.models import CallEdge, CallGraphSnapshot, CallGraphDiff


# ---------------------------------------------------------------------------
# sys.path context manager
# ---------------------------------------------------------------------------

@contextmanager
def _sys_path_prepend(directory: Path) -> Generator[None, None, None]:
    """Temporarily prepend a directory to sys.path."""
    dir_str = str(directory)
    sys.path.insert(0, dir_str)
    try:
        yield
    finally:
        try:
            sys.path.remove(dir_str)
        except ValueError:
            pass  # already removed


# ---------------------------------------------------------------------------
# Entry-point discovery
# ---------------------------------------------------------------------------

_EXCLUDE_DIRS = frozenset({
    "tests", "test", "__pycache__", ".git", "docs", "doc",
    "examples", "example", "benchmarks", "build", "dist",
})


def _collect_entry_points(source_dir: Path) -> list[str]:
    """
    Collect all .py files in source_dir, excluding test directories,
    __pycache__, and other non-source dirs.

    Returns absolute path strings suitable for PyCG entry_points.
    """
    entry_points: list[str] = []
    for py_file in source_dir.rglob("*.py"):
        # Exclude any path component that's in EXCLUDE_DIRS
        parts = set(py_file.relative_to(source_dir).parts[:-1])
        if parts & _EXCLUDE_DIRS:
            continue
        entry_points.append(str(py_file.resolve()))
    return entry_points


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------

def build_call_graph(
    source_dir: Path,
    package_name: str,
    version: str,
    *,
    max_iter: int = -1,
) -> CallGraphSnapshot:
    """
    Build a static call graph for the given package source directory.

    Args:
        source_dir: Path to the package's Python source directory
                    (the directory containing __init__.py).
        package_name: Import name of the package (e.g. "pandas").
        version: Version string for labeling the snapshot.
        max_iter: Max PyCG iterations (-1 = run until convergence).

    Returns:
        CallGraphSnapshot with nodes and edges extracted from PyCG output.
        Returns an empty snapshot if PyCG is unavailable or analysis fails.
    """
    empty = CallGraphSnapshot(version=version, package=package_name)

    if not _PYCG_AVAILABLE:
        log.warning("PyCG unavailable, returning empty snapshot for %s==%s", package_name, version)
        return empty

    if not source_dir.exists():
        log.warning("source_dir does not exist: %s", source_dir)
        return empty

    # PyCG needs the PARENT of the package dir as its --package argument
    # (i.e., the directory containing the package folder, not the package itself)
    pkg_parent = source_dir.parent
    entry_points = _collect_entry_points(source_dir)

    if not entry_points:
        log.warning("No entry points found in %s", source_dir)
        return empty

    log.info(
        "Building call graph for %s==%s (%d entry points)",
        package_name, version, len(entry_points)
    )

    cg_gen = None
    try:
        with _sys_path_prepend(pkg_parent):
            cg_gen = CallGraphGenerator(
                entry_points=entry_points,
                package=str(pkg_parent),
                max_iter=max_iter,
                operation=CALL_GRAPH_OP,
            )
            cg_gen.analyze()

        # Extract results AFTER teardown (hooks removed)
        raw_graph: dict[str, set[str]] = cg_gen.output()
        raw_edges: list[list[str]] = cg_gen.output_edges()

    except Exception as exc:
        log.warning(
            "PyCG analysis failed for %s==%s: %s", package_name, version, exc
        )
        return empty
    finally:
        if cg_gen is not None:
            try:
                cg_gen.tearDown()
            except Exception:
                pass

    # Build snapshot
    nodes = list(raw_graph.keys())
    edges = [
        CallEdge(caller=src, callee=dst)
        for src, dst in raw_edges
        if src and dst
    ]

    log.info(
        "Call graph built: %d nodes, %d edges for %s==%s",
        len(nodes), len(edges), package_name, version
    )

    return CallGraphSnapshot(
        version=version,
        package=package_name,
        nodes=nodes,
        edges=edges,
    )


# ---------------------------------------------------------------------------
# Graph differ
# ---------------------------------------------------------------------------

def diff_call_graphs(old: CallGraphSnapshot, new: CallGraphSnapshot) -> CallGraphDiff:
    """
    Compute the structural diff between two call graph snapshots.

    removed_nodes: nodes in old but not in new — primary removal signal.
    added_nodes:   nodes in new but not in old — may indicate renames.
    removed_edges: broken call relationships — decomposition of public interface.
    added_edges:   new call relationships — may indicate migration targets.
    """
    old_nodes = old.node_set()
    new_nodes = new.node_set()
    old_edges = old.edge_set()
    new_edges = new.edge_set()

    removed_node_names = sorted(old_nodes - new_nodes)
    added_node_names = sorted(new_nodes - old_nodes)

    removed_edge_pairs = sorted(old_edges - new_edges)
    added_edge_pairs = sorted(new_edges - old_edges)

    return CallGraphDiff(
        removed_nodes=removed_node_names,
        added_nodes=added_node_names,
        removed_edges=[CallEdge(caller=s, callee=d) for s, d in removed_edge_pairs],
        added_edges=[CallEdge(caller=s, callee=d) for s, d in added_edge_pairs],
    )
