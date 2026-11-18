"""``pymolt apply``: the plan's pymolt steps, written step by step in one transaction (design 3.2).

HOST side. One invocation applies the steps it selects, in plan order, on the text the steps
before it produced, under the migration lock, all or nothing:

1. **Load.** The plan's run is the run that applies it: one per plan (``plan_id``), opened by its
   first invocation, which closes any other open run (its steps in effect stay in effect). A
   closed run applies nothing more (re-plan). Once a run exists its frozen copy
   (``migrations/<run>/plan.json``) must be the same plan.
2. **Run preconditions.** An interrupted invocation is recovered first (``state.json`` names its
   files with their texts before and after, by hash). The plan must not be run-level stale: the
   setup fingerprint and the target manifest are as at plan time (H3). The baseline must be the
   one the plan is bound to, and describe the world the run started from: on the first
   invocation the project as it is, from the second on the run's stamped starting fingerprint or
   the same setup, so a pin an earlier invocation wrote never makes it stale.
3. **Selection.** ``apply .``: every pymolt step whose dependencies are in effect, in order;
   ``--step ID``: that one, refused when a dependency is not in effect; ``--through ID``: every
   pymolt step up to ID that is not in effect, refused when a dependency on the way is not a
   pymolt step in effect or on the way. A step in effect is a no-op.
4. **Per step, per file:** the rules of ``workorder.rules`` (fast path, reviewed text, re-base,
   present, stale). A stale step refuses the whole invocation, naming its lost anchors.
5. **Compile** every file the invocation changes, on the target and on the base where its steps
   claim to run there. A failure writes nothing.
6. **Write** every file in one transaction.
7. **Ledger and receipt:** one ``apply`` event per step (its ``seq`` is the step's new epoch), its
   entries, the texts in the blob store, and a step receipt with ``checks`` and ``risk_ids``. A
   failure here reverses the writes.
8. **Smoke import** (``--smoke``): on a failure the invocation is rolled back, with a ``rollback``
   event per step, and the step receipts say why.

``--dry-run`` stops after 5 and returns the diff it would write. ``skip_step`` marks a step
``skipped`` (a ``skip`` event; its dependents stay blocked).
"""

from __future__ import annotations

import difflib
import logging
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

from pydantic import BaseModel, Field

from pymolt.apply_check import SelfCheck
from pymolt.migration_plan import (
    Invocation,
    InvocationFile,
    MigrationCheckError,
    MigrationConflictError,
    MigrationEvidenceError,
    MigrationPlan,
    MigrationPlanError,
    MigrationRunState,
    _check_before_write,
    _collect_blobs,
    _migration_lock,
    _now,
    _plan_runs,
    _project_file,
    _pythons,
    _restore_ledger,
    _run_dir,
    _save_run_state,
    _sha256,
    _smoke_after,
    _step_risk_ids,
    _step_sites,
    _validate_run_id,
    inspect_migration_plan,
    recover_invocation,
)
from pymolt.migration_state import MigrationReceipt
from pymolt.workorder.models import PlanStep
from pymolt.workorder.rules import FileDecision, Pythons, decide
from pymolt.workorder.state import (
    CODE_TYPES,
    RunView,
    applicable_steps,
    basic_statuses,
    remaining,
)

logger = logging.getLogger(__name__)

Outcome = Literal["applied", "already_applied", "nothing", "present", "dry_run", "skipped",
                  "in_progress", "recorded", "aborted"]


class StepRefusal(MigrationConflictError):
    """The invocation is refused (exit 2) before any write. ``refused``: step id -> reasons."""

    def __init__(self, message: str, *, refused: dict[str, list[str]] | None = None,
                 hint: str | None = None) -> None:
        super().__init__(message, hint=hint)
        self.refused = dict(refused or {})


