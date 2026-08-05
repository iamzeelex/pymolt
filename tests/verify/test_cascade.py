"""Cascade: early-exit, coverage-gated scope switching, honesty markers. Pure, with fakes."""

from pymolt.core.enums import EvidenceLevel, TestStatus, TraceScope, Verdict
from pymolt.verify.cascade import run_cascade, verify_node
from pymolt.verify.models import BoundaryDiff, GoldenDiff, TestOutcome


class FakeDeps:
    """Configurable cascade dependencies that record which levels were invoked."""

    def __init__(self, *, tests=TestStatus.PASS, exercised=True, golden=None,
                 boundary=None):
        self._tests = tests
        self._exercised = exercised
        self._golden = golden
        self._boundary = boundary if boundary is not None else BoundaryDiff()
        self.traced = []
        self.golden_calls = 0

    def run_tests(self, node):
        return TestOutcome(status=self._tests, detail={"suite": "x"})

    def is_exercised(self, node):
        return self._exercised

    def run_golden(self, node):
        self.golden_calls += 1
        return self._golden

    def run_trace(self, node):
        self.traced.append(node.name)
        return self._boundary


def test_l1_confident_pass_on_exercised_node_exits_at_tests(flask_node):
    deps = FakeDeps(tests=TestStatus.PASS, exercised=True)
    res = verify_node(flask_node, TraceScope.BLIND_SPOTS, deps)
    assert res.verdict is Verdict.BEHAVIOR_STABLE
    assert res.evidence_level is EvidenceLevel.TESTS
    assert deps.traced == []  # not traced — settled by tests


def test_l1_fail_exits_at_tests_as_changed(flask_node):
    deps = FakeDeps(tests=TestStatus.FAIL)
    res = verify_node(flask_node, TraceScope.BLIND_SPOTS, deps)
    assert res.verdict is Verdict.BEHAVIOR_CHANGED
    assert res.evidence_level is EvidenceLevel.TESTS
    assert deps.traced == []


def test_pass_but_blind_descends_past_tests(flask_node):
    # A green suite proves nothing about an unexercised path -> must not early-exit.
    deps = FakeDeps(tests=TestStatus.PASS, exercised=False, golden=None)
    res = verify_node(flask_node, TraceScope.BLIND_SPOTS, deps)
    assert deps.traced == [flask_node.name]      # descended to trace
    assert "coverage-blind-spot" in res.honesty
    assert res.evidence_level is EvidenceLevel.TRACE


def test_l2_golden_settles_when_tests_inconclusive(flask_node):
    deps = FakeDeps(tests=TestStatus.INCONCLUSIVE, exercised=True,
                    golden=GoldenDiff(changed=[{"point": "p"}]))
    res = verify_node(flask_node, TraceScope.BLIND_SPOTS, deps)
    assert res.evidence_level is EvidenceLevel.GOLDEN
    assert res.verdict is Verdict.BEHAVIOR_CHANGED
    assert deps.traced == []                      # golden settled it; no trace
    assert "tests-inconclusive" in res.honesty


def test_l3_trace_terminal_when_no_golden_points(flask_node):
    deps = FakeDeps(tests=TestStatus.INCONCLUSIVE, exercised=True, golden=None,
                    boundary=BoundaryDiff(disappeared=[{"qualname": "flask.x"}]))
    res = verify_node(flask_node, TraceScope.BLIND_SPOTS, deps)
    assert res.evidence_level is EvidenceLevel.TRACE
    assert res.verdict is Verdict.BEHAVIOR_CHANGED
    assert "no-golden-points" in res.honesty


def test_skipped_opaque_only_yields_needs_action(flask_node):
    deps = FakeDeps(tests=TestStatus.INCONCLUSIVE, golden=None,
                    boundary=BoundaryDiff(skipped_opaque=[{"qualname": "flask.x"}]))
    res = verify_node(flask_node, TraceScope.BLIND_SPOTS, deps)
    assert res.verdict is Verdict.NEEDS_ACTION
    assert any(h.startswith("opaque-comparisons") for h in res.honesty)


def test_scope_switch_settled_node_skipped_in_blindspots_traced_in_full(flask_node):
    # The headline DoD: a node settled by L1 is skipped under BLIND_SPOTS, traced under FULL.
    blind = FakeDeps(tests=TestStatus.PASS, exercised=True)
    verify_node(flask_node, TraceScope.BLIND_SPOTS, blind)
    assert blind.traced == []

    full = FakeDeps(tests=TestStatus.PASS, exercised=True)
    res = verify_node(flask_node, TraceScope.FULL, full)
    assert full.traced == [flask_node.name]
    assert res.evidence_level is EvidenceLevel.TRACE
    assert "scope-full" in res.honesty


def test_run_cascade_assembles_report_with_coverage_summary(flask_node, marshmallow_node):
    deps = FakeDeps(tests=TestStatus.PASS, exercised=True)
    report = run_cascade([flask_node, marshmallow_node], TraceScope.BLIND_SPOTS, deps)
    assert report.scope is TraceScope.BLIND_SPOTS
    assert len(report.results) == 2
    assert report.coverage_summary == {"exercised": 2, "blind": 0, "total": 2}
