"""Ledger coverage (design 2.3): one alignment per file, entries located by anchors, the
strictest claim where entries overlap, and the steps a capture predates."""
from __future__ import annotations

import json

from pymolt.apply_ledger import (
    EntryAttrs,
    LedgerEvent,
    append_ledger,
    build_entries,
    entries_for,
    in_effect_entries,
)
from pymolt.codemods.client import DependencyMigration
from pymolt.codemods.models import CodemodPattern, FilePreview
from pymolt.migration_plan import apply_migration_plan, create_migration_plan
from pymolt.migration_state import MigrationReceipt, ReceiptRun, StepReceipt
from pymolt.verify import service
from pymolt.verify.facts import DiffContext, _line_map, _Provenance
from pymolt.verify.models import CaptureMode, ContractSlot, ContractState
from pymolt.workorder.coverage import ledger_coverage

PRESERVE = EntryAttrs(kind="rename-call", rule="rename-call:a.f→a.g", claim="preserve",
                      basis="same-signature", old_symbol="a.f", new_symbol="a.g")

T0 = ("import a\n"
      "\n"
      "\n"
      "def f(s):\n"
      "    s = s + 1\n"
      "    return a.f(s)\n")
T1 = T0.replace("a.f(s)", "a.g(s)")
HAND = "import os\nimport sys\nLIMIT = 3\n"          # a hand edit above, at module level


def _lines(text, needle):
    return [n for n, line in enumerate(text.splitlines(), 1) if needle in line]


def _event(kind, run="r1", step="mech-1", **kw):
    return LedgerEvent(event=kind, run_id=run, step_id=step, at="t", **kw)


def _receipt(*runs, steps=None):
    return MigrationReceipt(run_id=runs[-1], target_python="3.12",
                            source_manifest="requirements.txt",
                            runs=[ReceiptRun(run_id=r, opened_at="t") for r in runs],
                            steps=steps or {})


def test_apply_rollback_reapply_with_another_rebase_sees_exactly_one_generation(tmp_path):
    """Epoch 1 was written on T0 -> T1 (the call on line 6). It was rolled back, a person
    added three lines above, and the step was applied again, re-based: epoch 3 writes the
    same statement on line 9. Only epoch 3 is in effect, and it covers exactly the line of
    its statement in P. The line epoch 1 recorded (6) is covered by nothing, and the person's
    lines are hand edits."""
    rebased_before, rebased_after = HAND + T0, HAND + T1
    ledger = append_ledger(tmp_path, [
        (_event("apply", wrote=True), entries_for("m.py", T0, T1, PRESERVE, run_id="r1",
                                                  step_id="mech-1")),
        (_event("rollback", restored=True), []),
        (_event("apply", wrote=True), entries_for("m.py", rebased_before, rebased_after,
                                                  PRESERVE, run_id="r1", step_id="mech-1")),
    ])
    assert [(e.epoch, e.new_lines) for e in ledger.entries] == [(1, (6, 6)), (3, (9, 9))]
    receipt = _receipt("r1", steps={"r1:mech-1": StepReceipt(status="applied", epoch=3)})
    entries, note, _ = in_effect_entries(tmp_path, receipt)
    assert note is None and [e.epoch for e in entries] == [3]

    cov = ledger_coverage({"m.py": T0}, {"m.py": rebased_after}, entries)
    [located] = cov.entries
    call = _lines(rebased_after, "a.g(s)")
    assert located["epoch"] == 3 and located["located"] == {"old": [6], "new": call}
    assert cov.covering("m.py", call[0], 1)["epoch"] == 3
    assert cov.covering("m.py", 6, 1) is None                   # epoch 1's line, shifted away
    for n in _lines(rebased_after, "import os") + _lines(rebased_after, "LIMIT"):
        assert cov.covering("m.py", n, 1) is None               # the person's lines: hand
    assert not cov.not_in_capture


def test_the_ranges_an_entry_was_written_with_are_never_used(tmp_path):
    [entry] = entries_for("m.py", T0, T1, PRESERVE, run_id="r1", step_id="mech-1")
    lying = entry.model_copy(update={"new_lines": (1, 4), "old_lines": (1, 4)})
    cov = ledger_coverage({"m.py": T0}, {"m.py": T1}, [lying])
    assert cov.entries[0]["located"] == {"old": [6], "new": [6]}
    assert cov.entries[0]["recorded"] == {"old": [1, 4], "new": [1, 4]}


