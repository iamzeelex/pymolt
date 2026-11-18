"""A person's approval of a plan for agents (design 3.6, variant A).

HOST side. ``pymolt apply . --approve-agent``, run by a person on a terminal (an attestation,
``pymolt.attest``; refused under ``--agent``, ``--json`` and MCP), lets agents run the plan's
pymolt-checked steps: ``apply --agent`` writes them, and ``rollback --agent --step`` rolls one
back. It writes ``migrations/<run>/approval.json`` = ``{plan_id, plan_sha256, run_id, scope:
["pymolt-steps", "rollback-step"], approved_at, attestation}``.

An approval holds only for the plan it names, as it was: a re-plan (another plan, another run)
or any change to the plan's content (``plan_sha256``, over its canonical JSON) voids it, and so
does an attestation that does not verify on this machine. Like every gate but the attestation it
stops accidents, not an adversarial agent (design 5.6).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pymolt.migration_plan import (
    MigrationConflictError,
    MigrationPlan,
    _atomic_json,
    _now,
    _run_dir,
)

SCOPE = ("pymolt-steps", "rollback-step")
APPROVAL_FILE = "approval.json"


def plan_digest(plan: MigrationPlan) -> str:
    """sha256 of the plan's canonical JSON: any change to its content changes it."""
    data = json.dumps(plan.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def approval_path(root: Path, run_id: str) -> Path:
    return _run_dir(root, run_id) / APPROVAL_FILE


def approve(project_dir: str | Path, plan: MigrationPlan, *, attester) -> dict:
    """Record a person's approval of ``plan`` for agents. Raises ``MigrationConflictError``
    (exit 2) when no person attests it."""
    from pymolt.attest import project_id
    from pymolt.migration_plan import _migration_lock
    from pymolt.migration_state import MigrationReceipt
    from pymolt.workorder.apply import run_for

    root = Path(project_dir).resolve()
    with _migration_lock(root, "apply --approve-agent"):
        run_id, _state = run_for(root, plan, MigrationReceipt.load(root))
        digest = plan_digest(plan)
        pymolt_steps = [s.id for s in plan.steps if s.executor == "pymolt"]
        text = (f"Approve plan {plan.plan_id[:8]} for agents: they may apply its "
                f"{len(pymolt_steps)} pymolt step(s) ({', '.join(pymolt_steps[:8]) or 'none'}"
                f"{' ...' if len(pymolt_steps) > 8 else ''}) and roll back one step at a time. "
                "A re-plan or any change to the plan voids it.")
        record, why = attester.ask(text, {
            "kind": "approval", "project": project_id(root), "plan_id": plan.plan_id,
            "run_id": run_id, "plan_sha256": digest, "scope": list(SCOPE)}) if attester \
            else (None, "no person can attest here (--agent, --json or MCP)")
        if record is None:
            raise MigrationConflictError(
                f"--approve-agent is a person's attested act: {why}",
                hint="A person runs `pymolt apply . --approve-agent` on a terminal and types "
                     "the nonce it prints.")
        payload = {"plan_id": plan.plan_id, "plan_sha256": digest, "run_id": run_id,
                   "scope": list(SCOPE), "approved_at": _now(), "attestation": record["id"],
                   "user": record.get("user")}
        _atomic_json(approval_path(root, run_id), payload)
    return payload


def check_approval(project_dir: str | Path, plan: MigrationPlan, run_id: str,
                   scope: str) -> str | None:
    """None when a person approved ``plan`` (as it is) for agents in ``scope``; else why not."""
    from pymolt.attest import AttestationStore

    root = Path(project_dir).resolve()
    path = approval_path(root, run_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "no person approved this plan for agents"
    if not isinstance(data, dict) or data.get("plan_id") != plan.plan_id:
        return "the approval is for another plan (a re-plan voids it)"
    digest = plan_digest(plan)
    if data.get("plan_sha256") != digest:
        return "the plan changed since it was approved"
    if scope not in (data.get("scope") or ()):
        return f"the approval does not cover {scope}"
    check = AttestationStore().check(data.get("attestation"), kind="approval", project=root,
                                     plan_id=plan.plan_id, run_id=run_id, plan_sha256=digest)
    if not check.ok:
        return f"the approval's attestation does not hold ({check.reason})"
    return None


def require_approval(project_dir: str | Path, plan: MigrationPlan, run_id: str, scope: str,
                     command: str) -> None:
    """Refuse (exit 2) an agent's write the plan's approval does not cover."""
    why = check_approval(project_dir, plan, run_id, scope)
    if why is not None:
        from pymolt.workorder.apply import StepRefusal

        raise StepRefusal(
            f"under --agent, pymolt {command} only on a plan a person approved for agents, and "
            f"{why}",
            hint="A person runs `pymolt apply . --approve-agent` on a terminal, or runs "
                 f"`pymolt {command}` without --agent.")


def require_rollback_approval(project_dir: str | Path, run_id: str | None) -> None:
    """``rollback --agent --step``: the run's frozen plan must be approved for rolling back a
    step."""
    from pymolt.migration_state import MigrationReceipt

    root = Path(project_dir).resolve()
    receipt = MigrationReceipt.load(root)
    open_runs = [r.run_id for r in (receipt.runs if receipt else []) if not r.closed]
    effective = run_id or (open_runs[-1] if open_runs else None) or (
        receipt.run_id if receipt else None)
    if not effective:
        raise MigrationConflictError("no applied migration is available to roll back")
    plan = MigrationPlan.load(root, _run_dir(root, effective) / "plan.json")
    if plan is None:
        raise MigrationConflictError(f"migration {effective[:8]} has no frozen plan")
    require_approval(root, plan, effective, "rollback-step", "rollback --step")


__all__ = ["APPROVAL_FILE", "SCOPE", "approval_path", "approve", "check_approval",
           "plan_digest", "require_approval", "require_rollback_approval"]
