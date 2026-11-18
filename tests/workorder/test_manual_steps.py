"""Risks to steps (design 1.5.4): every risk gets a step_id; the ones the plan leaves become
manual steps grouped per (scope, package, symbol, fact kind, parameter); H5 items, rule
advisories and compile findings become steps too."""

from __future__ import annotations

from pymolt.codemods.apply import preview_rules_repo
from pymolt.codemods.client import DependencyMigration
from pymolt.codemods.models import recipe_key
from pymolt.codemods.rules import CodemodRule
from pymolt.migration_plan import PlannedAdvisory, PlannedManualItem, create_migration_plan
from pymolt.verify.facts import CallWarning
from pymolt.verify.predict import PredictedRisk, PredictionReport, RiskSite, RiskTier
from pymolt.workorder.builder import StepRecorder
from pymolt.workorder.manual import link_and_manual_steps
from tests.workorder.test_step_builder import APPEND, _project, _pymolt, _walk
from tests.workorder.test_step_builder import _plan as builder_plan

PANDAS = [DependencyMigration("pandas", "1.5.3", "2.2.3")]


def _risk(symbol, file, line, func, *, tier=RiskTier.CERTAIN, fact=None, fixed=False,
          scope="dependency", package="pandas", calls=1, warnings=(), fixed_by=(), rid=None):
    return PredictedRisk(id=rid or f"{abs(hash((symbol, file, line))) % 10**12:012d}",
                         tier=tier, symbol=symbol, package=package,
                         site=RiskSite(file=file, func=func, line=line), reason="why",
                         fact=fact, facts=[fact] if fact else [], calls=calls,
                         fixed_by_plan=fixed, fixed_by=list(fixed_by), scope=scope,
                         warnings=list(warnings),
                         basis="trace" if line else "static")


def _automated(tmp_path):
    root = _project(tmp_path)
    recorder = StepRecorder()
    plan = builder_plan(root, recorder, _walk(root, recorder))
    return root, plan


def test_every_risk_gets_a_step(tmp_path):
    root, plan = _automated(tmp_path)
    automated = _pymolt(plan)
    removed = {"kind": "REMOVED", "symbol": "pandas.DataFrame.ix", "successor": None}
    default = {"kind": "DEFAULT", "symbol": "pandas.DataFrame.mean", "param": "numeric_only",
               "old": "None", "new": "False"}
    risks = PredictionReport(risks=[
        _risk("pandas.core.series.Series.append", "app.py", 7, "combine", fixed=True,
              fact={"kind": "REMOVED", "symbol": "pandas.Series.append"}),
        _risk("pandas._testing.assert_frame_equal", "b_other.py", 5, "check", fixed=True,
              fact={"kind": "MOVED", "symbol": "pandas.util.testing.assert_frame_equal"}),
        _risk("pandas.DataFrame.ix", "app.py", 11, "frame", fact=removed, calls=3),
        _risk("pandas.DataFrame.ix", "c_only.py", None, "join", fact=removed,
              tier=RiskTier.LOW),
        _risk("pandas.DataFrame.mean", "app.py", 11, "frame", tier=RiskTier.HIGH, fact=default,
              warnings=[CallWarning(category="FutureWarning", message="numeric_only", count=2)]),
        _risk("pandas.DataFrame.sum", "app.py", 11, "frame", tier=RiskTier.HIGH,
              warnings=[CallWarning(category="FutureWarning", message="sum", count=1)]),
        _risk("imp.find_module", "app.py", 1, "<module>", scope="python", package=None,
              fact={"kind": "REMOVED", "symbol": "imp.find_module",
                    "evidence": ["hint: use importlib.util.find_spec"]}),
    ])
    steps = link_and_manual_steps(automated, root=root, changes=plan.changes, risks=risks,
                                  manual_items=[], advisories=[], compile_findings=[],
                                  dependencies=PANDAS, python_move=("3.11", "3.12"))
    by_id = {s.id: s for s in steps}
    append, move, ix, ix_static, mean, warned, imp = risks.risks
    # The fixed risks link to the pymolt step that rewrote their statement or binding import.
    assert append.step_id == "mech-1" and move.step_id == "mech-2"
    assert append.id in by_id["mech-1"].why.risk_ids
    assert by_id["mech-1"].why.tier == "certain"
    assert any(a.stmt == "return assert_frame_equal(a, b)" for a in by_id["mech-2"].where)
    # The rest: manual steps, by phase, the ix sites grouped in one step.
    # Within a phase: by tier, then by the first site (app.py:1 before app.py:11).
    assert [(s.id, s.type, s.executor) for s in steps if s.executor != "pymolt"] == [
        ("sig-1", "python-manual", "agent"), ("sig-2", "api-change", "agent"),
        ("beh-1", "behaviour-change", "person"), ("beh-2", "behaviour-change", "person")]
    sig = by_id["sig-2"]
    assert ix.step_id == ix_static.step_id == "sig-2"
    assert sig.why.risk_ids == [ix.id, ix_static.id] and sig.why.tier == "certain"
    assert sig.why.baseline.calls == 4 and sig.why.baseline.sites == 2
    assert [a.stmt for a in sig.where] == ["return df.ix[0]"]     # the static site has no line
    assert (sig.claim, sig.basis, sig.package, sig.from_, sig.to) == (
        "preserve", "manual", "pandas", "1.5.3", "2.2.3")
    assert sig.title == "`DataFrame.ix`: removed in pandas 2.2.3"
    assert sig.do.manual.summary.startswith("No successor is known")
    assert imp.step_id == "sig-1" and by_id["sig-1"].scope == "python"
    assert "importlib.util.find_spec" in by_id["sig-1"].do.manual.summary
    assert mean.step_id == "beh-1"
    assert by_id["beh-1"].why.delta.model_dump() == {
        "kind": "DEFAULT", "param": "numeric_only", "old": "None", "new": "False"}
    assert by_id["beh-1"].why.baseline.warnings == ["FutureWarning: numeric_only"]
    assert warned.step_id == "beh-2" and by_id["beh-2"].title.endswith("warned in the baseline")
    assert all(r.step_id for r in risks.risks)
    # Keys name the kind of change: the same group in a re-plan keeps its key.
    again = link_and_manual_steps(automated, root=root, changes=plan.changes,
                                  risks=risks.model_copy(deep=True), manual_items=[],
                                  advisories=[], compile_findings=[], dependencies=PANDAS,
                                  python_move=("3.11", "3.12"))
    assert [s.key for s in again] == [s.key for s in steps]


