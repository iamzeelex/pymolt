"""``apply --step ID --record`` (design 3.3): one record open per run, snapshots in the blob
store with a per-step manifest, epochs, re-record, ``--done`` with no ``--record`` first, the
claim limits at record time, ``--abort`` and ``--skip``; state table rows 8 and 15."""
from __future__ import annotations

import json

import pytest
from _stepwise import events, ledger, manual_step, plan_for, read, rename, with_steps
from typer.testing import CliRunner

from pymolt.apply_ledger import in_effect_entries, load_ledger
from pymolt.interfaces.cli.commands import app
from pymolt.migration_state import MigrationReceipt
from pymolt.workorder import record as record_mod
from pymolt.workorder.apply import StepRefusal, apply_steps, skip_step
from pymolt.workorder.record import record_step
from pymolt.workorder.rollback import rollback_steps
from pymolt.workorder.state import project_steps

M = "app/m.py"
APP = ("import a\n"
       "\n"
       "\n"
       "def run(v):\n"
       "    x = a.f(v)\n"
       "    return x\n"
       "\n"
       "\n"
       "def go(v):\n"
       "    y = a.h(v)\n"
       "    return y\n"
       "\n"
       "\n"
       "def other(v):\n"
       "    return v + 1\n")


@pytest.fixture(autouse=True)
def _no_compile(monkeypatch):
    monkeypatch.setattr("pymolt.apply_check.compile_changes", lambda root, plan: [])


def _plan(root, *, claim="preserve", step_type="api-change", executor="agent", expect=None):
    plan = plan_for(root, {M: (APP, [rename("a.f", "a.g")])}, baseline=False)
    return with_steps(root, plan, manual_step(root, M, 10, claim=claim, step_type=step_type,
                                              executor=executor, expect=expect, text=APP,
                                              sid="beh-1" if step_type in (
                                                  "behaviour-change", "review") else "sig-1"))


def _edit(root, old, new):
    path = root / M
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new), encoding="utf-8")


def _step(root, run, sid):
    receipt = MigrationReceipt.load(root)
    return receipt.steps.get(f"{run}:{sid}") if receipt else None


def _status(root, plan, sid):
    return project_steps(root, plan).step(sid).status


# ── --record, --done ──────────────────────────────────────────────────────────────────────


def test_record_snapshots_the_files_and_done_records_the_edit(tmp_path):
    plan = _plan(tmp_path)
    result = record_step(tmp_path, plan, "sig-1", agent=True)

    assert result.outcome == "in_progress" and result.in_progress == ["sig-1"]
    manifest = json.loads((tmp_path / result.snapshot).read_text())
    assert M in manifest["files"] and manifest["by"] == "agent"
    assert _step(tmp_path, plan.plan_id, "sig-1").status == "in_progress"
    assert _status(tmp_path, plan, "sig-1") == "in_progress"
    assert events(tmp_path) == []                         # --record appends no event

    _edit(tmp_path, "y = a.h(v)", "y = a.k(v)")
    done = record_step(tmp_path, plan, "sig-1", done=True, agent=True)

    assert done.outcome == "recorded" and done.files == [M]
    ev = ledger(tmp_path)["events"][-1]
    assert (ev["event"], ev["step_id"], ev["executor"], ev["claim"], ev["recorded_from"]) == (
        "record", "sig-1", "agent", "preserve", "snapshot")
    entries = [e for e in ledger(tmp_path)["entries"] if e["step_id"] == "sig-1"]
    assert len(entries) == 1
    entry = entries[0]
    assert (entry["claim"], entry["basis"], entry["old_symbol"], entry["new_symbol"]) == (
        "preserve", "manual", "a.h", "a.k")
    assert entry["anchor_old"]["stmt"] == "y = a.h(v)"
    assert entry["anchor_new"]["stmt"] == "y = a.k(v)"
    step = _step(tmp_path, plan.plan_id, "sig-1")
    assert (step.status, step.epoch, step.executor, step.in_effect) == (
        "recorded", ev["seq"], "agent", True)
    assert step.checks["smoke_import"].startswith("not checked")
    assert _status(tmp_path, plan, "sig-1") == "recorded"


