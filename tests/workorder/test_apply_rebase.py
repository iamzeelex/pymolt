"""Re-basing (design 3.2 rule 3, 1.6): a step whose file moved on applies at its statements
when they still resolve and its recipe does there exactly what the plan reviewed."""
from __future__ import annotations

import pytest
from _stepwise import APP, RECIPES, events, ledger, plan_for, read, rename

from pymolt.codemods.models import CodemodPattern
from pymolt.migration_plan import rollback_migration
from pymolt.workorder.apply import StepRefusal, apply_steps, skip_step
from pymolt.workorder.rebase import rerun
from pymolt.workorder.rules import REBASE, STALE, decide

M = "app/m.py"
HAND = "import a\nimport os\n"


def test_a_re_base_after_a_skipped_earlier_step(tmp_path):
    """mech-1 is skipped, so mech-2 runs on T0, not on the T1 it was planned on: its statement
    resolves there and the recipe gives the same change. mech-3 (which rewrites mech-1's
    output) stays blocked until mech-1 applies, re-based in its turn."""
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    skip_step(tmp_path, plan, "mech-1", "later")

    first = apply_steps(tmp_path, plan)

    assert (first.applied, first.rebased) == (["mech-2"], ["mech-2"])
    assert first.blocked == {"mech-3": ["needs mech-1 (skipped, pymolt step)"]}
    assert read(tmp_path, M) == APP.replace("a.h(x)", "a.k(x)")
    second = apply_steps(tmp_path, plan, step="mech-1")
    assert second.rebased == ["mech-1"]
    third = apply_steps(tmp_path, plan)
    assert (third.applied, third.rebased) == (["mech-3"], [])     # its own before-text again
    assert read(tmp_path, M) == plan.changes[0].new_source
    [_skip, *applies] = events(tmp_path)
    assert [e[2] for e in applies] == ["mech-2", "mech-1", "mech-3"]


def test_out_of_order_steps_re_base_onto_each_other(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})

    assert apply_steps(tmp_path, plan, step="mech-2").rebased == ["mech-2"]
    assert apply_steps(tmp_path, plan, step="mech-1").rebased == ["mech-1"]
    assert apply_steps(tmp_path, plan, step="mech-3").rebased == []

    assert read(tmp_path, M) == plan.changes[0].new_source


def test_a_hand_edit_elsewhere_re_bases_every_step_and_keeps_the_edit(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    (tmp_path / M).write_text(APP.replace("import a\n", HAND), encoding="utf-8")

    result = apply_steps(tmp_path, plan)

    assert result.rebased == ["mech-1", "mech-2", "mech-3"]
    assert read(tmp_path, M) == plan.changes[0].new_source.replace("import a\n", HAND)
    # The entries are anchored on the texts the invocation wrote: a line further down.
    lines = {e["step_id"]: e["new_lines"] for e in ledger(tmp_path)["entries"]}
    assert lines == {"mech-1": [6, 6], "mech-2": [8, 8], "mech-3": [6, 6]}


def test_a_rolled_back_step_applies_again_re_based_with_a_new_epoch(tmp_path):
    """Row 7: the rollback ended epoch 1; a person edited the file meanwhile; the re-apply is
    epoch 3, anchored on the new text."""
    from pymolt.apply_ledger import in_effect_entries
    from pymolt.migration_state import MigrationReceipt

    plan = plan_for(tmp_path, {M: (APP, [rename("a.f", "a.g")])})
    apply_steps(tmp_path, plan)
    rollback_migration(tmp_path)
    (tmp_path / M).write_text(APP.replace("import a\n", HAND), encoding="utf-8")

    again = apply_steps(tmp_path, plan)

    assert (again.applied, again.rebased) == (["mech-1"], ["mech-1"])
    assert [e[:3] for e in events(tmp_path)] == [
        (1, "apply", "mech-1"), (2, "rollback", "mech-1"), (3, "apply", "mech-1")]
    entries, _note, _l = in_effect_entries(tmp_path, MigrationReceipt.load(tmp_path))
    assert [(e.epoch, e.new_lines) for e in entries] == [(3, (6, 6))]


def test_a_recipe_that_now_does_something_else_is_stale(tmp_path):
    """An import move renames every reference to the name it moves; a reference a person added
    since makes the change bigger than the reviewed one, so the step is stale."""
    move = CodemodPattern(old_qualname="a.old.thing", new_qualname="a.new.other",
                          kind="rewrite-import")
    source = "from a.old import thing\n\n\ndef run():\n    return thing(1)\n"
    plan = plan_for(tmp_path, {M: (source, [move])})
    [step] = [s for s in plan.steps if s.executor == "pymolt" and s.phase == "mechanical"]
    assert step.do.recipe.kind == "move-group"
    (tmp_path / M).write_text(source.replace("def run", "LATE = thing\n\n\ndef run"),
                              encoding="utf-8")

    with pytest.raises(StepRefusal, match="another change here than the plan reviewed"):
        apply_steps(tmp_path, plan)

    assert "from a.old import thing" in read(tmp_path, M)


def test_a_rebinding_since_the_plan_makes_the_recipe_change_nothing(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, [rename("a.f", "a.g")])})
    step = plan.step("mech-1")
    shadowed = APP.replace("def run(v):\n", "def run(v):\n    import b as a\n")

    decision = decide(step, step.edit_for(M), shadowed, rerun=rerun)

    assert decision.rule == STALE
    assert any("changes nothing" in r or "another change" in r for r in decision.reasons)


def test_a_dependency_rename_is_restricted_to_the_steps_statements(tmp_path):
    """A rename the recipe cannot be told where to stop runs on the whole text; only its change
    at the step's statement is kept. A call a person added elsewhere is left as it is."""
    plan = plan_for(tmp_path, {M: (APP, [rename("a.f", "a.g")])})
    step = plan.step("mech-1")
    extra = APP.replace("import a\n", "import a\nEARLY = a.f(0)\n")

    decision = decide(step, step.edit_for(M), extra, rerun=rerun)

    assert decision.rule == REBASE
    assert "EARLY = a.f(0)" in decision.after and "x = a.g(v)" in decision.after


def test_dry_run_names_a_re_based_step(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})

    result = apply_steps(tmp_path, plan, step="mech-2", dry_run=True)

    assert (result.applied, result.rebased) == (["mech-2"], ["mech-2"])
    assert read(tmp_path, M) == APP and events(tmp_path) == []
