"""What status says about the current plan and the captures (design plan-lineage-and-report §1):
the plan's name and identity, the captures' ids, the 0.2 layout note and the branch-drift
advisory, in ``status``, ``status --json``, the status view and the apply and rollback views."""
from __future__ import annotations

import json
import shutil
import subprocess

import pytest
from _stepwise import APP, RECIPES, plan_for, plan_next, post_capture
from typer.testing import CliRunner

from pymolt.interfaces import views
from pymolt.interfaces.cli.commands import app
from pymolt.migration_plan import DEFAULT_PLAN_PATH, MigrationPlan
from pymolt.plan_store import load_pointer, migrate_legacy
from pymolt.status import LEGACY_BESIDE_NOTE, LEGACY_NOTE, build_status
from pymolt.verify.service import load_contract_state, slot_capture_id
from pymolt.workorder.apply import apply_steps
from pymolt.workorder.rollback import rollback_steps
from tests.fixtures.v02_loader import materialize

runner = CliRunner()
M = "app/m.py"
EMAIL = "t@example.invalid"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """No compile on apply, no git config of the machine's, no repository above tmp_path."""
    monkeypatch.setattr("pymolt.apply_check.compile_changes", lambda root, plan: [])
    config = tmp_path / "gitconfig"
    config.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))


def _planned(root) -> MigrationPlan:
    """A plan as `pymolt plan` saves it (stamped with its version and git source)."""
    plan = plan_for(root, {M: (APP, RECIPES)}, save=False)
    plan.save_planned(root)
    return plan


def test_status_names_the_plan_and_the_captures(tmp_path):
    plan = _planned(tmp_path)
    post_capture(tmp_path)
    state = load_contract_state(tmp_path)
    baseline_id = slot_capture_id(tmp_path, state.baseline)
    post_id = slot_capture_id(tmp_path, state.post_migration)

    status = build_status(tmp_path)

    assert status.phase("plan").detail.startswith(f"plan {plan.name} · ")
    assert status.phase("baseline").detail.startswith(f"{baseline_id} · ")
    assert status.phase("post-migration").detail.startswith(f"{post_id} · ")
    assert status.captures == {"baseline": baseline_id, "post_migration": post_id,
                               "baseline_replicate": None}
    assert status.plan == {
        "name": plan.name, "plan_id": plan.plan_id, "label": None,
        "created_at": plan.created_at, "schema": 3, "pymolt_version": plan.pymolt_version,
        "git_commit": None, "git_dirty": None,
        "lineage": {"root": plan.plan_id, "parent": None, "generation": 0},
        "identity": f"plan {plan.plan_id[:8]} · " + status.plan["identity"].split(" · ", 1)[1],
    }
    assert " · commit unknown (not-a-repo) · fresh" in status.plan["identity"]
    assert status.drift is None and status.notes == []


def test_the_status_view_and_the_cli_say_it(tmp_path):
    plan = _planned(tmp_path)
    baseline_id = slot_capture_id(tmp_path, load_contract_state(tmp_path).baseline)

    view = views.status_view(build_status(tmp_path))
    cli = runner.invoke(app, ["status", str(tmp_path)])
    as_json = json.loads(runner.invoke(app, ["status", str(tmp_path), "--json"]).stdout)

    assert f"Plan {plan.name}; baseline {baseline_id}." in view["summary"].splitlines()
    assert view["data"]["plan"]["name"] == plan.name and view["data"]["drift"] is None
    assert view["data"]["captures"]["baseline"] == baseline_id
    text = " ".join(cli.output.split())
    assert cli.exit_code == 0 and f"current plan: plan {plan.plan_id[:8]} · " in text
    assert as_json["plan"]["name"] == plan.name and as_json["captures"]["baseline"] == baseline_id


def test_status_reads_no_git_for_a_plan_made_outside_a_repository(tmp_path, monkeypatch):
    _planned(tmp_path)

    def no_git(*_a, **_k):
        raise AssertionError("status read git for a plan with no commit")

    monkeypatch.setattr("pymolt.adapters.git.head_commit", no_git)
    status = build_status(tmp_path)
    assert status.drift is None and status.notes == []


