"""The plan store: every plan pymolt saves, by name, and which one is the current plan.

HOST side (3.12+, pydantic). Design: ``docs/design/plan-lineage-and-report.md`` §1.

::

    .pymolt/plans/<name>/plan.json   a plan, written once in a folder created for it
    .pymolt/plans/<name>/PLAN.md     the plan for a person
    .pymolt/plans/current.json       the pointer to the current plan: local state, gitignored

**Plans are immutable.** ``MigrationPlan.save`` stores a plan in a new folder named after it
(``NAME_RE``); nothing rewrites a stored plan. ``plan.json`` and ``PLAN.md`` can be committed.

**The pointer** (``PlanPointer``) names the current plan by name and id, says what made it
current (``plan``, or a mutating ``apply`` of another plan) and when. It is a strict state file:
a damaged or newer pointer, one whose plan is not in the store, or one whose plan holds another
id refuses every command that reads the current plan (``StateFileError``), and ``status``
reports it. It is written atomically, under the state lock (inside the migration lock when one
is held), never as a symlink. ``migration_plan.plan_path`` resolves it, so every reader of
``MigrationPlan.load(root)`` reads the current plan; with no pointer it reads the 0.2 layout's
single file, ``.pymolt/migration_plan.json``.

**A 0.2 project.** Only ``plan`` moves the 0.2 file into the store (``migrate_legacy``), by its
stored form, so its runs, frozen copies and agent approvals hold. Every other command reads it
where it is and writes nothing under ``plans/``.

**Adoption** (``adopt``). A mutating ``apply`` (a step write, ``--skip``, ``--record``) of a plan
that is not the current one makes it current just before its first write. A plan file outside the
store (``plan -o PATH``) is first copied in, by its stored form, under its name; a different plan
stored under that name refuses the apply. ``--dry-run``, ``--brief`` and ``--approve-agent``
never move the pointer.

**No silent adoption.** With no pointer and no 0.2 file, saved plans (a fresh clone of a
repository that commits plan folders, a CI checkout) make no plan current: ``status`` and the
commands that need a current plan say how to choose one (``no_current_plan``), and ``plan`` starts
a fresh lineage.

**References** (``resolve_plan_ref``, ``apply --plan``): ``current``, a path, a name or a plan id
prefix.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, field_validator, model_validator

from pymolt.migration_plan import (
    DEFAULT_PLAN_PATH,
    NAME_RE,
    PLAN_KIND,
    PLAN_REMEDY,
    UNKNOWN_TIME,
    MigrationPlan,
    MigrationPlanError,
    PlanSource,
    _now,
    _validate_run_id,
    content_digest_of,
    name_of,
    plan_name,
)
from pymolt.state_io import (
    StateFileError,
    atomic_write_json,
    display_path,
    load_state,
    read_state_json,
    rebuild_remedy,
    state_lock,
)
from pymolt.workorder.models import PLAN_SCHEMA_VERSION

logger = logging.getLogger(__name__)

PLANS_DIR = Path(".pymolt") / "plans"
POINTER_FILE = "current.json"
PLAN_FILE = "plan.json"
PLAN_MD_FILE = "PLAN.md"
#: Where pymolt 0.2 wrote PLAN.md; still where a plan only exported (``plan -o``) gets it.
LEGACY_PLAN_MD = Path(".pymolt") / "PLAN.md"
POINTER_SCHEMA_VERSION = 1
POINTER_KIND = "current-plan pointer"
POINTER_REMEDY = rebuild_remedy(
    "run `pymolt plan .` to plan again, or make a saved plan current with "
    "`pymolt apply . --plan NAME`")
#: How many new plan ids ``store`` draws when the plan's folder is taken.
REDRAWS = 3

_HEX_PREFIX = re.compile(r"^[0-9a-f]{4,32}$")
_PLAN_ID = re.compile(r"^[0-9a-fA-F]{32}$")
_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
REF_FORMS = ("`current`", "a plan name (YYYYMMDDTHHMMZ-<id8>[-<label>])",
             "a plan id prefix (4 to 32 hex)", "a path to a plan file")


class PlanRefError(MigrationPlanError):
    """``apply --plan REF`` names no plan, or more than one (exit 2)."""


class PlanPointer(BaseModel):
    """``.pymolt/plans/current.json``: which stored plan is current. ``checked_commit``: the
    commit an unchanged re-plan last confirmed it for (None: the plan's own ``source``)."""

    schema_version: int = POINTER_SCHEMA_VERSION
    name: str
    plan_id: str
    set_by: Literal["plan", "apply"]
    set_at: str
    checked_commit: str | None = None

    @field_validator("name")
    @classmethod
    def _name_is_a_plan_name(cls, value: str) -> str:
        if not NAME_RE.match(value):
            raise ValueError(f"{value!r} is not a plan name (YYYYMMDDTHHMMZ-<id8>[-<label>])")
        return value

    @field_validator("plan_id")
    @classmethod
    def _plan_id_is_an_id(cls, value: str) -> str:
        try:
            return _validate_run_id(value)
        except MigrationPlanError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("checked_commit")
    @classmethod
    def _commit_is_a_commit(cls, value: str | None) -> str | None:
        if value is not None and not _COMMIT.match(value):
            raise ValueError(f"{value!r} is not a git commit id")
        return value

    @model_validator(mode="after")
    def _name_is_the_plans(self) -> PlanPointer:
        if NAME_RE.match(self.name).group(2) != self.plan_id[:8]:
            raise ValueError(f"the name {self.name} does not carry the plan id "
                             f"{self.plan_id[:8]}")
        return self


@dataclasses.dataclass
class PlanSaveResult:
    """What ``save_planned`` did. ``path``: where the plan is (its ``plan.json``, or the
    ``-o`` file); ``saved``: whether it was stored under ``plans/`` and made current;
    ``unchanged_from``: the current plan an unchanged re-plan kept (its name; never set yet);
    ``moved_legacy``: the name the 0.2 plan file was moved in under; ``pruned``: the plans
    retention removed."""

    plan: MigrationPlan
    path: Path
    saved: bool
    unchanged_from: str | None = None
    moved_legacy: str | None = None
    pruned: list[str] = dataclasses.field(default_factory=list)


# ── paths ─────────────────────────────────────────────────────────────────────────────────


def plans_dir(project_dir: str | Path) -> Path:
    return Path(project_dir).resolve() / PLANS_DIR


def pointer_path(project_dir: str | Path) -> Path:
    return plans_dir(project_dir) / POINTER_FILE


def plan_folder(project_dir: str | Path, name: str) -> Path:
    """The folder of the plan named ``name`` (refused unless it is a plan name: a name is never
    a path)."""
    if not NAME_RE.match(name or ""):
        raise ValueError(f"not a plan name: {name!r}")
    return plans_dir(project_dir) / name


def stored_plan_file(project_dir: str | Path, name: str) -> Path:
    return plan_folder(project_dir, name) / PLAN_FILE


def legacy_plan_file(project_dir: str | Path) -> Path:
    return Path(project_dir).resolve() / DEFAULT_PLAN_PATH


def is_stored_plan_file(project_dir: str | Path, path: str | Path) -> bool:
    """Whether ``path`` is a stored plan's ``plan.json`` (``plans/<name>/plan.json``)."""
    path = Path(path)
    return (path.name == PLAN_FILE and path.parent.parent == plans_dir(project_dir)
            and bool(NAME_RE.match(path.parent.name)))


def in_store(project_dir: str | Path, path: str | Path) -> bool:
    """Whether ``path`` is inside ``.pymolt/plans/``."""
    store = plans_dir(project_dir)
    resolved = Path(path).resolve()
    return resolved == store or store in resolved.parents


def saved_plans(project_dir: str | Path) -> list[str]:
    """The names of the stored plans (a folder named ``NAME_RE`` with a ``plan.json``), oldest
    first."""
    base = plans_dir(project_dir)
    if not base.is_dir():
        return []
    return sorted(child.name for child in base.iterdir()
                  if NAME_RE.match(child.name) and (child / PLAN_FILE).is_file())


def _stamp_name(plan: MigrationPlan) -> str:
    """The plan's name, stamped when it has none (``migration_plan.name_of``)."""
    if plan.name is None:
        plan.name = name_of(plan)
    return plan.name


def _canonical(data: dict) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


# ── the pointer ───────────────────────────────────────────────────────────────────────────


def load_pointer(project_dir: str | Path) -> PlanPointer | None:
    """The pointer; None when there is none. Damaged or newer: ``StateFileError``."""
    return load_state(pointer_path(project_dir), PlanPointer, kind=POINTER_KIND,
                      newest=POINTER_SCHEMA_VERSION, remedy=POINTER_REMEDY)


def pointer_error(project_dir: str | Path, detail: str) -> StateFileError:
    """A pointer that does not name a stored plan as it is: damaged."""
    return StateFileError(pointer_path(project_dir), kind=POINTER_KIND, problem="damaged",
                          detail=detail, remedy=POINTER_REMEDY)


def current_pointer(project_dir: str | Path) -> tuple[PlanPointer | None, Path]:
    """``(pointer, the current plan's file)``: the pointer's stored plan, or with no pointer the
    0.2 layout's single file (which need not exist). A pointer whose plan is not in the store is
    damaged (``StateFileError``)."""
    pointer = load_pointer(project_dir)
    if pointer is None:
        return None, legacy_plan_file(project_dir)
    path = stored_plan_file(project_dir, pointer.name)
    if not path.is_file():
        raise pointer_error(project_dir, f"it names plan {pointer.name}, which is not in "
                                         ".pymolt/plans/")
    return pointer, path


def check_plan_id(project_dir: str | Path, pointer: PlanPointer, plan_id: object) -> None:
    """Refuse a current plan whose id is not the pointer's (``StateFileError``, the pointer).
    A plan file with no valid id is the plan's own damage, which its reader reports."""
    if isinstance(plan_id, str) and _PLAN_ID.match(plan_id) and plan_id != pointer.plan_id:
        raise pointer_error(project_dir, f"it names plan {pointer.name} (id "
                                         f"{pointer.plan_id[:8]}), but that plan.json holds "
                                         f"plan {plan_id[:8]}")


def check_current(project_dir: str | Path) -> Path:
    """The pointer, and the current plan read as JSON only (a plan can be large): raise the
    first ``StateFileError`` (``status`` reports it). Returns the current plan's file."""
    pointer, path = current_pointer(project_dir)
    data = read_state_json(path, kind=PLAN_KIND, newest=PLAN_SCHEMA_VERSION, remedy=PLAN_REMEDY)
    if pointer is not None and data is not None:
        check_plan_id(project_dir, pointer, data.get("plan_id"))
    return path


def check_before_plan(project_dir: str | Path) -> None:
    """What ``plan`` replaces or moves, read before the engine runs, so a damaged or newer file
    refuses the plan now (``StateFileError``): the pointer, the current plan and the 0.2 plan
    file."""
    MigrationPlan.load(project_dir)
    legacy = legacy_plan_file(project_dir)
    if legacy.exists():
        MigrationPlan.load(project_dir, legacy)


def write_pointer(project_dir: str | Path, plan: MigrationPlan, *,
                  set_by: Literal["plan", "apply"],
                  checked_commit: str | None = None) -> PlanPointer:
    """Make ``plan`` (stored, so named) the current plan."""
    from pymolt.workorder.blobs import ensure_gitignore

    pointer = PlanPointer(name=_stamp_name(plan), plan_id=plan.plan_id, set_by=set_by,
                          set_at=_now(), checked_commit=checked_commit)
    root = Path(project_dir).resolve()
    with state_lock(root, operation=f"{set_by}: set the current plan"):
        ensure_gitignore(root)                # current.json is local state, never committed
        atomic_write_json(pointer_path(root), pointer.model_dump(mode="json"))
    return pointer


def no_current_plan(project_dir: str | Path) -> str | None:
    """With no pointer and no 0.2 file but saved plans: that no plan is current and how to
    choose one (nothing is adopted silently). None otherwise."""
    if pointer_path(project_dir).exists() or legacy_plan_file(project_dir).exists():
        return None
    names = saved_plans(project_dir)
    if not names:
        return None
    newest = names[-1]
    return (f"no current plan; {len(names)} saved plan(s), newest {newest}: choose one with "
            f"`pymolt apply . --plan {newest}`, or make a new one with `pymolt plan .`")


# ── storing ───────────────────────────────────────────────────────────────────────────────


def _redraw(plan: MigrationPlan) -> None:
    """A new id for a plan whose folder is taken: it is unbound (no run or approval names it
    until it is stored), so only its name and a fresh lineage's root follow."""
    old = plan.plan_id
    plan.plan_id = uuid.uuid4().hex
    plan.name = None
    _stamp_name(plan)
    if plan.lineage is not None and plan.lineage.root == old:
        plan.lineage = plan.lineage.model_copy(update={"root": plan.plan_id})
    plan._stored = None


def store(project_dir: str | Path, plan: MigrationPlan) -> Path:
    """Write ``plan`` (``model_dump``) into a folder created for it, ``plans/<name>/plan.json``,
    and return the file. The same plan stored already is that file. A folder that holds
    anything else (another plan, or this plan's id with other content: a stored plan is never
    rewritten) makes the plan draw a new id, ``REDRAWS`` times, then ``MigrationPlanError``:
    the plan is not bound to anything until it is stored. A plan with no lineage gets a fresh
    one. The caller holds the state lock."""
    from pymolt.migration_plan import PlanLineage

    root = Path(project_dir).resolve()
    if plan.lineage is None:                  # no lineage given: a fresh one
        plan.lineage = PlanLineage(root=plan.plan_id)
    for attempt in range(REDRAWS + 1):
        folder = plan_folder(root, _stamp_name(plan))
        data = plan.model_dump(mode="json")
        folder.parent.mkdir(parents=True, exist_ok=True)
        try:
            folder.mkdir()
        except FileExistsError:
            try:
                existing = read_state_json(folder / PLAN_FILE, kind=PLAN_KIND,
                                           newest=PLAN_SCHEMA_VERSION, remedy=PLAN_REMEDY)
            except StateFileError:
                existing = {}                 # a damaged plan: another plan's folder
            if existing is None:
                break                         # an empty folder a crash left: this name's
            if _canonical(existing) in (_canonical(data), _canonical(plan.stored_form())):
                return folder / PLAN_FILE     # this very plan is stored there already
            # Another plan's folder, or this plan's id with other content (a copy changed in
            # memory): a stored plan is never rewritten, so this one is a new plan.
            if attempt == REDRAWS:
                raise MigrationPlanError(
                    f"could not store the plan: its folder under .pymolt/plans/ was taken "
                    f"{REDRAWS + 1} times", hint="Run `pymolt plan .` again.")
            _redraw(plan)                     # another plan's folder: this one takes a new id
            continue
        break
    atomic_write_json(folder / PLAN_FILE, data)
    plan._stored = None                       # what is stored is the plan as it is now
    return folder / PLAN_FILE


def _put_stored_form(root: Path, name: str, data: dict, *, what: str) -> Path:
    """Copy a plan's stored form into ``plans/<name>/plan.json``: nothing when an equal file is
    there; ``MigrationPlanError`` when a different one is (both are kept)."""
    path = stored_plan_file(root, name)
    existing = read_state_json(path, kind=PLAN_KIND, newest=PLAN_SCHEMA_VERSION,
                               remedy=PLAN_REMEDY)
    if existing is not None:
        if _canonical(existing) != _canonical(data):
            raise MigrationPlanError(
                f"{what} and the saved plan {name} ({display_path(path)}) are different "
                "plans under one name",
                hint="Both are kept. Move one of them into .pymolt/archive/, then run the "
                     "command again.")
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, data)
    return path


