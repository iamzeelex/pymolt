from pymolt.core.enums import EvidenceLevel, TraceScope, Verdict
from pymolt.verify.models import (
    GoldenDiff,
    NodeRef,
    NodeVerifyResult,
    VerificationVerdict,
    VerifyReport,
)
from pymolt.verify.report import ContractReport
from pymolt.verify.unified import build_unified_verification


def _contract(verdict=VerificationVerdict.PASS):
    return ContractReport(root=".", verdict=verdict)


def test_unified_verdict_is_fail_dominant_across_oracles():
    cascade = VerifyReport(
        scope=TraceScope.FULL,
        results=[NodeVerifyResult(
            node=NodeRef(
                name="dep.api",
                old_version="1",
                new_version="2",
                trace_prefix="dep",
            ),
            verdict=Verdict.BEHAVIOR_STABLE,
            evidence_level=EvidenceLevel.TESTS,
        )],
    )
    report = build_unified_verification(
        _contract(),
        cascade=cascade,
        golden=GoldenDiff(changed=[{"point": "response"}]),
    )

    assert report.verdict is VerificationVerdict.FAIL
    assert [item.name for item in report.evidence] == ["contract", "cascade", "golden"]


def test_skipped_golden_prevents_false_pass():
    report = build_unified_verification(
        _contract(), golden=GoldenDiff(skipped=["opaque"])
    )

    assert report.verdict is VerificationVerdict.INCONCLUSIVE


def test_unified_report_round_trips(tmp_path):
    report = build_unified_verification(_contract(), run_id="abc")
    report.save(tmp_path)

    assert type(report).load(tmp_path) == report
