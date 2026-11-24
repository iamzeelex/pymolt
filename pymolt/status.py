"""Where am I in the migration, and what is the next command?

The funnel is a sequence with real gates — you cannot diff before two captures
exist — but pymolt deliberately does *not* enforce that order: people join
mid-flow, CI runs each phase in a fresh checkout, and an agent may want one
answer without the ceremony. A gate would say "no" to all three.

So the order is *projected*, not enforced. This module reads what is already on
disk (``.pymolt/env_config.json``, ``.pymolt/contract_state.json``, the target
manifest) and reports the state of each phase plus the single next command —
orientation without a state machine. It decides nothing and writes nothing.

A state file that is damaged, or was written by a newer pymolt (``pymolt.state_io``), does
not make status fail: it is read as missing, and reported as a problem of the phase it
belongs to, with no next command and its remedy as the reason.
"""

from __future__ import annotations

import shlex
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

    phase: str          # scan | setup | assess | baseline | plan | steps | migration |
    #                     post-migration | report
    state: str          # done | todo | stale | blocked
    detail: str = ""    # what was found, or what is missing
    blocker: str = ""   # why this cannot run yet (state == blocked)
    # A state file of this phase is damaged or newer (``StateFileError.as_dict()``); the
    # phase is then blocked on it.
    problem: dict | None = None


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
    # The saved plan's steps, each with its projected status (``workorder.state``):
    # ``{id, status, executor, title}``; empty without a plan of steps.
    steps: list[dict] = Field(default_factory=list)
    # The step the next command (or note) is about, when it is about one.
    next_step: str | None = None
    # The saved verdict's kind when it is INCONCLUSIVE and counts for this migration:
    # ``needs_review`` (a person must look) or ``needs_evidence`` (design decision 7).
    inconclusive_kind: str | None = None
    # Every damaged or newer state file found (``StateFileError.as_dict()`` plus ``phase``),
    # in funnel order; while there is one, there is no next command.
    problems: list[dict] = Field(default_factory=list)

    def phase(self, name: str) -> PhaseState | None:
        return next((p for p in self.phases if p.phase == name), None)


def shown_dir(project_dir: str | Path, cwd: str | Path | None = None) -> str:
    """The project dir as a printed command names it, shell-quoted: as the user gave it, or
    relative to the current directory when it lies inside it (``.`` for that directory)."""
    path = Path(project_dir)
    if path.is_absolute():
        here = Path(cwd) if cwd is not None else Path.cwd()
        for inner, outer in ((path, here), (path.resolve(), here.resolve())):
            try:
                path = inner.relative_to(outer)
                break
            except ValueError:
                continue
    return shlex.quote(str(path))


def command(shown: str, verb: str, *args: str) -> str:
    """``pymolt VERB DIR ARGS``: a command for the project ``shown`` (``shown_dir``) names."""
    return " ".join(("pymolt", verb, shown, *args))


def capture_command(shown: str, when: str, *args: str) -> str:
    """``pymolt capture WHEN [--project-dir DIR] ARGS``: capture takes its project dir as an
    option, left out for the current directory."""
    where = () if shown == "." else ("--project-dir", shown)
    return " ".join(("pymolt", "capture", when, *where, *args))


def plan_next(project_dir: str | Path, plan_file: str | Path | None = None,
              shown: str = ".") -> tuple[str | None, str, str | None]:
    """``(command, reason, step)`` after ``plan`` saved a plan: status's next (QA H4: plan,
    PLAN.md and status name one next command). A plan saved elsewhere (``plan --output``) is
    one status does not read: its next is ``apply --plan`` of that file."""
    from pymolt.migration_plan import plan_path

    if plan_file is not None and plan_path(project_dir, plan_file) != plan_path(project_dir):
        return (command(shown, "apply", "--plan", shlex.quote(str(plan_file))),
                "status follows the default plan file; `apply --plan` applies this one.", None)
    status = build_status(project_dir, shown=shown)
    return status.next_command, status.next_reason, status.next_step


#: Stands for the target Python in a printed command when setup names no target environment.
TARGET_PYTHON = "<target-python>"
_MODE_FLAG = {"test_suite": "tests", "live_command": "command", "live_attach": "attach"}