def test_a_second_record_is_refused_and_one_record_is_open_per_run(tmp_path):
    plan = _plan(tmp_path)
    plan = with_steps(tmp_path, plan, manual_step(tmp_path, M, 15, sid="sig-2", text=APP,
                                                  symbol=("a.x", None)))
    record_step(tmp_path, plan, "sig-1")
    before = (tmp_path / ".pymolt" / "migration_state.json").read_bytes()

    with pytest.raises(StepRefusal, match="already being recorded"):
        record_step(tmp_path, plan, "sig-1")
    with pytest.raises(StepRefusal, match="one step at a time"):
        record_step(tmp_path, plan, "sig-2")
    with pytest.raises(StepRefusal, match="one step at a time"):
        record_step(tmp_path, plan, "sig-2", done=True)
    # apply and rollback would write into the open record.
    with pytest.raises(StepRefusal, match="being recorded: apply"):
        apply_steps(tmp_path, plan, allow_no_baseline=True)
    with pytest.raises(StepRefusal, match="being recorded: rollback"):
        rollback_steps(tmp_path)
    assert (tmp_path / ".pymolt" / "migration_state.json").read_bytes() == before
    # A dry run writes nothing, so it still runs.
    assert apply_steps(tmp_path, plan, dry_run=True, allow_no_baseline=True).outcome == "dry_run"


def test_done_with_no_change_is_refused_and_abort_restores_the_status(tmp_path):
    plan = _plan(tmp_path)
    opened = record_step(tmp_path, plan, "sig-1")
    with pytest.raises(StepRefusal, match="no change to record since `--record`"):
        record_step(tmp_path, plan, "sig-1", done=True)
    assert _step(tmp_path, plan.plan_id, "sig-1").status == "in_progress"

    aborted = record_step(tmp_path, plan, "sig-1", abort=True)

    assert aborted.outcome == "aborted"
    assert [e[1] for e in events(tmp_path)] == ["abort"]
    assert _step(tmp_path, plan.plan_id, "sig-1") is None      # planned again
    assert not (tmp_path / opened.snapshot).exists()
    assert _status(tmp_path, plan, "sig-1") == "planned"
    with pytest.raises(StepRefusal, match="not being recorded"):
        record_step(tmp_path, plan, "sig-1", abort=True)


def test_row_8_a_re_record_supersedes_the_epoch_and_describes_the_whole_step(tmp_path):
    plan = _plan(tmp_path)
    record_step(tmp_path, plan, "sig-1")
    _edit(tmp_path, "y = a.h(v)", "y = a.k(v)")
    first = record_step(tmp_path, plan, "sig-1", done=True)
    first_seq = _step(tmp_path, plan.plan_id, "sig-1").epoch

    record_step(tmp_path, plan, "sig-1")                       # row 8: recorded -> --record
    assert _step(tmp_path, plan.plan_id, "sig-1").in_effect    # its epoch stays until --done
    _edit(tmp_path, "    return y\n", "    return y or 0\n")
    second = record_step(tmp_path, plan, "sig-1", done=True)

    assert first.outcome == second.outcome == "recorded"
    step = _step(tmp_path, plan.plan_id, "sig-1")
    assert step.epoch > first_seq
    record = ledger(tmp_path)["events"][-1]
    # Diffed from the previous epoch's before-text: the plan's text, not the snapshot.
    assert record["files"][0]["before"] == ledger(tmp_path)["events"][0]["files"][0]["before"]
    live, _note, _l = in_effect_entries(tmp_path, MigrationReceipt.load(tmp_path))
    mine = [e for e in live if e.step_id == "sig-1"]
    assert {e.epoch for e in mine} == {step.epoch}             # one generation in effect
    assert {e.anchor_new.stmt for e in mine if e.anchor_new} == {"y = a.k(v)", "return y or 0"}


