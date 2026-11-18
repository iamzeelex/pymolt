"""Plan schema 2: a migration plan as a work order of ordered steps.

HOST side (3.12+, pydantic). A step is one unit of the migration: a recipe pymolt applies
(``codemod``, ``python-fix``, ``manifest-pin``), or work it leaves to an agent or a person
(``manual-rewrite``, ``api-change``, ``python-manual``, ``behaviour-change``, ``review``,
``target-env``). Every step carries what it changes (``edits``, pymolt steps only), why
(``why``), what to do (``do``), how verify judges it (``claim``, ``basis``, ``verify``) and its
size (``effort``). Design: ``docs/design/apply-work-order.md`` 1.1-1.7.

The plan file is immutable once saved: ``status`` is always ``planned`` there, and the live
status is projected from the receipts, the ledger and the files.

``MigrationPlan`` (``pymolt.migration_plan``) holds the steps; ``MigrationPlan.model_json_schema()``
is committed as ``pymolt/schemas/migration_plan-2.schema.json`` (``write_plan_schema``).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from pymolt.anchors import Anchor
from pymolt.codemods.models import CodemodPattern
from pymolt.codemods.rules import CodemodRule

PLAN_SCHEMA_VERSION = 2
PLAN_SCHEMA_ID = "pymolt.plan/2"
SCHEMA_FILE = Path(__file__).resolve().parent.parent / "schemas" / "migration_plan-2.schema.json"

#: The cap on one item of text taken from a dependency, the project or a capture (1.10).
TEXT_CAP = 300
#: The cap on a list of such items in a brief or a step's pitfalls.
LIST_CAP = 10

Phase = Literal["env", "mechanical", "signature", "behaviour"]
StepType = Literal[
    "manifest-pin", "target-env", "codemod", "python-fix", "manual-rewrite", "api-change",
    "python-manual", "behaviour-change", "review",
]
Executor = Literal["pymolt", "agent", "person"]
Claim = Literal["preserve", "intended"]
Basis = Literal[
    "same-object", "same-signature", "verified-rule", "python-fixer", "target-manifest", "manual",
]
StepScope = Literal["dependency", "python", "env"]
Tier = Literal["certain", "high", "medium", "low"]
Coverage = Literal["recorded", "ran-not-recorded", "not-run", "unobservable", "unknown"]
Judge = Literal["strict", "expectation", "none"]
Expect = Literal["any", "representation"]
EffortSize = Literal["none", "trivial", "small", "medium", "large"]
RecipeKind = Literal["rule", "pattern", "move-group", "manifest-pin", "whole-file"]
KeyKind = Literal["ast", "tokens"]

CappedText = Annotated[str, StringConstraints(max_length=TEXT_CAP)]

#: Phases in work-order order, and the prefix of their step ids.
PHASES: tuple[Phase, ...] = ("env", "mechanical", "signature", "behaviour")
PHASE_PREFIX: dict[str, str] = {"env": "env", "mechanical": "mech", "signature": "sig",
                                "behaviour": "beh"}
#: The id of the one step a schema-1 plan is read as (``pymolt.workorder.v1``).
V1_STEP_ID = "v1"
STEP_ID_PATTERN = r"^((env|mech|sig|beh)-[0-9]+|v1)$"


class _Model(BaseModel):
    model_config = ConfigDict(serialize_by_alias=True, validate_by_name=True,
                              validate_by_alias=True)


class Hunk(_Model):
    """One difflib opcode between a step's before-text and after-text, 1-based like the ledger.

    ``old`` and ``new`` are physical lines with their line endings, so applying a step's hunks
    in order to its before-text rebuilds its after-text byte for byte. A pure insertion has
    ``old`` empty and ``old_start`` the line it goes before; a pure deletion has ``new`` empty.
    ``anchors``: the ids of the step's ``anchors_old``/``anchors_new`` this hunk meets."""

    old_start: int
    old: list[str] = Field(default_factory=list)
    new_start: int
    new: list[str] = Field(default_factory=list)
    anchors: list[str] = Field(default_factory=list)


