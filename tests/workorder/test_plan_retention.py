"""Plan retention (``plan_store.prune_plans``, owner decisions 2026-11-22): after a ``plan``, the
stored plans beyond the 20 newest are removed, except the current plan, its parent, every plan a
run references and every plan folder git tracks; nothing is removed when what it reads is
damaged, when git cannot say what it tracks, or while another operation holds the lock."""
from __future__ import annotations

import json
import shutil
import subprocess
import uuid

import pytest
from _stepwise import APP, RECIPES, plan_for

from pymolt import plan_store
from pymolt.migration_plan import MigrationPlan, MigrationRunState, PlanLineage, _migration_lock
from pymolt.migration_state import MigrationReceipt, ReceiptRun
from pymolt.plan_store import (
    KEEP_NEWEST,
    PruneResult,
    load_pointer,
    plan_folders,
    prune_plans,
    saved_plans,
)
from pymolt.state_io import state_lock
from pymolt.workorder.approval import APPROVAL_FILE
from pymolt.workorder.blobs import collect_after_plan

M = "app/m.py"
EMAIL = "t@example.invalid"
TOTAL = 30


@pytest.fixture(autouse=True)
def _no_repository_above(tmp_path, monkeypatch):
    """No git config of the machine's, and no repository above ``tmp_path``."""
    config = tmp_path / "gitconfig"
    config.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))


def _plans(root, n=TOTAL) -> list[MigrationPlan]:
    """``n`` stored plans, one a day from 2026-01-01 (oldest first), the last one current."""
    base = plan_for(root, {M: (APP, RECIPES)}, save=False)
    plans = []
    for day in range(n):
        plan = MigrationPlan.model_validate({
            **base.model_dump(), "plan_id": uuid.uuid4().hex, "name": None, "lineage": None,
            "created_at": f"2026-01-{day + 1:02d}T00:00:00+00:00"})
        plan.save(root)
        plans.append(plan)
    return plans


def _make_current(root, plan: MigrationPlan, *, parent: MigrationPlan | None = None):
    """Store ``plan`` again as a new current plan derived from ``parent``."""
    newer = MigrationPlan.model_validate({
        **plan.model_dump(), "plan_id": uuid.uuid4().hex, "name": None,
        "created_at": "2026-02-01T00:00:00+00:00",
        "lineage": PlanLineage(root=parent.plan_id, parent=parent.plan_id, generation=1)
        if parent else None})
    newer.save(root)
    return newer


def _run_dir(root, name: str) -> object:
    folder = root / ".pymolt" / "migrations" / name
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def test_retention_keeps_the_current_plan_its_parent_runs_plans_and_the_20_newest(tmp_path):
    plans = _plans(tmp_path)
    current = _make_current(tmp_path, plans[-1], parent=plans[0])
    # plans[1]: a run's state names it; plans[2]: a second run of it (another run id);
    # plans[3]: a folder holding only its approval; plans[4]: the receipt's run.
    (_run_dir(tmp_path, plans[1].plan_id) / "state.json").write_text(json.dumps(
        MigrationRunState(run_id=plans[1].plan_id, status="applied",
                          prepared_at="2026-01-02T00:00:00+00:00").model_dump(mode="json")))
    rerun = uuid.uuid4().hex
    (_run_dir(tmp_path, rerun) / "state.json").write_text(json.dumps(
        MigrationRunState(run_id=rerun, plan_id=plans[2].plan_id, attempt=2,
                          status="rolled_back",
                          prepared_at="2026-01-03T00:00:00+00:00").model_dump(mode="json")))
    (_run_dir(tmp_path, plans[3].plan_id) / APPROVAL_FILE).write_text("{}")
    MigrationReceipt(run_id=plans[4].plan_id, target_python="3.12",
                     source_manifest="requirements.txt",
                     runs=[ReceiptRun(run_id=plans[4].plan_id,
                                      opened_at="2026-01-05T00:00:00+00:00")]).save(tmp_path)
    # What retention never touches: anything not named like a plan.
    store = tmp_path / ".pymolt" / "plans"
    (store / "notes.txt").write_text("mine\n")
    (store / "keep-me").mkdir()
    (store / "20260101T0000Z-zzzzzzzz").mkdir()

    result = prune_plans(tmp_path)

    names = [p.name for p in plans]
    newest = sorted([*names, current.name])[-KEEP_NEWEST:]
    kept = {*newest, current.name, *names[:5]}
    assert result == PruneResult(removed=sorted(set(names) - kept))
    assert len(result.removed) == TOTAL + 1 - KEEP_NEWEST - 5
    assert set(saved_plans(tmp_path)) == kept
    assert (store / "notes.txt").read_text() == "mine\n" and (store / "keep-me").is_dir()
    assert (store / "20260101T0000Z-zzzzzzzz").is_dir()
    assert load_pointer(tmp_path).plan_id == current.plan_id
    assert prune_plans(tmp_path) == PruneResult()                   # nothing more to remove


def test_only_the_parent_is_kept_not_every_ancestor(tmp_path):
    plans = _plans(tmp_path)
    grandparent = plans[0]
    parent = MigrationPlan.model_validate({         # an old plan derived from plans[0]
        **grandparent.model_dump(), "plan_id": uuid.uuid4().hex, "name": None,
        "created_at": "2026-01-01T12:00:00+00:00",
        "lineage": PlanLineage(root=grandparent.plan_id, parent=grandparent.plan_id,
                               generation=1)})
    parent.save(tmp_path)
    _make_current(tmp_path, plans[-1], parent=parent)

    removed = prune_plans(tmp_path).removed

    assert parent.name not in removed and grandparent.name in removed


