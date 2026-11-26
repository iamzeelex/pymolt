"""The plan store (``pymolt.plan_store``): plans stored once in named folders, the current-plan
pointer, plan references, the move of a 0.2 project's plan file, and the adoption of a plan a
mutating ``apply`` names."""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import uuid

import pytest
from _stepwise import APP, RECIPES, plan_for, plan_next, rename
from _verifying import person
from typer.testing import CliRunner

from pymolt import plan_store
from pymolt.interfaces.cli.commands import app
from pymolt.migration_plan import (
    DEFAULT_PLAN_PATH,
    MigrationPlan,
    MigrationPlanError,
    plan_path,
)
from pymolt.migration_state import MigrationReceipt
from pymolt.plan_store import (
    PlanRefError,
    load_pointer,
    migrate_legacy,
    no_current_plan,
    resolve_plan_ref,
    saved_plans,
    stored_plan_file,
)
from pymolt.status import build_status
from pymolt.workorder.apply import StepRefusal, apply_steps, skip_step
from pymolt.workorder.approval import approve, check_approval, plan_digest
from tests.fixtures.v02_loader import expected, legacy_plan_digest, materialize

runner = CliRunner()
M = "app/m.py"
LEGACY = DEFAULT_PLAN_PATH


@pytest.fixture(autouse=True)
def _no_compile(monkeypatch):
    monkeypatch.setattr("pymolt.apply_check.compile_changes", lambda root, plan: [])


def _files(root) -> dict[str, bytes]:
    """Every file under ``.pymolt/`` (the lock files aside), by its bytes."""
    base = root / ".pymolt"
    return {p.relative_to(base).as_posix(): p.read_bytes() for p in sorted(base.rglob("*"))
            if p.is_file() and not p.name.endswith(".lock")}


def _v02(tmp_path):
    """The 0.2 fixture, relocated: a legacy plan file, its PLAN.md and an open run."""
    return materialize(tmp_path / "p")


def _new_plan_from(plan: MigrationPlan, **changes) -> MigrationPlan:
    """A re-plan of ``plan``: the same content with a new id and time (changed by ``changes``)."""
    data = {**plan.model_dump(), "plan_id": uuid.uuid4().hex, "name": None, "lineage": None,
            "created_at": "2026-11-24T10:00:00+00:00", "adapted_from": None,
            "schema_version": 3, "content_digest": None, **changes}
    return MigrationPlan.model_validate(data)


# ── storing ──────────────────────────────────────────────────────────────────────────────


def test_save_stores_the_plan_once_in_its_folder_and_makes_it_current(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)}, save=False)
    path = plan.save(tmp_path)

    root = tmp_path.resolve()
    assert path == root / ".pymolt" / "plans" / plan.name / "plan.json"
    assert plan.name.split("-")[1] == plan.plan_id[:8]
    assert json.loads(path.read_text()) == plan.model_dump(mode="json")
    assert plan.lineage.root == plan.plan_id and plan.lineage.parent is None
    pointer = load_pointer(tmp_path)
    assert (pointer.name, pointer.plan_id, pointer.set_by) == (plan.name, plan.plan_id, "plan")
    assert plan_path(tmp_path) == path and MigrationPlan.load(tmp_path) == plan
    assert not (root / LEGACY).exists()
    # The same plan saved again is the same file.
    assert plan.save(tmp_path) == path and saved_plans(tmp_path) == [plan.name]