def test_an_entry_covers_only_lines_the_one_alignment_calls_edited():
    """Its statement spans three lines; the alignment edited one of them."""
    before = "import a\nx = a.f(\n    1,\n)\n"
    after = "import a\nx = a.g(\n    1,\n)\n"
    [entry] = entries_for("m.py", before, after, PRESERVE, run_id="r1", step_id="mech-1")
    assert (entry.anchor_new.line, entry.new_lines) == (2, (2, 2))
    cov = ledger_coverage({"m.py": before}, {"m.py": after}, [entry])
    assert cov.entries[0]["located"] == {"old": [2], "new": [2]}
    om, nm = _line_map(before, after)
    edited = {n for n, (state, _o) in nm.items() if state != "same"}
    assert set(cov.entries[0]["located"]["new"]) <= edited


def test_two_runs_and_a_hand_edit_after_apply(tmp_path):
    """R1 rewrote `a.f`, R2 (a plan made on top) rewrote `a.h`, then a person edited a third
    line. Each run covers its own statement; the person's line is a hand edit."""
    t0 = "import a\nx = a.f()\ny = 1\nz = a.h()\nw = 2\n"
    t1 = t0.replace("a.f()", "a.g()")
    t2 = t1.replace("a.h()", "a.k()")
    first = apply_migration_plan(tmp_path, _plan(tmp_path, t0, t1, "a.f", "a.g"),
                                 allow_no_baseline=True)
    second = apply_migration_plan(tmp_path, _plan(tmp_path, t1, t2, "a.h", "a.k"),
                                  allow_no_baseline=True)
    p = t2.replace("w = 2", "w = 3")                           # the hand edit after apply
    (tmp_path / "m.py").write_text(p)

    entries, note, _ = in_effect_entries(tmp_path, MigrationReceipt.load(tmp_path))
    cov = ledger_coverage({"m.py": t0}, {"m.py": p}, entries)
    assert cov.covering("m.py", 2, 1)["run_id"] == first.run_id
    assert cov.covering("m.py", 4, 1)["run_id"] == second.run_id
    assert cov.covering("m.py", 5, 1) is None
    assert cov.covering("m.py", 2, 0)["run_id"] == first.run_id    # R1's old side is B
    # R2's old statement (`z = a.h()`) is B's too: the baseline had it, unchanged by R1.
    assert cov.covering("m.py", 4, 0)["run_id"] == second.run_id
    assert not cov.not_in_capture and not cov.superseded


def _plan(tmp_path, old, new, before_sym, after_sym, claim=True):
    (tmp_path / "m.py").write_text(old)
    pattern = CodemodPattern(
        old_qualname=before_sym, new_qualname=after_sym, kind="rename-call",
        evidence=[f"pymolt.delta DEPRECATED: {before_sym} -> {after_sym}"] if claim else [])
    return create_migration_plan(
        tmp_path, [FilePreview(path=str(tmp_path / "m.py"), old_source=old, new_source=new,
                               sites=1, patterns=[pattern])],
        [DependencyMigration("a", "1.0", "2.0")], target_python="3.12",
        source_manifest="requirements.txt", target_manifest_path=None, files_scanned=1,
        predict=False)


