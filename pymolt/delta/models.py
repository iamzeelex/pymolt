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

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_serializer

from pymolt.adapters.python_runner import RunnerSpec

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


_FACT_ADDITIONS = ("basis", "removed_in", "deprecated_in", "warns_since", "origin")


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
    # Where each side's answer came from: "probe" | "knowledge" | "import-statement"
    # ({"old": ..., "new": ...}); None = both probed (the dependency default).
    basis: dict[str, str] | None = None
    # Python-version lifetimes (subject "python"): the release that removed / deprecated it,
    # and the first release whose stdlib warns when it is used.
    removed_in: str | None = None
    deprecated_in: str | None = None
    warns_since: str | None = None
    # "stdlib" | "site-packages:<dist>" (a third-party shim, e.g. setuptools' distutils).
    origin: str | None = None

    @model_serializer(mode="wrap")
    def _omit_unset_additions(self, handler: Any) -> dict[str, Any]:
        # The pymolt.delta/1 additions are optional: a dependency fact without them dumps
        # exactly as before, so readers and golden files predating them are unaffected.
        data = handler(self)
        for key in _FACT_ADDITIONS:
            if data.get(key) is None:
                data.pop(key, None)
        return data

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
    # "dependency" (a package's API) or "python" (the stdlib between interpreter versions).
    subject: str = "dependency"
    # How each side was observed: {"old": "probe:<interpreter or runner>" |
    # "knowledge:<file>@<date>", "new": ...}.
    sides: dict[str, str] = Field(default_factory=dict)
    # Modules that raised on import on a side (never reported as REMOVED):
    # [{"module", "side", "error", "origin", "cause"}].
    import_errors: list[dict[str, Any]] = Field(default_factory=list)
    # Baseline types whose public spelling now names another class, same-object evidence from
    # both interpreters: [{"old": "pydantic.error_wrappers.ValidationError",
    # "new": "pydantic_core._pydantic_core.ValidationError", "via": "pydantic.ValidationError"}].
    type_relocations: list[dict[str, str]] = Field(default_factory=list)

    @model_serializer(mode="wrap")
    def _omit_default_additions(self, handler: Any) -> dict[str, Any]:
        data = handler(self)  # same rule as DeltaFact: absent additions dump as before
        if data.get("subject") == "dependency":
            data.pop("subject", None)
        for key in ("sides", "import_errors", "type_relocations"):
            if not data.get(key):
                data.pop(key, None)
        return data

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
    # Set when the interpreter lives in a container: ``executable`` is then a path inside it.
    runner: RunnerSpec | None = None
    # Old side only: wanted packages the baseline interpreter does not have at all. They are
    # absent in the old version (their symbols are new), not a reason to build another env.
    absent: list[str] = Field(default_factory=list)

    # source "static": no interpreter was reachable, so the old side is read from the release's
    # wheel or sdist (``delta/static_side.py``). ``static`` maps each package name to its
    # acquired source (``delta.acquire.Acquired``), ``static_errors`` to why one could not be.
    static: dict[str, Any] = Field(default_factory=dict, exclude=True)
    static_errors: dict[str, str] = Field(default_factory=dict, exclude=True)

    @property
    def available(self) -> bool:
        return self.executable is not None

    @property
    def is_static(self) -> bool:
        return self.executable is None and self.source == "static"
