"""Status by step (design 1.3, 6): ``project_steps`` and its precedence, the anchor cache, the
post-migration capture made stale by the files it recorded, and the next command for each rule
of section 6."""
from __future__ import annotations

import json
import os

import pytest
from _stepwise import APP, RECIPES, plan_for, plan_next, post_capture, read, rename
from typer.testing import CliRunner

from pymolt.interfaces.cli.commands import app
from pymolt.migration_state import MigrationReceipt, StepReceipt
from pymolt.status import build_status
from pymolt.verify.models import VerificationVerdict
from pymolt.verify.report import ContractReport
from pymolt.verify.unified import build_unified_verification
from pymolt.workorder.anchor_cache import AnchorCache, cache_path
from pymolt.workorder.apply import apply_steps, skip_step
from pymolt.workorder.rollback import rollback_steps
from pymolt.workorder.state import project_steps

M = "app/m.py"


def _statuses(root, plan=None):
    return {s.id: s.status for s in project_steps(root, plan).steps}


# ── project_steps ─────────────────────────────────────────────────────────────────────────


def test_planned_applied_blocked_rolled_back_and_skipped(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    assert _statuses(tmp_path) == {"env-1": "planned", "env-2": "planned", "mech-1": "planned",
                                   "mech-2": "planned", "mech-3": "blocked"}
    apply_steps(tmp_path, plan, step="mech-1")
    assert _statuses(tmp_path)["mech-1"] == "applied"
    assert _statuses(tmp_path)["mech-3"] == "planned"
    rollback_steps(tmp_path, step="mech-1")
    skip_step(tmp_path, plan, "mech-2", "no")
    states = _statuses(tmp_path)
    assert (states["mech-1"], states["mech-2"], states["mech-3"]) == (
        "rolled_back", "skipped", "blocked")


def test_a_lost_statement_is_stale_and_a_hand_made_change_is_present(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    (tmp_path / M).write_text(APP.replace("return a.h(x)", "return a.h(x, 1)"),
                              encoding="utf-8")
    projection = project_steps(tmp_path)
    mech2 = projection.step("mech-2")
    assert mech2.status == "stale" and "is lost" in mech2.reasons[0]
    assert projection.step("mech-1").status == "planned"     # re-basable: it still resolves
    # The change made by hand, in its slot, with another edit above.
    (tmp_path / M).write_text(APP.replace("import a\n", "import a\nimport os\n")
                              .replace("a.h(x)", "a.k(x)"), encoding="utf-8")
    assert _statuses(tmp_path, plan)["mech-2"] == "present"


def test_run_level_staleness_leaves_the_steps_in_effect_alone(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    apply_steps(tmp_path, plan, step="mech-1")
    (tmp_path / "requirements-target.txt").write_text("a==3.0\n", encoding="utf-8")

    projection = project_steps(tmp_path)

    assert projection.run_stale == ["target manifest changed: requirements-target.txt"]
    assert projection.step("mech-1").status == "applied"
    assert projection.step("mech-2").status == "stale"


def test_a_step_applied_after_the_capture_is_not_in_it(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    apply_steps(tmp_path, plan, step="mech-1")
    post_capture(tmp_path, M)
    apply_steps(tmp_path, plan, step="mech-2")

    states = _statuses(tmp_path)

    assert (states["mech-1"], states["mech-2"]) == ("applied", "not_in_capture")


def test_a_verify_judgement_counts_for_the_epoch_it_was_made_for(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    apply_steps(tmp_path, plan, step="mech-1")
    _judge(tmp_path, plan, {"mech-1": {"status": "needs_review", "epoch": 1}})
    assert _statuses(tmp_path)["mech-1"] == "needs_review"
    rollback_steps(tmp_path, step="mech-1")
    apply_steps(tmp_path, plan, step="mech-1")                  # epoch 3 now
    assert _statuses(tmp_path)["mech-1"] == "applied"


def test_in_progress_and_the_target_environment(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    apply_steps(tmp_path, plan, step="mech-1")
    receipt = MigrationReceipt.load(tmp_path)
    receipt.steps[f"{plan.plan_id}:env-1"] = StepReceipt(status="in_progress", epoch=None)
    receipt.save(tmp_path)
    from pymolt.ingestion.config import EnvConfig

    config = EnvConfig.load(tmp_path / ".pymolt" / "env_config.json")
    config.target_env_path = "venv-312"
    config.save(tmp_path / ".pymolt" / "env_config.json")
    (tmp_path / "venv-312" / "bin").mkdir(parents=True)
    (tmp_path / "venv-312" / "bin" / "python").write_text("")

    states = _statuses(tmp_path)

    assert (states["env-1"], states["env-2"]) == ("in_progress", "present")


def test_the_projection_is_total_deterministic_and_cached(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    (tmp_path / M).write_text(APP.replace("import a\n", "import a\nimport os\n"),
                              encoding="utf-8")
    first = project_steps(tmp_path)
    assert {s.id for s in first.steps} == {s.id for s in plan.steps}
    assert cache_path(tmp_path).is_file()

    cache = AnchorCache(tmp_path)
    again = project_steps(tmp_path, cache=cache)

    assert [(s.id, s.status, s.reasons) for s in again.steps] == [
        (s.id, s.status, s.reasons) for s in first.steps]
    assert cache.misses == 0 and cache.hits > 0 and cache.reads == 0   # nothing re-read
    stat = (tmp_path / M).stat()
    os.utime(tmp_path / M, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    touched = AnchorCache(tmp_path)
    project_steps(tmp_path, cache=touched)
    assert touched.reads == 1 and touched.misses == 0                   # same text, same sha


# ── the post-migration capture, by the files it recorded ──────────────────────────────────


def test_the_capture_is_stale_when_a_file_it_recorded_changed(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    apply_steps(tmp_path, plan, step="mech-1")
    post_capture(tmp_path, M)
    assert build_status(tmp_path).phase("post-migration").state == "done"

    apply_steps(tmp_path, plan, step="mech-2")                  # after the capture
    status = build_status(tmp_path)
    assert status.phase("post-migration").state == "stale"
    assert "app/m.py changed since it was captured" in status.phase("post-migration").detail

    rollback_steps(tmp_path, step="mech-2")                     # back at the recorded text
    assert build_status(tmp_path).phase("post-migration").state == "done"


def test_an_earlier_run_in_effect_keeps_the_migration_applied(tmp_path):
    """The latest run rolled back, an earlier one still in effect: the migration is done."""
    plan_for(tmp_path, {M: (APP, [rename("a.f", "a.g")])})
    from pymolt.migration_plan import MigrationPlan

    apply_steps(tmp_path, MigrationPlan.load(tmp_path))
    plan2 = plan_next(tmp_path, {M: [rename("a.h", "a.k")]})
    apply_steps(tmp_path, plan2)
    rollback_steps(tmp_path)

    status = build_status(tmp_path)

    migration = status.phase("migration")
    assert migration.state == "done" and "is in effect" in migration.detail


def test_a_re_plan_after_an_applied_run_is_projected_as_the_saved_plan(tmp_path):
    """`apply` then `plan` again: the plan phase describes the new plan, not the applied run."""
    from pymolt.migration_plan import MigrationPlan

    old = plan_for(tmp_path, {M: (APP, [rename("a.f", "a.g")])})
    apply_steps(tmp_path, MigrationPlan.load(tmp_path))
    new = plan_next(tmp_path, {M: [rename("a.h", "a.k")]})
    assert new.plan_id != old.plan_id

    status = build_status(tmp_path)

    plan_phase = status.phase("plan")
    assert plan_phase.state == "done"
    assert f"plan {new.plan_id[:8]}" in plan_phase.detail
    assert "applied by migration run" not in plan_phase.detail
    assert f"applied a previous plan ({old.plan_id[:8]})" in plan_phase.detail
    assert status.next_command == "pymolt apply ."
    assert {s["id"]: s["status"] for s in status.steps if s["id"] == "mech-1"} == {
        "mech-1": "planned"}


def test_the_applied_plan_itself_is_still_reported_as_applied(tmp_path):
    from pymolt.migration_plan import MigrationPlan

    plan_for(tmp_path, {M: (APP, [rename("a.f", "a.g")])})
    apply_steps(tmp_path, MigrationPlan.load(tmp_path))

    detail = build_status(tmp_path).phase("plan").detail

    assert "applied by migration run" in detail


# ── the next command, rule by rule (design 6) ─────────────────────────────────────────────


def _judge(root, plan, steps, verdict=VerificationVerdict.INCONCLUSIVE):
    report = build_unified_verification(ContractReport(root=str(root), verdict=verdict),
                                        run_id=plan.plan_id)
    path = report.save(root)
    data = json.loads(path.read_text())
    data["steps"] = steps
    path.write_text(json.dumps(data))


def test_rule_2_a_run_level_stale_plan_is_planned_again(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    apply_steps(tmp_path, plan, step="mech-1")
    (tmp_path / "requirements-target.txt").write_text("a==3.0\n", encoding="utf-8")

    status = build_status(tmp_path)

    assert status.next_command == "pymolt plan ."
    assert "target manifest changed" in status.next_reason
    assert status.phase("steps").state == "stale"


def test_rule_3_a_step_being_recorded(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    apply_steps(tmp_path, plan)
    receipt = MigrationReceipt.load(tmp_path)
    receipt.steps[f"{plan.plan_id}:env-1"] = StepReceipt(status="in_progress")
    receipt.save(tmp_path)

    assert build_status(tmp_path).next_command == "pymolt apply --step env-1 --record --done"


def test_rule_4_pymolt_steps_whose_dependencies_are_in_effect(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    status = build_status(tmp_path)
    assert status.next_command == "pymolt apply ."
    assert "mech-1, mech-2" in status.next_reason and "mech-3" not in status.next_reason

    apply_steps(tmp_path, plan, step="mech-1")
    status = build_status(tmp_path)
    assert status.next_command == "pymolt apply ."
    assert "mech-2, mech-3" in status.next_reason
    assert status.phase("steps").detail.startswith("5 step(s): 1 applied")


def test_rule_5_a_stale_pymolt_step(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    apply_steps(tmp_path, plan, through="mech-1")
    (tmp_path / M).write_text(read(tmp_path, M).replace("return a.h(x)", "return a.h(x, 1)"),
                              encoding="utf-8")

    status = build_status(tmp_path)

    assert status.next_command == "pymolt apply --step mech-2 --dry-run"
    assert "is lost" in status.next_reason and "pymolt plan ." in status.next_reason
    assert status.next_step == "mech-2"


def test_rule_6_a_step_for_a_person_is_recorded_and_named_with_its_brief(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    apply_steps(tmp_path, plan)

    status = build_status(tmp_path)

    assert status.next_command == "pymolt apply --step env-1 --record"
    assert status.next_step == "env-1"
    assert "`pymolt apply --step env-1 --brief`" in status.next_reason


def test_rules_7_and_8_the_capture_then_the_verdict(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    apply_steps(tmp_path, plan)
    skip_step(tmp_path, plan, "env-1", "the manifest is bumped by hand later")
    assert build_status(tmp_path).next_command.startswith("pymolt capture post-migration")
    post_capture(tmp_path, M)
    assert build_status(tmp_path).next_command == "pymolt verify ."


@pytest.mark.parametrize(("judgement", "command", "reason"), [
    ("failed", "pymolt rollback --step mech-1", "failed verify"),
    ("needs_review", "pymolt verify . --step mech-1", "review"),
    ("needs_evidence", None, "add a test that reaches its sites"),
])
def test_rules_9_to_11_a_step_verify_judged(tmp_path, judgement, command, reason):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    apply_steps(tmp_path, plan)
    skip_step(tmp_path, plan, "env-1", "bumped by hand")       # else rule 6 names it first
    post_capture(tmp_path, M)
    _judge(tmp_path, plan, {"mech-1": {"status": judgement, "epoch": 1}})

    status = build_status(tmp_path)

    assert (status.next_command, status.next_step) == (command, "mech-1")
    assert reason in status.next_reason


@pytest.mark.parametrize(("evidence", "settle", "command", "reason"), [
    ("not-exercised", "add a test that reaches its sites, then capture post-migration again",
     None, "Step mech-1 needs evidence: add a test that reaches its sites"),
    ("uncomparable", "capturing again does not settle it: the watcher records the first "
                     "2,000 compiled calls per call site", None,
     "Step mech-1 needs evidence: capturing again does not settle it"),
    # Noise: the existing replicate rule (here the baseline can be replicated as it is).
    ("noise", "capture a baseline replicate", "pymolt capture baseline --replicate",
     "Step mech-1 needs evidence: differences at its sites may be run-to-run noise"),
])
def test_rule_11_says_what_settles_the_step_by_its_cause(tmp_path, evidence, settle, command,
                                                         reason):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    apply_steps(tmp_path, plan)
    skip_step(tmp_path, plan, "env-1", "bumped by hand")
    post_capture(tmp_path, M)
    _judge(tmp_path, plan, {"mech-1": {"status": "needs_evidence", "epoch": 1,
                                       "evidence": evidence, "settle": settle}})

    status = build_status(tmp_path)

    assert (status.next_command, status.next_step) == (command, "mech-1")
    assert status.next_reason.startswith(reason)
    if evidence == "uncomparable":
        assert "add a test" not in status.next_reason


def test_rule_12_done(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    apply_steps(tmp_path, plan)
    skip_step(tmp_path, plan, "env-1", "bumped by hand")
    post_capture(tmp_path, M)
    _judge(tmp_path, plan, {s: {"status": "verified", "epoch": n}
                            for n, s in enumerate(["mech-1", "mech-2", "mech-3"], 1)},
           verdict=VerificationVerdict.PASS)

    status = build_status(tmp_path)

    assert status.next_command is None and "PASS" in status.next_reason


def test_a_migration_made_by_hand_goes_to_the_capture_and_the_verdict(tmp_path):
    """Rules 4 to 6 wait while no receipt exists and a post capture newer than the plan does:
    the person migrated by hand, and the verdict judges those edits as hand edits."""
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    (tmp_path / M).write_text(plan.changes[0].new_source, encoding="utf-8")
    post_capture(tmp_path, M)

    assert build_status(tmp_path).next_command == "pymolt verify ."


def test_status_agent_lists_the_steps_and_the_next_brief(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    apply_steps(tmp_path, plan, through="mech-1")
    (tmp_path / M).write_text(read(tmp_path, M).replace("return a.h(x)", "return a.h(x, 1)"),
                              encoding="utf-8")

    out = CliRunner().invoke(app, ["status", str(tmp_path), "--agent"])

    assert out.exit_code == 0, out.output
    data = json.loads(out.stdout)["data"]
    assert {s["id"]: s["status"] for s in data["steps"]}["mech-2"] == "stale"
    assert data["steps_total"] == 5 and data["next_step"] == "mech-2"
    assert data["next_brief"]["step"] == "mech-2"
    phases = [p["phase"] for p in data["phases"]]
    assert phases.index("steps") == phases.index("plan") + 1


def test_the_apply_summary_projects_what_remains(tmp_path):
    """The summary's remaining steps come from the projection; a no-op step next to an applied
    one is not mistaken for a file edited by hand."""
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    apply_steps(tmp_path, plan, step="mech-1")

    result = apply_steps(tmp_path, plan)

    assert (result.applied, result.noop) == (["mech-2", "mech-3"], ["mech-1"])
    assert result.notes == []
    assert result.remaining == {"planned": ["env-1", "env-2"]}