def test_the_strictest_claim_wins_where_entries_overlap():
    def entry(claim, step, **kw):
        [e] = entries_for("m.py", T0, T1, EntryAttrs(claim=claim, **kw), run_id="r1",
                          step_id=step)
        return e

    preserve, intended, hand = entry("preserve", "mech-1"), entry("intended", "beh-1"), \
        entry(None, "mech-2")
    for group, winner in (([intended, preserve], "mech-1"), ([hand, intended], "beh-1"),
                          ([hand, preserve, intended], "mech-1")):
        cov = ledger_coverage({"m.py": T0}, {"m.py": T1}, group)
        assert cov.covering("m.py", 6, 1)["step_id"] == winner
    # An attested intended claim is the most lenient: a hand edit's judgement beats it.
    cov = ledger_coverage({"m.py": T0}, {"m.py": T1}, [intended, hand],
                          attested=lambda e: e.step_id == "beh-1")
    assert cov.covering("m.py", 6, 1)["step_id"] == "mech-2"
    # Provenance takes the same entry: pymolt vouches for the line only under preserve.
    for group, strict in (([intended, preserve], True), ([intended, hand], False)):
        ctx = DiffContext(old_sources={"m.py": T0}, new_sources={"m.py": T1},
                          edits=ledger_coverage({"m.py": T0}, {"m.py": T1}, group).entries)
        prov = _Provenance(ctx, None)
        assert prov.ledger_covers("m.py", 6, 1) is strict
        assert prov.covering("m.py", 5, 1) is None


def test_a_forged_attested_field_in_the_ledger_counts_for_nothing(tmp_path):
    [e] = entries_for("m.py", T0, T1, EntryAttrs(claim="intended"), run_id="r1", step_id="b")
    forged = {**e.as_dict(), "attested": True}
    cov = ledger_coverage({"m.py": T0}, {"m.py": T1}, [forged])
    assert cov.entries[0]["attested"] is False


def test_a_step_applied_after_the_capture_is_not_in_it():
    entries = entries_for("m.py", T0, T1, PRESERVE, run_id="r1", step_id="mech-1")
    before = ledger_coverage({"m.py": T0}, {"m.py": T0}, entries)    # P captured before apply
    assert before.not_in_capture == [("r1", "mech-1")]
    assert before.entries[0]["located"] == {"old": [], "new": []}
    assert any("mech-1" in n and "not in the post-migration capture" in n for n in before.notes)
    after = ledger_coverage({"m.py": T0}, {"m.py": T1}, entries)
    assert after.not_in_capture == []
    # A person later rewrote the step's statement: the capture has it, a later edit took it.
    edited = T1.replace("a.g(s)", "a.g(s) + 0")
    taken = ledger_coverage({"m.py": T0}, {"m.py": edited}, entries)
    assert (taken.not_in_capture, taken.superseded) == ([], [("r1", "mech-1")])


# ── through verify ────────────────────────────────────────────────────────────────────────


def _captures(tmp_path, old_text, new_text):
    traces = tmp_path / ".pymolt" / "contract_traces"
    traces.mkdir(parents=True, exist_ok=True)
    old, new = traces / "baseline.jsonl", traces / "post_migration.jsonl"
    for trace, text in ((old, old_text), (new, new_text)):
        (tmp_path / "m.py").write_text(text)
        trace.write_text(json.dumps({"sym": "a.f", "where": {"file": "m.py", "line": 6}}) + "\n")
        service.record_sources(trace, tmp_path)
    return old, new


def test_verify_locates_the_in_effect_entries_once_in_the_two_snapshots(tmp_path):
    old, new = _captures(tmp_path, T0, HAND + T1)
    append_ledger(tmp_path, [(_event("apply", run="a" * 32), entries_for(
        "m.py", HAND + T0, HAND + T1, PRESERVE, run_id="a" * 32, step_id="mech-1"))])
    _receipt("a" * 32, steps={f"{'a' * 32}:mech-1": StepReceipt(status="applied", epoch=1)}
             ).save(tmp_path)
    ctx = service.build_diff_context(tmp_path, new, old)
    [entry] = ctx.edits
    assert entry["located"] == {"old": [6], "new": [9]}
    assert ctx.successors == {"a.f": "a.g"} and ctx.not_in_capture == []


def test_verify_says_which_steps_the_capture_predates(tmp_path):
    old, new = _captures(tmp_path, T0, T0)
    run = "b" * 32
    append_ledger(tmp_path, [(_event("apply", run=run), entries_for(
        "m.py", T0, T1, PRESERVE, run_id=run, step_id="mech-1"))])
    _receipt(run, steps={f"{run}:mech-1": StepReceipt(status="applied", epoch=1)}).save(
        tmp_path)
    ctx = service.build_diff_context(tmp_path, new, old)
    assert ctx.not_in_capture == [f"{run}:mech-1"]
    assert any("step mech-1 of migration run bbbbbbbb is not in the post-migration capture"
               in n for n in ctx.notes)


