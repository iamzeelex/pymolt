"""Ledger v2: events and entries, epochs, attribution by step, and the schema-1 reader."""
from __future__ import annotations

import json

import pytest

from pymolt.apply_ledger import (
    LEDGER_SCHEMA,
    EntryAttrs,
    Ledger,
    LedgerEntry,
    LedgerEvent,
    append_ledger,
    build_entries,
    entries_for,
    in_effect_entries,
    ledger_path,
    load_ledger,
)
from pymolt.codemods.models import CodemodPattern, DependencyMigration, FilePreview
from pymolt.migration_plan import (
    MigrationPlan,
    _ledger_steps,
    apply_migration_plan,
    create_migration_plan,
    rollback_migration,
)
from pymolt.migration_state import MigrationReceipt

PRESERVE = EntryAttrs(kind="rename-call", rule="rename-call:a.f→b.g", claim="preserve",
                      basis="same-signature", old_symbol="a.f", new_symbol="b.g")


def _event(kind, run="r1", step="mech-1", **kw):
    return LedgerEvent(event=kind, run_id=run, step_id=step, at="2026-11-17T00:00:00+00:00",
                       **kw)


def _entry(run="r1", step="mech-1", file="m.py"):
    return LedgerEntry(run_id=run, step_id=step, file=file, claim="preserve")


# ── epochs ────────────────────────────────────────────────────────────────────────────────


def test_an_epoch_is_the_seq_of_the_apply_or_record_that_opened_it():
    ledger = Ledger()
    for event in (_event("apply"), _event("rollback", restored=True), _event("apply"),
                  _event("abort"), _event("accept")):
        ledger.events.append(event.model_copy(update={"seq": ledger.next_seq()}))
    assert ledger.current_epochs() == {("r1", "mech-1"): 3}
    assert ledger.step_states() == {("r1", "mech-1"): "applied"}


def test_a_rollback_that_restored_no_file_keeps_the_epoch_and_a_skip_ends_it():
    ledger = Ledger(events=[
        _event("apply", seq=1), _event("rollback", seq=2, restored=False),
        _event("apply", step="mech-2", seq=3), _event("skip", step="mech-2", seq=4),
    ])
    assert ledger.current_epochs() == {("r1", "mech-1"): 1}
    assert ledger.step_states() == {("r1", "mech-1"): "rolled_back",
                                    ("r1", "mech-2"): "skipped"}


def test_only_the_entries_of_current_epochs_of_chain_runs_are_in_effect(tmp_path):
    ledger = append_ledger(tmp_path, [
        (_event("apply"), [_entry()]),                                 # epoch 1, ended below
        (_event("rollback", restored=True), []),
        (_event("apply"), [_entry(), _entry(file="n.py")]),            # epoch 3
        (_event("apply", run="r2", step="mech-1"), [_entry(run="r2")]),  # epoch 4, run r2
    ])
    assert [e.epoch for e in ledger.entries] == [1, 3, 3, 4]
    assert [(e.epoch, e.file) for e in ledger.in_effect({"r1"})] == [(3, "m.py"), (3, "n.py")]
    assert [e.run_id for e in ledger.in_effect({"r1", "r2"})] == ["r1", "r1", "r2"]
    assert ledger.epoch_event("r1", "mech-1").seq == 3


def test_a_re_record_supersedes_the_earlier_one(tmp_path):
    """A re-record opens a new epoch for the whole step; the earlier one is audit data."""
    first = entries_for("m.py", "x = a.f()\n", "x = b.g()\n", PRESERVE, run_id="r1",
                        step_id="beh-1")
    again = entries_for("m.py", "x = a.f()\n", "x = b.g(1)\n", PRESERVE, run_id="r1",
                        step_id="beh-1")
    ledger = append_ledger(tmp_path, [(_event("record", step="beh-1", executor="agent"), first)])
    ledger = append_ledger(tmp_path, [(_event("record", step="beh-1", executor="agent"), again)])
    [entry] = ledger.in_effect({"r1"})
    assert entry.epoch == 2 and entry.anchor_new.stmt == "x = b.g(1)"
    assert len(ledger.entries) == 2                       # the superseded one is kept
    assert load_ledger(tmp_path, receipt=None).current_epochs() == {("r1", "beh-1"): 2}


