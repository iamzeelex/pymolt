"""The automated step builder: recipe chains from the preview walk become ordered steps whose
hunks rebuild every planned file exactly (the ``changes`` oracle, design 1.5.1-1.5.2)."""

from __future__ import annotations

import sys
from types import SimpleNamespace

from pymolt.anchors import StatementIndex
from pymolt.codemods.apply import apply_patterns, preview_mixed_repo, preview_repo
from pymolt.codemods.client import DependencyMigration
from pymolt.codemods.models import CodemodPattern, FilePreview, MoveGroup
from pymolt.codemods.rules import CodemodRule
from pymolt.delta.models import DeltaFact, DeltaReport
from pymolt.migration_plan import create_migration_plan
from pymolt.pyversion.fixes import apply_python_fixes, python_patterns
from pymolt.workorder.builder import StepRecorder, replay_changes
from pymolt.workorder.edits import apply_hunks, sha256

APPEND = CodemodRule(
    library="pandas", from_version="1.5.3", to_version="2.2.3",
    match="$S.append($OTHER, ignore_index=$IGN)",
    rewrite=["pd.concat([$S, $OTHER], ignore_index=$IGN)"], condition="simple_name_args",
    confidence="verified", evidence=["pandas 2.0.0 removed Series.append"])
UNVERIFIED = CodemodRule(
    library="pandas", from_version="1.5.3", to_version="2.2.3", match="$DF.ix[$K]",
    rewrite=["$DF.loc[$K]"], confidence="heuristic")


def _move(name, old="pandas.util.testing", new="pandas.testing"):
    return CodemodPattern(old_qualname=f"{old}.{name}", new_qualname=f"{new}.{name}",
                          kind="rewrite-import", confidence="high",
                          evidence=[f"pymolt.delta MOVED {old}.{name}"])


APP = ("import collections\n"
       "from pandas.util.testing import assert_frame_equal, makeDataFrame\n"
       "import pandas as pd\n"
       "\n"
       "\n"
       "def combine(s1, s2):\n"
       "    return s1.append(s2, ignore_index=True)\n"
       "\n"
       "\n"
       "def frame(df):\n"
       "    return df.ix[0]\n"
       "\n"
       "\n"
       "X = collections.Iterable\n")
OTHER = ("from pandas.util.testing import assert_frame_equal\n"
         "\n"
         "\n"
         "def check(a, b):\n"
         "    return assert_frame_equal(a, b)\n")
ONLY_RULE = ("import pandas as pd\n\n\ndef join(a, b):\n"
             "    return a.append(b, ignore_index=False)\n")


def _site(spelling, line, file="app.py"):
    return SimpleNamespace(spelling=spelling, module=spelling.split(".")[0], file=file,
                           line=line, kind="attr", reached_at="import", guard=None)


def _project(tmp_path):
    (tmp_path / "app.py").write_text(APP, encoding="utf-8")
    (tmp_path / "b_other.py").write_text(OTHER, encoding="utf-8")
    (tmp_path / "c_only.py").write_text(ONLY_RULE, encoding="utf-8")
    (tmp_path / "requirements-target.txt").write_text("pandas==2.2.3\n", encoding="utf-8")
    return tmp_path


def _walk(root, recorder):
    """The plan's walk: verified rules, then patterns (a move group that splits a multi-name
    import above the python site), then a python fix."""
    patterns = [_move("assert_frame_equal")]
    previews = preview_mixed_repo(root, [APPEND, UNVERIFIED], patterns, verified_only=True,
                                  on_step=recorder.record)
    recorder.note_packages({"pandas": [APPEND, UNVERIFIED, *patterns]})
    report = DeltaReport(package="python", from_version="3.11", to_version="3.12",
                         quality="complete", subject="python",
                         facts=[DeltaFact(symbol="collections.Iterable", kind="MOVED",
                                          successor="collections.abc.Iterable")])
    sites = [_site("collections.Iterable", 14)]
    fixes = [p for p in python_patterns(report, sites) if p.kind == "rewrite-attr"]
    outcome = apply_python_fixes(root, previews, fixes, sites, target="3.12",
                                 target_executable=sys.executable, on_step=recorder.record)
    return outcome


def _pymolt(plan):
    return [s for s in plan.steps if s.executor == "pymolt"]


def _plan(root, recorder, outcome):
    stage = SimpleNamespace(previews=outcome.previews, checks=outcome.checks, risks=None,
                            python={"from": "3.11", "to": "3.12"}, findings=[])
    return create_migration_plan(
        root, outcome.previews, [DependencyMigration("pandas", "1.5.3", "2.2.3")],
        target_python="3.12", source_manifest="requirements.txt",
        target_manifest_path=root / "requirements-target.txt", files_scanned=3,
        predict=False, recorder=recorder, python_stage=stage, python_move=("3.11", "3.12"))


