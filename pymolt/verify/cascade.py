"""The cascade (early-exit) — tests -> golden master -> trace, per node.

Per node, stop at the cheapest level that yields a confident verdict:

  L1 TESTS   run the suite touching the node under both versions; compare pass/fail.
             confident PASS -> BEHAVIOR_STABLE (evidence=TESTS)   [early exit]
             confident FAIL -> BEHAVIOR_CHANGED (evidence=TESTS)  [early exit]
             inconclusive   -> descend
  L2 GOLDEN  snapshot registered points under both; diff.
             clean   -> BEHAVIOR_STABLE (evidence=GOLDEN)   [early exit]
             differs -> BEHAVIOR_CHANGED (evidence=GOLDEN)  [early exit]
             no points -> descend
  L3 TRACE   record the boundary contract under both; diff -> verdict (terminal).

The cascade is **per-node**; the scope is **global** (set once for the whole run, orthogonal
to the per-node descend decision):

  BLIND_SPOTS  trace only what L1/L2 left unsettled. A node confidently settled *and exercised*
               by tests is NOT traced. (Coverage is the honesty marker: a PASS only early-exits
               if the node was actually exercised — a green suite proves nothing about an
               unexercised path, so a pass-but-blind node descends.)
  FULL         trace every node, even covered+settled ones. L1/L2 still run, for cross-check
               recorded in ``detail``; the verdict comes from L3.

System-touching work (running the suite, taking snapshots, tracing under a version) is injected
as ``CascadeDeps`` callables, so this module is pure decision logic — unit-testable with fakes,
no target app required. The component decides nothing about migration: it emits typed,
honesty-marked behavioral facts.
"""
from collections.abc import Iterable
from typing import Protocol

from pymolt.core.enums import EvidenceLevel, TestStatus, TraceScope, Verdict
from pymolt.verify.models import (
    BoundaryDiff,
    GoldenDiff,
    NodeRef,
    NodeVerifyResult,
    TestOutcome,
    VerifyReport,
)


class CascadeDeps(Protocol):
    """The system-touching operations the cascade drives, injected for testability."""

    def run_tests(self, node: NodeRef) -> TestOutcome:
        """L1: run the suite touching ``node`` under both versions and compare."""

    def is_exercised(self, node: NodeRef) -> bool:
        """Coverage honesty marker: did the suite actually exercise this node's boundary?"""

    def run_golden(self, node: NodeRef) -> GoldenDiff | None:
        """L2: diff golden-master snapshots. Return None when no points are registered."""

    def run_trace(self, node: NodeRef) -> BoundaryDiff:
        """L3: record the boundary under both versions and fold into a BoundaryDiff."""


def _verdict_from_boundary(diff: BoundaryDiff) -> Verdict:
    if not diff.is_clean():
        return Verdict.BEHAVIOR_CHANGED
    if diff.skipped_opaque:
        # comparable contacts are stable, but some could not be compared at all
        return Verdict.NEEDS_ACTION
    return Verdict.BEHAVIOR_STABLE


def _trace_result(
    node: NodeRef, diff: BoundaryDiff, honesty: list[str], detail: dict
) -> NodeVerifyResult:
    if diff.skipped_opaque:
        honesty.append(f"opaque-comparisons:{len(diff.skipped_opaque)}")
    full_detail = dict(detail)
    full_detail["boundary_diff"] = diff.model_dump()
    return NodeVerifyResult(
        node=node,
        verdict=_verdict_from_boundary(diff),
        evidence_level=EvidenceLevel.TRACE,
        detail=full_detail,
        honesty=honesty,
    )


def verify_node(node: NodeRef, scope: TraceScope, deps: CascadeDeps) -> NodeVerifyResult:
    """Run the cascade for one node and return its typed behavioral result."""
    honesty: list[str] = []
    crosscheck: dict = {}

    # ── L1 TESTS ──────────────────────────────────────────────────────────────
    outcome = deps.run_tests(node)
    crosscheck["tests"] = {"status": outcome.status.value, **outcome.detail}
    exercised = deps.is_exercised(node)
    if not exercised:
        honesty.append("coverage-blind-spot")

    if scope is TraceScope.BLIND_SPOTS:
        if outcome.status is TestStatus.FAIL:
            return NodeVerifyResult(node=node, verdict=Verdict.BEHAVIOR_CHANGED,
                                    evidence_level=EvidenceLevel.TESTS,
                                    detail={"tests": crosscheck["tests"]}, honesty=honesty)
        if outcome.status is TestStatus.PASS and exercised:
            # confident pass on an exercised node -> settled by tests, do not trace
            return NodeVerifyResult(node=node, verdict=Verdict.BEHAVIOR_STABLE,
                                    evidence_level=EvidenceLevel.TESTS,
                                    detail={"tests": crosscheck["tests"]}, honesty=honesty)
        # inconclusive, or pass-but-blind -> descend
        if outcome.status is TestStatus.INCONCLUSIVE:
            honesty.append("tests-inconclusive")

        # ── L2 GOLDEN ─────────────────────────────────────────────────────────
        golden = deps.run_golden(node)
        if golden is not None:
            crosscheck["golden"] = {"changed": len(golden.changed), "skipped": len(golden.skipped)}
            if not golden.is_clean():
                return NodeVerifyResult(
                    node=node,
                    verdict=Verdict.BEHAVIOR_CHANGED,
                    evidence_level=EvidenceLevel.GOLDEN,
                    detail={"golden": golden.model_dump(), "tests": crosscheck["tests"]},
                    honesty=honesty,
                )
            if golden.skipped:
                honesty.append(f"golden-skipped:{len(golden.skipped)}")
            else:
                return NodeVerifyResult(
                    node=node,
                    verdict=Verdict.BEHAVIOR_STABLE,
                    evidence_level=EvidenceLevel.GOLDEN,
                    detail={"golden": golden.model_dump(), "tests": crosscheck["tests"]},
                    honesty=honesty,
                )
        else:
            honesty.append("no-golden-points")

        # ── L3 TRACE (terminal) ───────────────────────────────────────────────
        diff = deps.run_trace(node)
        return _trace_result(node, diff, honesty, {"crosscheck": crosscheck})

    # ── FULL scope: L1/L2 are cross-check only; L3 is always terminal ─────────
    honesty.append("scope-full")
    golden = deps.run_golden(node)
    if golden is not None:
        crosscheck["golden"] = {"changed": len(golden.changed), "skipped": len(golden.skipped)}
    diff = deps.run_trace(node)
    return _trace_result(node, diff, honesty, {"crosscheck": crosscheck})


def run_cascade(nodes: Iterable[NodeRef], scope: TraceScope, deps: CascadeDeps) -> VerifyReport:
    """Run the cascade over every node and assemble the per-run ``VerifyReport``."""
    nodes = list(nodes)
    results = [verify_node(n, scope, deps) for n in nodes]
    exercised = sum(1 for n in nodes if deps.is_exercised(n))
    return VerifyReport(
        results=results,
        scope=scope,
        coverage_summary={
            "exercised": exercised,
            "blind": len(nodes) - exercised,
            "total": len(nodes),
        },
        warnings=[],
    )


# EXTENSION POINT (deferred): derive the node set automatically from the dependency graph
# (which packages changed version × where our code touches them). Today the cascade takes an
# explicit node list so it is usable standalone; nothing here depends on that wiring.
def nodes_from_graph(graph) -> list[NodeRef]:  # pragma: no cover - documented seam
    raise NotImplementedError(
        "graph integration is a marked extension point; pass an explicit list of NodeRef "
        "(name, old_version, new_version, trace_prefix)."
    )
