"""``depends_on`` from effect overlap (design 1.5.3), and its re-run oracle: a step with no
dependency on an earlier one reproduces its effect when that earlier step is left out."""

from __future__ import annotations

import pytest

from pymolt.codemods.apply import preview_repo, preview_rules_repo
from pymolt.codemods.models import CodemodPattern
from pymolt.codemods.rules import CodemodRule
from pymolt.migration_plan import create_migration_plan
from pymolt.workorder.builder import StepRecorder, replay_changes
from pymolt.workorder.depends import (
    CROWDED,
    assign_depends_on,
    depends,
    independent_pairs,
    provided_names,
    used_names,
)
from pymolt.workorder.edits import make_edit
from pymolt.workorder.models import PlanStep, StepDo, StepRecipe
from pymolt.workorder.recipes import run_recipe


def _rule(match, rewrite):
    return CodemodRule(library="pkg", from_version="1", to_version="2", match=match,
                       rewrite=[rewrite], confidence="verified")


FOO_TO_BAR = _rule("$X.foo($A)", "$X.bar($A)")
BAR_TO_BAZ = _rule("$X.bar($A)", "$X.baz($A)")
QUX = _rule("$X.qux($A)", "$X.quux($A)")

CHAINED = ("def f(o, a):\n    return o.foo(a)\n\n\ndef g(o, a):\n    return o.qux(a)\n")


def _plan(root, previews, recorder):
    return create_migration_plan(root, previews, [], target_python="3.12",
                                 source_manifest="requirements.txt", target_manifest_path=None,
                                 files_scanned=1, predict=False, recorder=recorder)


def _rules_plan(tmp_path, rules, files):
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    recorder = StepRecorder()
    previews = preview_rules_repo(tmp_path, rules, on_step=recorder.record)
    return _plan(tmp_path, previews, recorder)


def _attrs_plan(tmp_path, text, patterns):
    """Attribute rewrites that add the import they need, as separate steps (patterns are not
    restricted to lines, unlike the python fixes)."""
    (tmp_path / "app.py").write_text(text, encoding="utf-8")
    recorder = StepRecorder()
    previews = preview_repo(tmp_path, patterns, on_step=recorder.record)
    return _plan(tmp_path, previews, recorder)


def _rerun_without(plan, left_out, step, file):
    """``step``'s effect in ``file`` when ``left_out`` never ran: every earlier step that
    changes the file re-run in order on T0, then ``step``."""
    (change,) = [c for c in plan.changes if c.path == file]
    text = change.old_source
    for earlier in plan.steps:
        if earlier.id == step.id:
            break
        if earlier.id != left_out.id and earlier.edit_for(file) is not None:
            text = run_recipe(earlier.do.recipe, text)
    return make_edit(file, text, run_recipe(step.do.recipe, text)).effect


def _oracle(plan):
    """The re-run oracle over every independent pair; returns how many pairs it checked."""
    pairs = independent_pairs(plan.steps)
    for left_out, step, file in pairs:
        effect = _rerun_without(plan, left_out, step, file)
        assert effect.same_as(step.edit_for(file).effect), (
            f"{step.id} has no dependency on {left_out.id}, yet without it its effect in {file} "
            "differs")
    return len(pairs)


def test_a_step_that_rewrites_what_an_earlier_step_wrote_depends_on_it(tmp_path):
    plan = _rules_plan(tmp_path, [FOO_TO_BAR, BAR_TO_BAZ, QUX], {"m.py": CHAINED})
    assert replay_changes(plan.changes, plan.steps)["m.py"] == plan.changes[0].new_source
    foo, bar, qux = plan.steps
    assert (foo.depends_on, bar.depends_on, qux.depends_on) == ([], [foo.id], [])
    # The oracle holds for the independent pairs, and has teeth for the dependent one.
    assert _oracle(plan) == 2
    assert _rerun_without(plan, foo, bar, "m.py").is_empty()


def test_steps_in_different_files_never_depend_on_each_other(tmp_path):
    plan = _rules_plan(tmp_path, [FOO_TO_BAR, QUX],
                       {"a.py": "def f(o, a):\n    return o.foo(a)\n",
                        "b.py": "def g(o, a):\n    return o.qux(a)\n"})
    foo, qux = plan.steps
    assert [e.file for e in foo.edits] == ["a.py"] and [e.file for e in qux.edits] == ["b.py"]
    assert qux.depends_on == [] and independent_pairs(plan.steps) == []


ABC = ("import collections\n"
       "\n"
       "A = collections.Iterable\n"
       "B = collections.Mapping\n")


def _attr(name):
    return CodemodPattern(old_qualname=f"collections.{name}",
                          new_qualname=f"collections.abc.{name}", kind="rewrite-attr",
                          requires_import="collections.abc")


