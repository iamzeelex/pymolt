"""
Tests for fusion.py — state-machine accumulation and signal merging.
No network, no griffe, no PyCG.
"""

import pytest
from axiom_graph.fusion import accumulate_chain, fuse_pairwise, _merge_changes
from axiom_graph.analyzers.griffe_diff import RawBreakingChange
from axiom_graph.models import (
    ApiState,
    BreakingChange,
    CallEdge,
    CallGraphDiff,
    ChangeRisk,
    PairwiseDelta,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_raw(path: str, kind: str = "object-removed", risk: ChangeRisk = ChangeRisk.STRUCTURAL) -> RawBreakingChange:
    return RawBreakingChange(
        kind=kind,
        path=path,
        risk=risk,
        explanation=f"{path} was changed",
    )


def make_change(path: str, state: ApiState = ApiState.REMOVED, **kwargs) -> BreakingChange:
    defaults = dict(
        kind="object-removed",
        path=path,
        risk=ChangeRisk.STRUCTURAL,
        explanation="removed",
        state=state,
    )
    defaults.update(kwargs)
    return BreakingChange(**defaults)


def make_cg_diff(**kwargs) -> CallGraphDiff:
    defaults = dict(removed_nodes=[], added_nodes=[], removed_edges=[], added_edges=[])
    defaults.update(kwargs)
    return CallGraphDiff(**defaults)


# ---------------------------------------------------------------------------
# fuse_pairwise tests
# ---------------------------------------------------------------------------

class TestFusePairwise:
    def test_basic_fusion(self):
        raw = [make_raw("pkg.Foo.bar")]
        cg_diff = make_cg_diff(removed_nodes=["pkg.Foo.bar"])
        delta = fuse_pairwise(
            raw_changes=raw,
            cg_diff=cg_diff,
            ast_hints={},
            test_examples={},
            from_version="1.0",
            to_version="2.0",
            package="pkg",
            transitions=[],
        )
        assert len(delta.changes) == 1
        c = delta.changes[0]
        assert c.call_graph_confirmed is True
        assert c.state == ApiState.REMOVED

    def test_ast_hint_overrides_griffe_hint(self):
        raw = [make_raw("pkg.Foo.bar", risk=ChangeRisk.STRUCTURAL)]
        raw[0].deprecation_hint = "Griffe docstring hint"
        cg_diff = make_cg_diff()
        delta = fuse_pairwise(
            raw_changes=raw,
            cg_diff=cg_diff,
            ast_hints={"pkg.Foo.bar": "AST hint — use baz()"},
            test_examples={},
            from_version="1.0",
            to_version="2.0",
            package="pkg",
            transitions=[],
        )
        c = delta.changes[0]
        assert c.deprecation_hint == "AST hint — use baz()"

    def test_test_examples_matched_by_leaf(self):
        raw = [make_raw("pkg.DataFrame.append")]
        cg_diff = make_cg_diff()
        delta = fuse_pairwise(
            raw_changes=raw,
            cg_diff=cg_diff,
            ast_hints={},
            test_examples={"append": ["@@ -1 +1 @@\n- df.append(other)\n+ pd.concat([df, other])"]},
            from_version="1.0",
            to_version="2.0",
            package="pkg",
            transitions=[],
        )
        c = delta.changes[0]
        assert len(c.test_examples) == 1
        assert "concat" in c.test_examples[0]

    def test_deprecated_state_when_hint_but_not_removed(self):
        raw = [make_raw("pkg.Foo.old_api", kind="attribute-changed-value", risk=ChangeRisk.BEHAVIORAL)]
        cg_diff = make_cg_diff()  # NOT in removed_nodes
        delta = fuse_pairwise(
            raw_changes=raw,
            cg_diff=cg_diff,
            ast_hints={"pkg.Foo.old_api": "Deprecated, use new_api()"},
            test_examples={},
            from_version="1.0",
            to_version="2.0",
            package="pkg",
            transitions=[],
        )
        c = delta.changes[0]
        assert c.state == ApiState.DEPRECATED


# ---------------------------------------------------------------------------
# accumulate_chain tests
# ---------------------------------------------------------------------------

class TestAccumulateChain:
    def _make_delta(self, from_v, to_v, changes, transitions=None):
        return PairwiseDelta(
            package="pkg",
            from_version=from_v,
            to_version=to_v,
            changes=changes,
            call_graph_diff=make_cg_diff(),
            transitions=transitions or [],
        )

    def test_deprecated_then_removed_fills_temporal_context(self):
        step1 = self._make_delta(
            "1.0", "1.4",
            [make_change("pkg.Foo.bar", state=ApiState.DEPRECATED, deprecation_hint="Use baz()")]
        )
        step2 = self._make_delta(
            "1.4", "2.0",
            [make_change("pkg.Foo.bar", state=ApiState.REMOVED)]
        )

        full = accumulate_chain([step1, step2], "pkg", "1.0", "2.0", ["1.4", "2.0"])

        bar = next(c for c in full.changes if c.path == "pkg.Foo.bar")
        assert bar.deprecated_since == "1.0"   # from_version of step1
        assert bar.removed_in == "2.0"         # to_version of step2
        assert bar.state == ApiState.REMOVED

    def test_single_step_removed(self):
        step = self._make_delta("1.0", "2.0", [make_change("pkg.X", state=ApiState.REMOVED)])
        full = accumulate_chain([step], "pkg", "1.0", "2.0", ["2.0"])
        x = next(c for c in full.changes if c.path == "pkg.X")
        assert x.removed_in == "2.0"
        assert x.deprecated_since is None

    def test_move_detection(self):
        step = self._make_delta(
            "1.0", "2.0",
            [make_change("pkg.OldFoo", state=ApiState.REMOVED)],
            transitions=[{"from_path": "pkg.OldFoo", "to_path": "pkg.NewFoo", "confidence": "high"}]
        )
        full = accumulate_chain([step], "pkg", "1.0", "2.0", ["2.0"])
        old_foo = next(c for c in full.changes if c.path == "pkg.OldFoo")
        assert old_foo.state == ApiState.MOVED

    def test_deduplication_keeps_richest(self):
        step1 = self._make_delta(
            "1.0", "1.5",
            [make_change("pkg.A", state=ApiState.DEPRECATED, deprecation_hint="hint")]
        )
        step2 = self._make_delta(
            "1.5", "2.0",
            [make_change("pkg.A", state=ApiState.REMOVED, call_graph_confirmed=True)]
        )
        full = accumulate_chain([step1, step2], "pkg", "1.0", "2.0", ["1.5", "2.0"])
        paths = [c.path for c in full.changes]
        assert paths.count("pkg.A") == 1
        a = next(c for c in full.changes if c.path == "pkg.A")
        assert a.call_graph_confirmed is True   # from step2
        assert a.deprecation_hint == "hint"     # from step1

    def test_empty_chain(self):
        full = accumulate_chain([], "pkg", "1.0", "2.0", ["2.0"])
        assert full.changes == []
        assert full.total_breaking == 0


# ---------------------------------------------------------------------------
# _merge_changes tests
# ---------------------------------------------------------------------------

class TestMergeChanges:
    def test_removed_wins_over_deprecated(self):
        a = make_change("pkg.X", state=ApiState.DEPRECATED)
        b = make_change("pkg.X", state=ApiState.REMOVED)
        merged = _merge_changes(a, b)
        assert merged.state == ApiState.REMOVED

    def test_hints_combined(self):
        a = make_change("pkg.X", deprecation_hint="hint A")
        b = make_change("pkg.X", deprecation_hint=None)
        merged = _merge_changes(a, b)
        assert merged.deprecation_hint == "hint A"

    def test_test_examples_unioned(self):
        a = make_change("pkg.X", test_examples=["ex1"])
        b = make_change("pkg.X", test_examples=["ex2", "ex1"])
        merged = _merge_changes(a, b)
        assert set(merged.test_examples) == {"ex1", "ex2"}

    def test_cg_confirmed_ored(self):
        a = make_change("pkg.X", call_graph_confirmed=False)
        b = make_change("pkg.X", call_graph_confirmed=True)
        merged = _merge_changes(a, b)
        assert merged.call_graph_confirmed is True