def test_row_15_abort_of_a_re_record_keeps_the_recorded_epoch(tmp_path):
    plan = _plan(tmp_path)
    record_step(tmp_path, plan, "sig-1")
    _edit(tmp_path, "y = a.h(v)", "y = a.k(v)")
    record_step(tmp_path, plan, "sig-1", done=True)
    epoch = _step(tmp_path, plan.plan_id, "sig-1").epoch

    record_step(tmp_path, plan, "sig-1")
    record_step(tmp_path, plan, "sig-1", abort=True)

    step = _step(tmp_path, plan.plan_id, "sig-1")
    assert (step.status, step.epoch) == ("recorded", epoch)
    assert _status(tmp_path, plan, "sig-1") == "recorded"
    assert load_ledger(tmp_path).current_epochs()[(plan.plan_id, "sig-1")] == epoch


def test_done_with_no_record_first_attributes_only_the_steps_statements(tmp_path):
    plan = _plan(tmp_path)
    apply_steps(tmp_path, plan, allow_no_baseline=True)          # mech-1: a.f -> a.g
    after_mech = read(tmp_path, M)
    _edit(tmp_path, "y = a.h(v)", "y = a.k(v)")                # the step's statement
    _edit(tmp_path, "return v + 1", "return v + 2")            # someone else's

    result = record_step(tmp_path, plan, "sig-1", done=True)

    assert result.outcome == "recorded"
    assert [u["scope"] for u in result.unrecorded] == ["other"]
    ev = ledger(tmp_path)["events"][-1]
    assert ev["recorded_from"] == "last-known"
    mine = [e for e in ledger(tmp_path)["entries"] if e["step_id"] == "sig-1"]
    assert [e["anchor_new"]["stmt"] for e in mine] == ["y = a.k(v)"]
    # Its before-text is the last known text (mech-1's) with the other edit kept, so rolling
    # the step back undoes only its own statement.
    rollback_steps(tmp_path, step="sig-1")
    assert read(tmp_path, M) == after_mech.replace("return v + 1", "return v + 2")


def test_done_with_no_record_first_and_nothing_in_scope_is_refused(tmp_path):
    plan = _plan(tmp_path)
    _edit(tmp_path, "return v + 1", "return v + 2")
    with pytest.raises(StepRefusal, match="no change to record in the step's statements"):
        record_step(tmp_path, plan, "sig-1", done=True)
    assert events(tmp_path) == []


def test_row_4_done_on_a_recorded_step_with_nothing_new_is_a_no_op(tmp_path):
    plan = _plan(tmp_path)
    record_step(tmp_path, plan, "sig-1")
    _edit(tmp_path, "y = a.h(v)", "y = a.k(v)")
    record_step(tmp_path, plan, "sig-1", done=True)
    before = ledger(tmp_path)

    again = record_step(tmp_path, plan, "sig-1", done=True)

    assert again.outcome == "already_applied" and again.noop == ["sig-1"]
    assert ledger(tmp_path) == before


def test_a_recorded_step_rolls_back_to_its_before_text(tmp_path):
    plan = _plan(tmp_path)
    record_step(tmp_path, plan, "sig-1")
    _edit(tmp_path, "y = a.h(v)", "y = a.k(v)")
    record_step(tmp_path, plan, "sig-1", done=True)

    rolled = rollback_steps(tmp_path, step="sig-1")

    assert rolled.steps == ["sig-1"] and read(tmp_path, M) == APP
    assert _status(tmp_path, plan, "sig-1") == "rolled_back"
    # A rolled-back step can be recorded again.
    record_step(tmp_path, plan, "sig-1")
    _edit(tmp_path, "y = a.h(v)", "y = a.k(v, 1)")
    assert record_step(tmp_path, plan, "sig-1", done=True).outcome == "recorded"


def test_a_review_step_records_a_completion_with_no_entries(tmp_path):
    plan = _plan(tmp_path, step_type="review", executor="person")
    record_step(tmp_path, plan, "beh-1")
    result = record_step(tmp_path, plan, "beh-1", done=True)

    assert result.outcome == "recorded" and result.files == []
    ev = ledger(tmp_path)["events"][-1]
    assert (ev["event"], ev["files"]) == ("record", [])
    assert not [e for e in ledger(tmp_path)["entries"] if e["step_id"] == "beh-1"]
    # A rollback of an epoch that changed no file keeps it (row 6).
    assert rollback_steps(tmp_path, step="beh-1").kept == ["beh-1"]


