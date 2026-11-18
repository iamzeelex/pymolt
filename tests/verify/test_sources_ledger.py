"""Edit provenance inputs: source snapshots, the apply ledger, snapshot contact maps and the
DiffContext wiring."""
from __future__ import annotations

import json

from pymolt.apply_ledger import (
    LEDGER_SCHEMA,
    EntryAttrs,
    LedgerEvent,
    append_ledger,
    build_entries,
    claim_for,
    entries_for,
    load_ledger,
)
from pymolt.codemods.models import CodemodPattern
from pymolt.verify import service
from pymolt.verify.contact_map import contacts_by_line
from pymolt.verify.models import CaptureMode, ContractSlot, ContractState


def write_ledger(root, run_id, applied_at, files):
    """A schema-1 ledger, as ``pymolt apply`` wrote it before ledger v2 (read through the
    v1 adapter): ``files`` = ``(path, before, after, recipes, claims)``."""
    entries = [{**entry, "run_id": run_id}
               for rel, before, after, recipes, claims in files
               for entry in build_entries(rel, before, after, recipes, claims)]
    path = root / ".pymolt" / "apply_ledger.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": "pymolt.apply_ledger/1", "run_id": run_id,
                                "applied_at": applied_at, "entries": entries}))


def _trace(path, *files):
    path.write_text("".join(
        json.dumps({"sym": "x.f", "where": {"file": f, "line": 1}}) + "\n" for f in files))


class TestSourceSnapshot:
    def test_every_project_file_with_a_fact_is_stored_with_its_hash(self, tmp_path):
        (tmp_path / "app").mkdir()
        (tmp_path / "app" / "a.py").write_text("import x\nx.f()\n")
        (tmp_path / "app" / "unused.py").write_text("pass\n")
        trace = tmp_path / "baseline.jsonl"
        _trace(trace, "app/a.py", "/elsewhere/lib.py", "/x/site-packages/y.py")
        out = service.record_sources(trace, tmp_path)
        data = service._read_snapshot(out)
        assert list(data["files"]) == ["app/a.py"] and data["fact_files"] == ["app/a.py"]
        assert data["files"]["app/a.py"]["text"] == "import x\nx.f()\n"
        assert len(data["files"]["app/a.py"]["sha256"]) == 64
        assert service.load_sources(out) == {"app/a.py": "import x\nx.f()\n"}

    def test_a_file_over_the_cap_is_skipped_and_recorded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(service, "SOURCES_MAX_BYTES", 5)
        (tmp_path / "big.py").write_text("x = 1\ny = 2\n")
        trace = tmp_path / "t.jsonl"
        _trace(trace, "big.py")
        data = service._read_snapshot(service.record_sources(trace, tmp_path))
        assert data["files"] == {} and data["skipped"][0]["file"] == "big.py"
        assert "over" in data["skipped"][0]["reason"]

    def test_archiving_a_capture_keeps_its_snapshot(self, tmp_path):
        traces = tmp_path / ".pymolt" / "contract_traces"
        traces.mkdir(parents=True)
        (tmp_path / "a.py").write_text("x\n")
        trace = traces / "baseline.jsonl"
        _trace(trace, "a.py")
        service.record_sources(trace, tmp_path)
        ContractState(baseline=ContractSlot(
            trace_path=str(trace), captured_at="2026-01-02T03:04:05", mode=CaptureMode.TEST_SUITE,
            sources_path=str(service.sources_path(trace)))).save(service._state_path(tmp_path))
        archived = service.archive_existing_capture(tmp_path, "baseline")
        assert archived and service.sources_path(archived).is_file()
        assert not service.sources_path(trace).exists()


