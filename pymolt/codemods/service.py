"""
pymolt/codemods/service.py

UI-agnostic codemod orchestration: turn an upgrade set
into codemod patterns/rules (via the Axiom Graph service) and apply them
locally.

Migration policy (per package): if the server returned Tier-2 rules, apply
RULES ONLY for that package (its Tier-1 patterns are re-expressed as kind-
tagged rules server-side — applying both would double-apply). If it returned no
rules (old server), the Tier-1 patterns path runs unchanged.

No printing / prompting here — callers own their I/O.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from pymolt.assess import AssessResult
from pymolt.assess.service import (
    _is_missing_container_error,
    build_comparison_rows,
    resolve_chosen_source,
)
from pymolt.codemods.apply import (
    apply_rules_to_repo,
    apply_to_repo,
    preview_repo,
    preview_rules_repo,
)
from pymolt.codemods.client import AxiomGraphClient, DependencyMigration
from pymolt.codemods.models import (
    CodemodBundle,
    CodemodPattern,
    CodemodRunResult,
    FileChange,
    FilePreview,
    ProgressSink,
)
from pymolt.codemods.rules import CodemodRule, RuleAdvisory
from pymolt.core.graph import DependencyGraph
from pymolt.ingestion.config import EnvConfig
from pymolt.ingestion.detect import DiscoveredSource, detect_sources
from pymolt.ingestion.orchestrator import orchestrate_ingestion

log = logging.getLogger(__name__)


def migrations_from_rows(rows: list[dict]) -> list[DependencyMigration]:
    """
    Convert assess comparison rows into dependency migrations — only the
    packages that actually changed version (status == 'upgrade', both versions
    concrete). 'added'/'removed'/'unchanged' rows yield no codemods.
    """
    migrations: list[DependencyMigration] = []
    for row in rows:
        if row.get("status") != "upgrade":
            continue
        old = row.get("baseline_version")
        new = row.get("target_version")
        name = row.get("name")
        if not (name and old and new) or old in ("unpinned", None) or new in ("unpinned", None):
            continue
        migrations.append(DependencyMigration(name=name, from_version=old, to_version=new))
    return migrations


def migrations_from_assess_result(result: AssessResult) -> list[DependencyMigration]:
    """Convert an AssessResult's comparison rows into codemod migrations."""
    return migrations_from_rows(list(result.rows))


def _config_value(config: dict | EnvConfig | None, key: str, default=None):
    if config is None:
        return default
    if isinstance(config, dict):
        return config.get(key, default)
    return getattr(config, key, default)


def _match_source(sources: list[DiscoveredSource], source_ref: str | Path) -> DiscoveredSource | None:
    ref = Path(source_ref).expanduser()
    ref_name = ref.name
    for src in sources:
        try:
            if src.path.resolve() == ref.resolve():
                return src
        except OSError:
            pass
    for src in sources:
        if src.path.name == ref_name:
            return src
    return None