def test_refusals_target_env_applied_skipped_and_dependencies(tmp_path):
    plan = _plan(tmp_path)
    with pytest.raises(StepRefusal, match="nothing to record"):
        record_step(tmp_path, plan, "env-2")
    apply_steps(tmp_path, plan, step="mech-1", allow_no_baseline=True)
    with pytest.raises(StepRefusal, match="applied by pymolt"):
        record_step(tmp_path, plan, "mech-1")
    skip_step(tmp_path, plan, "sig-1", "not needed")
    with pytest.raises(StepRefusal, match="was skipped"):
        record_step(tmp_path, plan, "sig-1")
    with pytest.raises(StepRefusal, match="unknown step"):
        record_step(tmp_path, plan, "sig-9")


def test_a_dependency_not_in_effect_is_refused(tmp_path):
    plan = _plan(tmp_path)
    plan = with_steps(tmp_path, plan, manual_step(tmp_path, M, 15, sid="sig-2", text=APP,
                                                  depends_on=["sig-1"]))
    with pytest.raises(StepRefusal, match="depends on sig-1"):
        record_step(tmp_path, plan, "sig-2")
    record_step(tmp_path, plan, "sig-1")
    _edit(tmp_path, "y = a.h(v)", "y = a.k(v)")
    record_step(tmp_path, plan, "sig-1", done=True)
    assert record_step(tmp_path, plan, "sig-2").outcome == "in_progress"


def test_a_snapshot_over_the_cap_lists_files_by_hash_and_leaves_their_edits_unrecorded(
        tmp_path, monkeypatch):
    plan = _plan(tmp_path)
    monkeypatch.setattr(record_mod, "SNAPSHOT_NEW_BYTES_CAP", 0)
    opened = record_step(tmp_path, plan, "sig-1")
    manifest = json.loads((tmp_path / opened.snapshot).read_text())
    assert manifest["files"][M]["hash_only"] is True
    _edit(tmp_path, "y = a.h(v)", "y = a.k(v)")

    with pytest.raises(StepRefusal, match="no change to record"):
        record_step(tmp_path, plan, "sig-1", done=True)


def test_a_new_file_is_recorded_from_empty(tmp_path):
    plan = _plan(tmp_path)
    record_step(tmp_path, plan, "sig-1")
    (tmp_path / "app" / "extra.py").write_text("import a\nz = a.k(1)\n", encoding="utf-8")
    result = record_step(tmp_path, plan, "sig-1", done=True)
    assert result.files == ["app/extra.py"]


def test_the_compile_check_failure_is_on_the_receipt_and_the_edit_stays(tmp_path, monkeypatch):
    from pymolt.apply_check import CompileCheck

    plan = _plan(tmp_path)
    record_step(tmp_path, plan, "sig-1")
    _edit(tmp_path, "y = a.h(v)", "y = a.k(v")
    monkeypatch.setattr(
        "pymolt.apply_check.compile_changes",
        lambda root, p: [CompileCheck(side="target", status="failed", python="3.12",
                                      files=[c.path for c in p.changes],
                                      failed={c.path: "SyntaxError" for c in p.changes})])

    result = record_step(tmp_path, plan, "sig-1", done=True)

    assert result.check_failed is True
    assert result.checks["sig-1"]["compile_target"].startswith("failed")
    assert "a.k(v\n" in read(tmp_path, M)                      # pymolt did not write it


# ── claims at record time (within the ladder) ─────────────────────────────────────────────


