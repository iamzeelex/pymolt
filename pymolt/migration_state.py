"""Persisted receipt for the mutating part of a migration: the receipt set.

The contract captures describe the worlds before and after a migration.  This
receipt is the missing link between them: it proves that the mutating command
actually ran, records which baseline authorised it, and gives ``status`` a
durable phase to project instead of guessing from the presence of two traces.

``.pymolt/migration_state.json``, ``schema_version: 2`` (design
``docs/design/apply-work-order.md`` 2.2, 2.4, 2.5):

- the schema-1 fields stay, as a summary of the latest run with their v1 values: ``status`` is
  ``applied`` while a step of the latest run is applied or recorded, ``rolled_back`` otherwise;
  ``in_effect`` holds while one of its steps is in effect (a rollback that restored no file
  leaves it in effect, as in v1);
- ``progress``: ``complete`` (every pymolt step of the latest run in effect or skipped),
  ``partial`` or ``none``;
- ``runs``: every run, oldest first. The **chain** is the runs whose code is on disk: the open
  run, and each closed one with a step still in effect. A new plan applied while a run is open
  closes it; its steps in effect stay in effect;
- ``steps``: ``"<run>:<step>"`` -> the step receipt: its status, current epoch (the ledger
  event that opened it), who did it, its files, checks and the plan-time ``risk_ids`` it
  addresses (the links calibration reads, never recomputed from a later plan).

A schema-1 file is read as ``runs: [that run]`` and ``steps: {"<run>:v1": ...}`` with its
status unchanged; the next save copies it to ``.pymolt/archive/`` first.
"""

from __future__ import annotations

import json
import re
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, PrivateAttr

from pymolt.apply_check import SelfCheck
from pymolt.state_io import atomic_write_json, keep_remedy, load_state, schema_version

MIGRATION_STATE_SCHEMA_VERSION = 2
RECEIPT_KIND = "apply receipt"
RECEIPT_REMEDY = keep_remedy(
    "the rollback information of every apply run; without it pymolt cannot tell which runs "
    "are in effect, nor roll them back")
#: The step a schema-1 run is read as.
V1_STEP = "v1"

StepStatus = Literal["in_progress", "applied", "recorded", "skipped", "rolled_back"]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class ReceiptRun(BaseModel):
    """One run: one plan applied (H2: a plan applied again after a rollback is a run of its
    own). ``starting_fingerprint`` is stamped once, before the run's first write."""

    run_id: str
    plan_id: str | None = None
    plan_path: str | None = None
    opened_at: str
    closed: bool = False
    closed_at: str | None = None
    starting_fingerprint: str | None = None
    setup_fingerprint: str | None = None
    target_manifest_sha256: str | None = None
    baseline_kind: Literal["full", "imports"] | None = "full"


class StepReceipt(BaseModel):
    """What a run did with one of its plan's steps. ``epoch``: the ``seq`` of the ledger event
    that opened its current epoch (None: it has none). ``checks``: apply's self-check on the
    step's files, ``ok`` / ``failed: <reason>`` / ``not checked: <reason>``."""

    status: StepStatus
    epoch: int | None = None
    executor: str | None = None
    actor: str | None = None
    claim: str | None = None
    applied_at: str | None = None
    rolled_back_at: str | None = None
    files: list[str] = Field(default_factory=list)
    files_restored: list[str] | None = None
    risk_ids: list[str] = Field(default_factory=list)
    sites: int = 0
    checks: dict[str, str] = Field(default_factory=dict)

    @property
    def in_effect(self) -> bool:
        """It has a current epoch: applied, recorded (or being re-recorded), or rolled back
        by a rollback that restored no file."""
        if self.epoch is None or self.status == "skipped":
            return False
        if self.status == "rolled_back":
            return self.files_restored == []
        return True


