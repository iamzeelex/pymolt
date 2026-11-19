"""Curated, versioned knowledge shipped with pymolt (``pymolt/knowledge/*.json``).

Data, not code: each file carries ``schema`` (``pymolt.knowledge/1``), a ``version``
date, and entries that each name their ``source`` and ``date``. The loader validates
the shape and answers narrow questions:

- :func:`codemod_rules` — structural rewrites (e.g. pandas ``append`` → ``pd.concat``)
  for a migration window, as :class:`~pymolt.codemods.rules.CodemodRule` objects the
  existing rules engine runs;
- :func:`successor_hint` — the curated successor of a removed symbol;
- :func:`succession_edges` — framework-succession edges;
- :func:`warning_switches` — env switches that make an old version warn about the new one;
- :func:`probe_contexts` — setup snippets the API probe runs so proxies resolve;
- :func:`stdlib_changes` / :func:`stdlib_lifetimes` — curated stdlib successors and the
  generated module/name lifetimes (read through :mod:`pymolt.core.stdlib`).

Nothing here touches the network or the project.
"""

from __future__ import annotations

import hashlib
import json
from functools import cache
from pathlib import Path
from typing import Any, Literal

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, Field

KNOWLEDGE_DIR = Path(__file__).resolve().parent
SCHEMA = "pymolt.knowledge/1"
_FILES = (
    "codemod_rules",
    "succession",
    "warning_switches",
    "probe_contexts",
    "stdlib_changes",
    "stdlib_lifetimes",
    "stdlib_behavior",
    "environment_readers",
)


class KnowledgeError(ValueError):
    """A shipped knowledge file is missing or malformed."""


class _Entry(BaseModel):
    id: str
    source: str
    date: str


class RequiredImport(BaseModel):
    module: str
    as_: str | None = Field(default=None, alias="as")


class CuratedRule(_Entry):
    package: str
    removed_in: str
    symbols: list[str] = Field(default_factory=list)
    successor: str | None = None
    anchors: list[str] = Field(default_factory=list)
    requires_import: RequiredImport | None = None
    rule: dict[str, Any]
    notes: list[str] = Field(default_factory=list)

    def applies(self, from_version: str, to_version: str) -> bool:
        """True when the window crosses the removal: ``from < removed_in <= to``."""
        try:
            removed = Version(self.removed_in)
            return Version(from_version) < removed <= Version(to_version)
        except InvalidVersion:
            return False

    def names(self, symbol: str) -> bool:
        return symbol in self.symbols


class WarningSwitch(_Entry):
    package: str
    old: str
    new: str
    env: dict[str, str] = Field(default_factory=dict)
    warning_filter: str | None = None
    effect: str = ""
    doc_link: str | None = None

    def applies(self, from_version: str, to_version: str) -> bool:
        try:
            return Version(from_version) in SpecifierSet(self.old) and Version(
                to_version
            ) in SpecifierSet(self.new)
        except (InvalidVersion, InvalidSpecifier):
            return False


class ProbeContext(_Entry):
    package: str
    roots: list[str] = Field(default_factory=list)
    code: str
    effect: str = ""


@cache
def _load_raw(name: str) -> dict[str, Any]:
    path = KNOWLEDGE_DIR / f"{name}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise KnowledgeError(f"cannot read knowledge file {path.name}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise KnowledgeError(f"{path.name}: expected schema {SCHEMA!r}")
    return data


@cache
def _load(name: str) -> dict[str, Any]:
    data = _load_raw(name)
    if not isinstance(data.get("entries"), list):
        raise KnowledgeError(f"{name}.json: 'entries' must be a list")
    return data


@cache
def knowledge_version() -> str:
    """A digest over every knowledge file: changes when any curated entry changes."""
    digest = hashlib.sha256()
    for name in _FILES:
        digest.update((KNOWLEDGE_DIR / f"{name}.json").read_bytes())
    return digest.hexdigest()[:16]


@cache
def _rules() -> tuple[CuratedRule, ...]:
    return tuple(CuratedRule.model_validate(e) for e in _load("codemod_rules")["entries"])


