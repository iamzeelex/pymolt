"""The state table, rows 5 to 16 (design 4.1), rollback phase 1 (4.2), ``--force`` across runs,
the cross-run refusals, the capture rule and the refusals under ``--agent``. Rows 1 to 4 are in
``test_apply_steps``; row 7's re-base in ``test_apply_rebase``."""
from __future__ import annotations

import pytest
from _stepwise import (
    APP,
    RECIPES,
    events,
    ledger,
    plan_for,
    plan_next,
    post_capture,
    read,
    rename,
)

from pymolt.apply_ledger import EventFile, LedgerEvent, append_ledger, in_effect_entries
from pymolt.migration_plan import MigrationConflictError
from pymolt.migration_state import MigrationReceipt, StepReceipt
from pymolt.verify import service
from pymolt.workorder.apply import StepRefusal, apply_steps, skip_step
from pymolt.workorder.blobs import put_texts
from pymolt.workorder.edits import sha256
from pymolt.workorder.rollback import rollback_steps

M = "app/m.py"
N = "app/n.py"


def _texts(plan):
    from pymolt.workorder.edits import apply_hunks

    out = [plan.changes[0].old_source]
    for step in [s for s in plan.steps if s.type == "codemod"]:
        out.append(apply_hunks(out[-1], step.edit_for(M).hunks))
    return out


def _status(root, run, step):
    receipt = MigrationReceipt.load(root)
    s = receipt.steps.get(f"{run}:{step}")
    return (s.status, s.epoch, s.in_effect) if s else None


# ── row 5: file-level rollback of one step ────────────────────────────────────────────────


