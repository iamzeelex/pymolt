"""Durable, reviewable migration plans and reversible application.

A plan freezes the exact before/after source that a dry-run produced. Applying
it never asks Axiom Graph to recompute recipes: every source hash is checked and
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

from pydantic import BaseModel, Field, computed_field, field_validator
from pydantic_core import ValidationError

from pymolt.apply_ledger import claim_for
from pymolt.codemods.models import ApiImpact, FilePreview, recipe_key
from pymolt.verify.predict import PredictionReport

logger = logging.getLogger(__name__)

PLAN_SCHEMA_VERSION = 1
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


class PlannedApiImpact(ApiImpact):
    package: str


class PlanInspection(BaseModel):
    reasons: list[str] = Field(default_factory=list)

    @computed_field
    @property
    def valid(self) -> bool:
        return not self.reasons


class MigrationPlan(BaseModel):
    schema_version: int = PLAN_SCHEMA_VERSION
    plan_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    created_at: str = Field(default_factory=_now)
    target_python: str
    source_manifest: str
    target_manifest_path: str | None = None
    target_manifest_sha256: str | None = None
    environment_fingerprint: str | None = None
    baseline_trace_path: str | None = None
    baseline_captured_at: str | None = None
    baseline_trace_sha256: str | None = None
    dependencies: list[PlannedDependency] = Field(default_factory=list)
    impacts: list[PlannedApiImpact] = Field(default_factory=list)
    files_scanned: int = 0
    changes: list[PlannedFileChange] = Field(default_factory=list)
    advisories: list[PlannedAdvisory] = Field(default_factory=list)
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
                return cls.model_validate(json.load(stream))
        except (OSError, json.JSONDecodeError, ValidationError):
            return None

    def save(
        self, project_dir: str | Path, path: str | Path | None = None
    ) -> Path:
        return _atomic_json(
            plan_path(project_dir, path), self.model_dump(mode="json")
        )


class MigrationRunState(BaseModel):
    run_id: str
    status: Literal["prepared", "applied", "failed", "rolled_back"]
    prepared_at: str
    applied_at: str | None = None
    rolled_back_at: str | None = None
    error: str | None = None


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
) -> MigrationPlan:
    """Freeze a locally verified preview into an exact, portable project plan.

    With a baseline capture (and ``predict``), the plan also stores its predicted risks:
    the baseline's facts joined with ``delta_reports`` (the migrations' ``pymolt.delta/1``
    reports; computed offline with ``pymolt.delta.compute_deltas`` when not given)."""
    from pymolt.verify.service import environment_fingerprint, load_contract_state

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
        stage = _python_stage(root, python_move, previews)
    if stage is not None:
        previews = stage.previews

    changes: list[PlannedFileChange] = []
    advisories: list[PlannedAdvisory] = []
    for preview in previews:
        relative = _relative_project_file(root, preview.path)
        if preview.old_source != preview.new_source:
            changes.append(PlannedFileChange(
                path=relative,
                old_sha256=_sha256(preview.old_source),
                new_sha256=_sha256(preview.new_source),
                old_source=preview.old_source,
                new_source=preview.new_source,
                sites=preview.sites,
                recipes=[
                    recipe_key(item)
                    for item in [*preview.rules, *preview.patterns]
                ],
                claims={recipe_key(item): claim for item in [*preview.rules, *preview.patterns]
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
            ))

    risks = None
    if predict and baseline_path is not None:
        risks = _predict_risks(root, baseline_path, list(migrations), changes, delta_reports)
    type_relocations = list(getattr(risks, "_type_relocations", None) or [])
    python, compile_findings = None, []
    if predict and python_move is not None:
        risks, python, compile_findings = _python_part(root, python_move, risks, stage)

    return MigrationPlan(
        target_python=target_python,
        source_manifest=source_manifest,
        target_manifest_path=target_relative,
        target_manifest_sha256=target_hash,
        environment_fingerprint=environment_fingerprint(root),
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
        risks=risks,
        type_relocations=type_relocations,
        python=python,
        compile_findings=compile_findings,
    )


def _runs_on_base(preview) -> bool | None:
    """The python fixes in ``preview`` run on the base only if every one of them does."""
    fixes = [p for p in preview.patterns if getattr(p, "scope", "dependency") == "python"]
    if not fixes:
        return None
    flags = [p.runs_on_base for p in fixes]
    if all(f is True for f in flags):
        return True
    return False if any(f is False for f in flags) else None


def _python_stage(root: Path, move: tuple[str, str], previews: list[FilePreview]):
    """The python move computed for ``previews``; None (with no raise) when it fails."""
    try:
        from pymolt.pyversion.plan import plan_python_stage

        return plan_python_stage(root, move[0], move[1], previews=previews)
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
) -> PlanInspection:
    """Check that a plan still describes the current files and environment."""
    from pymolt.verify.service import environment_fingerprint

    root = Path(project_dir).resolve()
    reasons: list[str] = []
    if plan.schema_version != PLAN_SCHEMA_VERSION:
        reasons.append(
            f"unsupported plan schema {plan.schema_version}; expected {PLAN_SCHEMA_VERSION}"
        )
    if check_environment:
        current_fingerprint = environment_fingerprint(root)
        if plan.environment_fingerprint != current_fingerprint:
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


def apply_migration_plan(
    project_dir: str | Path,
    plan: MigrationPlan,
    *,
    allow_no_baseline: bool = False,
):
    with _migration_lock(project_dir, "apply"):
        return _apply_migration_plan_unlocked(
            project_dir, plan, allow_no_baseline=allow_no_baseline
        )


def _recover_interrupted_apply(root: Path, plan: MigrationPlan) -> None:
    """Restore a prepared/failed run to its exact pre-apply source boundary."""
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


def _apply_migration_plan_unlocked(
    project_dir: str | Path,
    plan: MigrationPlan,
    *,
    allow_no_baseline: bool = False,
):
    """Apply exactly one reviewed plan and retain everything needed to undo it."""
    from pymolt.codemods.apply import write_previews
    from pymolt.migration_state import MigrationReceipt
    from pymolt.verify.models import CaptureValidity
    from pymolt.verify.service import (
        environment_fingerprint,
        inspect_trace_artifact,
        load_contract_state,
    )

    root = Path(project_dir).resolve()
    run_dir = _run_dir(root, plan.plan_id)
    existing = _load_run_state(run_dir / "state.json")
    if existing and existing.status in {"prepared", "failed"}:
        frozen = MigrationPlan.load(root, run_dir / "plan.json")
        if frozen is None or frozen.model_dump() != plan.model_dump():
            raise MigrationConflictError(
                f"interrupted run {plan.plan_id[:8]} does not match this plan"
            )
        _recover_interrupted_apply(root, frozen)
    if existing and existing.status == "applied":
        return _already_applied(root, plan, existing)

    inspection = inspect_migration_plan(root, plan)
    if not inspection.valid:
        raise MigrationConflictError(
            "stale migration plan: " + "; ".join(inspection.reasons)
        )

    # The world this migration starts from, taken before any source write. The
    # receipt keeps it so the baseline can later be judged against it rather
    # than against whatever manifest the migration moved the project to.
    starting_fingerprint = environment_fingerprint(root)
    baseline = load_contract_state(root).baseline
    if not allow_no_baseline:
        if baseline is None:
            raise MigrationEvidenceError(
                "a valid baseline capture is required before apply"
            )
        quality = inspect_trace_artifact(baseline.trace_path)
        if (
            baseline.validity is not CaptureValidity.VALID
            or quality.validity is not CaptureValidity.VALID
            or baseline.env_fingerprint != starting_fingerprint
        ):
            raise MigrationEvidenceError("the baseline capture is invalid or stale")
        if not plan.baseline_trace_path or not plan.baseline_captured_at:
            raise MigrationEvidenceError(
                "the plan is not bound to its authorizing baseline"
            )
        from pymolt.verify.service import same_slot_path

        if (
            not same_slot_path(root, plan.baseline_trace_path, baseline.trace_path)
            or plan.baseline_captured_at != baseline.captured_at
        ):
            raise MigrationEvidenceError(
                "the baseline capture was replaced after planning"
            )
        baseline_path = Path(baseline.trace_path)
        if not baseline_path.is_absolute():
            baseline_path = root / baseline_path
        try:
            baseline_hash = hashlib.sha256(baseline_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise MigrationEvidenceError("the baseline trace is unreadable") from exc
        if not plan.baseline_trace_sha256 or baseline_hash != plan.baseline_trace_sha256:
            raise MigrationEvidenceError(
                "the baseline trace content changed after planning"
            )

    if existing and existing.status == "rolled_back":
        raise MigrationConflictError(f"plan {plan.plan_id[:8]} is already rolled back")
    run_dir.mkdir(parents=True, exist_ok=True)
    frozen_plan_path = plan.save(root, run_dir / "plan.json")
    state = MigrationRunState(
        run_id=plan.plan_id, status="prepared", prepared_at=_now()
    )
    _save_run_state(run_dir, state)

    previews = [
        FilePreview(
            path=str(_project_file(root, change.path)),
            old_source=change.old_source,
            new_source=change.new_source,
            sites=change.sites,
        )
        for change in plan.changes
    ]
    try:
        write_previews(previews)
    except BaseException as exc:
        _save_run_state(
            run_dir,
            state.model_copy(update={"status": "failed", "error": str(exc)}),
        )
        raise

    applied_at = _now()
    _write_apply_ledger(root, plan, applied_at)
    receipt = MigrationReceipt(
        run_id=plan.plan_id,
        applied_at=applied_at,
        target_python=plan.target_python,
        source_manifest=plan.source_manifest,
        target_manifest_path=(
            str(_project_file(root, plan.target_manifest_path))
            if plan.target_manifest_path else None
        ),
        baseline_trace_path=baseline.trace_path if baseline else None,
        baseline_captured_at=baseline.captured_at if baseline else None,
        environment_fingerprint=starting_fingerprint,
        allow_no_baseline=allow_no_baseline,
        files_changed=[str(_project_file(root, item.path)) for item in plan.changes],
        patterns_applied=sum(item.sites for item in plan.changes),
        no_codemods_required=not plan.changes,
        plan_path=str(frozen_plan_path),
    )
    try:
        _save_run_state(
            run_dir,
            state.model_copy(update={"status": "applied", "applied_at": applied_at}),
        )
        receipt.save(root)
    except BaseException as metadata_exc:
        reverse = [
            FilePreview(
                path=preview.path,
                old_source=preview.new_source,
                new_source=preview.old_source,
                sites=preview.sites,
            )
            for preview in previews
        ]
        try:
            write_previews(reverse)
        except BaseException as rollback_exc:
            raise RuntimeError(
                "source apply succeeded, metadata failed, and automatic rollback "
                f"was incomplete: {rollback_exc}"
            ) from metadata_exc
        try:
            _save_run_state(
                run_dir,
                state.model_copy(update={
                    "status": "failed", "error": f"metadata: {metadata_exc}",
                }),
            )
        except OSError:
            pass
        raise
    return receipt


def _already_applied(root: Path, plan: MigrationPlan, state: MigrationRunState):
    """Apply run again on an applied plan: a no-op while every planned file still holds the
    plan's new source (the receipt says so); a conflict, with nothing written, otherwise."""
    from pymolt.migration_state import MigrationReceipt

    inspection = inspect_migration_plan(root, plan, expected="new", check_environment=False)
    if not inspection.valid:
        raise MigrationConflictError(
            f"plan {plan.plan_id[:8]} is already applied (run {state.run_id[:8]}), and "
            "files changed since: " + "; ".join(inspection.reasons),
            hint="Nothing was written. `pymolt rollback .` restores the files the apply "
                 "changed (`--force` also discards the later edits). A plan made now would "
                 "stack a second migration on the applied one.",
        )
    receipt = MigrationReceipt.load(root)
    if receipt is None or receipt.run_id != state.run_id:
        # A later run's receipt is the saved one: describe this run from its frozen plan.
        receipt = MigrationReceipt(
            run_id=state.run_id, applied_at=state.applied_at or state.prepared_at,
            target_python=plan.target_python, source_manifest=plan.source_manifest,
            target_manifest_path=(str(_project_file(root, plan.target_manifest_path))
                                  if plan.target_manifest_path else None),
            baseline_trace_path=plan.baseline_trace_path,
            baseline_captured_at=plan.baseline_captured_at,
            files_changed=[str(_project_file(root, item.path)) for item in plan.changes],
            patterns_applied=sum(item.sites for item in plan.changes),
            no_codemods_required=not plan.changes,
            plan_path=str(_run_dir(root, state.run_id) / "plan.json"),
        )
    receipt.mark_outcome("already_applied")
    return receipt