def curated_rules(package: str, from_version: str, to_version: str) -> list[CuratedRule]:
    """Curated rule entries for one migration window (``from < removed_in <= to``)."""
    pkg = canonicalize_name(package)
    return [
        r for r in _rules()
        if canonicalize_name(r.package) == pkg and r.applies(from_version, to_version)
    ]


def codemod_rules(package: str, from_version: str, to_version: str):
    """The curated rules for a window as ``CodemodRule``s (unverified: the caller verifies)."""
    from pymolt.codemods.rules import CodemodRule

    out = []
    for entry in curated_rules(package, from_version, to_version):
        payload = dict(entry.rule)
        payload.update(library=package, from_version=from_version, to_version=to_version)
        evidence = list(payload.get("evidence") or [])
        evidence.append(f"pymolt knowledge {entry.id} ({entry.source}, {entry.date})")
        payload["evidence"] = evidence
        out.append(CodemodRule.model_validate(payload))
    return out


def successor_hint(
    package: str, symbols: list[str], from_version: str, to_version: str
) -> tuple[str, CuratedRule] | None:
    """The curated successor for any of ``symbols`` in this window, with its entry."""
    for entry in curated_rules(package, from_version, to_version):
        if entry.successor and any(entry.names(s) for s in symbols):
            return entry.successor, entry
    return None


def succession_edges(frameworks: list[str]):
    """Curated framework-succession edges for the detected frameworks.

    Matching is case-insensitive on the base package name, so ``tensorflow==1.15``
    still hits ``tensorflow``. De-duplicated, in first-seen order.
    """
    from pymolt.strategic.succession.models import SuccessionEdge

    data = _load("succession")
    index: dict[str, list[str]] = data.get("index") or {}
    by_id = {e["id"]: e for e in data["entries"]}
    seen: set[str] = set()
    out = []
    for fw in frameworks:
        base = str(fw).strip().lower()
        for sep in ("<", ">", "=", "~", "!", "[", ";", " "):
            base = base.split(sep)[0]
        for edge_id in index.get(base, []):
            if edge_id in seen or edge_id not in by_id:
                continue
            seen.add(edge_id)
            entry = by_id[edge_id]
            edge = SuccessionEdge.model_validate(entry["edge"])
            edge.evidence = [*edge.evidence, f"pymolt knowledge {edge_id} ({entry['source']})"]
            out.append(edge)
    return out


@cache
def _switches() -> tuple[WarningSwitch, ...]:
    return tuple(WarningSwitch.model_validate(e) for e in _load("warning_switches")["entries"])


def warning_switches(package: str, from_version: str, to_version: str) -> list[WarningSwitch]:
    """Env switches that make ``package`` at ``from_version`` warn about ``to_version``."""
    pkg = canonicalize_name(package)
    return [
        s for s in _switches()
        if canonicalize_name(s.package) == pkg and s.applies(from_version, to_version)
    ]


class StdlibChangeEntry(_Entry):
    """One curated stdlib change; mirrors :class:`pymolt.core.stdlib.StdlibChange`."""

    symbols: list[str]
    kind: Literal["module-removed", "module-moved", "name-removed", "name-moved", "deprecated"]
    deprecated_in: str | None = None
    warns_since: str | None = None
    removed_in: str | None = None
    successor: str | None = None
    same_object: bool = False
    successor_since: str | None = None
    fix: Literal["rewrite-import", "rewrite-attr", "replace-attr-path", "advisory"] = "advisory"
    hint: str = ""
    shimmed_by: str | None = None


@cache
def stdlib_changes() -> tuple[StdlibChangeEntry, ...]:
    """The curated stdlib changes, validated."""
    return tuple(StdlibChangeEntry.model_validate(e) for e in _load("stdlib_changes")["entries"])


class _Span(BaseModel):
    present: tuple[str, str | None]  # (first, last); last None = still in the newest swept
    since_unknown: bool = False  # sweep-derived: "first" is only the oldest interpreter swept


