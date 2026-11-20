"""One persisted verdict across contract, golden, and canary evidence."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from pymolt.state_io import atomic_write_json, load_state, rebuild_remedy, state_lock
from pymolt.verify.models import GoldenDiff, VerificationVerdict
from pymolt.verify.policy import AutoRollbackDecision, CanaryPolicyEvaluation
from pymolt.verify.report import ContractReport
from pymolt.verify.shadow import ShadowReport

VERIFICATION_PATH = Path(".pymolt/verification.json")
#: The newest ``verification.json`` schema this pymolt reads (a higher one is refused as newer).
VERIFICATION_SCHEMA_NEWEST = 2
VERIFICATION_KIND = "saved verdict"
VERIFICATION_REMEDY = rebuild_remedy("run `pymolt verify .` to judge again")


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
    # Each step's judgement (``workorder.judge``): step id -> {status, epoch, entries, failing,
    # review, unexercised, risks}; ``steps_run``: the run they are for.
    steps: dict[str, dict[str, Any]] = Field(default_factory=dict)
    steps_run: str | None = None

    def save(self, project_dir: str | Path) -> Path:
        """Write the verdict (with its steps' judgements) atomically, under the state lock;
        never over a damaged or newer one (``StateFileError``)."""
        path = Path(project_dir) / VERIFICATION_PATH
        with state_lock(project_dir, operation="verify"):
            UnifiedVerificationReport.load(project_dir)
            return atomic_write_json(path, self.model_dump(mode="json"))

    @classmethod
    def load(cls, project_dir: str | Path) -> UnifiedVerificationReport | None:
        """The saved verdict; None when there is none. A damaged file, or one written by a
        newer pymolt, raises ``StateFileError``."""
        return load_state(Path(project_dir) / VERIFICATION_PATH, cls, kind=VERIFICATION_KIND,
                          newest=VERIFICATION_SCHEMA_NEWEST, remedy=VERIFICATION_REMEDY)


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
            "coverage": contract.coverage,
            "recall": contract.recall,
            "coverage_granularity": contract.coverage_granularity,
            "diff": contract.diff,
            "impact_filter_active": contract.impact_filter_active,
        },
    )]
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
    steps_run, steps = _judge_steps(contract)
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
        steps=steps,
        steps_run=steps_run,
    )


def _judge_steps(contract: ContractReport) -> tuple[str | None, dict]:
    """Each plan step's judgement on this comparison (``workorder.judge``); none when the
    report did not compare the two captures, or the judging failed (noted)."""
    if getattr(contract, "_fact_diff", None) is None:
        return None, {}
    try:
        from pymolt.workorder.judge import judge_steps

        return judge_steps(contract.root, contract, contract.calibration)
    except Exception as exc:  # noqa: BLE001 - the verdict stands without step judgements
        contract.notes.append(f"steps not judged ({type(exc).__name__}: {exc})")
        return None, {}