def _target_interpreter(project_path: Path, config) -> str | None:
    """The target environment's interpreter as setup names it (capture runs commands in the
    project dir, so a relative environment stays relative); None without one that exists."""
    if config is None or not config.target_env_path:
        return None
    env = Path(config.target_env_path)
    for rel in (Path("bin") / "python", Path("Scripts") / "python.exe"):
        if (env if env.is_absolute() else project_path / env).joinpath(rel).is_file():
            return str(env / rel)
    return None


def post_capture_command(project_path: str | Path, config, baseline_slot,
                         shown: str = ".") -> str | None:
    """The post-migration capture verify can compare with the baseline: the baseline's mode,
    target, privacy and sample rate, and its workload (its command after the interpreter, the
    part ``verify.service`` compares), run by the target environment's interpreter, or by
    ``TARGET_PYTHON`` when setup names none. None without a baseline."""
    if baseline_slot is None:
        return None
    from pymolt.verify.models import CaptureMode
    from pymolt.verify.service import default_sample_rate

    mode = CaptureMode(getattr(baseline_slot.mode, "value", baseline_slot.mode))
    args = ["--mode", _MODE_FLAG[mode.value]]
    if baseline_slot.target and baseline_slot.target != "all":
        args += ["--target", shlex.quote(baseline_slot.target)]
    if baseline_slot.privacy_profile != ("values" if mode is CaptureMode.TEST_SUITE
                                         else "shape"):
        args += ["--privacy", shlex.quote(baseline_slot.privacy_profile)]
    if abs(baseline_slot.sample_rate - default_sample_rate(mode)) > 1e-9:
        args += ["--sample-rate", f"{baseline_slot.sample_rate:g}"]
    if mode is CaptureMode.LIVE_ATTACH:
        return capture_command(shown, "post-migration", *args)
    if not baseline_slot.command:
        return None
    head, rest = baseline_slot.command[0], list(baseline_slot.command[1:])
    if Path(head).name.lower().startswith("python"):
        interpreter = _target_interpreter(Path(project_path), config)
        head = shlex.quote(interpreter) if interpreter else TARGET_PYTHON
    else:
        # A tool (pytest, tox): by name, which capture runs from the target environment
        # when setup names one.
        head = shlex.quote(Path(head).name)
    return capture_command(shown, "post-migration", *args, "--",
                           " ".join([head, *map(shlex.quote, rest)]))


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


def environment_checks(project_dir: str | Path, config, shown: str = "."
                       ) -> list[EnvironmentCheck]:
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
            hint=f"Build it, then re-run `{command(shown, 'setup', '--target-env', 'PATH')}`.",
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
        live = [r.run_id for r in receipt.runs_in_effect()] \
            if hasattr(receipt, "runs_in_effect") else []
        if live:
            # An earlier run's steps are still on disk: the migration is that run's.
            return PhaseState(
                phase="migration", state=DONE,
                detail=f"run {live[0][:8]} is in effect; the latest run "
                       f"{receipt.run_id[:8]} was rolled back")
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
    by_step = _run_checks(receipt)
    detail += f" · self-check: {by_step}" if by_step else \
        f" · self-check: {check.summary()}" if check is not None else \
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


_CHECK_LABELS = (("compile_target", "compile on the target"),
                 ("compile_base", "compile on the base"), ("smoke_import", "smoke import"))