def test_the_ledger_file_is_v2_append_only_and_round_trips(tmp_path):
    append_ledger(tmp_path, [(_event("apply", files=[]), [_entry()])])
    data = json.loads(ledger_path(tmp_path).read_text())
    assert data["schema"] == LEDGER_SCHEMA and set(data) == {"schema", "events", "entries"}
    assert data["entries"][0]["epoch"] == 1
    assert load_ledger(tmp_path, receipt=None).model_dump() == Ledger.model_validate(
        data).model_dump()


# ── entries name statements ───────────────────────────────────────────────────────────────


def test_entries_pair_replaced_statements_and_name_added_imports():
    before = "import a\n\n\ndef f():\n    # note\n    return a.f(\n        1,\n    )\n"
    after = ("import a\nimport b\n\n\ndef f():\n    # other note\n    return b.g(\n"
             "        1,\n    )\n")
    entries = entries_for("m.py", before, after, PRESERVE, run_id="r1", step_id="mech-1")
    by_kind = {(e.anchor_old is not None, e.anchor_new is not None): e for e in entries}
    added = by_kind[(False, True)]
    assert (added.anchor_new.stmt, added.old_lines, added.new_lines) == ("import b", None, (2, 2))
    replaced = by_kind[(True, True)]
    assert replaced.anchor_old.scope == replaced.anchor_new.scope == "f"
    assert replaced.anchor_old.stmt.startswith("return a.f(")
    assert (replaced.old_lines, replaced.new_lines) == ((6, 6), (7, 7))   # clipped to the region
    assert len(entries) == 2                     # the comment-only change is no statement's
    assert {(e.claim, e.basis, e.old_symbol, e.new_symbol) for e in entries} == {
        ("preserve", "same-signature", "a.f", "b.g")}


def test_a_text_nothing_reads_is_recorded_by_its_lines_only():
    before, after = "x = (\n", "y = (\n"                  # neither parses nor tokenizes
    [entry] = entries_for("m.py", before, after, PRESERVE, run_id="r1", step_id="mech-1")
    assert entry.anchor_old is None and entry.anchor_new is None
    assert (entry.old_lines, entry.new_lines) == ((1, 1), (1, 1))


# ── apply and rollback write it ───────────────────────────────────────────────────────────


def _pattern(old, new, **kw):
    return CodemodPattern(old_qualname=old, new_qualname=new, kind="rename-call", **kw)


def _plan(tmp_path, files, recorder=None):
    """A plan of ``{path: (old, new, [patterns])}``, applied without a baseline. Without a
    ``recorder`` (the preview walk's recipe chains) the builder keeps each file's change in one
    whole-file step."""
    previews = []
    for name, (old, new, patterns) in files.items():
        (tmp_path / name).write_text(old, encoding="utf-8")
        previews.append(FilePreview(path=str(tmp_path / name), old_source=old, new_source=new,
                                    sites=len(patterns), patterns=patterns))
    return create_migration_plan(
        tmp_path, previews, [DependencyMigration("a", "1.0", "2.0")], target_python="3.12",
        source_manifest="requirements.txt", target_manifest_path=None, files_scanned=len(files),
        predict=False, recorder=recorder)


@pytest.mark.parametrize("steps", ["builder", "none"])
def test_a_whole_file_step_attributes_each_region_to_its_recipe_with_its_claim(tmp_path, steps):
    """The v1 attribution, kept for the one case a step cannot say: a delta-checked rename
    vouches for its region, a bare pattern for nothing. (A region is a difflib opcode, as in
    v1: two adjacent rewritten lines are one region, attributed to one recipe.) Both kinds of
    whole-file step: the builder's, for a file with no recorded chain, and the synthetic
    ``v1`` of a plan with no steps."""
    old = "import a\nx = a.f()\nz = 1\ny = a.h()\n"
    new = "import a\nx = a.g()\nz = 1\ny = a.k()\n"
    checked = _pattern("a.f", "a.g", evidence=["pymolt.delta DEPRECATED: a.f -> a.g"])
    plan = _plan(tmp_path, {"m.py": (old, new, [checked, _pattern("a.h", "a.k")])})
    [whole] = [s for s in plan.steps if s.executor == "pymolt"]
    assert whole.do.recipe.kind == "whole-file"
    if steps == "none":
        plan.steps = []
    receipt = apply_migration_plan(tmp_path, plan, allow_no_baseline=True)

    ledger = load_ledger(tmp_path)
    [event] = ledger.events
    step_id = whole.id if steps == "builder" else "v1"
    assert (event.run_id, event.step_id, event.files[0].before) == (
        receipt.run_id, step_id, plan.changes[0].old_sha256)
    claims = {e.anchor_new.stmt: (e.step_id, e.rule, e.claim, e.basis) for e in ledger.entries}
    assert claims == {
        "x = a.g()": (step_id, "rename-call:a.f→a.g", "preserve", "same-signature"),
        "y = a.k()": (step_id, "rename-call:a.h→a.k", None, None),
    }
    v1 = build_entries("m.py", old, new, plan.changes[0].recipes, plan.changes[0].claims)
    assert [e["claim"] for e in v1] == ["same-signature", None]    # the same split as v1