def test_row_5_a_step_that_is_the_last_change_to_its_files_rolls_back(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    apply_steps(tmp_path, plan, through="mech-2")
    t0, t1, _t2, _t3 = _texts(plan)

    result = rollback_steps(tmp_path, step="mech-2")

    assert (result.steps, result.files_restored) == (["mech-2"], [M])
    assert read(tmp_path, M) == t1
    rollback = ledger(tmp_path)["events"][-1]
    edit = plan.step("mech-2").edit_for(M)
    assert (rollback["event"], rollback["step_id"], rollback["restored"]) == (
        "rollback", "mech-2", True)
    assert (rollback["files"][0]["before"], rollback["files"][0]["after"]) == (
        edit.after_sha256, edit.before_sha256)
    assert _status(tmp_path, plan.plan_id, "mech-2") == ("rolled_back", None, False)
    assert _status(tmp_path, plan.plan_id, "mech-1") == ("applied", 1, True)
    receipt = MigrationReceipt.load(tmp_path)
    assert receipt.status == "applied" and receipt.in_effect
    assert receipt.steps[f"{plan.plan_id}:mech-2"].files_restored == [M]
    # The rest applies again, and mech-1 rolls back now that it is the last change.
    rollback_steps(tmp_path, step="mech-1")
    assert read(tmp_path, M) == t0


def test_row_5_refuses_a_step_a_later_step_or_an_edit_changed_since(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    apply_steps(tmp_path, plan, through="mech-2")

    with pytest.raises(MigrationConflictError, match="mech-2 changed it after mech-1"):
        rollback_steps(tmp_path, step="mech-1")
    (tmp_path / M).write_text(read(tmp_path, M) + "# by hand\n", encoding="utf-8")
    with pytest.raises(MigrationConflictError, match="was edited after mech-2 wrote it"):
        rollback_steps(tmp_path, step="mech-2")
    assert read(tmp_path, M).endswith("# by hand\n")


def test_dependents_are_refused_and_cascade_rolls_them_back_first(tmp_path):
    plan = plan_for(tmp_path, {M: ("import a\nx = a.f()\n", [rename("a.f", "a.g"),
                                                              rename("a.g", "a.m")])})
    apply_steps(tmp_path, plan)
    assert plan.step("mech-2").depends_on == ["mech-1"]
    # mech-2 is the last change to the file, but it is the dependent that blocks mech-1.
    with pytest.raises(MigrationConflictError, match="has dependents in effect: mech-2"):
        rollback_steps(tmp_path, step="mech-1")

    result = rollback_steps(tmp_path, step="mech-1", cascade=True)

    assert result.steps == ["mech-2", "mech-1"]
    assert read(tmp_path, M) == "import a\nx = a.f()\n"
    last = [e[1:3] for e in events(tmp_path)][-2:]
    assert last == [("rollback", "mech-2"), ("rollback", "mech-1")]
    chain = ledger(tmp_path)["events"][-2:]
    assert chain[0]["files"][0]["after"] == chain[1]["files"][0]["before"]


def test_row_5_the_capture_is_set_aside_only_when_it_recorded_the_step(tmp_path):
    """Set aside: the capture postdates the step and its snapshot has the step's new statement.
    Kept: a capture taken before the step (it recorded the code before it), or one whose
    snapshot does not hold the step's statement."""
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    apply_steps(tmp_path, plan, step="mech-1")
    before = post_capture(tmp_path, M)                  # records T1: mech-1 is in it
    apply_steps(tmp_path, plan, step="mech-2")

    kept = rollback_steps(tmp_path, step="mech-2")      # applied after the capture

    assert kept.post == "kept"
    assert service.load_contract_state(tmp_path).post_migration == before
    aside = rollback_steps(tmp_path, step="mech-1")
    assert aside.post == "set_aside"
    state = service.load_contract_state(tmp_path)
    assert state.post_migration is None
    assert state.diagnostic_captures[-1].set_aside_by == plan.plan_id


def test_row_5_a_capture_whose_snapshot_lacks_the_file_is_kept(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    apply_steps(tmp_path, plan, step="mech-1")
    post_capture(tmp_path)                               # it saw no call in app/m.py

    assert rollback_steps(tmp_path, step="mech-1").post == "kept"


# ── row 6: an epoch that changed no file ──────────────────────────────────────────────────


def test_row_6_an_epoch_with_no_file_keeps_its_epoch(tmp_path):
    """A completion with no entries (a person's env or review step, recorded): its rollback
    rewrites nothing, says ``restored: false``, and the step stays in effect."""
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    apply_steps(tmp_path, plan, step="mech-1")
    saved = append_ledger(tmp_path, [(LedgerEvent(
        event="record", run_id=plan.plan_id, step_id="env-2", at="2026-11-17T00:00:00+00:00",
        executor="person", actor="person"), [])])
    receipt = MigrationReceipt.load(tmp_path)
    receipt.steps[f"{plan.plan_id}:env-2"] = StepReceipt(
        status="recorded", epoch=saved.events[-1].seq, executor="person", actor="person")
    receipt.save(tmp_path)

    result = rollback_steps(tmp_path, step="env-2")

    assert (result.steps, result.kept, result.files_restored) == ([], ["env-2"], [])
    last = ledger(tmp_path)["events"][-1]
    assert (last["event"], last["step_id"], last["restored"]) == ("rollback", "env-2", False)
    assert _status(tmp_path, plan.plan_id, "env-2") == ("rolled_back", 2, True)


def test_a_recorded_step_rolls_back_from_its_record_blobs(tmp_path):
    """4.2, recorded steps: the same phase 1, from the record's before- and after-text."""
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    edited = APP.replace("z = 1", "z = 2")
    put_texts(tmp_path, [APP, edited])
    (tmp_path / M).write_text(edited, encoding="utf-8")
    saved = append_ledger(tmp_path, [(LedgerEvent(
        event="record", run_id=plan.plan_id, step_id="env-2", at="2026-11-17T00:00:00+00:00",
        executor="agent", actor="agent",
        files=[EventFile(file=M, before=sha256(APP), after=sha256(edited))]), [])])
    MigrationReceipt(run_id=plan.plan_id, plan_id=plan.plan_id, target_python="3.12",
                     source_manifest="requirements.txt", steps={
                         f"{plan.plan_id}:env-2": StepReceipt(
                             status="recorded", epoch=saved.events[-1].seq, files=[M])},
                     ).save(tmp_path)
    run_dir = tmp_path / ".pymolt" / "migrations" / plan.plan_id
    run_dir.mkdir(parents=True)
    plan.save(tmp_path, run_dir / "plan.json")
    from pymolt.migration_plan import MigrationRunState, _save_run_state

    _save_run_state(run_dir, MigrationRunState(run_id=plan.plan_id, status="applied",
                                               prepared_at="2026-11-17T00:00:00+00:00"))

    rollback_steps(tmp_path, step="env-2")

    assert read(tmp_path, M) == APP


# ── rows 9 to 14: two runs ────────────────────────────────────────────────────────────────


def _two_runs(root, *, built_on: bool):
    """R1 rewrites a.f in app/m.py; then a re-plan R2 rewrites either what R1 wrote
    (``built_on``) or a call in another file."""
    plan1 = plan_for(root, {M: (APP, [rename("a.f", "a.g")]),
                            N: ("import a\ny = a.h()\n", [])})
    apply_steps(root, plan1)
    if built_on:
        plan2 = plan_next(root, {M: [rename("a.g", "a.m")]})
    else:
        plan2 = plan_next(root, {N: [rename("a.h", "a.k")]})
    apply_steps(root, plan2)
    return plan1, plan2


def test_row_9_a_plan_applied_over_an_open_run_closes_it_and_keeps_its_steps(tmp_path):
    plan1, plan2 = _two_runs(tmp_path, built_on=False)

    receipt = MigrationReceipt.load(tmp_path)

    assert [(r.run_id, r.closed) for r in receipt.runs] == [
        (plan1.plan_id, True), (plan2.plan_id, False)]
    assert _status(tmp_path, plan1.plan_id, "mech-1")[2] is True
    entries, _note, _l = in_effect_entries(tmp_path, receipt)
    assert {(e.run_id, e.file) for e in entries} == {(plan1.plan_id, M), (plan2.plan_id, N)}


def test_row_10_a_closed_run_applies_nothing_more(tmp_path):
    plan1, _plan2 = _two_runs(tmp_path, built_on=False)

    with pytest.raises(StepRefusal, match="is closed"):
        apply_steps(tmp_path, plan1, step="mech-1")
    with pytest.raises(StepRefusal, match="is closed"):
        apply_steps(tmp_path, plan1)


def test_row_11_an_earlier_runs_step_a_later_run_built_on_is_refused(tmp_path):
    plan1, plan2 = _two_runs(tmp_path, built_on=True)

    with pytest.raises(MigrationConflictError, match="mech-1 of run"):
        rollback_steps(tmp_path, run_id=plan1.plan_id, step="mech-1")

    rollback_steps(tmp_path, step="mech-1")             # R2's step, the open run's
    rollback_steps(tmp_path, run_id=plan1.plan_id, step="mech-1")
    assert read(tmp_path, M) == APP


def test_row_11_an_earlier_runs_step_on_another_file_rolls_back(tmp_path):
    plan1, plan2 = _two_runs(tmp_path, built_on=False)

    rollback_steps(tmp_path, run_id=plan1.plan_id, step="mech-1")

    assert read(tmp_path, M) == APP and "a.k()" in read(tmp_path, N)


def test_row_12_rollback_dot_rolls_back_the_open_run_only(tmp_path):
    plan1, plan2 = _two_runs(tmp_path, built_on=True)

    result = rollback_steps(tmp_path)

    assert result.run_id == plan2.plan_id
    assert read(tmp_path, M) == APP.replace("a.f(v)", "a.g(v)")       # R1's text again
    assert _status(tmp_path, plan1.plan_id, "mech-1")[2] is True
    assert _status(tmp_path, plan2.plan_id, "mech-1")[:2] == ("rolled_back", None)


def test_row_13_an_earlier_run_is_refused_while_a_later_one_is_on_its_files(tmp_path):
    plan1, _plan2 = _two_runs(tmp_path, built_on=True)

    with pytest.raises(MigrationConflictError, match="is not the last change to its files"):
        rollback_steps(tmp_path, run_id=plan1.plan_id)
    with pytest.raises(MigrationConflictError, match="is not the last change to its files"):
        rollback_steps(tmp_path, run_id=plan1.plan_id, force=True)   # force does not override


def test_row_14_force_restores_the_runs_files_and_discards_later_edits(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES), N: ("import a\ny = a.h()\n", [])})
    apply_steps(tmp_path, plan)
    (tmp_path / M).write_text(read(tmp_path, M) + "# by hand\n", encoding="utf-8")
    (tmp_path / N).write_text("# untouched by the run\n", encoding="utf-8")
    post_capture(tmp_path, M)
    with pytest.raises(MigrationConflictError, match="post-migration edits"):
        rollback_steps(tmp_path)

    result = rollback_steps(tmp_path, force=True)

    assert read(tmp_path, M) == APP
    assert read(tmp_path, N) == "# untouched by the run\n"        # never written
    assert result.steps == ["mech-3", "mech-2", "mech-1"]
    assert all(e["restored"] for e in ledger(tmp_path)["events"][-3:])
    assert result.post == "set_aside"


# ── rows 15 and 16 ────────────────────────────────────────────────────────────────────────


def test_row_16_skip_from_planned_rolled_back_or_stale(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    skip_step(tmp_path, plan, "mech-2", "planned")
    apply_steps(tmp_path, plan, step="mech-1")
    rollback_steps(tmp_path, step="mech-1")
    skip_step(tmp_path, plan, "mech-1", "rolled back")

    assert _status(tmp_path, plan.plan_id, "mech-1")[:2] == ("skipped", None)
    assert [e[1:3] for e in events(tmp_path)] == [
        ("skip", "mech-2"), ("apply", "mech-1"), ("rollback", "mech-1"), ("skip", "mech-1")]
    assert read(tmp_path, M) == APP


# ── refusals ──────────────────────────────────────────────────────────────────────────────


def test_rollback_refusals(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    apply_steps(tmp_path, plan, step="mech-1")

    with pytest.raises(MigrationConflictError, match="--force is refused under --agent"):
        rollback_steps(tmp_path, force=True, agent=True)
    with pytest.raises(MigrationConflictError, match="--cascade is refused under --agent"):
        rollback_steps(tmp_path, step="mech-1", cascade=True, agent=True)
    with pytest.raises(MigrationConflictError, match="no person approved this plan"):
        rollback_steps(tmp_path, step="mech-1", agent=True)
    with pytest.raises(MigrationConflictError, match="a person rolls back a whole run"):
        rollback_steps(tmp_path, agent=True)
    with pytest.raises(MigrationConflictError, match="does not go with --step"):
        rollback_steps(tmp_path, step="mech-1", force=True)
    with pytest.raises(MigrationConflictError, match="--cascade goes with --step"):
        rollback_steps(tmp_path, cascade=True)
    with pytest.raises(MigrationConflictError, match="mech-2 .* is not in effect"):
        rollback_steps(tmp_path, step="mech-2")
    with pytest.raises(MigrationConflictError, match="unknown step mech-9"):
        rollback_steps(tmp_path, step="mech-9")
    assert _status(tmp_path, plan.plan_id, "mech-1")[2] is True


def test_the_cli_rollback_step_options(tmp_path):
    import json

    from typer.testing import CliRunner

    from pymolt.interfaces.cli.commands import app

    runner = CliRunner()
    apply_steps(tmp_path, plan_for(tmp_path, {M: (APP, RECIPES)}))
    d = str(tmp_path)

    assert runner.invoke(app, ["rollback", d, "--agent"]).exit_code == 2
    assert runner.invoke(app, ["rollback", d, "--step", "mech-1"]).exit_code == 2
    one = runner.invoke(app, ["rollback", d, "--step", "mech-3", "--json"])
    assert one.exit_code == 0, one.output
    assert json.loads(one.stdout)["steps"] == ["mech-3"]
    # mech-2 changed the file after mech-1 and is not its dependent: --cascade does not help.
    assert runner.invoke(app, ["rollback", d, "--step", "mech-1", "--cascade"]).exit_code == 2
    text = runner.invoke(app, ["rollback", d, "--step", "mech-2"])
    assert text.exit_code == 0, text.output
    assert "Migration rolled back" in text.output and "mech-2" in text.output
