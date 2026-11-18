"""Where am I in the migration, and what is the next command?

The funnel is a sequence with real gates — you cannot diff before two captures
exist — but pymolt deliberately does *not* enforce that order: people join
mid-flow, CI runs each phase in a fresh checkout, and an agent may want one
answer without the ceremony. A gate would say "no" to all three.

So the order is *projected*, not enforced. This module reads what is already on
disk (``.pymolt/env_config.json``, ``.pymolt/contract_state.json``, the target
manifest) and reports the state of each phase plus the single next command —
orientation without a state machine. It decides nothing and writes nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

#: Phase states. `stale` is deliberately distinct from `todo`: work that was
#: done but no longer describes this project is worse than work not done,
#: because it still looks like an answer.
DONE = "done"
TODO = "todo"
STALE = "stale"
BLOCKED = "blocked"


class PhaseState(BaseModel):
    """One phase of the funnel, as found on disk."""

    phase: str          # scan | setup | assess | baseline | plan | migration | post-migration | report
    state: str          # done | todo | stale | blocked
    detail: str = ""    # what was found, or what is missing
    blocker: str = ""   # why this cannot run yet (state == blocked)


class EnvironmentCheck(BaseModel):
    """Whether one configured environment exists."""

    name: str           # base environment | target environment
    state: str          # ok | warning | error
    detail: str
    hint: str | None = None


class FunnelStatus(BaseModel):
    """The whole projection: every phase, plus what to do next."""

    project_dir: str
    phases: list[PhaseState] = Field(default_factory=list)
    checks: list[EnvironmentCheck] = Field(default_factory=list)
    next_command: str | None = None
    next_reason: str = ""
    notes: list[str] = Field(default_factory=list)

    def phase(self, name: str) -> PhaseState | None:
        return next((p for p in self.phases if p.phase == name), None)


def detect_workload_command(project_dir: str | Path) -> list[str]:
    """Return the most likely repeatable workload, without executing anything."""
    root = Path(project_dir)
    pyproject = root / "pyproject.toml"
    has_pytest_config = any(
        (root / name).is_file()
        for name in ("pytest.ini", "setup.cfg")
    )
    if pyproject.is_file():
        try:
            has_pytest_config = has_pytest_config or "[tool.pytest" in pyproject.read_text(
                encoding="utf-8"
            )
        except OSError:
            pass
    if has_pytest_config or (root / "tests").is_dir():
        return ["python", "-m", "pytest"]
    if (root / "tox.ini").is_file():
        return ["tox"]
    if (root / "noxfile.py").is_file():
        return ["nox"]
    return []


def environment_checks(project_dir: str | Path, config) -> list[EnvironmentCheck]:
    """Whether the configured baseline and target environments exist.

    Writes nothing. A container baseline is checked with ``docker inspect``, which
    is why ``build_status`` runs this only when asked.
    """
    from pymolt.verify.service import container_is_running, display_container

    if config is None:
        return []
    checks: list[EnvironmentCheck] = []
    if config.selected_tool and config.selected_tool.value == "container":
        running = bool(config.container_id and container_is_running(config.container_id))
        checks.append(EnvironmentCheck(
            name="base environment",
            state="ok" if running else "error",
            detail=(
                f"container {display_container(config.container_id)} is running"
                if running else f"container {config.container_id or '?'} is not running"
            ),
            hint=None if running else "Start the container or re-run setup.",
        ))
    else:
        checks.append(EnvironmentCheck(
            name="base environment", state="warning",
            detail=f"local Python {config.base_python or '?'}; interpreter not pinned",
        ))

    if config.target_env_path:
        # Resolved the way `capture` resolves it, so "ok" here means capture finds it.
        target = Path(config.target_env_path)
        if not target.is_absolute():
            target = Path(project_dir) / target
        candidates = (target / "bin" / "python", target / "Scripts" / "python.exe")
        interpreter = next((path for path in candidates if path.is_file()), None)
        checks.append(EnvironmentCheck(
            name="target environment",
            state="ok" if interpreter else "error",
            detail=str(interpreter or target),
            hint=None if interpreter else "Build the configured target environment.",
        ))
    else:
        checks.append(EnvironmentCheck(
            name="target environment", state="warning",
            detail="no target environment configured",
            hint="Build it, then re-run `pymolt setup . --target-env PATH`.",
        ))
    return checks


def _age(iso_timestamp: str | None) -> str:
    """"3 days ago" — captures earn trust by being recent to the code they describe."""
    if not iso_timestamp:
        return ""
    try:
        taken = datetime.fromisoformat(iso_timestamp)
    except ValueError:
        return ""
    if taken.tzinfo is None:
        taken = taken.replace(tzinfo=UTC)
    delta = datetime.now(UTC) - taken
    if delta.days >= 1:
        return f"{delta.days} day{'s' if delta.days != 1 else ''} ago"
    hours = delta.seconds // 3600
    if hours:
        return f"{hours}h ago"
    return f"{max(delta.seconds // 60, 1)}m ago"


def _capture_phase(
    name: str, slot, current_fingerprint: str | None, *, world: str = ""
) -> PhaseState:
    """Project one capture slot, including whether its evidence still applies.

    ``current_fingerprint`` is the environment the capture must describe;
    ``world`` names it in the stale detail when it is not the project as it is now.
    """
    label = "baseline" if name == "baseline" else "post-migration"
    if slot is None:
        return PhaseState(
            phase=label, state=TODO,
            detail="not captured",
        )

    bits = [f"{slot.events} events"]
    if age := _age(slot.captured_at):
        bits.append(age)
    if slot.command:
        bits.append(" ".join(slot.command))
    if slot.coverage_pct is not None:
        bits.append(f"coverage {slot.coverage_pct:.0f}%")
    red = getattr(slot, "red", None)
    if red:
        failed = red.get("failed")
        bits.append(f"RED: {failed if failed is not None else 'some'} test(s) failed "
                    f"(exit {red.get('returncode')})")
    detail = " · ".join(bits)

    if slot.events == 0:
        # An empty recording is not a capture: it will later diff as "everything
        # disappeared", which reads like a finding rather than a missing input.
        return PhaseState(
            phase=label, state=STALE,
            detail=f"{detail} — EMPTY: the command never touched the traced dependency",
        )
    validity = getattr(slot, "validity", None)
    validity_value = getattr(validity, "value", validity)
    if validity_value != "valid":
        reason = getattr(slot, "validity_reason", None) or validity_value or "unknown"
        return PhaseState(
            phase=label, state=STALE,
            detail=f"{detail} — capture validity is {reason}",
        )
    if current_fingerprint is not None and slot.env_fingerprint is None:
        # Taken before setup: it records no environment, so it cannot be checked
        # against this one, and `apply` rejects it as a baseline.
        return PhaseState(
            phase=label, state=STALE,
            detail=f"{detail} — taken before setup, so it records no environment",
        )
    if (current_fingerprint is not None and slot.env_fingerprint is not None
            and slot.env_fingerprint != current_fingerprint):
        than = f" than {world}" if world else ""
        return PhaseState(
            phase=label, state=STALE,
            detail=f"{detail} — taken against a different manifest/environment{than}",
        )
    return PhaseState(phase=label, state=DONE, detail=detail)


def _baseline_phase(
    slot, current_fingerprint: str | None, receipt, project_path: Path,
    current_setup: str | None = None,
) -> PhaseState:
    """Project the baseline against the world it must describe.

    Before a recorded migration that is the project as it is now. After one it is
    the environment the migration started from: a migration that upgrades the
    manifest moves "now" away from the baseline by design, and re-capturing it
    then would record the migrated world, not the starting point.
    """
    if receipt is None or receipt.status != "applied":
        phase = _capture_phase("baseline", slot, current_fingerprint)
        return _content_only_move(phase, slot, current_setup)
    run = receipt.run_id[:8]
    start = receipt.starting_fingerprint(project_path)
    if start is not None:
        world = f"the one migration run {run} started from"
        phase = _capture_phase("baseline", slot, start, world=world)
        # Pins bumped in the same manifest before the apply are part of the migration.
        return _content_only_move(
            phase, slot, getattr(receipt, "environment_setup_fingerprint", None),
            than=f" than {world}")
    phase = _capture_phase("baseline", slot, None)
    if (
        phase.state == DONE
        and current_fingerprint is not None
        and slot.env_fingerprint is not None
        and slot.env_fingerprint != current_fingerprint
    ):
        if _setup_unchanged(slot, current_setup):
            phase.detail += _CONTENT_ONLY_NOTE
            return phase
        # Not stale on the evidence, but not vouched for either: say so.
        phase.detail += (
            f" — environment changed since; run {run} did not record where it started, "
            "so this cannot be confirmed as its starting point"
        )
    return phase


def _setup_unchanged(slot, current_setup: str | None) -> bool:
    """Same rule as ``service.baseline_setup_unchanged`` (kept import-free here)."""
    recorded = getattr(slot, "env_setup_fingerprint", None)
    return recorded is not None and current_setup is not None and recorded == current_setup


_CONTENT_ONLY_NOTE = (
    " — the manifest's contents changed since (same file, base Python, toolset and "
    "container): taken as the migration's starting point"
)


def _content_only_move(phase: PhaseState, slot, setup: str | None,
                       than: str = "") -> PhaseState:
    """A baseline is not stale when only the contents of the same manifest moved.

    Bumping pins in the selected manifest is the migration itself; re-capturing the
    baseline then would record the migrated world, not the starting point. Only the
    fingerprint mismatch is forgiven — empty or invalid captures stay stale. ``setup`` is the
    setup fingerprint the baseline must match: the project's now, or a migration's start.
    """
    suffix = " — taken against a different manifest/environment" + than
    if (
        phase.state == STALE
        and phase.detail.endswith(suffix)
        and _setup_unchanged(slot, setup)
    ):
        detail = phase.detail.removesuffix(suffix)
        return PhaseState(phase=phase.phase, state=DONE, detail=detail + _CONTENT_ONLY_NOTE)
    return phase


def _migration_phase(receipt, baseline: PhaseState, baseline_slot,
                     project_path: Path | None = None) -> PhaseState:
    """Project the explicit mutation receipt; never infer an apply from captures."""
    if receipt is None:
        if baseline.state == DONE:
            return PhaseState(
                phase="migration", state=TODO,
                detail="baseline is ready — code changes have not been applied",
            )
        return PhaseState(
            phase="migration", state=BLOCKED,
            blocker="a valid baseline is required before applying code changes",
            detail="no migration receipt",
        )

    if getattr(receipt, "status", "applied") == "rolled_back":
        return PhaseState(
            phase="migration",
            state=TODO,
            detail=f"run {receipt.run_id[:8]} was rolled back; no migration is applied",
        )

    detail = (
        f"run {receipt.run_id[:8]} · {len(receipt.files_changed)} file(s) · "
        f"{receipt.patterns_applied} rewrite(s)"
    )
    check = getattr(receipt, "self_check", None)
    detail += f" · self-check: {check.summary()}" if check is not None else \
        " · self-check: not recorded (applied before it existed)"
    if receipt.allow_no_baseline:
        return PhaseState(
            phase="migration", state=DONE,
            detail=f"{detail} — applied without baseline; behavioral proof is unavailable",
        )
    if baseline_slot is None:
        return PhaseState(
            phase="migration", state=STALE,
            detail=f"{detail} — its authorising baseline is missing",
        )
    from pymolt.verify.service import same_slot_path

    if (
        not same_slot_path(project_path or Path("."), receipt.baseline_trace_path,
                           baseline_slot.trace_path)
        or receipt.baseline_captured_at != baseline_slot.captured_at
    ):
        return PhaseState(
            phase="migration", state=STALE,
            detail=f"{detail} — baseline was replaced after this migration",
        )
    return PhaseState(phase="migration", state=DONE, detail=detail)


def _plan_phase(project_path: Path, assess: PhaseState, receipt) -> PhaseState:
    """Project the saved exact plan, including hash/environment staleness."""
    from pymolt.migration_plan import MigrationPlan, inspect_migration_plan

    if receipt is not None and getattr(receipt, "status", "applied") == "applied":
        return PhaseState(
            phase="plan", state=DONE,
            detail=f"consumed by migration run {receipt.run_id[:8]}",
        )
    if assess.state != DONE:
        return PhaseState(
            phase="plan", state=BLOCKED,
            detail="no target manifest to plan for",
            blocker="assessment has not produced a pinned target manifest",
        )
    plan = MigrationPlan.load(project_path)
    if plan is None:
        return PhaseState(
            phase="plan", state=TODO,
            detail="no plan saved",
        )
    inspection = inspect_migration_plan(project_path, plan)
    if not inspection.valid:
        return PhaseState(
            phase="plan", state=STALE,
            detail=f"plan {plan.plan_id[:8]} is stale: {inspection.reasons[0]}",
        )
    # A rolled-back plan whose files are back at their planned originals applies again as is.
    again = (receipt is not None and receipt.status == "rolled_back"
             and (receipt.plan_id or receipt.run_id) == plan.plan_id)
    return PhaseState(
        phase="plan", state=DONE,
        detail=(
            f"plan {plan.plan_id[:8]} · {len(plan.changes)} file(s) · "
            f"{sum(change.sites for change in plan.changes)} rewrite(s)"
            + (f" · rolled back in run {receipt.run_id[:8]}; apply runs it again"
               if again else "")
        ),
    )


def _precedes_start(post_slot, baseline_slot, receipt) -> bool:
    """A post-migration capture taken before the baseline, or before the rollback that took
    the last apply away: it cannot record a migration made since."""
    if baseline_slot is not None and _capture_precedes(post_slot.captured_at,
                                                       baseline_slot.captured_at):
        return True
    after = getattr(receipt, "rolled_back_at", None)
    return bool(after) and _capture_precedes(post_slot.captured_at, after)


def _hand_migration(project_path: Path, baseline_slot, post_slot, plan: PhaseState,
                    current: str | None, current_setup: str | None,
                    receipt) -> PhaseState | None:
    """The migration phase when no apply run is in effect and the baseline is done: DONE when
    the project shows a migration made by hand, else None (the receipt-based phase stands).

    The evidence, in order: a post-migration capture taken since the baseline; then, unless a
    valid saved plan is waiting for `apply`, files that differ from the baseline's source
    snapshot or pins bumped in the same manifest since the baseline."""
    from pymolt.verify.service import sources_changed

    if post_slot is not None and not _precedes_start(post_slot, baseline_slot, receipt):
        return PhaseState(
            phase="migration", state=DONE,
            detail="no apply run: a migration made by hand, which the post-migration "
                   "capture records",
        )
    if plan.state == DONE:
        return None
    changed = sources_changed(project_path, baseline_slot.sources_path) or []
    pins = (current is not None and baseline_slot.env_fingerprint is not None
            and baseline_slot.env_fingerprint != current
            and _setup_unchanged(baseline_slot, current_setup))
    if not (changed or pins):
        return None
    found = []
    if changed:
        found.append(f"{len(changed)} file(s) changed since the baseline")
    if pins:
        found.append("the manifest's pins changed since the baseline")
    return PhaseState(
        phase="migration", state=DONE,
        detail="no apply run: " + " and ".join(found) + ", taken as a migration made by hand",
    )


def _capture_precedes(captured_at: str, applied_at: str) -> bool:
    try:
        captured = datetime.fromisoformat(captured_at)
        applied = datetime.fromisoformat(applied_at)
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=UTC)
        if applied.tzinfo is None:
            applied = applied.replace(tzinfo=UTC)
        return captured < applied
    except ValueError:
        return True


def build_status(project_dir: str | Path, *, check_environments: bool = False) -> FunnelStatus:
    """Read the project's funnel state. Opens files, writes none.

    ``check_environments`` also reports whether the configured environments exist
    (``docker inspect`` for a container). Off by default: the ``next →`` line every
    command prints must stay cheap.
    """
    from pymolt.ingestion.config import EnvConfig
    from pymolt.ingestion.detect import detect_sources
    from pymolt.migration_state import MigrationReceipt
    from pymolt.verify.service import (
        environment_fingerprint,
        environment_setup_fingerprint,
        load_contract_state,
        sources_changed,
        usable_replicate,
    )

    project_path = Path(project_dir)
    status = FunnelStatus(project_dir=str(project_path))

    # ── scan: no persisted artifact; it is always available and always cheap ──
    try:
        sources = detect_sources(project_path)
    except OSError:
        sources = []
    status.phases.append(PhaseState(
        phase="scan",
        state=DONE if sources else TODO,
        detail=(f"{len(sources)} manifest(s): " + ", ".join(s.path.name for s in sources[:4])
                if sources else "no dependency manifest found here"),
    ))

    # ── setup ────────────────────────────────────────────────────────────────
    config = EnvConfig.load(project_path / ".pymolt" / "env_config.json")
    if config is None:
        status.phases.append(PhaseState(
            phase="setup", state=TODO,
            detail="not configured (no .pymolt/env_config.json)",
        ))
    else:
        target = config.target_python or "?"
        status.phases.append(PhaseState(
            phase="setup", state=DONE,
            detail=f"{config.selected_manifest or '?'} · "
                   f"{config.selected_tool.value if config.selected_tool else '?'} · "
                   f"{config.base_python or '?'} → {target}",
        ))

    # ── assess: its artifact is the pinned target manifest ───────────────────
    written = [
        name for name in ("requirements-target.txt", "environment-target.yml")
        if (project_path / name).is_file()
    ]
    if config is None:
        status.phases.append(PhaseState(
            phase="assess", state=BLOCKED, blocker="setup has not chosen a target Python",
            detail="needs a configured target",
        ))
    elif written:
        status.phases.append(PhaseState(
            phase="assess", state=DONE, detail=f"target manifest: {', '.join(written)}",
        ))
    else:
        status.phases.append(PhaseState(
            phase="assess", state=TODO, detail="no target manifest yet",
        ))

    # ── contract: the two captures, then the report they enable ──────────────
    contract = load_contract_state(project_path)
    fingerprint = environment_fingerprint(project_path)
    setup_fingerprint = environment_setup_fingerprint(project_path)
    baseline_slot = contract.baseline
    post_slot = contract.post_migration
    receipt = MigrationReceipt.load(project_path)
    baseline = _baseline_phase(
        baseline_slot, fingerprint, receipt, project_path, current_setup=setup_fingerprint,
    )
    assess_phase = status.phase("assess")
    assert assess_phase is not None
    plan = _plan_phase(project_path, assess_phase, receipt)
    migration = _migration_phase(receipt, baseline, baseline_slot, project_path)
    # No apply run is in effect: plan and apply are optional, so the migration may be one made
    # by hand (or by an agent). It is judged by what is on disk, not by a receipt.
    hand = receipt is None or not receipt.in_effect
    if hand and baseline.state == DONE:
        migration = _hand_migration(
            project_path, baseline_slot, post_slot, plan, fingerprint, setup_fingerprint,
            receipt) or migration
    by_hand = hand and migration.state == DONE
    if by_hand and plan.state == TODO:
        plan = plan.model_copy(update={"detail": plan.detail + " (optional: the migration was "
                                                               "made by hand)"})
    post = _capture_phase("post_migration", post_slot, fingerprint)
    if post_slot is not None and hand and _precedes_start(post_slot, baseline_slot, receipt):
        post = PhaseState(
            phase="post-migration", state=STALE,
            detail=f"{post.detail} — captured before the migration it should record",
        )
    elif post_slot is not None and hand and not by_hand:
        post = PhaseState(
            phase="post-migration", state=STALE,
            detail=f"{post.detail} — no recorded migration precedes this capture",
        )
    elif (
        not hand
        and post_slot is not None
        and receipt is not None
        and _capture_precedes(post_slot.captured_at, receipt.applied_at)
    ):
        post = PhaseState(
            phase="post-migration", state=STALE,
            detail=f"{post.detail} — captured before migration run {receipt.run_id[:8]}",
        )
    status.phases.extend([baseline, plan, migration, post])

    if baseline.state == TODO and post.state == TODO:
        report = PhaseState(
            phase="report", state=BLOCKED, blocker="no capture to build a report from",
            detail="no capture: every call site would be BLIND",
        )
    elif baseline.state == DONE and post.state == DONE:
        report = PhaseState(
            phase="report", state=TODO,
            detail="both captures exist",
        )
    else:
        report = PhaseState(
            phase="report", state=TODO,
            detail="one capture: coverage only, nothing to compare yet",
        )

    from pymolt.verify.unified import UnifiedVerificationReport

    verification = UnifiedVerificationReport.load(project_path)
    # A verdict saved by `verify --post-migration OTHER` compared another recording: it is
    # kept, but it is not the verdict on this migration's post-migration capture. (No record
    # of what was compared: saved before it was recorded, when only the captures were saved.)
    compared = verification.contract.compared if verification is not None else None
    if (
        verification is not None
        and (by_hand or (receipt is not None and receipt.status == "applied"
                         and verification.run_id == receipt.run_id))
        and post_slot is not None
        and not _capture_precedes(verification.created_at, post_slot.captured_at)
        and (compared is None or compared.after_capture == "post-migration")
    ):
        judged = verification.contract
        coverage = ""
        if judged.coverage is not None:
            coverage = (f"; coverage {round(judged.coverage * 100)}%"
                        + (f", recall {round(judged.recall * 100)}%"
                           if judged.recall is not None else "")
                        + f" (granularity {judged.coverage_granularity})")
        report = PhaseState(
            phase="report",
            state=DONE,
            detail=f"verdict: {verification.verdict.value}{coverage}",
        )
    status.phases.append(report)

    if check_environments:
        status.checks = environment_checks(project_path, config)
    workload = detect_workload_command(project_path)
    _decide_next(
        status, config, baseline, plan, migration, post, report,
        receipt, verification, workload, by_hand=by_hand,
        has_replicate=usable_replicate(project_path, contract)[0] is not None,
        can_replicate=lambda: baseline_slot is not None and fingerprint is not None
        and fingerprint == baseline_slot.env_fingerprint
        and sources_changed(project_path, baseline_slot.sources_path) == [],
    )
    return status


_REPLICATE_REASON = "a baseline replicate would tell"  # service._fact_diff_reasons


def _decide_next(
    status: FunnelStatus,
    config,
    baseline: PhaseState,
    plan: PhaseState,
    migration: PhaseState,
    post: PhaseState,
    report: PhaseState,
    receipt,
    verification,
    workload: list[str],
    *,
    by_hand: bool = False,
    has_replicate: bool = False,
    can_replicate=lambda: False,
) -> None:
    """The single next command. One, not a menu: the point is to remove the choice."""
    scan = status.phase("scan")
    assess = status.phase("assess")

    if scan and scan.state == TODO:
        status.next_command = None
        status.next_reason = (
            "No dependency manifest here — point pymolt at the project root, or add one."
        )
        return
    if config is None:
        status.next_command = "pymolt setup ."
        status.next_reason = "Choose the manifest, tool and Python versions."
        return
    if assess and assess.state == TODO:
        target = config.target_python or "<X.Y>"
        status.next_command = f"pymolt assess . --target-python {target}"
        status.next_reason = "Resolve the target dependencies and write the target manifest."
        return
    workload_text = " ".join(workload) if workload else "<command that exercises the app>"
    migrated_without_baseline = receipt is not None and receipt.allow_no_baseline
    if baseline.state == TODO and not migrated_without_baseline:
        status.next_command = (
            f"pymolt capture baseline --mode tests -- {workload_text}"
        )
        status.next_reason = (
            "Record the calls your code makes before any code change. This cannot be "
            "done later."
        )
        return
    if baseline.state == STALE and not migrated_without_baseline:
        status.next_command = (
            f"pymolt capture baseline --mode tests -- {workload_text}"
        )
        status.next_reason = (
            "The baseline does not match the project. Capture it again before comparing."
        )
        return
    if migration.state != DONE and plan.state in {TODO, STALE}:
        status.next_command = "pymolt plan ."
        status.next_reason = (
            "Save the code changes as a plan and review the diff."
        )
        return
    if migration.state in {TODO, STALE}:
        status.next_command = "pymolt apply ."
        status.next_reason = (
            "Apply the saved plan. The original files are kept for rollback."
        )
        return
    if post.state in {TODO, STALE}:
        status.next_command = (
            f"pymolt capture post-migration --mode tests -- {workload_text}"
        )
        status.next_reason = "Record the same calls after the migration, in the target environment."
        return
    if report.state != DONE:
        status.next_command = "pymolt verify ."
        status.next_reason = "Both captures exist. Compare them."
        return
    if verification is not None and verification.verdict.value == "fail":
        if by_hand:
            status.next_command = None
            status.next_reason = (
                "The verdict is FAIL. No apply run made this migration, so there is nothing to "
                "roll back: fix the changes, then capture post-migration again.")
            return
        status.next_command = "pymolt rollback ."
        status.next_reason = "The verdict is FAIL. Rollback restores the files apply changed."
        return
    if (verification is not None and verification.verdict.value == "inconclusive"
            and not has_replicate and baseline.state == DONE
            and any(_REPLICATE_REASON in r for r in verification.reasons)):
        if can_replicate():
            status.next_command = "pymolt capture baseline --replicate"
            status.next_reason = (
                "The verdict is INCONCLUSIVE on differences that may be run-to-run noise. Run "
                "the baseline's command again (same code, same environment) so verify can "
                "measure that noise, then verify again.")
        elif by_hand:
            status.next_command = None
            status.next_reason = (
                "The verdict is INCONCLUSIVE on differences that may be run-to-run noise. A "
                "baseline replicate measures it but must run the baseline's code in its "
                "environment: put that back, `pymolt capture baseline --replicate`, redo the "
                "migration, verify. (`pymolt capture baseline --runs 2` records both before "
                "migrating.)")
        else:
            status.next_command = "pymolt rollback ."
            status.next_reason = (
                "The verdict is INCONCLUSIVE on differences that may be run-to-run noise. A "
                "baseline replicate measures it but must run the baseline's code in its "
                "environment: roll back, `pymolt capture baseline --replicate`, apply again, "
                "verify. (`pymolt capture baseline --runs 2` records both before migrating.)")
        return
    status.next_command = None
    status.next_reason = (
        "Done: the verdict is PASS."
        if verification is not None and verification.verdict.value == "pass"
        else "The verdict is INCONCLUSIVE; its reasons name the missing evidence."
    )