def test_a_ledger_whose_run_is_not_in_effect_is_ignored_and_the_report_says_why(tmp_path):
    old, new = _captures(tmp_path, T0, T1)
    run = "c" * 32
    append_ledger(tmp_path, [
        (_event("apply", run=run), entries_for("m.py", T0, T1, PRESERVE, run_id=run,
                                               step_id="mech-1")),
        (_event("rollback", run=run, restored=True), [])])
    receipt = _receipt(run, steps={f"{run}:mech-1": StepReceipt(
        status="rolled_back", files_restored=["m.py"])})
    receipt.status = "rolled_back"
    receipt.save(tmp_path)
    ContractState(
        baseline=ContractSlot(trace_path=str(old), captured_at="t1", mode=CaptureMode.TEST_SUITE),
        post_migration=ContractSlot(trace_path=str(new), captured_at="t2",
                                    mode=CaptureMode.TEST_SUITE),
    ).save(service._state_path(tmp_path))
    assert service.build_diff_context(tmp_path, new, old).edits is None
    report = service.build_contract_report_from_state(tmp_path)
    assert any("apply ledger of run cccccccc is not used (no apply run is in effect)" in n
               for n in report.notes)


def test_without_snapshots_entries_are_located_by_the_lines_they_were_written_at(tmp_path):
    old, new = _captures(tmp_path, T0, T1)
    for trace in (old, new):
        service.sources_path(trace).unlink()
    run = "d" * 32
    append_ledger(tmp_path, [(_event("apply", run=run), entries_for(
        "m.py", T0, T1, PRESERVE, run_id=run, step_id="mech-1"))])
    _receipt(run, steps={f"{run}:mech-1": StepReceipt(status="applied", epoch=1)}).save(
        tmp_path)
    ctx = service.build_diff_context(tmp_path, new, old)
    assert ctx.edits[0]["located"] == {"old": [6], "new": [6]}
    assert any("located by the lines they were written at" in n for n in ctx.notes)


def test_a_single_run_is_located_where_v1_put_it(tmp_path):
    """The remap is the identity on one run: every line v1's ranges covered that the
    alignment calls edited is covered, and no other (an added import, a multi-line call)."""
    before = ("import a\n\n\ndef f(x):\n    y = a.f(\n        x,\n    )\n    return y\n"
              "\n\nclass K:\n    v = a.h(1)\n")
    after = ("import a\nimport b\n\n\ndef f(x):\n    y = b.g(\n        x,\n    )\n"
             "    return y\n\n\nclass K:\n    v = b.k(1)\n")
    recipes = ["rename-call:a.f→b.g", "rename-call:a.h→b.k"]
    claims = dict.fromkeys(recipes, "same-signature")
    v1 = build_entries("m.py", before, after, recipes, claims)
    om, nm = _line_map(before, after)
    edited = {n for n, (state, _o) in nm.items() if state != "same"}
    v1_new = {n for e in v1 if e["new_lines"]
              for n in range(e["new_lines"][0], e["new_lines"][1] + 1)} & edited

    from pymolt.apply_ledger import _recipe_for, attrs_for_recipe

    v2 = entries_for("m.py", before, after,
                     lambda o, n: attrs_for_recipe(_recipe_for(recipes, o, n), claims),
                     run_id="r", step_id="v1")
    cov = ledger_coverage({"m.py": before}, {"m.py": after}, v2)
    assert {n for e in cov.entries for n in e["located"]["new"]} == v1_new
    prov_v1 = _Provenance(DiffContext(old_sources={"m.py": before}, new_sources={"m.py": after},
                                      edits=v1), None)
    prov_v2 = _Provenance(DiffContext(old_sources={"m.py": before}, new_sources={"m.py": after},
                                      edits=cov.entries), None)
    for side, text in ((0, before), (1, after)):
        for n in range(1, len(text.splitlines()) + 1):
            assert prov_v1.ledger_covers("m.py", n, side) == prov_v2.ledger_covers(
                "m.py", n, side), (side, n)
