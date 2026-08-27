"""
axiom_graph/models.py

Data models for the Axiom Graph delta engine.
All models are Pydantic v2 dataclasses for clean serialization.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ApiState(str, Enum):
    """Lifecycle state of a public API symbol across versions."""

    ACTIVE = "active"
    DEPRECATED = "deprecated"  # warn present, symbol still in public API
    REMOVED = "removed"  # symbol disappeared from public API
    MOVED = "moved"  # symbol relocated (rename / refactor)


class ChangeRisk(str, Enum):
    """Risk tier for a breaking change — drives migration strategy."""

    MECHANICAL = "mechanical"
    """Rename / move / kind-swap — high-confidence automated codemod."""

    BEHAVIORAL = "behavioral"
    """Default / type / required changed — fixable but must be test-verified."""

    STRUCTURAL = "structural"
    """Object or base class removed — may require architectural redesign."""


# ---------------------------------------------------------------------------
# Call-graph models
# ---------------------------------------------------------------------------


class CallEdge(BaseModel):
    """A directed edge in a call graph: caller → callee (dotted namespaces)."""

    caller: str
    callee: str

    def __hash__(self) -> int:
        return hash((self.caller, self.callee))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CallEdge):
            return NotImplemented
        return self.caller == other.caller and self.callee == other.callee


class CallGraphSnapshot(BaseModel):
    """Static call graph for one version of a package."""

    version: str
    package: str
    nodes: list[str] = Field(default_factory=list)
    """All public callable namespaces discovered by PyCG."""
    edges: list[CallEdge] = Field(default_factory=list)
    """Directed call edges between namespaces."""

    def node_set(self) -> set[str]:
        return set(self.nodes)

    def edge_set(self) -> set[tuple[str, str]]:
        return {(e.caller, e.callee) for e in self.edges}


class CallGraphDiff(BaseModel):
    """Delta between two call-graph snapshots."""

    removed_nodes: list[str] = Field(default_factory=list)
    """Nodes present in old snapshot but absent in new — strong removal signal."""
    added_nodes: list[str] = Field(default_factory=list)
    removed_edges: list[CallEdge] = Field(default_factory=list)
    """Broken call relationships — public interface decomposition signal."""
    added_edges: list[CallEdge] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Breaking-change models
# ---------------------------------------------------------------------------


class BreakingChange(BaseModel):
    """A single breaking API change, enriched with all available signals."""

    kind: str
    """Raw griffe BreakageKind value (e.g. 'object-removed')."""

    path: str
    """Fully-qualified dotted path of the affected symbol (e.g. 'pandas.DataFrame.append')."""

    risk: ChangeRisk
    """Migration risk tier."""

    explanation: str
    """Human-readable one-line explanation from griffe."""

    state: ApiState = ApiState.REMOVED
    """Final lifecycle state of this symbol."""

    # -- Temporal tracking (populated by state-machine accumulation) ---------
    deprecated_since: str | None = None
    """Version string in which the deprecation warning first appeared."""

    removed_in: str | None = None
    """Version string in which the symbol was removed from the public API."""

    # -- Enrichment signals --------------------------------------------------
    deprecation_hint: str | None = None
    """Message text from @deprecated decorator or warnings.warn call."""

    call_graph_confirmed: bool = False
    """True when call graph independently confirmed node disappearance."""

    location: str | None = None
    """Relative file path where the breakage originated (from griffe)."""

    test_examples: list[str] = Field(default_factory=list)
    """Relevant diff hunks from the library's own test suite showing migration."""

    migration_patterns: list[dict] = Field(default_factory=list)
    """Generalized structural migration patterns extracted from test suite diffs."""


# ---------------------------------------------------------------------------
# Codemod pattern (the actionable output — what the API returns)
# ---------------------------------------------------------------------------


class CodemodPattern(BaseModel):
    """
    A proposed source replacement: old_qualname → new_qualname. This is the
    serializable, user-code-free unit the Axiom Graph API emits; applying it to
    a user's source happens locally (pymolt), never on the server.
    """

    old_qualname: str
    new_qualname: str
    kind: str
    """'rewrite-import' (module moved) | 'rename-call' (symbol renamed)."""
    confidence: str
    """'high' | 'medium' | 'low'."""
    evidence: list[str] = Field(default_factory=list)
    """Why we believe this mapping (data-flow / prose / chain path)."""


# ---------------------------------------------------------------------------
# Codemod rule (compound migration template — the richer sibling of
# CodemodPattern, for fixes that are not a simple rename)
# ---------------------------------------------------------------------------