class TestApplyLedger:
    def test_a_multi_line_rewrite_and_an_added_import_are_exact(self):
        old = "import a\n\nx = a.f(\n    1,\n    2,\n)\ny = 3\n"
        new = "import a\nimport b\n\nx = b.g(\n    1,\n    2,\n)\ny = 3\n"
        entries = build_entries("m.py", old, new, ["rename-call:a.f→b.g"],
                                {"rename-call:a.f→b.g": "same-signature"})
        by_new = {tuple(e["new_lines"] or ()): e for e in entries}
        assert by_new[(2, 2)]["old_lines"] is None            # the added import line
        rewrite = by_new[(4, 4)]
        assert rewrite["old_lines"] == [3, 3]
        assert (rewrite["rule"], rewrite["claim"], rewrite["old_symbol"],
                rewrite["new_symbol"], rewrite["kind"]) == (
            "rename-call:a.f→b.g", "same-signature", "a.f", "b.g", "rename-call")

    def test_a_reapply_appends_to_the_ledger_and_archives_nothing(self, tmp_path):
        def apply(run, at):
            event = LedgerEvent(event="apply", run_id=run, step_id="v1", at=at)
            return event, entries_for("m.py", "a = 1\n", "b = 1\n", EntryAttrs(),
                                      run_id=run, step_id="v1")

        append_ledger(tmp_path, [apply("r1", "2026-01-01T00:00:00")])
        append_ledger(tmp_path, [apply("r2", "2026-01-02T00:00:00")])
        assert not (tmp_path / ".pymolt" / "archive").exists()
        current = json.loads((tmp_path / ".pymolt" / "apply_ledger.json").read_text())
        assert current["schema"] == LEDGER_SCHEMA
        assert [(e["seq"], e["run_id"]) for e in current["events"]] == [(1, "r1"), (2, "r2")]
        ledger = load_ledger(tmp_path, receipt=None)
        assert [(e.epoch, e.run_id, e.new_lines) for e in ledger.entries] == [
            (1, "r1", (1, 1)), (2, "r2", (1, 1))]

    def test_claims_follow_the_pattern(self):
        def pat(**kw):
            return CodemodPattern(old_qualname="a.f", new_qualname="b.f", kind="rename-call", **kw)
        assert claim_for(pat(scope="python")) == "python-fixer"
        assert claim_for(pat(evidence=["pymolt.delta MOVED: a.f -> b.f"])) == "same-object"
        assert claim_for(pat(evidence=["pymolt.delta DEPRECATED: a.f -> b.f"])) == "same-signature"
        assert claim_for(pat()) is None


def test_contacts_by_line_come_from_the_snapshot_text(tmp_path):
    sources = {"app/m.py": "import requests\n\n\ndef f():\n    return requests.get('u')\n",
               "app/own.py": "import os\nfrom app import m\nm.f()\n"}
    got = contacts_by_line(sources, tmp_path)
    assert got == {"app/m.py": {5: {"requests.get"}}}
    assert contacts_by_line(sources, tmp_path) == got      # second call: served from the cache


def test_the_context_is_built_from_slots_ledger_and_plan_and_reaches_the_diff(
        tmp_path, monkeypatch):
    traces = tmp_path / ".pymolt" / "contract_traces"
    traces.mkdir(parents=True)
    old, new = traces / "baseline.jsonl", traces / "post_migration.jsonl"
    (tmp_path / "m.py").write_text("import requests\nrequests.get('u')\n")
    for t in (old, new):
        _trace(t, "m.py")
        service.record_sources(t, tmp_path)
    write_ledger(tmp_path, "r", "2026-01-01T00:00:00",
                 [("m.py", "a.f()\n", "b.g()\n", ["rename-call:a.f→b.g"],
                   {"rename-call:a.f→b.g": "same-object"})])
    from pymolt.migration_state import MigrationReceipt

    MigrationReceipt(run_id="r", target_python="3.12",
                     source_manifest="requirements.txt").save(tmp_path)
    ctx = service.build_diff_context(tmp_path, new, old)
    assert ctx.old_sources == ctx.new_sources == {"m.py": "import requests\nrequests.get('u')\n"}
    assert ctx.old_contacts == {"m.py": {2: {"requests.get"}}}
    assert (ctx.edits[0]["claim"], ctx.edits[0]["basis"]) == ("preserve", "same-object")
    assert ctx.successors == {"a.f": "b.g"}
    # an older capture without snapshots: no sources, no contacts
    old.with_name("baseline.jsonl.sources.json.gz").unlink()
    assert service.build_diff_context(tmp_path, new, old).old_sources is None

    seen = {}
    from pymolt.verify import report as report_mod

    def spy(*a, **kw):
        seen["context"] = kw.get("context")
        raise ValueError("spy: the diff itself is not under test")
    monkeypatch.setattr(report_mod, "diff_facts", spy)
    ContractState(
        baseline=ContractSlot(trace_path=str(old), captured_at="t1", mode=CaptureMode.TEST_SUITE),
        post_migration=ContractSlot(trace_path=str(new), captured_at="t2",
                                    mode=CaptureMode.TEST_SUITE),
    ).save(service._state_path(tmp_path))
    service.build_contract_report_from_state(tmp_path)
    assert seen["context"] is not None and seen["context"].edits[0]["rule"]


