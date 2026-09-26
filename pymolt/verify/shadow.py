"""Safety classification for operations considered for shadow execution."""

from __future__ import annotations

from enum import StrEnum

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

SHADOW_POLICY_SCHEMA_VERSION = 1

_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_IDEMPOTENT_METHODS = _READ_METHODS | {"PUT", "DELETE"}
_STANDARD_HTTP_METHODS = _IDEMPOTENT_METHODS | {"CONNECT", "PATCH", "POST"}


class ShadowDisposition(StrEnum):
    ELIGIBLE = "eligible"
    SUPPRESSED = "suppressed"


class ShadowOperation(BaseModel):
    """One boundary operation and the safety facts known about it."""

    model_config = ConfigDict(extra="ignore")

    operation_id: str = Field(min_length=1)
    http_method: str | None = None
    read_only: bool | None = None
    idempotent: bool | None = None
    external_write: bool | None = None

    @field_validator("operation_id")
    @classmethod
    def _strip_operation_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("operation_id must not be blank")
        return value

    @field_validator("http_method")
    @classmethod
    def _normalise_method(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().upper()
        return value or None

    @model_validator(mode="after")
    def _reject_contradictory_facts(self) -> ShadowOperation:
        if self.read_only is True and self.external_write is True:
            raise ValueError("an operation cannot be both read-only and an external write")
        return self


class ShadowPolicy(BaseModel):
    """User overrides around the conservative shadow classifier.

    Denylists always win.  All external writes require idempotence; by default,
    even an idempotent write also requires an explicit operation or HTTP-method
    allowlist entry.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    schema_version: int = SHADOW_POLICY_SCHEMA_VERSION
    operation_allowlist: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("operation_allowlist", "allowlist"),
    )
    operation_denylist: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("operation_denylist", "denylist"),
    )
    http_method_allowlist: list[str] = Field(default_factory=list)
    http_method_denylist: list[str] = Field(default_factory=list)
    require_allowlist_for_external_writes: bool = True

    @field_validator("operation_allowlist", "operation_denylist")
    @classmethod
    def _normalise_operations(cls, values: list[str]) -> list[str]:
        return sorted({value.strip() for value in values if value.strip()})

    @field_validator("http_method_allowlist", "http_method_denylist")
    @classmethod
    def _normalise_methods(cls, values: list[str]) -> list[str]:
        return sorted({value.strip().upper() for value in values if value.strip()})


class ShadowDecision(BaseModel):
    """Serializable explanation of why one operation may or may not be shadowed."""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = SHADOW_POLICY_SCHEMA_VERSION
    operation_id: str
    http_method: str | None = None
    disposition: ShadowDisposition
    eligible: bool
    read_only: bool | None = None
    idempotent: bool | None = None
    external_write: bool | None = None
    allowlisted: bool = False
    denylisted: bool = False
    reason: str


class ShadowReport(BaseModel):
    """Ordered batch report that keeps every suppressed operation visible."""

    model_config = ConfigDict(extra="ignore")

    schema_version: int = SHADOW_POLICY_SCHEMA_VERSION
    decisions: list[ShadowDecision] = Field(default_factory=list)
    eligible_count: int = 0
    suppressed_count: int = 0


def _effective_safety(
    operation: ShadowOperation,
) -> tuple[bool | None, bool | None, bool | None]:
    method = operation.http_method
    method_known = method in _STANDARD_HTTP_METHODS if method else False

    read_only = operation.read_only
    if read_only is None and method_known:
        read_only = method in _READ_METHODS

    idempotent = operation.idempotent
    if idempotent is None and method_known:
        idempotent = method in _IDEMPOTENT_METHODS

    external_write = operation.external_write
    if external_write is None:
        if operation.read_only is True:
            external_write = False
        elif method_known:
            external_write = method not in _READ_METHODS

    return read_only, idempotent, external_write


def classify_shadow_operation(
    operation: ShadowOperation,
    policy: ShadowPolicy | None = None,
) -> ShadowDecision:
    """Conservatively classify a single operation without executing it."""

    policy = policy or ShadowPolicy()
    method = operation.http_method
    operation_denied = operation.operation_id in policy.operation_denylist
    method_denied = method is not None and method in policy.http_method_denylist
    denylisted = operation_denied or method_denied
    allowlisted = (
        operation.operation_id in policy.operation_allowlist
        or (method is not None and method in policy.http_method_allowlist)
    )
    read_only, idempotent, external_write = _effective_safety(operation)

    base = {
        "operation_id": operation.operation_id,
        "http_method": method,
        "read_only": read_only,
        "idempotent": idempotent,
        "external_write": external_write,
        "allowlisted": allowlisted,
        "denylisted": denylisted,
    }

    if denylisted:
        return ShadowDecision(
            **base,
            disposition=ShadowDisposition.SUPPRESSED,
            eligible=False,
            reason="operation is explicitly denylisted",
        )

    if read_only is True and external_write is not True:
        return ShadowDecision(
            **base,
            disposition=ShadowDisposition.ELIGIBLE,
            eligible=True,
            reason="operation is read-only",
        )

    if external_write is True:
        if idempotent is not True:
            return ShadowDecision(
                **base,
                disposition=ShadowDisposition.SUPPRESSED,
                eligible=False,
                reason="unsafe external write is not explicitly idempotent",
            )
        if policy.require_allowlist_for_external_writes and not allowlisted:
            return ShadowDecision(
                **base,
                disposition=ShadowDisposition.SUPPRESSED,
                eligible=False,
                reason="idempotent external write is not explicitly allowlisted",
            )
        permission_reason = (
            "idempotent external write is explicitly allowlisted"
            if allowlisted
            else "idempotent external write is permitted by policy"
        )
        return ShadowDecision(
            **base,
            disposition=ShadowDisposition.ELIGIBLE,
            eligible=True,
            reason=permission_reason,
        )

    if external_write is None:
        return ShadowDecision(
            **base,
            disposition=ShadowDisposition.SUPPRESSED,
            eligible=False,
            reason="external side-effect safety is unknown",
        )

    if idempotent is True:
        return ShadowDecision(
            **base,
            disposition=ShadowDisposition.ELIGIBLE,
            eligible=True,
            reason="operation is explicitly idempotent and has no external write",
        )

    return ShadowDecision(
        **base,
        disposition=ShadowDisposition.SUPPRESSED,
        eligible=False,
        reason="operation is neither read-only nor explicitly idempotent",
    )


def classify_shadow_operations(
    operations: list[ShadowOperation],
    policy: ShadowPolicy | None = None,
) -> ShadowReport:
    """Classify a batch while preserving input order for deterministic reports."""

    effective_policy = policy or ShadowPolicy()
    decisions = [
        classify_shadow_operation(operation, effective_policy)
        for operation in operations
    ]
    eligible_count = sum(decision.eligible for decision in decisions)
    return ShadowReport(
        decisions=decisions,
        eligible_count=eligible_count,
        suppressed_count=len(decisions) - eligible_count,
    )
