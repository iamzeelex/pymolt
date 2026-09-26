from __future__ import annotations

import pytest

from pymolt.verify.shadow import (
    ShadowDisposition,
    ShadowOperation,
    ShadowPolicy,
    classify_shadow_operation,
    classify_shadow_operations,
)


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", "TRACE"])
def test_read_only_http_methods_are_eligible(method: str) -> None:
    decision = classify_shadow_operation(
        ShadowOperation(operation_id=f"read-{method.lower()}", http_method=method)
    )

    assert decision.eligible is True
    assert decision.disposition is ShadowDisposition.ELIGIBLE
    assert decision.read_only is True
    assert decision.external_write is False


@pytest.mark.parametrize("method", ["PUT", "DELETE"])
def test_idempotent_external_writes_require_explicit_allowlist(method: str) -> None:
    operation = ShadowOperation(operation_id="write-profile", http_method=method)

    suppressed = classify_shadow_operation(operation)
    eligible = classify_shadow_operation(
        operation,
        ShadowPolicy(operation_allowlist=["write-profile"]),
    )

    assert suppressed.eligible is False
    assert "not explicitly allowlisted" in suppressed.reason
    assert eligible.eligible is True
    assert eligible.idempotent is True
    assert eligible.external_write is True


@pytest.mark.parametrize("method", ["POST", "PATCH", "CONNECT"])
def test_unsafe_external_writes_are_suppressed_even_when_allowlisted(
    method: str,
) -> None:
    decision = classify_shadow_operation(
        ShadowOperation(operation_id="unsafe-write", http_method=method),
        ShadowPolicy(operation_allowlist=["unsafe-write"]),
    )

    assert decision.eligible is False
    assert decision.allowlisted is True
    assert "not explicitly idempotent" in decision.reason


def test_semantically_idempotent_post_needs_and_honours_allowlist() -> None:
    operation = ShadowOperation(
        operation_id="reconcile",
        http_method="post",
        idempotent=True,
        external_write=True,
    )

    suppressed = classify_shadow_operation(operation)
    eligible = classify_shadow_operation(
        operation,
        ShadowPolicy(http_method_allowlist=["post"]),
    )

    assert suppressed.eligible is False
    assert eligible.eligible is True
    assert eligible.http_method == "POST"


def test_policy_can_permit_known_idempotent_writes_without_allowlist() -> None:
    decision = classify_shadow_operation(
        ShadowOperation(operation_id="replace", http_method="PUT"),
        ShadowPolicy(require_allowlist_for_external_writes=False),
    )

    assert decision.eligible is True
    assert decision.allowlisted is False
    assert decision.reason == "idempotent external write is permitted by policy"


def test_unknown_side_effects_are_suppressed_and_reported() -> None:
    decision = classify_shadow_operation(
        ShadowOperation(operation_id="custom-rpc", http_method="CALL")
    )

    assert decision.eligible is False
    assert decision.external_write is None
    assert decision.reason == "external side-effect safety is unknown"


def test_explicit_read_only_fact_can_make_custom_transport_eligible() -> None:
    decision = classify_shadow_operation(
        ShadowOperation(
            operation_id="lookup",
            http_method="CALL",
            read_only=True,
        )
    )

    assert decision.eligible is True
    assert decision.external_write is False


def test_denylist_wins_over_operation_and_method_allowlists() -> None:
    operation = ShadowOperation(operation_id="replace", http_method="PUT")
    policy = ShadowPolicy(
        operation_allowlist=["replace"],
        operation_denylist=["replace"],
        http_method_allowlist=["PUT"],
    )

    decision = classify_shadow_operation(operation, policy)

    assert decision.eligible is False
    assert decision.allowlisted is True
    assert decision.denylisted is True
    assert decision.reason == "operation is explicitly denylisted"


def test_http_method_denylist_suppresses_otherwise_safe_read() -> None:
    decision = classify_shadow_operation(
        ShadowOperation(operation_id="health", http_method="get"),
        ShadowPolicy(http_method_denylist=[" get "]),
    )

    assert decision.eligible is False
    assert decision.http_method == "GET"
    assert decision.denylisted is True


def test_batch_report_preserves_order_and_reports_suppressed_operations() -> None:
    report = classify_shadow_operations(
        [
            ShadowOperation(operation_id="read", http_method="GET"),
            ShadowOperation(operation_id="write", http_method="POST"),
            ShadowOperation(operation_id="unknown"),
        ]
    )

    assert [item.operation_id for item in report.decisions] == [
        "read",
        "write",
        "unknown",
    ]
    assert report.eligible_count == 1
    assert report.suppressed_count == 2


def test_shadow_policy_accepts_legacy_aliases_and_ignores_future_json_fields() -> None:
    policy = ShadowPolicy.model_validate(
        {
            "allowlist": [" put-profile ", "put-profile"],
            "denylist": ["delete-account"],
            "future_policy_field": True,
        }
    )

    dumped = policy.model_dump(mode="json")

    assert policy.operation_allowlist == ["put-profile"]
    assert policy.operation_denylist == ["delete-account"]
    assert "future_policy_field" not in dumped
    assert dumped["schema_version"] == 1
