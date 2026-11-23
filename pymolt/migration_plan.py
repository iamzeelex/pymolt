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
import re
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    computed_field,
    field_validator,
    model_validator,
)

from pymolt.apply_ledger import claim_for
from pymolt.codemods.models import ApiImpact, FilePreview, recipe_key
from pymolt.state_io import (
    MIGRATION_LOCK,
    FileLock,
    LockBusy,
    atomic_write_json,
    holds_state_lock,
    keep_remedy,
    load_state,
    rebuild_remedy,
    schema_version,
    state_lock,
)
from pymolt.verify.predict import PredictionReport
from pymolt.workorder.models import PLAN_SCHEMA_ID, PLAN_SCHEMA_VERSION, V1_STEP_ID, PlanStep

logger = logging.getLogger(__name__)

# PLAN_SCHEMA_VERSION 3: the plan is a work order of steps (``pymolt.workorder``, schema 2) with
# an identity (its name, source, baseline and lineage). A schema-2 file is read as a schema-3
# plan whose identity is filled from what it holds (``fill_identity``); a schema-1 file as one
# synthetic step (``pymolt.workorder.v1``).
# Plans are stored in ``.pymolt/plans/<name>/plan.json`` (``pymolt.plan_store``); this is the
# 0.2 layout's single plan file, read while there is no current-plan pointer.
DEFAULT_PLAN_PATH = Path(".pymolt/migration_plan.json")

#: A plan's label (``plan --label``): lowercase letters, digits, ``.``, ``_`` and ``-``, 1 to 32
#: characters, beginning and ending with a letter or a digit. Nothing else is rewritten into one.
LABEL_PATTERN = r"[a-z0-9](?:[a-z0-9._-]{0,30}[a-z0-9])?"
LABEL_RE = re.compile(rf"^{LABEL_PATTERN}$")
LABEL_RULE = ("a label is 1 to 32 lowercase letters, digits, '.', '_' or '-', beginning and "
              "ending with a letter or a digit")