def test_the_builders_steps_are_recorded_each_with_its_own_claim_and_anchors(tmp_path):
    """A plan built from the walk's recorded chain has one step per recipe. Each step's
    event names its own before- and after-text, and each entry its own step, claim, basis,
    symbols and anchors made on that step's texts."""
    from pymolt.workorder.blobs import get_text
    from pymolt.workorder.builder import StepRecorder

    t0 = "import a\nx = a.f()\nz = 1\ny = a.h()\n"
    t1 = "import a\nx = a.g()\nz = 1\ny = a.h()\n"
    t2 = "import a\nx = a.g()\nz = 1\ny = a.k()\n"
    checked = _pattern("a.f", "a.g", evidence=["pymolt.delta MOVED: a.f -> a.g"])
    bare = _pattern("a.h", "a.k")
    recorder = StepRecorder()
    recorder.record(checked, str(tmp_path / "m.py"), t0, t1)
    recorder.record(bare, str(tmp_path / "m.py"), t1, t2)
    plan = _plan(tmp_path, {"m.py": (t0, t2, [checked, bare])}, recorder=recorder)
    first, second = [s for s in plan.steps if s.executor == "pymolt"]
    assert (first.do.recipe.kind, first.claim, second.claim) == ("pattern", "preserve", None)
    assert [s.id for s, _files in _ledger_steps(plan)] == [first.id, second.id]
    receipt = apply_migration_plan(tmp_path, plan, allow_no_baseline=True)

    ledger = load_ledger(tmp_path)
    e1, e2 = ledger.events
    assert [(e.seq, e.step_id) for e in ledger.events] == [(1, first.id), (2, second.id)]
    assert (get_text(tmp_path, e1.files[0].before), get_text(tmp_path, e1.files[0].after),
            get_text(tmp_path, e2.files[0].after)) == (t0, t1, t2)
    assert e2.files[0].before == e1.files[0].after
    assert [(e.epoch, e.step_id, e.claim, e.basis, e.old_symbol, e.new_symbol,
             e.anchor_old.stmt, e.anchor_new.stmt) for e in ledger.entries] == [
        (1, first.id, "preserve", "same-object", "a.f", "a.g", "x = a.f()", "x = a.g()"),
        (2, second.id, None, None, "a.h", "a.k", "y = a.h()", "y = a.k()")]
    # The anchors are the ones the plan made on each step's own texts.
    assert ledger.entries[0].anchor_new.key == first.edits[0].anchors_new[0].key
    assert ledger.entries[1].anchor_old.key == second.edits[0].anchors_old[0].key
    saved = MigrationReceipt.load(tmp_path)
    assert {k.split(":", 1)[1]: (r.epoch, r.claim) for k, r in saved.steps.items()} == {
        first.id: (1, "preserve"), second.id: (2, None)}
    assert saved.progress == "complete" and saved.run_id == receipt.run_id
    # Steps whose hunks do not chain to the plan's text fall back to the whole-file step.
    partial = plan.model_copy(update={"steps": [s for s in plan.steps if s.id != second.id]})
    assert [s.id for s, _files in _ledger_steps(partial)] == ["v1"]


