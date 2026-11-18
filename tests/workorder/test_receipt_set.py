"""The receipt set: runs, step receipts, the chain, and the schema-1 files read as v2."""
from __future__ import annotations

import json

import pytest

from pymolt.apply_ledger import build_entries, in_effect_entries, ledger_path, load_ledger
from pymolt.codemods.client import DependencyMigration
from pymolt.codemods.models import ApiImpact, CodemodPattern, FilePreview
from pymolt.migration_plan import (
    MigrationPlan,
    apply_migration_plan,
    create_migration_plan,
    rollback_migration,
)
from pymolt.migration_state import (
    MigrationReceipt,
    StepReceipt,
    plan_paths_in_effect,
    receipt_path,
)
from pymolt.verify import service


def _pattern(old, new):
    return CodemodPattern(old_qualname=old, new_qualname=new, kind="rename-call",
                          evidence=[f"pymolt.delta DEPRECATED: {old} -> {new}"])


def _plan(tmp_path, old, new, pattern, *, impact=None):
    (tmp_path / "m.py").write_text(old, encoding="utf-8")
    preview = FilePreview(path=str(tmp_path / "m.py"), old_source=old, new_source=new, sites=1,
                          patterns=[pattern])
    impacts = {"a": [ApiImpact(path=impact, kind="signature-changed", state="changed",
                               risk="low", explanation="t")]} if impact else None
    return create_migration_plan(
        tmp_path, [preview], [DependencyMigration("a", "1.0", "2.0")], target_python="3.12",
        source_manifest="requirements.txt", target_manifest_path=None, files_scanned=1,
        predict=False, impacts=impacts)


T0 = "import a\nx = a.f()\ny = 1\nz = a.h()\n"
T1 = "import a\nx = a.g()\ny = 1\nz = a.h()\n"
T2 = "import a\nx = a.g()\ny = 1\nz = a.k()\n"


@pytest.mark.parametrize("steps", ["builder", "none"])
def test_apply_records_its_run_and_a_step_receipt_with_epoch_checks_and_links(tmp_path, steps):
    """The step receipt of the plan's own step, or of the synthetic ``v1`` of a plan with
    no steps."""
    plan = _plan(tmp_path, T0, T1, _pattern("a.f", "a.g"))
    [planned] = [s.id for s in plan.steps if s.executor == "pymolt"]
    if steps == "none":
        plan.steps = []
    step_id = planned if steps == "builder" else "v1"
    receipt = apply_migration_plan(tmp_path, plan, allow_no_baseline=True)

    saved = MigrationReceipt.load(tmp_path)
    assert saved == receipt and saved.schema_version == 2
    [run] = saved.runs
    assert (run.run_id, run.plan_id, run.closed, run.starting_fingerprint) == (
        receipt.run_id, plan.plan_id, False, receipt.environment_fingerprint)
    assert list(saved.steps) == [f"{receipt.run_id}:{step_id}"]
    step = saved.steps[f"{receipt.run_id}:{step_id}"]
    assert (step.status, step.epoch, step.executor, step.claim, step.files, step.sites) == (
        "applied", 1, "pymolt", "preserve", ["m.py"], 1)
    assert set(step.checks) == {"compile_target", "compile_base", "smoke_import"}
    assert step.checks["compile_base"] == "not checked: no base claim"
    assert step.checks["smoke_import"] == "not checked: not requested"
    assert (saved.status, saved.progress, saved.in_effect) == ("applied", "complete", True)
    state = json.loads((tmp_path / ".pymolt" / "migrations" / receipt.run_id / "state.json")
                       .read_text())
    assert state["steps"] == {step_id: "applied"}

    rollback_migration(tmp_path)
    after = MigrationReceipt.load(tmp_path)
    step = after.steps[f"{receipt.run_id}:{step_id}"]
    assert (step.status, step.epoch, step.files_restored) == ("rolled_back", None, ["m.py"])
    assert (after.status, after.progress, after.in_effect) == ("rolled_back", "none", False)
    assert after.files_changed == receipt.files_changed          # the v1 value is kept


