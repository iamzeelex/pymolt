"""
Tests for axiom_graph models — serialization, state machine, merging.
No network, no filesystem.
"""

import pytest
from axiom_graph.models import (
    ApiState,
    BreakingChange,
    CallEdge,
    CallGraphDiff,
    CallGraphSnapshot,
    ChangeRisk,
    FullDelta,
    PairwiseDelta,
)


class TestCallEdge:
    def test_hash_equality(self):
        e1 = CallEdge(caller="a.b", callee="c.d")
        e2 = CallEdge(caller="a.b", callee="c.d")
        assert e1 == e2
        assert hash(e1) == hash(e2)

    def test_set_deduplication(self):
        edges = {CallEdge(caller="a", callee="b"), CallEdge(caller="a", callee="b")}
        assert len(edges) == 1


class TestCallGraphSnapshot:
    def test_node_set(self):
        snap = CallGraphSnapshot(
            version="1.0",
            package="pkg",
            nodes=["a.b", "a.c", "a.d"],
        )
        assert snap.node_set() == {"a.b", "a.c", "a.d"}

    def test_edge_set(self):
        snap = CallGraphSnapshot(
            version="1.0",
            package="pkg",
            edges=[CallEdge(caller="a", callee="b"), CallEdge(caller="b", callee="c")],
        )
        assert snap.edge_set() == {("a", "b"), ("b", "c")}


class TestBreakingChange:
    def make(self, **kwargs) -> BreakingChange:
        defaults = dict(
            kind="object-removed",
            path="pkg.Foo.bar",
            risk=ChangeRisk.STRUCTURAL,
            explanation="bar was removed",
        )
        defaults.update(kwargs)
        return BreakingChange(**defaults)

    def test_defaults(self):
        c = self.make()
        assert c.state == ApiState.REMOVED
        assert c.deprecated_since is None
        assert c.removed_in is None
        assert c.call_graph_confirmed is False
        assert c.test_examples == []

    def test_serialization_roundtrip(self):
        c = self.make(
            state=ApiState.DEPRECATED,
            deprecated_since="1.2.0",
            deprecation_hint="Use baz() instead.",
        )
        data = c.model_dump()
        restored = BreakingChange(**data)
        assert restored.deprecated_since == "1.2.0"
        assert restored.state == ApiState.DEPRECATED


class TestFullDelta:
    def _make_change(self, path: str, risk: ChangeRisk) -> BreakingChange:
        return BreakingChange(
            kind="object-removed",
            path=path,
            risk=risk,
            explanation="removed",
            state=ApiState.REMOVED,
        )

    def test_summary_computed_on_init(self):
        changes = [
            self._make_change("pkg.A", ChangeRisk.STRUCTURAL),
            self._make_change("pkg.B", ChangeRisk.STRUCTURAL),
            self._make_change("pkg.C", ChangeRisk.BEHAVIORAL),
            self._make_change("pkg.D", ChangeRisk.MECHANICAL),
        ]
        delta = FullDelta(
            package="pkg",
            from_version="1.0",
            to_version="2.0",
            release_chain=["1.5", "2.0"],
            changes=changes,
        )
        assert delta.total_breaking == 4
        assert delta.summary_by_risk["structural"] == 2
        assert delta.summary_by_risk["behavioral"] == 1
        assert delta.summary_by_risk["mechanical"] == 1

    def test_skipped_delta(self):
        delta = FullDelta(
            package="pkg",
            from_version="1.0",
            to_version="2.0",
            skipped=True,
            skip_reason="no sdist",
        )
        assert delta.skipped
        assert delta.total_breaking == 0

    def test_json_roundtrip(self):
        delta = FullDelta(
            package="pkg",
            from_version="1.0",
            to_version="2.0",
            release_chain=["2.0"],
        )
        import json
        data = json.dumps(delta.model_dump())
        restored = FullDelta.model_validate_json(data)
        assert restored.package == "pkg"


class TestApiStateEnum:
    def test_values(self):
        assert ApiState.ACTIVE == "active"
        assert ApiState.DEPRECATED == "deprecated"
        assert ApiState.REMOVED == "removed"
        assert ApiState.MOVED == "moved"


class TestChangeRiskEnum:
    def test_ordering_assumption(self):
        # Structural is the most severe
        assert ChangeRisk.STRUCTURAL == "structural"
        assert ChangeRisk.MECHANICAL == "mechanical"