def test_an_agent_cannot_loosen_a_claim_or_set_a_person_only_expectation(tmp_path):
    plan = _plan(tmp_path)
    record_step(tmp_path, plan, "sig-1", agent=True)
    _edit(tmp_path, "y = a.h(v)", "y = a.k(v)")
    with pytest.raises(StepRefusal, match="needs a person's attestation"):
        record_step(tmp_path, plan, "sig-1", done=True, agent=True, claim="intended")
    with pytest.raises(StepRefusal, match="--expect goes with --claim intended"):
        record_step(tmp_path, plan, "sig-1", done=True, agent=True, expect="representation")
    with pytest.raises(StepRefusal, match="refused under --agent"):
        record_step(tmp_path, plan, "sig-1", done=True, agent=True, by="person")
    with pytest.raises(StepRefusal, match="takes any, representation"):
        record_step(tmp_path, plan, "sig-1", done=True, agent=True, expect="whatever")
    assert record_step(tmp_path, plan, "sig-1", done=True, agent=True).outcome == "recorded"


def test_an_intended_step_takes_a_tighter_claim_and_expectation_but_no_person_only_one(
        tmp_path):
    plan = _plan(tmp_path, claim="intended", step_type="behaviour-change", executor="person")
    record_step(tmp_path, plan, "beh-1", agent=True)
    _edit(tmp_path, "y = a.h(v)", "y = a.k(v)")
    with pytest.raises(StepRefusal, match="person's attested expectation"):
        record_step(tmp_path, plan, "beh-1", done=True, agent=True, claim="intended",
                    expect="raise:ValueError")
    result = record_step(tmp_path, plan, "beh-1", done=True, agent=True,
                         expect="representation", pairs=["a.h=a.k", "a.x=a.y"])
    assert result.outcome == "recorded"
    entries = [e for e in ledger(tmp_path)["entries"] if e["step_id"] == "beh-1"]
    assert {(e["claim"], e["expect"]) for e in entries} == {("intended", "representation")}
    assert {(e["old_symbol"], e["new_symbol"]) for e in entries} >= {("a.h", "a.k"),
                                                                    ("a.x", "a.y")}


def test_tightening_an_intended_step_to_preserve_is_allowed(tmp_path):
    plan = _plan(tmp_path, claim="intended", step_type="behaviour-change", executor="person")
    record_step(tmp_path, plan, "beh-1")
    _edit(tmp_path, "y = a.h(v)", "y = a.k(v)")
    record_step(tmp_path, plan, "beh-1", done=True, claim="preserve")
    entries = [e for e in ledger(tmp_path)["entries"] if e["step_id"] == "beh-1"]
    assert {e["claim"] for e in entries} == {"preserve"}


def test_a_person_without_an_attestation_is_recorded_unattested(tmp_path):
    plan = _plan(tmp_path)
    record_step(tmp_path, plan, "sig-1", by="person", actor="ana")
    _edit(tmp_path, "y = a.h(v)", "y = a.k(v)")
    result = record_step(tmp_path, plan, "sig-1", done=True)
    ev = ledger(tmp_path)["events"][-1]
    assert (ev["executor"], ev["actor"], ev["attestation"]) == ("person", "ana", None)
    assert any("unattested person" in n for n in result.notes)


# ── the CLI ───────────────────────────────────────────────────────────────────────────────


def test_cli_record_done_and_exit_codes(tmp_path):
    plan = _plan(tmp_path)
    runner = CliRunner()
    opened = runner.invoke(app, ["apply", str(tmp_path), "--step", "sig-1", "--record",
                                 "--agent"])
    assert opened.exit_code == 0, opened.stdout
    view = json.loads(opened.stdout)
    assert view["data"]["outcome"] == "in_progress"
    again = runner.invoke(app, ["apply", str(tmp_path), "--step", "sig-1", "--record",
                                "--agent"])
    assert again.exit_code == 2 and "already being recorded" in again.stdout
    _edit(tmp_path, "y = a.h(v)", "y = a.k(v)")
    done = runner.invoke(app, ["apply", str(tmp_path), "--step", "sig-1", "--record", "--done",
                               "--agent"])
    assert done.exit_code == 0, done.stdout
    assert json.loads(done.stdout)["data"]["recorded"] == ["sig-1"]
    bad = runner.invoke(app, ["apply", str(tmp_path), "--step", "sig-1", "--done"])
    assert bad.exit_code == 2
    bad = runner.invoke(app, ["apply", str(tmp_path), "--claim", "preserve"])
    assert bad.exit_code == 2
    assert plan.plan_id