def test_a_plan_applied_on_top_of_an_open_run_closes_it_and_both_stay_in_effect(tmp_path):
    """Row 9 of the state table: R1's steps in effect stay in effect; verify reads both
    runs' plans and both runs' ledger entries."""
    first = apply_migration_plan(tmp_path, _plan(tmp_path, T0, T1, _pattern("a.f", "a.g"),
                                                 impact="a.f"), allow_no_baseline=True)
    second_plan = _plan(tmp_path, T1, T2, _pattern("a.h", "a.k"), impact="a.h")
    second = apply_migration_plan(tmp_path, second_plan, allow_no_baseline=True)

    receipt = MigrationReceipt.load(tmp_path)
    assert receipt.run_id == second.run_id
    r1, r2 = receipt.runs
    assert (r1.run_id, r1.closed, r2.run_id, r2.closed) == (
        first.run_id, True, second.run_id, False)
    assert receipt.chain_run_ids() == [first.run_id, second.run_id]
    assert [r.run_id for r in receipt.runs_in_effect()] == [second.run_id, first.run_id]
    assert plan_paths_in_effect(receipt) == [second.plan_path, first.plan_path]
    assert receipt.starting_fingerprint(tmp_path) == first.environment_fingerprint
    entries, note, _ = in_effect_entries(tmp_path, receipt)
    assert note is None
    assert {(e.run_id, e.anchor_new.stmt) for e in entries} == {
        (first.run_id, "x = a.g()"), (second.run_id, "z = a.k()")}
    assert service._applied_plan_impact_targets(tmp_path) == ["a.h", "a.f"]


def test_a_rolled_back_step_applies_again_in_its_run(tmp_path):
    """Design 4.1 row 7: the run stays open and in the chain; the step gets epoch 3."""
    plan = _plan(tmp_path, T0, T1, _pattern("a.f", "a.g"))
    first = apply_migration_plan(tmp_path, plan, allow_no_baseline=True)
    rollback_migration(tmp_path)
    again = apply_migration_plan(tmp_path, plan, allow_no_baseline=True)

    receipt = MigrationReceipt.load(tmp_path)
    assert again.run_id == first.run_id
    assert [(r.run_id, r.closed) for r in receipt.runs] == [(first.run_id, False)]
    assert receipt.chain_run_ids() == [first.run_id]
    [step_id] = [s.id for s in plan.steps if s.executor == "pymolt"]
    assert receipt.steps[f"{first.run_id}:{step_id}"].epoch == 3
    entries, _note, _ = in_effect_entries(tmp_path, receipt)
    assert {e.epoch for e in entries} == {3}


def test_progress_counts_every_pymolt_step_of_the_plan(tmp_path):
    """A pymolt step apply cannot write (a manifest pin with no edit) leaves it partial."""
    from pymolt.workorder.models import ManifestPin, PlanStep, StepDo, StepRecipe

    plan = _plan(tmp_path, T0, T1, _pattern("a.f", "a.g"))
    pin = PlanStep(id="env-1", key="0" * 10, phase="env", type="manifest-pin",
                   title="Pin a", executor="pymolt", claim="intended", basis="target-manifest",
                   scope="env", do=StepDo(recipe=StepRecipe(kind="manifest-pin", pins=[
                       ManifestPin(name="a", from_version="1.0", to_version="2.0",
                                   old="a==1.0", new="a==2.0")])))
    plan.steps = [pin, *plan.steps]
    apply_migration_plan(tmp_path, plan, allow_no_baseline=True)
    receipt = MigrationReceipt.load(tmp_path)
    assert [k.split(":", 1)[1] for k in receipt.steps] == [plan.steps[1].id]
    assert (receipt.status, receipt.progress) == ("applied", "partial")


def test_a_step_receipt_is_in_effect_while_it_has_an_epoch():
    assert StepReceipt(status="applied", epoch=1).in_effect
    assert StepReceipt(status="recorded", epoch=4).in_effect
    assert StepReceipt(status="in_progress", epoch=4).in_effect     # a re-record keeps it
    assert not StepReceipt(status="in_progress").in_effect
    assert StepReceipt(status="rolled_back", epoch=1, files_restored=[]).in_effect
    assert not StepReceipt(status="rolled_back", epoch=None, files_restored=["m.py"]).in_effect
    assert not StepReceipt(status="skipped", epoch=None).in_effect


# ── schema 1 → 2 ──────────────────────────────────────────────────────────────────────────


