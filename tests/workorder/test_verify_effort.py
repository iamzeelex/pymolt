"""How to verify a step and how big it is (design 1.5.5)."""

from __future__ import annotations

import json

import pytest

from pymolt.verify.predict import PredictionReport, RiskTier
from pymolt.workorder.manual import T0, link_and_manual_steps
from pymolt.workorder.models import (
    ManualDo,
    PlanStep,
    StepDelta,
    StepDo,
    StepEffort,
    StepRecipe,
    StepWhy,
)
from pymolt.workorder.verify_effort import TESTS_NOTE, effort_of, finish_steps
from tests.workorder.test_manual_steps import PANDAS, _automated, _risk
from tests.workorder.test_step_builder import _pymolt


def _manual(step_type="api-change", *, sites=1, successor="pkg.new", kind="REMOVED",
            suggestion=None, executor="agent"):
    phase = {"behaviour-change": "behaviour", "review": "behaviour",
             "manual-rewrite": "mechanical"}.get(step_type, "signature")
    return PlanStep(id="sig-1" if phase == "signature" else "beh-1" if phase == "behaviour"
                    else "mech-9", key="0123456789", phase=phase, type=step_type, title="t",
                    executor=executor,
                    why=StepWhy(delta=StepDelta(kind=kind) if kind else None),
                    do=StepDo(manual=ManualDo(summary="s", successor=successor,
                                              suggestion=suggestion)),
                    effort=StepEffort(sites=sites, files=1))


@pytest.mark.parametrize("step, size, why", [
    (_manual("behaviour-change", kind="DEFAULT", executor="person"), "large", "behaviour"),
    (_manual(successor=None), "large", "no successor"),
    (_manual(sites=21), "large", "more than 20"),
    (_manual(kind="SIGNATURE"), "medium", "signature differs"),
    (_manual(sites=6), "medium", "6 to 20"),
    (_manual("review", successor=None, executor="person"), "medium", "a review"),
    (_manual(kind="MOVED"), "trivial", "one site"),
    (_manual(sites=3), "small", "up to 5"),
    (_manual("manual-rewrite", successor=None, suggestion="$DF.loc[$K]"), "small", "up to 5"),
    (_manual("manual-rewrite", successor=None), "large", "no successor"),
])
def test_the_effort_table(step, size, why):
    effort = effort_of(step)
    assert effort.size == size and why in effort.why


def test_a_pymolt_step_takes_no_effort():
    step = PlanStep(id="mech-1", key="0123456789", phase="mechanical", type="codemod",
                    title="t", executor="pymolt", do=StepDo(recipe=StepRecipe(kind="rule")),
                    effort=StepEffort(sites=40))
    assert effort_of(step).model_dump() == {"size": "none", "sites": 40, "files": 0,
                                            "why": "executor pymolt"}


def _trace(tmp_path, root, lines):
    """A baseline trace whose sidecar records which lines of c_only.py ran."""
    from pymolt.verify._backend import encode_lines

    trace = tmp_path / "baseline.jsonl"
    trace.write_text("", encoding="utf-8")
    meta = {"executed": {"v": 1, "granularity": "line", "complete": True, "files": {
        str(root / "c_only.py"): {"lines": encode_lines(lines)},
        str(root / "app.py"): {"lines": encode_lines({1, 2})}}}}
    (tmp_path / "baseline.jsonl.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return trace


@pytest.mark.parametrize("ran, coverage, uncovered", [
    ({5}, "ran-not-recorded", 0),       # a line of join() ran, the trace has nothing there
    ({1}, "not-run", 1),                # the file ran, join() did not
])
def test_verify_lists_each_site_with_its_baseline_calls_and_coverage(tmp_path, ran, coverage,
                                                                     uncovered):
    (tmp_path / "p").mkdir()
    root, plan = _automated(tmp_path / "p")
    removed = {"kind": "REMOVED", "symbol": "pandas.DataFrame.ix", "successor": None}
    risks = PredictionReport(risks=[
        _risk("pandas.core.series.Series.append", "app.py", 7, "combine", fixed=True,
              calls=2, fact={"kind": "REMOVED", "symbol": "pandas.Series.append"}),
        _risk("pandas.DataFrame.ix", "app.py", 11, "frame", fact=removed, calls=3),
        _risk("pandas.DataFrame.ix", "c_only.py", None, "join", fact=removed,
              tier=RiskTier.LOW),
        _risk("pandas.DataFrame.mean", "app.py", 11, "frame", tier=RiskTier.HIGH,
              fact={"kind": "DEFAULT", "symbol": "pandas.DataFrame.mean",
                    "param": "numeric_only", "old": "None", "new": "False"}),
    ])
    risks.risks[3].evidence.append("baseline: numeric_only left at its default at this site")
    t0 = T0(root, plan.changes, "3.11")
    steps = link_and_manual_steps(_pymolt(plan), root=root, changes=plan.changes,
                                  risks=risks, manual_items=[], advisories=[],
                                  compile_findings=[], dependencies=PANDAS, t0=t0)
    finish_steps(steps, risks, root=root, t0=t0, baseline_trace=_trace(tmp_path, root, ran),
                 baseline_command=["pytest", "-q"])
    by_id = {s.id: s for s in steps}
    rule = by_id["mech-1"]
    assert rule.verify.judge == "strict" and rule.verify.command == ["pytest", "-q"]
    site = next(s for s in rule.verify.sites if s.baseline_calls)
    assert (site.baseline_calls, site.coverage, site.file) == (2, "recorded", "app.py")
    assert rule.verify.tests is None and rule.verify.note == TESTS_NOTE
    assert rule.effort.size == "none"
    ix = by_id["sig-1"]
    assert [(s.file, s.baseline_calls, s.coverage) for s in ix.verify.sites] == [
        ("app.py", 3, "recorded"), ("c_only.py", 1, coverage)]
    assert ix.verify.uncovered == uncovered
    assert (ix.effort.size, ix.effort.sites) == ("large", 2)       # no successor is known
    assert ix.do.manual.before_after.before == "return df.ix[0]"
    mean = by_id["beh-1"]
    assert mean.effort.size == "large"
    assert mean.do.manual.pitfalls == [
        "numeric_only left at its default at this site",
        "old default None is decided at call time: passing it may not restore the old "
        "behaviour"]


def test_without_a_baseline_every_static_site_is_unknown(tmp_path):
    root, plan = _automated(tmp_path)
    risks = PredictionReport(risks=[_risk("pkg.f", "c_only.py", None, "join",
                                          tier=RiskTier.LOW,
                                          fact={"kind": "SIGNATURE", "symbol": "pkg.f",
                                                "param": "x", "old": "x=1", "new": "x"})])
    t0 = T0(root, plan.changes)
    steps = link_and_manual_steps(_pymolt(plan), root=root, changes=plan.changes, risks=risks,
                                  manual_items=[], advisories=[], compile_findings=[], t0=t0)
    finish_steps(steps, risks, root=root, t0=t0)
    (sig,) = [s for s in steps if s.executor != "pymolt"]
    assert [s.coverage for s in sig.verify.sites] == ["unknown"]
    assert sig.verify.uncovered == 0 and sig.verify.command is None
    assert sig.effort.size == "large"                               # no successor known
    assert sig.do.manual.signature.model_dump() == {"old": "x=1", "new": "x",
                                                    "diff": ["-x: x=1", "+x: x"]}