def test_a_stored_plan_is_never_rewritten_a_changed_copy_is_a_new_plan(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    first = stored_plan_file(tmp_path, plan.name).read_bytes()
    changed = plan.model_copy(update={"steps": plan.steps[:-1]})

    path = changed.save(tmp_path)

    assert changed.plan_id != plan.plan_id and changed.lineage.root == changed.plan_id
    assert path == stored_plan_file(tmp_path, changed.name)
    assert stored_plan_file(tmp_path, plan.name).read_bytes() == first
    assert load_pointer(tmp_path).plan_id == changed.plan_id


def test_a_folder_another_plan_took_draws_a_new_id_three_times_then_refuses(tmp_path,
                                                                           monkeypatch):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)}, save=False)
    taken = [f"{n:x}" * 32 for n in range(1, 4)]      # the ids the 3 redraws draw
    for plan_id in [plan.plan_id, *taken]:
        other = plan.model_copy(update={"plan_id": plan_id, "name": None,
                                        "target_python": "3.13"})
        folder = plan_store.plan_folder(tmp_path, plan_store._stamp_name(other))
        folder.mkdir(parents=True)
        (folder / "plan.json").write_text(json.dumps(other.model_dump(mode="json")))
    ids = iter(taken)
    monkeypatch.setattr(plan_store.uuid, "uuid4",
                        lambda: type("U", (), {"hex": next(ids)})())

    with pytest.raises(MigrationPlanError, match="was taken 4 times"):
        plan.save(tmp_path)
    assert load_pointer(tmp_path) is None


def test_an_export_is_written_there_only(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    other = plan_next(tmp_path, {M: [rename("a.f", "a.g")]}, save=False)

    out = other.save(tmp_path, "exports/p.json")

    assert out == tmp_path.resolve() / "exports" / "p.json"
    assert load_pointer(tmp_path).plan_id == plan.plan_id
    assert saved_plans(tmp_path) == [plan.name]
    with pytest.raises(MigrationPlanError, match="plan store"):
        other.save(tmp_path, ".pymolt/plans/x/plan.json")


def test_the_pointer_is_gitignored_and_the_plans_are_not(tmp_path):
    plan = plan_for(tmp_path, {M: (APP, RECIPES)})
    assert "plans/current.json" in (tmp_path / ".pymolt" / ".gitignore").read_text().split()
    if shutil.which("git") is None:
        pytest.skip("no git")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)

    def ignored(rel: str) -> bool:
        return subprocess.run(["git", "-C", str(tmp_path), "check-ignore", "-q", rel],
                              check=False).returncode == 0

    assert ignored(".pymolt/plans/current.json")
    assert not ignored(f".pymolt/plans/{plan.name}/plan.json")
    assert not ignored(f".pymolt/plans/{plan.name}/PLAN.md")


# ── references ───────────────────────────────────────────────────────────────────────────


def _two_plans(tmp_path):
    one = plan_for(tmp_path, {M: (APP, RECIPES)})
    two = plan_next(tmp_path, {M: [rename("a.f", "a.g")]})
    return one, two


def test_every_form_of_a_plan_reference(tmp_path):
    one, two = _two_plans(tmp_path)
    root = tmp_path.resolve()
    first = stored_plan_file(root, one.name)
    assert resolve_plan_ref(tmp_path, "current") == stored_plan_file(root, two.name)
    assert resolve_plan_ref(tmp_path, f".pymolt/plans/{one.name}/plan.json") == first
    assert resolve_plan_ref(tmp_path, str(first)) == first
    assert resolve_plan_ref(tmp_path, one.name) == first
    assert resolve_plan_ref(tmp_path, one.plan_id[:4]) == first
    assert resolve_plan_ref(tmp_path, one.plan_id[:8]) == first
    assert resolve_plan_ref(tmp_path, one.plan_id) == first
    assert resolve_plan_ref(tmp_path, one.plan_id[:12]) == first