def export(project_dir: str | Path, plan: MigrationPlan, output: str | Path) -> Path:
    """``plan -o PATH``: the plan written only to ``PATH`` (as 0.2 did); nothing is stored and
    the current plan stays. ``apply --plan PATH`` imports it into the store (``adopt``)."""
    from pymolt.migration_plan import plan_path

    root = Path(project_dir).resolve()
    target = plan_path(root, output)
    if in_store(root, target):
        raise MigrationPlanError(
            f"-o cannot write into the plan store: {display_path(target)}",
            hint="Leave out -o to store the plan, or write it outside .pymolt/plans/.")
    _stamp_name(plan)
    return atomic_write_json(target, plan.model_dump(mode="json"))


def stamp_provenance(project_dir: str | Path, plan: MigrationPlan) -> MigrationPlan:
    """What ``plan`` records about where a plan was made, when it is not set: the pymolt
    version, the git source (``adapters.git``, the local repository only) and its name; then
    its content digest."""
    import pymolt
    from pymolt.adapters.git import git_facts

    if plan.pymolt_version is None:
        plan.pymolt_version = pymolt.__version__
    if plan.source is None:
        plan.source = PlanSource(**dataclasses.asdict(git_facts(project_dir)))
    _stamp_name(plan)
    plan.content_digest = content_digest_of(plan)
    return plan