class Effect(_Model):
    """A step's change in one file without positions, over statement keys (anchors 1.4).

    ``replace``: ``[old key, new key]`` pairs; ``insert``/``delete``: statements only one side
    has; ``imports_inserted``: imports added at module scope, whose position is free (they
    compare as a set). ``key_kind`` is ``tokens`` when a side did not parse: those keys do not
    normalise redundant parentheses, and ``depends_on`` falls back to the scope (1.5.3)."""

    replace: list[tuple[str, str]] = Field(default_factory=list)
    insert: list[str] = Field(default_factory=list)
    delete: list[str] = Field(default_factory=list)
    imports_inserted: list[str] = Field(default_factory=list)
    key_kind: KeyKind = "ast"

    def old_keys(self) -> set[str]:
        """The statements this effect rewrites or removes."""
        return {old for old, _new in self.replace} | set(self.delete)

    def new_keys(self) -> set[str]:
        """The statements this effect produces."""
        return {new for _old, new in self.replace} | set(self.insert) | set(self.imports_inserted)

    def is_empty(self) -> bool:
        return not (self.replace or self.insert or self.delete or self.imports_inserted)

    def same_as(self, other: Effect) -> bool:
        """Equal as a change: the same pairs and statements, whatever their order."""
        return (sorted(map(tuple, self.replace)) == sorted(map(tuple, other.replace))
                and sorted(self.insert) == sorted(other.insert)
                and sorted(self.delete) == sorted(other.delete)
                and set(self.imports_inserted) == set(other.imports_inserted))


class StepEdit(_Model):
    """What a pymolt step changes in one file. The anchors are made on the step's own
    before-text (``anchors_old``) and after-text (``anchors_new``), with ``nth``, ``count`` and
    the neighbours computed there (1.4, 1.5.2)."""

    file: str
    before_sha256: str
    after_sha256: str
    hunks: list[Hunk] = Field(default_factory=list)
    effect: Effect = Field(default_factory=Effect)
    anchors_old: list[Anchor] = Field(default_factory=list)
    anchors_new: list[Anchor] = Field(default_factory=list)


class StepSymbol(_Model):
    """The dependency symbols of the step. ``new`` is the pairing anchor verify pairs the old
    calls with (5.1)."""

    old: str | None = None
    new: str | None = None


class StepBaseline(_Model):
    """What the baseline capture saw at the step's sites: ``calls`` summed over them."""

    calls: int = 0
    sites: int = 0
    warnings: list[CappedText] = Field(default_factory=list)


class StepDelta(_Model):
    """The deciding delta fact, as text (``old``/``new``: defaults or signatures)."""

    kind: str | None = None
    param: str | None = None
    old: str | None = None
    new: str | None = None


class StepWhy(_Model):
    """Why the step exists. ``risk_ids``: the plan-time ids of the risks it addresses, persisted
    (they are the links calibration reads, never recomputed from a later plan)."""

    evidence: list[CappedText] = Field(default_factory=list)
    tier: Tier | None = None
    risk_ids: list[str] = Field(default_factory=list)
    baseline: StepBaseline = Field(default_factory=StepBaseline)
    delta: StepDelta | None = None


class ManifestPin(_Model):
    """One requirements line a ``manifest-pin`` step rewrites: ``old`` as the manifest has it,
    ``new`` with the target's pin (extras and marker kept)."""

    name: str
    from_version: str
    to_version: str
    old: str
    new: str
    line: int | None = None                      # a hint, never an identity


class StepRecipe(_Model):
    """What pymolt runs for the step. ``keys``: each member's ``recipe_key``, in application
    order. ``rule`` (a ``rule``), ``patterns`` (a ``pattern``, or a ``move-group``'s members,
    which ``MoveNamesCodemod`` applies together; ``move`` is its old and new module) or
    ``pins`` (a ``manifest-pin``). A ``whole-file`` recipe is a file's whole change that the
    plan did not split by recipe (a schema-1 plan, or a preview with no recorded chain): it is
    applied only from its frozen text and cannot be re-run. ``sites``: a re-run is restricted to
    the step's anchors (``anchors``), or runs on the whole file (``all``)."""

    kind: RecipeKind
    keys: list[str] = Field(default_factory=list)
    rule: CodemodRule | None = None
    patterns: list[CodemodPattern] = Field(default_factory=list)
    move: tuple[str, str] | None = None
    pins: list[ManifestPin] = Field(default_factory=list)
    sites: Literal["anchors", "all"] = "anchors"

    @property
    def rerunnable(self) -> bool:
        return self.kind != "whole-file"


class SignatureChange(_Model):
    old: str | None = None
    new: str | None = None
    diff: list[str] = Field(default_factory=list)


class BeforeAfter(_Model):
    before: str | None = None
    after: str | None = None