class ApplyResult(BaseModel):
    """What one ``apply`` invocation did. ``applied``: steps that got an epoch (``rebased``: of
    those, re-based onto another text; ``unwritten``: already at the reviewed text, so nothing
    was written); ``noop``: already in effect; ``present``: effect already in its slots, no
    epoch; ``blocked``: left out by ``apply .`` (a dependency not in effect), with why;
    ``remaining``: the plan's steps not in effect afterwards, by status. A record
    (``workorder.record``) sets ``in_progress`` (``--record``, with its ``snapshot``),
    ``recorded`` (``--done``, with the changes it left ``unrecorded``, the ``attestation`` id
    and ``check_failed`` when the compile check failed on the recorded files)."""

    outcome: Outcome
    plan_id: str
    run_id: str | None = None
    applied: list[str] = Field(default_factory=list)
    rebased: list[str] = Field(default_factory=list)
    unwritten: list[str] = Field(default_factory=list)
    noop: list[str] = Field(default_factory=list)
    present: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    blocked: dict[str, list[str]] = Field(default_factory=dict)
    checks: dict[str, dict[str, str]] = Field(default_factory=dict)
    remaining: dict[str, list[str]] = Field(default_factory=dict)
    files: list[str] = Field(default_factory=list)
    diff: list[dict[str, str]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    self_check: SelfCheck | None = None
    receipt: MigrationReceipt | None = None
    in_progress: list[str] = Field(default_factory=list)
    recorded: list[str] = Field(default_factory=list)
    unrecorded: list[dict] = Field(default_factory=list)
    snapshot: str | None = None
    attestation: str | None = None
    check_failed: bool = False


AGENT_REFUSAL = ("under --agent, pymolt applies its steps only for a plan a person approved for "
                 "agents, and this pymolt records no approval: a person runs `pymolt apply .`")


# ── entry points ──────────────────────────────────────────────────────────────────────────


def apply_steps(project_dir: str | Path, plan: MigrationPlan, *, step: str | None = None,
                through: str | None = None, dry_run: bool = False,
                allow_no_baseline: bool = False, smoke: bool = False,
                agent: bool = False) -> ApplyResult:
    """Apply the selected pymolt steps of ``plan`` (module docstring). Raises ``StepRefusal`` /
    ``MigrationConflictError`` (exit 2), ``MigrationEvidenceError`` (exit 4),
    ``MigrationCheckError`` (exit 1) or ``MigrationPlanError`` / ``OSError`` (exit 3)."""
    if step and through:
        raise StepRefusal("--step and --through cannot be combined")
    if agent and allow_no_baseline:
        raise StepRefusal("--allow-no-baseline is refused under --agent: a person decides to "
                          "apply without a baseline")
    if agent and not dry_run:
        raise StepRefusal(AGENT_REFUSAL)
    with _migration_lock(project_dir, "apply"):
        return _apply(Path(project_dir).resolve(), plan, step=step, through=through,
                      dry_run=dry_run, allow_no_baseline=allow_no_baseline, smoke=smoke)


def skip_step(project_dir: str | Path, plan: MigrationPlan, step_id: str, reason: str, *,
              agent: bool = False) -> ApplyResult:
    """Mark a step ``skipped`` (design 4.1 row 16): a ``skip`` event, no file written. Allowed
    while it is planned, rolled back or stale, never in effect; its dependents stay blocked."""
    if agent:
        raise StepRefusal("--skip is refused under --agent: a person declines a step")
    if not (reason or "").strip():
        raise StepRefusal("--skip needs --reason TEXT: why the step is declined")
    with _migration_lock(project_dir, "skip"):
        return _skip(Path(project_dir).resolve(), plan, step_id, reason.strip())


# ── the run ───────────────────────────────────────────────────────────────────────────────


def run_for(root: Path, plan: MigrationPlan, receipt: MigrationReceipt | None
            ) -> tuple[str, MigrationRunState | None]:
    """The run that applies ``plan``: its latest run, or a new one under the plan's id.
    Refuses a run a later plan's run closed (design 4.1 row 10)."""
    _validate_run_id(plan.plan_id)
    runs = _plan_runs(root, plan.plan_id)
    state = runs[-1] if runs else None
    run_id = state.run_id if state is not None else plan.plan_id
    record = receipt.run(run_id) if receipt is not None else None
    if record is not None and record.closed:
        raise StepRefusal(
            f"run {run_id[:8]} of plan {plan.plan_id[:8]} is closed: a later plan's run "
            "replaced it, so it applies nothing more",
            hint="Its steps in effect stay in effect. Re-plan (`pymolt plan .`) for what is "
                 "left.")
    return run_id, state


def _check_frozen(root: Path, run_id: str, plan: MigrationPlan) -> None:
    frozen = _run_dir(root, run_id) / "plan.json"
    if not frozen.is_file():
        return
    saved = MigrationPlan.load(root, frozen)
    if saved is None or saved.model_dump() != plan.model_dump():
        raise StepRefusal(
            f"this plan file is not the plan run {run_id[:8]} applies (its frozen copy "
            f"{frozen} differs)",
            hint="Nothing was written. Restore the plan file, or re-plan with `pymolt plan .`.")


def _load_ledger(root: Path, receipt):
    from pymolt.apply_ledger import Ledger, ledger_path, load_ledger

    ledger = load_ledger(root, receipt=receipt)
    if ledger is None and ledger_path(root).exists():
        raise MigrationPlanError(f"the apply ledger cannot be read: {ledger_path(root)}")
    return ledger if ledger is not None else Ledger()


def _preconditions(root: Path, plan: MigrationPlan, receipt, run_id: str, *,
                   allow_no_baseline: bool):
    """Step 2's checks. Returns ``(starting fingerprint, starting setup, baseline slot)``."""
    from pymolt.verify.models import CaptureValidity
    from pymolt.verify.service import (
        baseline_setup_unchanged,
        environment_fingerprint,
        environment_setup_fingerprint,
        inspect_trace_artifact,
        load_contract_state,
        same_slot_path,
    )

    inspection = inspect_migration_plan(root, plan, check_files=False)
    if not inspection.valid:
        raise StepRefusal("stale migration plan: " + "; ".join(inspection.reasons),
                          hint="Every step not in effect is stale. Re-plan with `pymolt plan .`.")
    record = receipt.run(run_id) if receipt is not None else None
    setup_now = environment_setup_fingerprint(root)
    if record is not None and record.starting_fingerprint is not None:
        start, start_setup = record.starting_fingerprint, record.setup_fingerprint
    else:
        start, start_setup = environment_fingerprint(root), setup_now
    baseline = load_contract_state(root).baseline
    if allow_no_baseline:
        return start, start_setup, baseline
    if baseline is None:
        raise MigrationEvidenceError("a valid baseline capture is required before apply")
    quality = inspect_trace_artifact(baseline.trace_path)
    # The world the run started from, or the same setup with only the manifest's pins moved
    # (a pin is part of the migration, whichever invocation wrote it).
    same_world = baseline.env_fingerprint == start or (
        baseline.env_fingerprint is not None and baseline_setup_unchanged(baseline, setup_now))
    if (baseline.validity is not CaptureValidity.VALID
            or quality.validity is not CaptureValidity.VALID or not same_world):
        raise MigrationEvidenceError("the baseline capture is invalid or stale")
    if not plan.baseline_trace_path or not plan.baseline_captured_at:
        raise MigrationEvidenceError("the plan is not bound to its authorizing baseline")
    if (not same_slot_path(root, plan.baseline_trace_path, baseline.trace_path)
            or plan.baseline_captured_at != baseline.captured_at):
        raise MigrationEvidenceError("the baseline capture was replaced after planning")
    import hashlib

    path = Path(baseline.trace_path)
    path = path if path.is_absolute() else root / path
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise MigrationEvidenceError("the baseline trace is unreadable") from exc
    if not plan.baseline_trace_sha256 or digest != plan.baseline_trace_sha256:
        raise MigrationEvidenceError("the baseline trace content changed after planning")
    return start, start_setup, baseline


# ── selection ─────────────────────────────────────────────────────────────────────────────


def _status_word(view: RunView, step_id: str) -> str:
    return view.status(step_id) or "planned"


def select(plan: MigrationPlan, steps: list[PlanStep], view: RunView, *,
           step: str | None, through: str | None
           ) -> tuple[list[PlanStep], list[str], dict[str, list[str]]]:
    """``(steps to apply, no-ops, blocked)`` for one invocation (design 3.2 step 3)."""
    by_id = {s.id: s for s in plan.steps}
    by_id.update({s.id: s for s in steps})
    applicable = {s.id for s in steps}
    order = [s.id for s in plan.steps]
    order += [s.id for s in steps if s.id not in order]

    def kind(sid: str) -> str:
        s = by_id.get(sid)
        return f"{s.executor} step" if s is not None else "unknown step"

    if step is not None:
        target = by_id.get(step)
        if target is None:
            raise StepRefusal(f"unknown step {step}: the plan has "
                              + (", ".join(order) if order else "no step"))
        if step not in applicable and target.executor == "pymolt" \
                and target.type in CODE_TYPES:
            raise StepRefusal(
                f"step {step} cannot be applied on its own: the plan's steps do not rebuild "
                f"its changes, so apply writes them as one whole-file step "
                f"({', '.join(s.id for s in steps)})",
                hint="Apply the plan with `pymolt apply .`, or re-plan with `pymolt plan .`.")
        if step not in applicable:
            raise StepRefusal(
                f"step {step} is a {target.executor} step ({target.type}): pymolt does not "
                "write it",
                hint="An agent or a person makes this change; `pymolt apply --step "
                     f"{step} --brief` says what it is.")
        if view.in_effect(step):
            return [], [step], {}
        missing = [d for d in target.depends_on if not view.in_effect(d)]
        if missing:
            raise StepRefusal(
                f"step {step} depends on " + ", ".join(
                    f"{d} ({_status_word(view, d)}, {kind(d)})" for d in missing)
                + ", not in effect",
                refused={step: [f"needs {d}" for d in missing]},
                hint="Apply the dependencies first, or `pymolt apply --through "
                     f"{step}`.")
        return [target], [], {}
    if through is not None:
        if through not in by_id:
            raise StepRefusal(f"unknown step {through}: the plan has "
                              + (", ".join(order) if order else "no step"))
        limit = order.index(through)
        wanted = [s for s in steps if order.index(s.id) <= limit]
    else:
        wanted = list(steps)
    chosen: list[PlanStep] = []
    noop: list[str] = []
    blocked: dict[str, list[str]] = {}
    coming: set[str] = set()
    for s in wanted:
        if view.in_effect(s.id):
            noop.append(s.id)
            continue
        if view.status(s.id) == "skipped":
            continue                      # a person declined it: only --step applies it again
        missing = [d for d in s.depends_on if not view.in_effect(d) and d not in coming]
        if missing:
            why = [f"needs {d} ({_status_word(view, d)}, {kind(d)})" for d in missing]
            if through is not None:
                raise StepRefusal(
                    f"step {s.id} on the way to {through} depends on "
                    + ", ".join(m[6:] for m in why) + ", not in effect",
                    refused={s.id: why},
                    hint="A dependency an agent or a person does, or one that was skipped, "
                         "must be in effect first.")
            blocked[s.id] = why
            continue
        chosen.append(s)
        coming.add(s.id)
    return chosen, noop, blocked


# ── one invocation ────────────────────────────────────────────────────────────────────────


def _rerun():
    """Re-basing (rule 3), when this pymolt has it."""
    try:
        from pymolt.workorder.rebase import rerun
    except ImportError:
        return None
    return rerun


class _Texts:
    """The files' texts as the invocation sees them: the disk first, then each step's output."""

    def __init__(self, root: Path):
        self.root = root
        self.start: dict[str, str] = {}
        self.now: dict[str, str] = {}

    def get(self, rel: str) -> str:
        if rel not in self.now:
            path = _project_file(self.root, rel)
            if path.is_symlink():
                raise StepRefusal(f"refusing a planned file that is a symlink: {rel}")
            try:
                text = path.read_text(encoding="utf-8")
            except FileNotFoundError as exc:
                raise StepRefusal(f"planned source is missing: {rel}") from exc
            except (OSError, UnicodeDecodeError) as exc:
                raise MigrationPlanError(f"planned source is unreadable: {rel}") from exc
            self.start[rel] = self.now[rel] = text
        return self.now[rel]

    def changed(self) -> list[tuple[str, str, str]]:
        return [(rel, self.start[rel], text) for rel, text in self.now.items()
                if text != self.start[rel]]


def _classify(decisions: list[FileDecision]) -> str:
    if any(d.rule == 5 for d in decisions):
        return "stale"
    present = [d for d in decisions if d.rule == 4]
    if present and len(present) == len(decisions):
        return "present"
    if present:
        return "partly"
    return "apply"


def _apply(root: Path, plan: MigrationPlan, *, step: str | None, through: str | None,
           dry_run: bool, allow_no_baseline: bool, smoke: bool) -> ApplyResult:
    receipt = MigrationReceipt.load(root)
    run_id, state = run_for(root, plan, receipt)
    _check_frozen(root, run_id, plan)
    if state is not None and dry_run and recover_invocation(root, state, plan, check_only=True):
        raise StepRefusal("an interrupted apply or rollback needs recovering first: run "
                          "`pymolt apply .` without --dry-run")
    if state is not None:
        state = recover_invocation(root, state, plan) or state
        receipt = MigrationReceipt.load(root)
    if not dry_run:
        from pymolt.workorder.record import refuse_open_record

        refuse_open_record(receipt, "apply")
    ledger = _load_ledger(root, receipt)
    start_fp, start_setup, baseline = _preconditions(
        root, plan, receipt, run_id, allow_no_baseline=allow_no_baseline)
    view = RunView.of(ledger, run_id)
    steps = applicable_steps(plan)
    chosen, noop, blocked = select(plan, steps, view, step=step, through=through)

    base, target = _pythons(root, plan)
    pythons = Pythons(base=base, target=target)
    rerun = _rerun()
    texts = _Texts(root)
    decided: list[tuple[PlanStep, list[FileDecision]]] = []
    present: list[str] = []
    refused: dict[str, list[str]] = {}
    effective = {sid for sid in view.current}
    for s in chosen:
        late = [d for d in s.depends_on if d not in effective]
        if late:
            # A dependency on the way was refused or turned out present (no epoch).
            why = [f"needs {d}, which this invocation does not put in effect" for d in late]
            if step is None and through is None:
                blocked[s.id] = why
            else:
                refused[s.id] = why
            continue
        decisions = [decide(s, edit, texts.get(edit.file), pythons=pythons, rerun=rerun)
                     for edit in s.edits]
        verdict = _classify(decisions)
        if verdict == "stale":
            refused[s.id] = [r for d in decisions for r in d.reasons]
        elif verdict == "partly":
            refused[s.id] = [f"its effect is already in {d.file} but not in the step's other "
                             "files: the step is partly made by hand"
                             for d in decisions if d.rule == 4]
        elif verdict == "present":
            present.append(s.id)
        else:
            for d in decisions:
                texts.now[d.file] = d.after
            decided.append((s, decisions))
            effective.add(s.id)
    if refused:
        raise StepRefusal(
            "stale migration plan: " + "; ".join(
                f"step {sid}: {'; '.join(reasons)}" for sid, reasons in refused.items()),
            refused=refused,
            hint="Nothing was written. `pymolt apply --step ID --dry-run` shows a step's lost "
                 "statements; re-plan with `pymolt plan .` when the code moved on.")

    result = ApplyResult(outcome="nothing", plan_id=plan.plan_id, run_id=run_id,
                         noop=noop, present=present, blocked=blocked)
    result.notes += _drift_notes(root, ledger, receipt, noop, texts)
    if not decided:
        first_run = receipt is None or receipt.run(run_id) is None
        if not steps and first_run and step is None and through is None and not dry_run:
            # A plan with nothing for pymolt to write: the run is recorded as applied (v1).
            return _commit(root, plan, run_id, state, receipt, ledger, [], texts, result,
                           start_fp=start_fp, start_setup=start_setup, baseline=baseline,
                           allow_no_baseline=allow_no_baseline, smoke=False,
                           check=SelfCheck(compile=[]), smoke_before=None)
        result.outcome = ("present" if present else
                          "already_applied" if noop or (not steps and not first_run) else
                          "nothing")
        result.receipt = _receipt_for_noop(root, plan, run_id, state, receipt)
        _finish(result, root, plan, steps, ledger, run_id)
        return result

    check, smoke_before = _check_before_write(
        root, _pseudo_plan(plan, texts.changed(), decided), smoke and not dry_run)
    result.self_check = check
    result.checks = {s.id: _checks(check, s, decisions) for s, decisions in decided}
    if dry_run:
        result.outcome = "dry_run"
        result.applied = [s.id for s, _d in decided]
        result.rebased = [s.id for s, ds in decided if any(d.rule == 3 for d in ds)]
        result.unwritten = [s.id for s, ds in decided if not any(d.writes for d in ds)]
        result.files = [rel for rel, _a, _b in texts.changed()]
        result.diff = [{"file": rel, "diff": "".join(difflib.unified_diff(
            old.splitlines(keepends=True), new.splitlines(keepends=True),
            fromfile=f"a/{rel}", tofile=f"b/{rel}"))} for rel, old, new in texts.changed()]
        _finish(result, root, plan, steps, ledger, run_id)
        return result
    return _commit(root, plan, run_id, state, receipt, ledger, decided, texts, result,
                   start_fp=start_fp, start_setup=start_setup, baseline=baseline,
                   allow_no_baseline=allow_no_baseline, smoke=smoke, check=check,
                   smoke_before=smoke_before)


def _pseudo_plan(plan: MigrationPlan, changed: list[tuple[str, str, str]],
                 decided: list[tuple[PlanStep, list[FileDecision]]]):
    """What D compiles: each changed Python file, old (the disk) and new (the invocation's
    text), on the base too when every python fix of this invocation in it runs there (v1's
    rule over a file's python fixes; a whole-file step carries its change's flag)."""
    by_path = {c.path: c for c in plan.changes}
    changes = []
    for rel, old, new in changed:
        if not rel.endswith(".py"):
            continue
        flags = []
        for s, ds in decided:
            if not any(d.file == rel for d in ds):
                continue
            if s.do.recipe is not None and s.do.recipe.kind == "whole-file":
                flag = by_path[rel].runs_on_base if rel in by_path else None
                if flag is not None:
                    flags.append(flag)
            elif s.type == "python-fix":
                flags.append(s.runs_on_base)
        on_base = True if flags and all(f is True for f in flags) else (
            False if any(f is False for f in flags) else None)
        changes.append(SimpleNamespace(path=rel, old_source=old, new_source=new,
                                       runs_on_base=on_base))
    return SimpleNamespace(changes=changes, target_python=plan.target_python)


def _checks(check: SelfCheck, step: PlanStep, decisions: list[FileDecision]) -> dict[str, str]:
    from pymolt.migration_state import step_checks

    return step_checks(check, [d.file for d in decisions], runs_on_base=step.runs_on_base)


def _drift_notes(root: Path, ledger, receipt, noop: list[str], texts: _Texts) -> list[str]:
    """A no-op step whose file moved on since the last epoch that wrote it: verify judges the
    changed lines as hand edits (H1 said so by refusing; a step in effect is a no-op)."""
    if not noop or ledger is None:
        return []
    from pymolt.apply_ledger import chain_runs

    chain = chain_runs(receipt) if receipt is not None else set()
    epochs = {seq for (run, _sid), seq in ledger.current_epochs().items() if run in chain}
    last: dict[str, str] = {}
    for event in sorted(ledger.events, key=lambda e: e.seq):
        if event.seq in epochs:
            last.update({f.file: f.after for f in event.files if f.after})
    notes = []
    for rel, digest in sorted(last.items()):
        try:
            texts.get(rel)
            now = _sha256(texts.start[rel])          # the disk, not this invocation's text
        except (MigrationPlanError, OSError, KeyError):
            continue
        if now != digest:
            notes.append(f"{rel} changed since its last step wrote it: verify judges the "
                         "changed lines as hand edits")
    return notes


# ── commit ────────────────────────────────────────────────────────────────────────────────


def _commit(root: Path, plan: MigrationPlan, run_id: str, state: MigrationRunState | None,
            receipt, ledger, decided, texts: _Texts, result: ApplyResult, *, start_fp,
            start_setup, baseline, allow_no_baseline: bool, smoke: bool, check: SelfCheck,
            smoke_before) -> ApplyResult:
    from pymolt.apply_ledger import LedgerSnapshot, append_ledger
    from pymolt.codemods.apply import write_previews
    from pymolt.codemods.models import FilePreview
    from pymolt.workorder.blobs import put_texts

    now = _now()
    run_dir = _run_dir(root, run_id)
    records = [(s, _event(root, plan, run_id, s, decisions), decisions)
               for s, decisions in decided]
    changed = texts.changed()
    invocation = Invocation(
        id=uuid.uuid4().hex, operation="apply", status="prepared", at=now,
        steps=[s.id for s, _d in decided], ledger_next_seq=ledger.next_seq(),
        files=[InvocationFile(file=rel, before=_sha256(old), after=_sha256(new))
               for rel, old, new in changed])
    # The texts first: the events and the invocation name them by hash, and the store keeps
    # them for rollback and recovery.
    put_texts(root, [t for _s, _e, ds in records for d in ds for t in (d.before, d.after)
                     if t is not None] + [t for _r, old, new in changed for t in (old, new)])
    run_dir.mkdir(parents=True, exist_ok=True)
    frozen = run_dir / "plan.json"
    if not frozen.is_file():
        plan.save(root, frozen)
    base_state = state or MigrationRunState(
        run_id=run_id, status="prepared", prepared_at=now,
        plan_id=None if run_id == plan.plan_id else plan.plan_id)
    _save_run_state(run_dir, base_state.model_copy(update={"invocation": invocation}))

    previews = [FilePreview(path=str(_project_file(root, rel)), old_source=old,
                            new_source=new, sites=0) for rel, old, new in changed]
    try:
        write_previews(previews)
    except BaseException as exc:
        _save_run_state(run_dir, base_state.model_copy(update={
            "invocation": invocation.model_copy(update={"status": "failed",
                                                        "error": str(exc)})}))
        raise
    ledger_before = LedgerSnapshot.take(root)
    try:
        saved = append_ledger(root, [(event, entries) for _s, (event, entries), _d in records],
                              ledger=ledger)
        appended = list(zip([s for s, _e, _d in records],
                            saved.events[len(saved.events) - len(records):], strict=True))
        new_receipt = receipt_after_apply(
            root, receipt, plan, run_id, appended, check, now=now, start_fp=start_fp,
            start_setup=start_setup, baseline=baseline, allow_no_baseline=allow_no_baseline,
            frozen=str(frozen), invocation=invocation.id,
            files={s.id: [d.file for d in ds] for s, ds in decided})
        new_receipt.save(root)
        steps_state = {**(base_state.steps or {}), **{s.id: "applied" for s, _e in appended}}
        state = base_state.model_copy(update={
            "status": "applied", "applied_at": now, "steps": steps_state, "error": None,
            "invocation": invocation.model_copy(update={"status": "done"})})
        _save_run_state(run_dir, state)
    except BaseException as metadata_exc:
        _restore_ledger(ledger_before)
        try:
            write_previews([FilePreview(path=p.path, old_source=p.new_source,
                                        new_source=p.old_source, sites=0) for p in previews])
        except BaseException as rollback_exc:
            raise RuntimeError(
                "source apply succeeded, metadata failed, and automatic rollback "
                f"was incomplete: {rollback_exc}") from metadata_exc
        try:
            _save_run_state(run_dir, base_state.model_copy(update={
                "invocation": invocation.model_copy(update={
                    "status": "failed", "error": f"metadata: {metadata_exc}"})}))
        except OSError:
            pass
        raise
    result.outcome = "applied"
    result.applied = [s.id for s, _e in appended]
    result.rebased = [s.id for s, ds in decided if any(d.rule == 3 for d in ds)]
    result.unwritten = [s.id for s, ds in decided if not any(d.writes for d in ds)]
    result.files = [rel for rel, _o, _n in changed]
    result.receipt = new_receipt
    if smoke_before is not None:
        check.smoke = _smoke_after(root, smoke_before)
        result.checks = {s.id: _checks(check, s, ds) for s, ds in decided}
        if check.smoke.status == "failed":
            _undo_after_smoke(root, plan, run_id, state, new_receipt, appended, previews,
                              check, decided)
            failures = "; ".join(f"{name}: {error}" for name, error in check.smoke.failed.items())
            raise MigrationCheckError(
                f"the smoke import failed after the write, so the files were restored: "
                f"{failures}", check=check,
                hint="The plan's rewrite broke an import that worked before it. The files are "
                     "as they were before apply, and the ledger records the rollback; fix the "
                     "rewrite by hand, or apply without --smoke and let capture and verify "
                     "judge it.")
        new_receipt.self_check = check
        for s, ds in decided:
            key = f"{run_id}:{s.id}"
            if key in new_receipt.steps:
                new_receipt.steps[key].checks = result.checks[s.id]
        new_receipt.save(root)
    result.self_check = check
    if not decided:
        result.outcome = "applied"
    _finish(result, root, plan, applicable_steps(plan), saved if decided else ledger, run_id)
    _collect_blobs(root)
    return result


def _event(root: Path, plan: MigrationPlan, run_id: str, step: PlanStep,
           decisions: list[FileDecision]):
    """The step's ``apply`` event and its entries (anchored on the texts this invocation saw
    and wrote; a whole-file step attributes each region to the recipe that names it)."""
    from pymolt.apply_ledger import (
        EventFile,
        LedgerEvent,
        _recipe_for,
        attrs_for_recipe,
        entries_for,
        step_attrs,
    )

    base, target = _pythons(root, plan)
    changes = {c.path: c for c in plan.changes}
    whole = step.do.recipe is not None and step.do.recipe.kind == "whole-file"
    entries = []
    for d in decisions:
        if whole:
            change = changes.get(d.file)
            recipes = list(change.recipes) if change else []
            claims = dict(change.claims) if change else {}

            def attrs(old_text, new_text, recipes=recipes, claims=claims):
                return attrs_for_recipe(_recipe_for(recipes, old_text, new_text), claims)
        else:
            attrs = step_attrs(step)
        # T0 is the base Python's code; every later text the target's.
        t0 = changes[d.file].old_source if d.file in changes else None
        kwargs = {"run_id": run_id, "step_id": step.id,
                  "python_before": base if d.before == t0 else target, "python_after": target}
        if step.type == "manifest-pin":
            continue                      # a manifest is not code: the event names its texts
        try:
            entries += entries_for(d.file, d.before, d.after, attrs, **kwargs)
        except Exception:  # noqa: BLE001 - provenance degrades to lines, the apply stands
            logger.warning("could not anchor the ledger entries of %s", d.file, exc_info=True)
            entries += entries_for(d.file, d.before, d.after, attrs, anchored=False, **kwargs)
    event = LedgerEvent(
        event="apply", run_id=run_id, step_id=step.id, at=_now(), executor="pymolt",
        actor="pymolt", wrote=any(d.writes for d in decisions),
        risk_ids=_step_risk_ids(plan, step),
        files=[EventFile(file=d.file, before=_sha256(d.before), after=_sha256(d.after))
               for d in decisions])
    return event, entries


def receipt_after_apply(root: Path, prior, plan: MigrationPlan, run_id: str, appended, check,
                        *, now: str, start_fp, start_setup, baseline, allow_no_baseline: bool,
                        frozen: str, invocation: str, files: dict[str, list[str]]):
    """The receipt set after an invocation: the run opens (closing any other open run, whose
    steps in effect stay in effect) and each step it applied gets a receipt with its epoch,
    checks and risk links. The schema-1 summary describes this run."""
    from pymolt.migration_state import ReceiptRun, StepReceipt, step_checks

    if prior is not None and prior.run_id == run_id:
        receipt = prior.model_copy(deep=True)
        receipt.applied_at, receipt.self_check = now, check
        receipt.rolled_back_at, receipt.files_restored = None, None
        receipt.allow_no_baseline = receipt.allow_no_baseline or allow_no_baseline
        receipt.plan_path = frozen
    else:
        receipt = MigrationReceipt(
            run_id=run_id, plan_id=plan.plan_id, applied_at=now,
            target_python=plan.target_python, source_manifest=plan.source_manifest,
            target_manifest_path=(str(_project_file(root, plan.target_manifest_path))
                                  if plan.target_manifest_path else None),
            baseline_trace_path=baseline.trace_path if baseline else None,
            baseline_captured_at=baseline.captured_at if baseline else None,
            environment_fingerprint=start_fp, environment_setup_fingerprint=start_setup,
            allow_no_baseline=allow_no_baseline, self_check=check,
            no_codemods_required=not plan.changes, plan_path=frozen,
            runs=[r.model_copy() for r in prior.runs] if prior is not None else [],
            steps=dict(prior.steps) if prior is not None else {})
    receipt.last_invocation = invocation
    for run in receipt.runs:
        if not run.closed and run.run_id != run_id:
            run.closed, run.closed_at = True, now
    if receipt.run(run_id) is None:
        receipt.runs.append(ReceiptRun(
            run_id=run_id, plan_id=plan.plan_id, plan_path=frozen, opened_at=now,
            starting_fingerprint=start_fp, setup_fingerprint=start_setup,
            target_manifest_sha256=plan.target_manifest_sha256))
    for step, event in appended:
        mine = files.get(step.id) or [f.file for f in event.files]
        receipt.steps[f"{run_id}:{step.id}"] = StepReceipt(
            status="applied", epoch=event.seq, executor="pymolt", actor="pymolt",
            claim=step.claim, applied_at=now, files=mine, risk_ids=list(event.risk_ids),
            sites=_step_sites(plan, step, mine),
            checks=step_checks(check, mine, runs_on_base=step.runs_on_base))
    summarize_run(root, receipt, plan)
    return receipt


def summarize_run(root: Path, receipt, plan: MigrationPlan) -> None:
    """The schema-1 summary of the latest run: ``files_changed`` and ``patterns_applied`` over
    its code steps in effect (the v1 values while none is: v1 counted the Python files the
    rewrite changed, not the manifest a pin step rewrites), ``status`` and ``progress``."""
    steps = receipt.steps_of(receipt.run_id)
    env = {s.id for s in plan.steps if s.phase == "env"}
    live = [(sid, st) for sid, st in steps.items() if st.in_effect and sid not in env]
    if live:
        files: list[str] = []
        for _sid, st in live:
            for rel in st.files:
                path = str(_project_file(root, rel))
                if path not in files:
                    files.append(path)
        receipt.files_changed = files
        receipt.patterns_applied = sum(st.sites for _sid, st in live)
    planned = [s.id for s in plan.steps if s.executor == "pymolt"]
    applicable = [s.id for s in applicable_steps(plan)]
    if not set(applicable) <= set(planned):      # the plan's one whole-file step (``v1``)
        planned = applicable + [s.id for s in plan.steps
                                if s.executor == "pymolt" and s.phase == "env"]
    receipt.summarize(planned)


def _receipt_for_noop(root: Path, plan: MigrationPlan, run_id: str, state, receipt):
    """The receipt an invocation that wrote nothing returns: the saved one when it describes
    this run, else one made from the run's frozen plan (``outcome`` set by the caller)."""
    if receipt is not None and receipt.run_id == run_id:
        out = receipt.model_copy(deep=True)
        out.mark_outcome("already_applied")
        return out
    if receipt is None or receipt.run(run_id) is None:
        return None
    out = MigrationReceipt(
        run_id=run_id, plan_id=plan.plan_id,
        applied_at=(state.applied_at or state.prepared_at) if state else _now(),
        target_python=plan.target_python, source_manifest=plan.source_manifest,
        target_manifest_path=(str(_project_file(root, plan.target_manifest_path))
                              if plan.target_manifest_path else None),
        baseline_trace_path=plan.baseline_trace_path,
        baseline_captured_at=plan.baseline_captured_at,
        files_changed=[str(_project_file(root, c.path)) for c in plan.changes],
        patterns_applied=sum(c.sites for c in plan.changes),
        no_codemods_required=not plan.changes,
        plan_path=str(_run_dir(root, run_id) / "plan.json"))
    out.mark_outcome("already_applied")
    return out


def _finish(result: ApplyResult, root: Path, plan: MigrationPlan, steps: list[PlanStep],
            ledger, run_id: str) -> None:
    """``remaining``: the plan's steps not in effect, by projected status (``project_steps``);
    the ledger alone if the projection fails."""
    try:
        from pymolt.workorder.state import project_steps

        projection = project_steps(root, plan, ledger=ledger)
    except Exception:  # noqa: BLE001 - the summary degrades to the ledger's statuses
        logger.warning("could not project the plan's steps", exc_info=True)
        projection = None
    if projection is not None and projection.steps:
        statuses = {s.id: s.status for s in projection.steps}
        statuses.update({sid: "blocked" for sid in result.blocked
                         if statuses.get(sid) == "planned"})
    else:
        view = RunView.of(ledger, run_id)
        extra = {sid: "present" for sid in result.present}
        extra.update({sid: "blocked" for sid in result.blocked})
        statuses = basic_statuses(plan.steps or steps, view, extra)
    result.remaining = remaining(plan.steps or steps, statuses)



def _undo_after_smoke(root: Path, plan: MigrationPlan, run_id: str, state, receipt, appended,
                      previews, check: SelfCheck, decided) -> None:
    """Step 8: the smoke import failed after the write. The files go back, a ``rollback`` event
    (``restored: true``) ends each step's new epoch, and the step receipts say why."""
    from pymolt.apply_ledger import EventFile, LedgerEvent, append_ledger
    from pymolt.codemods.apply import write_previews
    from pymolt.codemods.models import FilePreview
    from pymolt.migration_state import step_checks

    write_previews([FilePreview(path=p.path, old_source=p.new_source, new_source=p.old_source,
                                sites=0) for p in previews])
    now = _now()
    events = []
    for step, event in reversed(appended):
        events.append((LedgerEvent(
            event="rollback", run_id=run_id, step_id=step.id, at=now, executor="pymolt",
            actor="pymolt", restored=True, reason="the smoke import failed after the write",
            files=[EventFile(file=f.file, before=f.after, after=f.before)
                   for f in event.files]), []))
    append_ledger(root, events)
    files = {s.id: [d.file for d in ds] for s, ds in decided}
    for step, _event in appended:
        key = f"{run_id}:{step.id}"
        if key in receipt.steps:
            receipt.steps[key] = receipt.steps[key].model_copy(update={
                "status": "rolled_back", "epoch": None, "rolled_back_at": now,
                "files_restored": files.get(step.id, []),
                "checks": step_checks(check, files.get(step.id), runs_on_base=step.runs_on_base)})
    receipt.self_check = check
    receipt.status, receipt.rolled_back_at = "rolled_back", now
    summarize_run(root, receipt, plan)
    receipt.save(root)
    steps_state = {**(state.steps or {}), **{s.id: "rolled_back" for s, _e in appended}}
    live = any(st.in_effect for st in receipt.steps_of(run_id).values())
    _save_run_state(_run_dir(root, run_id), state.model_copy(update={
        "status": "applied" if live else "rolled_back", "rolled_back_at": now,
        "steps": steps_state,
        "error": "smoke import failed: " + "; ".join(
            f"{m}: {e}" for m, e in (check.smoke.failed if check.smoke else {}).items())}))


# ── skip ──────────────────────────────────────────────────────────────────────────────────


def _skip(root: Path, plan: MigrationPlan, step_id: str, reason: str) -> ApplyResult:
    from pymolt.apply_ledger import LedgerEvent, LedgerSnapshot, append_ledger
    from pymolt.migration_state import StepReceipt

    receipt = MigrationReceipt.load(root)
    run_id, state = run_for(root, plan, receipt)
    _check_frozen(root, run_id, plan)
    if state is not None:
        state = recover_invocation(root, state, plan) or state
        receipt = MigrationReceipt.load(root)
    ledger = _load_ledger(root, receipt)
    step = plan.step(step_id)
    if step is None:
        raise StepRefusal(f"unknown step {step_id}")
    view = RunView.of(ledger, run_id)
    if view.in_effect(step_id):
        raise StepRefusal(f"step {step_id} is in effect ({view.status(step_id)}): roll it "
                          f"back first (`pymolt rollback --step {step_id}`)")
    if view.status(step_id) == "skipped":
        result = ApplyResult(outcome="skipped", plan_id=plan.plan_id, run_id=run_id,
                             skipped=[step_id], notes=[f"step {step_id} was skipped already"])
        _finish(result, root, plan, applicable_steps(plan), ledger, run_id)
        return result
    inspection = inspect_migration_plan(root, plan, check_files=False)
    if not inspection.valid:
        raise StepRefusal("stale migration plan: " + "; ".join(inspection.reasons))
    now = _now()
    before = LedgerSnapshot.take(root)
    try:
        saved = append_ledger(root, [(LedgerEvent(
            event="skip", run_id=run_id, step_id=step_id, at=now, executor=step.executor,
            actor="person", reason=reason), [])], ledger=ledger)
        if receipt is not None:
            receipt = receipt.model_copy(deep=True)
        receipt = _receipt_with_run(root, receipt, plan, run_id, now)
        receipt.steps[f"{run_id}:{step_id}"] = StepReceipt(
            status="skipped", epoch=None, executor=step.executor, actor="person",
            claim=step.claim, files=list(step.files))
        if receipt.run_id == run_id:
            summarize_run(root, receipt, plan)
        receipt.save(root)
        run_dir = _run_dir(root, run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        if not (run_dir / "plan.json").is_file():
            plan.save(root, run_dir / "plan.json")
        base_state = state or MigrationRunState(
            run_id=run_id, status="prepared", prepared_at=now,
            plan_id=None if run_id == plan.plan_id else plan.plan_id)
        _save_run_state(run_dir, base_state.model_copy(update={
            "steps": {**(base_state.steps or {}), step_id: "skipped"}}))
    except BaseException:
        _restore_ledger(before)
        raise
    result = ApplyResult(outcome="skipped", plan_id=plan.plan_id, run_id=run_id,
                         skipped=[step_id], receipt=receipt)
    _finish(result, root, plan, applicable_steps(plan), saved, run_id)
    return result


def _receipt_with_run(root: Path, receipt, plan: MigrationPlan, run_id: str, now: str):
    """The receipt with ``run_id`` in its run list (opened now, closing any other open run)."""
    from pymolt.migration_state import ReceiptRun
    from pymolt.verify.service import environment_fingerprint, environment_setup_fingerprint

    if receipt is None:
        receipt = MigrationReceipt(
            run_id=run_id, plan_id=plan.plan_id, applied_at=now, status="rolled_back",
            target_python=plan.target_python, source_manifest=plan.source_manifest,
            target_manifest_path=(str(_project_file(root, plan.target_manifest_path))
                                  if plan.target_manifest_path else None),
            baseline_trace_path=plan.baseline_trace_path,
            baseline_captured_at=plan.baseline_captured_at,
            no_codemods_required=not plan.changes,
            plan_path=str(_run_dir(root, run_id) / "plan.json"))
    if receipt.run(run_id) is None:
        for run in receipt.runs:
            if not run.closed:
                run.closed, run.closed_at = True, now
        receipt.runs.append(ReceiptRun(
            run_id=run_id, plan_id=plan.plan_id,
            plan_path=str(_run_dir(root, run_id) / "plan.json"), opened_at=now,
            starting_fingerprint=environment_fingerprint(root),
            setup_fingerprint=environment_setup_fingerprint(root),
            target_manifest_sha256=plan.target_manifest_sha256))
    return receipt


__all__ = ["AGENT_REFUSAL", "ApplyResult", "StepRefusal", "apply_steps", "receipt_after_apply",
           "run_for", "select", "skip_step", "summarize_run"]
