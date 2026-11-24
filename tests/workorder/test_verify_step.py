"""``verify --step`` (design 5.3-5.5): a filter over the one post-migration capture, each step's
judgement saved in ``verification.json`` and projected by status, the exit codes, and
calibration by step (a fix credited only when the step's effect replaced the risk's
statement)."""
from __future__ import annotations

import json

from _verifying import LINE, M, capture, edit, json_project, verify
from typer.testing import CliRunner

from pymolt.interfaces.cli.commands import app
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
    assert (status.next_command, status.next_step) == ("pymolt apply . --step beh-1 --record",
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
    # A new epoch, made after the capture: the saved judgement is for the old one, and the
    # capture holds the first epoch's statement, not the current epoch's (design 1.3, 5.4).
    # Neither its old nor its new statement is in the capture, which is no sign that a later
    # edit overtook it: the capture predates the epoch.
    assert project_steps(tmp_path, plan).step("beh-1").status == "not_in_capture"
    skip_step(tmp_path, plan, "env-1", "pinned by hand")      # else rule 6 names it first
    status = build_status(tmp_path)
    assert status.next_command.startswith("pymolt capture post-migration")


def test_a_step_a_later_hand_edit_overtook_before_the_capture_is_in_it(tmp_path):
    # Neither the step's old nor its new statement is in the capture, but the capture was taken
    # after its epoch: a later edit overtook it (superseded), and capturing again changes
    # nothing. It keeps its apply-side status.
    plan = _plan(tmp_path)
    _record(tmp_path, plan, SAME)
    edit(tmp_path, "y = json.dumps(v, indent=None)", "y = json.dumps(v, indent=None) or y")
    capture(tmp_path, "post_migration")
    assert project_steps(tmp_path, plan).step("beh-1").status == "recorded"


# ── who settles a step that is not verified (decision 7) ──────────────────────────────────


def _judged(root, change_entries):
    """``beh-1``'s judgement on the two captures, after ``change_entries(fact_diff, mine)``
    turned its entries into the situation under test."""
    from pymolt.verify import service
    from pymolt.workorder.judge import judge_steps

    report = service.build_contract_report_from_state(root)
    fdiff = report._fact_diff
    mine = [e for e in fdiff.entries if e.step_id == "beh-1"]
    assert mine, "the recorded step has an entry at its site"
    change_entries(fdiff, mine)
    return judge_steps(root, report)[1]["beh-1"]


def _uncomparable(entry, note):
    from pymolt.verify.facts import Comparison, FactCategory

    entry.category, entry.comparison, entry.review = (
        FactCategory.UNCOMPARABLE, Comparison.UNCOMPARABLE, None)
    entry.notes.append(note)
    return entry


def test_a_step_whose_only_entry_is_uncomparable_needs_evidence(tmp_path):
    """It used to end needs_review: no person can settle a fact the captures could not compare
    (there is no finding to accept); what settles it is said."""
    plan = _plan(tmp_path)
    _record(tmp_path, plan, SAME)
    capture(tmp_path, "post_migration")

    judged = _judged(tmp_path, lambda _f, mine: [_uncomparable(e, "c-site-capped:old")
                                                 for e in mine])

    assert judged["status"] == "needs_evidence"
    assert judged["review"] == [] and judged["unexercised"] == []
    assert judged["uncomparable"] and "json.dumps" in judged["uncomparable"][0]
    assert judged["evidence"] == "uncomparable"
    assert "capturing again does not settle it" in judged["settle"]


def test_a_step_with_a_review_item_and_an_uncomparable_entry_needs_review(tmp_path):
    plan = _plan(tmp_path, claim="intended")
    _record(tmp_path, plan, VALUE)
    capture(tmp_path, "post_migration")

    def add_uncomparable(fdiff, mine):
        assert all(e.review for e in mine)                   # the changed value is a review
        fdiff.entries.append(_uncomparable(mine[0].model_copy(deep=True),
                                           "possibly-dropped:new"))

    judged = _judged(tmp_path, add_uncomparable)

    assert judged["status"] == "needs_review"
    assert judged["review"] and judged["uncomparable"]
    assert "evidence" not in judged and "settle" not in judged


def test_unmeasured_noise_at_a_step_needs_evidence_until_a_replicate_measured_it(tmp_path):
    from pymolt.verify.facts import UNMEASURED_NOISE, Comparison, FactCategory

    plan = _plan(tmp_path)
    _record(tmp_path, plan, SAME)
    capture(tmp_path, "post_migration")

    def noisy(measured):
        def change(fdiff, mine):
            for e in mine:
                e.category, e.comparison = FactCategory.CHANGED, Comparison.UNCOMPARABLE
                e.review = f"{UNMEASURED_NOISE}: the value differs; capture a replicate"
            fdiff.noise_measured = {"replicate": "r"} if measured else None
        return change

    judged = _judged(tmp_path, noisy(measured=False))
    assert (judged["status"], judged["evidence"]) == ("needs_evidence", "noise")
    assert judged["noise"] and "--replicate" in judged["settle"]
    # Once a replicate was measured, a difference it did not settle is a person's.
    assert _judged(tmp_path, noisy(measured=True))["status"] == "needs_review"


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


def test_a_verdict_on_another_recording_judges_no_step(tmp_path):
    from pymolt.verify import service

    plan = _plan(tmp_path)
    _record(tmp_path, plan, SAME)
    post = capture(tmp_path, "post_migration")
    # The control: on the post-migration capture itself, the step is judged.
    control = build_unified_verification(service.build_contract_report_from_state(tmp_path))
    assert list(control.steps) == ["beh-1"]
    other = tmp_path / "other.jsonl"
    other.write_bytes((tmp_path / post.trace_path).read_bytes())

    report = service.build_contract_report_from_state(tmp_path, trace_override=str(other))
    unified = build_unified_verification(report)

    assert unified.steps == {}
    assert not any("steps not judged" in n for n in report.notes)    # not judged, not failed
