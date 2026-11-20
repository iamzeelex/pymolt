"""Test oracles over a plan's steps: what a step's recipe does to a text, what the steps'
hunks rebuild, and which steps are independent (so they must commute)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pymolt.workorder.edits import apply_hunks, sha256
from pymolt.workorder.models import PlanStep, StepRecipe


def run_recipe(recipe: StepRecipe, source: str) -> str:
    """``source`` after the recipe, unrestricted (every site it matches)."""
    from pymolt.codemods.apply import apply_patterns
    from pymolt.codemods.rules import apply_rule_detailed

    if recipe.kind == "rule" and recipe.rule is not None:
        return apply_rule_detailed(source, recipe.rule).new_source
    if recipe.kind in ("pattern", "move-group"):
        return apply_patterns(source, list(recipe.patterns))[0]
    raise ValueError(f"a {recipe.kind} recipe is not re-run")


def replay_changes(changes: list[Any], steps: list[PlanStep]) -> dict[str, str]:
    """Each changed file's text after every pymolt code step, rebuilt from T0 by the steps'
    hunks in order: the ``changes`` oracle (it must equal ``new_source``)."""
    out = {c.path: c.old_source for c in changes}
    for step in steps:
        if step.executor != "pymolt" or step.phase != "mechanical":
            continue
        for edit in step.edits:
            if edit.file in out:
                if sha256(out[edit.file]) != edit.before_sha256:
                    raise ValueError(f"{step.id} does not start from the text before it in "
                                     f"{edit.file}")
                out[edit.file] = apply_hunks(out[edit.file], edit.hunks)
    return out


def independent_pairs(steps: Iterable[PlanStep]) -> list[tuple[PlanStep, PlanStep, str]]:
    """``(i, j, file)`` for every pair of pymolt steps in plan order that change one file and
    have no dependency: what the re-run oracle checks."""
    out = []
    items = [s for s in steps if s.executor == "pymolt" and s.phase != "env"]
    for j, sj in enumerate(items):
        for si in items[:j]:
            if si.id in sj.depends_on:
                continue
            for edit in sj.edits:
                if si.edit_for(edit.file) is not None:
                    out.append((si, sj, edit.file))
    return out