def _run_checks(receipt) -> str | None:
    """The run's self-check, by step: each step in effect keeps the checks of the invocation
    that applied or recorded it, while the receipt's ``self_check`` is only the latest
    invocation's (after `apply --step mech-1` then `apply --step env-1`, env-1's "no file
    changed" is not the run's). None when no step in effect records its checks."""
    steps_of = getattr(receipt, "steps_of", None)
    if steps_of is None:
        return None
    steps = {sid: st for sid, st in steps_of(receipt.run_id).items()
             if st.in_effect and st.checks}
    if not steps:
        return None
    # A step that changed no Python file (a manifest pin) has nothing to compile or import: its
    # checks are the invocation's, not its own.
    code = {sid: st for sid, st in steps.items() if any(f.endswith(".py") for f in st.files)}
    parts = []
    for key, label in _CHECK_LABELS:
        by: dict[str, list[str]] = {}
        for sid, st in code.items():
            value = st.checks.get(key) or ""
            if not value or value == "not checked: no base claim":
                continue
            state = ("ok" if value == "ok" else "failed" if value.startswith("failed")
                     else "not checked")
            by.setdefault(state, []).append(sid)
        if by:
            parts.append(f"{label} " + ", ".join(
                f"{state} ({', '.join(by[state])})" for state in ("failed", "ok", "not checked")
                if state in by))
    rest = [sid for sid in steps if sid not in code]
    if rest:
        parts.append(f"no Python file to check ({', '.join(rest)})")
    return "; ".join(parts) or None