def test_an_ambiguous_unknown_or_malformed_reference_is_refused(tmp_path, monkeypatch):
    one, _two = _two_plans(tmp_path)
    tail = "1" if one.plan_id[8] == "0" else "0"
    clash = one.model_copy(update={"plan_id": one.plan_id[:8] + tail * 24, "name": None,
                                   "created_at": "2026-01-01T00:00:00+00:00"})
    clash.save(tmp_path)                    # another plan whose id begins with the same 8

    with pytest.raises(PlanRefError, match=f"{one.plan_id[:8]} matches 2 plans: "):
        resolve_plan_ref(tmp_path, one.plan_id[:8])
    assert resolve_plan_ref(tmp_path, one.plan_id[:9]) == stored_plan_file(tmp_path, one.name)
    assert resolve_plan_ref(tmp_path, clash.plan_id[:9]) == \
        stored_plan_file(tmp_path, clash.name)
    with pytest.raises(PlanRefError, match="no saved plan with id 0000"):
        resolve_plan_ref(tmp_path, "0000")
    with pytest.raises(PlanRefError, match="no saved plan named 20200101T0000Z-00000000"):
        resolve_plan_ref(tmp_path, "20200101T0000Z-00000000")
    with pytest.raises(PlanRefError, match="no plan file at nowhere/p.json"):
        resolve_plan_ref(tmp_path, "nowhere/p.json")
    for bad in ("abc", "NotAPlan", "f74941c5-x", "../x"):
        with pytest.raises(PlanRefError) as caught:
            resolve_plan_ref(tmp_path, bad)
        if bad != "../x":
            assert "not a plan reference" in str(caught.value)
            assert "a plan id prefix" in caught.value.hint


def test_a_prefix_also_matches_the_02_file_once(tmp_path):
    root = _v02(tmp_path)
    plan_id = expected()["plan"]["plan_id"]
    assert resolve_plan_ref(root, plan_id[:6]) == root / LEGACY
    migrate_legacy(root)
    (root / LEGACY).write_bytes(stored_plan_file(root, saved_plans(root)[0]).read_bytes())
    # The same plan in the store and in the 0.2 file is one match: the stored one.
    assert resolve_plan_ref(root, plan_id[:6]) == stored_plan_file(root, saved_plans(root)[0])