def _resolve_graph_for_source(
    project_dir: str | Path,
    source_ref: str | Path,
    *,
    base_python: str | None = None,
    container_id: str | None = None,
    tool: str | None = None,
    use_cache: bool = True,
) -> tuple[DependencyGraph, Path, str | None]:
    """Resolve a DependencyGraph for one manifest path and return the graph + root.

    Degrades gracefully if ``container_id`` is configured but the container is no
    longer running: falls back to the container-free lock/static path (mirrors
    ``assess.service._resolve_baseline_graph``) instead of raising, and returns a
    warning as the third tuple element (``None`` when no degrade happened).
    """
    source_path = Path(source_ref).expanduser()
    candidate_roots = []
    if source_path.exists():
        candidate_roots.append(source_path.parent)
    candidate_roots.append(Path(project_dir))

    tried: list[Path] = []
    for root in candidate_roots:
        root = Path(root)
        if root in tried:
            continue
        tried.append(root)
        # codemods explicitly consumes the assess-generated target manifest, so it
        # must be visible here (assess auto-discovery excludes it — see detect_sources).
        sources = detect_sources(root, include_generated=True)
        chosen = _match_source(sources, source_path)
        if chosen is None and source_path.is_dir() and root.resolve() == source_path.resolve():
            chosen = resolve_chosen_source(sources, None) if sources else None
        if chosen is None:
            continue
        try:
            graph, _report = orchestrate_ingestion(
                str(root),
                chosen_source=chosen,
                base_python=base_python,
                container_id=container_id,
                use_cache=use_cache,
                tool=tool,
            )
            return graph, root, None
        except Exception as exc:
            if not (container_id and _is_missing_container_error(exc)):
                raise
            fallback_tool = "uv" if tool == "container" else tool
            try:
                graph, _report = orchestrate_ingestion(
                    str(root),
                    chosen_source=chosen,
                    base_python=base_python,
                    container_id=None,
                    use_cache=use_cache,
                    tool=fallback_tool,
                )
            except Exception as fallback_exc:
                # Same masking as in assess: without this the user is told to
                # install a legacy interpreter and never learns the container
                # they configured is gone.
                raise ValueError(
                    f"configured container {container_id} is not running, and "
                    f"resolving without it also failed.\n"
                    f"Start the container (or re-run setup to pick a new one).\n\n"
                    f"Fallback error: {fallback_exc}"
                ) from fallback_exc
            warning = (
                f"configured container {container_id} not running — resolved "
                "from manifest/lock; re-run setup to refresh."
            )
            log.warning(warning)
            return graph, root, warning

    raise ValueError(f"Could not resolve dependency source {source_path} in {project_dir}")


def migrations_from_dependency_file(
    project_dir: str | Path,
    target_dependency_file: str | Path,
    *,
    config: dict | EnvConfig | None = None,
    use_cache: bool = True,
    warnings: list[str] | None = None,
) -> list[DependencyMigration]:
    """Compare the baseline manifest against a target dependency file and derive migrations.

    If ``warnings`` is passed, any container-degrade warning (see
    ``_resolve_graph_for_source``) is appended to it so a caller (e.g. the CLI)
    can surface it — the return type stays unchanged for existing callers.
    """
    project_path = Path(project_dir)
    # A bare filename means "in this project", not "in whatever directory the
    # process happens to be in". Resolved against the CWD it silently compared
    # the project's baseline against a same-named target manifest belonging to
    # another repo — producing a plausible-looking, entirely wrong upgrade set.
    target_path = Path(target_dependency_file)
    if not target_path.is_absolute():
        candidate = project_path / target_path
        if candidate.exists():
            # The project's copy wins even when a same-named file exists in the
            # CWD — especially then: that is the case that silently succeeds
            # with another repo's manifest instead of failing loudly.
            target_dependency_file = candidate
    cfg = config if config is not None else EnvConfig.load(project_path / ".pymolt" / "env_config.json")

    baseline_manifest = _config_value(cfg, "selected_manifest")
    baseline_tool = _config_value(cfg, "selected_tool", "uv")
    if hasattr(baseline_tool, "value"):
        baseline_tool = baseline_tool.value
    base_python = _config_value(cfg, "base_python")
    container_id = _config_value(cfg, "container_id")

    baseline_graph, _, baseline_warning = _resolve_graph_for_source(
        project_path,
        baseline_manifest or project_path,
        base_python=base_python,
        container_id=container_id,
        tool=baseline_tool,
        use_cache=use_cache,
    )
    # The target side is resolved fresh under the TARGET Python and must never
    # go through the legacy container (see the invariant in CLAUDE.md). Passing
    # the baseline container here made the lookup search for a 3.13 interpreter
    # inside a 3.6 image, find nothing, and report the live container as "not
    # running" — degrading a resolve that was never supposed to use it.
    target_graph, _, target_warning = _resolve_graph_for_source(
        target_dependency_file,
        target_dependency_file,
        base_python=_config_value(cfg, "target_python") or base_python,
        container_id=None,
        tool="uv" if baseline_tool == "container" else baseline_tool,
        use_cache=use_cache,
    )

    if warnings is not None:
        for w in (baseline_warning, target_warning):
            if w:
                warnings.append(w)

    rows = build_comparison_rows(baseline_graph, target_graph, target_python="target", target_error=None)
    return migrations_from_rows(rows)


