"""``verify --step`` (design 5.3-5.5): a filter over the one post-migration capture, each step's
judgement saved in ``verification.json`` and projected by status, the exit codes, and
calibration by step (a fix credited only when the step's effect replaced the risk's
statement)."""
from __future__ import annotations

import json

import pytest
from _verifying import LINE, M, capture, edit, json_project, verify
from typer.testing import CliRunner

from pymolt.interfaces.cli.commands import app
from pymolt.migration_plan import MigrationPlan
from pymolt.status import build_status
from pymolt.verify.predict import PredictedRisk, PredictionReport, RiskSite, RiskTier
from pymolt.verify.unified import build_unified_verification
from pymolt.workorder.apply import skip_step
from pymolt.workorder.record import record_step
from pymolt.workorder.state import project_steps

RISK = "7c0e9a41d2b3"
SAME = ("y = json.dumps(v)", "y = json.dumps(v, indent=None)")     # equal outcome
VALUE = ("y = json.dumps(v)", "y = json.dumps(v, indent=1)")       # another value
runner = CliRunner()


def _plan(root, *, claim="preserve", risk=True):
    sid = "beh-1"
    plan = json_project(root, dict(sid=sid, step_type="behaviour-change", executor="person",
                                   claim=claim, risk_ids=[RISK] if risk else []))
    if risk:
        plan = plan.model_copy(update={"risks": PredictionReport(risks=[PredictedRisk(
            id=RISK, tier=RiskTier.HIGH, symbol="json.dumps", reason="its default changes",
            site=RiskSite(file=M, func="go", line=LINE, text="y = json.dumps(v)"),
            step_id=sid)])})
        plan.save(root)
    return plan


def _record(root, plan, change, **done):
    record_step(root, plan, "beh-1", agent=True)
    edit(root, *change)
    return record_step(root, plan, "beh-1", done=True, agent=True, **done)


def _verify_step(root, step="beh-1", *extra):
    out = runner.invoke(app, ["verify", str(root), "--step", step, "--agent", *extra])
    view = json.loads(out.stdout)
    return out.exit_code, view


def _saved(root) -> dict:
    return json.loads((root / ".pymolt" / "verification.json").read_text())


# ── the step filter and its exit codes ────────────────────────────────────────────────────


def test_a_recorded_step_that_kept_its_outcomes_is_verified(tmp_path):
    plan = _plan(tmp_path)
    _record(tmp_path, plan, SAME)
    capture(tmp_path, "post_migration")

    code, view = _verify_step(tmp_path)

    assert code == 0, view
    judged = view["data"]["step"]
    assert (judged["status"], judged["id"]) == ("verified", "beh-1") and judged["entries"]
    saved = _saved(tmp_path)
    assert saved["steps"]["beh-1"]["status"] == "verified"
    assert saved["steps_run"] == plan.plan_id
    assert saved["verdict"] == view["data"]["verdict"]          # the whole surface still
    assert project_steps(tmp_path, plan).step("beh-1").status == "verified"


def test_a_preserve_step_whose_outcome_changed_failed(tmp_path):
    plan = _plan(tmp_path)
    _record(tmp_path, plan, VALUE)
    capture(tmp_path, "post_migration")

    code, view = _verify_step(tmp_path)

    assert code == 1 and view["data"]["step"]["status"] == "failed"
    assert view["data"]["step"]["failing"]
    skip_step(tmp_path, plan, "env-1", "pinned by hand")      # else rule 6 names it first
    status = build_status(tmp_path)
    assert (status.next_command, status.next_step) == ("pymolt apply --step beh-1 --record",
                                                       "beh-1")


def test_an_intended_step_whose_value_changed_needs_review(tmp_path):
    plan = _plan(tmp_path, claim="intended")
    _record(tmp_path, plan, VALUE)
    capture(tmp_path, "post_migration")

    code, view = _verify_step(tmp_path)

    assert code == 4 and view["data"]["step"]["status"] == "needs_review"
    skip_step(tmp_path, plan, "env-1", "pinned by hand")
    status = build_status(tmp_path)
    assert status.next_command == "pymolt verify . --step beh-1"
    assert "pymolt verify . --accept ID" in status.next_reason