def _plan_phase(project_path: Path, assess: PhaseState, receipt) -> PhaseState:
    """Project the saved exact plan, including hash/environment staleness."""
    from pymolt.migration_plan import MigrationPlan, inspect_migration_plan

    earlier = ""
    if receipt is not None and getattr(receipt, "status", "applied") == "applied":
        applied_plan = receipt.plan_id or receipt.run_id
        saved = MigrationPlan.load(project_path)
        if saved is None or saved.plan_id == applied_plan:
            return PhaseState(
                phase="plan", state=DONE,
                detail=f"applied by migration run {receipt.run_id[:8]} (its steps: see steps)",
            )
        # A re-plan after an apply: the saved plan is a new one, projected as such.
        earlier = (f" · an earlier run ({receipt.run_id[:8]}) applied a previous plan "
                   f"({applied_plan[:8]})")
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
    # Run-level only: a planned file that moved is a step's business (re-based, present or
    # stale: the steps phase).
    inspection = inspect_migration_plan(project_path, plan, check_files=False)
    if not inspection.valid:
        return PhaseState(
            phase="plan", state=STALE,
            detail=f"plan {plan.plan_id[:8]} is stale: {inspection.reasons[0]}{earlier}",
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
            + earlier
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


def build_status(project_dir: str | Path, *, check_environments: bool = False,
                 shown: str = ".") -> FunnelStatus:
    """Read the project's funnel state. Opens files, writes none.

    ``check_environments`` also reports whether the configured environments exist
    (``docker inspect`` for a container). Off by default: the ``next →`` line every
    command prints must stay cheap. ``shown``: how the commands it names spell the project
    dir (``shown_dir``); ``.`` by default, the form a file read from the project uses.

    Never raises on a damaged or newer state file: each is read as missing and reported
    (``problems``, and the phase it belongs to), with no next command.
    """
    from pymolt.state_io import StateFileError, ignoring

    found = {exc.path.resolve(): exc for exc in _state_file_problems(Path(project_dir))}
    for _ in range(len(_STATE_FILE_PHASES) + 8):   # a file the scan missed: once more
        try:
            with ignoring(found):
                status = _build_status(project_dir, check_environments=check_environments,
                                       shown=shown)
        except StateFileError as exc:
            if exc.path.resolve() in found:
                raise
            found[exc.path.resolve()] = exc
            continue
        if found:
            _report_problems(status, list(found.values()))
        return status
    raise RuntimeError("status: too many damaged state files")  # pragma: no cover


#: Which phase a state file belongs to, by name (the files of ``.pymolt/migrations/<run>/``
#: are the migration's).
_STATE_FILE_PHASES = {
    "env_config.json": "setup",
    "contract_state.json": "baseline",
    "migration_plan.json": "plan",
    "migration_state.json": "migration",
    "apply_ledger.json": "migration",
    "verification.json": "report",
}


def _problem_phase(path: Path) -> str:
    if path.parent.parent.name == "migrations":
        return "migration"
    return _STATE_FILE_PHASES.get(path.name, "migration")


def _state_file_problems(root: Path) -> list:
    """Every damaged or newer state file of the project. The small ones are read with their
    own loaders; the plans, the ledger and the saved verdict, which can be large (or costly
    to adapt from schema 1), as JSON only: ``build_status`` reads them in full and reports
    what fails there too."""
    from pymolt.apply_ledger import LEDGER_KIND, LEDGER_REMEDY, _ledger_version
    from pymolt.ingestion.config import EnvConfig
    from pymolt.migration_plan import (
        FROZEN_PLAN_KIND,
        FROZEN_PLAN_REMEDY,
        PLAN_KIND,
        PLAN_REMEDY,
        _load_run_state,
    )
    from pymolt.migration_state import MigrationReceipt
    from pymolt.state_io import StateFileError, read_state_json
    from pymolt.verify.models import ContractState
    from pymolt.verify.unified import (
        VERIFICATION_KIND,
        VERIFICATION_REMEDY,
        VERIFICATION_SCHEMA_NEWEST,
    )
    from pymolt.workorder.approval import APPROVAL_FILE, APPROVAL_KIND, APPROVAL_REMEDY
    from pymolt.workorder.models import PLAN_SCHEMA_VERSION

    state = root / ".pymolt"
    problems: list[StateFileError] = []

    def check(read) -> None:
        try:
            read()
        except StateFileError as exc:
            problems.append(exc)

    check(lambda: EnvConfig.load(state / "env_config.json"))
    check(lambda: ContractState.load(state / "contract_state.json"))
    check(lambda: read_state_json(state / "migration_plan.json", kind=PLAN_KIND,
                                  newest=PLAN_SCHEMA_VERSION, remedy=PLAN_REMEDY))
    check(lambda: MigrationReceipt.load(root))
    check(lambda: read_state_json(state / "apply_ledger.json", kind=LEDGER_KIND, newest=2,
                                  remedy=LEDGER_REMEDY, version_of=_ledger_version))
    check(lambda: read_state_json(state / "verification.json", kind=VERIFICATION_KIND,
                                  newest=VERIFICATION_SCHEMA_NEWEST,
                                  remedy=VERIFICATION_REMEDY))
    runs = state / "migrations"
    for run in sorted(runs.iterdir()) if runs.is_dir() else ():
        if not run.is_dir():
            continue
        check(lambda run=run: _load_run_state(run / "state.json"))
        check(lambda run=run: read_state_json(run / "plan.json", kind=FROZEN_PLAN_KIND,
                                              newest=PLAN_SCHEMA_VERSION,
                                              remedy=FROZEN_PLAN_REMEDY))
        check(lambda run=run: read_state_json(run / APPROVAL_FILE, kind=APPROVAL_KIND,
                                              newest=None, remedy=APPROVAL_REMEDY))
    return problems


def _report_problems(status: FunnelStatus, problems: list) -> None:
    """Each damaged or newer state file as a problem of its phase; no next command, and the
    first problem's remedy (in funnel order) as the reason."""
    order = [p.phase for p in status.phases]
    rows = sorted(({**exc.as_dict(), "phase": _problem_phase(exc.path)} for exc in problems),
                  key=lambda row: order.index(row["phase"]) if row["phase"] in order
                  else len(order))
    status.problems = rows
    for row in rows:
        index = next((i for i, p in enumerate(status.phases) if p.phase == row["phase"]),
                     None)
        if index is None:
            status.phases.append(PhaseState(phase=row["phase"], state=BLOCKED,
                                            blocker=row["error"], detail=row["error"],
                                            problem=row))
            continue
        phase = status.phases[index]
        if phase.problem is None:
            status.phases[index] = phase.model_copy(update={
                "state": BLOCKED, "blocker": row["error"], "problem": row})
    status.next_command = None
    status.next_step = None
    status.next_reason = rows[0]["remedy"]


def _build_status(project_dir: str | Path, *, check_environments: bool = False,
                  shown: str = ".") -> FunnelStatus:
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
    projection = _projection(project_path, receipt, post_slot)
    steps = _steps_phase(projection)
    status.steps = [s.as_dict() for s in projection.steps] if projection else []
    migration = _migration_phase(receipt, baseline, baseline_slot, project_path)
    # No apply run is in effect: plan and apply are optional, so the migration may be one made
    # by hand (or by an agent). It is judged by what is on disk, not by a receipt.
    hand = receipt is None or not (receipt.in_effect or receipt.runs_in_effect())
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
    elif post_slot is not None and post.state == DONE:
        # Stale when a file the capture recorded no longer has the recorded text, whatever
        # changed it (a later step, a rollback, a re-record, a hand edit), after `apply` or by
        # hand: `verify` holds the same rule (``service._stamp_source_staleness``).
        from pymolt.workorder.anchor_cache import changed_since

        moved = changed_since(project_path, post_slot.sources_path)
        if (not hand and moved is None
                and _capture_precedes(post_slot.captured_at, receipt.applied_at)):
            post = PhaseState(
                phase="post-migration", state=STALE,
                detail=f"{post.detail} — captured before migration run {receipt.run_id[:8]}",
            )
        elif moved:
            listed = ", ".join(moved[:3]) + (f" and {len(moved) - 3} more" if len(moved) > 3
                                             else "")
            post = PhaseState(
                phase="post-migration", state=STALE,
                detail=f"{post.detail} — {listed} changed since it was captured",
            )
    status.phases.extend([baseline, plan, *([steps] if steps else []), migration, post])

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
            bounded = getattr(judged, "coverage_bounded", False)
            coverage = (f"; coverage {'at most ' if bounded else ''}"
                        f"{round(judged.coverage * 100)}%"
                        + (f", recall {'at least ' if bounded else ''}"
                           f"{round(judged.recall * 100)}%"
                           if judged.recall is not None else "")
                        + f" (granularity {judged.coverage_granularity})")
        kind = verification.inconclusive_kind
        status.inconclusive_kind = kind.value if kind is not None else None
        report = PhaseState(
            phase="report",
            state=DONE,
            detail=f"verdict: {verification.verdict.value}"
                   + (f" ({_KIND_LABEL[kind.value]})" if kind is not None else "") + coverage,
        )
    if report.state == DONE and post.state == STALE:
        # The saved verdict judged a capture that no longer describes the project: it stands
        # as history, not as the verdict on the code on disk.
        report = report.model_copy(update={
            "state": STALE,
            "detail": f"{report.detail} — on a post-migration capture that is stale now: "
                      "capture post-migration again, then verify"})
    status.phases.append(report)

    if check_environments:
        status.checks = environment_checks(project_path, config, shown)
    workload = detect_workload_command(project_path)
    _decide_next(
        status, config, baseline, plan, migration, post, report,
        receipt, verification, workload, by_hand=by_hand, projection=projection, shown=shown,
        post_capture=post_capture_command(project_path, config, baseline_slot, shown),
        has_replicate=usable_replicate(project_path, contract)[0] is not None,
        can_replicate=lambda: baseline_slot is not None and fingerprint is not None
        and fingerprint == baseline_slot.env_fingerprint
        and sources_changed(project_path, baseline_slot.sources_path) == [],
    )
    return status


def _projection(project_path: Path, receipt, post_slot):
    """The saved plan's steps, projected (None: no plan of steps, or the projection failed:
    status then reads as before steps). ``post_newer``: a post-migration capture was taken
    after the plan was made."""
    from pymolt.state_io import StateFileError

    try:
        from pymolt.workorder.state import project_steps

        projection = project_steps(project_path, receipt=receipt)
        projection.post_newer = bool(
            post_slot is not None and projection.created_at
            and not _capture_precedes(post_slot.captured_at, projection.created_at))
    except StateFileError:
        raise                           # build_status reports it as a problem
    except Exception:  # noqa: BLE001 - orientation must never break on a step it cannot read
        import logging

        logging.getLogger(__name__).warning("could not project the plan's steps",
                                            exc_info=True)
        return None
    return projection if projection.steps else None


def _steps_phase(projection) -> PhaseState | None:
    if projection is None:
        return None
    pymolt = [s for s in projection.steps if s.executor == "pymolt"]
    if projection.run_stale or any(s.status == "stale" for s in projection.steps):
        state = STALE
    elif all(s.status not in ("planned", "blocked") for s in pymolt):
        state = DONE
    else:
        state = TODO
    detail = projection.summary()
    if projection.run_stale:
        detail += f" — {projection.run_stale[0]}"
    return PhaseState(phase="steps", state=state, detail=detail)


# A schema-1 verdict carries no causes: its replicate reason is found by its text
# (``service._fact_diff_causes``).
_REPLICATE_REASON = "a baseline replicate would tell"
_KIND_LABEL = {"needs_review": "needs a person", "needs_evidence": "needs evidence"}


def _noise_settles(verification) -> bool:
    """A baseline replicate settles part of the saved INCONCLUSIVE verdict: one of its causes
    is unmeasured noise (rule 11's replicate rule)."""
    if verification is None or verification.verdict.value != "inconclusive":
        return False
    causes = getattr(verification, "inconclusive_causes", None)
    if getattr(verification, "schema_version", 1) >= 2 and causes is not None:
        return any(c.cause == "noise" for c in causes)
    return any(_REPLICATE_REASON in r for r in verification.reasons)


def _replicate_rule(status: FunnelStatus, *, by_hand: bool, can_replicate,
                    step: str | None = None, shown: str = ".") -> None:
    """Measure run-to-run noise with a baseline replicate: it must run the baseline's code in
    its environment, so after a migration it means putting that back first."""
    lead = (f"Step {step} needs evidence: differences at its sites may be run-to-run noise."
            if step else
            "The verdict is INCONCLUSIVE on differences that may be run-to-run noise.")
    status.next_step = step
    replicate = capture_command(shown, "baseline", "--replicate")
    runs = capture_command(shown, "baseline", "--runs 2")
    if can_replicate():
        status.next_command = replicate
        status.next_reason = (
            f"{lead} Run the baseline's command again (same code, same environment) so verify "
            "can measure that noise, then verify again.")
    elif by_hand:
        status.next_command = None
        status.next_reason = (
            f"{lead} A baseline replicate measures it but must run the baseline's code in its "
            f"environment: put that back, `{replicate}`, redo the "
            f"migration, verify. (`{runs}` records both before "
            "migrating.)")
    else:
        status.next_command = command(shown, "rollback")
        status.next_reason = (
            f"{lead} A baseline replicate measures it but must run the baseline's code in its "
            f"environment: roll back, `{replicate}`, apply again, "
            f"verify. (`{runs}` records both before migrating.)")


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
    projection=None,
    shown: str = ".",
    post_capture: str | None = None,
) -> None:
    """The single next command. One, not a menu: the point is to remove the choice. After the
    baseline, the steps decide (design 6) while a plan of steps exists. ``shown``: the project
    dir as the commands name it (``shown_dir``)."""
    scan = status.phase("scan")
    assess = status.phase("assess")

    if scan and scan.state == TODO:
        status.next_command = None
        status.next_reason = (
            "No dependency manifest here — point pymolt at the project root, or add one."
        )
        return
    if config is None:
        status.next_command = command(shown, "setup")
        status.next_reason = "Choose the manifest, tool and Python versions."
        return
    if assess and assess.state == TODO:
        target = config.target_python or "<X.Y>"
        status.next_command = command(shown, "assess", "--target-python", target)
        status.next_reason = "Resolve the target dependencies and write the target manifest."
        return
    workload_text = " ".join(workload) if workload else "<command that exercises the app>"
    migrated_without_baseline = receipt is not None and receipt.allow_no_baseline
    if baseline.state == TODO and not migrated_without_baseline:
        status.next_command = capture_command(
            shown, "baseline", "--mode", "tests", "--", workload_text)
        status.next_reason = (
            "Record the calls your code makes before any code change. This cannot be "
            "done later."
        )
        return
    if baseline.state == STALE and not migrated_without_baseline:
        status.next_command = capture_command(
            shown, "baseline", "--mode", "tests", "--", workload_text)
        status.next_reason = (
            "The baseline does not match the project. Capture it again before comparing."
        )
        return
    if migration.state != DONE and plan.state in {TODO, STALE}:
        status.next_command = command(shown, "plan")
        status.next_reason = (
            "Save the code changes as a plan and review the diff."
        )
        return
    if projection is not None and _decide_steps(status, projection, receipt, post, by_hand,
                                                shown=shown):
        return
    if migration.state in {TODO, STALE}:
        status.next_command = command(shown, "apply")
        status.next_reason = (
            "Apply the saved plan. The original files are kept for rollback."
        )
        return
    if post.state in {TODO, STALE}:
        status.next_command = post_capture or capture_command(
            shown, "post-migration", "--mode", "tests", "--", workload_text)
        status.next_reason = "Record the same calls after the migration, in the target environment."
        if post_capture is not None:
            status.next_reason = (
                "Record the same calls after the migration: the baseline's workload, in the "
                "target environment, so verify compares like with like.")
        if post_capture is not None and TARGET_PYTHON in post_capture:
            status.next_reason += (
                f" Replace {TARGET_PYTHON} with the target environment's interpreter (or name "
                f"the environment with `{command(shown, 'setup', '--target-env', 'PATH')}`).")
        return
    if report.state != DONE:
        status.next_command = command(shown, "verify")
        status.next_reason = "Both captures exist. Compare them."
        return
    def replicate(step: str | None = None) -> bool:
        # The replicate rule applies while no usable replicate exists (verify already applies
        # one) and the baseline it repeats is current.
        if has_replicate or baseline.state != DONE:
            return False
        _replicate_rule(status, by_hand=by_hand, can_replicate=can_replicate, step=step,
                        shown=shown)
        return True

    if projection is not None and _decide_judged(status, projection, verification, replicate,
                                                 shown=shown):
        return
    if verification is not None and verification.verdict.value == "fail":
        # Not a whole-run rollback: it also undoes the steps verify verified. What follows a
        # FAIL is a person's call: fix the code (bring back a rolled-back step whose sites
        # failed), or accept an intended change. A pymolt step in effect that failed was
        # handled above (``_decide_judged``: its own rollback).
        steps = list(getattr(verification, "failing_steps", None) or ())
        current = {s.id: s.status for s in projection.steps} if projection is not None else {}
        named = ", ".join(f"{s['id']} ({current.get(s['id'], s['status'])})" for s in steps)
        back = [s["id"] for s in steps if current.get(s["id"], s["status"]) == "rolled_back"]
        status.next_command = None
        status.next_reason = (
            "The verdict is FAIL" + (f" at the sites of plan step(s) {named}" if named else "")
            + (". No apply run made this migration, so there is nothing to roll back"
               if by_hand else "")
            + ". A person decides: fix the code"
            + (f" (`pymolt apply --step {back[0]}` brings the rolled-back step back)"
               if back else "")
            + ", then capture post-migration again and verify; or accept an intended change "
              "with `pymolt verify . --accept ID` on a terminal.")
        return
    if _noise_settles(verification) and replicate():
        return
    status.next_command = None
    status.next_reason = _verdict_reason(verification)