class CodemodRule(BaseModel):
    """
    A compound migration rule: a metavariable match/rewrite template plus
    everything a client needs to apply it safely and verify it locally. Like
    CodemodPattern, this is pure data — the server never executes a rule.
    pymolt's apply_rule is the sole executor, and pymolt's local verify_rule
    is the sole confidence authority; `confidence` here is only a claim.
    """

    library: str
    from_version: str
    to_version: str

    match: str
    """A pattern-1 metavariable expression, e.g. '$DF.lookup($ROWS, $COLS)'."""
    rewrite: list[str]
    """Ordered replacement statements. May reference metavars bound by
    `match` plus one unbound result metavar (canonically $RESULT) the client
    binds from the enclosing single-target `X = <call>` assignment."""
    condition: str = "always"
    """Name of a static predicate (client CONDITION_CATALOG) that must hold
    at a call site for the rewrite to apply, e.g. 'simple_name_args'."""
    runtime_precondition: str | None = None
    """Name of a human-readable runtime assumption (client
    RUNTIME_PRECONDITIONS) the rewrite depends on but can't check statically,
    e.g. 'unique_index_and_columns'. Never gates application — the client
    applies the rewrite and flags the site instead."""
    confidence: str = "heuristic"
    """'verified' (a golden pair round-trips locally) | 'heuristic' (an
    unverified claim — the client re-derives this, never trusts it as-is)."""

    test_before: str | None = None
    """Golden-pair source before the rewrite, for local verify_rule."""
    test_after: str | None = None
    """Golden-pair source after the rewrite (must match apply_rule's output)."""
    doc_link: str | None = None
    """Optional URL to the upstream migration guide / changelog entry."""

    result_var: str | None = None
    """Explicit override for the unbound rewrite metavar the client binds
    from the assignment target. None → inferred as the sole unbound metavar."""

    kind: str | None = None
    """Set only for Tier-1 projections: 'rename-call' | 'rewrite-import'.
    None (or 'template') marks a template rule with no legacy-visitor
    delegation — match/rewrite is the sole executable form."""
    old_qualname: str | None = None
    new_qualname: str | None = None
    """Dotted qualnames carried through from the originating CodemodPattern,
    so a `kind`-tagged rule can delegate execution to the existing
    binding-aware visitors (apply_pattern) instead of the template."""
    evidence: list[str] = Field(default_factory=list)
    """Why we believe this rule — mined test-diff hunks / data-flow / prose,
    same provenance role as CodemodPattern.evidence."""


# ---------------------------------------------------------------------------
# Pairwise delta (one step A → B in the release chain)
# ---------------------------------------------------------------------------


class PairwiseDelta(BaseModel):
    """API delta for a single adjacent version pair."""

    package: str
    from_version: str
    to_version: str
    changes: list[BreakingChange] = Field(default_factory=list)
    transitions: list[dict] = Field(default_factory=list)
    """High-confidence name-match moves: [{"from_path": ..., "to_path": ..., "confidence": ...}]"""
    codemods: list[CodemodPattern] = Field(default_factory=list)
    """Per-step codemod proposals for symbols changed in this version pair."""


# ---------------------------------------------------------------------------
# Full delta (accumulated across the entire release chain)
# ---------------------------------------------------------------------------


class FullDelta(BaseModel):
    """
    Complete chronological API delta between two arbitrary versions.

    Built by accumulating PairwiseDeltas across the full release chain,
    then collapsing per-symbol state into the Active→Deprecated→Removed
    state machine. Each BreakingChange carries temporal context (deprecated_since,
    removed_in) that a flat two-point diff would lose.
    """

    package: str
    from_version: str
    to_version: str

    release_chain: list[str] = Field(default_factory=list)
    """All intermediate versions traversed, inclusive of to_version."""

    changes: list[BreakingChange] = Field(default_factory=list)
    """De-duplicated, temporally-enriched final list of breaking changes."""

    codemods: list[CodemodPattern] = Field(default_factory=list)
    """Transitively-resolved codemod patterns across the whole chain (A→C)."""

    rules: list[CodemodRule] = Field(default_factory=list)
    """Curated catalog hits plus Tier-1 projections of `codemods`, in the
    richer CodemodRule wire shape. Additive: empty on a delta with no matches."""

    total_breaking: int = 0
    summary_by_risk: dict[str, int] = Field(default_factory=dict)
    """Counts per ChangeRisk tier: {"structural": N, "behavioral": M, "mechanical": K}"""

    # -- Skip handling -------------------------------------------------------
    skipped: bool = False
    skip_reason: str | None = None

    def model_post_init(self, __context) -> None:  # noqa: ANN001
        if not self.skipped:
            self.total_breaking = len(self.changes)
            from collections import Counter

            counts = Counter(c.risk.value for c in self.changes)
            self.summary_by_risk = dict(counts)
