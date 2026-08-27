"""
axiom_graph/fusion.py

Signal fusion: merges griffe diff + AST hints + test examples into rich
BreakingChange objects, then accumulates a chain of PairwiseDelta into a
final FullDelta via state-machine reduction.

State machine per symbol:
    Active  ──(deprecation warn found)──►  Deprecated
    Active  ──(symbol removed)──────────►  Removed
    Deprecated ──(symbol removed)────────►  Removed  (with context!)
    Any     ──(name-match move detected)──► Moved

The key insight: by accumulating pairwise deltas in chronological order,
we can fill in deprecated_since and removed_in fields that a flat two-point
diff would lose entirely.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from axiom_graph.analyzers.griffe_diff import RawBreakingChange

from axiom_graph.analyzers import extract_patterns
from axiom_graph.core.models import (
    ApiState,
    BreakingChange,
    ChangeRisk,
    CodemodPattern,
    FullDelta,
    PairwiseDelta,
)

log = logging.getLogger(__name__)



# ---------------------------------------------------------------------------
# Pairwise fusion
# ---------------------------------------------------------------------------

def fuse_pairwise(
    raw_changes: list[RawBreakingChange],
    ast_hints: dict[str, str] | None = None,
    test_examples: dict[str, list[str]] | None = None,
    from_version: str = "",
    to_version: str = "",
    package: str = "",
    transitions: list[dict] | None = None,
    codemods: list[CodemodPattern] | None = None,
    cg_diff: CallGraphDiff | None = None,
) -> PairwiseDelta:
    """
    Fuse all signals for one version step into a PairwiseDelta.

    Signal priority for deprecation_hint:
        AST miner (authoritative) > griffe docstring scan (best-effort)

    State assignment for this step:
        - If the path appears in cg_diff.removed_nodes → REMOVED
        - Elif AST hint exists in old version → DEPRECATED (hint appeared in old)
        - Else → REMOVED (griffe said it's gone)

    Note: deprecated_since / removed_in are set by accumulate_chain, not here.
    """
    ast_hints = ast_hints or {}
    test_examples = test_examples or {}
    transitions = transitions or []
    removed_nodes_set = set(cg_diff.removed_nodes) if cg_diff is not None else set()

    # Build a suffix-indexed lookup for test_examples
    # (griffe paths are dotted; api_name keys from test_miner are unqualified)
    def _match_test_examples(path: str) -> list[str]:
        # Try exact match first (unlikely), then suffix match
        leaf = path.split(".")[-1]
        return test_examples.get(leaf, [])

    enriched: list[BreakingChange] = []
    for raw in raw_changes:
        # --- deprecation hint ---
        hint = ast_hints.get(raw.path) or raw.deprecation_hint

        # --- call graph confirmation ---
        cg_confirmed = raw.path in removed_nodes_set if removed_nodes_set else False
        if not cg_confirmed and removed_nodes_set:
            cg_confirmed = any(
                node.endswith(raw.path.split(".")[-1]) or raw.path in node
                for node in removed_nodes_set
            )

        # --- state ---
        if cg_confirmed:
            state = ApiState.REMOVED
        elif hint:
            # Deprecation hint in this version's source = it's still present but warned
            state = ApiState.DEPRECATED
        else:
            state = ApiState.REMOVED

        # --- test examples & migration patterns ---
        examples = _match_test_examples(raw.path)
        patterns = []
        seen_patterns = set()
        api_leaf = raw.path.split(".")[-1]
        for diff in examples:
            pattern = extract_patterns(diff, api_leaf)
            if pattern:
                sig = f"{pattern['before']} -> {pattern['after']}"
                if sig not in seen_patterns:
                    patterns.append(pattern)
                    seen_patterns.add(sig)

        enriched.append(BreakingChange(
            kind=raw.kind,
            path=raw.path,
            risk=raw.risk,
            explanation=raw.explanation,
            state=state,
            deprecation_hint=hint,
            call_graph_confirmed=cg_confirmed,
            location=raw.location,
            test_examples=examples,
            migration_patterns=patterns,
        ))

    return PairwiseDelta(
        package=package,
        from_version=from_version,
        to_version=to_version,
        changes=enriched,
        transitions=transitions,
        codemods=codemods or [],
    )


# ---------------------------------------------------------------------------
# Chain accumulation (state machine)
# ---------------------------------------------------------------------------

def accumulate_chain(
    pairwise_deltas: list[PairwiseDelta],
    package: str,
    from_v: str,
    to_v: str,
    release_chain: list[str],
) -> FullDelta:
    """
    Collapse a sequence of PairwiseDelta into a FullDelta via state-machine
    reduction.

    For each unique symbol path, we track:
        - first_deprecated: the from_version of the step where it appeared
          as DEPRECATED (i.e. the old version still had the warn)
        - removed_in: the to_version of the step where it became REMOVED

    Final state per symbol:
        - REMOVED with both deprecated_since and removed_in → rich context
        - REMOVED with no deprecated_since → sudden removal (or no sdist for intermediate)
        - DEPRECATED still in last version → not yet removed (unusual but valid)
        - MOVED → detected via transitions

    De-duplication: we keep the richest version of each change (most context).
    """
    # Track per-path state across the chain
    # {path: BreakingChange (best accumulated version)}
    by_path: dict[str, BreakingChange] = {}

    # Track deprecation appearances: {path: version_string}
    deprecated_since: dict[str, str] = {}
    removed_in: dict[str, str] = {}

    # Track transitions (moves) across all steps
    all_transitions: list[dict] = []
    moved_paths: set[str] = set()

    # Collect every per-step codemod pattern for transitive resolution.
    all_codemods: list[CodemodPattern] = []

    for step in pairwise_deltas:
        step_from = step.from_version
        step_to = step.to_version

        all_codemods.extend(step.codemods)

        # Collect transitions (moves)
        for t in step.transitions:
            from_path = t.get("from_path", "")
            all_transitions.append(t)
            moved_paths.add(from_path)

        for change in step.changes:
            path = change.path

            # State machine transitions
            if change.state == ApiState.DEPRECATED:
                if path not in deprecated_since:
                    deprecated_since[path] = step_from

            elif change.state == ApiState.REMOVED:
                if path not in removed_in:
                    removed_in[path] = step_to
                if path not in deprecated_since and change.deprecation_hint:
                    # Might have been deprecated before we started tracking
                    deprecated_since[path] = step_from

            # Merge into by_path (take richest version)
            existing = by_path.get(path)
            if existing is None:
                by_path[path] = change
            else:
                # Prefer the version with more information
                merged = _merge_changes(existing, change)
                by_path[path] = merged

    # Apply accumulated temporal context
    final_changes: list[BreakingChange] = []
    for path, change in by_path.items():
        final_state = change.state

        # Apply move detection
        if path in moved_paths:
            final_state = ApiState.MOVED

        final_changes.append(change.model_copy(update={
            "state": final_state,
            "deprecated_since": deprecated_since.get(path),
            "removed_in": removed_in.get(path),
        }))

    # Sort: structural first, then behavioral, then mechanical; alpha within tier
    _tier_ord = {ChangeRisk.STRUCTURAL: 0, ChangeRisk.BEHAVIORAL: 1, ChangeRisk.MECHANICAL: 2}
    final_changes.sort(key=lambda c: (_tier_ord.get(c.risk, 9), c.path))

    # Collapse per-step codemods into end-to-end patterns across the chain
    # (A→B→C ⇒ A→C). Lazy import keeps networkx off the fusion import path.
    from axiom_graph.core.propose import transitive_patterns

    final_codemods = transitive_patterns(all_codemods) if all_codemods else []
    final_codemods.sort(key=lambda c: c.old_qualname)

    return FullDelta(
        package=package,
        from_version=from_v,
        to_version=to_v,
        release_chain=release_chain,
        changes=final_changes,
        codemods=final_codemods,
    )


def _merge_changes(a: BreakingChange, b: BreakingChange) -> BreakingChange:
    """
    Merge two BreakingChange records for the same path.
    Keeps the richer of the two (more fields populated).
    """
    # Prefer REMOVED over DEPRECATED (it's a stronger statement)
    state = b.state if b.state == ApiState.REMOVED else a.state

    # Prefer the version with a deprecation hint
    hint = a.deprecation_hint or b.deprecation_hint

    # Union test examples, deduplicated
    seen: set[str] = set()
    examples: list[str] = []
    for ex in (a.test_examples or []) + (b.test_examples or []):
        if ex not in seen:
            seen.add(ex)
            examples.append(ex)

    # Union migration patterns, deduplicated
    seen_pat: set[str] = set()
    patterns: list[dict] = []
    for pat in (a.migration_patterns or []) + (b.migration_patterns or []):
        before = pat.get("before") or pat.get("old_qualname") or ""
        after = pat.get("after") or pat.get("new_qualname") or ""
        sig = f"{before} -> {after}"
        if sig not in seen_pat:
            seen_pat.add(sig)
            patterns.append(pat)

    return a.model_copy(update={
        "state": state,
        "deprecation_hint": hint,
        "call_graph_confirmed": a.call_graph_confirmed or b.call_graph_confirmed,
        "test_examples": examples,
        "migration_patterns": patterns,
    })