def test_a_plan_whose_whole_file_step_cannot_be_built_still_applies(tmp_path, monkeypatch):
    """A plan with no steps gets the synthetic ``v1``; should building it fail, the step is
    made without its anchors and the apply stands."""
    from pymolt.workorder import v1

    def broken(plan):
        raise RecursionError("too deep")

    monkeypatch.setattr(v1, "v1_step", broken)
    plan = _plan(tmp_path, {"m.py": ("x = a.f()\n", "x = a.g()\n", [_pattern("a.f", "a.g")])})
    plan.steps = []
    apply_migration_plan(tmp_path, plan, allow_no_baseline=True)
    [event] = load_ledger(tmp_path).events
    assert event.step_id == "v1" and (tmp_path / "m.py").read_text() == "x = a.g()\n"


def test_a_step_whose_statements_cannot_be_anchored_is_recorded_by_its_lines(tmp_path,
                                                                            monkeypatch):
    from pymolt import apply_ledger

    def broken(text, python):
        raise RecursionError("too deep")

    monkeypatch.setattr(apply_ledger, "_index", broken)
    plan = _plan(tmp_path, {"m.py": ("x = a.f()\n", "x = a.g()\n", [_pattern("a.f", "a.g")])})
    apply_migration_plan(tmp_path, plan, allow_no_baseline=True)
    [entry] = load_ledger(tmp_path).entries
    assert entry.step_id == plan.steps[0].id
    assert (entry.anchor_old, entry.anchor_new, entry.new_lines) == (None, None, (1, 1))


def test_rollback_ends_the_runs_epochs_and_a_no_op_run_keeps_its_own(tmp_path):
    plan = _plan(tmp_path, {"m.py": ("x = a.f()\n", "x = a.g()\n", [_pattern("a.f", "a.g")])})
    receipt = apply_migration_plan(tmp_path, plan, allow_no_baseline=True)
    rollback_migration(tmp_path)
    ledger = load_ledger(tmp_path)
    rollback = ledger.events[-1]
    assert (rollback.event, rollback.run_id, rollback.restored) == (
        "rollback", receipt.run_id, True)
    assert rollback.files[0].after == plan.changes[0].old_sha256
    assert ledger.current_epochs() == {}
    entries, note, _ = in_effect_entries(tmp_path, MigrationReceipt.load(tmp_path))
    assert entries is None and "no apply run is in effect" in note


def test_a_run_that_changed_no_file_leaves_a_known_empty_ledger(tmp_path):
    """Its edits are none, not unknown: the diff does not say "no apply ledger"."""
    plan = create_migration_plan(
        tmp_path, [], [DependencyMigration("a", "1.0", "2.0")], target_python="3.12",
        source_manifest="requirements.txt", target_manifest_path=None, files_scanned=0,
        predict=False)
    apply_migration_plan(tmp_path, plan, allow_no_baseline=True)
    assert load_ledger(tmp_path).events == []
    entries, note, _ = in_effect_entries(tmp_path, MigrationReceipt.load(tmp_path))
    assert (entries, note) == ([], None)


def test_a_failed_receipt_write_leaves_the_ledger_as_it_was(tmp_path, monkeypatch):
    plan = _plan(tmp_path, {"m.py": ("x = a.f()\n", "x = a.g()\n", [_pattern("a.f", "a.g")])})

    def fail(self, project_dir):
        raise OSError("disk full")

    monkeypatch.setattr(MigrationReceipt, "save", fail)
    with pytest.raises(OSError, match="disk full"):
        apply_migration_plan(tmp_path, plan, allow_no_baseline=True)
    assert not ledger_path(tmp_path).exists()
    assert (tmp_path / "m.py").read_text() == "x = a.f()\n"


# ── schema 1 ──────────────────────────────────────────────────────────────────────────────


def _v1_run(tmp_path, *, status="applied", files_restored=None):
    """A schema-1 run as the H stack left it: frozen plan, v1 receipt, v1 ledger."""
    old, new = "import a\nx = a.f()\n", "import a\nx = b.g()\n"
    plan = _plan(tmp_path, {"m.py": (old, new, [_pattern("a.f", "b.g")])})
    run = plan.plan_id
    run_dir = tmp_path / ".pymolt" / "migrations" / run
    run_dir.mkdir(parents=True)
    plan.save(tmp_path, run_dir / "plan.json")
    entries = [{**e, "run_id": run} for e in build_entries(
        "m.py", old, new, ["rename-call:a.f→b.g"], {"rename-call:a.f→b.g": "same-signature"})]
    ledger_path(tmp_path).write_text(json.dumps({
        "schema": "pymolt.apply_ledger/1", "run_id": run, "applied_at": "2026-11-01T00:00:00",
        "entries": entries}))
    receipt = MigrationReceipt(run_id=run, plan_id=run, target_python="3.12",
                               source_manifest="requirements.txt", status=status,
                               files_restored=files_restored, plan_path=str(run_dir / "plan.json"))
    return run, receipt