def test_a_step_recorded_after_the_capture_is_not_in_it(tmp_path):
    plan = _plan(tmp_path)
    capture(tmp_path, "post_migration")
    _record(tmp_path, plan, SAME)

    code, view = _verify_step(tmp_path)

    assert code == 4 and view["data"]["step"]["status"] == "not_in_capture"


def test_a_step_not_in_effect_and_an_unknown_step(tmp_path):
    _plan(tmp_path)
    capture(tmp_path, "post_migration")
    code, view = _verify_step(tmp_path)
    assert code == 4 and view["data"]["step"]["status"] == "planned"
    assert "not in effect" in view["data"]["step"]["note"]
    out = runner.invoke(app, ["verify", str(tmp_path), "--step", "beh-9", "--agent"])
    assert out.exit_code == 2 and "unknown step beh-9" in out.stdout


def test_a_judgement_of_an_older_epoch_does_not_apply(tmp_path):
    plan = _plan(tmp_path)
    _record(tmp_path, plan, SAME)
    capture(tmp_path, "post_migration")
    _verify_step(tmp_path)
    assert project_steps(tmp_path, plan).step("beh-1").status == "verified"
    record_step(tmp_path, plan, "beh-1")
    edit(tmp_path, "y = json.dumps(v, indent=None)", "y = json.dumps(v, indent=None) or y")
    record_step(tmp_path, plan, "beh-1", done=True)
    # A new epoch: the saved judgement is for the old one.
    assert project_steps(tmp_path, plan).step("beh-1").status in ("recorded", "not_in_capture")


# ── calibration by step ───────────────────────────────────────────────────────────────────


def _calibrated(root):
    report = verify(root)
    risk = next(r for r in report.calibration.risks if r.id == RISK)
    return risk, report.calibration


def test_a_recorded_fix_prevents_its_risk(tmp_path):
    plan = _plan(tmp_path)
    _record(tmp_path, plan, SAME)
    capture(tmp_path, "post_migration")
    risk, cal = _calibrated(tmp_path)
    assert (risk.result.value, risk.fix, risk.step_id) == ("prevented", "fixed", "beh-1")
    assert cal.by_step["beh-1"]["prevented"] == 1


def test_an_unrelated_record_is_recorded_not_fixing(tmp_path):
    plan = _plan(tmp_path)
    _record(tmp_path, plan, ("    return y\n", "    return y or ''\n"))
    capture(tmp_path, "post_migration")
    risk, cal = _calibrated(tmp_path)
    assert (risk.result.value, risk.fix) == ("miss", "recorded-not-fixing")
    assert cal.by_step["beh-1"]["miss"] == 1


def test_a_skipped_step_leaves_its_risk_unfixed(tmp_path):
    plan = _plan(tmp_path)
    skip_step(tmp_path, plan, "beh-1", "not now")
    edit(tmp_path, *SAME)                                # a hand edit does not credit a fix
    capture(tmp_path, "post_migration")
    risk, _cal = _calibrated(tmp_path)
    assert (risk.result.value, risk.fix) == ("miss", "unfixed")


def test_a_risk_at_an_accepted_change_scores_accepted(tmp_path):
    from _verifying import person

    plan = _plan(tmp_path, claim="intended")
    record_step(tmp_path, plan, "beh-1")
    edit(tmp_path, "y = json.dumps(v)", "y = json.dumps(v, cls=1)")
    record_step(tmp_path, plan, "beh-1", done=True, expect="raise:TypeError",
                attestation=person(tmp_path))
    capture(tmp_path, "post_migration")
    risk, cal = _calibrated(tmp_path)
    assert risk.result.value == "accepted" and cal.accepted == 1


@pytest.mark.parametrize("compared_other", [True])
def test_a_verdict_on_another_recording_judges_no_step(tmp_path, compared_other):
    plan = _plan(tmp_path)
    _record(tmp_path, plan, SAME)
    post = capture(tmp_path, "post_migration")
    from pymolt.verify import service

    other = tmp_path / "other.jsonl"
    other.write_bytes((tmp_path / post.trace_path).read_bytes()
                      if not str(post.trace_path).startswith("/") else
                      open(post.trace_path, "rb").read())
    report = service.build_contract_report_from_state(tmp_path, trace_override=str(other))
    unified = build_unified_verification(report)
    assert unified.steps == {}
    assert MigrationPlan.load(tmp_path) is not None