def _v1_files(tmp_path, *, status="applied", files_restored=None):
    """The three files a schema-1 apply left: frozen plan, receipt and ledger (plus state)."""
    plan = _plan(tmp_path, T0, T1, _pattern("a.f", "a.g"))
    run = plan.plan_id
    run_dir = tmp_path / ".pymolt" / "migrations" / run
    run_dir.mkdir(parents=True)
    data = plan.model_dump(mode="json")
    for key in ("steps", "adapted_from"):
        data.pop(key)
    data["schema_version"] = 1
    for path in (run_dir / "plan.json", tmp_path / ".pymolt" / "migration_plan.json"):
        path.write_text(json.dumps(data))
    (run_dir / "state.json").write_text(json.dumps({
        "run_id": run, "status": status, "attempt": 1, "prepared_at": "2026-11-01T00:00:00",
        "applied_at": "2026-11-01T00:00:01"}))
    change = plan.changes[0]
    entries = [{**e, "run_id": run} for e in build_entries(
        "m.py", T0, T1, change.recipes, change.claims)]
    ledger_path(tmp_path).write_text(json.dumps({
        "schema": "pymolt.apply_ledger/1", "run_id": run, "applied_at": "2026-11-01T00:00:01",
        "entries": entries}))
    receipt_path(tmp_path).write_text(json.dumps({
        "schema_version": 1, "run_id": run, "plan_id": run, "status": status,
        "applied_at": "2026-11-01T00:00:01", "target_python": "3.12",
        "source_manifest": "requirements.txt", "environment_fingerprint": "fp",
        "files_changed": [str(tmp_path / "m.py")], "patterns_applied": 1,
        "plan_path": str(run_dir / "plan.json"),
        "files_restored": files_restored,
        "rolled_back_at": "2026-11-02T00:00:00" if status == "rolled_back" else None}))
    if status == "applied":
        (tmp_path / "m.py").write_text(T1, encoding="utf-8")
    return run


def test_the_three_schema_1_files_read_as_v2(tmp_path):
    run = _v1_files(tmp_path)

    plan = MigrationPlan.load(tmp_path)
    assert plan.adapted_from == 1 and [s.id for s in plan.steps] == ["v1"]

    receipt = MigrationReceipt.load(tmp_path)
    assert receipt.adapted_from == 1 and receipt.status == "applied"
    [r] = receipt.runs
    assert (r.run_id, r.closed, r.starting_fingerprint) == (run, False, "fp")
    step = receipt.steps[f"{run}:v1"]
    assert (step.status, step.epoch, step.files, step.sites) == ("applied", 1, ["m.py"], 1)
    assert receipt.in_effect and receipt.progress == "complete"
    assert receipt.chain_run_ids() == [run]

    ledger = load_ledger(tmp_path, receipt=receipt)
    assert ledger.adapted_from == 1
    [entry] = ledger.entries
    assert (entry.epoch, entry.step_id, entry.claim, entry.basis) == (
        1, "v1", "preserve", "same-signature")
    assert entry.anchor_new.stmt == "x = a.g()"
    entries, note, _ = in_effect_entries(tmp_path, receipt)
    assert len(entries) == 1 and note is None


def test_the_next_mutation_rewrites_the_schema_1_files_as_v2_and_archives_the_receipt(tmp_path):
    run = _v1_files(tmp_path)
    rollback_migration(tmp_path)

    archived = list((tmp_path / ".pymolt" / "archive").glob("migration_state-v1-*.json"))
    assert len(archived) == 1 and json.loads(archived[0].read_text())["schema_version"] == 1
    receipt = json.loads(receipt_path(tmp_path).read_text())
    assert receipt["schema_version"] == 2 and receipt["status"] == "rolled_back"
    assert receipt["steps"][f"{run}:v1"]["status"] == "rolled_back"
    ledger = json.loads(ledger_path(tmp_path).read_text())
    assert ledger["schema"] == "pymolt.apply_ledger/2"
    assert [(e["seq"], e["event"], e.get("restored")) for e in ledger["events"]] == [
        (1, "apply", None), (2, "rollback", True)]
    state = json.loads((tmp_path / ".pymolt" / "migrations" / run / "state.json").read_text())
    assert state["status"] == "rolled_back" and state["steps"] == {"v1": "rolled_back"}


def test_a_schema_1_rollback_reads_as_its_v1_status(tmp_path):
    run = _v1_files(tmp_path, status="rolled_back", files_restored=[str(tmp_path / "m.py")])
    receipt = MigrationReceipt.load(tmp_path)
    step = receipt.steps[f"{run}:v1"]
    assert (step.status, step.epoch, step.files_restored) == ("rolled_back", None, ["m.py"])
    assert not receipt.in_effect and receipt.status == "rolled_back"

    noop = tmp_path / "noop"
    noop.mkdir()
    run = _v1_files(noop, status="rolled_back", files_restored=[])
    receipt = MigrationReceipt.load(noop)
    assert receipt.in_effect and receipt.status == "rolled_back"      # it restored nothing
    assert receipt.steps[f"{run}:v1"].epoch == 1