def test_the_steps_rebuild_every_planned_file_exactly(tmp_path):
    root = _project(tmp_path)
    recorder = StepRecorder()
    plan = _plan(root, recorder, _walk(root, recorder))
    assert sorted(c.path for c in plan.changes) == ["app.py", "b_other.py", "c_only.py"]
    rebuilt = replay_changes(plan.changes, plan.steps)
    assert rebuilt == {c.path: c.new_source for c in plan.changes}
    # The order is the walk's: the rule, the move group, then the python fix.
    assert [(s.id, s.type, s.do.recipe.kind) for s in _pymolt(plan)] == [
        ("mech-1", "codemod", "rule"), ("mech-2", "codemod", "move-group"),
        ("mech-3", "python-fix", "pattern")]
    rule, move, fix = _pymolt(plan)
    # One step per recipe across every file it changes; the blocked rule is in none.
    assert [e.file for e in rule.edits] == ["app.py", "c_only.py"]
    assert [e.file for e in move.edits] == ["app.py", "b_other.py"]
    assert not any("ix" in k for s in _pymolt(plan) for k in s.do.recipe.keys)
    # Claims per recipe: a verified rule, a same-object move, a python fixer.
    assert [(s.claim, s.basis) for s in _pymolt(plan)] == [
        ("preserve", "verified-rule"), ("preserve", "same-object"),
        ("preserve", "python-fixer")]
    assert move.do.recipe.move == ("pandas.util.testing", "pandas.testing")
    assert len(move.do.recipe.patterns) == 1 and move.package == "pandas"
    assert (move.from_, move.to) == ("1.5.3", "2.2.3")
    assert rule.package == "pandas" and (rule.from_, rule.to) == ("1.5.3", "2.2.3")
    assert fix.scope == "python" and (fix.from_, fix.to) == ("3.11", "3.12")
    assert fix.runs_on_base is True
    assert fix.checks_at_plan == {"compile_target": "ok",
                                  "compile_base": "not checked: no base interpreter or runner"}
    assert all(s.effort.size == "none" for s in _pymolt(plan))
    # The blocked rule is left to an agent, after the pymolt steps of its phase.
    assert [(s.id, s.type, s.executor) for s in plan.steps if s.executor != "pymolt"] == [
        ("env-1", "target-env", "person"), ("mech-4", "manual-rewrite", "agent")]


def test_each_steps_anchors_are_made_on_its_own_before_text(tmp_path):
    root = _project(tmp_path)
    recorder = StepRecorder()
    plan = _plan(root, recorder, _walk(root, recorder))
    rule, move, fix = _pymolt(plan)
    t0 = APP
    t1 = apply_hunks(t0, rule.edit_for("app.py").hunks)
    t2 = apply_hunks(t1, move.edit_for("app.py").hunks)
    edit = fix.edit_for("app.py")
    assert edit.before_sha256 == sha256(t2)
    # The fix's old statement is anchored on T2 (after the split import), not on T0.
    index = StatementIndex.build(t2)
    (anchor,) = edit.anchors_old
    assert anchor.stmt == "X = collections.Iterable"
    assert index.resolve(anchor).ok and anchor.line == 15       # T0 had it on line 14
    assert move.edit_for("app.py").before_sha256 == sha256(t1)
    # ``where`` is on T0: the fix's statement there is on line 14.
    assert [(a.stmt, a.line) for a in fix.where] == [("X = collections.Iterable", 14)]
    # The move's split import: one replaced statement and one inserted import (T0's line 2
    # became two lines, so the fix's statement moved from line 14 to 15).
    effect = move.edit_for("app.py").effect
    assert len(effect.replace) == 1 and len(effect.imports_inserted) == 1


