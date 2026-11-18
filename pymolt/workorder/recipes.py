"""Running a step's recipe on a text (HOST side): what re-basing (stage 4) and the re-run
oracle of ``depends_on`` (a test) need. Nothing here writes a file."""

from __future__ import annotations

from pymolt.workorder.models import StepRecipe


class NotRerunnable(ValueError):
    """The recipe cannot be re-run: a ``whole-file`` change, or a manifest pin (a line rewrite
    of a manifest, not of Python source)."""


def run_recipe(recipe: StepRecipe, source: str) -> str:
    """``source`` after the recipe, unrestricted (every site it matches)."""
    from pymolt.codemods.apply import apply_patterns
    from pymolt.codemods.rules import apply_rule_detailed

    if recipe.kind == "rule" and recipe.rule is not None:
        return apply_rule_detailed(source, recipe.rule).new_source
    if recipe.kind in ("pattern", "move-group"):
        return apply_patterns(source, list(recipe.patterns))[0]
    raise NotRerunnable(f"a {recipe.kind} recipe is not re-run")
