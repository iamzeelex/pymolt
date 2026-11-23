"""Re-planning (``plan_store.save_planned``, design plan-lineage-and-report §1-2): a plan the same
as the current one saves nothing and keeps its approvals; a changed one is stored, derived from
the current plan (its lineage); ``--fresh`` and ``--label``; ``-o``; ``plan --json``/``--agent``."""
from __future__ import annotations

import json
import shutil
import subprocess
import uuid

import pytest
from _stepwise import APP, RECIPES, plan_for, plan_next, rename
from _verifying import person
from typer.testing import CliRunner

import pymolt
from pymolt.interfaces.cli.commands import app
from pymolt.migration_plan import DEFAULT_PLAN_PATH, LABEL_RULE, MigrationPlan
from pymolt.plan_store import check_label, load_pointer, saved_plans, stored_plan_file
from pymolt.workorder.apply import apply_steps
from pymolt.workorder.approval import approve, check_approval, plan_digest
from tests.fixtures.v02_loader import legacy_plan_digest, materialize

runner = CliRunner()
M = "app/m.py"
EMAIL = "t@example.invalid"


@pytest.fixture(autouse=True)
def _no_compile(monkeypatch):
    monkeypatch.setattr("pymolt.apply_check.compile_changes", lambda root, plan: [])


def _store(root) -> dict[str, bytes]:
    """Every file under ``.pymolt/plans/`` but the pointer, by its bytes."""
    base = root / ".pymolt" / "plans"
    return {p.relative_to(base).as_posix(): p.read_bytes() for p in sorted(base.rglob("*"))
            if p.is_file() and p.name != "current.json"}


def _again(root, recipes=RECIPES) -> MigrationPlan:
    """``plan`` again over the files as they are (not saved yet)."""
    return plan_next(root, {M: recipes}, save=False)


def _new_plan_from(plan: MigrationPlan, **changes) -> MigrationPlan:
    """A re-plan of ``plan`` made by the engine: its content, a new id and time."""
    data = {**plan.model_dump(), "plan_id": uuid.uuid4().hex, "name": None, "lineage": None,
            "created_at": "2026-11-24T10:00:00+00:00", "adapted_from": None,
            "schema_version": 3, "content_digest": None, "source": None,
            "pymolt_version": None, **changes}
    return MigrationPlan.model_validate(data)


# ── unchanged ────────────────────────────────────────────────────────────────────────────


def test_an_unchanged_replan_saves_nothing_and_its_approval_holds(tmp_path):
    first = plan_for(tmp_path, {M: (APP, RECIPES)})
    approve(tmp_path, MigrationPlan.load(tmp_path), attester=person(tmp_path))
    store, pointer = _store(tmp_path), load_pointer(tmp_path)

    result = _again(tmp_path).save_planned(tmp_path)

    assert (result.saved, result.unchanged_from) == (False, first.name)
    assert result.plan.plan_id == first.plan_id and result.plan.name == first.name
    assert result.path == stored_plan_file(tmp_path, first.name)
    assert _store(tmp_path) == store and saved_plans(tmp_path) == [first.name]
    # Outside a git repository there is no commit to confirm: the pointer is as it was.
    assert load_pointer(tmp_path) == pointer
    current = MigrationPlan.load(tmp_path)
    assert check_approval(tmp_path, current, first.plan_id, "pymolt-steps") is None