class TestSnapshotCoverage:
    def _project(self, tmp_path):
        (tmp_path / "a.py").write_text("import x\nx.f()\n")
        (tmp_path / "helper.py").write_text("def h():\n    pass\n")
        (tmp_path / "idle.py").write_text("y = 1\n" * 10)
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "deep.py").write_text("z = 2\n")
        trace = tmp_path / "baseline.jsonl"
        _trace(trace, "a.py")
        return trace

    def test_a_baseline_stores_the_whole_tree_with_facts_marked(self, tmp_path):
        trace = self._project(tmp_path)
        data = service._read_snapshot(service.record_sources(trace, tmp_path, whole_tree=True))
        assert set(data["files"]) == {"a.py", "helper.py", "idle.py", "pkg/deep.py"}
        assert data["fact_files"] == ["a.py"]
        # without whole_tree only the facts' file is known
        data = service._read_snapshot(service.record_sources(trace, tmp_path))
        assert set(data["files"]) == {"a.py"}

    def test_the_budget_keeps_fact_files_first_then_the_smallest_and_records_the_rest(
            self, tmp_path, monkeypatch):
        trace = self._project(tmp_path)
        monkeypatch.setattr(service, "SOURCES_TOTAL_BUDGET", 45)
        data = service._read_snapshot(service.record_sources(trace, tmp_path, whole_tree=True))
        assert "a.py" in data["files"]                         # facts are never budgeted out
        assert "pkg/deep.py" in data["files"] and "helper.py" in data["files"]  # smallest first
        assert data["left_out"] == ["idle.py"] and data["left_out_total"] == 1

    def test_a_post_capture_stores_the_union_of_both_sides_fact_files(self, tmp_path):
        traces = tmp_path / ".pymolt" / "contract_traces"
        traces.mkdir(parents=True)
        for name in ("a.py", "helper.py", "gone.py"):
            (tmp_path / name).write_text("pass\n")
        old, new = traces / "baseline.jsonl", traces / "post_migration.jsonl"
        _trace(old, "a.py", "gone.py")
        service._record_sources_for(old, tmp_path, "baseline")
        ContractState(baseline=ContractSlot(
            trace_path=str(old), captured_at="t", mode=CaptureMode.TEST_SUITE,
            sources_path=str(service.sources_path(old)))).save(service._state_path(tmp_path))
        _trace(new, "a.py", "helper.py")          # helper gained calls; gone.py lost all
        service._record_sources_for(new, tmp_path, "post_migration")
        post = service._read_snapshot(service.sources_path(new))
        assert set(post["files"]) == {"a.py", "helper.py", "gone.py"}
        assert post["fact_files"] == ["a.py", "helper.py"]
        old_data = service._read_snapshot(service.sources_path(old))
        assert "helper.py" in old_data["files"]    # the whole tree: the old text exists


