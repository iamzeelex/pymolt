"""
Tests for call_graph.py — diff logic only (no actual PyCG analysis).
"""

import pytest
from axiom_graph.analyzers.call_graph import diff_call_graphs
from axiom_graph.models import CallEdge, CallGraphSnapshot


def make_snapshot(version: str, nodes: list[str], edges: list[tuple[str, str]]) -> CallGraphSnapshot:
    return CallGraphSnapshot(
        version=version,
        package="pkg",
        nodes=nodes,
        edges=[CallEdge(caller=s, callee=d) for s, d in edges],
    )


class TestDiffCallGraphs:
    def test_removed_nodes(self):
        old = make_snapshot("1.0", ["pkg.Foo", "pkg.Bar", "pkg.Baz"], [])
        new = make_snapshot("2.0", ["pkg.Foo"], [])
        diff = diff_call_graphs(old, new)
        assert set(diff.removed_nodes) == {"pkg.Bar", "pkg.Baz"}
        assert diff.added_nodes == []

    def test_added_nodes(self):
        old = make_snapshot("1.0", ["pkg.Foo"], [])
        new = make_snapshot("2.0", ["pkg.Foo", "pkg.NewBar"], [])
        diff = diff_call_graphs(old, new)
        assert diff.added_nodes == ["pkg.NewBar"]
        assert diff.removed_nodes == []

    def test_removed_edges(self):
        old = make_snapshot("1.0", [], [("pkg.A", "pkg.B"), ("pkg.B", "pkg.C")])
        new = make_snapshot("2.0", [], [("pkg.A", "pkg.B")])
        diff = diff_call_graphs(old, new)
        removed = [(e.caller, e.callee) for e in diff.removed_edges]
        assert ("pkg.B", "pkg.C") in removed

    def test_added_edges(self):
        old = make_snapshot("1.0", [], [("pkg.A", "pkg.B")])
        new = make_snapshot("2.0", [], [("pkg.A", "pkg.B"), ("pkg.A", "pkg.C")])
        diff = diff_call_graphs(old, new)
        added = [(e.caller, e.callee) for e in diff.added_edges]
        assert ("pkg.A", "pkg.C") in added

    def test_no_changes(self):
        snap = make_snapshot("1.0", ["pkg.A"], [("pkg.A", "pkg.B")])
        diff = diff_call_graphs(snap, snap)
        assert diff.removed_nodes == []
        assert diff.added_nodes == []
        assert diff.removed_edges == []
        assert diff.added_edges == []

    def test_empty_snapshots(self):
        old = make_snapshot("1.0", [], [])
        new = make_snapshot("2.0", [], [])
        diff = diff_call_graphs(old, new)
        assert diff.removed_nodes == []
        assert diff.added_nodes == []