def _verdict_reason(verification) -> str:
    """The last rule: done on PASS; on INCONCLUSIVE, who settles it (its kind) and how."""
    if verification is not None and verification.verdict.value == "pass":
        return "Done: the verdict is PASS."
    kind = getattr(verification, "inconclusive_kind", None)
    if kind is None:
        return "The verdict is INCONCLUSIVE; its reasons name the missing evidence."
    settle = next((c.settle for c in verification.inconclusive_causes if c.kind == kind), None)
    if kind.value == "needs_review":
        return ("The verdict is INCONCLUSIVE and needs a person: "
                + (settle or "review its findings") + ".")
    return ("The verdict is INCONCLUSIVE and needs evidence: "
            + (settle or "its reasons name what is missing") + ".")


def _decide_steps(status: FunnelStatus, projection, receipt, post: PhaseState,
                  by_hand: bool, *, shown: str = ".") -> bool:
    """Design 6, rules 2 to 6 (rule 1 is the plan rule before it). Rules 4 to 6 apply while a
    receipt exists or no post-migration capture newer than the plan exists: a migration made by
    hand goes straight to the capture and the verdict."""
    pending = [s for s in projection.steps if s.status not in ("applied", "recorded")
               and s.status not in ("rolled_back", "skipped")]
    if projection.run_stale and pending:
        status.next_command = command(shown, "plan")
        status.next_reason = ("The plan is stale (" + projection.run_stale[0] + "): its steps "
                              "not in effect no longer describe the project. Plan again.")
        return True
    busy = projection.first("in_progress")
    if busy is not None:
        status.next_command = command(shown, "apply", "--step", busy.id, "--record", "--done")
        status.next_step = busy.id
        status.next_reason = f"Step {busy.id} is being recorded: finish (or abort) it."
        return True
    if receipt is None and projection.post_newer:
        return False
    ready = [s.id for s in projection.steps if s.executor == "pymolt" and s.status == "planned"]
    stale = projection.first("stale", pymolt=True)
    # Rule 4 before rule 5, except that `apply .` refuses while one of its steps is stale: then
    # the stale step comes first.
    if ready and stale is None:
        status.next_command = command(shown, "apply")
        status.next_reason = ("Apply the plan's pymolt steps " + ", ".join(ready[:6])
                              + (" and more" if len(ready) > 6 else "")
                              + ". The original files are kept for rollback.")
        return True
    if stale is not None:
        status.next_command = command(shown, "apply", "--step", stale.id, "--dry-run")
        status.next_step = stale.id
        why = stale.reasons[0] if stale.reasons else "its statements moved"
        status.next_reason = (f"Step {stale.id} is stale ({why}). The dry run shows the "
                              f"statement it lost; then `{command(shown, 'plan')}` plans what "
                              "is left again.")
        return True
    manual = next((s for s in projection.steps if s.executor != "pymolt"
                   and s.status == "planned" and s.type != "target-env"), None)
    if manual is not None:
        who = "an agent" if manual.executor == "agent" else "a person"
        status.next_command = command(shown, "apply", "--step", manual.id, "--record")
        status.next_step = manual.id
        brief = command(shown, "apply", "--step", manual.id, "--brief")
        skip = command(shown, "apply", "--step", manual.id, "--skip", "--reason", "TEXT")
        status.next_reason = (
            f"Step {manual.id} is for {who}: `{brief}` says "
            "what to do. Start its record, make the change, then `--record --done` (or "
            f"`{skip}` declines it).")
        return True
    return False


