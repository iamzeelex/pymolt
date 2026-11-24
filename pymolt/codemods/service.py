"""
pymolt/codemods/service.py

UI-agnostic codemod orchestration: turn an upgrade set into codemod
patterns/rules, preview them locally, and build the migration plan from the
previews (``pymolt apply`` writes the plan; nothing here writes source).

The provider is ``pymolt.delta.provider.LocalDeltaProvider``: it computes the API
changes from the old and new environments on this machine.

Migration policy (per package): if the provider returned Tier-2 rules, apply
RULES ONLY for that package (its Tier-1 patterns are re-expressed as kind-
tagged rules — applying both would double-apply). If it returned no rules,
the Tier-1 patterns path runs unchanged.

No printing / prompting here — callers own their I/O.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from pathlib import Path

from pymolt.assess import AssessResult
from pymolt.assess.service import (
    UNKNOWN_VERSION,
    _is_missing_container_error,
    build_comparison_rows,
    resolve_chosen_source,
)
from pymolt.codemods.apply import (
    preview_mixed_repo,
    preview_repo,
    preview_rules_repo,
)
from pymolt.codemods.models import (
    ApiImpact,
    CodemodBundle,
    CodemodPattern,
    CodemodRunResult,
    DependencyMigration,
    FileChange,
    FilePreview,
    ProgressSink,
)
from pymolt.codemods.rules import CodemodRule, RuleAdvisory
from pymolt.core.graph import DependencyGraph
from pymolt.core.versions import same_version
from pymolt.ingestion.config import EnvConfig
from pymolt.ingestion.detect import DiscoveredSource, detect_sources, find_source
from pymolt.ingestion.orchestrator import orchestrate_ingestion

log = logging.getLogger(__name__)


def migrations_from_rows(rows: list[dict]) -> list[DependencyMigration]:
    """
    Convert assess comparison rows into dependency migrations: the packages that
    changed version (status == 'upgrade', both versions concrete), and the ones whose
    base version is unknown ('base-unknown'), with ``from_version`` "unknown" — the plan
    states those as moves it could not compute (see :func:`known_migrations`), never as
    a move from a guessed release. 'added'/'removed'/'unchanged' rows yield no codemods.
    """
    migrations: list[DependencyMigration] = []
    for row in rows:
        status = row.get("status")
        if status not in ("upgrade", "base-unknown"):
            continue
        old = UNKNOWN_VERSION if status == "base-unknown" else row.get("baseline_version")
        new = row.get("target_version")
        name = row.get("name")
        if not (name and old and new) or old in ("unpinned", None) or new in ("unpinned", None):
            continue
        migrations.append(DependencyMigration(name=name, from_version=old, to_version=new))
    return migrations


def known_migrations(
    migrations: list[DependencyMigration],
) -> tuple[list[DependencyMigration], list[DependencyMigration]]:
    """``(known, unknown)``: the moves with a base version, and those without one.

    Only the known ones go to the delta engine: for an unknown base it would have to pick
    an old release (the manifest's floor, a 2013 sdist), which is a guess."""
    known = [m for m in migrations if m.from_version != UNKNOWN_VERSION]
    return known, [m for m in migrations if m.from_version == UNKNOWN_VERSION]


_WITNESSABLE = ("upgrade", "unchanged", "added", "base-unknown")


def witness_rows(
    rows: list[dict], witnessed: dict[str, str] | None,
) -> tuple[list[dict], list[str]]:
    """Comparison rows with the baseline side set to what the baseline capture ran.

    A baseline manifest that does not pin a package is resolved today, to its newest
    release: the plan would then compute API changes from a version the app never ran
    (pandas 3.x instead of the 1.5 the baseline captured). The capture's record is
    witnessed, so it wins, and each correction is reported. Returns ``(rows, notes)``;
    rows without a record pass through unchanged.
    """
    if not witnessed:
        return rows, []
    from packaging.utils import canonicalize_name

    out: list[dict] = []
    notes: list[str] = []
    for row in rows:
        name = row.get("name")
        ran = witnessed.get(canonicalize_name(name)) if isinstance(name, str) else None
        target = row.get("target_version")
        if (ran is None or row.get("status") not in _WITNESSABLE
                or target in (None, "unpinned") or same_version(ran, row.get("baseline_version"))):
            out.append(row)
            continue
        resolved = row.get("baseline_version")
        status = "unchanged" if same_version(ran, target) else "upgrade"
        out.append({**row, "baseline_version": ran, "status": status})
        if "upgrade" not in (status, row.get("status")) and row.get("status") != "base-unknown":
            continue                        # the correction changes no migration
        notes.append(f"{name}: the baseline capture ran {ran}"
                     + (f" (the baseline manifest resolves to {resolved} today)"
                        if resolved not in (None, "unpinned", UNKNOWN_VERSION)
                        else " (the base version was unknown)"
                        if resolved == UNKNOWN_VERSION else ""))
    return out, notes


def _config_value(config: dict | EnvConfig | None, key: str, default=None):
    if config is None:
        return default
    if isinstance(config, dict):
        return config.get(key, default)
    return getattr(config, key, default)


def _locate_source(
    project_dir: str | Path, source_ref: str | Path
) -> tuple[Path, DiscoveredSource] | None:
    """The ingestion root and the manifest ``source_ref`` names, or None.

    A relative ``source_ref`` is relative to the project, the way setup records it
    (``etc/requirements.txt``), never to the process's working directory: resolved
    against the cwd it named another project's file, or none, and a match on the bare
    file name then picked a different manifest that happened to share it. The lookup is
    assess's (:func:`find_source`), so plan and assess read the same file. When
    ``project_dir`` is not a directory it names the manifest itself (the target
    manifest), and its folder is the root.
    """
    project_path = Path(project_dir).expanduser()
    ref = Path(source_ref).expanduser()
    if not project_path.is_dir():
        project_path, ref = project_path.parent, Path(project_path.name)
    candidate = ref if ref.is_absolute() else project_path / ref
    if candidate.is_dir():
        # No manifest named: the project's default one.
        if candidate.resolve() != project_path.resolve():
            return None
        sources = detect_sources(project_path)
        return (project_path, resolve_chosen_source(sources, None)) if sources else None
    # Pymolt's own generated target manifest is found only when it is the one named.
    include_gen = ref.name.endswith(("-target.txt", "-target.yml"))
    sources = detect_sources(project_path, include_generated=include_gen)
    chosen = find_source(sources, ref.as_posix(), project_path)
    return (project_path, chosen) if chosen is not None else None


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
    located = _locate_source(project_dir, source_ref)
    if located is not None:
        root, chosen = located
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

    raise ValueError(f"Could not resolve dependency source {source_ref} in {project_dir}")


def _resolver_failure(side: str, manifest: str | Path, exc: subprocess.CalledProcessError):
    """A resolver (uv) failure while planning, as a :class:`PlanInputError`.

    The summary is the resolver's own first error line, not a Python traceback; the
    hint names the culprits and the way out.
    """
    from pymolt.assess.service import describe_resolver_failure

    found = describe_resolver_failure(exc)
    name = Path(manifest).name
    culprits = f"Culprit(s): {', '.join(found.culprits)}. " if found.culprits else ""
    return PlanInputError(
        f"the {side} manifest {name} does not resolve: {found}",
        f"{culprits}Fix or loosen them, then run `pymolt assess .` and plan again. "
        "No source files were changed.",
    )


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
    cfg = config if config is not None else EnvConfig.load(
        project_path / ".pymolt" / "env_config.json"
    )

    baseline_manifest = _config_value(cfg, "selected_manifest")
    baseline_tool = _config_value(cfg, "selected_tool", "uv")
    if hasattr(baseline_tool, "value"):
        baseline_tool = baseline_tool.value
    base_python = _config_value(cfg, "base_python")
    container_id = _config_value(cfg, "container_id")

    try:
        baseline_graph, _, baseline_warning = _resolve_graph_for_source(
            project_path,
            baseline_manifest or project_path,
            base_python=base_python,
            container_id=container_id,
            tool=baseline_tool,
            use_cache=use_cache,
        )
    except subprocess.CalledProcessError as exc:
        raise _resolver_failure("baseline", baseline_manifest or project_path, exc) from exc
    # The target side is resolved fresh under the TARGET Python and must never
    # go through the legacy container (see the invariant in CLAUDE.md). Passing
    # the baseline container here made the lookup search for a 3.13 interpreter
    # inside a 3.6 image, find nothing, and report the live container as "not
    # running" — degrading a resolve that was never supposed to use it.
    try:
        target_graph, _, target_warning = _resolve_graph_for_source(
            target_dependency_file,
            target_dependency_file,
            base_python=_config_value(cfg, "target_python") or base_python,
            container_id=None,
            tool="uv" if baseline_tool == "container" else baseline_tool,
            use_cache=use_cache,
        )
    except subprocess.CalledProcessError as exc:
        raise _resolver_failure("target", target_dependency_file, exc) from exc

    if warnings is not None:
        for w in (baseline_warning, target_warning):
            if w:
                warnings.append(w)

    rows = build_comparison_rows(
        baseline_graph, target_graph, target_python="target", target_error=None
    )
    rows, notes = witness_rows(rows, witnessed_baseline(project_path))
    if warnings is not None:
        warnings.extend(notes)
    return migrations_from_rows(rows)


def witnessed_baseline(project_dir: str | Path) -> dict[str, str] | None:
    """The versions the baseline capture ran, if it recorded them (see ``witness_rows``)."""
    from pymolt.verify.service import witnessed_distributions

    try:
        return witnessed_distributions(project_dir, "baseline")
    except Exception as exc:  # a broken state file must not break planning
        log.warning("could not read the baseline capture's versions: %s", exc)
        return None


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
        rows, notes = witness_rows(list(assess_result.rows), witnessed_baseline(project_dir))
        if warnings is not None:
            warnings.extend(notes)
        return migrations_from_rows(rows), f"cached assess result ({len(rows)} row(s))"

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


def codemod_provider(project_dir: str | Path, *, online: bool = False):
    """The codemod source for a project: the local delta engine.

    ``online`` lets it fetch intermediate releases (package names and versions only).
    """
    from pymolt.delta.provider import LocalDeltaProvider

    return LocalDeltaProvider(project_dir, online=online)


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


def preview_codemods(
    root: str | Path,
    migrations: list[DependencyMigration],
    *,
    client=None,
    progress: Callable[[str], None] | None = None,
    on_recipes: Callable[[dict[str, list[CodemodPattern] | list[CodemodRule]]], None] | None = None,
    on_impacts: Callable[[dict[str, list[ApiImpact]]], None] | None = None,
    on_file: ProgressSink = None,
    auto_apply_only: bool = False,
    online: bool = False,
    on_downgraded: Callable[[list[str]], None] | None = None,
    on_step=None,
) -> tuple[dict[str, list[CodemodPattern] | list[CodemodRule]], list[FilePreview]]:
    """
    Fetch patterns/rules for `migrations` and compute per-file before/after
    previews — nothing is written. Callers render these as reviewable diff
    cards; the engineer accepts each one before it lands on disk.

    Signature intentionally unchanged: still a 2-tuple, with no separate
    "downgraded" list. A rule the provider downgraded already carries
    confidence=="heuristic" on the returned `FilePreview.rules`, which is what
    the review card renders — so the downgrade is
    visible without widening this return type (``on_downgraded`` hands over the
    fuller list); callers (interfaces, tests) destructure exactly two values.

    `progress` reports phase transitions (fetch → preview), `on_recipes` hands
    over the fetched recipe set the moment it is known (before the walk starts,
    so an interface can show what is about to be applied), and `on_file`
    reports every file the walk scans. ``on_impacts`` exposes the structured
    API-change paths for persistence in the local migration plan, and
    ``on_downgraded`` the summaries of the rules the provider downgraded (the plan
    lists them for a person). ``on_step(recipe, path, before, after)`` sees each
    recipe's change to each kept file, in application order (the plan's steps).
    All six are pure observers — none of them changes what is returned.
    """
    client = client or codemod_provider(root, online=online)
    bundles = client.fetch_bundle(migrations, progress=progress)
    by_pkg, rules_flat, patterns_flat, downgraded = _split_bundles(bundles)

    if on_recipes:
        on_recipes(by_pkg)
    if on_downgraded:
        on_downgraded(list(downgraded))
    if on_impacts:
        on_impacts({name: bundle.impacts for name, bundle in bundles.items()})
    if progress:
        n = len(rules_flat) + len(patterns_flat)
        progress(f"Previewing {n} codemod(s) under {root} via LibCST…")

    if not rules_flat:
        return by_pkg, preview_repo(root, patterns_flat, on_file=on_file, on_step=on_step)

    if patterns_flat:
        return by_pkg, preview_mixed_repo(
            root, rules_flat, patterns_flat, on_file=on_file,
            verified_only=auto_apply_only, on_step=on_step,
        )
    return by_pkg, preview_rules_repo(
        root, rules_flat, on_file=on_file, verified_only=auto_apply_only, on_step=on_step
    )


# ── the migration plan ───────────────────────────────────────────────────────
# `pymolt plan` (CLI and MCP) and `pymolt succession --plan` build the one plan
# that `pymolt apply` writes. Every source change goes through it.

_TARGET_MANIFESTS = ("requirements-target.txt", "environment-target.yml")


class PlanInputError(ValueError):
    """A plan cannot be built from what is on disk. ``hint`` says what to run."""

    def __init__(self, message: str, hint: str) -> None:
        super().__init__(message)
        self.hint = hint


def plan_inputs(project_dir: str | Path, target_file: str | Path | None = None):
    """The saved setup and the target manifest a plan is bound to.

    Returns ``(config, target_path)``; raises :class:`PlanInputError` when setup
    or assess has not run.
    """
    root = Path(project_dir)
    config = EnvConfig.load(root / ".pymolt" / "env_config.json")
    if config is None:
        raise PlanInputError(
            "pymolt is not configured for this project",
            "Run `pymolt setup .` and `pymolt assess .` first.",
        )
    if not target_file:
        target_file = next(
            (name for name in _TARGET_MANIFESTS if (root / name).is_file()), None
        )
    if not target_file:
        raise PlanInputError(
            "no pinned target manifest is available",
            "Run `pymolt assess .` before creating a plan.",
        )
    target_path = Path(target_file)
    if not target_path.is_absolute():
        target_path = root / target_path
    if not target_path.is_file():
        raise PlanInputError(f"target manifest does not exist: {target_path}", "")
    return config, target_path


def build_migration_plan(
    project_dir: str | Path,
    *,
    target_file: str | Path | None = None,
    progress: Callable[[str], None] | None = None,
    online: bool = False,
):
    """Preview the verified codemods for the target manifest and freeze them as a plan.

    Returns ``(plan, previews, source_desc, by_pkg)``. Nothing is saved or written:
    callers save the plan, and only `pymolt apply` changes source files. The API
    changes are computed locally; `online` lets the engine fetch intermediate releases
    (names and versions only). Raises :class:`PlanInputError`.
    """
    from pymolt.migration_plan import create_migration_plan
    from pymolt.workorder.builder import StepRecorder

    root = Path(project_dir)
    # Each recipe's change to each file, in application order: the plan's automated steps.
    recorder = StepRecorder()
    config, target_path = plan_inputs(root, target_file)
    # The state the plan is bound to and replaces: a damaged or newer file refuses the plan
    # now (``StateFileError``), not after the engine ran.
    from pymolt.migration_plan import MigrationPlan
    from pymolt.verify.service import load_contract_state

    load_contract_state(root)
    MigrationPlan.load(root)
    notes: list[str] = []
    migrations, source_desc = resolve_codemod_migrations(
        root, target_dependency_file=target_path, config=config.model_dump(mode="json"),
        warnings=notes,
    )
    if notes:
        # Where the from-versions came from (the baseline capture's record over today's
        # resolve of an unpinned manifest) and any degraded resolve, stated, not implied.
        source_desc = f"{source_desc}; " + "; ".join(notes)
    scanned: set[str] = set()
    impacts: dict[str, list[ApiImpact]] = {}
    by_pkg: dict[str, list[CodemodPattern] | list[CodemodRule]] = {}
    previews: list[FilePreview] = []
    downgraded: list[str] = []
    provider = codemod_provider(root, online=online)
    known, unknown = known_migrations(migrations)
    if known:
        by_pkg, previews = preview_codemods(
            root, known, client=provider, progress=progress,
            on_impacts=impacts.update, on_file=lambda item: scanned.add(item.path),
            auto_apply_only=True, on_downgraded=downgraded.extend, on_step=recorder.record,
        )
        recorder.note_packages(by_pkg)
    # The provider's delta reports feed the plan's risk prediction as-is, so the facts are
    # computed once. A move from an unknown base is never computed: its delta is
    # inconclusive, with what settles it.
    from pymolt.delta.provider import inconclusive_impact, unknown_base_report

    reports = list(getattr(provider, "reports", None) or ())
    for move in unknown:
        report = unknown_base_report(move.name, move.to_version)
        reports.append(report)
        impacts[move.name] = [inconclusive_impact(report)]
    delta_reports = reports or None
    python_move = None
    if config.base_python and config.target_python:
        from pymolt.pyversion.plan import same_python_minor

        if not same_python_minor(config.base_python, config.target_python):
            python_move = (config.base_python, config.target_python)
    python_stage = None
    if python_move is not None:
        # The python fixes join the dependency rewrites in one preview, so the plan's file
        # changes (and the diffs the interfaces render) hold both.
        from pymolt.pyversion.plan import plan_python_stage

        try:
            python_stage = plan_python_stage(root, *python_move, previews=previews,
                                             on_step=recorder.record)
            previews = python_stage.previews
        except Exception:  # noqa: BLE001 - create_migration_plan reports a failed python part
            python_stage = None
    plan = create_migration_plan(
        root, previews, migrations,
        target_python=config.target_python or "unknown",
        source_manifest=config.selected_manifest or "unknown",
        target_manifest_path=target_path,
        files_scanned=len(scanned),
        impacts=impacts,
        delta_reports=delta_reports,
        python_move=python_move,
        python_stage=python_stage,
        downgraded=downgraded,
        recorder=recorder,
    )
    return plan, previews, source_desc, by_pkg
