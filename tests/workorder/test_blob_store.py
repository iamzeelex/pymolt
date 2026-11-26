"""The blob store: content-addressed texts, .pymolt/.gitignore, retention and collection."""
from __future__ import annotations

import gzip
import json

from _apply import apply_migration_plan
from pymolt.apply_ledger import EventFile, LedgerEvent, append_ledger, load_ledger
from pymolt.codemods.models import DependencyMigration
from pymolt.codemods.models import CodemodPattern, FilePreview
from pymolt.migration_plan import (
    _migration_lock,
    create_migration_plan,
    rollback_migration,
)
from pymolt.migration_state import MigrationReceipt, ReceiptRun, StepReceipt, receipt_path
from pymolt.workorder import blobs


def test_a_text_is_stored_once_by_its_hash_and_read_back_checked(tmp_path):
    digest = blobs.put_text(tmp_path, "x = 1\n")
    assert digest == blobs.sha256("x = 1\n")
    assert blobs.put_text(tmp_path, "x = 1\n") == digest
    assert blobs.stored(tmp_path) == {digest}
    assert blobs.get_text(tmp_path, digest) == "x = 1\n"
    blobs.blob_path(tmp_path, digest).write_bytes(gzip.compress(b"tampered"))
    assert blobs.get_text(tmp_path, digest) is None          # it no longer matches its hash
    assert blobs.get_text(tmp_path, "0" * 64) is None


def test_the_store_writes_its_own_gitignore_and_never_the_projects(tmp_path):
    (tmp_path / ".gitignore").write_text("build/\n")
    (tmp_path / ".pymolt").mkdir()
    (tmp_path / ".pymolt" / ".gitignore").write_text("mine/")
    blobs.put_text(tmp_path, "x = 1\n")
    lines = (tmp_path / ".pymolt" / ".gitignore").read_text().splitlines()
    assert lines == ["mine/", "blobs/", "cache/", "migrations/*/steps/", "plans/current.json"]
    assert (tmp_path / ".gitignore").read_text() == "build/\n"
    blobs.ensure_gitignore(tmp_path)                          # idempotent
    assert (tmp_path / ".pymolt" / ".gitignore").read_text().count("blobs/") == 1


def _pattern():
    return CodemodPattern(old_qualname="a.f", new_qualname="a.g", kind="rename-call")


def _apply(tmp_path, old="x = a.f()\n", new="x = a.g()\n"):
    (tmp_path / "m.py").write_text(old, encoding="utf-8")
    plan = create_migration_plan(
        tmp_path, [FilePreview(path=str(tmp_path / "m.py"), old_source=old, new_source=new,
                               sites=1, patterns=[_pattern()])],
        [DependencyMigration("a", "1.0", "2.0")], target_python="3.12",
        source_manifest="requirements.txt", target_manifest_path=None, files_scanned=1,
        predict=False)
    return plan, apply_migration_plan(tmp_path, plan, allow_no_baseline=True)


def test_apply_stores_every_steps_before_and_after_text_and_rollback_what_it_rewrote(tmp_path):
    plan, _receipt = _apply(tmp_path)
    [event] = load_ledger(tmp_path).events
    assert blobs.get_text(tmp_path, event.files[0].before) == plan.changes[0].old_source
    assert blobs.get_text(tmp_path, event.files[0].after) == plan.changes[0].new_source
    assert (tmp_path / ".pymolt" / ".gitignore").is_file()

    (tmp_path / "m.py").write_text("x = a.g()\n# a hand edit\n")
    rollback_migration(tmp_path, force=True)
    rollback = load_ledger(tmp_path).events[-1]
    assert blobs.get_text(tmp_path, rollback.files[0].before) == "x = a.g()\n# a hand edit\n"
    assert blobs.get_text(tmp_path, rollback.files[0].after) == plan.changes[0].old_source


