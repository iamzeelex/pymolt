"""A schema-1 plan read as a schema-2 work order with one synthetic step, ``v1`` (design 1.8).

A schema-1 plan froze whole files and never recorded where one recipe ends and the next
begins, so it cannot be split into steps without a re-plan. Its one step is a pymolt
``codemod`` whose ``edits`` hold one whole-file hunk per change, with the effect and anchors
made on the plan's ``old_source``/``new_source``. ``apply .`` and ``apply --step v1`` act on it as
on the schema-1 plan. Risk ids stay as the plan saved them (``pymolt.risks/1`` ids keep their
line); a risk the plan fixed links to ``v1``.
"""

from __future__ import annotations

from typing import Any

from pymolt.workorder.edits import make_edit
from pymolt.workorder.models import (
    V1_STEP_ID,
    PlanStep,
    StepDo,
    StepEffort,
    StepRecipe,
    StepVerify,
    StepWhy,
    step_key,
)

_CLAIMS = ("same-object", "same-signature", "verified-rule", "python-fixer")


def read_v1(model: type, data: dict[str, Any]):
    """``data`` (a schema-1 plan's JSON) as a ``model`` (``MigrationPlan``) of schema 2."""
    from pymolt.migration_plan import PLAN_SCHEMA_VERSION

    plan = model.model_validate({**data, "schema_version": PLAN_SCHEMA_VERSION,
                                 "adapted_from": 1, "steps": []})
    if plan.changes:
        plan.steps = [v1_step(plan)]
        if plan.risks is not None:
            for risk in plan.risks.risks:
                if risk.fixed_by_plan and risk.step_id is None:
                    risk.step_id = V1_STEP_ID
    return plan


def _basis(claims: list[str]) -> str | None:
    """The one claim every recipe of the plan made; None when they differ or none made one
    (a ledger entry keeps each recipe's own claim)."""
    found = {c for c in claims if c in _CLAIMS}
    return found.pop() if len(found) == 1 else None


def v1_step(plan) -> PlanStep:
    """The synthetic step of a schema-1 plan (see the module docstring)."""
    base = (plan.python or {}).get("from")
    target = plan.target_python
    recipes = list(dict.fromkeys(r for change in plan.changes for r in change.recipes))
    claims = [c for change in plan.changes for c in change.claims.values()]
    basis = _basis(claims)
    edits = [make_edit(change.path, change.old_source, change.new_source,
                       python_before=base, python_after=target, whole_file=True)
             for change in plan.changes]
    flags = [c.runs_on_base for c in plan.changes if c.runs_on_base is not None]
    runs_on_base = (None if not flags else True if all(flags) else False)
    evidence = ["a schema-1 plan: it froze whole files, so its recipes are one step; "
                "`pymolt plan .` splits them into steps"]
    if len(set(claims)) > 1:
        evidence.append("its recipes make different claims: " + ", ".join(sorted(set(claims))))
    dependency = plan.dependencies[0] if len(plan.dependencies) == 1 else None
    files = len(plan.changes)
    return PlanStep(
        id=V1_STEP_ID,
        key=step_key("codemod", "v1|" + "|".join(sorted(recipes))),
        phase="mechanical", type="codemod",
        title=f"The schema-1 plan's rewrite: {files} file(s), {len(recipes)} recipe(s)",
        executor="pymolt", claim="preserve" if claims else None, basis=basis,
        runs_on_base=runs_on_base,
        scope="python" if claims and all(c == "python-fixer" for c in claims) else "dependency",
        package=dependency.name if dependency else None,
        from_=dependency.from_version if dependency else None,
        to=dependency.to_version if dependency else None,
        where=[a for e in edits for a in e.anchors_old],
        why=StepWhy(evidence=evidence),
        do=StepDo(recipe=StepRecipe(kind="whole-file", keys=recipes, sites="all")),
        edits=edits,
        verify=StepVerify(judge="strict" if claims else "none"),
        effort=StepEffort(size="none", sites=sum(c.sites for c in plan.changes), files=files,
                          why="pymolt applies it"),
    )