def test_apply_and_rollback_views_name_the_plan(tmp_path):
    plan = _planned(tmp_path)
    result = apply_steps(tmp_path, plan, step="mech-1")
    assert views.apply_view(result, plan)["data"]["plan_name"] == plan.name
    rolled = rollback_steps(tmp_path, step="mech-1")
    assert views.rollback_view(rolled)["data"]["plan_name"] == plan.name


# ── branch drift ─────────────────────────────────────────────────────────────────────────


@pytest.fixture
def repo(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    root = tmp_path / "p"
    root.mkdir()

    def git(*args):
        return subprocess.run(["git", "-c", f"user.email={EMAIL}", "-c", "user.name=T",
                               "-c", "commit.gpgsign=false", *args], cwd=root, check=True,
                              capture_output=True, text=True).stdout.strip()

    git("init", "-q")
    return root, git


def _commit(git, root, name: str) -> str:
    (root / name).write_text(f"{name}\n")
    git("add", "-A")
    git("commit", "-q", "-m", name)
    return git("rev-parse", "HEAD")


def test_drift_when_head_moved_since_the_plan_and_none_once_re_planned(repo):
    root, git = repo
    plan_for(root, {M: (APP, RECIPES)}, save=False)       # the project's files and baseline
    first = _commit(git, root, "one")
    plan = plan_next(root, {M: RECIPES}, save=False)
    plan.save_planned(root)
    assert plan.source.git_commit == first and plan.source.git_dirty is False
    assert build_status(root).drift is None

    second = _commit(git, root, "two")
    status = build_status(root)

    assert status.drift == {"plan_commit": first, "head": second}
    assert status.notes == [
        f"the current plan was made at commit {first[:7]}; HEAD is {second[:7]}: re-plan "
        "(`pymolt plan .`) if the code it plans for changed"]
    # Advisory only: no phase changes state for it, and the next command is the same.
    assert status.next_command == "pymolt apply ."
    assert status.phase("plan").state == "done"

    again = plan_next(root, {M: RECIPES}, save=False).save_planned(root)
    assert again.unchanged_from == plan.name
    assert load_pointer(root).checked_commit == second
    assert build_status(root).drift is None and build_status(root).notes == []


def test_drift_says_when_the_plan_was_made_with_uncommitted_changes(repo):
    root, git = repo
    plan_for(root, {M: (APP, RECIPES)}, save=False)
    _commit(git, root, "one")
    (root / "one").write_text("changed\n")                # a tracked file differs: dirty
    plan = plan_next(root, {M: RECIPES}, save=False)
    plan.save_planned(root)
    assert plan.source.git_dirty is True
    _commit(git, root, "two")

    note = build_status(root).notes[0]

    assert " with uncommitted changes; HEAD is " in note


# ── a 0.2 project ────────────────────────────────────────────────────────────────────────


def test_status_says_a_plan_is_in_the_02_layout(tmp_path):
    root = materialize(tmp_path / "p")
    legacy = MigrationPlan.load(root)

    status = build_status(root)

    assert status.notes == [LEGACY_NOTE]
    assert status.plan["name"] == legacy.name and status.plan["schema"] == 2
    assert status.plan["pymolt_version"] is None and status.plan["git_commit"] is None
    assert status.plan["identity"].endswith(" · made by pymolt before 0.3")
    assert status.phase("plan").detail.startswith(f"plan {legacy.name} applied by migration run ")

    shutil.copy(root / DEFAULT_PLAN_PATH, tmp_path / "legacy.json")
    migrate_legacy(root)
    assert build_status(root).notes == []
    shutil.copy(tmp_path / "legacy.json", root / DEFAULT_PLAN_PATH)   # 0.2 planned again
    assert build_status(root).notes == [LEGACY_BESIDE_NOTE]
