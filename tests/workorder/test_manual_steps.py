"""Risks to steps (design 1.5.4): every risk gets a step_id; the ones the plan leaves become
manual steps grouped per (scope, package, symbol, fact kind, parameter); H5 items, rule
advisories and compile findings become steps too."""

from __future__ import annotations

import pytest

from pymolt.codemods.apply import preview_rules_repo
from pymolt.codemods.models import DependencyMigration, recipe_key
from pymolt.codemods.rules import CodemodRule
from pymolt.migration_plan import PlannedAdvisory, PlannedManualItem, create_migration_plan
from pymolt.verify.facts import CallWarning
from pymolt.verify.predict import PredictedRisk, PredictionReport, RiskSite, RiskTier
from pymolt.workorder.apply import StepRefusal
from pymolt.workorder.builder import StepRecorder
from pymolt.workorder.manual import T0, link_and_manual_steps
from pymolt.workorder.record import check_claim
from pymolt.workorder.render import brief_data, plan_markdown
from pymolt.workorder.verify_effort import finish_steps
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


def _every_kind_of_risk() -> PredictionReport:
    """Two risks the pymolt steps fix, the ix sites (one with no line), a DEFAULT, a warning
    with no fact, and a Python removal."""
    removed = {"kind": "REMOVED", "symbol": "pandas.DataFrame.ix", "successor": None}
    default = {"kind": "DEFAULT", "symbol": "pandas.DataFrame.mean", "param": "numeric_only",
               "old": "None", "new": "False"}
    return PredictionReport(risks=[
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


def test_every_risk_gets_a_step(tmp_path):
    root, plan = _automated(tmp_path)
    automated = _pymolt(plan)
    risks = _every_kind_of_risk()
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
        "intended", "manual", "pandas", "1.5.3", "2.2.3")       # removed: another object
    assert by_id["beh-1"].claim == "preserve"
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


def test_a_re_plan_keeps_each_groups_key(tmp_path):
    """Keys name the kind of change: the same group in a re-plan keeps its key."""
    root, plan = _automated(tmp_path)
    risks = _every_kind_of_risk()

    def link(report):
        return link_and_manual_steps(_pymolt(plan), root=root, changes=plan.changes,
                                     risks=report, manual_items=[], advisories=[],
                                     compile_findings=[], dependencies=PANDAS,
                                     python_move=("3.11", "3.12"))

    steps = link(risks)
    again = link(risks.model_copy(deep=True))

    assert [s.key for s in again] == [s.key for s in steps]
    assert len({s.key for s in steps}) == len(steps)             # one key per group


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


# ── the default claim follows the successor kind (owner decision 2026-11-18) ──────────────

#: name -> (scope, symbol, fact or None, expected claim, expected expect, words in why.claim)
KINDS = {
    "moved": ("dependency", "pandas.util.testing.assert_series_equal",
              {"kind": "MOVED", "successor": "pandas.testing.assert_series_equal",
               "evidence": ["existence",
                            "same object reachable as pandas.testing.assert_series_equal"]},
              "preserve", None, "same object as `pandas.testing.assert_series_equal` (MOVED)"),
    "signature": ("dependency", "pandas.read_csv",
                  {"kind": "SIGNATURE", "param": "squeeze", "old": "POSITIONAL_OR_KEYWORD"},
                  "preserve", None, "stays the same object; only its signature changes"),
    "rebound": ("dependency", "pandas.io.json.json_normalize",
                {"kind": "SIGNATURE", "param": "meta", "evidence": [
                    "signature", "rebound: pandas.io.json.json_normalize -> "
                                 "pandas.io.json._normalize.json_normalize"]},
                "intended", "representation", "now names another object"),
    "removed": ("dependency", "pandas.DataFrame.ix", {"kind": "REMOVED", "successor": None},
                "intended", "representation", "removed and the same object is reachable "
                                              "nowhere else (REMOVED), so its successor is"),
    "removed with a successor": ("dependency", "pandas.DataFrame.iteritems",
                                 {"kind": "REMOVED", "successor": "pandas.DataFrame.items"},
                                 "intended", "representation",
                                 "its successor `pandas.DataFrame.items` is another object"),
    "deprecated with a successor": ("dependency", "pandas.DataFrame.swapaxes",
                                    {"kind": "DEPRECATED", "successor": "pandas.DataFrame.T"},
                                    "intended", "representation",
                                    "names `pandas.DataFrame.T` as the successor of the "
                                    "deprecated `DataFrame.swapaxes` (DEPRECATED)"),
    "deprecated": ("dependency", "pandas.DataFrame.applymap",
                   {"kind": "DEPRECATED", "successor": None}, "preserve", None,
                   "not loosened for lack of evidence: the delta names no successor"),
    "default": ("dependency", "pandas.DataFrame.mean",
                {"kind": "DEFAULT", "param": "numeric_only", "old": "None", "new": "False"},
                "preserve", None, "a behaviour change"),
    "warned": ("dependency", "pandas.DataFrame.sum", None, "preserve", None,
               "a behaviour change"),
    "python removed": ("python", "inspect.getargspec", {"kind": "REMOVED"}, "intended",
                       "representation", "so its successor is another object"),
    "python behaviour": ("python", "re.sub", {"kind": "BEHAVIOR"}, "preserve", None,
                         "a behaviour change"),
    "python, no fact kind": ("python", "SyntaxWarning", {"kind": None}, "preserve", None,
                             "not loosened for lack of evidence: no delta fact about a "
                             "successor"),
}


def test_the_default_claim_follows_the_kind_of_successor(tmp_path):
    root, plan = _automated(tmp_path)
    risks = []
    for i, (scope, symbol, fact, *_rest) in enumerate(KINDS.values()):
        risks.append(_risk(symbol, "app.py", 11, "frame", scope=scope,
                           package=None if scope == "python" else "pandas",
                           fact=None if fact is None else {"symbol": symbol, **fact},
                           rid=f"{i:012d}"))
    report = PredictionReport(risks=risks)
    steps = link_and_manual_steps(_pymolt(plan), root=root, changes=plan.changes, risks=report,
                                  manual_items=[], advisories=[], compile_findings=[],
                                  dependencies=PANDAS, python_move=("3.11", "3.12"))
    by_id = {s.id: s for s in steps}
    for risk, (name, (*_head, claim, expect, words)) in zip(report.risks, KINDS.items(),
                                                            strict=True):
        step = by_id[risk.step_id]
        got = (step.claim, step.basis, step.verify.expect, step.verify.judge)
        judge = "strict" if claim == "preserve" else "expectation"
        assert got == (claim, "manual", expect, judge), name
        assert words in step.why.claim, (name, step.why.claim)


def test_rules_and_compile_findings_keep_preserve_and_say_why(tmp_path):
    root, plan = _automated(tmp_path)
    items = [PlannedManualItem(path="app.py", lines=[11], sites=1,
                               rule="template:$DF.ix[$K]→$DF.loc[$K]", reason="not applied")]
    advisories = [PlannedAdvisory(path="app.py", line=7, column=11, severity="precondition",
                                  reason="applied, but requires x", rule=recipe_key(APPEND))]
    findings = [{"file": "app.py", "line": 14, "kind": "SyntaxWarning",
                 "message": "invalid escape sequence '\\d'", "base": None}]
    steps = link_and_manual_steps(_pymolt(plan), root=root, changes=plan.changes, risks=None,
                                  manual_items=items, advisories=advisories,
                                  compile_findings=findings, python_move=("3.11", "3.12"))
    manual = {s.type: s for s in steps if s.executor != "pymolt"}
    assert {t: (s.claim, s.verify.judge, s.verify.expect) for t, s in manual.items()} == {
        "manual-rewrite": ("preserve", "strict", None),
        "python-manual": ("preserve", "strict", None),
        "review": ("preserve", "strict", None)}
    assert manual["manual-rewrite"].why.claim.startswith("not loosened for lack of evidence")
    assert manual["python-manual"].why.claim.startswith("not loosened for lack of evidence")
    assert "pymolt's rewrite" in manual["review"].why.claim


def test_a_plan_keeps_the_representation_expectation_and_shows_why(tmp_path):
    """The whole plan build: ``finish_steps`` keeps the expectation, PLAN.md and the brief
    show the claim with its reason, and a recorded edit starts from it."""
    root, plan = _automated(tmp_path)
    risks = PredictionReport(risks=[_risk(
        "pandas.DataFrame.iteritems", "app.py", 11, "frame",
        fact={"kind": "REMOVED", "symbol": "pandas.DataFrame.iteritems",
              "successor": "pandas.DataFrame.items"})])
    steps = link_and_manual_steps(_pymolt(plan), root=root, changes=plan.changes, risks=risks,
                                  manual_items=[], advisories=[], compile_findings=[],
                                  dependencies=PANDAS)
    finish_steps(steps, risks, root=root, t0=T0(root, plan.changes))
    step = next(s for s in steps if s.executor == "agent")
    assert (step.claim, step.verify.judge, step.verify.expect) == (
        "intended", "expectation", "representation")
    full = plan.model_copy(update={"steps": steps})
    md = plan_markdown(full)
    assert f"| {step.id} | " in md and "| agent | intended (representation) |" in md
    assert "- **Claim:** intended (representation). The delta shows `DataFrame.iteritems` " \
           "is removed" in md
    assert "a change of type or representation only is drift" in md
    data = brief_data(full, step, root)
    assert (data["claim"], data["expect"]) == ("intended", "representation")
    assert "is another object" in data["claim_why"]
    assert data["constraints"][0].startswith("Keep the value of every call at these sites")
    # An agent's record carries the step's claim and expectation; it may tighten them only.
    assert check_claim(step, claim=None, expect=None, attested=False)[:2] == (
        "intended", "representation")
    assert check_claim(step, claim="preserve", expect=None, attested=False)[:2] == (
        "preserve", None)
    with pytest.raises(StepRefusal, match="loosens it"):
        check_claim(step, claim=None, expect="any", attested=False)


def test_a_compile_finding_and_its_risk_make_one_step(tmp_path):
    """QA H6: predict rates each compile finding as a python risk too. The risk used to become a
    step of its own ("`SyntaxWarning`: changed", no successor) beside the compile step, for the
    same sites; it now joins the compile step."""
    root, plan = _automated(tmp_path)
    findings = [{"file": "app.py", "line": 14, "kind": "SyntaxWarning",
                 "message": "invalid escape sequence '\\d'", "base": None},
                {"file": "c_only.py", "line": 5, "kind": "SyntaxWarning",
                 "message": "invalid escape sequence '\\w'", "base": None}]
    compiled = [_risk("SyntaxWarning", f["file"], f["line"], "", tier=RiskTier.LOW,
                      scope="python", package="python") for f in findings]
    other = _risk("imp.find_module", "app.py", 1, "<module>", scope="python", package=None,
                  fact={"kind": "REMOVED", "symbol": "imp.find_module"})
    risks = PredictionReport(risks=[*compiled, other])
    steps = link_and_manual_steps(_pymolt(plan), root=root, changes=plan.changes, risks=risks,
                                  manual_items=[], advisories=[], compile_findings=findings,
                                  python_move=("3.11", "3.12"))
    python = [s for s in steps if s.type == "python-manual"]
    assert [s.title for s in python] == ["`imp.find_module`: removed on Python 3.12",
                                         "SyntaxWarning on Python 3.12: 2 site(s)"]
    compile_step = python[1]
    assert compile_step.why.risk_ids == [r.id for r in compiled]
    assert {r.step_id for r in compiled} == {compile_step.id}
    assert compile_step.why.tier == "low" and other.step_id == python[0].id
    from pymolt.workorder.verify_effort import effort_of

    # The message says what to do: linking its risks does not make it "no successor known".
    assert effort_of(compile_step).size == "small"