#: A plan's name: ``YYYYMMDDTHHMMZ-<id8>[-<label>]``, its creation time in UTC to the minute, the
#: first 8 hex of its id and its label. It sorts by time and is safe as a folder name.
NAME_RE = re.compile(rf"^(\d{{8}}T\d{{4}}Z)-([0-9a-f]{{8}})(?:-({LABEL_PATTERN}))?$")


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
    """Serialize source mutations; the OS releases this lock after a crash. Refused at once
    (``MigrationConflictError``) while another operation holds it, this thread included.
    Taken before the state lock, never while holding it (``pymolt.state_io``)."""
    root = Path(project_dir).resolve()
    if holds_state_lock(root):
        raise RuntimeError("lock order: migration.lock is taken before state.lock, never "
                           "while holding it")
    lock = FileLock(root / MIGRATION_LOCK, operation, timeout=0)
    try:
        lock.acquire()
    except LockBusy as exc:
        raise MigrationConflictError(
            f"another migration operation is active ({exc.holder})") from exc
    try:
        yield
    finally:
        lock.release()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _sha256(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


PLAN_KIND = "plan"
PLAN_REMEDY = rebuild_remedy("run `pymolt plan .` to plan again")
FROZEN_PLAN_KIND = "frozen plan of an apply run"
FROZEN_PLAN_REMEDY = keep_remedy("the texts this run's rollback restores")
RUN_STATE_KIND = "apply run state"
RUN_STATE_REMEDY = keep_remedy("this run's rollback information")


def _plan_file_kind(project_dir: str | Path, source: Path) -> tuple[str, str]:
    """What a plan file is to a person (a saved plan, a run's frozen copy, or a plan file
    given on the command line) and the remedy when it is damaged. By its path alone: a damaged
    pointer never stops a frozen copy from loading."""
    from pymolt.plan_store import is_stored_plan_file

    if source.parent.parent.name == "migrations" and source.parent.parent.parent.name == \
            ".pymolt":
        return FROZEN_PLAN_KIND, FROZEN_PLAN_REMEDY
    root = Path(project_dir).resolve()
    resolved = source.resolve()
    if is_stored_plan_file(root, resolved) or resolved == (root / DEFAULT_PLAN_PATH).resolve():
        return PLAN_KIND, PLAN_REMEDY
    return PLAN_KIND, "Pass a readable plan file, or run `pymolt plan .` to plan again."


def plan_path(project_dir: str | Path, path: str | Path | None = None) -> Path:
    """``path`` (relative to the project), or the current plan's file: the one the current-plan
    pointer names (``.pymolt/plans/<name>/plan.json``), else the 0.2 layout's single file. A
    pointer whose plan is not in the store raises ``StateFileError``."""
    root = Path(project_dir).resolve()
    if path is None:
        from pymolt.plan_store import current_pointer

        return current_pointer(root)[1]
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


def plan_name(created_at: str, plan_id: str, label: str | None = None) -> str:
    """A plan's name (``NAME_RE``): ``created_at`` in UTC to the minute, ``plan_id[:8]`` and the
    label. Raises ``ValueError`` when ``created_at`` is not a time (a time without a zone is
    read as UTC) or the label breaks ``LABEL_RULE``."""
    when = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    if when.tzinfo is not None:
        when = when.astimezone(UTC)
    if label is not None and not LABEL_RE.match(label):
        raise ValueError(f"invalid label {label!r}: {LABEL_RULE}")
    return f"{when:%Y%m%dT%H%MZ}-{_validate_run_id(plan_id)[:8]}" + (f"-{label}" if label else "")


def _plan_id_field(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return _validate_run_id(value)
    except MigrationPlanError as exc:
        raise ValueError(str(exc)) from exc


SourceQuality = Literal["git", "no-commit", "not-a-repo", "no-git", "error"]


class PlanSource(BaseModel):
    """The code a plan was made from, as the project's local git repository says it
    (``adapters.git.git_facts``): ``HEAD``'s commit, whether a tracked file under the project
    differed from it (``.pymolt/`` aside), and the project's folder in the repository (POSIX,
    ``""`` at its top). Each is None when unknown; ``quality`` says why, ``note`` in words."""

    git_commit: str | None = None
    git_dirty: bool | None = None
    git_prefix: str | None = None
    quality: SourceQuality
    note: str | None = None


class CaptureRef(BaseModel):
    """The baseline capture a plan was made from, copied from its slot at plan time:
    ``trace_sha256`` is the recording's sha256 as the plan hashed it and ``capture_id`` the id
    made from it (``verify.models.capture_id``). The slot's fingerprints are kept beside the
    plan's own ``environment_*fingerprint`` (taken at plan time), so a mismatch shows."""

    capture_id: str | None = None
    captured_at: str
    trace_sha256: str | None = None
    env_fingerprint: str | None = None
    env_setup_fingerprint: str | None = None
    command: list[str] = Field(default_factory=list)


class PlanLineage(BaseModel):
    """Where a plan comes from, by plan ids: the first plan of its lineage (``root``; its own id
    for a fresh plan), the plan it was derived from (``parent``) and how many derivations lie
    between them (``generation``). Recorded only: nothing is inherited through it yet."""

    root: str
    parent: str | None = None
    generation: int = Field(default=0, ge=0)

    @field_validator("root", "parent")
    @classmethod
    def _ids_are_plan_ids(cls, value: str | None) -> str | None:
        return _plan_id_field(value)


#: The fields ``content_digest_of`` leaves out: what names, times and places a plan, and the
#: inputs ``plan`` compares on their own (the baseline, the environment, the target manifest).
#: A field missing here is part of the content: forgetting an identity field only makes an
#: unchanged re-plan save again, while forgetting a content field would hide a change.
DIGEST_EXCLUDED_FIELDS = frozenset({
    # identity and time
    "schema_version", "plan_id", "created_at", "name", "label", "adapted_from", "content_digest",
    # provenance
    "pymolt_version", "source", "lineage",
    # input bindings
    "baseline", "baseline_trace_path", "baseline_captured_at", "baseline_trace_sha256",
    "environment_fingerprint", "environment_setup_fingerprint", "target_manifest_sha256",
})


def content_digest_of(plan: MigrationPlan) -> str:
    """sha256 of what the plan says to do: its canonical JSON (``model_dump(mode="json")``,
    sorted keys, no spaces) without ``DIGEST_EXCLUDED_FIELDS`` and without the provenance text
    that varies with cache state: ``risks.notes``, each ``risks.deltas[*].notes``,
    ``python.notes`` and ``python.sides`` (e.g. "old side read statically from ...")."""
    # A JSON round trip first: a plan made in memory and the same plan read back hash alike.
    data = json.loads(json.dumps(plan.model_dump(mode="json")))
    for field in DIGEST_EXCLUDED_FIELDS:
        data.pop(field, None)
    risks = data.get("risks")
    if isinstance(risks, dict):
        risks.pop("notes", None)
        for delta in risks.get("deltas") or ():
            if isinstance(delta, dict):
                delta.pop("notes", None)
    python = data.get("python")
    if isinstance(python, dict):
        python.pop("notes", None)
        python.pop("sides", None)
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
    # The schema this plan was read from when an adapter made it (1: a schema-1 plan, read as
    # one synthetic step ``v1``; 2: a schema-2 plan, its identity filled by ``fill_identity``);
    # None for a plan written as it is.
    adapted_from: int | None = None
    # Schema 3: the plan's identity. Optional, so an adapted schema-2 plan fits; pymolt 0.3
    # writes them for every plan it makes.
    # ``YYYYMMDDTHHMMZ-<id8>[-<label>]`` (``NAME_RE``): the plan's folder under .pymolt/plans/.
    name: str | None = None
    label: str | None = None
    # The pymolt that made the plan; None: unknown (a plan made before it was recorded).
    pymolt_version: str | None = None
    source: PlanSource | None = None
    baseline: CaptureRef | None = None
    lineage: PlanLineage | None = None
    # ``content_digest_of`` at save time (64 hex).
    content_digest: str | None = None

    # The JSON object the plan was read from (``load``); None for a plan made in memory. Its
    # stored form: what ``plan_digest`` hashes and ``freeze`` writes, so an adapted plan's
    # frozen copy and approval stay those of the file as it was written.
    _stored: dict[str, Any] | None = PrivateAttr(default=None)
    # Why the baseline binding needs a person's eye (the recording changed since its capture);
    # set by ``create_migration_plan``, never saved.
    _baseline_warning: str | None = PrivateAttr(default=None)

    def __eq__(self, other: object) -> bool:
        """Plans are equal by their fields: where a plan was read from is not part of it."""
        if not isinstance(other, MigrationPlan):
            return NotImplemented
        return type(self) is type(other) and self.__dict__ == other.__dict__

    @property
    def setup_fingerprint(self) -> str | None:
        """The setup fingerprint at plan time (``environment_setup_fingerprint``): H3's input."""
        return self.environment_setup_fingerprint

    @property
    def baseline_warning(self) -> str | None:
        """Set on a plan just made when its baseline's recording no longer has the sha256 its
        capture stamped: the plan binds the recording as it is now."""
        return self._baseline_warning

    def step(self, step_id: str) -> PlanStep | None:
        return next((s for s in self.steps if s.id == step_id), None)

    def stored_form(self) -> dict[str, Any]:
        """The plan's JSON object as stored: as it was read, or ``model_dump`` for a plan made in
        memory."""
        return self._stored if self._stored is not None else self.model_dump(mode="json")

    @field_validator("plan_id")
    @classmethod
    def _plan_id_is_safe(cls, value: str) -> str:
        try:
            return _validate_run_id(value)
        except MigrationPlanError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("label")
    @classmethod
    def _label_is_safe(cls, value: str | None) -> str | None:
        if value is not None and not LABEL_RE.match(value):
            raise ValueError(f"invalid label {value!r}: {LABEL_RULE}")
        return value

    @field_validator("content_digest")
    @classmethod
    def _digest_is_hex(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("content_digest is not a sha256 (64 lowercase hex)")
        return value

    @model_validator(mode="after")
    def _name_is_the_plans(self) -> MigrationPlan:
        """A name is a folder name: it must be ``NAME_RE``, with this plan's id and label."""
        if self.name is None:
            return self
        match = NAME_RE.match(self.name)
        if match is None:
            raise ValueError(f"invalid plan name {self.name!r}: not YYYYMMDDTHHMMZ-<id8>"
                             "[-<label>]")
        if match.group(2) != self.plan_id[:8] or match.group(3) != self.label:
            raise ValueError(f"plan name {self.name!r} does not carry the plan's id "
                             f"{self.plan_id[:8]} and label {self.label!r}")
        return self

    @classmethod
    def load(
        cls, project_dir: str | Path, path: str | Path | None = None
    ) -> MigrationPlan | None:
        """The plan at ``path`` (default: the current plan, ``plan_path``; a schema-2 plan is
        read with its identity filled, a schema-1 plan as one synthetic step); None when there
        is none. A damaged plan, one written by a newer pymolt, or a current plan whose id is
        not the pointer's raises ``StateFileError``."""
        pointer = None
        if path is None:
            from pymolt.plan_store import current_pointer

            pointer, source = current_pointer(project_dir)
        else:
            source = plan_path(project_dir, path)
        kind, remedy = _plan_file_kind(project_dir, source)

        def modified() -> float | None:
            try:
                return source.stat().st_mtime
            except OSError:
                return None

        def adapt(data: dict) -> MigrationPlan:
            version = schema_version(data)
            if version == 1:
                from pymolt.workorder.v1 import read_v1

                plan = read_v1(cls, data, modified=modified)
            elif version == 2:
                plan = fill_identity(cls.model_validate({
                    **data, "schema_version": PLAN_SCHEMA_VERSION,
                    # A frozen copy 0.2 made of a schema-1 plan keeps 1.
                    "adapted_from": data.get("adapted_from") or 2}), modified=modified)
            else:
                plan = cls.model_validate(data)
            plan._stored = data
            return plan

        plan = load_state(source, cls, kind=kind, newest=PLAN_SCHEMA_VERSION, remedy=remedy,
                          adapt=adapt)
        if pointer is not None and plan is not None:
            from pymolt.plan_store import check_plan_id

            check_plan_id(project_dir, pointer, plan.plan_id)
        return plan

    def freeze(self, path: str | Path) -> Path:
        """Write the plan's stored form (``stored_form``) to ``path``: a run's frozen copy is
        the plan file as it was read, so its digest, and the approvals bound to it, hold."""
        return atomic_write_json(path, self.stored_form())

    def save(
        self, project_dir: str | Path, path: str | Path | None = None
    ) -> Path:
        """Store the plan and make it the current plan (``pymolt.plan_store``): its name is
        stamped when it has none, ``.pymolt/plans/<name>/plan.json`` is written in a folder
        created for it (a folder another plan took draws a new id: the plan is not bound to
        anything yet), and the pointer names it. Refused while the pointer or the current plan
        is damaged or newer (``StateFileError``). With ``path``: written there only, as an
        export (``model_dump``; nothing is stored, the current plan stays)."""
        from pymolt.plan_store import in_store, store, write_pointer

        if path is not None:
            target = plan_path(project_dir, path)
            if in_store(project_dir, target):
                raise MigrationPlanError(
                    f"refusing to write a plan into the plan store by path: {target}")
            return atomic_write_json(target, self.model_dump(mode="json"))
        with state_lock(project_dir, operation="plan"):
            MigrationPlan.load(project_dir)     # never replaces a damaged or newer current plan
            stored = store(project_dir, self)
            write_pointer(project_dir, self, set_by="plan")
            return stored

    def save_planned(self, project_dir: str | Path, *,
                     output: str | Path | None = None):
        """Save the plan ``pymolt plan`` made (``plan_store.save_planned``): with ``output``
        (``-o``) only there; otherwise the 0.2 plan file is moved into the store, the plan is
        stored and made current, and the blob store is collected. Returns a
        ``plan_store.PlanSaveResult``."""
        from pymolt.plan_store import save_planned

        return save_planned(project_dir, self, output=output)


def fill_identity(plan: MigrationPlan, *,
                  modified: Callable[[], float | None] | None = None) -> MigrationPlan:
    """Fill the schema-3 identity of a plan read from an older schema, from what it holds:
    ``name`` from ``created_at`` and ``plan_id`` (``modified()``, the file's mtime, in UTC when
    ``created_at`` is not a time), a fresh ``lineage`` and ``baseline`` from
    ``baseline_captured_at`` and ``baseline_trace_sha256``. ``pymolt_version``, ``source`` and
    ``content_digest`` stay None: unknown. It reads nothing else and is deterministic, so a
    plan and its frozen copy fill alike."""
    from pymolt.verify.models import capture_id

    if plan.name is None:
        try:
            plan.name = plan_name(plan.created_at, plan.plan_id, plan.label)
        except ValueError:
            stamp = modified() if modified is not None else None
            if stamp is not None:
                plan.name = plan_name(datetime.fromtimestamp(stamp, UTC).isoformat(),
                                      plan.plan_id, plan.label)
    if plan.lineage is None:
        plan.lineage = PlanLineage(root=plan.plan_id)
    if plan.baseline is None and plan.baseline_captured_at:
        plan.baseline = CaptureRef(
            capture_id=capture_id(plan.baseline_captured_at, plan.baseline_trace_sha256),
            captured_at=plan.baseline_captured_at, trace_sha256=plan.baseline_trace_sha256)
    return plan


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
    from pymolt.verify.models import capture_id
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
    baseline_ref = None
    baseline_warning = None
    if baseline is not None:
        baseline_path = Path(baseline.trace_path)
        if not baseline_path.is_absolute():
            baseline_path = root / baseline_path
        try:
            # The plan binds the recording as it is now (apply checks this hash), whatever
            # its capture stamped.
            baseline_hash = hashlib.sha256(baseline_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise MigrationPlanError(
                f"cannot bind the plan to baseline trace: {baseline_path}"
            ) from exc
        baseline_ref = CaptureRef(
            capture_id=capture_id(baseline.captured_at, baseline_hash),
            captured_at=baseline.captured_at, trace_sha256=baseline_hash,
            env_fingerprint=baseline.env_fingerprint,
            env_setup_fingerprint=baseline.env_setup_fingerprint,
            command=list(baseline.command))
        if baseline.trace_sha256 and baseline.trace_sha256 != baseline_hash:
            baseline_warning = (
                f"the baseline recording changed since its capture "
                f"({baseline.capture_id or baseline.trace_sha256[:8]}): the plan is bound to "
                f"it as it is now ({baseline_ref.capture_id}); capture the baseline again if "
                "the change was not yours")
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

    plan = MigrationPlan(
        target_python=target_python,
        source_manifest=source_manifest,
        target_manifest_path=target_relative,
        target_manifest_sha256=target_hash,
        environment_fingerprint=environment_fingerprint(root),
        environment_setup_fingerprint=environment_setup_fingerprint(root),
        baseline_trace_path=baseline.trace_path if baseline else None,
        baseline_captured_at=baseline.captured_at if baseline else None,
        baseline_trace_sha256=baseline_hash,
        baseline=baseline_ref,
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
    plan._baseline_warning = baseline_warning
    return plan


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
    """A run's ``state.json``; None when there is none. Damaged: ``StateFileError``."""
    return load_state(path, MigrationRunState, kind=RUN_STATE_KIND, newest=None,
                      remedy=RUN_STATE_REMEDY)


def _save_run_state(run_dir: Path, state: MigrationRunState) -> Path:
    return atomic_write_json(run_dir / "state.json", state.model_dump(mode="json"))


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
