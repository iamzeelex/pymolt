"""Framework-succession orchestration: detect → look up → preview IN_PLACE shims.

The IN_PLACE strategy reuses the codemod pipeline (patterns via ``apply_to_repo``,
rules via ``apply_rules_to_repo``), always as a dry run. Writing the shims goes
through a migration plan (``in_place_previews`` → ``create_migration_plan``) and
``pymolt apply``, like every other source change. TRANSPLANT edges are returned for
the caller to render as a plan (S3) and are never auto-applied here.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pymolt.codemods.apply import apply_rules_to_repo, apply_to_repo, preview_mixed_repo
from pymolt.codemods.models import CodemodRunResult, FilePreview
from pymolt.codemods.service import _merge_run_results
from pymolt.strategic.succession.detect import detect_frameworks
from pymolt.strategic.succession.models import SuccessionEdge


def run_succession(
    project_dir: str | Path,
    *,
    edges_for: Callable[[list[str]], list[SuccessionEdge]] | None = None,
    progress: Callable[[str], None] | None = None,
) -> tuple[list[SuccessionEdge], CodemodRunResult | None]:
    """Detect the project's frameworks, look up their succession edges, preview IN_PLACE
    shims.

    Returns ``(edges, preview_result)``. Nothing is written. ``preview_result`` is
    ``None`` when nothing was detected or no in-place shim applies. Transplant edges
    ride along in ``edges`` for the caller to render but are never applied here.
    ``edges_for`` maps framework requirements to edges; it defaults to pymolt's curated
    knowledge (``pymolt.knowledge.succession_edges``).
    """
    frameworks = detect_frameworks(project_dir)
    if not frameworks:
        return [], None
    if edges_for is None:
        from pymolt.knowledge import succession_edges as edges_for
    if progress:
        progress(f"Looking up succession paths for {len(frameworks)} framework(s) in "
                 "pymolt's curated knowledge…")
    edges = edges_for(frameworks)

    patterns = [p for e in edges if e.is_in_place for p in e.patterns]
    rules = [r for e in edges if e.is_in_place for r in e.rules]
    if not (patterns or rules):
        return edges, None

    if progress:
        progress(
            f"Previewing {len(patterns) + len(rules)} in-place shim(s) under {project_dir} "
            "via LibCST…"
        )
    result: CodemodRunResult | None = None
    if patterns:
        result = apply_to_repo(project_dir, patterns, write=False)
    if rules:
        rules_result = apply_rules_to_repo(project_dir, rules, write=False)
        result = rules_result if result is None else _merge_run_results(result, rules_result)
    return edges, result


def in_place_previews(project_dir: str | Path, edges: list[SuccessionEdge],
                      on_step=None) -> list[FilePreview]:
    """The in-place shims as before/after per file, verified rules only: what a plan holds.
    ``on_step`` observes each recipe's change per file (the plan's steps)."""
    patterns = [p for e in edges if e.is_in_place for p in e.patterns]
    rules = [r for e in edges if e.is_in_place for r in e.rules]
    if not (patterns or rules):
        return []
    return preview_mixed_repo(project_dir, rules, patterns, verified_only=True,
                              on_step=on_step)