def save_planned(project_dir: str | Path, plan: MigrationPlan, *,
                 output: str | Path | None = None) -> PlanSaveResult:
    """Save the plan ``pymolt plan`` made. With ``output`` (``-o``) it is only written there
    (``export``). Otherwise, under the state lock: the 0.2 plan file is moved into the store
    (``migrate_legacy``), and the plan is stored and made current (``MigrationPlan.save``).
    Then the blobs retention no longer keeps are dropped (``collect_after_plan``: it waits when
    another operation holds the migration lock)."""
    from pymolt.workorder.blobs import collect_after_plan

    root = Path(project_dir).resolve()
    stamp_provenance(root, plan)
    if output is not None:
        path = export(root, plan, output)
        collect_after_plan(root)
        return PlanSaveResult(plan=plan, path=path, saved=False)
    with state_lock(root, operation="plan"):
        moved = migrate_legacy(root)
        path = plan.save(root)
    collect_after_plan(root)
    return PlanSaveResult(plan=plan, path=path, saved=True, moved_legacy=moved)


# ── a 0.2 project ─────────────────────────────────────────────────────────────────────────


def migrate_legacy(project_dir: str | Path) -> str | None:
    """Move the 0.2 layout's plan file into the store; returns its name (None: there was none).

    1. its stored form goes to ``plans/<name>/plan.json``: skipped when an equal file is there,
       refused (``MigrationPlanError``, both kept) when a different one is;
    2. it becomes the current plan when there is no pointer;
    3. ``.pymolt/PLAN.md``, when it begins ``# Migration plan <id8>`` of that plan, is rendered
       again into its folder, and removed;
    4. the 0.2 file is removed.

    Each step is safe to repeat, so a crash at any of them leaves a state the next ``plan``
    finishes (PLAN.md goes before the 0.2 file, which is what the next ``plan`` looks for)."""
    root = Path(project_dir).resolve()
    legacy = legacy_plan_file(root)
    with state_lock(root, operation="plan: move the 0.2 plan into .pymolt/plans/"):
        plan = MigrationPlan.load(root, legacy)  # a damaged or newer file refuses the plan
        if plan is None:
            return None
        name = _stamp_name(plan)
        _put_stored_form(root, name, plan.stored_form(),
                         what=f"the 0.2 plan file {display_path(legacy)}")
        if load_pointer(root) is None:
            write_pointer(root, plan, set_by="plan")
        _move_plan_md(root, plan)
        legacy.unlink()
    logger.info("moved the 0.2 plan file into .pymolt/plans/%s", name)
    return name