def resolve_codemod_migrations(
    project_dir: str | Path,
    *,
    assess_result: AssessResult | None = None,
    target_dependency_file: str | Path | None = None,
    config: dict | EnvConfig | None = None,
    use_cache: bool = True,
    warnings: list[str] | None = None,
) -> tuple[list[DependencyMigration], str]:
    """Resolve codemod migrations from cached assess data or a fallback target manifest.

    If ``warnings`` is passed, any degrade warning encountered while resolving
    (e.g. a configured container that is no longer running) is appended to it.
    """
    if assess_result is not None and assess_result.rows:
        migrations = migrations_from_assess_result(assess_result)
        return migrations, f"cached assess result ({len(assess_result.rows)} row(s))"

    if target_dependency_file:
        migrations = migrations_from_dependency_file(
            project_dir,
            target_dependency_file,
            config=config,
            use_cache=use_cache,
            warnings=warnings,
        )
        return migrations, f"target dependency file {Path(target_dependency_file).name}"

    raise ValueError(
        "No cached assess result is available; provide a target dependency file "
        "(for example requirements-target.txt or environment-target.yml)."
    )


def _split_bundles(
    bundles: dict[str, CodemodBundle],
) -> tuple[
    dict[str, list[CodemodPattern] | list[CodemodRule]],
    list[CodemodRule],
    list[CodemodPattern],
    list[str],
]:
    """
    Split fetched bundles by the per-package migration policy: a package whose
    bundle carries rules goes through the rules path ONLY (its patterns are
    dropped — they are the same Tier-1 facts, re-expressed server-side as
    kind-tagged rules); a package with no rules falls back to its patterns.

    Returns (by_pkg, rules_flat, patterns_flat, downgraded) — the two flat
    lists are each ready for one repo-wide apply/preview call, and `by_pkg`
    is what callers render (patterns or rules, per package).
    """
    by_pkg: dict[str, list[CodemodPattern] | list[CodemodRule]] = {}
    rules_flat: list[CodemodRule] = []
    patterns_flat: list[CodemodPattern] = []
    downgraded: list[str] = []
    for name, bundle in bundles.items():
        downgraded.extend(bundle.downgraded)
        if bundle.rules:
            by_pkg[name] = bundle.rules
            rules_flat.extend(bundle.rules)
        else:
            by_pkg[name] = bundle.patterns
            patterns_flat.extend(bundle.patterns)
    return by_pkg, rules_flat, patterns_flat, downgraded


def _merge_run_results(a: CodemodRunResult, b: CodemodRunResult) -> CodemodRunResult:
    """Merge two CodemodRunResult from separate repo walks (a Tier-2 rules pass
    and a Tier-1 patterns pass, run for disjoint packages in the same batch)
    into one — combining same-path FileChange/advisory entries so a file
    touched by both passes appears once."""
    changes_by_path: dict[str, FileChange] = {}
    for change in [*a.changes, *b.changes]:
        existing = changes_by_path.get(change.path)
        if existing is None:
            changes_by_path[change.path] = change.model_copy(deep=True)
        else:
            existing.sites += change.sites
            existing.patterns.extend(change.patterns)
            existing.advisories.extend(change.advisories)

    advisories_by_file: dict[str, list[RuleAdvisory]] = {}
    for src in (a.advisories_by_file, b.advisories_by_file):
        for path, advisories in src.items():
            advisories_by_file.setdefault(path, []).extend(advisories)

    return CodemodRunResult(
        root=a.root,
        dry_run=a.dry_run,
        files_scanned=max(a.files_scanned, b.files_scanned),
        changes=list(changes_by_path.values()),
        patterns_applied=a.patterns_applied + b.patterns_applied,
        advisories_by_file=advisories_by_file,
    )


