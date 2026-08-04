"""Phase 3 — assess: resolve lock-first, compare baseline vs target, rank risk.

The compute core is UI-agnostic: :func:`run_assess` resolves both graphs, builds
the comparison rows and (optionally) the risk report, returning an
:class:`AssessResult`. No prompts, no printing — callers drive
this same core and own their own I/O. The CLI additionally reuses the pure
helpers (``resolve_target_graph``/``build_comparison_rows``) for its interactive
override loop.
"""

from pymolt.assess.service import (
    AssessResult,
    build_comparison_rows,
    extract_constraint,
    resolve_chosen_source,
    resolve_target_graph,
    run_assess,
    write_constraints_file,
)

__all__ = [
    "AssessResult",
    "build_comparison_rows",
    "extract_constraint",
    "resolve_chosen_source",
    "resolve_target_graph",
    "run_assess",
    "write_constraints_file",
]
