from pymolt.verify.models import GoldenDiff, VerificationVerdict
from pymolt.verify.report import ContractReport
from pymolt.verify.unified import build_unified_verification


def _contract(verdict=VerificationVerdict.PASS):
    return ContractReport(root=".", verdict=verdict)


def test_unified_verdict_is_fail_dominant_across_oracles():
    report = build_unified_verification(
        _contract(),
        golden=GoldenDiff(changed=[{"point": "response"}]),
    )

    assert report.verdict is VerificationVerdict.FAIL
    assert [item.name for item in report.evidence] == ["contract", "golden"]


def test_skipped_golden_prevents_false_pass():
    report = build_unified_verification(
        _contract(), golden=GoldenDiff(skipped=["opaque"])
    )

    assert report.verdict is VerificationVerdict.INCONCLUSIVE


def test_unified_report_round_trips(tmp_path):
    report = build_unified_verification(_contract(), run_id="abc")
    report.save(tmp_path)

    assert type(report).load(tmp_path) == report
