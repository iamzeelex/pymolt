from __future__ import annotations

import pytest

from pymolt.migration_state import MigrationReceipt
from pymolt.verify.models import VerificationVerdict
from pymolt.verify.policy import (
    AutoRollbackPolicy,
    CanaryMetrics,
    CanaryPolicy,
    CanaryPolicyEvaluation,
    MetricGate,
    MetricName,
    decide_auto_rollback,
    evaluate_canary_policy,
)


def _policy() -> CanaryPolicy:
    return CanaryPolicy(
        error_rate=MetricGate(threshold=0.01),
        p95_latency_ms=MetricGate(threshold=100),
        p99_latency_ms=MetricGate(threshold=250),
        output_shape_mismatch_rate=MetricGate(threshold=0.005),
        dependency_call_count_drift=MetricGate(threshold=0.1),
        dropped_event_rate=MetricGate(threshold=0.02),
    )


def _boundary_metrics() -> CanaryMetrics:
    return CanaryMetrics(
        error_rate=0.01,
        p95_latency_ms=100,
        p99_latency_ms=250,
        output_shape_mismatch_rate=0.005,
        dependency_call_count_drift=0.1,
        dropped_event_rate=0.02,
    )


def _failed_evaluation() -> CanaryPolicyEvaluation:
    metrics = _boundary_metrics().model_copy(update={"error_rate": 0.02})
    return evaluate_canary_policy(_policy(), metrics)


def test_threshold_boundaries_pass_with_ordered_metric_evidence() -> None:
    result = evaluate_canary_policy(_policy(), _boundary_metrics())

    assert result.verdict is VerificationVerdict.PASS
    assert [item.metric for item in result.evidence] == list(MetricName)
    assert all(item.verdict is VerificationVerdict.PASS for item in result.evidence)
    assert result.evidence[0].comparator == "<="
    assert result.evidence[1].unit == "milliseconds"


@pytest.mark.parametrize(
    ("metric", "value"),
    [
        ("error_rate", 0.010001),
        ("p95_latency_ms", 100.001),
        ("p99_latency_ms", 250.001),
        ("output_shape_mismatch_rate", 0.005001),
        ("dependency_call_count_drift", 0.100001),
        ("dropped_event_rate", 0.020001),
    ],
)
def test_each_threshold_breach_fails(metric: str, value: float) -> None:
    metrics = _boundary_metrics().model_copy(update={metric: value})

    result = evaluate_canary_policy(_policy(), metrics)

    assert result.verdict is VerificationVerdict.FAIL
    failed = [item for item in result.evidence if item.verdict is VerificationVerdict.FAIL]
    assert [item.metric.value for item in failed] == [metric]
    assert "exceeds maximum" in failed[0].reason


def test_missing_mandatory_metric_is_inconclusive_and_never_passes() -> None:
    metrics = _boundary_metrics().model_copy(update={"p99_latency_ms": None})

    result = evaluate_canary_policy(_policy(), metrics)

    assert result.verdict is VerificationVerdict.INCONCLUSIVE
    assert result.reasons == ["mandatory metric p99_latency_ms is missing"]
    p99 = next(item for item in result.evidence if item.metric is MetricName.P99_LATENCY_MS)
    assert p99.observed is None
    assert p99.verdict is VerificationVerdict.INCONCLUSIVE


def test_failure_takes_precedence_but_missing_evidence_is_still_reported() -> None:
    metrics = _boundary_metrics().model_copy(
        update={"error_rate": 0.02, "p99_latency_ms": None}
    )

    result = evaluate_canary_policy(_policy(), metrics)

    assert result.verdict is VerificationVerdict.FAIL
    assert result.reasons == [
        "error_rate 0.02 exceeds maximum 0.01",
        "mandatory metric p99_latency_ms is missing",
    ]


def test_missing_optional_or_disabled_gate_does_not_block_pass() -> None:
    policy = _policy().model_copy(
        update={
            "dependency_call_count_drift": MetricGate(
                threshold=0.1, mandatory=False
            ),
            "dropped_event_rate": MetricGate(threshold=0.02, enabled=False),
        }
    )
    metrics = _boundary_metrics().model_copy(
        update={"dependency_call_count_drift": None, "dropped_event_rate": None}
    )

    result = evaluate_canary_policy(policy, metrics)

    assert result.verdict is VerificationVerdict.PASS
    assert result.evidence[-2].verdict is VerificationVerdict.INCONCLUSIVE
    assert result.evidence[-1].enabled is False


def test_policy_json_defaults_are_backward_safe_and_future_fields_are_ignored() -> None:
    payload = _policy().model_dump(mode="json")
    payload["future_policy_field"] = {"safe": True}
    payload["error_rate"] = {"threshold": 0.01, "future_gate_field": "ignored"}

    restored = CanaryPolicy.model_validate(payload)
    result = evaluate_canary_policy(restored, _boundary_metrics())
    round_tripped = CanaryPolicyEvaluation.model_validate(
        result.model_dump(mode="json") | {"future_result_field": 42}
    )

    assert restored.error_rate.mandatory is True
    assert restored.error_rate.enabled is True
    assert round_tripped == result


def test_auto_rollback_is_opt_in_and_requires_failed_policy() -> None:
    receipt = MigrationReceipt(
        run_id="run-123",
        target_python="3.13",
        source_manifest="requirements.txt",
    )

    disabled = decide_auto_rollback(
        _failed_evaluation(), AutoRollbackPolicy(), receipt=receipt
    )
    passing = decide_auto_rollback(
        evaluate_canary_policy(_policy(), _boundary_metrics()),
        AutoRollbackPolicy(enabled=True),
        receipt=receipt,
    )

    assert disabled.should_rollback is False
    assert "explicit opt-in" in disabled.reasons[0]
    assert passing.should_rollback is False
    assert "requires a FAIL" in passing.reasons[0]


def test_auto_rollback_requires_matching_applied_receipt_and_run_id() -> None:
    enabled = AutoRollbackPolicy(enabled=True)
    failed = _failed_evaluation()
    applied = MigrationReceipt(
        run_id="run-123",
        target_python="3.13",
        source_manifest="requirements.txt",
    )
    rolled_back = applied.model_copy(update={"status": "rolled_back"})

    assert decide_auto_rollback(failed, enabled).should_rollback is False
    assert (
        decide_auto_rollback(failed, enabled, receipt=rolled_back).should_rollback
        is False
    )
    mismatch = decide_auto_rollback(
        failed, enabled, receipt=applied, run_id="another-run"
    )
    approved = decide_auto_rollback(
        failed, enabled, receipt=applied, run_id="run-123"
    )

    assert mismatch.should_rollback is False
    assert "does not match" in mismatch.reasons[0]
    assert approved.should_rollback is True
    assert approved.run_id == "run-123"
    assert approved.receipt_status == "applied"


def test_auto_rollback_ignores_failure_of_optional_gate() -> None:
    policy = _policy().model_copy(update={
        "error_rate": MetricGate(threshold=0.01, mandatory=False),
    })
    evaluation = evaluate_canary_policy(
        policy, _boundary_metrics().model_copy(update={"error_rate": 0.02})
    )
    receipt = MigrationReceipt(
        run_id="run-123",
        target_python="3.13",
        source_manifest="requirements.txt",
    )

    decision = decide_auto_rollback(
        evaluation, AutoRollbackPolicy(enabled=True), receipt=receipt
    )

    assert evaluation.verdict is VerificationVerdict.FAIL
    assert decision.should_rollback is False
    assert decision.reasons == ["no mandatory canary gate failed"]