def test_a_recipe_seen_alone_first_still_follows_the_one_before_it(tmp_path):
    """b.py meets only r2; a later file meets r1 then r2: r1 is still ordered first."""
    r1 = CodemodPattern(old_qualname="pkg.a.one", new_qualname="pkg.a.uno", kind="rename-call",
                        confidence="verified")
    r2 = CodemodPattern(old_qualname="pkg.a.two", new_qualname="pkg.a.dos", kind="rename-call",
                        confidence="verified")
    (tmp_path / "b.py").write_text("from pkg.a import two\ntwo()\n", encoding="utf-8")
    (tmp_path / "c.py").write_text("from pkg.a import one, two\none()\ntwo()\n",
                                   encoding="utf-8")
    recorder = StepRecorder()
    previews = preview_repo(tmp_path, [r1, r2], on_step=recorder.record)
    plan = create_migration_plan(tmp_path, previews, [], target_python="3.12",
                                 source_manifest="requirements.txt", target_manifest_path=None,
                                 files_scanned=2, predict=False, recorder=recorder)
    assert [s.symbol.old for s in plan.steps] == ["pkg.a.one", "pkg.a.two"]
    assert replay_changes(plan.changes, plan.steps) == {
        c.path: c.new_source for c in plan.changes}


def test_previews_with_no_recorded_chain_keep_each_file_whole(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    preview = FilePreview(path=str(tmp_path / "a.py"), old_source="x = 1\n",
                          new_source="x = 2\n", sites=1,
                          patterns=[CodemodPattern(old_qualname="m.x", new_qualname="m.y",
                                                   kind="rename-call")])
    plan = create_migration_plan(tmp_path, [preview], [], target_python="3.12",
                                 source_manifest="requirements.txt", target_manifest_path=None,
                                 files_scanned=1, predict=False)
    (step,) = plan.steps
    assert step.do.recipe.kind == "whole-file" and not step.do.recipe.rerunnable
    assert "no recorded recipe chain" in step.why.evidence[0]
    assert step.claim is None and step.verify.judge == "none"
    assert replay_changes(plan.changes, plan.steps) == {"a.py": "x = 2\n"}


def test_a_chain_that_does_not_reach_the_preview_keeps_the_file_whole(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    recorder = StepRecorder()
    recorder.record("not a recipe", str(tmp_path / "a.py"), "x = 1\n", "x = 3\n")
    preview = FilePreview(path=str(tmp_path / "a.py"), old_source="x = 1\n",
                          new_source="x = 2\n", sites=1)
    plan = create_migration_plan(tmp_path, [preview], [], target_python="3.12",
                                 source_manifest="requirements.txt", target_manifest_path=None,
                                 files_scanned=1, predict=False, recorder=recorder)
    assert [s.do.recipe.kind for s in plan.steps] == ["whole-file"]
    assert "did not report" in plan.steps[0].why.evidence[0]


def test_the_observer_changes_nothing_the_walk_returns(tmp_path):
    root = _project(tmp_path)
    patterns = [_move("assert_frame_equal"), _move("makeDataFrame")]
    plain = preview_mixed_repo(root, [APPEND], patterns, verified_only=True)
    seen = []
    observed = preview_mixed_repo(root, [APPEND], patterns, verified_only=True,
                                  on_step=lambda *event: seen.append(event))
    assert [p.model_dump() for p in plain] == [p.model_dump() for p in observed]
    assert {type(e[0]).__name__ for e in seen} == {"CodemodRule", "MoveGroup"}

    def boom(*_event):
        raise RuntimeError("observer bug")

    broken = preview_mixed_repo(root, [APPEND], patterns, verified_only=True, on_step=boom)
    assert [p.model_dump() for p in broken] == [p.model_dump() for p in plain]


def test_a_move_group_is_reported_once_with_its_members_hits():
    seen = []
    source = "from pandas.util.testing import assert_frame_equal, makeDataFrame\n"
    new, hits = apply_patterns(source, [_move("assert_frame_equal"), _move("makeDataFrame")],
                               on_step=lambda *e: seen.append(e))
    ((group, before, after),) = seen
    assert isinstance(group, MoveGroup) and (before, after) == (source, new)
    assert dict(group.hits) == {"rewrite-import:pandas.util.testing.assert_frame_equal→"
                                "pandas.testing.assert_frame_equal": 1,
                                "rewrite-import:pandas.util.testing.makeDataFrame→"
                                "pandas.testing.makeDataFrame": 1}


def test_a_dropped_python_fix_reports_no_step(tmp_path, monkeypatch):
    root = _project(tmp_path)
    monkeypatch.setattr("pymolt.pyversion.fixes.recompile",
                        lambda files, exe, **k: ({p: "SyntaxError: boom" for p in files}, None))
    recorder = StepRecorder()
    outcome = _walk(root, recorder)
    assert "app.py" in outcome.dropped and "app.py" not in outcome.checks
    plan = _plan(root, recorder, outcome)
    assert [s.type for s in _pymolt(plan)] == ["codemod", "codemod"]
    assert replay_changes(plan.changes, plan.steps) == {
        c.path: c.new_source for c in plan.changes}