@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    """``tmp_path/p`` in a git repository of its own (no global or system git config)."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    config = tmp_path / "gitconfig"
    config.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    root = tmp_path / "p"
    root.mkdir()

    def git(*args):
        return subprocess.run(["git", "-c", f"user.email={EMAIL}", "-c", "user.name=T",
                               "-c", "commit.gpgsign=false", *args], cwd=root, check=True,
                              capture_output=True, text=True).stdout.strip()

    git("init", "-q")
    return root, git


def test_an_unchanged_replan_records_the_commit_it_confirmed_the_plan_for(git_repo):
    root, git = git_repo
    first = plan_for(root, {M: (APP, RECIPES)})
    git("add", "-A")
    git("commit", "-q", "-m", "one")
    (root / "README").write_text("two\n")
    git("add", "-A")
    git("commit", "-q", "-m", "two")
    store = _store(root)

    result = _again(root).save_planned(root)

    assert result.unchanged_from == first.name
    assert load_pointer(root).checked_commit == git("rev-parse", "HEAD")
    assert (load_pointer(root).name, load_pointer(root).set_by) == (first.name, "plan")
    assert _store(root) == store


@pytest.mark.parametrize("change", ["steps", "capture", "trace_path", "setup", "target",
                                    "label", "fresh"])
def test_each_input_that_makes_a_replan_a_new_plan(tmp_path, change):
    first = plan_for(tmp_path, {M: (APP, RECIPES)})
    again = _again(tmp_path, [rename("a.f", "a.g")] if change == "steps" else RECIPES)
    if change == "capture":
        again.baseline = again.baseline.model_copy(
            update={"capture_id": "20260101T000000Z-00000000"})
    elif change == "trace_path":
        again.baseline_trace_path = ".pymolt/elsewhere.jsonl"
    elif change == "setup":
        again.environment_setup_fingerprint = "0" * 64
    elif change == "target":
        again.target_manifest_sha256 = "1" * 64

    result = again.save_planned(tmp_path, label="numpy2" if change == "label" else None,
                                fresh=change == "fresh")

    assert result.saved and result.unchanged_from is None
    assert result.plan.plan_id != first.plan_id
    assert saved_plans(tmp_path) == sorted([first.name, again.name])
    assert load_pointer(tmp_path).plan_id == again.plan_id


def test_the_baseline_recording_is_bound_by_its_path_in_the_project(tmp_path):
    first = plan_for(tmp_path, {M: (APP, RECIPES)})
    assert first.baseline_trace_path.startswith(".pymolt/")      # never the checkout's path
    again = _again(tmp_path)
    # A 0.2 plan stored the same recording's absolute path: the same binding.
    again.baseline_trace_path = str(tmp_path.resolve() / first.baseline_trace_path)

    assert again.save_planned(tmp_path).unchanged_from == first.name


# ── labels ───────────────────────────────────────────────────────────────────────────────


def test_a_label_names_the_plan_and_only_a_new_one_saves_again(tmp_path):
    plan_for(tmp_path, {M: (APP, RECIPES)})
    labelled = _again(tmp_path)
    labelled.save_planned(tmp_path, label="numpy2", fresh=True)
    assert labelled.label == "numpy2" and labelled.name.endswith(
        f"-{labelled.plan_id[:8]}-numpy2")
    assert stored_plan_file(tmp_path, labelled.name).is_file()

    same = _again(tmp_path).save_planned(tmp_path, label="numpy2")
    unlabelled = _again(tmp_path).save_planned(tmp_path)        # keeps the current label
    assert same.unchanged_from == unlabelled.unchanged_from == labelled.name

    other = _again(tmp_path)
    result = other.save_planned(tmp_path, label="numpy3")
    assert result.saved and other.name.endswith("-numpy3")
    assert other.lineage.parent == labelled.plan_id


@pytest.mark.parametrize("label", ["Numpy2", "-x", "x-", "a b", "x" * 33, "a/b", "", "é"])
def test_an_invalid_label_is_refused_with_the_rule(tmp_path, label):
    with pytest.raises(ValueError, match="invalid label"):
        check_label(label)
    plan_for(tmp_path, {M: (APP, RECIPES)})
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="1 to 32 lowercase"):
        _again(tmp_path).save_planned(tmp_path, label=label)
    assert _store(tmp_path) == store


def test_valid_labels():
    for label in ("numpy2", "a", "py3.12", "x_y-z", "a" * 32):
        assert check_label(label) == label


# ── lineage ──────────────────────────────────────────────────────────────────────────────


def test_three_generations_then_a_fresh_plan(tmp_path):
    first = plan_for(tmp_path, {M: (APP, RECIPES)})
    assert (first.lineage.root, first.lineage.parent, first.lineage.generation) == (
        first.plan_id, None, 0)
    second = _again(tmp_path, [rename("a.f", "a.g")])
    second.save_planned(tmp_path)
    third = _again(tmp_path)                    # the first plan's content: not the current's
    third.save_planned(tmp_path)

    assert (second.lineage.root, second.lineage.parent, second.lineage.generation) == (
        first.plan_id, first.plan_id, 1)
    assert (third.lineage.root, third.lineage.parent, third.lineage.generation) == (
        first.plan_id, second.plan_id, 2)
    stored = json.loads(stored_plan_file(tmp_path, third.name).read_text())
    assert stored["lineage"] == {"root": first.plan_id, "parent": second.plan_id,
                                 "generation": 2}

    fresh = _again(tmp_path)                    # unchanged, but --fresh saves (owner's Q2)
    result = fresh.save_planned(tmp_path, fresh=True)
    assert result.saved and (fresh.lineage.root, fresh.lineage.parent,
                             fresh.lineage.generation) == (fresh.plan_id, None, 0)
    assert len(saved_plans(tmp_path)) == 4


def test_with_saved_plans_but_no_pointer_plan_starts_a_fresh_lineage(tmp_path):
    first = plan_for(tmp_path, {M: (APP, RECIPES)})
    (tmp_path / ".pymolt" / "plans" / "current.json").unlink()   # a fresh clone

    again = _again(tmp_path)
    result = again.save_planned(tmp_path)

    assert result.saved and again.plan_id != first.plan_id
    assert again.lineage.parent is None and again.lineage.root == again.plan_id


# ── a 0.2 project ────────────────────────────────────────────────────────────────────────


def test_an_unchanged_plan_on_the_02_fixture_keeps_its_run_and_approval(tmp_path):
    root = materialize(tmp_path / "p")
    legacy = MigrationPlan.load(root)
    digest = legacy_plan_digest(root / DEFAULT_PLAN_PATH)
    approve(root, legacy, attester=person(root))
    frozen = root / ".pymolt" / "migrations" / legacy.plan_id / "plan.json"
    frozen_bytes = frozen.read_bytes()

    result = _new_plan_from(legacy).save_planned(root)

    assert (result.saved, result.unchanged_from, result.moved_legacy) == (
        False, legacy.name, legacy.name)
    assert result.plan.plan_id == legacy.plan_id and saved_plans(root) == [legacy.name]
    assert not (root / DEFAULT_PLAN_PATH).exists()
    assert plan_digest(MigrationPlan.load(root)) == digest and frozen.read_bytes() == \
        frozen_bytes
    current = MigrationPlan.load(root)
    assert check_approval(root, current, legacy.plan_id, "pymolt-steps") is None
    # Its open run goes on.
    applied = apply_steps(root, current, agent=True, step="mech-1")
    assert applied.applied == ["mech-1"] and applied.run_id == legacy.plan_id


def test_a_changed_plan_on_the_02_fixture_is_derived_from_the_moved_plan(tmp_path):
    root = materialize(tmp_path / "p")
    legacy = MigrationPlan.load(root)
    steps = [s.model_copy(update={"title": s.title + " (re-planned)"}) if s.id == "beh-3"
             else s for s in legacy.steps]
    new = _new_plan_from(legacy, steps=steps)

    result = new.save_planned(root)

    assert result.saved and result.moved_legacy == legacy.name
    assert (new.lineage.root, new.lineage.parent, new.lineage.generation) == (
        legacy.plan_id, legacy.plan_id, 1)


# ── -o ───────────────────────────────────────────────────────────────────────────────────


def test_o_writes_the_current_plan_when_unchanged_and_a_derived_plan_otherwise(tmp_path):
    first = plan_for(tmp_path, {M: (APP, RECIPES)})
    pointer, store = load_pointer(tmp_path), _store(tmp_path)
    out = tmp_path / "out" / "same.json"

    same = _again(tmp_path).save_planned(tmp_path, output=out)

    assert (same.saved, same.unchanged_from, same.exported) == (False, first.name, True)
    assert json.loads(out.read_text()) == json.loads(
        stored_plan_file(tmp_path, first.name).read_text())
    assert load_pointer(tmp_path) == pointer and _store(tmp_path) == store

    other = _again(tmp_path, [rename("a.f", "a.g")])
    changed = other.save_planned(tmp_path, output=tmp_path / "out" / "other.json")

    assert (changed.saved, changed.unchanged_from) == (False, None)
    assert other.lineage.parent == first.plan_id and other.lineage.generation == 1
    assert load_pointer(tmp_path) == pointer and _store(tmp_path) == store


# ── the CLI and its machine output ───────────────────────────────────────────────────────


IDENTITY_KEYS = {"plan_name", "label", "created_at", "pymolt_version", "plan_source",
                 "baseline_capture_id", "baseline_warning", "lineage", "content_digest",
                 "saved", "unchanged_from", "moved_legacy", "pruned"}


@pytest.fixture
def cli_project(tmp_path, monkeypatch):
    from tests.interfaces.test_cli_plan import _configured_project, _mock_plan_services

    _configured_project(tmp_path)
    _mock_plan_services(tmp_path, monkeypatch)
    return tmp_path


def _plan_json(root, *args):
    result = runner.invoke(app, ["plan", str(root), *args, "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_plan_json_says_what_was_saved_and_what_was_kept(cli_project):
    from pymolt.verify.service import load_contract_state

    root = cli_project
    first = _plan_json(root)
    assert IDENTITY_KEYS <= first.keys()
    capture = load_contract_state(root).baseline
    assert first["saved"] is True and first["unchanged_from"] is None
    assert first["plan_name"].split("-")[1] == first["plan_id"][:8]
    assert first["plan_path"] == str(stored_plan_file(root, first["plan_name"]))
    assert first["lineage"] == {"root": first["plan_id"], "parent": None, "generation": 0}
    assert first["pymolt_version"] == pymolt.__version__
    assert first["plan_source"]["quality"] in ("not-a-repo", "git", "no-git", "error")
    assert first["baseline_capture_id"] == capture.capture_id
    assert len(first["content_digest"]) == 64 and first["label"] is None
    assert first["pruned"] == {"removed": [], "skipped": None}
    assert first["moved_legacy"] is None
    assert first["baseline_warning"] is None

    again = _plan_json(root)
    assert (again["saved"], again["unchanged_from"]) == (False, first["plan_name"])
    assert (again["plan_id"], again["plan_name"], again["plan_path"]) == (
        first["plan_id"], first["plan_name"], first["plan_path"])

    labelled = _plan_json(root, "--label", "numpy2")
    assert labelled["saved"] is True and labelled["label"] == "numpy2"
    assert labelled["plan_name"].endswith("-numpy2")
    assert labelled["lineage"] == {"root": first["plan_id"], "parent": first["plan_id"],
                                   "generation": 1}
    fresh = _plan_json(root, "--fresh")
    assert fresh["saved"] is True and fresh["lineage"]["parent"] is None


def test_plan_agent_carries_the_same_identity(cli_project):
    root = cli_project
    first = json.loads(runner.invoke(app, ["plan", str(root), "--agent"]).stdout)
    again = json.loads(runner.invoke(app, ["plan", str(root), "--agent"]).stdout)

    assert first["ok"] and again["ok"]
    assert IDENTITY_KEYS <= first["data"].keys()
    assert first["data"]["saved"] is True and again["data"]["saved"] is False
    assert again["data"]["unchanged_from"] == first["data"]["plan_name"]
    assert again["data"]["plan_id"] == first["data"]["plan_id"]
    assert "Unchanged: same steps, baseline, setup and target manifest" in again["summary"]
    assert again["summary"].splitlines()[1].startswith(f"plan {first['data']['plan_id'][:8]} · ")


def test_plan_says_unchanged_and_names_the_next_command(cli_project):
    root = cli_project
    first = runner.invoke(app, ["plan", str(root)])
    assert first.exit_code == 0, first.output
    assert "Next: pymolt apply ." in " ".join(first.output.split())
    plan_md = stored_plan_file(root, load_pointer(root).name).parent / "PLAN.md"
    plan_md.write_text("kept\n")

    again = runner.invoke(app, ["plan", str(root)])

    text = " ".join(again.output.replace("│", " ").split())
    assert again.exit_code == 0, again.output
    assert "Unchanged: same steps, baseline, setup and target manifest as the current plan. " \
           "Nothing saved; its approvals still hold." in text
    plan = MigrationPlan.load(root)
    assert f"plan {plan.plan_id[:8]} · " in text and "· fresh" in text
    assert plan_md.read_text() == "kept\n"                 # PLAN.md is there: not rewritten
    plan_md.unlink()
    runner.invoke(app, ["plan", str(root)])
    assert plan_md.read_text().startswith(f"# Migration plan {plan.name}")


def test_plan_refuses_an_invalid_label_before_planning(cli_project):
    root = cli_project
    result = runner.invoke(app, ["plan", str(root), "--label", "NumPy2", "--json"])

    assert result.exit_code == 2, result.output
    payload = json.loads(result.stdout)
    assert "invalid label" in payload["error"] and LABEL_RULE in payload["hint"]
    assert not (root / ".pymolt" / "plans").exists()


def test_plan_json_carries_the_baseline_warning(cli_project):
    from pymolt.verify.service import load_contract_state

    root = cli_project
    trace = load_contract_state(root).baseline.trace_path
    with open(trace, "a", encoding="utf-8") as fh:
        fh.write("\n")

    payload = _plan_json(root)

    assert "the baseline recording changed since its capture" in payload["baseline_warning"]
    assert payload["baseline_capture_id"] != load_contract_state(root).baseline.capture_id