def test_a_v1_ledger_is_read_as_one_epoch_of_step_v1_anchored_on_its_frozen_plan(tmp_path):
    run, receipt = _v1_run(tmp_path)
    ledger = load_ledger(tmp_path, receipt=receipt)
    assert ledger.adapted_from == 1
    assert [(e.seq, e.event, e.run_id, e.step_id) for e in ledger.events] == [
        (1, "apply", run, "v1")]
    [entry] = ledger.entries
    assert (entry.epoch, entry.step_id, entry.claim, entry.basis) == (
        1, "v1", "preserve", "same-signature")
    assert (entry.anchor_old.stmt, entry.anchor_new.stmt) == ("x = a.f()", "x = b.g()")
    entries, note, _ = in_effect_entries(tmp_path, receipt)
    assert [e.run_id for e in entries] == [run] and note is None


def test_a_rolled_back_v1_run_reads_as_an_ended_epoch_and_the_first_write_makes_it_v2(tmp_path):
    run, receipt = _v1_run(tmp_path, status="rolled_back", files_restored=["m.py"])
    ledger = load_ledger(tmp_path, receipt=receipt)
    assert [(e.seq, e.event, e.restored) for e in ledger.events] == [
        (1, "apply", None), (2, "rollback", True)]
    assert ledger.current_epochs() == {}

    append_ledger(tmp_path, [(_event("apply", run="f" * 32, step="v1"), [])], ledger=ledger)
    data = json.loads(ledger_path(tmp_path).read_text())
    assert data["schema"] == LEDGER_SCHEMA
    assert [e["seq"] for e in data["events"]] == [1, 2, 3]
    assert data["entries"][0]["anchor_new"]["stmt"] == "x = b.g()"


def test_a_v1_ledger_with_no_frozen_plan_pairs_symbols_only(tmp_path):
    ledger_path(tmp_path).parent.mkdir(parents=True)
    ledger_path(tmp_path).write_text(json.dumps({
        "schema": "pymolt.apply_ledger/1", "run_id": "e" * 32, "applied_at": "t",
        "entries": [{"file": "m.py", "old_lines": [1, 1], "new_lines": [1, 1],
                     "rule": "rename-call:a.f→b.g", "claim": None, "kind": "rename-call",
                     "old_symbol": "a.f", "new_symbol": "b.g"}]}))
    ledger = load_ledger(tmp_path, receipt=None)
    [entry] = ledger.entries
    assert entry.anchor_old is None and entry.new_lines == (1, 1) and entry.claim is None
    assert any("no frozen plan" in n for n in ledger.notes)


def test_an_archived_v1_ledger_is_ignored(tmp_path):
    archive = tmp_path / ".pymolt" / "archive"
    archive.mkdir(parents=True)
    (archive / "apply_ledger-2026-01-01.json").write_text(json.dumps({
        "schema": "pymolt.apply_ledger/1", "run_id": "r", "entries": [{"file": "m.py"}]}))
    assert load_ledger(tmp_path, receipt=None) is None


def test_a_v1_plan_applies_as_step_v1(tmp_path):
    plan = _plan(tmp_path, {"m.py": ("x = a.f()\n", "x = a.g()\n", [_pattern("a.f", "a.g")])})
    data = plan.model_dump(mode="json")
    data.update(schema_version=1)
    data.pop("steps"), data.pop("adapted_from")
    v1 = MigrationPlan.model_validate({**data, "schema_version": 2})
    v1.save(tmp_path)
    path = tmp_path / ".pymolt" / "migration_plan.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "schema_version": 1}))
    loaded = MigrationPlan.load(tmp_path)
    assert loaded.adapted_from == 1 and [s.id for s in loaded.steps] == ["v1"]
    apply_migration_plan(tmp_path, loaded, allow_no_baseline=True)
    assert [e.step_id for e in load_ledger(tmp_path).events] == ["v1"]
