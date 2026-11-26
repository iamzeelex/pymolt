"""A plan, its PLAN.md and the status view name no folder of the machine (design
plan-lineage-and-report §5.2): made in a git repository under a fake ``$HOME``, with the
baseline's interpreter and the target environment under it, they hold no temp, home or
repository-toplevel path in any field, the commands included (``pymolt.machine_paths``). The
project's own text is data: a path written in its source stays as written."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys

import pytest
from _stepwise import APP, RECIPES, plan_for, plan_next

from pymolt.ingestion.config import EnvConfig
from pymolt.interfaces import views
from pymolt.plan_store import stored_plan_file
from pymolt.status import build_status
from pymolt.verify import service
from pymolt.verify.models import CaptureMode
from tests.test_no_private_data import leaks

M = "app/m.py"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def test_a_plan_and_status_hold_no_path_of_the_machine(tmp_path, monkeypatch):
    home = tmp_path / "home" / "tess"
    home.mkdir(parents=True)
    (home / ".gitconfig").write_text("[user]\n\temail = t@example.invalid\n\tname = T\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    toplevel = tmp_path / "work" / "monorepo"
    root = toplevel / "services" / "api"
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=toplevel, check=True)
    plan_for(root, {M: (APP, RECIPES)}, baseline=False, save=False)
    subprocess.run(["git", "add", "-A"], cwd=toplevel, check=True)
    subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"],
                   cwd=toplevel, check=True)
    # The baseline's interpreter and the target environment live under the home folder.
    python = home / ".envs" / "base" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    config_path = root / ".pymolt" / "env_config.json"
    config = EnvConfig.load(config_path)
    config.target_env_path = str(home / ".envs" / "3.12")
    config.save(config_path)
    service.capture_named_trace(root, "baseline", CaptureMode.LIVE_COMMAND,
                                command=[str(python), "-c", "import json; json.loads('{}')"],
                                target="json")
    plan = plan_next(root, {M: RECIPES}, save=False)
    result = plan.save_planned(root)
    from pymolt.workorder.render import write_saved_plan_md

    plan_md = write_saved_plan_md(result, root)

    assert plan.source.quality == "git" and plan.source.git_prefix == "services/api"
    stored = json.loads(stored_plan_file(root, plan.name).read_text())
    command = ["~/.envs/base/bin/python", "-c", "import json; json.loads('{}')"]
    assert stored["baseline"]["command"] == command
    target_env = next(s for s in stored["steps"] if s["type"] == "target-env")
    assert "setup names a target environment: ~/.envs/3.12" in target_env["why"]["evidence"]
    texts = {
        "plan.json": json.dumps(stored),
        "PLAN.md": plan_md.read_text(),
        "status view": json.dumps(views.status_view(build_status(root))),
        "plan view": json.dumps(views.plan_view(
            plan, {}, "requirements-target.txt", plan_path=".pymolt/plans/x/plan.json",
            saved=result)["data"]),
    }
    machine = {str(p) for base in (tmp_path, home, toplevel) for p in (base, base.resolve())}
    for what, text in texts.items():
        found = [p for p in machine if p in text]
        assert not found, f"{what} names {found}"
    assert leaks(texts["plan.json"]) == [] and leaks(texts["PLAN.md"]) == []


def test_the_projects_own_text_is_kept_as_written(tmp_path, monkeypatch):
    home = tmp_path / "home" / "tess"
    root = home / "app"
    root.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    literal = f'DATA = "{root}/data.txt"  # {home}\n'
    source = APP + literal
    plan = plan_for(root, {M: (source, RECIPES)}, baseline=False)

    stored = json.loads(stored_plan_file(root, plan.name).read_text())
    (change,) = stored["changes"]
    assert change["old_source"] == source and change["new_source"].endswith(literal)