def _run(tmp_path, run_id, text, at):
    digest = blobs.put_text(tmp_path, text)
    return LedgerEvent(event="apply", run_id=run_id, step_id="v1", at=at,
                       files=[EventFile(file="m.py", before=digest, after=digest)]), []


def test_retention_keeps_the_chain_in_progress_snapshots_and_the_two_runs_that_left_last(
        tmp_path):
    ids = {name: name * 32 for name in "abcde"}
    append_ledger(tmp_path, [_run(tmp_path, ids[n], f"{n} = 1\n", "t") for n in "abcde"])
    snap = blobs.put_text(tmp_path, "snapshot = 1\n")
    stray = blobs.put_text(tmp_path, "nobody = 1\n")
    folder = tmp_path / ".pymolt" / "migrations" / ids["e"] / "steps" / "beh-1"
    folder.mkdir(parents=True)
    (folder / "snapshot-7.json").write_text(json.dumps({"m.py": snap}))

    def run(name, closed_at=None):
        return ReceiptRun(run_id=ids[name], opened_at="t0", closed=closed_at is not None,
                          closed_at=closed_at)

    receipt = MigrationReceipt(
        run_id=ids["e"], target_python="3.12", source_manifest="requirements.txt",
        runs=[run("a", "t1"), run("b", "t2"), run("c", "t3"), run("d", "t4"), run("e")],
        steps={f"{ids['d']}:v1": StepReceipt(status="applied", epoch=4),      # d: in effect
               f"{ids['e']}:beh-1": StepReceipt(status="in_progress")})
    receipt.save(tmp_path)
    assert receipt.chain_run_ids() == [ids["d"], ids["e"]]

    removed = blobs.collect_blobs(tmp_path)
    kept = {blobs.get_text(tmp_path, h) for h in blobs.stored(tmp_path)}
    assert kept == {"b = 1\n", "c = 1\n", "d = 1\n", "e = 1\n", "snapshot = 1\n"}
    assert set(removed) == {blobs.sha256("a = 1\n"), stray}


def test_without_run_history_the_latest_other_runs_of_the_ledger_are_kept(tmp_path):
    append_ledger(tmp_path, [_run(tmp_path, n * 32, f"{n} = 1\n", "t") for n in "abcd"])
    kept = blobs.kept_runs(load_ledger(tmp_path, receipt=None), None)
    assert kept == {"c" * 32, "d" * 32}


def test_nothing_is_collected_while_the_receipt_or_ledger_cannot_be_read(tmp_path):
    stray = blobs.put_text(tmp_path, "nobody = 1\n")
    receipt_path(tmp_path).write_text("{not json")
    assert blobs.collect_blobs(tmp_path) == [] and blobs.has_blob(tmp_path, stray)
    receipt_path(tmp_path).unlink()
    assert blobs.collect_blobs(tmp_path) == [stray]


def test_plan_collects_when_the_lock_is_free_and_leaves_it_otherwise(tmp_path):
    stray = blobs.put_text(tmp_path, "nobody = 1\n")
    with _migration_lock(tmp_path, "apply"):
        assert blobs.collect_after_plan(tmp_path).blobs == []
    assert blobs.has_blob(tmp_path, stray)
    assert blobs.collect_after_plan(tmp_path).blobs == [stray]


def test_apply_collects_what_no_run_keeps(tmp_path):
    stray = blobs.put_text(tmp_path, "nobody = 1\n")
    _apply(tmp_path)
    assert not blobs.has_blob(tmp_path, stray)
    assert len(blobs.stored(tmp_path)) == 2                   # the run's before and after


def test_saving_a_new_plan_collects_the_store(tmp_path):
    plan, _receipt = _apply(tmp_path)
    rollback_migration(tmp_path)
    stray = blobs.put_text(tmp_path, "nobody = 1\n")
    plan.save_planned(tmp_path)
    assert not blobs.has_blob(tmp_path, stray)
    assert len(blobs.stored(tmp_path)) == 2           # the open run keeps its texts
