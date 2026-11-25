"""A step's key names a kind of change across re-plans, so it has to be unique within a plan."""

from __future__ import annotations

from collections import Counter

from pymolt.codemods.models import recipe_key
from pymolt.migration_plan import PlannedAdvisory, PlannedManualItem
from pymolt.workorder.manual import link_and_manual_steps
from tests.workorder.test_manual_steps import (
    APPEND,
    PANDAS,
    _automated,
    _every_kind_of_risk,
)
from tests.workorder.test_render import _plan as golden_plan
from tests.workorder.test_step_builder import _pymolt


def _collisions(steps) -> dict[str, list[tuple[str, str]]]:
    counts = Counter(s.key for s in steps)
    return {k: [(s.id, s.type) for s in steps if s.key == k] for k, n in counts.items() if n > 1}


def _link(tmp_path, **kwargs):
    root, plan = _automated(tmp_path)
    defaults = dict(risks=None, manual_items=[], advisories=[], compile_findings=[],
                    dependencies=PANDAS, python_move=("3.11", "3.12"))
    steps = link_and_manual_steps(_pymolt(plan), root=root, changes=plan.changes,
                                  **{**defaults, **kwargs})
    return steps


def test_keys_are_unique_in_the_golden_plan(tmp_path):
    _root, plan = golden_plan(tmp_path)
    assert {s.executor for s in plan.steps} == {"pymolt", "agent", "person"}
    assert _collisions(plan.steps) == {}


def test_keys_are_unique_with_every_kind_of_risk(tmp_path):
    steps = _link(tmp_path, risks=_every_kind_of_risk())
    assert {s.executor for s in steps} >= {"pymolt", "agent", "person"}
    assert _collisions(steps) == {}


def test_keys_are_unique_with_manual_items_advisories_and_compile_findings(tmp_path):
    items = [PlannedManualItem(path="app.py", lines=[11], sites=1,
                               rule="template:$DF.ix[$K]→$DF.loc[$K]", reason="not applied"),
             PlannedManualItem(rule="pandas-x: a → b", reason="downgraded, so never applied")]
    advisories = [
        PlannedAdvisory(path="app.py", line=7, column=11, severity="precondition",
                        reason="applied, but requires x", rule=recipe_key(APPEND)),
        PlannedAdvisory(path="c_only.py", line=5, column=11, severity="not-applied",
                        reason="matched call has non-simple receiver", rule=recipe_key(APPEND)),
    ]
    findings = [{"file": "app.py", "line": 14, "kind": "SyntaxWarning",
                 "message": "invalid escape sequence '\\d'", "base": None},
                {"file": "c_only.py", "line": 5, "kind": "SyntaxWarning",
                 "message": "invalid escape sequence '\\w'", "base": None}]
    steps = _link(tmp_path, manual_items=items, advisories=advisories,
                  compile_findings=findings)
    assert {s.executor for s in steps} >= {"pymolt", "agent", "person"}
    assert _collisions(steps) == {}


def test_keys_are_unique_with_both_risks_and_h5_items(tmp_path):
    items = [PlannedManualItem(path="app.py", lines=[11], sites=1,
                               rule="template:$DF.ix[$K]→$DF.loc[$K]", reason="not applied"),
             PlannedManualItem(rule="pandas-x: a → b", reason="downgraded, so never applied")]
    steps = _link(tmp_path, risks=_every_kind_of_risk(), manual_items=items)
    assert _collisions(steps) == {}