class MigrationReceipt(BaseModel):
    """The receipt set; its schema-1 fields summarise the latest ``pymolt apply`` run."""

    schema_version: int = MIGRATION_STATE_SCHEMA_VERSION
    run_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    # The plan the run applied. A plan applied again after a rollback is a new run with its
    # own id; None in receipts from before this was recorded, when the two ids were equal.
    plan_id: str | None = None
    status: Literal["applied", "rolled_back"] = "applied"
    progress: Literal["complete", "partial", "none"] | None = None
    # Capture timestamps are second-granularity; use the same clock precision so
    # a post capture taken immediately after apply is not misclassified as older.
    applied_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds")
    )
    updated_at: str | None = None
    target_python: str
    source_manifest: str
    target_manifest_path: str | None = None
    baseline_trace_path: str | None = None
    baseline_captured_at: str | None = None
    # The environment fingerprint the migration started from, stamped before any
    # source write. A baseline is judged against this, not against the project
    # as it is now: a migration that upgrades the manifest moves "now" away from
    # the baseline by design.
    environment_fingerprint: str | None = None
    # Its setup part (manifest file, base Python, toolset, container). A baseline with the
    # same setup is the migration's starting point even when the manifest's pins moved
    # between the capture and the apply. None in receipts from before it was recorded.
    environment_setup_fingerprint: str | None = None
    allow_no_baseline: bool = False
    files_changed: list[str] = Field(default_factory=list)
    patterns_applied: int = 0
    no_codemods_required: bool = False
    plan_path: str | None = None
    rolled_back_at: str | None = None
    # The project files the rollback rewrote ([] = it restored no file: the run had changed
    # none). None = not rolled back, or rolled back before this was recorded.
    files_restored: list[str] | None = None
    # What apply checked about its own write: the compile on the target (and the base), and
    # the smoke import when requested. None in receipts from before the self-check existed.
    self_check: SelfCheck | None = None
    # The receipt set (schema 2): every run, oldest first, and each run's steps.
    runs: list[ReceiptRun] = Field(default_factory=list)
    steps: dict[str, StepReceipt] = Field(default_factory=dict)
    # The id of the last apply or rollback invocation this receipt records: an interrupted one
    # whose receipt was saved had committed (``migration_plan.recover_invocation``).
    last_invocation: str | None = None
    # What the apply call that returned this receipt did; not saved. ``already_applied``: the
    # plan was applied before and its files still hold its changes, so nothing was written.
    _outcome: Literal["applied", "already_applied"] = PrivateAttr(default="applied")
    # 1 when the receipt was read from a schema-1 file; not saved.
    _adapted_from: int | None = PrivateAttr(default=None)

    @property
    def outcome(self) -> str:
        return self._outcome

    def mark_outcome(self, outcome: Literal["applied", "already_applied"]) -> None:
        self._outcome = outcome

    @property
    def adapted_from(self) -> int | None:
        return self._adapted_from

    # ── the receipt set ──────────────────────────────────────────────────────────────────

    def run(self, run_id: str) -> ReceiptRun | None:
        return next((r for r in self.runs if r.run_id == run_id), None)

    def steps_of(self, run_id: str) -> dict[str, StepReceipt]:
        """``step id -> receipt`` of one run's steps."""
        prefix = f"{run_id}:"
        return {key[len(prefix):]: step for key, step in self.steps.items()
                if key.startswith(prefix)}

    def run_in_effect(self, run_id: str) -> bool:
        """At least one of the run's steps is in effect. A run with no step (it changed no
        file) follows the schema-1 rule while it is the latest run."""
        steps = self.steps_of(run_id)
        if steps:
            return any(step.in_effect for step in steps.values())
        return run_id == self.run_id and (
            self.status == "applied" or self.files_restored == [])

    @property
    def in_effect(self) -> bool:
        """Whether the code on disk is the code the latest run left: one of its steps is in
        effect (applied or recorded, or rolled back without restoring a file). Verify reads
        such a run's plan; a rollback that restored files took the migration's code away,
        and with it the plan's say over verify."""
        return self.run_in_effect(self.run_id)

    def chain(self) -> list[ReceiptRun]:
        """The runs whose code is on disk, oldest first: the open run, and every closed run
        with a step still in effect. A schema-1 receipt's run is its whole chain."""
        if not self.runs:
            return []
        return [r for r in self.runs if not r.closed or self.run_in_effect(r.run_id)]

    def chain_run_ids(self) -> list[str]:
        if not self.runs:
            return [self.run_id] if self.in_effect else []
        return [r.run_id for r in self.chain()]

    def runs_in_effect(self) -> list[ReceiptRun]:
        """The runs with a step in effect, latest first."""
        return [r for r in reversed(self.runs) if self.run_in_effect(r.run_id)]

    def plan_paths_in_effect(self) -> list[str]:
        """The frozen plans of the runs in effect, latest first."""
        if not self.runs:
            return [self.plan_path] if self.in_effect and self.plan_path else []
        out: list[str] = []
        for run in self.runs_in_effect():
            path = run.plan_path or (self.plan_path if run.run_id == self.run_id else None)
            if path and path not in out:
                out.append(path)
        return out

    def starting_fingerprint(self, project_dir: str | Path) -> str | None:
        """The environment fingerprint this migration started from; None if unrecorded.

        With several runs on disk, it is the oldest run in effect's: a plan stacked on an
        applied one starts from that one's result, but the migration started before it.
        Receipts written before the field existed fall back to the frozen plan:
        apply then refused a plan whose fingerprint differed from the project's at
        apply time, so for such a run the two are the same value.
        """
        for run in reversed(self.runs_in_effect()):          # oldest first
            if run.starting_fingerprint is not None:
                return run.starting_fingerprint
        if self.environment_fingerprint is not None:
            return self.environment_fingerprint
        if not self.plan_path:
            return None
        from pymolt.migration_plan import MigrationPlan

        plan = MigrationPlan.load(project_dir, self.plan_path)
        return plan.environment_fingerprint if plan is not None else None

    def summarize(self, pymolt_steps: list[str] | None = None) -> None:
        """Set the schema-1 summary from the latest run's steps: ``status`` (``applied`` while
        one is applied or recorded), ``progress`` over ``pymolt_steps`` (the plan's pymolt
        step ids; None: the run's own steps) and ``updated_at``."""
        steps = self.steps_of(self.run_id)
        if steps:
            self.status = "applied" if any(
                s.in_effect and s.status != "rolled_back" for s in steps.values()) \
                else "rolled_back"
        wanted = pymolt_steps if pymolt_steps is not None else list(steps)
        done = [sid for sid in wanted if sid in steps
                and (steps[sid].in_effect or steps[sid].status == "skipped")]
        if not wanted:
            self.progress = "complete" if self.in_effect else "none"
        elif len(done) == len(wanted):
            self.progress = "complete"
        else:
            self.progress = "partial" if any(steps[sid].in_effect for sid in done) else "none"
        self.updated_at = _now()

    # ── files ─────────────────────────────────────────────────────────────────────────────

    @classmethod
    def load(cls, project_dir: str | Path) -> MigrationReceipt | None:
        """The receipt set (a schema-1 file read as one); None when there is none. A damaged
        file, or one written by a newer pymolt, raises ``StateFileError``: read as none, apply
        and verify would act on a world without the runs it records."""
        root = Path(project_dir)

        def adapt(data: dict) -> MigrationReceipt:
            receipt = cls.model_validate(data)
            if schema_version(data) < 2:
                receipt._adapt_v1(root)
            return receipt

        return load_state(receipt_path(root), cls, kind=RECEIPT_KIND,
                          newest=MIGRATION_STATE_SCHEMA_VERSION, remedy=RECEIPT_REMEDY,
                          adapt=adapt)

    def _adapt_v1(self, root: Path) -> None:
        """Read a schema-1 receipt as a receipt set: its run, and its step ``v1``."""
        self._adapted_from = 1
        self.schema_version = MIGRATION_STATE_SCHEMA_VERSION
        self.runs = [ReceiptRun(
            run_id=self.run_id, plan_id=self.plan_id or self.run_id, plan_path=self.plan_path,
            opened_at=self.applied_at, starting_fingerprint=self.environment_fingerprint,
            setup_fingerprint=self.environment_setup_fingerprint)]
        if self.files_changed and not self.no_codemods_required:
            rolled = self.status == "rolled_back"
            # A rollback that restored no file kept the run in effect; one from before
            # ``files_restored`` was recorded (None) did not.
            ended = rolled and self.files_restored != []
            self.steps = {f"{self.run_id}:{V1_STEP}": StepReceipt(
                status="rolled_back" if rolled else "applied", epoch=None if ended else 1,
                executor="pymolt", actor="pymolt", applied_at=self.applied_at,
                rolled_back_at=self.rolled_back_at,
                files=[_relative(root, f) for f in self.files_changed],
                files_restored=(None if self.files_restored is None
                                else [_relative(root, f) for f in self.files_restored]),
                sites=self.patterns_applied, checks=step_checks(self.self_check))}
        self.progress = "complete" if self.in_effect else "none"

    def save(self, project_dir: str | Path) -> Path:
        """Atomically persist the receipt after all code writes have succeeded. A schema-1
        file it replaces is copied to ``.pymolt/archive/`` first."""
        path = receipt_path(project_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        _archive_v1(path)
        return atomic_write_json(path, self.model_dump(mode="json"))


def receipt_path(project_dir: str | Path) -> Path:
    return Path(project_dir) / ".pymolt" / "migration_state.json"


def _relative(root: Path, path: str) -> str:
    try:
        return Path(path).resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return path


def _archive_v1(path: Path) -> None:
    """Copy a schema-1 receipt to ``.pymolt/archive/`` before it is rewritten as v2."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(data, dict) or int(data.get("schema_version") or 1) >= 2:
        return
    archive = path.parent / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    stamp = re.sub(r"[^0-9A-Za-z]", "-", _now())
    target = archive / f"migration_state-v1-{stamp}.json"
    n = 2
    while target.exists():
        target = archive / f"migration_state-v1-{stamp}-{n}.json"
        n += 1
    shutil.copy2(path, target)


def step_checks(check: SelfCheck | None, files: list[str] | None = None, *,
                runs_on_base: bool | None = None) -> dict[str, str]:
    """A step's ``checks`` from apply's self-check, over the step's ``files`` (None: all)."""
    if check is None:
        return {"compile_target": "not checked: applied before the self-check existed",
                "compile_base": "not checked: applied before the self-check existed",
                "smoke_import": "not checked: applied before the self-check existed"}
    wanted = None if files is None else set(files)

    def compile_result(side: str) -> str:
        if side == "base" and not runs_on_base:
            return "not checked: no base claim"
        found = [c for c in check.compile if c.side == side]
        if not found:
            return "not checked: no file compiled"
        c = found[0]
        if c.status == "skipped":
            return f"not checked: {c.reason or 'no interpreter'}"
        failed = {f: e for f, e in c.failed.items() if wanted is None or f in wanted}
        if failed:
            return "failed: " + "; ".join(f"{f}: {e}" for f, e in failed.items())
        return "ok"

    smoke = check.smoke
    if smoke is None:
        smoke_result = "not checked: not requested"
    elif smoke.status == "skipped":
        smoke_result = f"not checked: {smoke.reason or 'skipped'}"
    elif smoke.status == "failed":
        smoke_result = "failed: " + "; ".join(f"{m}: {e}" for m, e in smoke.failed.items())
    else:
        smoke_result = "ok"
    return {"compile_target": compile_result("target"), "compile_base": compile_result("base"),
            "smoke_import": smoke_result}


def plan_paths_in_effect(receipt: Any) -> list[str]:
    """The frozen plans whose code is on disk, latest first (a receipt set, or any object with
    the schema-1 fields ``in_effect`` and ``plan_path``)."""
    if receipt is None:
        return []
    method = getattr(receipt, "plan_paths_in_effect", None)
    if callable(method):
        return list(method())
    path = getattr(receipt, "plan_path", None)
    return [path] if getattr(receipt, "in_effect", False) and path else []


def plans_in_effect(project_dir: str | Path, receipt: Any) -> list:
    """The ``MigrationPlan`` of each run in effect, latest first (missing ones left out; a
    damaged one raises ``StateFileError``)."""
    from pymolt.migration_plan import MigrationPlan

    plans = []
    for path in plan_paths_in_effect(receipt):
        plan = MigrationPlan.load(project_dir, path)
        if plan is not None:
            plans.append(plan)
    return plans


__all__ = [
    "MIGRATION_STATE_SCHEMA_VERSION", "MigrationReceipt", "ReceiptRun", "StepReceipt",
    "plan_paths_in_effect", "plans_in_effect", "receipt_path", "step_checks",
]
