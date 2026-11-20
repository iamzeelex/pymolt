"""Test helper: apply a whole plan in one call (what ``pymolt apply .`` does for its pymolt
steps), over ``pymolt.workorder.apply.apply_steps``."""

from __future__ import annotations

from pathlib import Path

from pymolt.migration_plan import MigrationPlan


def apply_migration_plan(
    project_dir: str | Path,
    plan: MigrationPlan,
    *,
    allow_no_baseline: bool = False,
    smoke: bool = False,
):
    """Apply every pymolt step of ``plan`` whose dependencies are in effect. Before the first
    write every changed file is compiled (``apply_check``); with ``smoke`` the touched modules
    are also imported in the target environment, before and after the write, and a module the
    write broke undoes it. Raises ``MigrationCheckError`` for either failure. Returns the
    receipt, whose ``outcome`` is ``already_applied`` when every step was in effect already
    (nothing written)."""
    from pymolt.migration_state import MigrationReceipt
    from pymolt.workorder.apply import apply_steps

    result = apply_steps(project_dir, plan, allow_no_baseline=allow_no_baseline, smoke=smoke)
    receipt = result.receipt or MigrationReceipt.load(project_dir)
    if receipt is not None:
        receipt.mark_outcome("applied" if result.outcome == "applied" else "already_applied")
    return receipt
