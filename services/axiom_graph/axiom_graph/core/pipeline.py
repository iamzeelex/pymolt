"""
axiom_graph/pipeline.py

Main orchestrator for Axiom Graph delta computation.

Drives the full chronological pipeline:
    1. Build release chain (PyPI API + optional git tags)
    2. Load checkpoint — skip already-completed steps
    3. For each adjacent version pair (v_i → v_{i+1}):
       a. Acquire sdist for both versions
       b. Compute griffe structural diff (Layer 1: fast filter)
       c. Mine AST deprecation hints from old version source
       d. Mine test-suite migration examples
       e. Fuse all signals → PairwiseDelta
       f. Save step to checkpoint immediately (crash-safe)
    4. Accumulate chain → FullDelta

Design principles:
- Never raises for individual step failures — marks as skipped and logs.
- Checkpoint-based resumption: restart picks up where it left off.
- Progress callbacks are injected by the CLI; core is non-interactive.
- Each step is independently logged with timing for observability.
"""

from __future__ import annotations

import logging
import time

from axiom_graph.acquisition.checkpoint import (
    clear_checkpoint,
    load_checkpoint,
    save_step,
)
from axiom_graph.acquisition.sdist_cache import (
    find_package_source_dir,
    find_tests_dir,
    get_sdist,
)
from axiom_graph.analyzers.ast_miner import mine_deprecation_hints
from axiom_graph.analyzers.griffe_diff import _ensure_griffe_cache, griffe_diff
from axiom_graph.analyzers.test_miner import golden_pair_from_hunk, mine_test_examples
from axiom_graph.analyzers.upstream_compat import mine_upstream_compatibility_tables
from axiom_graph.core.fusion import accumulate_chain, fuse_pairwise
from axiom_graph.core.models import CodemodPattern, CodemodRule, FullDelta, PairwiseDelta
from axiom_graph.core.propose import propose_codemods_for_changes, rule_from_pattern, to_pattern
from axiom_graph.core.rule_catalog import rules_for
from axiom_graph.sources.version_manifest import build_version_manifest

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Progress callback types
# ---------------------------------------------------------------------------

class StepProgress:
    """
    Progress context passed to the CLI for rich display.
    The pipeline calls these methods; the CLI updates the UI.
    """

    def on_chain_built(self, chain: list[str], skipped_count: int) -> None:
        """Called once after the release chain is resolved."""

    def on_step_start(
        self,
        step_index: int,
        total_steps: int,
        from_v: str,
        to_v: str,
        resumed: bool,
    ) -> None:
        """Called at the start of each version-pair step."""

    def on_step_phase(self, phase: str) -> None:
        """Called when entering a named phase within a step (e.g. 'griffe', 'ast')."""

    def on_step_done(self, delta: PairwiseDelta, elapsed: float, from_checkpoint: bool) -> None:
        """Called when a step completes (either freshly computed or loaded from checkpoint)."""

    def on_done(self, full_delta: FullDelta, total_elapsed: float) -> None:
        """Called when the entire pipeline is complete."""


# ---------------------------------------------------------------------------
# Release chain builder
# ---------------------------------------------------------------------------

def build_release_chain(
    package: str,
    from_v: str,
    to_v: str,
    *,
    use_git: bool = False,
    include_prereleases: bool = False,
    clone_bare: bool = False,
) -> list[str]:
    """
    Build an ordered list of all versions between from_v and to_v (exclusive start,
    inclusive end) using the Time & Versioning Management system.

    Delegates to version_manifest for:
      - PyPI + git-tag acquisition
      - Deduplication and semantic-version sorting
      - Chronological ordering validation
      - Optional bare-repo cloning for local tag verification

    Returns at least [to_v] even if intermediate releases can't be fetched.
    """
    manifest = build_version_manifest(
        package,
        from_v,
        to_v,
        use_git=use_git,
        clone_bare=clone_bare,
        include_prereleases=include_prereleases,
    )

    chain = manifest.chain()
    log.info("Release chain via version manifest: %d versions", len(chain))
    if manifest.git_repo_path:
        log.info("  ↳ Bare repository: %s", manifest.git_repo_path)
    return chain


# ---------------------------------------------------------------------------
# Single-step pipeline
# ---------------------------------------------------------------------------

