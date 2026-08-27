"""
axiom_graph/propose.py

The codemod proposal engine — where the analyzers converge into an actionable
list of CodemodProposals, and where NetworkX does its job: the in-memory graph
layer that resolves a symbol's *transitive* replacement across a release chain.

LibCST and NetworkX sit on different layers, not in competition:
  - LibCST  : the syntax layer — read/fingerprint/rewrite source.
  - NetworkX: the graph layer  — assemble per-step replacements into a directed
              evolution graph and answer "where did symbol X ultimately go?"
              (A→B in v1→v2 and B→C in v2→v3 collapse to A→C).

Per-function signal fusion for one deprecated symbol:
  - value_flow.detect_delegation  → the call the body forwards to   (high)
  - comment_markers               → the 'use X instead' prose hint  (medium)
A proposal is emitted when at least one names a successor; agreement raises
confidence.

Pure and offline (the analyzers need only source text).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import networkx as nx

from axiom_graph.analyzers.comment_markers import extract_from_source
from axiom_graph.analyzers.fingerprint import classify_change, fingerprint_source
from axiom_graph.analyzers.value_flow import detect_delegation
from dataclasses import replace

from axiom_graph.core.codemods import CodemodProposal
from axiom_graph.core.models import CodemodPattern, CodemodRule
from axiom_graph.core.policy import (
    DEFAULT_POLICY,
    RECURRENCE_DEMOTE_THRESHOLD,
    ProposalPolicy,
)

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Proposal ↔ serializable pattern conversion
# ─────────────────────────────────────────────────────────────────────────────


def to_pattern(proposal: CodemodProposal) -> CodemodPattern:
    """Convert a (dataclass) CodemodProposal into the pydantic CodemodPattern."""
    return CodemodPattern(
        old_qualname=proposal.old_qualname,
        new_qualname=proposal.new_qualname,
        kind=proposal.kind,
        confidence=proposal.confidence,
        evidence=list(proposal.evidence),
    )


def to_proposal(pattern: CodemodPattern) -> CodemodProposal:
    """Convert a CodemodPattern back into a CodemodProposal (kind re-derived)."""
    return CodemodProposal(
        old_qualname=pattern.old_qualname,
        new_qualname=pattern.new_qualname,
        confidence=pattern.confidence,
        evidence=list(pattern.evidence),
    )


def transitive_patterns(patterns: list[CodemodPattern]) -> list[CodemodPattern]:
    """
    Collapse per-step CodemodPatterns into end-to-end ones across the chain
    (A→B in one step + B→C in another → A→C), via the NetworkX evolution graph.
    """
    evo = build_evolution([to_proposal(p) for p in patterns])
    return [to_pattern(p) for p in evo.transitive_proposals()]


def _rule_leaf(qualname: str) -> str:
    return qualname.rsplit(".", 1)[-1]


def _rule_module(qualname: str) -> str:
    return qualname.rsplit(".", 1)[0] if "." in qualname else ""


def rule_from_pattern(
    pattern: CodemodPattern,
    *,
    library: str,
    from_version: str,
    to_version: str,
    examples: list[str] | None = None,
) -> CodemodRule:
    """
    Project a Tier-1 CodemodPattern into the CodemodRule wire shape.

    kind/old_qualname/new_qualname are carried through so the client's LEGACY
    branch can execute this rule via the existing binding-aware visitors
    (apply_pattern) rather than the match/rewrite template — match/rewrite
    are DISPLAY-ONLY here, a leaf-name stand-in for what those visitors do.

    A golden test_before/test_after pair is synthesized only where the shape
    is mechanically unambiguous:
      - rewrite-import: the moved import line + a call to the (unchanged) leaf.
      - rename-call, when BOTH qualnames are dotted (module path + leaf): the
        same shape, with the call's name changing too.
    A method rename (`Service.old_method` → bare `do_new`, see
    propose_from_function's self./cls. stripping) has no module to import
    from, so it gets no pair — fabricating one would invent a receiver that
    was never observed.

    confidence carries over from the pattern unchanged: a projection makes no
    independent claim beyond what Layer 2 already established.
    """
    old_leaf = _rule_leaf(pattern.old_qualname)
    new_leaf = _rule_leaf(pattern.new_qualname)
    old_module = _rule_module(pattern.old_qualname)
    new_module = _rule_module(pattern.new_qualname)
    test_before: str | None = None
    test_after: str | None = None

    if pattern.kind == "rewrite-import":
        match = f"from {old_module} import {old_leaf}"
        rewrite = [f"from {new_module} import {new_leaf}"]
        if old_module and new_module:
            test_before = f"from {old_module} import {old_leaf}\nr = {old_leaf}()"
            test_after = f"from {new_module} import {new_leaf}\nr = {new_leaf}()"
    else:  # "rename-call"
        match = f"$X.{old_leaf}(...)"
        rewrite = [f"$X.{new_leaf}(...)"]
        if old_module and new_module:
            test_before = f"from {old_module} import {old_leaf}\nr = {old_leaf}()"
            test_after = f"from {new_module} import {new_leaf}\nr = {new_leaf}()"

    return CodemodRule(
        library=library,
        from_version=from_version,
        to_version=to_version,
        match=match,
        rewrite=rewrite,
        confidence=pattern.confidence,
        test_before=test_before,
        test_after=test_after,
        kind=pattern.kind,
        old_qualname=pattern.old_qualname,
        new_qualname=pattern.new_qualname,
        evidence=list(examples or []),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Per-function proposal
# ─────────────────────────────────────────────────────────────────────────────


def _qualify(target: str, old_qualname: str) -> str:
    """
    If `target` is a bare leaf and we know the old symbol's module, leave it
    bare (we can't invent a module); if it's already dotted, keep it. The
    codemod kind (rename-call vs rewrite-import) is derived from the leaves.
    """
    return target


def propose_from_function(
    source: str,
    qualname: str,
    in_module_symbol: str | None = None,
    policy: ProposalPolicy = DEFAULT_POLICY,
) -> CodemodProposal | None:
    """
    Produce a codemod proposal for a single deprecated/removed function, given
    the source of the version where it still exists (so its body + prose are
    available) and its dotted qualname.

    `in_module_symbol` is the path used to LOCATE the function inside `source`
    (e.g. "Service.old_method" for a class method) — class-aware, so name
    collisions across classes don't pick the wrong body. Defaults to the leaf
    of `qualname` (correct for module-level functions).

    `policy` (see core.policy) gates which signals count and which targets are
    real: prose-source trust, dispatcher rejection, and no-op suppression.

    Returns None if no successor can be identified.
    """
    locator = in_module_symbol or qualname.rsplit(".", 1)[-1]

    flow = detect_delegation(source, locator)
    marker = extract_from_source(source, locator)

    flow_target = _strip_receiver(flow.primary_callee())
    # Prose is only trusted from authoritative sources (deprecation warnings by
    # default); docstring/comment prose is too noisy to name a successor.
    marker_target = (
        _strip_receiver(marker.target)
        if (marker and policy.accepts_prose(marker.source))
        else None
    )

    # A private callee (`self._append`, `_json.loads`) is an internal impl
    # detail, never an actionable public codemod. Crucially this is decided
    # DURING selection, not after: when the data flow forwards to a private
    # wrapper but the deprecation prose names a public successor
    # (`DataFrame.append → self._append` in the body, but the FutureWarning says
    # "use pandas.concat"), we must fall back to the prose — not drop the codemod.
    flow_ok = bool(flow_target) and not _is_private(flow_target)
    marker_ok = bool(marker_target) and not _is_private(marker_target)

    evidence: list[str] = []
    target: str | None = None
    confidence = "low"

    if flow_ok and marker_ok:
        if flow_target.rsplit(".", 1)[-1] == marker_target.rsplit(".", 1)[-1]:
            # Strongest case: data flow and prose agree (compare on the leaf,
            # since prose may give a different module spelling).
            target = flow_target  # prefer the data-flow spelling (real callee)
            confidence = "high"
            evidence.append(f"data-flow delegation → {flow_target} ({flow.delegations[0].via})")
            evidence.append(f"prose marker ({marker.source}) → {marker_target}")
        else:
            # Disagreement, both public: we're guessing which is the real
            # successor (often the prose is right and the flow is an internal
            # step). Keep the data-flow spelling but mark it LOW — not a
            # confident codemod.
            target = flow_target
            confidence = "low"
            evidence.append(f"data-flow delegation → {flow_target}")
            evidence.append(f"prose marker disagrees → {marker_target}")
    elif flow_ok:
        target = flow_target
        confidence = "high"
        evidence.append(f"data-flow delegation → {flow_target} ({flow.delegations[0].via})")
    elif marker_ok:
        # Either no data flow, or it forwarded to a private wrapper — the prose
        # names the real public successor.
        target = marker_target
        confidence = "medium"
        if flow_target:  # there was a flow, but it was private
            evidence.append(f"data-flow → {flow_target} (private wrapper)")
        evidence.append(f"prose marker ({marker.source}) → {marker_target}")

    if target is None:
        return None

    # The body may forward through a generic dispatcher (`self.operate(...)`) or
    # to a same-named module function (`DataFrame.merge → merge`): neither is a
    # real, actionable successor. Both are gated by the policy.
    if not policy.accepts_target(qualname, target):
        log.debug("%s → %s: dispatcher/no-op target, not a codemod", qualname, target)
        return None

    # Confidence floor: drop the `low` baseline tier (a guessed target from a
    # data-flow/prose disagreement) unless the policy opts back in.
    if not policy.accepts_confidence(confidence):
        log.debug("%s → %s: below confidence floor (%s)", qualname, target, confidence)
        return None

    return CodemodProposal(
        old_qualname=qualname,
        new_qualname=_qualify(target, qualname),
        confidence=confidence,
        evidence=evidence,
    )


def _strip_receiver(target: str | None) -> str | None:
    """
    `self.do_new` / `cls.do_new` is an instance/class-relative call; for an
    external caller the actionable pattern is the bare method (`do_new`).
    """
    if target and target.startswith(("self.", "cls.")):
        return target.split(".", 1)[1]
    return target


def _is_private(qualname: str) -> bool:
    """True if any component is private (leading underscore, not dunder)."""
    return any(
        part.startswith("_") and not part.startswith("__")
        for part in qualname.split(".")
    )


def is_codemod_candidate(qualname: str) -> bool:
    """
    Layer-1.5 filter: is this changed symbol worth running Layer 2 on?

    Griffe's structural diff flags *every* changed public-ish symbol, including
    masses that can never yield an actionable codemod for a user — test modules
    (`pkg.tests.*`, `conftest`, `test_*`) and private/internal namespaces
    (`pkg._libs.*`, `pkg._testing.*`; C-extension symbols have no Python source
    to analyze). Filtering them here — before source resolution and CST parsing
    — is the dominant speedup on large libraries (pandas, numpy) and keeps the
    log free of benign "could not find function" noise. By product definition a
    codemod targets the *public* API the user imports, so dropping private-source
    symbols loses nothing actionable.
    """
    for seg in qualname.split("."):
        if seg in ("tests", "conftest") or seg.startswith("test_"):
            return False
    return not _is_private(qualname)


def resolve_function_source(
    pkg_src_dir: Path, package: str, qualname: str
) -> tuple[str, str] | None:
    """
    Locate the module file for a dotted `qualname` under a package source dir
    and return (module_source, in_module_symbol).

    `pkg_src_dir` is the directory holding the package's __init__.py (what
    acquisition.find_package_source_dir returns). The longest dotted prefix that
    maps to a .py file (or package __init__.py) is the module; the REMAINDER is
    the in-module symbol path (e.g. "safe_join" for a function, "Flask.run" for
    a method) — passed on so the function is located class-aware. Returns None
    if no module file is found.
    """
    parts = qualname.split(".")
    # Drop the leading package component(s) so paths are package-relative.
    pkg_leaf = package.replace("-", "_").split(".")[-1]
    if parts and parts[0] == pkg_leaf:
        parts = parts[1:]
    if not parts:
        return None

    # Longest module prefix wins: helpers.safe_join → helpers.py + "safe_join";
    # json.dumps → json/__init__.py + "dumps"; app.Flask.run → app.py + "Flask.run".
    for k in range(len(parts) - 1, 0, -1):
        mod_parts, symbol_parts = parts[:k], parts[k:]
        symbol = ".".join(symbol_parts)
        module_file = pkg_src_dir.joinpath(*mod_parts).with_suffix(".py")
        if module_file.exists():
            return module_file.read_text(encoding="utf-8"), symbol
        init_file = pkg_src_dir.joinpath(*mod_parts) / "__init__.py"
        if init_file.exists():
            return init_file.read_text(encoding="utf-8"), symbol

    # Symbol lives directly in the package __init__.
    init_file = pkg_src_dir / "__init__.py"
    if init_file.exists():
        return init_file.read_text(encoding="utf-8"), ".".join(parts)
    return None


def propose_codemods_for_changes(
    pkg_src_dir: Path,
    package: str,
    qualnames: list[str],
    policy: ProposalPolicy = DEFAULT_POLICY,
) -> list[CodemodProposal]:
    """
    Propose codemods for a set of changed/removed symbols, resolving each one's
    source from the (old-version) package directory. Symbols whose source can't
    be located or that name no successor are skipped.

    `policy` (see core.policy) controls precision: the public-API source gate
    plus the prose/dispatcher/no-op gates inside propose_from_function, then a
    batch-level recurrence demotion (a bare target fanned out across many
    unrelated symbols is a prose artifact) and the confidence floor.
    """
    proposals: list[CodemodProposal] = []
    seen: set[str] = set()
    for qualname in qualnames:
        if qualname in seen:
            continue
        seen.add(qualname)
        if not is_codemod_candidate(qualname):
            continue  # test module / private namespace → never a user codemod
        if not policy.accepts_source(qualname):
            continue  # library-internal symbol → not user-facing (tunable)
        resolved = resolve_function_source(pkg_src_dir, package, qualname)
        if resolved is None:
            continue
        module_source, in_module_symbol = resolved
        try:
            proposal = propose_from_function(
                module_source, qualname, in_module_symbol=in_module_symbol, policy=policy
            )
        except Exception as exc:  # malformed source / parse error → skip
            log.debug("codemod proposal skipped for %s: %s", qualname, exc)
            continue
        if proposal is not None:
            proposals.append(proposal)

    return _demote_recurrent_targets(proposals, policy)


def _demote_recurrent_targets(
    proposals: list[CodemodProposal], policy: ProposalPolicy
) -> list[CodemodProposal]:
    """
    A *bare* successor named for many unrelated symbols (`count`, `mad`, … all →
    "groupby") is almost always a prose-extraction artifact, not a real rename.
    Demote those to `low` and re-apply the confidence floor.

    Careful: only **non-high** proposals are demoted. A common leaf can be both a
    real rename for one symbol and an artifact for others — e.g. `take` is the
    genuine successor of `take_nd` (high, data-flow direct) while also appearing
    as an internal-delegation coincidence elsewhere. High-confidence proposals
    (data flow and prose agree, or a direct public delegation) are trusted and
    left alone. Dotted targets (`pandas.concat`) are never counted as recurrent.
    """
    from collections import Counter

    bare_counts: Counter = Counter()
    for p in proposals:
        if "." not in p.new_qualname:
            bare_counts[p.new_qualname] += 1

    kept: list[CodemodProposal] = []
    for p in proposals:
        confidence = p.confidence
        if (
            confidence != "high"
            and bare_counts.get(p.new_qualname, 0) >= RECURRENCE_DEMOTE_THRESHOLD
        ):
            confidence = "low"
        if not policy.accepts_confidence(confidence):
            log.debug("recurrent/low target dropped: %s → %s", p.old_qualname, p.new_qualname)
            continue
        kept.append(p if confidence == p.confidence else replace(p, confidence=confidence))
    return kept


def propose_for_pair(
    old_source: str,
    new_source: str | None,
    qualname: str,
) -> CodemodProposal | None:
    """
    Propose a codemod for a symbol across one version step.

    `new_source` (the next version's module source, or None if the symbol's
    module is gone) lets us skip no-op churn: if the function still exists and
    only its formatting/comments changed, there's nothing to migrate.
    """
    func_name = qualname.rsplit(".", 1)[-1]

    if new_source is not None:
        try:
            old_fp = fingerprint_source(old_source, func_name)
            new_fp = fingerprint_source(new_source, func_name)
        except ValueError:
            new_fp = None  # removed in new version → definitely propose
        else:
            diff = classify_change(old_fp, new_fp)
            if diff.classification in ("formatting-only", "comment-only"):
                log.info("%s: %s — no migration needed", qualname, diff.classification)
                return None

    return propose_from_function(old_source, qualname)


# ─────────────────────────────────────────────────────────────────────────────
# NetworkX evolution graph — transitive resolution across the chain
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ResolvedPath:
    source: str
    final: str
    path: list[str]
    confidence: str


class SymbolEvolution:
    """
    Directed graph of replacement edges (old_qualname → new_qualname) gathered
    across a whole release chain. Collapses multi-hop renames into a single
    end-to-end codemod.
    """

    def __init__(self) -> None:
        self.g = nx.DiGraph()

    def add_proposal(self, proposal: CodemodProposal) -> None:
        self.g.add_edge(
            proposal.old_qualname,
            proposal.new_qualname,
            confidence=proposal.confidence,
            evidence=list(proposal.evidence),
        )

    def resolve_final(self, symbol: str) -> ResolvedPath:
        """Follow replacement edges to the terminal successor (a sink)."""
        path = [symbol]
        seen = {symbol}
        confidences = []
        cur = symbol
        while self.g.out_degree(cur) > 0:
            successor = next(iter(self.g.successors(cur)))
            confidences.append(self.g.edges[cur, successor]["confidence"])
            if successor in seen:  # cycle guard
                break
            seen.add(successor)
            path.append(successor)
            cur = successor
        return ResolvedPath(
            source=symbol,
            final=cur,
            path=path,
            confidence=_weakest(confidences) if confidences else "high",
        )

    def transitive_proposals(self) -> list[CodemodProposal]:
        """
        One end-to-end CodemodProposal per chain origin: each symbol that is
        replaced but is not itself a replacement target (a graph source).
        """
        origins = [
            n for n in self.g
            if self.g.in_degree(n) == 0 and self.g.out_degree(n) > 0
        ]
        out: list[CodemodProposal] = []
        for origin in sorted(origins):
            resolved = self.resolve_final(origin)
            evidence = [f"chain: {' → '.join(resolved.path)}"]
            for a, b in zip(resolved.path, resolved.path[1:]):
                evidence.extend(self.g.edges[a, b]["evidence"])
            out.append(
                CodemodProposal(
                    old_qualname=resolved.source,
                    new_qualname=resolved.final,
                    confidence=resolved.confidence,
                    evidence=evidence,
                )
            )
        return out


_CONF_ORDER = {"low": 0, "medium": 1, "high": 2}


def _weakest(confidences: list[str]) -> str:
    """A chain is only as confident as its weakest link."""
    return min(confidences, key=lambda c: _CONF_ORDER.get(c, 0))


def build_evolution(proposals: list[CodemodProposal]) -> SymbolEvolution:
    """Assemble per-step proposals into a transitive evolution graph."""
    evo = SymbolEvolution()
    for p in proposals:
        evo.add_proposal(p)
    return evo


# ─────────────────────────────────────────────────────────────────────────────
# Store bridge — load the evolution graph from any GraphStore for queries
# ─────────────────────────────────────────────────────────────────────────────


def evolution_from_store(store) -> SymbolEvolution:
    """
    Rebuild the in-memory NetworkX evolution graph from a persisted GraphStore
    (JSON today, Neo4j later) so transitive resolution can run over stored data.
    """
    from axiom_graph.storage.graph_store import read_patterns

    return build_evolution([to_proposal(p) for p in read_patterns(store)])