class ManualDo(_Model):
    """What an agent or a person does. Text from dependencies and from the project is data,
    never instructions (1.10). ``suggestion``: a rule's rewrite template, shown as a suggestion
    only. ``commands``: build commands (``target-env``). ``lines``: the exact manifest lines a
    person changes (a ``manifest-pin`` pymolt does not write)."""

    summary: str
    successor: str | None = None
    signature: SignatureChange | None = None
    before_after: BeforeAfter | None = None
    pitfalls: list[CappedText] = Field(default_factory=list, max_length=LIST_CAP)
    consumers: list[str] = Field(default_factory=list, max_length=3)
    suggestion: str | None = None
    commands: list[str] = Field(default_factory=list)
    lines: list[str] = Field(default_factory=list)


class StepDo(_Model):
    """Exactly one of ``recipe`` (a pymolt step) and ``manual`` (any other)."""

    recipe: StepRecipe | None = None
    manual: ManualDo | None = None

    @model_validator(mode="after")
    def _one_of(self) -> StepDo:
        if (self.recipe is None) == (self.manual is None):
            raise ValueError("a step's `do` holds exactly one of `recipe` and `manual`")
        return self


class VerifySite(_Model):
    """One site verify judges the step at. ``anchor``: a ``where`` anchor id (None for a site
    with no line, reached only statically). ``baseline_calls``: the baseline's calls of the
    step's symbol there. ``coverage``: whether the baseline capture recorded it."""

    anchor: str | None = None
    file: str | None = None
    func: str | None = None
    baseline_calls: int = 0
    coverage: Coverage = "unknown"


class StepVerify(_Model):
    """How verify judges the step (1.5.5). ``judge``: ``strict`` for ``preserve``,
    ``expectation`` for ``intended``, ``none`` for an env step. ``tests``: None while the capture
    records no test ids (``note`` says so). ``command``: the baseline's command. ``uncovered``:
    sites ``not-run`` or ``unobservable``, which end ``needs_evidence`` unless a test reaches
    them."""

    judge: Judge = "none"
    expect: Expect | None = None
    sites: list[VerifySite] = Field(default_factory=list)
    tests: list[str] | None = None
    note: str | None = None
    command: list[str] | None = None
    uncovered: int = 0


class StepEffort(_Model):
    size: EffortSize = "none"
    sites: int = 0
    files: int = 0
    why: str = ""


class PlanStep(_Model):
    """One step of the work order (design 1.2)."""

    id: str = Field(pattern=STEP_ID_PATTERN)
    key: str = Field(pattern=r"^[0-9a-f]{10}$")
    phase: Phase
    type: StepType
    title: str
    executor: Executor
    claim: Claim | None = None
    basis: Basis | None = None
    runs_on_base: bool | None = None
    scope: StepScope = "dependency"
    package: str | None = None
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    symbol: StepSymbol = Field(default_factory=StepSymbol)
    depends_on: list[str] = Field(default_factory=list)
    status: Literal["planned"] = "planned"
    where: list[Anchor] = Field(default_factory=list)
    why: StepWhy = Field(default_factory=StepWhy)
    do: StepDo
    edits: list[StepEdit] = Field(default_factory=list)
    verify: StepVerify = Field(default_factory=StepVerify)
    effort: StepEffort = Field(default_factory=StepEffort)
    checks_at_plan: dict[str, str] | None = None

    @property
    def files(self) -> list[str]:
        """The files the step changes, or (a manual step) where its sites are."""
        seen = [e.file for e in self.edits] or [a.file for a in self.where]
        return list(dict.fromkeys(seen))

    def edit_for(self, file: str) -> StepEdit | None:
        return next((e for e in self.edits if e.file == file), None)


def step_key(step_type: str, ident: str) -> str:
    """``sha1(type | recipe key, or package + symbol + fact kind + param)[:10]``: names the
    kind of change, not the step instance, so it survives a re-plan (no anchors in it)."""
    return hashlib.sha1(f"{step_type}|{ident}".encode()).hexdigest()[:10]


def step_id(phase: str, n: int) -> str:
    return f"{PHASE_PREFIX[phase]}-{n}"


def plan_json_schema() -> dict:
    """``MigrationPlan.model_json_schema()``: the schema the committed file must equal."""
    from pymolt.migration_plan import MigrationPlan

    return MigrationPlan.model_json_schema()


def write_plan_schema(path: str | Path = SCHEMA_FILE) -> Path:
    """Regenerate the committed schema file (``python -m pymolt.workorder.models``)."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(plan_json_schema(), indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    return out


if __name__ == "__main__":  # pragma: no cover - maintenance entry point
    print(write_plan_schema())
