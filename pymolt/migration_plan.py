"""Durable, reviewable migration plans and reversible application.

A plan freezes the exact before/after source that a dry-run produced. Applying
it never recomputes recipes: every source hash is checked and
the reviewed batch is committed atomically. The same frozen source is retained
under ``.pymolt/migrations/<run-id>/`` so rollback remains available after the
temporary transaction files have been removed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator
from pydantic_core import ValidationError

from pymolt.apply_ledger import claim_for
from pymolt.codemods.models import ApiImpact, FilePreview, recipe_key
from pymolt.verify.predict import PredictionReport
from pymolt.workorder.models import PLAN_SCHEMA_ID, PLAN_SCHEMA_VERSION, V1_STEP_ID, PlanStep

logger = logging.getLogger(__name__)

# PLAN_SCHEMA_VERSION 2: the plan is a work order of steps (``pymolt.workorder``); a schema-1
# file is read as one synthetic step (``pymolt.workorder.v1``).
DEFAULT_PLAN_PATH = Path(".pymolt/migration_plan.json")


class MigrationPlanError(RuntimeError):
    """The saved plan cannot be safely applied or rolled back. ``hint``: what to do instead,
    when the error knows better than the interface's default."""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint


class MigrationConflictError(MigrationPlanError):
    """The requested mutation conflicts with current project/run state."""


class MigrationEvidenceError(MigrationPlanError):
    """Required baseline or migration evidence is absent, stale, or changed."""


class MigrationCheckError(MigrationPlanError):
    """apply's self-check found a failure the plan introduced; nothing stays written.
    ``check``: the ``apply_check.SelfCheck`` with every result."""

    def __init__(self, message: str, *, check, hint: str | None = None) -> None:
        super().__init__(message, hint=hint)
        self.check = check