def test_an_old_current_plan_is_kept(tmp_path):
    plans = _plans(tmp_path)
    plan_store.write_pointer(tmp_path, plans[0], set_by="apply")    # an apply adopted it

    removed = prune_plans(tmp_path).removed

    assert plans[0].name not in removed and plans[1].name in removed
    assert len(saved_plans(tmp_path)) == KEEP_NEWEST + 1


def test_twenty_plans_or_fewer_are_never_pruned(tmp_path):
    plans = _plans(tmp_path, KEEP_NEWEST)
    assert prune_plans(tmp_path) == PruneResult()
    assert saved_plans(tmp_path) == [p.name for p in plans]


@pytest.mark.parametrize("damaged", ["receipt", "ledger", "pointer", "current plan",
                                     "run state"])
def test_a_damaged_state_file_means_nothing_is_pruned(tmp_path, damaged):
    plans = _plans(tmp_path)
    pymolt = tmp_path / ".pymolt"
    if damaged == "receipt":
        (pymolt / "migration_state.json").write_text("{not json")
    elif damaged == "ledger":
        (pymolt / "apply_ledger.json").write_text("[]")
    elif damaged == "pointer":
        (pymolt / "plans" / "current.json").write_text("{}")
    elif damaged == "current plan":
        (pymolt / "plans" / plans[-1].name / "plan.json").write_text("{not json")
    else:
        (_run_dir(tmp_path, uuid.uuid4().hex) / "state.json").write_text("{not json")

    result = prune_plans(tmp_path)

    assert result.removed == [] and result.skipped.startswith("no plan was removed: ")
    assert "references are unknown" in result.skipped
    assert len(plan_folders(tmp_path)) == TOTAL


def test_a_busy_migration_lock_skips_the_blobs_and_the_plans(tmp_path):
    _plans(tmp_path)
    with _migration_lock(tmp_path, "apply"):
        collected = collect_after_plan(tmp_path, plans=True)
    assert collected.plans.removed == [] and "migration lock" in collected.plans.skipped
    assert len(plan_folders(tmp_path)) == TOTAL
    assert len(collect_after_plan(tmp_path, plans=True).plans.removed) == TOTAL - KEEP_NEWEST


def test_pruning_holds_the_state_lock(tmp_path, monkeypatch):
    _plans(tmp_path)
    held = []
    real = plan_store.shutil.rmtree

    def rmtree(path, *a, **k):
        from pymolt.state_io import holds_state_lock

        held.append(holds_state_lock(tmp_path.resolve()))
        return real(path, *a, **k)

    monkeypatch.setattr(plan_store.shutil, "rmtree", rmtree)
    prune_plans(tmp_path)
    assert held and all(held)
    with state_lock(tmp_path, operation="test"):                     # re-entrant
        assert prune_plans(tmp_path) == PruneResult()


def test_a_plan_saves_then_prunes_and_says_what_it_removed(tmp_path):
    plans = _plans(tmp_path, KEEP_NEWEST)
    newer = MigrationPlan.model_validate({
        **plans[-1].model_dump(), "plan_id": uuid.uuid4().hex, "name": None, "lineage": None,
        "created_at": "2026-03-01T00:00:00+00:00",
        "steps": plans[-1].steps[:-1]})                 # another plan: it saves

    result = newer.save_planned(tmp_path)

    assert result.saved and result.pruned == PruneResult(removed=[plans[0].name])
    from pymolt.interfaces.views import plan_identity

    assert plan_identity(result.plan, result)["pruned"] == {"removed": [plans[0].name],
                                                           "skipped": None}


# ── git ──────────────────────────────────────────────────────────────────────────────────


def _git(root, *args):
    return subprocess.run(["git", "-c", f"user.email={EMAIL}", "-c", "user.name=T",
                           "-c", "commit.gpgsign=false", *args], cwd=root, check=True,
                          capture_output=True, text=True).stdout


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_a_plan_folder_git_tracks_is_never_pruned(tmp_path):
    root = tmp_path / "repo" / "svc"                 # a project in a subfolder of its repo
    root.mkdir(parents=True)
    _git(root.parent, "init", "-q")
    plans = _plans(root)
    for plan in plans[:3]:
        _git(root, "add", f".pymolt/plans/{plan.name}/plan.json")
    _git(root, "commit", "-q", "-m", "plans")

    removed = prune_plans(root).removed

    assert not {p.name for p in plans[:3]} & set(removed)
    assert set(removed) == {p.name for p in plans[3:TOTAL - KEEP_NEWEST]}


def test_when_git_cannot_say_what_it_tracks_nothing_is_pruned(tmp_path, monkeypatch):
    _plans(tmp_path)
    monkeypatch.setattr("pymolt.adapters.git.tracked_files", lambda root, spec: None)

    result = prune_plans(tmp_path)

    assert result.removed == [] and "git could not say" in result.skipped
    assert len(plan_folders(tmp_path)) == TOTAL


def test_without_git_nothing_is_tracked_and_pruning_proceeds(tmp_path, monkeypatch):
    _plans(tmp_path)
    monkeypatch.setenv("PATH", "")
    assert len(prune_plans(tmp_path).removed) == TOTAL - KEEP_NEWEST