class _Lifetimes(BaseModel):
    source: str
    date: str
    pythons: list[str]
    modules: dict[str, _Span]
    names: dict[str, _Span]
    submodules: list[str] = Field(default_factory=list)
    stable: dict[str, list[str]] = Field(default_factory=dict)


@cache
def stdlib_lifetimes() -> dict[str, Any]:
    """The generated stdlib lifetimes (``bench/stdlib_sweep``), validated, as a dict."""
    data = _load_raw("stdlib_lifetimes")
    try:
        _Lifetimes.model_validate(data)
    except ValueError as exc:
        raise KnowledgeError(f"stdlib_lifetimes.json: {exc}") from exc
    return data


def _minor_tuple(value: str | None) -> tuple[int, int] | None:
    try:
        release = Version(str(value)).release
    except InvalidVersion:
        return None
    return (release[0], release[1] if len(release) > 1 else 0) if release else None


class StdlibBehaviorEntry(_Entry):
    """One curated Python behaviour change that keeps every name in place
    (``stdlib_behavior.json``): what changed in ``changed_in``, its effect class, and the
    static detector (``pymolt.pyversion.behavior``) that finds affected project code."""

    symbols: list[str]
    changed_in: str
    effect: Literal["dict-order", "hash-order"]
    detector: str | None = None
    detail: str
    old: str = ""
    new: str = ""
    hint: str = ""
    notes: list[str] = Field(default_factory=list)

    def applies(self, base_python: str, target_python: str) -> bool:
        """True when the move crosses the change: ``base < changed_in <= target`` (minors)."""
        base, changed, target = (_minor_tuple(v) for v in (base_python, self.changed_in,
                                                            target_python))
        return None not in (base, changed, target) and base < changed <= target


@cache
def stdlib_behavior() -> tuple[StdlibBehaviorEntry, ...]:
    """The curated Python behaviour changes, validated (unique ids, a parseable
    ``changed_in``)."""
    try:
        entries = tuple(StdlibBehaviorEntry.model_validate(e)
                        for e in _load("stdlib_behavior")["entries"])
    except ValueError as exc:
        raise KnowledgeError(f"stdlib_behavior.json: {exc}") from exc
    seen: set[str] = set()
    for entry in entries:
        if entry.id in seen:
            raise KnowledgeError(f"stdlib_behavior.json: duplicate id {entry.id!r}")
        if _minor_tuple(entry.changed_in) is None:
            raise KnowledgeError(f"stdlib_behavior.json: {entry.id}: changed_in "
                                 f"{entry.changed_in!r} is not a version")
        seen.add(entry.id)
    return entries


class EnvironmentReaderEntry(_Entry):
    """Calls whose outcome depends on an aspect of the runtime environment (verify reviews a
    difference there when that aspect differs between the captures)."""

    aspects: list[str]
    symbols: list[str]                       # fnmatch patterns over the dependency symbol
    readings: dict[str, str] = Field(default_factory=dict)  # symbol -> recorded reading
    when: Literal["port<1024"] | None = None


@cache
def environment_readers() -> tuple[EnvironmentReaderEntry, ...]:
    """The curated environment readers, validated."""
    return tuple(EnvironmentReaderEntry.model_validate(e)
                 for e in _load("environment_readers")["entries"])


@cache
def _contexts() -> tuple[ProbeContext, ...]:
    return tuple(ProbeContext.model_validate(e) for e in _load("probe_contexts")["entries"])


def probe_contexts(roots: set[str] | list[str]) -> list[ProbeContext]:
    """Probe setup snippets whose package roots are among ``roots``."""
    wanted = set(roots)
    return [c for c in _contexts() if wanted & set(c.roots)]


__all__ = [
    "CuratedRule",
    "KnowledgeError",
    "ProbeContext",
    "StdlibChangeEntry",
    "WarningSwitch",
    "codemod_rules",
    "curated_rules",
    "knowledge_version",
    "probe_contexts",
    "stdlib_changes",
    "stdlib_lifetimes",
    "succession_edges",
    "successor_hint",
    "warning_switches",
]