def test_h5_items_advisories_and_compile_findings_become_steps(tmp_path):
    root, plan = _automated(tmp_path)
    items = [PlannedManualItem(path="app.py", lines=[11], sites=1,
                               rule="template:$DF.ix[$K]→$DF.loc[$K]",
                               reason="not applied: it is not verified; rewrite these sites "
                                      "by hand or leave them"),
             PlannedManualItem(rule="pandas-x: a → b", reason="downgraded, so never applied")]
    advisories = [
        PlannedAdvisory(path="app.py", line=7, column=11, severity="precondition",
                        reason="applied, but requires x; verify before shipping",
                        rule=recipe_key(APPEND)),
        PlannedAdvisory(path="c_only.py", line=5, column=11, severity="not-applied",
                        reason="matched call has non-simple receiver", rule=recipe_key(APPEND)),
    ]
    findings = [{"file": "app.py", "line": 14, "kind": "SyntaxWarning",
                 "message": "invalid escape sequence '\\d'", "base": None},
                {"file": "c_only.py", "line": 5, "kind": "SyntaxWarning",
                 "message": "invalid escape sequence '\\w'", "base": None}]
    steps = link_and_manual_steps(_pymolt(plan), root=root, changes=plan.changes, risks=None,
                                  manual_items=items, advisories=advisories,
                                  compile_findings=findings, python_move=("3.11", "3.12"))
    manual = [(s.id, s.type, s.executor) for s in steps if s.executor != "pymolt"]
    assert manual == [("mech-4", "manual-rewrite", "agent"), ("mech-5", "manual-rewrite", "agent"),
                      ("mech-6", "manual-rewrite", "agent"), ("sig-1", "python-manual", "agent"),
                      ("beh-1", "review", "person")]
    by_id = {s.id: s for s in steps}
    assert by_id["mech-4"].do.manual.suggestion == "$DF.loc[$K]"
    assert [a.stmt for a in by_id["mech-4"].where] == ["return df.ix[0]"]
    assert by_id["mech-5"].where == [] and by_id["mech-5"].effort.files == 0
    assert by_id["mech-6"].files == ["c_only.py"]
    assert by_id["sig-1"].title == "SyntaxWarning on Python 3.12: 2 site(s)"
    assert by_id["sig-1"].effort.files == 2
    assert by_id["beh-1"].title.startswith("Review the rewrite of `$S.append(")


LOOKUP = CodemodRule(
    library="pandas", from_version="1.0.0", to_version="1.2.0",
    match="$DF.lookup($ROWS, $COLS)",
    rewrite=["_ridx = $DF.index.get_indexer($ROWS)", "_cidx = $DF.columns.get_indexer($COLS)",
             "$RESULT = $DF.to_numpy()[_ridx, _cidx]"],
    condition="simple_name_args", confidence="verified")


def test_a_plans_advisories_name_their_rule(tmp_path):
    (tmp_path / "m.py").write_text("def f(df, r, c):\n    return df.lookup(r, c)\n",
                                   encoding="utf-8")
    recorder = StepRecorder()
    previews = preview_rules_repo(tmp_path, [LOOKUP], on_step=recorder.record,
                                  verified_only=True)
    assert previews[0].advisories[0].rule == recipe_key(LOOKUP)
    plan = create_migration_plan(tmp_path, previews, [], target_python="3.12",
                                 source_manifest="requirements.txt", target_manifest_path=None,
                                 files_scanned=1, predict=False, recorder=recorder)
    assert plan.advisories[0].rule == recipe_key(LOOKUP)
    (step,) = plan.steps
    assert (step.type, step.executor, step.files) == ("manual-rewrite", "agent", ["m.py"])