def _run_step(
    package: str,
    old_v: str,
    new_v: str,
    *,
    progress: StepProgress | None = None,
) -> PairwiseDelta:
    """
    Run the full analysis pipeline for one version pair.
    Returns a PairwiseDelta (possibly with empty fields if steps failed).
    """
    t0 = time.monotonic()
    log.info("─── Step %s → %s ───", old_v, new_v)

    # 1. Acquire sdist directories
    if progress:
        progress.on_step_phase("📦 acquiring sdist")
    old_sdist = get_sdist(package, old_v)
    new_sdist = get_sdist(package, new_v)

    # 2. Find package source and tests directories
    old_src = find_package_source_dir(old_sdist, package) if old_sdist else None
    old_tests = find_tests_dir(old_sdist) if old_sdist else None
    new_tests = find_tests_dir(new_sdist) if new_sdist else None

    # 3. Griffe structural diff (Layer 1: the fast filter)
    if progress:
        progress.on_step_phase("🔍 griffe structural diff")
    raw_changes, transitions = griffe_diff(package, old_v, new_v)
    log.info("griffe diff: %d breaking changes", len(raw_changes))

    # 4. AST deprecation mining (from old version — we want to know what was warned there)
    ast_hints: dict[str, str] = {}
    if old_src:
        if progress:
            progress.on_step_phase("🔎 AST deprecation mining")
        ast_hints = mine_deprecation_hints(old_src, package)
        log.info("AST hints: %d deprecation messages", len(ast_hints))

    # 5. Test-suite diff mining
    removed_api_names = list({
        c.path.split(".")[-1]
        for c in raw_changes
        if c.kind in ("object-removed", "Public object was removed")
    })
    test_examples: dict[str, list[str]] = {}
    if old_tests and new_tests and removed_api_names:
        if progress:
            progress.on_step_phase("📋 test suite diff mining")
        test_examples = mine_test_examples(old_tests, new_tests, removed_api_names)
        total_ex = sum(len(v) for v in test_examples.values())
        log.info("Test examples: %d hunks across %d APIs", total_ex, len(removed_api_names))

    # 6. Codemod proposals (Layer 2: value-flow + prose over the changed
    #    symbols, resolved from the OLD version's source where they still exist).
    codemods: list[CodemodPattern] = []
    if old_src:
        if progress:
            progress.on_step_phase("🛠  proposing codemods")
        changed_qualnames = [c.path for c in raw_changes if c.path]
        proposals = propose_codemods_for_changes(
            old_src, package, changed_qualnames
        )
        codemods = [to_pattern(p) for p in proposals]
        log.info("Codemod proposals: %d", len(codemods))

    # 6.5 Ingest upstream author-provided compatibility tables (e.g. TF renames_v2, JAX deprecations)
    upstream_changes: list[BreakingChange] = []
    upstream_patterns: list[CodemodPattern] = []
    for sdist_root in (old_sdist, new_sdist):
        if sdist_root:
            u_changes, u_patterns = mine_upstream_compatibility_tables(sdist_root, package)
            upstream_changes.extend(u_changes)
            upstream_patterns.extend(u_patterns)

    if upstream_patterns:
        codemods.extend(upstream_patterns)

    # 7. Fuse
    if progress:
        progress.on_step_phase("⚡ fusing signals")
    delta = fuse_pairwise(
        raw_changes=raw_changes,
        ast_hints=ast_hints,
        test_examples=test_examples,
        from_version=old_v,
        to_version=new_v,
        package=package,
        transitions=transitions,
        codemods=codemods,
    )

    if upstream_changes:
        existing_paths = {c.path for c in delta.changes}
        for u_change in upstream_changes:
            if u_change.path not in existing_paths:
                u_change.deprecated_since = old_v
                u_change.removed_in = new_v
                delta.changes.append(u_change)
                existing_paths.add(u_change.path)

    elapsed = time.monotonic() - t0
    log.info("Step %s→%s done in %.1fs: %d changes", old_v, new_v, elapsed, len(delta.changes))
    return delta


# ---------------------------------------------------------------------------
# Rule assembly (FullDelta.rules — runs once, after chain accumulation)
# ---------------------------------------------------------------------------

def _build_rules(
    full_delta: FullDelta, package: str, from_v: str, to_v: str
) -> list[CodemodRule]:
    """
    Populate FullDelta.rules from two sources:
      (a) curated catalog hits (core.rule_catalog) for symbols that changed
          or were removed anywhere in this delta, stamped with the requested
          (package, from_v, to_v) range;
      (b) a Tier-1 projection (core.propose.rule_from_pattern) of every
          transitively-resolved CodemodPattern, carrying that symbol's mined
          test-diff hunks as evidence and — only when the projection didn't
          already synthesize one — promoting the first parseable mined hunk
          into the rule's golden pair via golden_pair_from_hunk.
    """
    rules: list[CodemodRule] = [
        r.model_copy(update={"library": package, "from_version": from_v, "to_version": to_v})
        for r in rules_for(package, [c.path for c in full_delta.changes if c.path])
    ]

    hunks_by_symbol = {c.path: c.test_examples for c in full_delta.changes}
    for pattern in full_delta.codemods:
        hunks = hunks_by_symbol.get(pattern.old_qualname, [])
        rule = rule_from_pattern(
            pattern, library=package, from_version=from_v, to_version=to_v, examples=hunks
        )
        if rule.test_before is None:
            for hunk in hunks:
                pair = golden_pair_from_hunk(hunk)
                if pair is not None:
                    rule = rule.model_copy(update={"test_before": pair[0], "test_after": pair[1]})
                    break
        rules.append(rule)

    return rules