@contextmanager
def _migration_lock(project_dir: str | Path, operation: str):
    """Serialize source mutations; the OS releases this lock after a crash."""
    root = Path(project_dir).resolve()
    lock_path = root / ".pymolt" / "migration.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    stream = lock_path.open("a+", encoding="utf-8")
    release = None
    try:
        try:
            try:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

                def release():
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            except ImportError:  # pragma: no cover - Windows compatibility
                import msvcrt

                stream.seek(0)
                if not stream.read(1):
                    stream.write("\0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)

                def release():
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        except (BlockingIOError, OSError) as exc:
            stream.seek(0)
            owner = stream.read().strip() or "unknown owner"
            raise MigrationConflictError(
                f"another migration operation is active ({owner})"
            ) from exc
        stream.seek(0)
        stream.truncate()
        json.dump({"pid": os.getpid(), "operation": operation, "started_at": _now()}, stream)
        stream.flush()
        os.fsync(stream.fileno())
        try:
            yield
        finally:
            if release is not None:
                stream.seek(0)
                release()
    finally:
        stream.close()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _sha256(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _atomic_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, staged_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged_name, path)
    except BaseException:
        try:
            os.unlink(staged_name)
        except OSError:
            pass
        raise
    return path


def plan_path(project_dir: str | Path, path: str | Path | None = None) -> Path:
    root = Path(project_dir).resolve()
    if path is None:
        return root / DEFAULT_PLAN_PATH
    candidate = Path(path).expanduser()
    return candidate if candidate.is_absolute() else root / candidate


def _project_file(project_dir: str | Path, relative: str) -> Path:
    root = Path(project_dir).resolve()
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise MigrationPlanError(f"plan path escapes the project root: {relative}")
    return candidate


def _validate_run_id(value: str) -> str:
    if len(value) != 32 or any(char not in "0123456789abcdef" for char in value.lower()):
        raise MigrationPlanError(f"invalid migration run id: {value!r}")
    return value


def _relative_project_file(project_dir: Path, path: str | Path) -> str:
    candidate = Path(path).resolve()
    try:
        return candidate.relative_to(project_dir.resolve()).as_posix()
    except ValueError as exc:
        raise MigrationPlanError(
            f"cannot put a file outside the project into a migration plan: {candidate}"
        ) from exc


class PlannedDependency(BaseModel):
    name: str
    from_version: str
    to_version: str


class PlannedFileChange(BaseModel):
    path: str
    old_sha256: str
    new_sha256: str
    old_source: str
    new_source: str
    sites: int = 0
    recipes: list[str] = Field(default_factory=list)
    # recipe key -> the claim it makes about its edit (see ``apply_ledger.claim_for``).
    claims: dict[str, str] = Field(default_factory=dict)
    # Python fixes only: does the rewritten file also run on the base Python? None when the
    # change has no python fix (or the successor's age on the base is unknown).
    runs_on_base: bool | None = None


class PlannedAdvisory(BaseModel):
    path: str
    line: int
    column: int
    severity: str
    reason: str
    # The rule that raised it (its ``recipe_key``); None in plans made before it was recorded.
    rule: str | None = None


class PlannedManualItem(BaseModel):
    """Work the plan leaves to a person: a curated rule that matched a file but is not
    verified, so apply does not write it (``path`` set, ``lines`` where it would rewrite), or
    a rule the provider downgraded that matched no site (``path`` None)."""

    path: str | None = None
    lines: list[int] = Field(default_factory=list)
    sites: int = 0
    rule: str
    reason: str


class PlannedApiImpact(ApiImpact):
    package: str


class PlanInspection(BaseModel):
    reasons: list[str] = Field(default_factory=list)

    @computed_field
    @property
    def valid(self) -> bool:
        return not self.reasons


class MigrationPlan(BaseModel):
    model_config = ConfigDict(json_schema_extra={
        "$schema": "https://json-schema.org/draft/2020-12/schema", "$id": PLAN_SCHEMA_ID})

    schema_version: int = PLAN_SCHEMA_VERSION
    plan_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    created_at: str = Field(default_factory=_now)
    target_python: str
    source_manifest: str
    target_manifest_path: str | None = None
    target_manifest_sha256: str | None = None
    environment_fingerprint: str | None = None
    # The setup part of it (manifest file, base Python, toolset, container), which is what
    # apply checks: bumping pins in the same manifest does not make the plan stale. None in
    # plans made before it was recorded; apply then checks the full fingerprint.
    environment_setup_fingerprint: str | None = None
    baseline_trace_path: str | None = None
    baseline_captured_at: str | None = None
    baseline_trace_sha256: str | None = None
    dependencies: list[PlannedDependency] = Field(default_factory=list)
    impacts: list[PlannedApiImpact] = Field(default_factory=list)
    files_scanned: int = 0
    changes: list[PlannedFileChange] = Field(default_factory=list)
    advisories: list[PlannedAdvisory] = Field(default_factory=list)
    # Rules that are not applied and need a person (see ``PlannedManualItem``).
    manual: list[PlannedManualItem] = Field(default_factory=list)
    # Predicted risks per call site (baseline facts x the migrations' delta facts), each
    # marked fixed by this plan or not; None when there was no baseline to predict from.
    risks: PredictionReport | None = None
    # Baseline types whose public spelling now names another class, with same-object evidence
    # from both interpreters: [{"old", "new", "via"}]. verify reads it when comparing type names.
    type_relocations: list[dict[str, str]] = Field(default_factory=list)
    # The python pseudo-migration: {from, to, quality, sides, ...}; None when the plan does
    # not move Python. Its risks are in ``risks`` (scope "python"); the target interpreter's
    # SyntaxError / SyntaxWarning findings are here.
    python: dict[str, Any] | None = None
    compile_findings: list[dict[str, Any]] = Field(default_factory=list)
    # The work order (``pymolt.workorder.models``): ordered by phase, then by application order.
    # ``changes`` stays the v1 view: every file's text after all of its pymolt code steps.
    steps: list[PlanStep] = Field(default_factory=list)
    # The schema this plan was read from when an adapter made its steps (1: a schema-1 plan,
    # read as one synthetic step ``v1``); None for a plan written as it is.
    adapted_from: int | None = None

    @property
    def setup_fingerprint(self) -> str | None:
        """The setup fingerprint at plan time (``environment_setup_fingerprint``): H3's input."""
        return self.environment_setup_fingerprint

    def step(self, step_id: str) -> PlanStep | None:
        return next((s for s in self.steps if s.id == step_id), None)

    @field_validator("plan_id")
    @classmethod
    def _plan_id_is_safe(cls, value: str) -> str:
        try:
            return _validate_run_id(value)
        except MigrationPlanError as exc:
            raise ValueError(str(exc)) from exc

    @classmethod
    def load(
        cls, project_dir: str | Path, path: str | Path | None = None
    ) -> MigrationPlan | None:
        source = plan_path(project_dir, path)
        if not source.is_file():
            return None
        try:
            with source.open(encoding="utf-8") as stream:
                data = json.load(stream)
            if isinstance(data, dict) and data.get("schema_version", 1) == 1:
                from pymolt.workorder.v1 import read_v1

                return read_v1(cls, data)
            return cls.model_validate(data)
        except (OSError, json.JSONDecodeError, ValidationError):
            return None

    def save(
        self, project_dir: str | Path, path: str | Path | None = None
    ) -> Path:
        return _atomic_json(
            plan_path(project_dir, path), self.model_dump(mode="json")
        )

    def save_planned(self, project_dir: str | Path, path: str | Path | None = None) -> Path:
        """Save the plan ``pymolt plan`` made, then drop the blobs retention no longer keeps
        (``plan`` and ``apply`` collect the store; it waits when another operation holds the
        migration lock)."""
        from pymolt.workorder.blobs import collect_after_plan

        saved = self.save(project_dir, path)
        collect_after_plan(project_dir)
        return saved


class InvocationFile(BaseModel):
    """One file an invocation writes: its text before and after, by sha256 (the texts are in
    the blob store)."""

    file: str
    before: str
    after: str


class Invocation(BaseModel):
    """The run's latest mutating invocation (an ``apply`` or a ``rollback``), saved before its
    first write. ``prepared`` until its receipt is saved (``done``); an invocation left
    ``prepared`` or ``failed`` is recovered by the next one: its files go back to their texts
    before it, and the ledger events it appended (``seq >= ledger_next_seq``) are dropped,
    unless the receipt says it committed (``last_invocation``)."""

    id: str
    operation: Literal["apply", "rollback"]
    status: Literal["prepared", "done", "failed", "recovered"]
    at: str
    steps: list[str] = Field(default_factory=list)
    files: list[InvocationFile] = Field(default_factory=list)
    ledger_next_seq: int = 1
    error: str | None = None


class MigrationRunState(BaseModel):
    run_id: str
    status: Literal["prepared", "applied", "failed", "rolled_back"]
    # The plan this run applies (None: the run's id is the plan's, as for every first run) and
    # which application of it this is: a plan applied again after a rollback is a new run.
    plan_id: str | None = None
    attempt: int = 1
    prepared_at: str
    applied_at: str | None = None
    rolled_back_at: str | None = None
    error: str | None = None
    # step id -> its status in this run (``applied``, ``rolled_back``, ``skipped``), from the
    # run's first mutation since steps exist; None in runs written before.
    steps: dict[str, str] | None = None
    # The latest mutating invocation (None in runs written before invocations were recorded).
    invocation: Invocation | None = None


def create_migration_plan(
    project_dir: str | Path,
    previews: list[FilePreview],
    migrations,
    *,
    target_python: str,
    source_manifest: str,
    target_manifest_path: str | Path | None,
    files_scanned: int,
    impacts: dict[str, list[ApiImpact]] | None = None,
    delta_reports: list | None = None,
    predict: bool = True,
    python_move: tuple[str, str] | None = None,
    python_stage=None,
    downgraded: list[str] | None = None,
    recorder=None,
) -> MigrationPlan:
    """Freeze a locally verified preview into an exact, portable project plan.

    With a baseline capture (and ``predict``), the plan also stores its predicted risks:
    the baseline's facts joined with ``delta_reports`` (the migrations' ``pymolt.delta/1``
    reports; computed offline with ``pymolt.delta.compute_deltas`` when not given).
    ``downgraded``: the provider's summaries of the rules it downgraded, which become manual
    items with the blocked matches of the previews. ``recorder``: the preview walk's recipe
    chains (``workorder.builder.StepRecorder``), from which the automated steps are made; a
    file with no recorded chain keeps its whole change in one step."""
    from pymolt.verify.service import (
        environment_fingerprint,
        environment_setup_fingerprint,
        load_contract_state,
    )

    root = Path(project_dir).resolve()
    contract = load_contract_state(root)
    baseline = contract.baseline
    baseline_hash = None
    baseline_path = None
    if baseline is not None:
        baseline_path = Path(baseline.trace_path)
        if not baseline_path.is_absolute():
            baseline_path = root / baseline_path
        try:
            baseline_hash = hashlib.sha256(baseline_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise MigrationPlanError(
                f"cannot bind the plan to baseline trace: {baseline_path}"
            ) from exc
    target_relative = None
    target_hash = None
    if target_manifest_path:
        target_relative = _relative_project_file(root, target_manifest_path)
        target = _project_file(root, target_relative)
        try:
            target_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        except OSError as exc:
            raise MigrationPlanError(
                f"cannot read target manifest for the plan: {target}"
            ) from exc

    stage = python_stage
    if stage is None and predict and python_move is not None:
        stage = _python_stage(root, python_move, previews,
                              on_step=recorder.record if recorder is not None else None)
    if stage is not None:
        previews = stage.previews

    changes: list[PlannedFileChange] = []
    advisories: list[PlannedAdvisory] = []
    for preview in previews:
        relative = _relative_project_file(root, preview.path)
        # A blocked rule changed nothing, so it is neither a recipe of the change nor a claim.
        applied = preview.applied_recipes()
        if preview.old_source != preview.new_source:
            changes.append(PlannedFileChange(
                path=relative,
                old_sha256=_sha256(preview.old_source),
                new_sha256=_sha256(preview.new_source),
                old_source=preview.old_source,
                new_source=preview.new_source,
                sites=preview.sites,
                recipes=[recipe_key(item) for item in applied],
                claims={recipe_key(item): claim for item in applied
                        if (claim := claim_for(item))},
                runs_on_base=_runs_on_base(preview),
            ))
        for advisory in preview.advisories:
            advisories.append(PlannedAdvisory(
                path=relative,
                line=advisory.line,
                column=advisory.column,
                severity=advisory.severity,
                reason=advisory.reason,
                rule=getattr(advisory, "rule", None),
            ))

    manual = _manual_items(root, previews, downgraded or [])
    from pymolt.workorder.builder import automated_steps

    automated = automated_steps(
        changes, recorder, root=root, dependencies=list(migrations),
        python_move=python_move, fix_checks=getattr(stage, "checks", None),
        target_python=target_python)

    risks = None
    if predict and baseline_path is not None:
        risks = _predict_risks(root, baseline_path, list(migrations), changes, delta_reports)
    type_relocations = list(getattr(risks, "_type_relocations", None) or [])
    python, compile_findings = None, []
    if predict and python_move is not None:
        risks, python, compile_findings = _python_part(root, python_move, risks, stage)

    from pymolt.workorder.manual import T0, link_and_manual_steps
    from pymolt.workorder.verify_effort import finish_steps

    # Every risk gets a step: the pymolt step that fixes it, or a manual one (``step_id``).
    t0 = T0(root, changes, python_move[0] if python_move else None)
    steps = link_and_manual_steps(
        automated, root=root, changes=changes, risks=risks, manual_items=manual,
        advisories=advisories, compile_findings=compile_findings,
        dependencies=list(migrations), python_move=python_move, t0=t0)
    steps = [*_env_steps(root, source_manifest, target_relative, target_python, migrations),
             *steps]
    finish_steps(steps, risks, root=root, t0=t0, baseline_trace=baseline_path,
                 baseline_command=list(baseline.command) if baseline is not None else None)

    return MigrationPlan(
        target_python=target_python,
        source_manifest=source_manifest,
        target_manifest_path=target_relative,
        target_manifest_sha256=target_hash,
        environment_fingerprint=environment_fingerprint(root),
        environment_setup_fingerprint=environment_setup_fingerprint(root),
        baseline_trace_path=baseline.trace_path if baseline else None,
        baseline_captured_at=baseline.captured_at if baseline else None,
        baseline_trace_sha256=baseline_hash,
        dependencies=[
            PlannedDependency(
                name=item.name,
                from_version=item.from_version,
                to_version=item.to_version,
            )
            for item in migrations
        ],
        impacts=[
            PlannedApiImpact(package=package, **impact.model_dump())
            for package, package_impacts in (impacts or {}).items()
            for impact in package_impacts
        ],
        files_scanned=files_scanned,
        changes=changes,
        advisories=advisories,
        manual=manual,
        risks=risks,
        type_relocations=type_relocations,
        python=python,
        compile_findings=compile_findings,
        steps=steps,
    )


def _env_steps(root: Path, source_manifest: str, target_manifest: str | None,
               target_python: str, migrations) -> list:
    """The env phase: the manifest pin and the target environment (``workorder.env``)."""
    from pymolt.ingestion.config import EnvConfig
    from pymolt.workorder.env import env_steps

    config = EnvConfig.load(root / ".pymolt" / "env_config.json")
    return env_steps(root, source_manifest=source_manifest, target_manifest=target_manifest,
                     target_python=target_python if target_python != "unknown" else None,
                     dependencies=list(migrations),
                     target_env=config.target_env_path if config is not None else None)


def _knowledge_id(rule) -> str | None:
    """The curated knowledge entry a rule came from (its ``pymolt knowledge ID (...)``)."""
    for line in getattr(rule, "evidence", None) or ():
        if str(line).startswith("pymolt knowledge "):
            return str(line).split()[2]
    return None


def _manual_items(root: Path, previews: list[FilePreview],
                  downgraded: list[str]) -> list[PlannedManualItem]:
    """The blocked matches of ``previews``, one item per rule and file, then each downgraded
    rule that matched nowhere. A blocked match carries the downgrade's reason when the
    provider gave one (``ID: match → rewrite (reason)``)."""
    reasons: dict[str, str] = {}
    for label in downgraded:
        head, sep, tail = label.partition(": ")
        if sep and tail.endswith(")") and " (" in tail:
            reasons[head] = tail[tail.rindex(" (") + 2:-1]
    items: list[PlannedManualItem] = []
    matched: set[str] = set()
    for preview in previews:
        for block in preview.blocked:
            entry = _knowledge_id(block.rule)
            if entry:
                matched.add(entry)
            why = reasons.get(entry or "") or "it is not verified"
            items.append(PlannedManualItem(
                path=_relative_project_file(root, preview.path), lines=block.lines,
                sites=block.sites, rule=recipe_key(block.rule),
                reason=f"not applied: {why}; rewrite these sites by hand or leave them"))
    for label in downgraded:
        if label.partition(": ")[0] not in matched:
            items.append(PlannedManualItem(
                rule=label, reason="downgraded, so never applied; it matched no site in "
                                   "the shape it rewrites: check other calls by hand"))
    return items


def _runs_on_base(preview) -> bool | None:
    """The python fixes in ``preview`` run on the base only if every one of them does."""
    fixes = [p for p in preview.patterns if getattr(p, "scope", "dependency") == "python"]
    if not fixes:
        return None
    flags = [p.runs_on_base for p in fixes]
    if all(f is True for f in flags):
        return True
    return False if any(f is False for f in flags) else None


def _python_stage(root: Path, move: tuple[str, str], previews: list[FilePreview],
                  on_step=None):
    """The python move computed for ``previews``; None (with no raise) when it fails."""
    try:
        from pymolt.pyversion.plan import plan_python_stage

        return plan_python_stage(root, move[0], move[1], previews=previews, on_step=on_step)
    except Exception:  # noqa: BLE001 - _python_part reports the failure
        return None


def _python_part(root: Path, move: tuple[str, str], risks: PredictionReport | None,
                 stage=None):
    """The python move's risks merged into ``risks`` (also with no baseline), its summary and
    its compile findings. A failure becomes a note: the plan stands without it."""
    from pymolt.verify.predict import merge_python_risks

    base_python, target_python = move
    if stage is None:
        try:
            from pymolt.pyversion.plan import plan_python_stage

            stage = plan_python_stage(root, base_python, target_python)
        except Exception as exc:  # noqa: BLE001
            note = f"python risk prediction failed: {type(exc).__name__}: {exc}"
            if risks is None:
                risks = PredictionReport(status="partial")
            risks.notes.append(note)
            return risks, {"from": base_python, "to": target_python, "quality": "inconclusive",
                           "sides": {}, "notes": [note]}, []
    return merge_python_risks(risks, stage.risks), stage.python, stage.findings


def _predict_risks(root: Path, baseline_path: Path, migrations: list,
                   changes: list[PlannedFileChange],
                   delta_reports: list | None) -> PredictionReport:
    """Never lets a prediction failure cost the plan: it becomes an "unavailable" report."""
    from pymolt.verify.predict import predict_plan_risks

    try:
        return predict_plan_risks(root, baseline_path, migrations, changes,
                                  delta_reports=delta_reports)
    except Exception as exc:  # noqa: BLE001 - the plan stands without its predictions
        return PredictionReport(
            status="unavailable",
            notes=[f"risk prediction failed: {type(exc).__name__}: {exc}"])


def inspect_migration_plan(
    project_dir: str | Path,
    plan: MigrationPlan,
    *,
    expected: Literal["old", "new"] = "old",
    check_environment: bool = True,
    check_files: bool = True,
) -> PlanInspection:
    """Check that a plan still describes the current files and environment.

    The environment is the setup (manifest file, base Python, toolset, container) and the
    target manifest's contents. The source manifest's contents may move: bumping its pins is
    the migration (the rule verify applies to a baseline, ``environment_setup_fingerprint``).
    A plan that predates the setup fingerprint is checked against the full fingerprint.
    ``check_files=False`` checks the plan's own texts against their hashes but not the disk:
    apply decides each step's files by its own rules (``workorder.rules``)."""
    from pymolt.verify.service import environment_fingerprint, environment_setup_fingerprint

    root = Path(project_dir).resolve()
    reasons: list[str] = []
    if plan.schema_version != PLAN_SCHEMA_VERSION:
        reasons.append(
            f"unsupported plan schema {plan.schema_version}; expected {PLAN_SCHEMA_VERSION}"
        )
    if check_environment:
        if plan.environment_setup_fingerprint is not None:
            if plan.environment_setup_fingerprint != environment_setup_fingerprint(root):
                reasons.append("configured environment changed after planning (manifest "
                               "file, base Python, toolset or container)")
        elif plan.environment_fingerprint != environment_fingerprint(root):
            reasons.append("configured environment or source manifest changed after planning")

    if plan.target_manifest_path:
        try:
            target = _project_file(root, plan.target_manifest_path)
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            if not plan.target_manifest_sha256:
                reasons.append(
                    f"target manifest hash is missing: {plan.target_manifest_path}"
                )
            elif digest != plan.target_manifest_sha256:
                reasons.append(f"target manifest changed: {plan.target_manifest_path}")
        except (OSError, MigrationPlanError):
            reasons.append(f"target manifest is missing or unreadable: {plan.target_manifest_path}")

    for change in plan.changes:
        if _sha256(change.old_source) != change.old_sha256:
            reasons.append(f"plan's original-source hash is invalid: {change.path}")
            continue
        if _sha256(change.new_source) != change.new_sha256:
            reasons.append(f"plan's replacement-source hash is invalid: {change.path}")
            continue
        if not check_files:
            continue
        try:
            path = _project_file(root, change.path)
            if path.is_symlink():
                reasons.append(f"refusing planned symlink: {change.path}")
                continue
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError, MigrationPlanError):
            reasons.append(f"planned source is missing or unreadable: {change.path}")
            continue
        expected_hash = change.old_sha256 if expected == "old" else change.new_sha256
        if _sha256(source) != expected_hash:
            state = "planning" if expected == "old" else "application"
            reasons.append(f"source changed after {state}: {change.path}")
    return PlanInspection(reasons=list(dict.fromkeys(reasons)))


def _run_dir(project_dir: str | Path, run_id: str) -> Path:
    return (
        Path(project_dir).resolve()
        / ".pymolt"
        / "migrations"
        / _validate_run_id(run_id)
    )


def _load_run_state(path: Path) -> MigrationRunState | None:
    try:
        with path.open(encoding="utf-8") as stream:
            return MigrationRunState.model_validate(json.load(stream))
    except (OSError, json.JSONDecodeError, ValidationError):
        return None


def _save_run_state(run_dir: Path, state: MigrationRunState) -> Path:
    return _atomic_json(run_dir / "state.json", state.model_dump(mode="json"))


def _plan_runs(root: Path, plan_id: str) -> list[MigrationRunState]:
    """Every run of ``plan_id``, oldest first. The first run is kept under the plan's own id;
    each application after a rollback is a run of its own."""
    base = root / ".pymolt" / "migrations"
    runs: list[MigrationRunState] = []
    for child in sorted(base.iterdir()) if base.is_dir() else ():
        state = _load_run_state(child / "state.json")
        if state is not None and state.run_id == child.name \
                and (state.plan_id or state.run_id) == plan_id:
            runs.append(state)
    return sorted(runs, key=lambda item: (item.attempt, item.prepared_at))


def needs_recovery(state: MigrationRunState | None) -> bool:
    """The run's last invocation was interrupted (or a run from before invocations were
    recorded was left prepared or failed: it has no step statuses, which a run gets once one
    of its mutations committed, and no invocation)."""
    if state is None:
        return False
    if state.invocation is not None:
        return state.invocation.status in ("prepared", "failed")
    return state.status in ("prepared", "failed") and not state.steps


def recover_invocation(root: Path, state: MigrationRunState, plan: MigrationPlan, *,
                       check_only: bool = False):
    """Recover the run's interrupted invocation (``Invocation``): its files back at their
    texts before it, the ledger events it appended dropped, unless its receipt was saved (it
    committed). Returns the run state after it (None: nothing to recover); ``check_only``:
    whether there is something to recover. Refuses (``MigrationConflictError``) when a file
    was edited since the interruption."""
    if not needs_recovery(state):
        return False if check_only else None
    if check_only:
        return True
    run_dir = _run_dir(root, state.run_id)
    invocation = state.invocation
    if invocation is None:
        frozen = MigrationPlan.load(root, run_dir / "plan.json")
        if frozen is None or frozen.model_dump() != plan.model_dump():
            raise MigrationConflictError(
                f"interrupted run {state.run_id[:8]} does not match this plan"
            )
        _recover_interrupted_apply(root, frozen)
        return state
    from pymolt.migration_state import MigrationReceipt

    receipt = MigrationReceipt.load(root)
    if receipt is not None and receipt.last_invocation == invocation.id:
        recovered = state.model_copy(update={
            "invocation": invocation.model_copy(update={"status": "done"})})
        _save_run_state(run_dir, recovered)
        return recovered
    from pymolt.codemods.apply import write_previews
    from pymolt.workorder.blobs import get_text

    reverse: list[FilePreview] = []
    for item in invocation.files:
        path = _project_file(root, item.file)
        try:
            current = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise MigrationConflictError(
                f"cannot recover the interrupted {invocation.operation}: unreadable "
                f"{item.file}") from exc
        digest = _sha256(current)
        if digest == item.before:
            continue
        before = get_text(root, item.before)
        if digest != item.after or before is None:
            raise MigrationConflictError(
                f"cannot recover the interrupted {invocation.operation} of run "
                f"{state.run_id[:8]}: {item.file} was edited since",
                hint="Put the file back as the interrupted command left it, or restore it by "
                     "hand and remove the run's `invocation` from its state.json.")
        reverse.append(FilePreview(path=str(path), old_source=current, new_source=before,
                                   sites=0))
    write_previews(reverse)
    _drop_ledger_after(root, invocation.ledger_next_seq)
    recovered = state.model_copy(update={
        "invocation": invocation.model_copy(update={"status": "recovered"})})
    _save_run_state(run_dir, recovered)
    return recovered


def _drop_ledger_after(root: Path, next_seq: int) -> None:
    """Drop the ledger events (and their entries) an uncommitted invocation appended."""
    from pymolt.apply_ledger import load_ledger, save_ledger

    ledger = load_ledger(root)
    if ledger is None or not any(e.seq >= next_seq for e in ledger.events):
        return
    ledger.events = [e for e in ledger.events if e.seq < next_seq]
    ledger.entries = [e for e in ledger.entries if e.epoch < next_seq]
    save_ledger(root, ledger)


def _recover_interrupted_apply(root: Path, plan: MigrationPlan) -> None:
    """Restore a prepared/failed run from before invocations were recorded to its exact
    pre-apply source boundary."""
    from pymolt.codemods.apply import write_previews

    reverse: list[FilePreview] = []
    for change in plan.changes:
        path = _project_file(root, change.path)
        try:
            current = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise MigrationConflictError(
                f"cannot recover interrupted migration; unreadable source: {change.path}"
            ) from exc
        if current == change.old_source:
            continue
        if current != change.new_source:
            raise MigrationConflictError(
                "cannot recover interrupted migration because source was edited: "
                + change.path
            )
        reverse.append(FilePreview(
            path=str(path),
            old_source=current,
            new_source=change.old_source,
            sites=change.sites,
        ))
    write_previews(reverse)


def _collect_blobs(root: Path) -> None:
    """Drop the blobs retention no longer keeps (we hold the lock). Never fails the command."""
    from pymolt.workorder.blobs import collect_blobs

    try:
        collect_blobs(root)
    except Exception:  # noqa: BLE001 - housekeeping
        logger.warning("could not collect the blob store", exc_info=True)


def _check_before_write(root: Path, plan: MigrationPlan, smoke: bool):
    """The compile check (refusing a file the plan breaks) and, with ``smoke``, the import
    of the touched modules on the old code. Returns ``(SelfCheck, smoke state)``; the smoke
    state is None when no smoke import follows the write (``check.smoke`` then says why)."""
    from pymolt.apply_check import (
        SelfCheck,
        SmokeCheck,
        compile_changes,
        smoke_modules,
        smoke_target,
    )

    check = SelfCheck(compile=compile_changes(root, plan))
    broken = [(c.side, path, error) for c in check.compile for path, error in c.failed.items()]
    if broken:
        raise MigrationCheckError(
            "the plan's new source does not compile: " + "; ".join(
                f"{path} on the {side} ({error})" for side, path, error in broken),
            check=check,
            hint="Nothing was written. The rewrite of these files is invalid Python where "
                 "the original compiled; make the change by hand or re-plan after fixing it.")
    if not smoke:
        return check, None
    exe, why = smoke_target(root)
    modules, left_out = smoke_modules(root, plan)
    if exe is None or not modules:
        check.smoke = SmokeCheck(status="skipped", not_imported=left_out,
                                 reason=why or "no changed file is an importable module")
        return check, None
    from pymolt.apply_check import import_modules

    before, python, before_why = import_modules(exe, root, modules)
    return check, (exe, modules, left_out, before, python, before_why)


def _smoke_after(root: Path, state):
    from pymolt.apply_check import import_modules, smoke_verdict

    exe, modules, left_out, before, python, before_why = state
    if before is None:
        after, after_why = None, "not run"
    else:
        after, python_after, after_why = import_modules(exe, root, modules)
        python = python or python_after
    return smoke_verdict(modules, before, after, exe=exe, python=python,
                         before_why=before_why, after_why=after_why, left_out=left_out)


def _restore_ledger(snapshot) -> None:
    """Put the ledger back as it was before a mutation whose metadata failed (best effort: the
    mutation's own error is the one to report)."""
    try:
        snapshot.restore()
    except OSError:
        logger.warning("could not restore the apply ledger", exc_info=True)


def _pythons(root: Path, plan: MigrationPlan) -> tuple[str | None, str | None]:
    """The Python each side of the plan's texts is for: the base (before-texts) and the target
    (after-texts). Anchors parse with them (a 3.6 text may use ``async`` as a name)."""
    from pymolt.ingestion.config import EnvConfig

    config = EnvConfig.load(root / ".pymolt" / "env_config.json")
    base = (plan.python or {}).get("from") or (config.base_python if config else None)
    return base, plan.target_python or (config.target_python if config else None)


def _ledger_steps(plan: MigrationPlan) -> list[tuple[PlanStep, list[tuple[str, str, str]]]]:
    """The pymolt code steps a whole-plan apply writes, in order, each with its files'
    ``(path, before-text, after-text)``. The plan's own steps when their hunks chain every
    changed file from ``old_source`` to ``new_source``; otherwise (a plan with no steps, or one
    whose steps do not chain) its one whole-file step, ``v1``."""
    from pymolt.workorder.edits import HunkMismatch, apply_hunks
    from pymolt.workorder.edits import sha256 as text_sha

    if not plan.changes:
        return []
    texts = {c.path: c.old_source for c in plan.changes}
    chained: list[tuple[PlanStep, list[tuple[str, str, str]]]] = []
    code = [s for s in plan.steps if s.executor == "pymolt" and s.edits
            and s.type in ("codemod", "python-fix") and s.do.recipe is not None]
    for step in code:
        files = []
        for edit in step.edits:
            before = texts.get(edit.file)
            if before is None or text_sha(before) != edit.before_sha256:
                chained = []
                break
            try:
                after = apply_hunks(before, edit.hunks)
            except HunkMismatch:
                chained = []
                break
            if text_sha(after) != edit.after_sha256:
                chained = []
                break
            texts[edit.file] = after
            files.append((edit.file, before, after))
        else:
            chained.append((step, files))
            continue
        break
    if chained and all(texts[c.path] == c.new_source for c in plan.changes):
        return chained
    step = plan.step(V1_STEP_ID) or _whole_plan_step(plan)
    return [(step, [(c.path, c.old_source, c.new_source) for c in plan.changes])]


def _whole_plan_step(plan: MigrationPlan) -> PlanStep:
    """The plan's one whole-file step (``workorder.v1.v1_step``). Its anchors are only
    metadata here; should making them fail, the step is built without them (its edits then
    name the texts by hash and hunks alone), so the apply is never refused for its
    provenance."""
    from pymolt.workorder.edits import hunks_of
    from pymolt.workorder.models import StepDo, StepEdit, StepEffort, StepRecipe, step_key
    from pymolt.workorder.v1 import v1_step

    try:
        return v1_step(plan)
    except Exception:  # noqa: BLE001 - provenance degrades, the apply stands
        logger.warning("could not build the plan's whole-file step", exc_info=True)
    recipes = list(dict.fromkeys(r for c in plan.changes for r in c.recipes))
    claims = [c for change in plan.changes for c in change.claims.values()]
    return PlanStep(
        id=V1_STEP_ID, key=step_key("codemod", "v1|" + "|".join(sorted(recipes))),
        phase="mechanical", type="codemod", title="The plan's rewrite", executor="pymolt",
        claim="preserve" if claims else None,
        do=StepDo(recipe=StepRecipe(kind="whole-file", keys=recipes, sites="all")),
        edits=[StepEdit(file=c.path, before_sha256=c.old_sha256, after_sha256=c.new_sha256,
                        hunks=hunks_of(c.old_source, c.new_source)) for c in plan.changes],
        effort=StepEffort(sites=sum(c.sites for c in plan.changes), files=len(plan.changes),
                          why="pymolt applies it"))


def _step_risk_ids(plan: MigrationPlan, step: PlanStep) -> list[str]:
    """The plan-time ids of the risks the step addresses (persisted on its event, M4). A
    whole-file step addresses every risk the plan fixes."""
    if step.why.risk_ids:
        return list(step.why.risk_ids)
    risks = plan.risks.risks if plan.risks is not None else []
    return [r.id for r in risks if r.step_id == step.id
            or (step.id == V1_STEP_ID and r.step_id is None and r.fixed_by_plan)]


def _step_sites(plan: MigrationPlan, step: PlanStep, files: list[str]) -> int:
    if step.effort.sites:
        return step.effort.sites
    return sum(c.sites for c in plan.changes if c.path in files) if step.id == V1_STEP_ID \
        else 0


def rollback_migration(
    project_dir: str | Path,
    *,
    run_id: str | None = None,
    force: bool = False,
    agent: bool = False,
):
    """Roll back every step in effect of the run (default: the latest), restoring each file it
    touched (``workorder.rollback``). Returns ``(plan, rolled_back_at, run_id)``. Under ``agent``
    it is refused: a whole run is a person's to roll back (``workorder.rollback``)."""
    from pymolt.workorder.rollback import rollback_steps

    result = rollback_steps(project_dir, run_id=run_id, force=force, agent=agent)
    return result.plan, result.rolled_back_at, result.run_id
