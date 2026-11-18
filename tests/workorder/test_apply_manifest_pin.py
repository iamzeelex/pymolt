"""Decision 1: ``manifest-pin`` is a pymolt step. apply rewrites the pinned requirements lines
reversibly, records them in the ledger, and rollback restores them."""
from __future__ import annotations

import pytest
from _stepwise import APP, RECIPES, events, ledger, plan_for, read

from pymolt.migration_plan import rollback_migration
from pymolt.migration_state import MigrationReceipt
from pymolt.verify import service
from pymolt.workorder.apply import StepRefusal, apply_steps

M = "app/m.py"
PINNED = "# app\na[extra]==1.0 ; python_version >= '3'  # keep\nrequests\n"
TARGET = "a==2.0\nrequests==2.31.0\n"


def _plan(root, manifest=PINNED):
    return plan_for(root, {M: (APP, RECIPES)}, manifest=manifest, target=TARGET)


def test_apply_writes_the_pin_first_and_records_it_without_code_entries(tmp_path):
    plan = _plan(tmp_path)
    pin = plan.step("env-1")
    assert (pin.executor, pin.type, pin.files) == ("pymolt", "manifest-pin", ["requirements.txt"])

    result = apply_steps(tmp_path, plan)

    assert result.applied == ["env-1", "mech-1", "mech-2", "mech-3"]
    assert read(tmp_path, "requirements.txt") == (
        "# app\na[extra]==2.0 ; python_version >= '3'  # keep\nrequests\n")
    first = ledger(tmp_path)["events"][0]
    assert (first["step_id"], first["wrote"], [f["file"] for f in first["files"]]) == (
        "env-1", True, ["requirements.txt"])
    assert {e["step_id"] for e in ledger(tmp_path)["entries"]} == {"mech-1", "mech-2", "mech-3"}
    receipt = MigrationReceipt.load(tmp_path)
    step = receipt.steps[f"{plan.plan_id}:env-1"]
    assert (step.status, step.claim, step.files) == ("applied", "intended", ["requirements.txt"])
    # The v1 summary keeps counting the Python files the rewrite changed.
    assert [p.rsplit("/", 2)[-2:] for p in receipt.files_changed] == [["app", "m.py"]]
    assert receipt.progress == "complete"


def test_rollback_restores_the_pinned_lines(tmp_path):
    plan = _plan(tmp_path)
    apply_steps(tmp_path, plan)

    result = rollback_migration(tmp_path)

    assert read(tmp_path, "requirements.txt") == PINNED
    assert read(tmp_path, M) == APP
    assert [e[1:3] for e in events(tmp_path)][-4:] == [
        ("rollback", "mech-3"), ("rollback", "mech-2"), ("rollback", "mech-1"),
        ("rollback", "env-1")]
    assert result[2] == plan.plan_id


def test_the_pin_applies_alone_and_the_code_after_it_in_a_later_invocation(tmp_path):
    """The pin changes the environment fingerprint (the manifest's contents): the plan stays
    valid (same setup) and the run's later invocations still accept its baseline."""
    plan = _plan(tmp_path)
    before = service.environment_fingerprint(tmp_path)

    assert apply_steps(tmp_path, plan, step="env-1").applied == ["env-1"]
    assert service.environment_fingerprint(tmp_path) != before

    assert apply_steps(tmp_path, plan).applied == ["mech-1", "mech-2", "mech-3"]
    run = MigrationReceipt.load(tmp_path).run(plan.plan_id)
    assert run.starting_fingerprint == before


def test_through_a_code_step_takes_the_pin_on_the_way(tmp_path):
    plan = _plan(tmp_path)

    assert apply_steps(tmp_path, plan, through="mech-1").applied == ["env-1", "mech-1"]


def test_a_manifest_already_at_the_pin_is_recorded_with_no_write(tmp_path):
    plan = _plan(tmp_path)
    after = "# app\na[extra]==2.0 ; python_version >= '3'  # keep\nrequests\n"
    (tmp_path / "requirements.txt").write_text(after, encoding="utf-8")

    result = apply_steps(tmp_path, plan, step="env-1")

    assert (result.applied, result.unwritten) == (["env-1"], ["env-1"])
    rollback_migration(tmp_path)
    assert read(tmp_path, "requirements.txt") == PINNED


def test_a_moved_pin_line_is_re_based_and_a_hand_pin_is_present(tmp_path):
    plan = _plan(tmp_path)
    (tmp_path / "requirements.txt").write_text("-r base.txt\n" + PINNED, encoding="utf-8")

    result = apply_steps(tmp_path, plan, step="env-1")

    assert result.rebased == ["env-1"]
    assert read(tmp_path, "requirements.txt").startswith("-r base.txt\n# app\na[extra]==2.0 ;")
    other = tmp_path / "other"
    other.mkdir()
    plan2 = _plan(other)
    (other / "requirements.txt").write_text(
        "# pinned by hand\na[extra]==2.0 ; python_version >= '3'  # keep\nrequests\n",
        encoding="utf-8")
    present = apply_steps(other, plan2, step="env-1")
    assert (present.outcome, present.present) == ("present", ["env-1"])


def test_a_pin_line_changed_to_another_specifier_is_stale(tmp_path):
    plan = _plan(tmp_path)
    (tmp_path / "requirements.txt").write_text(PINNED.replace("==1.0", ">=1.0"),
                                               encoding="utf-8")

    with pytest.raises(StepRefusal, match="env-1") as caught:
        apply_steps(tmp_path, plan)

    assert any("is gone" in r for r in caught.value.refused["env-1"])
    assert read(tmp_path, M) == APP


def test_an_unpinned_manifest_leaves_the_pin_to_a_person(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)}, manifest="a\n", target=TARGET)

    result = apply_steps(tmp_path, plan)

    assert plan.step("env-1").executor == "person"
    assert "env-1" not in result.applied and read(tmp_path, "requirements.txt") == "a\n"
    with pytest.raises(StepRefusal, match="env-1 is a person step"):
        apply_steps(tmp_path, plan, step="env-1")