def test_apply_takes_a_reference_and_refuses_a_bad_one_with_exit_2(tmp_path):
    one, two = _two_plans(tmp_path)
    for ref in (one.name, one.plan_id[:6], f".pymolt/plans/{one.name}/plan.json"):
        result = runner.invoke(app, ["apply", str(tmp_path), "--plan", ref, "--dry-run",
                                     "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["plan_id"] == one.plan_id
    current = runner.invoke(app, ["apply", str(tmp_path), "--plan", "current", "--dry-run",
                                  "--json"])
    assert json.loads(current.stdout)["plan_id"] == two.plan_id
    for ref, message in (("0000", "no saved plan with id 0000"),
                         ("bogus", "not a plan reference"),
                         ("missing.json", "no plan file at missing.json")):
        result = runner.invoke(app, ["apply", str(tmp_path), "--plan", ref, "--json"])
        assert result.exit_code == 2, result.output
        assert message in json.loads(result.stdout)["error"]
    assert load_pointer(tmp_path).plan_id == two.plan_id      # a dry run never moves it


# ── a 0.2 project ────────────────────────────────────────────────────────────────────────


def test_the_first_plan_moves_the_02_file_into_the_store_by_its_stored_form(tmp_path):
    exp = expected()
    root = _v02(tmp_path)
    digest = legacy_plan_digest(root / LEGACY)
    legacy = MigrationPlan.load(root)

    name = migrate_legacy(root)

    assert name == legacy.name and saved_plans(root) == [name]
    stored = stored_plan_file(root, name)
    assert legacy_plan_digest(stored) == digest
    assert json.loads(stored.read_text())["schema_version"] == 2
    pointer = load_pointer(root)
    assert (pointer.name, pointer.plan_id, pointer.set_by) == (name, exp["plan"]["plan_id"],
                                                               "plan")
    assert not (root / LEGACY).exists() and not (root / ".pymolt" / "PLAN.md").exists()
    assert (stored.parent / "PLAN.md").read_text().startswith("# Migration plan")
    # Its run, frozen copy and approval digest are untouched: the plan is the same file.
    moved = MigrationPlan.load(root)
    assert moved.plan_id == legacy.plan_id and moved.model_dump() == legacy.model_dump()
    assert plan_digest(moved) == digest
    assert migrate_legacy(root) is None                       # nothing left to move


@pytest.mark.parametrize("crash", ["store", "pointer", "plan_md", "unlink"])
def test_a_move_a_crash_interrupted_is_finished_by_the_next(tmp_path, monkeypatch, crash):
    root = _v02(tmp_path)
    clean = materialize(tmp_path / "clean")
    migrate_legacy(clean)                        # the move, uninterrupted
    legacy = root / LEGACY
    digest = legacy_plan_digest(legacy)

    class Crash(Exception):
        pass

    def boom(*_a, **_k):
        raise Crash

    with monkeypatch.context() as patch:
        if crash == "store":
            patch.setattr(plan_store, "_put_stored_form", boom)
        elif crash == "pointer":
            patch.setattr(plan_store, "write_pointer", boom)
        elif crash == "plan_md":
            patch.setattr(plan_store, "_move_plan_md", boom)
        else:
            real = pathlib.Path.unlink

            def unlink(self, *a, **k):
                if self == legacy:
                    raise Crash
                return real(self, *a, **k)

            patch.setattr(pathlib.Path, "unlink", unlink)
        with pytest.raises(Crash):
            migrate_legacy(root)
    assert legacy.exists()                       # what the next plan looks for

    name = migrate_legacy(root)

    assert name == saved_plans(clean)[0] and saved_plans(root) == [name]
    assert _files(root).keys() == _files(clean).keys()        # the same files, wherever it is
    assert legacy_plan_digest(stored_plan_file(root, name)) == digest
    assert not legacy.exists() and not (root / ".pymolt" / "PLAN.md").exists()
    assert load_pointer(root).plan_id == load_pointer(clean).plan_id


def test_a_different_plan_under_the_02_plans_name_is_refused_and_both_are_kept(tmp_path):
    root = _v02(tmp_path)
    name = MigrationPlan.load(root).name
    stored = stored_plan_file(root, name)
    stored.parent.mkdir(parents=True)
    other = {**json.loads((root / LEGACY).read_text()), "target_python": "3.13"}
    stored.write_text(json.dumps(other))
    before = _files(root)

    with pytest.raises(MigrationPlanError, match="different plans under one name"):
        migrate_legacy(root)
    assert _files(root) == before


def test_plan_md_moves_only_when_it_is_the_02_plans(tmp_path):
    root = _v02(tmp_path)
    old = root / ".pymolt" / "PLAN.md"
    old.write_text("# Migration plan 00000000\nsomeone else's\n")

    name = migrate_legacy(root)

    assert old.read_text() == "# Migration plan 00000000\nsomeone else's\n"
    assert not (plan_store.plan_folder(root, name) / "PLAN.md").exists()


def test_with_a_pointer_already_the_02_file_is_moved_and_the_pointer_kept(tmp_path):
    root = _v02(tmp_path)
    legacy = MigrationPlan.load(root)
    newer = _new_plan_from(legacy)
    shutil.copy(root / LEGACY, tmp_path / "keep.json")
    (root / LEGACY).unlink()
    newer.save(root)                                     # a 0.3 plan, then 0.2 planned again
    shutil.copy(tmp_path / "keep.json", root / LEGACY)

    assert migrate_legacy(root) == legacy.name
    assert load_pointer(root).plan_id == newer.plan_id
    assert saved_plans(root) == sorted([legacy.name, newer.name])


# ── the 0.2 fixture, end to end ──────────────────────────────────────────────────────────


def test_status_on_a_02_project_writes_nothing(tmp_path):
    root = _v02(tmp_path)
    before = _files(root)

    status = build_status(root)
    result = runner.invoke(app, ["status", str(root), "--json"])

    assert result.exit_code == 0, result.output
    after = _files(root)
    # No state file and nothing under plans/: only the lenient caches status keeps (anchors,
    # capture ids), and the store's .gitignore that lists them, which gains the pointer's line.
    assert {k: v for k, v in after.items() if not k.startswith(("cache/", ".gitignore"))} == \
        {k: v for k, v in before.items() if not k.startswith(("cache/", ".gitignore"))}
    assert after[".gitignore"].splitlines() == [*before[".gitignore"].splitlines(),
                                                b"plans/current.json"]
    assert not (root / ".pymolt" / "plans").exists()
    plan = next(p for p in status.phases if p.phase == "plan")
    assert status.problems == [] and plan.problem is None


def test_apply_continues_the_02_run_and_its_approval_holds(tmp_path):
    root = _v02(tmp_path)
    plan = MigrationPlan.load(root)
    approval = approve(root, plan, attester=person(root))
    assert approval["plan_sha256"] == legacy_plan_digest(root / LEGACY)

    result = apply_steps(root, plan, agent=True, step="mech-1")

    assert result.applied == ["mech-1"] and result.run_id == plan.plan_id
    assert check_approval(root, plan, plan.plan_id, "pymolt-steps") is None
    receipt = MigrationReceipt.load(root)
    assert [(r.run_id, r.closed) for r in receipt.runs] == [(plan.plan_id, False)]
    assert not (root / ".pymolt" / "plans").exists() and (root / LEGACY).is_file()


def test_a_changed_plan_moves_the_02_file_and_its_first_apply_closes_the_02_run(tmp_path):
    root = _v02(tmp_path)
    legacy = MigrationPlan.load(root)
    approve(root, legacy, attester=person(root))
    frozen = root / ".pymolt" / "migrations" / legacy.plan_id / "plan.json"
    frozen_bytes = frozen.read_bytes()
    steps = [s.model_copy(update={"title": s.title + " (re-planned)"}) if s.id == "beh-3"
             else s for s in legacy.steps]
    new = _new_plan_from(legacy, steps=steps)

    result = new.save_planned(root)

    assert result.saved and result.moved_legacy == legacy.name
    assert result.path == stored_plan_file(root, new.name)
    assert saved_plans(root) == sorted([legacy.name, new.name])
    assert load_pointer(root).plan_id == new.plan_id
    assert new.pymolt_version and new.source is not None and new.content_digest
    # The 0.2 run, its frozen copy and its approval are untouched.
    assert frozen.read_bytes() == frozen_bytes
    moved = MigrationPlan.load(root, stored_plan_file(root, legacy.name))
    assert check_approval(root, moved, legacy.plan_id, "pymolt-steps") is None

    applied = apply_steps(root, new, step="mech-1")

    assert applied.run_id == new.plan_id
    runs = {r.run_id: r.closed for r in MigrationReceipt.load(root).runs}
    assert runs == {legacy.plan_id: True, new.plan_id: False}


# ── adoption ─────────────────────────────────────────────────────────────────────────────


def test_a_mutating_apply_of_an_exported_plan_imports_it_and_makes_it_current(tmp_path):
    first = plan_for(tmp_path, {M: (APP, RECIPES)})
    other = plan_next(tmp_path, {M: [rename("a.f", "a.g")]}, save=False)
    result = other.save_planned(tmp_path, output=tmp_path / "out" / "plan.json")
    assert not result.saved and load_pointer(tmp_path).plan_id == first.plan_id
    exported = MigrationPlan.load(tmp_path, result.path)

    dry = apply_steps(tmp_path, exported, dry_run=True)
    assert dry.outcome == "dry_run" and load_pointer(tmp_path).plan_id == first.plan_id
    assert saved_plans(tmp_path) == [first.name]

    apply_steps(tmp_path, exported)

    pointer = load_pointer(tmp_path)
    assert (pointer.plan_id, pointer.set_by, pointer.name) == (other.plan_id, "apply",
                                                               other.name)
    stored = stored_plan_file(tmp_path, other.name)
    assert json.loads(stored.read_text()) == json.loads(result.path.read_text())
    assert plan_digest(MigrationPlan.load(tmp_path)) == plan_digest(exported)


def test_skip_adopts_and_approving_never_does(tmp_path):
    first = plan_for(tmp_path, {M: (APP, RECIPES)})
    second = plan_next(tmp_path, {M: [rename("a.f", "a.g")]})
    assert load_pointer(tmp_path).plan_id == second.plan_id

    approve(tmp_path, first, attester=person(tmp_path))
    assert load_pointer(tmp_path).plan_id == second.plan_id

    skip_step(tmp_path, first, "mech-1", "not now")
    assert (load_pointer(tmp_path).plan_id, load_pointer(tmp_path).set_by) == (
        first.plan_id, "apply")


def test_another_plan_under_the_name_refuses_the_adoption(tmp_path):
    first = plan_for(tmp_path, {M: (APP, RECIPES)})
    other = plan_next(tmp_path, {M: [rename("a.f", "a.g")]}, save=False)
    export = other.save_planned(tmp_path, output=tmp_path / "p.json").path
    folder = plan_store.plan_folder(tmp_path, other.name)
    folder.mkdir(parents=True)
    (folder / "plan.json").write_text(json.dumps(
        {**other.model_dump(mode="json"), "target_python": "3.13"}))
    before = _files(tmp_path)

    with pytest.raises(StepRefusal, match=f"another plan is saved as {other.name}"):
        apply_steps(tmp_path, MigrationPlan.load(tmp_path, export))
    assert _files(tmp_path) == before and load_pointer(tmp_path).plan_id == first.plan_id


def test_plan_o_keeps_02s_meaning_and_apply_plan_path_imports_it(tmp_path, monkeypatch):
    from tests.interfaces.test_cli_plan import _configured_project, _mock_plan_services

    _configured_project(tmp_path)
    _mock_plan_services(tmp_path, monkeypatch)
    out = runner.invoke(app, ["plan", str(tmp_path), "-o", "exported.json", "--json"])
    assert out.exit_code == 0, out.output
    payload = json.loads(out.stdout)
    assert payload["plan_path"] == str(tmp_path.resolve() / "exported.json")
    assert payload["plan_md"] == str(tmp_path.resolve() / ".pymolt" / "PLAN.md")
    assert not (tmp_path / ".pymolt" / "plans").exists()
    assert MigrationPlan.load(tmp_path) is None

    applied = runner.invoke(app, ["apply", str(tmp_path), "--plan", "exported.json",
                                  "--no-smoke", "--json"])

    assert applied.exit_code == 0, applied.output
    pointer = load_pointer(tmp_path)
    assert pointer.plan_id == payload["plan_id"] and pointer.set_by == "apply"
    assert MigrationPlan.load(tmp_path).plan_id == payload["plan_id"]


# ── no current plan ──────────────────────────────────────────────────────────────────────


def test_saved_plans_without_a_pointer_make_no_plan_current(tmp_path):
    one, two = _two_plans(tmp_path)
    (tmp_path / ".pymolt" / "plans" / "current.json").unlink()   # a fresh clone: no pointer
    newest = max(one.name, two.name)        # by name: in one minute, by id

    message = no_current_plan(tmp_path)
    assert message == (f"no current plan; 2 saved plan(s), newest {newest}: choose one with "
                       f"`pymolt apply . --plan {newest}`, or make a new one with "
                       "`pymolt plan .`")
    assert MigrationPlan.load(tmp_path) is None
    plan = next(p for p in build_status(tmp_path).phases if p.phase == "plan")
    assert plan.detail == message
    result = runner.invoke(app, ["apply", str(tmp_path), "--json"])
    assert result.exit_code == 2 and json.loads(result.stdout)["error"] == message
    assert not (tmp_path / ".pymolt" / "plans" / "current.json").exists()


def test_a_02_plan_without_a_readable_date_moves_under_an_unknown_time(tmp_path):
    root = _v02(tmp_path)
    legacy = root / LEGACY
    data = {**json.loads(legacy.read_text()), "created_at": "sometime"}
    legacy.write_text(json.dumps(data))
    frozen = root / ".pymolt" / "migrations" / data["plan_id"] / "plan.json"
    frozen.write_text(json.dumps(data))
    plan = MigrationPlan.load(root)

    name = migrate_legacy(root)

    assert name == f"00000000T0000Z-{data['plan_id'][:8]}" == plan.name
    assert MigrationPlan.load(root).name == MigrationPlan.load(root, frozen).name == name
    (root / LEGACY).write_text(json.dumps(data))      # the same plan in both places
    assert resolve_plan_ref(root, data["plan_id"][:6]) == stored_plan_file(root, name)