def _move_plan_md(root: Path, plan: MigrationPlan) -> None:
    """0.2's ``.pymolt/PLAN.md`` of ``plan``, rendered again into the plan's folder."""
    old = root / LEGACY_PLAN_MD
    try:
        head = old.read_text(encoding="utf-8").split("\n", 1)[0].rstrip()
    except (OSError, UnicodeDecodeError):
        return
    if head != f"# Migration plan {plan.plan_id[:8]}":
        return
    from pymolt.workorder.render import write_plan_md

    write_plan_md(plan, root, plan_folder(root, _stamp_name(plan)) / PLAN_MD_FILE)
    old.unlink()


# ── adoption ──────────────────────────────────────────────────────────────────────────────


def is_current(project_dir: str | Path, plan: MigrationPlan) -> bool:
    """Whether ``plan`` is the current plan: the pointer's, or with no pointer the 0.2 file's.
    A damaged pointer or current plan raises ``StateFileError``."""
    pointer, path = current_pointer(project_dir)
    if pointer is not None:
        check_plan_id(project_dir, pointer, (read_state_json(
            path, kind=PLAN_KIND, newest=PLAN_SCHEMA_VERSION, remedy=PLAN_REMEDY) or {}
        ).get("plan_id"))
        return pointer.plan_id == plan.plan_id
    data = read_state_json(path, kind=PLAN_KIND, newest=PLAN_SCHEMA_VERSION, remedy=PLAN_REMEDY)
    return data is not None and data.get("plan_id") == plan.plan_id


