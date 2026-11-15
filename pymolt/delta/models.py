"""Delta fact schema (``pymolt.delta/1``) and the engine's input/interpreter models.

``DeltaReport.model_dump()`` is the documented schema (docs/design/rework-cli-first-local-delta.md,
section 2)::

    {"schema": "pymolt.delta/1", "package": "pandas", "from": "1.5.3", "to": "2.2.3",
     "old_python": "3.11.13", "new_python": "3.12.11", "quality": "complete|partial|inconclusive",
     "facts": [{"symbol": ..., "aliases": [...], "kind": "DEFAULT", "param": "numeric_only",
                "old": "None", "new": "False", "deprecated_in_old": true, "successor": null,
                "evidence": ["signature", "warning:FutureWarning: ..."]}],
     "notes": [...]}

``notes`` is the one addition: the reasons behind a ``partial`` or ``inconclusive``
quality (an unbuildable environment, a probe timeout, a version mismatch), so a
report never reads as "no changes" when the engine could not look.

Fact kinds:

- ``REMOVED``    the spelling is gone in the new version and the object is not reachable
                 elsewhere;
- ``MOVED``      the spelling is gone but the same object is reachable at ``successor``
                 (an alias dropped, or a defining module/base class re-homed; a base-class
                 move is never a removal). ``old``/``new`` are the old and new canonical names;
- ``SIGNATURE``  a parameter was removed, renamed, added as required or changed kind
                 (``param`` names it), or, with ``param`` null and ``evidence`` "bases", the
                 class's bases changed;
- ``DEFAULT``    a parameter's default value changed (``old``/``new`` are reprs);
- ``DEPRECATED`` the symbol warns: ``deprecated_in_old`` says whether the old version already
                 did; an ``evidence`` entry starting ``deprecated-in-new:`` says the new one does;
- ``UNCHANGED``  checked, and nothing above applies.

``symbol`` is the canonical old name (the spelling a baseline trace records) when the fact is
about the object, and the dropped or rebound spelling when it is about a spelling; ``aliases``
are the other touched spellings that exhibit the fact.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

SCHEMA = "pymolt.delta/1"

FactKind = Literal["REMOVED", "MOVED", "SIGNATURE", "DEFAULT", "DEPRECATED", "UNCHANGED"]
Quality = Literal["complete", "partial", "inconclusive"]
Origin = Literal["contact", "trace", "given"]

KIND_ORDER = {"REMOVED": 0, "MOVED": 1, "SIGNATURE": 2, "DEFAULT": 3, "DEPRECATED": 4,
              "UNCHANGED": 5}


class RecordedWarning(BaseModel):
    """A warning the old version raised at runtime (the baseline capture)."""

    category: str = "Warning"
    message: str
    count: int = 1

    def text(self) -> str:
        return f"{self.category}: {self.message}"


class TouchedSymbol(BaseModel):
    """One symbol the project touches, with where that knowledge came from."""

    path: str
    origin: Origin = "given"
    warnings: list[RecordedWarning] = Field(default_factory=list)


class DeltaFact(BaseModel):
    symbol: str
    aliases: list[str] = Field(default_factory=list)
    kind: FactKind
    param: str | None = None
    old: str | None = None
    new: str | None = None
    deprecated_in_old: bool = False
    successor: str | None = None
    evidence: list[str] = Field(default_factory=list)

    # Engine-side detail the provider uses (not part of the schema, never dumped):
    # importable spellings, whether the successor has the same signature, origins.
    _detail: dict[str, Any] = PrivateAttr(default_factory=dict)

    @property
    def detail(self) -> dict[str, Any]:
        return self._detail

    def spellings(self) -> list[str]:
        return [self.symbol, *self.aliases]


class DeltaReport(BaseModel):
    """API-change facts for one dependency migration, scoped to the touched symbols."""

    model_config = ConfigDict(populate_by_name=True)

    schema_: str = Field(default=SCHEMA, alias="schema")
    package: str
    from_version: str = Field(alias="from")
    to_version: str = Field(alias="to")
    old_python: str | None = None
    new_python: str | None = None
    quality: Quality
    facts: list[DeltaFact] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    # Timings and cache counters of the run (see ``engine.run_stats``); never dumped.
    _stats: dict[str, Any] = PrivateAttr(default_factory=dict)

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:  # the schema spells "from"/"to"
        kwargs.setdefault("by_alias", True)
        return super().model_dump(**kwargs)

    def model_dump_json(self, **kwargs: Any) -> str:
        kwargs.setdefault("by_alias", True)
        return super().model_dump_json(**kwargs)

    def changes(self) -> list[DeltaFact]:
        """Every fact except UNCHANGED."""
        return [f for f in self.facts if f.kind != "UNCHANGED"]


class Interpreter(BaseModel):
    """The interpreter chosen for one side of the delta, or why there is none."""

    executable: str | None = None
    source: str = "none"  # trace-metadata | base-env | target-env | throwaway | none
    python: str | None = None
    note: str | None = None
    reason: str | None = None  # set when executable is None
    built_seconds: float | None = None
    versions: dict[str, str | None] = Field(default_factory=dict)

    @property
    def available(self) -> bool:
        return self.executable is not None