def _write_apply_ledger(root: Path, plan: MigrationPlan, applied_at: str) -> None:
    """Record what apply wrote, from the files as they are now. Never fails the apply."""
    from pymolt.apply_ledger import write_ledger

    try:
        files = []
        for change in plan.changes:
            written = _project_file(root, change.path).read_text(encoding="utf-8")
            files.append((change.path, change.old_source, written, change.recipes,
                          change.claims))
        write_ledger(root, plan.plan_id, applied_at, files)
    except Exception:  # noqa: BLE001 - provenance is best effort; the migration stands
        logger.warning("could not write the apply ledger", exc_info=True)


def rollback_migration(
    project_dir: str | Path,
    *,
    run_id: str | None = None,
    force: bool = False,
):
    with _migration_lock(project_dir, "rollback"):
        return _rollback_migration_unlocked(
            project_dir, run_id=run_id, force=force
        )


def _rollback_migration_unlocked(
    project_dir: str | Path,
    *,
    run_id: str | None = None,
    force: bool = False,
):
    """Atomically restore every source frozen in an applied migration plan."""
    from pymolt.codemods.apply import write_previews
    from pymolt.migration_state import MigrationReceipt
    from pymolt.verify.service import (
        load_contract_state,
        restore_archived_capture,
        set_aside_post_migration,
    )

    root = Path(project_dir).resolve()
    receipt = MigrationReceipt.load(root)
    original_receipt = receipt.model_copy(deep=True) if receipt is not None else None
    contract_state = load_contract_state(root)
    original_contract_state = contract_state.model_copy(deep=True)
    effective_run_id = run_id or (receipt.run_id if receipt else None)
    if not effective_run_id:
        raise MigrationConflictError("no applied migration is available to roll back")
    run_dir = _run_dir(root, effective_run_id)
    state = _load_run_state(run_dir / "state.json")
    if state is None or state.status != "applied":
        status = state.status if state else "missing"
        raise MigrationConflictError(
            f"migration {effective_run_id[:8]} is not rollback-ready ({status})"
        )
    plan = MigrationPlan.load(root, run_dir / "plan.json")
    if plan is None:
        raise MigrationPlanError(f"frozen plan for {effective_run_id[:8]} is unreadable")

    inspection = inspect_migration_plan(
        root, plan, expected="new", check_environment=False
    )
    if not inspection.valid and not force:
        raise MigrationConflictError(
            "refusing to overwrite post-migration edits: "
            + "; ".join(inspection.reasons)
            + "; pass --force only if discarding them is intentional"
        )

    previews: list[FilePreview] = []
    for change in plan.changes:
        path = _project_file(root, change.path)
        try:
            current = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise MigrationPlanError(f"cannot restore missing source: {change.path}") from exc
        previews.append(FilePreview(
            path=str(path),
            old_source=current if force else change.new_source,
            new_source=change.old_source,
            sites=change.sites,
        ))
    # What the rollback rewrites: a run that changed no file (a no-op apply) restores nothing.
    restored = [
        change.path for change, preview in zip(plan.changes, previews, strict=True)
        if preview.old_source != preview.new_source
    ]
    rolled_back_at = _now()
    write_previews(previews)
    state_path = root / ".pymolt" / "contract_state.json"
    original_post = original_contract_state.post_migration
    archived: str | None = None
    try:
        _save_run_state(
            run_dir,
            state.model_copy(update={
                "status": "rolled_back", "rolled_back_at": rolled_back_at,
            }),
        )
        if receipt and receipt.run_id == effective_run_id:
            receipt = receipt.model_copy(update={
                "status": "rolled_back", "rolled_back_at": rolled_back_at,
                "files_restored": restored,
            })
            receipt.save(root)
        # The post-migration capture stays while the code it recorded is still on disk (the
        # rollback restored no file); otherwise it is set aside, kept among the diagnostics.
        if contract_state.post_migration is not None and restored:
            archived = set_aside_post_migration(root, contract_state, effective_run_id)
            contract_state.save(state_path)
    except BaseException as metadata_exc:
        forward = [
            FilePreview(
                path=preview.path,
                old_source=preview.new_source,
                new_source=preview.old_source,
                sites=preview.sites,
            )
            for preview in previews
        ]
        try:
            write_previews(forward)
        except BaseException as restore_exc:
            raise RuntimeError(
                "source rollback succeeded, metadata failed, and restoring the applied "
                f"state was incomplete: {restore_exc}"
            ) from metadata_exc
        try:
            _save_run_state(run_dir, state)
            if original_receipt is not None:
                original_receipt.save(root)
            if archived is not None and original_post is not None:
                restore_archived_capture(archived, original_post.trace_path)
            original_contract_state.save(state_path)
        except OSError:
            pass
        raise
    return plan, rolled_back_at