# ---------------------------------------------------------------------------
# Full pipeline
# ---------------------------------------------------------------------------

def compute_full_delta(
    package: str,
    from_v: str,
    to_v: str,
    *,
    use_git: bool = False,
    include_prereleases: bool = False,
    progress: StepProgress | None = None,
    resume: bool = True,
    force_restart: bool = False,
) -> FullDelta:
    """
    Compute the complete chronological API delta between two arbitrary versions.

    Args:
        package:             PyPI package name (e.g. "pandas").
        from_v:              Source version (e.g. "1.3.5").
        to_v:                Target version (e.g. "2.0.0").
        use_git:             If True, also fetch versions from git tags.
        include_prereleases: If True, include alpha/beta/rc releases.
        progress:            Optional StepProgress instance for live display callbacks.
        resume:              If True (default), resume from checkpoint if available.
        force_restart:       If True, clear existing checkpoint and start fresh.

    Returns:
        FullDelta with temporally-enriched breaking changes.
        If the package cannot be analyzed, returns FullDelta(skipped=True).
    """
    t_start = time.monotonic()
    log.info("═══ Axiom Graph: %s %s → %s ═══", package, from_v, to_v)

    # Clear checkpoint if forced restart
    if force_restart:
        cleared = clear_checkpoint(package, from_v, to_v)
        if cleared:
            log.info("Cleared checkpoint for fresh run.")

    # Build release chain
    try:
        release_chain = build_release_chain(
            package, from_v, to_v,
            use_git=use_git,
            include_prereleases=include_prereleases,
        )
    except Exception as exc:
        log.error("Release chain build failed: %s", exc)
        return FullDelta(
            package=package,
            from_version=from_v,
            to_version=to_v,
            skipped=True,
            skip_reason=f"release chain failed: {exc}",
        )

    if not release_chain:
        return FullDelta(
            package=package,
            from_version=from_v,
            to_version=to_v,
            skipped=True,
            skip_reason="no releases found between versions",
        )

    # Build version pairs: (from_v, chain[0]), (chain[0], chain[1]), ..., (..., to_v)
    all_versions = [from_v] + release_chain
    version_pairs = list(zip(all_versions, all_versions[1:]))
    total_steps = len(version_pairs)

    # ── Parallel pre-fetch (concurrent wheel and sdist downloads) ──
    def _prefetch_version(v: str) -> None:
        try:
            _ensure_griffe_cache(package, v)
        except Exception:
            pass
        try:
            get_sdist(package, v)
        except Exception:
            pass

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(len(all_versions), 8)) as executor:
        list(executor.map(_prefetch_version, all_versions))

    # Load checkpoint — find already-completed steps
    completed_by_key: dict[tuple[str, str], PairwiseDelta] = {}
    if resume and not force_restart:
        for delta in load_checkpoint(package, from_v, to_v):
            key = (delta.from_version, delta.to_version)
            completed_by_key[key] = delta

    skipped_count = len(completed_by_key)
    log.info(
        "Processing %d step(s), %d already checkpointed",
        total_steps, skipped_count
    )

    if progress:
        progress.on_chain_built(release_chain, skipped_count)

    # Run each step (skip if checkpointed)
    pairwise_deltas: list[PairwiseDelta] = []

    for step_idx, (old_v, new_v) in enumerate(version_pairs):
        key = (old_v, new_v)

        if key in completed_by_key:
            # Resume from checkpoint
            cached = completed_by_key[key]
            pairwise_deltas.append(cached)
            if progress:
                progress.on_step_start(step_idx, total_steps, old_v, new_v, resumed=True)
                progress.on_step_done(cached, elapsed=0.0, from_checkpoint=True)
            continue

        if progress:
            progress.on_step_start(step_idx, total_steps, old_v, new_v, resumed=False)

        try:
            step_delta = _run_step(
                package, old_v, new_v,
                progress=progress,
            )
            pairwise_deltas.append(step_delta)

            # ── Checkpoint immediately ──
            save_step(step_delta, package, from_v, to_v)

            elapsed = time.monotonic() - t_start
            if progress:
                progress.on_step_done(step_delta, elapsed, from_checkpoint=False)

        except Exception as exc:
            log.warning("Step %s→%s failed (skipping): %s", old_v, new_v, exc)

    # Accumulate
    full_delta = accumulate_chain(
        pairwise_deltas,
        package=package,
        from_v=from_v,
        to_v=to_v,
        release_chain=release_chain,
    )
    full_delta.rules = _build_rules(full_delta, package, from_v, to_v)

    total_elapsed = time.monotonic() - t_start
    log.info(
        "═══ Done in %.1fs: %d total breaking changes (%s) ═══",
        total_elapsed, full_delta.total_breaking, full_delta.summary_by_risk,
    )

    if progress:
        progress.on_done(full_delta, total_elapsed)

    return full_delta
