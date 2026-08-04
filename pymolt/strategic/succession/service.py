"""Framework-succession orchestration: detect → fetch → apply IN_PLACE shims.

The IN_PLACE strategy reuses the codemod apply pipeline verbatim (patterns via
``apply_to_repo``, rules via ``apply_rules_to_repo`` — which re-verifies and only
writes ``verified`` rules). TRANSPLANT edges are returned for the caller to render
as a plan (S3) and are never auto-applied here — honesty over silent best-effort.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pymolt.codemods.apply import apply_rules_to_repo, apply_to_repo
from pymolt.codemods.models import CodemodRunResult
from pymolt.codemods.service import _merge_run_results
from pymolt.strategic.succession.client import SuccessionClient
from pymolt.strategic.succession.detect import detect_frameworks
from pymolt.strategic.succession.models import SuccessionEdge


def run_succession(
    project_dir: str | Path,
    *,
    base_url: str,
    write: bool = False,
    client: SuccessionClient | None = None,
    progress: Callable[[str], None] | None = None,
) -> tuple[list[SuccessionEdge], CodemodRunResult | None]:
    """Detect the project's frameworks, fetch succession edges, apply IN_PLACE shims.

    Returns ``(edges, apply_result)``. ``apply_result`` is ``None`` when nothing was
    detected or no in-place shims applied (dry-run unless ``write``). Transplant edges
    ride along in ``edges`` for the caller to render but are never applied here.
    Raises ``SuccessionError`` if the service is unreachable (caller owns the UX).
    """
    frameworks = detect_frameworks(project_dir)
    if not frameworks:
        return [], None
    client = client or SuccessionClient(base_url)
    edges = client.fetch(frameworks, progress=progress)

    patterns = [p for e in edges if e.is_in_place for p in e.patterns]
    rules = [r for e in edges if e.is_in_place for r in e.rules]
    if not (patterns or rules):
        return edges, None

    if progress:
        progress(
            f"Applying {len(patterns) + len(rules)} in-place shim(s) under {project_dir} "
            "via LibCST…"
        )
    result: CodemodRunResult | None = None
    if patterns:
        result = apply_to_repo(project_dir, patterns, write=write)
    if rules:
        rules_result = apply_rules_to_repo(project_dir, rules, write=write)
        result = rules_result if result is None else _merge_run_results(result, rules_result)
    return edges, result