def adopt(project_dir: str | Path, plan: MigrationPlan) -> bool:
    """Before a mutating ``apply`` of ``plan`` writes (inside the migration lock): make it the
    current plan when it is not. A plan file outside the store is first copied in, by its
    stored form, under its name. Returns whether the pointer moved. Raises ``StepRefusal``
    (exit 2) when another plan is stored under its name, ``StateFileError`` for a damaged
    pointer or current plan."""
    from pymolt.workorder.apply import StepRefusal

    root = Path(project_dir).resolve()
    with state_lock(root, operation="apply: set the current plan"):
        if is_current(root, plan):
            return False
        name = _stamp_name(plan)
        try:
            _put_stored_form(root, name, plan.stored_form(), what="this plan file")
        except MigrationPlanError as exc:
            raise StepRefusal(
                f"another plan is saved as {name}: this plan cannot become the current plan",
                hint=exc.hint) from exc
        write_pointer(root, plan, set_by="apply")
    return True


# ── references ────────────────────────────────────────────────────────────────────────────


def _forms() -> str:
    return "; ".join(REF_FORMS)


def resolve_plan_ref(project_dir: str | Path, ref: str) -> Path:
    """The plan file ``apply --plan REF`` names, trying in order:

    1. ``current``: the current plan's file;
    2. a path (it holds ``/`` or ``\\``, or ends ``.json``), as given (relative to the project);
    3. a plan name whose folder is in the store;
    4. a plan id prefix (4 to 32 lowercase hex), matched against the id8 of the stored plans'
       names, and the 0.2 file while it exists (beyond 8 digits, the candidates' ids are read).

    Anything else, no match and several matches raise ``PlanRefError`` (exit 2)."""
    from pymolt.migration_plan import plan_path

    root = Path(project_dir).resolve()
    ref = (ref or "").strip()
    if ref == "current":
        return plan_path(root)
    if "/" in ref or "\\" in ref or ref.endswith(".json"):
        path = plan_path(root, ref)
        if not path.is_file():
            raise PlanRefError(f"no plan file at {ref}",
                               hint="Give an existing plan file, a plan name or an id prefix.")
        return path
    if NAME_RE.match(ref):
        path = stored_plan_file(root, ref)
        if path.is_file():
            return path
        raise PlanRefError(f"no saved plan named {ref}", hint=_saved_hint(root))
    if _HEX_PREFIX.match(ref):
        return _by_prefix(root, ref)
    raise PlanRefError(f"not a plan reference: {ref!r}",
                       hint=f"A plan reference is {_forms()}.")


