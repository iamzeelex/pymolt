"""
pymolt/codemods/models.py

Local models for the codemod loop. DependencyMigration is one dependency to move
(versions only); CodemodPattern is the Tier-1 unit (flat old → new); CodemodRule
(pymolt.codemods.rules, imported here) is the Tier-2 declarative successor — a metavariable match/
rewrite template with its own honest advisory channel. FileChange /
CodemodRunResult describe what was rewritten on disk; FilePreview is the
reviewable before/after unit the interface renders.

`rules.py` only imports this module LAZILY (inside a function, for its LEGACY
delegation), so importing CodemodRule/RuleAdvisory here at module level is safe
— no circular import.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from pymolt.codemods.rules import CodemodRule, RuleAdvisory


class DependencyMigration:
    """One dependency to move: its name and the from and to versions."""

    def __init__(self, name: str, from_version: str, to_version: str) -> None:
        self.name = name
        self.from_version = from_version
        self.to_version = to_version


class CodemodPattern(BaseModel):
    """A symbol replacement the delta engine derived: old → new."""

    old_qualname: str
    new_qualname: str
    kind: str
    """'rewrite-import' (module moved) | 'rename-call' (symbol renamed) | 'rewrite-attr' (a
    module segment inserted into an attribute chain) | 'replace-attr-path' (a cross-module
    rename such as ``imp.reload`` → ``importlib.reload``) | 'rewrite-module'."""
    confidence: str = "medium"
    evidence: list[str] = Field(default_factory=list)
    requires_import: str | None = None
    """A module the rewritten code needs imported, e.g. ``collections.abc``."""
    scope: str = "dependency"
    """``"dependency"`` (a package's API) or ``"python"`` (a stdlib fix for the Python move)."""
    runs_on_base: bool | None = None
    """Whether the rewritten code also runs on the base Python (None: not known/applicable)."""
    # Successor kinds (``rename-keyword``, ``rename-member``, ``forward-template``: made only from
    # a proof in the library's own code, ``pymolt.codemods.successors``):
    params: dict[str, str] | None = None
    """``rename-keyword``: old keyword -> new keyword."""
    receivers: list[str] | None = None
    """The receiver classes the baseline recorded at the sites (a member's rewrite is gated on
    them)."""
    template: dict[str, Any] | None = None
    """What the rewrite needs from its proof: the old and new signatures, the forward's
    arguments (``forward-template``), the member name, the other changed parameters."""
    sites: list[Any] | None = None
    """The statements the pattern may rewrite, as anchors (``pymolt.anchors.Anchor``), re-resolved
    on the text it runs on; None: wherever its binding holds."""
    aliases: list[str] = Field(default_factory=list)
    """Other spellings of the old callee (the binding check accepts any of them)."""

    def summary(self) -> str:
        return f"[{self.kind}] {self.old_qualname} → {self.new_qualname}  ({self.confidence})"


class ApiImpact(BaseModel):
    """A changed dependency API path the delta engine found.

    The package name is supplied by the surrounding ``CodemodBundle`` map and
    is added only when impacts are frozen into a migration plan.
    """

    path: str
    replacement_path: str | None = None
    kind: str
    state: str
    risk: str
    explanation: str
    transition_confidence: str | None = None
    transition_reason: str | None = None
    param: str | None = None
    """The parameter a parameter-level change is about (``replacement_path`` is then the
    parameter that replaces it)."""


class CodemodBundle(BaseModel):
    """Everything the server returned for one package: Tier-1 patterns (legacy,
    unchanged) and Tier-2 rules (new). Migration policy (client.py/service.py):
    when `rules` is non-empty, pymolt applies RULES ONLY for that package — the
    server's Tier-1 projections arrive as kind-tagged rules, so applying both
    lists would double-apply. Empty/absent `rules` (old server) falls back to
    the patterns path, unchanged.
    """

    patterns: list[CodemodPattern] = Field(default_factory=list)
    rules: list[CodemodRule] = Field(default_factory=list)
    impacts: list[ApiImpact] = Field(default_factory=list)
    """Changed public API paths used to scope local ContactMap verification."""
    downgraded: list[str] = Field(default_factory=list)
    """Short summaries of rules the provider downgraded: their golden pair did not
    verify, or the environment lacks what they need — see
    `pymolt.delta.provider.LocalDeltaProvider.fetch_bundle`."""


class FileChange(BaseModel):
    """A single file the applier touched (or would touch in dry-run)."""

    path: str
    sites: int
    """How many pattern/rule applications changed this file."""
    patterns: list[str] = Field(default_factory=list)
    """Summaries of the patterns/rules that hit this file."""
    advisories: list[RuleAdvisory] = Field(default_factory=list)
    """Rule advisories raised while producing this change (see RuleAdvisory)."""


class CodemodRunResult(BaseModel):
    """Outcome of applying a set of patterns/rules across a repository."""

    root: str
    dry_run: bool
    files_scanned: int = 0
    changes: list[FileChange] = Field(default_factory=list)
    patterns_applied: int = 0
    advisories_by_file: dict[str, list[RuleAdvisory]] = Field(default_factory=dict)
    """Every file that raised >=1 advisory, INCLUDING files with zero rewrites
    (e.g. a file whose sole deprecated call is `return df.lookup(...)`: 0
    sites, 1 advisory) — an advisory-only file must surface here, not vanish."""

    @property
    def files_changed(self) -> int:
        return len(self.changes)


def recipe_key(item: CodemodPattern | CodemodRule) -> str:
    """A stable identity for one pattern/rule, shared by every interface.

    Patterns and rules describe the same thing in two vocabularies, so a live
    progress view (and anything else grouping by "which recipe fired") needs
    one key that works for both.
    """
    kind = getattr(item, "kind", None) or "template"
    old = getattr(item, "old_qualname", None)
    new = getattr(item, "new_qualname", None)
    params = getattr(item, "params", None)
    if params:                         # rename-keyword: one recipe per renamed keyword set
        kind += "[" + ",".join(f"{a}={b}" for a, b in sorted(params.items())) + "]"
    if old and new:
        return f"{kind}:{old}→{new}"
    match = getattr(item, "match", "") or ""
    rewrite = getattr(item, "rewrite", None) or [""]
    return f"{kind}:{match}→{rewrite[0]}"


@dataclass(frozen=True)
class PreviewProgress:
    """One file's outcome during a preview walk.

    Emitted for *every* file scanned — a file that matched nothing reports
    ``sites == 0`` and no recipes — so a progress view can show the true
    denominator rather than only the files that happened to change.
    """

    path: str
    sites: int = 0
    recipes: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return self.sites > 0


#: Observer invoked once per scanned file by the preview walkers.
ProgressSink = Callable[[PreviewProgress], None] | None


@dataclass(frozen=True)
class MoveGroup:
    """The import-move patterns ``apply_patterns`` applies together: those that share an old and
    a new module (``MoveNamesCodemod``), so ``from OLD import a, b`` becomes one statement per
    target module. ``hits``: ``(recipe_key, sites)`` of each member that changed something."""

    old_module: str
    new_module: str
    members: tuple[CodemodPattern, ...]
    hits: tuple[tuple[str, int], ...] = ()


#: Observer of one recipe's change to one text: ``(recipe, before, after)``. The recipe is a
#: ``CodemodRule``, a ``CodemodPattern`` or a ``MoveGroup``; it is called each time one of them
#: changes the text, in application order, and changes nothing the caller returns.
StepObserver = Callable[[Any, str, str], None] | None
#: The same with the file: ``(recipe, path, before, after)`` (the plan's step builder).
FileStepObserver = Callable[[Any, str, str, str], None] | None


class BlockedRewrite(BaseModel):
    """A rule that matched in a file but was not adopted: it is not verified, and only verified
    rules are applied automatically. ``lines``: the 1-based lines its rewrite would change."""

    rule: CodemodRule
    sites: int
    lines: list[int] = Field(default_factory=list)


class FilePreview(BaseModel):
    """Before/after for one file — the unit the engineer reviews and decides on."""

    path: str
    old_source: str
    new_source: str
    sites: int
    patterns: list[CodemodPattern] = Field(default_factory=list)
    """The patterns that changed this file (for the card's info header)."""
    rules: list[CodemodRule] = Field(default_factory=list)
    """The rules that changed OR advised on this file (for the card's info
    header)."""
    advisories: list[RuleAdvisory] = Field(default_factory=list)
    """Advisories raised on this file. A file with advisories but no rewrite
    (old_source == new_source, sites == 0) still yields a preview — the
    manual-review card."""
    blocked: list[BlockedRewrite] = Field(default_factory=list)
    """Rules that matched here but were not adopted (``verified_only``): they changed
    nothing, and the plan lists them for a person. Their keys are not among the recipes
    that changed the file (``applied_recipes``)."""

    def applied_recipes(self) -> list[CodemodPattern | CodemodRule]:
        """The patterns and rules that changed this file or advised on it: ``rules`` and
        ``patterns`` without the blocked ones."""
        blocked = {recipe_key(b.rule) for b in self.blocked}
        return [item for item in [*self.rules, *self.patterns]
                if recipe_key(item) not in blocked]