def test_the_plans_type_relocations_list_reaches_the_context_as_a_map(tmp_path, monkeypatch):
    """The plan carries [{"old", "new", "via"}]; the diff wants old type -> new type."""
    from types import SimpleNamespace

    from pymolt import migration_plan, migration_state

    traces = tmp_path / ".pymolt" / "contract_traces"
    traces.mkdir(parents=True)
    old, new = traces / "baseline.jsonl", traces / "post_migration.jsonl"
    for t in (old, new):
        _trace(t, "m.py")
    plan = SimpleNamespace(impacts=[], risks=None, type_relocations=[
        {"old": "pydantic.error_wrappers.ValidationError",
         "new": "pydantic_core._pydantic_core.ValidationError", "via": "pydantic.ValidationError"},
        {"old": "incomplete.Entry"},
    ])
    receipt = SimpleNamespace(status="applied", in_effect=True, plan_path="plan.json")
    monkeypatch.setattr(migration_state.MigrationReceipt, "load",
                        staticmethod(lambda root: receipt))
    monkeypatch.setattr(migration_plan.MigrationPlan, "load",
                        staticmethod(lambda root, path: plan))
    ctx = service.build_diff_context(tmp_path, new, old)
    assert ctx.type_relocations == {
        "pydantic.error_wrappers.ValidationError": "pydantic_core._pydantic_core.ValidationError"}


def test_the_ledger_counts_only_while_its_run_is_in_effect(tmp_path):
    """A rollback that restored files leaves the ledger behind; a hand edit made after it on
    one of those lines must not be judged by the claim pymolt made for the rolled-back run."""
    from pymolt.apply_ledger import in_effect_entries
    from pymolt.migration_state import MigrationReceipt

    def in_effect(receipt):
        entries, note, _ledger = in_effect_entries(tmp_path, receipt)
        return ([e.run_id for e in entries] if entries is not None else None), note

    write_ledger(tmp_path, "r1", "2026-01-01T00:00:00",
                 [("m.py", "a.f()\n", "b.g()\n", ["rename-call:a.f→b.g"],
                   {"rename-call:a.f→b.g": "same-object"})])
    applied = MigrationReceipt(run_id="r1", target_python="3.12",
                               source_manifest="requirements.txt")

    assert in_effect(applied) == (["r1"], None)
    restored = applied.model_copy(update={"status": "rolled_back", "files_restored": ["m.py"]})
    assert in_effect(restored) == (
        None, "the apply ledger of run r1 is not used (no apply run is in effect): edits are "
              "judged as hand edits")
    noop = applied.model_copy(update={"status": "rolled_back", "files_restored": []})
    assert in_effect(noop) == (["r1"], None)            # it restored nothing: in effect
    other = applied.model_copy(update={"run_id": "r2"})
    assert in_effect(other)[0] is None
    assert "run r2 is the one in effect" in in_effect(other)[1]
    assert in_effect(None)[0] is None


def test_verify_says_when_it_left_the_ledger_out(tmp_path):
    from pymolt.migration_state import MigrationReceipt

    traces = tmp_path / ".pymolt" / "contract_traces"
    traces.mkdir(parents=True)
    old, new = traces / "baseline.jsonl", traces / "post_migration.jsonl"
    (tmp_path / "m.py").write_text("import requests\nrequests.get('u')\n")
    for t in (old, new):
        _trace(t, "m.py")
        service.record_sources(t, tmp_path)
    write_ledger(tmp_path, "r1", "2026-01-01T00:00:00",
                 [("m.py", "a.f()\n", "b.g()\n", ["rename-call:a.f→b.g"], {})])
    MigrationReceipt(run_id="r1", target_python="3.12", source_manifest="requirements.txt",
                     status="rolled_back", files_restored=["m.py"]).save(tmp_path)
    ContractState(
        baseline=ContractSlot(trace_path=str(old), captured_at="t1", mode=CaptureMode.TEST_SUITE),
        post_migration=ContractSlot(trace_path=str(new), captured_at="t2",
                                    mode=CaptureMode.TEST_SUITE),
    ).save(service._state_path(tmp_path))

    assert service.build_diff_context(tmp_path, new, old).edits is None
    report = service.build_contract_report_from_state(tmp_path)
    assert any("apply ledger of run r1 is not used" in n for n in report.notes)
