"""The default claim by successor kind, end to end (owner decision 2026-11-18): the planner
builds an agent step from a delta fact, an agent records its edit under the step's claim, and
verify judges the real captures. A step moving to another object (``intended`` with the
expectation ``representation``) reports a representation-only change as DRIFT and fails a
different value or a new raise; a step keeping the same object (``preserve``) fails all three."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from _stepwise import project, with_steps

from pymolt.codemods.client import DependencyMigration
from pymolt.migration_plan import create_migration_plan
from pymolt.verify import service
from pymolt.verify.models import CaptureMode
from pymolt.verify.predict import PredictedRisk, PredictionReport, RiskSite, RiskTier
from pymolt.workorder.builder import StepRecorder
from pymolt.workorder.manual import T0, link_and_manual_steps
from pymolt.workorder.record import record_step
from pymolt.workorder.verify_effort import finish_steps

M = "app/m.py"
LINE = 6
APP = ("import json\n"
       "\n"
       "\n"
       "def go(v):\n"
       "    try:\n"
       "        y = json.loads(v, object_pairs_hook=list)\n"
       "    except TypeError:\n"
       "        y = None\n"
       "    return y\n")
RUN = ("import sys\n"
       "sys.path.insert(0, {root!r})\n"
       "from app.m import go\n"
       "go('{{\"a\": 1, \"b\": 2}}')\n")
OLD = "y = json.loads(v, object_pairs_hook=list)"
EDITS = {
    "representation": "y = json.loads(v, object_pairs_hook=tuple)",    # the same pairs
    "value": "y = json.loads(v, object_pairs_hook=list, parse_int=str)",  # '1', not 1
    "raise": "y = json.loads(v, object_pairs_hook=list, cls=1)",          # TypeError
}
FACTS = {
    "another object": {"kind": "REMOVED", "symbol": "json.loads", "successor": None},
    "same object": {"kind": "SIGNATURE", "symbol": "json.loads", "param": "object_pairs_hook",
                    "old": "KEYWORD_ONLY", "new": None},
}
EXPECTED = {
    "another object": {"representation": "drift", "value": "fail", "raise": "fail"},
    "same object": {"representation": "fail", "value": "fail", "raise": "fail"},
}


def _capture(root: Path, when: str):
    script = root / "run.py"
    script.write_text(RUN.format(root=str(root)), encoding="utf-8")
    return service.capture_named_trace(root, when, CaptureMode.LIVE_COMMAND,
                                       command=[sys.executable, str(script)], target="json",
                                       privacy="values")


def _planned(root: Path, fact: dict):
    """A project calling ``json.loads``, its baseline, and a plan whose one agent step the
    planner built from ``fact`` at the call's site."""
    project(root, baseline=False)
    (root / "app").mkdir()
    (root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (root / M).write_text(APP, encoding="utf-8")
    _capture(root, "baseline")
    deps = [DependencyMigration("a", "1.0", "2.0")]
    plan = create_migration_plan(
        root, [], deps, target_python="3.12", source_manifest="requirements.txt",
        target_manifest_path=root / "requirements-target.txt", files_scanned=1, predict=False,
        recorder=StepRecorder())
    risks = PredictionReport(risks=[PredictedRisk(
        id="r0000000001", tier=RiskTier.CERTAIN, symbol="json.loads", package="a",
        site=RiskSite(file=M, func="go", line=LINE), reason="why", fact=fact, facts=[fact],
        calls=1)])
    t0 = T0(root, [])
    steps = link_and_manual_steps([], root=root, changes=[], risks=risks, manual_items=[],
                                  advisories=[], compile_findings=[], dependencies=deps, t0=t0)
    finish_steps(steps, risks, root=root, t0=t0)
    return with_steps(root, plan, *steps)


def _judgement(report) -> tuple[str, set[str]]:
    """The judgement of the ``json.loads`` call at the step's site, and its provenance."""
    entries = [e for e in (report.all_fact_diff or {}).get("entries") or ()
               if e.get("symbol") == "json.loads"
               and any((e.get(side) or {}).get("line") == LINE
                       for side in ("old_site", "new_site"))]
    assert entries, "no json.loads entry at the step's site"
    provenance = {e.get("provenance") for e in entries}
    if any(e.get("fails") for e in entries):
        return "fail", provenance
    if any(e.get("review") for e in entries):
        return "review", provenance
    if any(e.get("category") == "drift" for e in entries):
        return "drift", provenance
    return "pass", provenance


@pytest.mark.parametrize("change", list(EDITS))
@pytest.mark.parametrize("successor", list(FACTS))
def test_a_recorded_edit_is_judged_by_its_steps_default_claim(tmp_path, successor, change):
    plan = _planned(tmp_path, FACTS[successor])
    step = plan.steps[-1]
    want = ("intended", "representation") if successor == "another object" else (
        "preserve", None)
    assert (step.executor, step.claim, step.verify.expect) == ("agent", *want)

    record_step(tmp_path, plan, step.id, agent=True)
    path = tmp_path / M
    path.write_text(path.read_text(encoding="utf-8").replace(OLD, EDITS[change]),
                    encoding="utf-8")
    done = record_step(tmp_path, plan, step.id, done=True, agent=True)
    assert done.recorded == [step.id]
    _capture(tmp_path, "post_migration")
    report = service.build_contract_report_from_state(tmp_path)

    provenance = "intended" if successor == "another object" else "ledger"
    assert _judgement(report) == (EXPECTED[successor][change], {provenance})
    if EXPECTED[successor][change] == "fail":
        assert report.all_fact_diff["verdict"] == "fail"