def run_codemods(
    root: str | Path,
    migrations: list[DependencyMigration],
    *,
    base_url: str,
    write: bool = False,
    client: AxiomGraphClient | None = None,
    progress: Callable[[str], None] | None = None,
) -> tuple[dict[str, list[CodemodPattern] | list[CodemodRule]], CodemodRunResult]:
    """
    Fetch patterns/rules for `migrations` from Axiom Graph and apply them under
    `root` (per package: rules if the server returned any, else patterns).

    Returns (by_package, run_result). Dry-run unless write=True. Auto-apply
    only writes confidence=="verified" rules (see `apply.apply_rules_to_repo`);
    `run_result.downgraded` carries the rules the client downgraded on fetch.
    Raises AxiomGraphError if the service is unreachable (caller decides UX).

    `progress`, if given, is called with human-readable status lines as each
    phase (fetch → local verify → apply) starts, so an interface can show that work
    is happening instead of appearing to hang on a long server-side analysis.
    """
    client = client or AxiomGraphClient(base_url)
    bundles = client.fetch_bundle(migrations, progress=progress)
    by_pkg, rules_flat, patterns_flat, downgraded = _split_bundles(bundles)

    if progress:
        n = len(rules_flat) + len(patterns_flat)
        progress(f"Applying {n} codemod(s) under {root} via LibCST…")

    if not rules_flat:
        # No package returned rules (old server, or nothing to migrate) — the
        # Tier-1 path runs byte-identical to today.
        result = apply_to_repo(root, patterns_flat, write=write)
    else:
        rules_result = apply_rules_to_repo(root, rules_flat, write=write)
        result = (
            _merge_run_results(rules_result, apply_to_repo(root, patterns_flat, write=write))
            if patterns_flat
            else rules_result
        )
    result.downgraded = downgraded
    return by_pkg, result


def preview_codemods(
    root: str | Path,
    migrations: list[DependencyMigration],
    *,
    base_url: str,
    client: AxiomGraphClient | None = None,
    progress: Callable[[str], None] | None = None,
    on_recipes: Callable[[dict[str, list[CodemodPattern] | list[CodemodRule]]], None] | None = None,
    on_file: ProgressSink = None,
) -> tuple[dict[str, list[CodemodPattern] | list[CodemodRule]], list[FilePreview]]:
    """
    Fetch patterns/rules for `migrations` and compute per-file before/after
    previews — nothing is written. Callers render these as reviewable diff
    cards; the engineer accepts each one before it lands on disk.

    Signature intentionally unchanged: still a 2-tuple, with no separate
    "downgraded" list. A rule the client downgraded already carries
    confidence=="heuristic" on the returned `FilePreview.rules`, which is what
    the review card renders — so the downgrade is
    visible without widening this return type. `run_codemods` has room to
    carry the fuller `CodemodRunResult.downgraded` list because it returns a
    model, not a bare tuple; `preview_codemods` does not, and callers (interfaces,
    tests) already destructure exactly two values.

    `progress` reports phase transitions (fetch → preview), `on_recipes` hands
    over the fetched recipe set the moment it is known (before the walk starts,
    so an interface can show what is about to be applied), and `on_file`
    reports every file the walk scans. All three are pure observers — none of
    them changes what is returned.

    Raises AxiomGraphError if the service is unreachable (caller decides UX).
    """
    client = client or AxiomGraphClient(base_url)
    bundles = client.fetch_bundle(migrations, progress=progress)
    by_pkg, rules_flat, patterns_flat, _downgraded = _split_bundles(bundles)

    if on_recipes:
        on_recipes(by_pkg)
    if progress:
        n = len(rules_flat) + len(patterns_flat)
        progress(f"Previewing {n} codemod(s) under {root} via LibCST…")

    if not rules_flat:
        return by_pkg, preview_repo(root, patterns_flat, on_file=on_file)

    previews = preview_rules_repo(root, rules_flat, on_file=on_file)
    if patterns_flat:
        previews = previews + preview_repo(root, patterns_flat, on_file=on_file)
    return by_pkg, previews
