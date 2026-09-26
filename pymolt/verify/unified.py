"""One persisted verdict across contract, cascade, golden, and canary evidence."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from pymolt.core.enums import Verdict
from pymolt.verify.models import GoldenDiff, VerificationVerdict, VerifyReport
from pymolt.verify.policy import AutoRollbackDecision, CanaryPolicyEvaluation
from pymolt.verify.report import ContractReport
from pymolt.verify.shadow import ShadowReport

VERIFICATION_PATH = Path(".pymolt/verification.json")


class VerificationEvidence(BaseModel):
    name: str
    verdict: VerificationVerdict
    reasons: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class UnifiedVerificationReport(BaseModel):
    schema_version: int = 1
    created_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )
    run_id: str | None = None
    comparator_profile: str = "exact"
    verdict: VerificationVerdict
    reasons: list[str] = Field(default_factory=list)
    evidence: list[VerificationEvidence] = Field(default_factory=list)
    contract: ContractReport
    golden: GoldenDiff | None = None
    canary: CanaryPolicyEvaluation | None = None
    shadow: ShadowReport | None = None
    rollback: AutoRollbackDecision | None = None
    rollback_executed: bool = False

    def save(self, project_dir: str | Path) -> Path:
        path = Path(project_dir) / VERIFICATION_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, staged = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.model_dump(mode="json"), stream, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staged, path)
        except BaseException:
            try:
                os.unlink(staged)
            except OSError:
                pass
            raise
        return path

    @classmethod
    def load(cls, project_dir: str | Path) -> UnifiedVerificationReport | None:
        path = Path(project_dir) / VERIFICATION_PATH
        try:
            return cls.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError):
            return None


def _cascade_evidence(report: VerifyReport) -> VerificationEvidence:
    verdicts = [item.verdict for item in report.results]
    if Verdict.BEHAVIOR_CHANGED in verdicts:
        verdict = VerificationVerdict.FAIL
    elif Verdict.NEEDS_ACTION in verdicts or not verdicts:
        verdict = VerificationVerdict.INCONCLUSIVE
    else:
        verdict = VerificationVerdict.PASS
    reasons = [
        f"{item.node.name}: {item.verdict.value}"
        for item in report.results
        if item.verdict is not Verdict.BEHAVIOR_STABLE
    ]
    if not report.results:
        reasons.append("cascade produced no node evidence")
    return VerificationEvidence(
        name="cascade",
        verdict=verdict,
        reasons=reasons,
        details={"nodes": len(report.results), "coverage": report.coverage_summary},
    )


def _golden_evidence(diff: GoldenDiff) -> VerificationEvidence:
    if diff.changed or diff.missing:
        verdict = VerificationVerdict.FAIL
        reasons = []
        if diff.changed:
            reasons.append(f"{len(diff.changed)} golden value(s) changed")
        if diff.missing:
            reasons.append(f"{len(diff.missing)} golden point(s) are missing")
    elif diff.skipped:
        verdict = VerificationVerdict.INCONCLUSIVE
        reasons = [f"{len(diff.skipped)} golden point(s) were skipped"]
    else:
        verdict = VerificationVerdict.PASS
        reasons = []
    return VerificationEvidence(
        name="golden",
        verdict=verdict,
        reasons=reasons,
        details=diff.model_dump(mode="json"),
    )


def build_unified_verification(
    contract: ContractReport,
    *,
    run_id: str | None = None,
    comparator_profile: str = "exact",
    cascade: VerifyReport | None = None,
    golden: GoldenDiff | None = None,
    canary: CanaryPolicyEvaluation | None = None,
    shadow: ShadowReport | None = None,
    rollback: AutoRollbackDecision | None = None,
) -> UnifiedVerificationReport:
    evidence = [VerificationEvidence(
        name="contract",
        verdict=contract.verdict,
        reasons=contract.verdict_reasons,
        details={
            "static_targets": contract.static_targets,
            "confirmed": contract.confirmed,
            "blind": contract.blind,
            "diff": contract.diff,
            "impact_filter_active": contract.impact_filter_active,
        },
    )]
    if cascade is not None:
        evidence.append(_cascade_evidence(cascade))
    if golden is not None:
        evidence.append(_golden_evidence(golden))
    if canary is not None:
        evidence.append(VerificationEvidence(
            name="canary",
            verdict=canary.verdict,
            reasons=canary.reasons,
            details={"evidence": [item.model_dump(mode="json") for item in canary.evidence]},
        ))

    if any(item.verdict is VerificationVerdict.FAIL for item in evidence):
        verdict = VerificationVerdict.FAIL
    elif any(item.verdict is VerificationVerdict.INCONCLUSIVE for item in evidence):
        verdict = VerificationVerdict.INCONCLUSIVE
    else:
        verdict = VerificationVerdict.PASS
    reasons = [
        f"{item.name}: {reason}"
        for item in evidence
        if item.verdict is not VerificationVerdict.PASS
        for reason in (item.reasons or [item.verdict.value])
    ]
    return UnifiedVerificationReport(
        run_id=run_id,
        comparator_profile=comparator_profile,
        verdict=verdict,
        reasons=list(dict.fromkeys(reasons)),
        evidence=evidence,
        contract=contract,
        golden=golden,
        canary=canary,
        shadow=shadow,
        rollback=rollback,
    )