def test_a_step_that_needs_an_import_an_earlier_step_inserted_depends_on_it(tmp_path):
    plan = _attrs_plan(tmp_path, ABC, [_attr("Iterable"), _attr("Mapping")])
    (change,) = plan.changes
    assert change.new_source.count("import collections.abc") == 1
    iterable, mapping = plan.steps
    assert iterable.edit_for("app.py").effect.imports_inserted
    assert not mapping.edit_for("app.py").effect.imports_inserted
    assert mapping.depends_on == [iterable.id]
    assert _oracle(plan) == 0


def test_an_import_the_file_already_had_makes_no_dependency(tmp_path):
    text = "import collections\nimport collections.abc\n\nA = collections.Iterable\n" \
           "B = collections.Mapping\n"
    plan = _attrs_plan(tmp_path, text, [_attr("Iterable"), _attr("Mapping")])
    iterable, mapping = plan.steps
    assert mapping.depends_on == []
    assert _oracle(plan) == 1


def test_the_builder_fixture_has_no_dependency_and_the_oracle_agrees(tmp_path):
    from tests.workorder.test_step_builder import _plan as builder_plan
    from tests.workorder.test_step_builder import _project, _walk

    root = _project(tmp_path)
    recorder = StepRecorder()
    plan = builder_plan(root, recorder, _walk(root, recorder))
    assert [s.depends_on for s in plan.steps] == [[], [], []]
    assert _oracle(plan) >= 3


# ── hand-made edits: the inserted-import key and the fallback ────────────────────────────────


def _step(n, edit):
    return PlanStep(id=f"mech-{n}", key=f"{n:010d}", phase="mechanical", type="codemod",
                    title=str(n), executor="pymolt",
                    do=StepDo(recipe=StepRecipe(kind="whole-file")), edits=[edit])


def test_two_steps_inserting_the_same_import_depend_in_order():
    base = "import os\n\nX = 1\nY = 2\n"
    first = make_edit("m.py", base, "import os\nimport collections.abc\n\nX = 10\nY = 2\n")
    second = make_edit("m.py", base, "import os\nimport collections.abc\n\nX = 1\nY = 20\n")
    assert first.effect.imports_inserted == second.effect.imports_inserted
    assert depends(first, second) == "needs an import it inserted"


def test_an_effect_with_token_keys_falls_back_to_the_scope():
    # Python 2 source: no grammar parses it, so its keys are tokens.
    old = "def f():\n    print 'a'\n    print 'b'\n\n\ndef g():\n    print 'c'\n"
    in_f = make_edit("m.py", old, old.replace("'a'", "'A'"))
    in_f2 = make_edit("m.py", old, old.replace("'b'", "'B'"))
    in_g = make_edit("m.py", old, old.replace("'c'", "'C'"))
    assert in_f.effect.key_kind == "tokens"
    assert depends(in_f, in_f2) is not None            # same scope
    assert depends(in_f, in_g) is None                  # another scope


def test_more_than_crowded_steps_in_one_scope_depend_on_the_earlier_ones_there():
    lines = [f"    v{i} = {i}\n" for i in range(CROWDED + 1)]
    old = "def f():\n" + "".join(lines) + "\n\ndef g():\n    a = 1\n    b = 2\n"
    steps = []
    for i in range(CROWDED + 1):
        steps.append(_step(i + 1, make_edit("m.py", old,
                                            old.replace(f"v{i} = {i}", f"v{i} = {i + 100}"))))
    steps.append(_step(20, make_edit("m.py", old, old.replace("a = 1", "a = 10"))))
    steps.append(_step(21, make_edit("m.py", old, old.replace("b = 2", "b = 20"))))
    why = assign_depends_on(steps)
    assert steps[CROWDED].depends_on == [s.id for s in steps[:CROWDED]]
    assert steps[1].depends_on == ["mech-1"]
    assert steps[-1].depends_on == [] and steps[-2].depends_on == []   # g: not crowded
    assert "changed by more than 8 steps" in why[("mech-2", "mech-1")]


@pytest.mark.parametrize("text, names", [
    ("import collections.abc", {"collections", "collections.abc"}),
    ("import numpy as np", {"np"}),
    ("from collections.abc import Iterable as It, Mapping", {"It", "Mapping"}),
])
def test_what_an_import_provides(text, names):
    assert provided_names(text) == names


def test_what_a_statement_uses():
    assert {"collections", "collections.abc", "collections.abc.Mapping", "B"} <= used_names(
        "B = collections.abc.Mapping")
    assert "x" in used_names("if x.y:")            # a compound header alone
