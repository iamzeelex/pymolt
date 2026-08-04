"""
pymolt/codemods/models.py

Local models for the codemod loop. CodemodPattern mirrors the Axiom Graph API's
Tier-1 response unit (flat old → new); CodemodRule (pymolt.codemods.rules,
imported here) is the Tier-2 declarative successor — a metavariable match/
rewrite template with its own honest advisory channel. FileChange /
CodemodRunResult describe what was rewritten on disk; FilePreview is the
reviewable before/after unit the TUI renders.

`rules.py` only imports this module LAZILY (inside a function, for its LEGACY
delegation), so importing CodemodRule/RuleAdvisory here at module level is safe
— no circular import.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, Field

from pymolt.codemods.rules import CodemodRule, RuleAdvisory


class CodemodPattern(BaseModel):
    """A symbol replacement received from Axiom Graph: old → new."""

    old_qualname: str
    new_qualname: str
    kind: str
    """'rewrite-import' (module moved) | 'rename-call' (symbol renamed)."""
    confidence: str = "medium"
    evidence: list[str] = Field(default_factory=list)

    def summary(self) -> str:
        return f"[{self.kind}] {self.old_qualname} → {self.new_qualname}  ({self.confidence})"


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
    downgraded: list[str] = Field(default_factory=list)
    """Short summaries of rules the client downgraded: the server claimed
    `verified` but local re-verification (the sole verification authority)
    failed — see `client.AxiomGraphClient.fetch_bundle`."""


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
    downgraded: list[str] = Field(default_factory=list)
    """Summaries of rules the client downgraded during fetch (threaded up from
    CodemodBundle.downgraded, merged across packages) so interfaces can show
    them."""

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