def _saved_hint(root: Path) -> str:
    names = saved_plans(root)
    if not names:
        return "No plan is saved: make one with `pymolt plan .`."
    shown = ", ".join(names[-5:])
    return f"Saved plans ({len(names)}, newest last): {shown}."


def _legacy_name(data: dict) -> str | None:
    """The name the 0.2 file's plan reads with (``migration_plan.name_of``), from its JSON."""
    plan_id, label = str(data.get("plan_id") or ""), data.get("label")
    try:
        return plan_name(str(data.get("created_at")), plan_id, label)
    except MigrationPlanError:
        return None
    except ValueError:
        return f"{UNKNOWN_TIME}-{plan_id[:8]}" + (f"-{label}" if label else "")


def _by_prefix(root: Path, prefix: str) -> Path:
    found: dict[str, Path] = {}                 # the plan as shown -> its file
    for name in saved_plans(root):
        id8 = NAME_RE.match(name).group(2)
        if not (id8.startswith(prefix) if len(prefix) <= 8 else id8 == prefix[:8]):
            continue
        path = stored_plan_file(root, name)
        if len(prefix) > 8:
            data = read_state_json(path, kind=PLAN_KIND, newest=PLAN_SCHEMA_VERSION,
                                   remedy=PLAN_REMEDY) or {}
            if not str(data.get("plan_id") or "").startswith(prefix):
                continue
        found[name] = path
    legacy = legacy_plan_file(root)
    data = read_state_json(legacy, kind=PLAN_KIND, newest=PLAN_SCHEMA_VERSION,
                           remedy=PLAN_REMEDY) if legacy.is_file() else None
    # The same plan in the store and in the 0.2 file (a move a crash interrupted): the stored
    # one.
    if data is not None and str(data.get("plan_id") or "").startswith(prefix) \
            and _legacy_name(data) not in found:
        found[f"the 0.2 plan file {display_path(legacy)}"] = legacy
    if not found:
        raise PlanRefError(f"no saved plan with id {prefix}", hint=_saved_hint(root))
    if len(found) > 1:
        raise PlanRefError(f"{prefix} matches {len(found)} plans: {', '.join(sorted(found))}; "
                           "give more digits or a name")
    return next(iter(found.values()))


__all__ = [
    "LEGACY_PLAN_MD", "PLANS_DIR", "PLAN_FILE", "PLAN_MD_FILE", "POINTER_FILE", "POINTER_KIND",
    "POINTER_REMEDY", "PlanPointer", "PlanRefError", "PlanSaveResult", "adopt", "check_current",
    "current_pointer", "export", "in_store", "is_current", "is_stored_plan_file",
    "legacy_plan_file", "load_pointer", "migrate_legacy", "no_current_plan", "plan_folder",
    "plans_dir", "pointer_path", "resolve_plan_ref", "save_planned", "saved_plans",
    "stamp_provenance", "store", "stored_plan_file", "write_pointer",
]