def _decide_judged(status: FunnelStatus, projection, verification=None,
                   replicate=lambda step=None: False, *, shown: str = ".") -> bool:
    """Design 6, rules 9 to 11: a step verify judged for its current epoch. Rule 11 says what
    settles the step's evidence: the replicate rule for noise (``replicate``, which reports
    whether it applies), else its judgement's ``settle`` (a test that reaches its sites by
    default)."""
    failed = projection.first("failed")
    if failed is not None:
        status.next_step = failed.id
        if failed.executor == "pymolt":
            status.next_command = command(shown, "rollback", "--step", failed.id)
            status.next_reason = (f"Step {failed.id} failed verify: an outcome at its sites "
                                  "changed. Rolling it back restores the text before it.")
        else:
            status.next_command = command(shown, "apply", "--step", failed.id, "--record")
            status.next_reason = (f"Step {failed.id} failed verify: fix it under a new record "
                                  "(`--record`, the fix, `--record --done`), then capture "
                                  "post-migration again.")
        return True
    review = projection.first("needs_review")
    if review is not None:
        status.next_step = review.id
        status.next_command = command(shown, "verify", "--step", review.id)
        status.next_reason = (f"Step {review.id} needs a person's review of what changed at "
                              "its sites: the report names each finding's id, and a person "
                              "accepts an intended one with "
                              f"`{command(shown, 'verify', '--accept', 'ID')}`.")
        return True
    evidence = projection.first("needs_evidence")
    if evidence is not None:
        steps = getattr(verification, "steps", None) or {}
        judged = steps.get(evidence.id) if isinstance(steps, dict) else None
        judged = judged if isinstance(judged, dict) else {}
        if judged.get("evidence") == "noise" and replicate(evidence.id):
            return True
        status.next_step = evidence.id
        status.next_command = None
        settle = judged.get("settle") or ("add a test that reaches its sites, then capture "
                                          "post-migration again")
        status.next_reason = f"Step {evidence.id} needs evidence: {settle}."
        return True
    return False
