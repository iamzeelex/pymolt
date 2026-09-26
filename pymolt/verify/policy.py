"""Deterministic canary safety policy evaluation.

This module deliberately produces decisions only.  In particular,
``decide_auto_rollback`` never imports or invokes the rollback implementation;
the caller remains responsible for executing an approved decision.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pymolt.migration_state import MigrationReceipt
from pymolt.verify.models import VerificationVerdict

POLICY_SCHEMA_VERSION = 1


class MetricName(StrEnum):
    """Stable machine names for canary safety signals."""

    ERROR_RATE = "error_rate"
    P95_LATENCY_MS = "p95_latency_ms"
    P99_LATENCY_MS = "p99_latency_ms"
    OUTPUT_SHAPE_MISMATCH_RATE = "output_shape_mismatch_rate"
    DEPENDENCY_CALL_COUNT_DRIFT = "dependency_call_count_drift"
    DROPPED_EVENT_RATE = "dropped_event_rate"


class MetricGate(BaseModel):
    """Maximum accepted value for one canary metric."""

    model_config = ConfigDict(extra="ignore")

    threshold: float = Field(ge=0, allow_inf_nan=False)
    mandatory: bool = True
    enabled: bool = True


class CanaryPolicy(BaseModel):
    """Thresholds that a canary must satisfy before promotion."""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = POLICY_SCHEMA_VERSION
    error_rate: MetricGate
    p95_latency_ms: MetricGate
    p99_latency_ms: MetricGate
    output_shape_mismatch_rate: MetricGate
    dependency_call_count_drift: MetricGate
    dropped_event_rate: MetricGate

    @model_validator(mode="after")
    def _rates_are_ratios(self) -> CanaryPolicy:
        for field_name in (
            "error_rate",
            "output_shape_mismatch_rate",
            "dropped_event_rate",
        ):
            if getattr(self, field_name).threshold > 1:
                raise ValueError(f"{field_name} threshold must be between 0 and 1")
        return self


class CanaryMetrics(BaseModel):
    """Observed canary values; ``None`` means no usable evidence was collected."""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = POLICY_SCHEMA_VERSION
    error_rate: float | None = Field(
        default=None, ge=0, le=1, allow_inf_nan=False
    )
    p95_latency_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    p99_latency_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    output_shape_mismatch_rate: float | None = Field(
        default=None, ge=0, le=1, allow_inf_nan=False
    )
    dependency_call_count_drift: float | None = Field(
        default=None, ge=0, allow_inf_nan=False
    )
    dropped_event_rate: float | None = Field(
        default=None, ge=0, le=1, allow_inf_nan=False
    )


class MetricEvidence(BaseModel):
    """The complete, serializable comparison behind one gate result."""

    model_config = ConfigDict(extra="ignore")

    metric: MetricName
    observed: float | None = None
    threshold: float
    comparator: Literal["<="] = "<="
    unit: Literal["ratio", "milliseconds"]
    mandatory: bool
    enabled: bool
    verdict: VerificationVerdict
    reason: str


class CanaryPolicyEvaluation(BaseModel):
    """Aggregate canary verdict with ordered reasons and metric evidence."""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = POLICY_SCHEMA_VERSION
    verdict: VerificationVerdict
    reasons: list[str] = Field(default_factory=list)
    evidence: list[MetricEvidence] = Field(default_factory=list)


_METRICS: tuple[tuple[MetricName, str, Literal["ratio", "milliseconds"]], ...] = (
    (MetricName.ERROR_RATE, "error_rate", "ratio"),
    (MetricName.P95_LATENCY_MS, "p95_latency_ms", "milliseconds"),
    (MetricName.P99_LATENCY_MS, "p99_latency_ms", "milliseconds"),
    (
        MetricName.OUTPUT_SHAPE_MISMATCH_RATE,
        "output_shape_mismatch_rate",
        "ratio",
    ),
    (
        MetricName.DEPENDENCY_CALL_COUNT_DRIFT,
        "dependency_call_count_drift",
        "ratio",
    ),
    (MetricName.DROPPED_EVENT_RATE, "dropped_event_rate", "ratio"),
)


def _number(value: float) -> str:
    return format(value, ".12g")


def evaluate_canary_policy(
    policy: CanaryPolicy,
    metrics: CanaryMetrics,
) -> CanaryPolicyEvaluation:
    """Evaluate every gate in a stable order.

    A concrete threshold breach wins over missing evidence and yields ``FAIL``.
    Otherwise any missing mandatory value yields ``INCONCLUSIVE``.  Only a
    fully satisfied set of enabled mandatory gates can yield ``PASS``.
    """

    evidence: list[MetricEvidence] = []
    blocking_reasons: list[str] = []
    has_failure = False
    has_missing_mandatory = False

    for metric_name, field_name, unit in _METRICS:
        gate: MetricGate = getattr(policy, field_name)
        observed: float | None = getattr(metrics, field_name)

        if not gate.enabled:
            reason = f"{metric_name.value} gate is disabled"
            evidence.append(
                MetricEvidence(
                    metric=metric_name,
                    observed=observed,
                    threshold=gate.threshold,
                    unit=unit,
                    mandatory=gate.mandatory,
                    enabled=False,
                    verdict=VerificationVerdict.INCONCLUSIVE,
                    reason=reason,
                )
            )
            continue

        if observed is None:
            qualifier = "mandatory" if gate.mandatory else "optional"
            reason = f"{qualifier} metric {metric_name.value} is missing"
            evidence.append(
                MetricEvidence(
                    metric=metric_name,
                    threshold=gate.threshold,
                    unit=unit,
                    mandatory=gate.mandatory,
                    enabled=True,
                    verdict=VerificationVerdict.INCONCLUSIVE,
                    reason=reason,
                )
            )
            if gate.mandatory:
                has_missing_mandatory = True
                blocking_reasons.append(reason)
            continue

        if observed <= gate.threshold:
            verdict = VerificationVerdict.PASS
            reason = (
                f"{metric_name.value} {_number(observed)} is within maximum "
                f"{_number(gate.threshold)}"
            )
        else:
            verdict = VerificationVerdict.FAIL
            reason = (
                f"{metric_name.value} {_number(observed)} exceeds maximum "
                f"{_number(gate.threshold)}"
            )
            has_failure = True
            blocking_reasons.append(reason)

        evidence.append(
            MetricEvidence(
                metric=metric_name,
                observed=observed,
                threshold=gate.threshold,
                unit=unit,
                mandatory=gate.mandatory,
                enabled=True,
                verdict=verdict,
                reason=reason,
            )
        )

    if has_failure:
        verdict = VerificationVerdict.FAIL
    elif has_missing_mandatory:
        verdict = VerificationVerdict.INCONCLUSIVE
    else:
        verdict = VerificationVerdict.PASS
        blocking_reasons = [
            "all enabled mandatory canary gates have sufficient passing evidence"
        ]

    return CanaryPolicyEvaluation(
        verdict=verdict,
        reasons=blocking_reasons,
        evidence=evidence,
    )


class AutoRollbackPolicy(BaseModel):
    """Explicit opt-in for turning a failed policy into a rollback recommendation."""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = POLICY_SCHEMA_VERSION
    enabled: bool = False


class AutoRollbackDecision(BaseModel):
    """Pure recommendation; executing rollback is intentionally out of scope."""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = POLICY_SCHEMA_VERSION
    should_rollback: bool = False
    run_id: str | None = None
    policy_verdict: VerificationVerdict
    receipt_status: Literal["applied", "rolled_back"] | None = None
    reasons: list[str] = Field(default_factory=list)


def decide_auto_rollback(
    evaluation: CanaryPolicyEvaluation,
    policy: AutoRollbackPolicy,
    *,
    receipt: MigrationReceipt | None = None,
    run_id: str | None = None,
) -> AutoRollbackDecision:
    """Recommend rollback only for an opted-in failed, applied migration."""

    base = {
        "policy_verdict": evaluation.verdict,
        "receipt_status": receipt.status if receipt is not None else None,
    }
    if not policy.enabled:
        return AutoRollbackDecision(
            **base,
            reasons=["automatic rollback is disabled; explicit opt-in is required"],
        )
    if evaluation.verdict is not VerificationVerdict.FAIL:
        return AutoRollbackDecision(
            **base,
            reasons=[
                "automatic rollback requires a FAIL canary verdict; "
                f"received {evaluation.verdict.value}"
            ],
        )
    mandatory_failures = [
        item for item in evaluation.evidence
        if item.mandatory and item.verdict is VerificationVerdict.FAIL
    ]
    if not mandatory_failures:
        return AutoRollbackDecision(
            **base,
            reasons=["no mandatory canary gate failed"],
        )
    if receipt is None:
        return AutoRollbackDecision(
            **base,
            reasons=["no applied migration receipt is available"],
        )
    if receipt.status != "applied":
        return AutoRollbackDecision(
            **base,
            reasons=[f"migration receipt is {receipt.status}, not applied"],
        )
    receipt_run_id = receipt.run_id.strip()
    if not receipt_run_id:
        return AutoRollbackDecision(
            **base,
            reasons=["applied migration receipt has no run id"],
        )
    if run_id is not None and run_id != receipt_run_id:
        return AutoRollbackDecision(
            **base,
            run_id=run_id,
            reasons=[
                f"requested run id {run_id!r} does not match applied receipt "
                f"{receipt_run_id!r}"
            ],
        )
    return AutoRollbackDecision(
        **base,
        should_rollback=True,
        run_id=receipt_run_id,
        reasons=[
            "canary policy failed and automatic rollback is enabled for the "
            "applied migration"
        ],
    )
