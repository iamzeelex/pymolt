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

    phase: str          # scan | setup | assess | baseline | post-migration | report
    state: str          # done | todo | stale | blocked
    detail: str = ""    # what was found, or what is missing
    blocker: str = ""   # why this cannot run yet (state == blocked)


class FunnelStatus(BaseModel):
    """The whole projection: every phase, plus what to do next."""

    project_dir: str
    phases: list[PhaseState] = Field(default_factory=list)
    next_command: str | None = None
    next_reason: str = ""
    notes: list[str] = Field(default_factory=list)

    def phase(self, name: str) -> PhaseState | None:
        return next((p for p in self.phases if p.phase == name), None)


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


def _capture_phase(name: str, slot, current_fingerprint: str | None) -> PhaseState:
    """Project one capture slot, including whether its evidence still applies."""
    label = "baseline" if name == "baseline" else "post-migration"
    if slot is None:
        return PhaseState(
            phase=label, state=TODO,
            detail="not captured — no dynamic evidence for this side",
        )

    bits = [f"{slot.events} events"]
    if age := _age(slot.captured_at):
        bits.append(age)
    if slot.command:
        bits.append(" ".join(slot.command))
    if slot.coverage_pct is not None:
        bits.append(f"coverage {slot.coverage_pct:.0f}%")
    detail = " · ".join(bits)

    if slot.events == 0:
        # An empty recording is not a capture: it will later diff as "everything
        # disappeared", which reads like a finding rather than a missing input.
        return PhaseState(
            phase=label, state=STALE,
            detail=f"{detail} — EMPTY: the command never touched the traced dependency",
        )
    if (current_fingerprint is not None and slot.env_fingerprint is not None
            and slot.env_fingerprint != current_fingerprint):
        return PhaseState(
            phase=label, state=STALE,
            detail=f"{detail} — taken against a different manifest/environment",
        )
    return PhaseState(phase=label, state=DONE, detail=detail)


def build_status(project_dir: str | Path) -> FunnelStatus:
    """Read the project's funnel state. Pure: opens files, writes none."""
    from pymolt.ingestion.config import EnvConfig
    from pymolt.ingestion.detect import detect_sources
    from pymolt.verify.service import environment_fingerprint, load_contract_state

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
            detail="no .pymolt/env_config.json — manifest and target Python unchosen",
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
            phase="assess", state=TODO, detail="no pinned target manifest yet",
        ))

    # ── contract: the two captures, then the report they enable ──────────────
    contract = load_contract_state(project_path)
    fingerprint = environment_fingerprint(project_path)
    baseline = _capture_phase("baseline", contract.baseline, fingerprint)
    post = _capture_phase("post_migration", contract.post_migration, fingerprint)
    status.phases.extend([baseline, post])

    if baseline.state == TODO and post.state == TODO:
        status.phases.append(PhaseState(
            phase="report", state=BLOCKED, blocker="no capture to build a report from",
            detail="static map only — every contact would be BLIND",
        ))
    elif baseline.state == DONE and post.state == DONE:
        status.phases.append(PhaseState(
            phase="report", state=TODO,
            detail="both sides captured — the before/after diff is available",
        ))
    else:
        status.phases.append(PhaseState(
            phase="report", state=TODO,
            detail="one side captured — coverage report, no version diff yet",
        ))

    _decide_next(status, config, baseline, post)
    return status


def _decide_next(status: FunnelStatus, config, baseline: PhaseState, post: PhaseState) -> None:
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
        status.next_reason = "Choose the manifest, toolset and target Python."
        return
    if assess and assess.state == TODO:
        target = config.target_python or "<X.Y>"
        status.next_command = f"pymolt assess . --target-python {target}"
        status.next_reason = "Resolve baseline vs target and pin the target manifest."
        return
    if baseline.state == TODO:
        status.next_command = "pymolt contract capture --when baseline --mode tests -- <test cmd>"
        status.next_reason = (
            "Record how the code behaves BEFORE migrating — this is the one step that "
            "cannot be done later."
        )
        return
    if baseline.state == STALE:
        status.next_command = "pymolt contract capture --when baseline --mode tests -- <test cmd>"
        status.next_reason = (
            "The baseline no longer describes this project — re-capture before trusting "
            "any diff against it."
        )
        return
    if post.state == TODO:
        status.next_command = (
            "pymolt contract capture --when post-migration --mode tests -- <test cmd>"
        )
        status.next_reason = "Migrate, then record the new behavior to diff against the baseline."
        return
    status.next_command = "pymolt contract report ."
    status.next_reason = "Both sides captured — fold them into the before/after verdict."
