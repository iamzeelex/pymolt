"""Shared fixtures for the stage 5 verify tests: a project whose code calls ``json`` (traced as a
named stdlib module), real baseline and post-migration captures, a plan bound to the baseline
with agent and person steps, and a fake terminal for person attestations."""
from __future__ import annotations

import sys
from pathlib import Path

from _stepwise import manual_step, project, with_steps

from pymolt.attest import AttestationStore, Attester
from pymolt.codemods.models import DependencyMigration
from pymolt.migration_plan import create_migration_plan
from pymolt.verify import service
from pymolt.verify.models import CaptureMode
from pymolt.workorder.builder import StepRecorder

M = "app/m.py"
APP = ("import json\n"
       "\n"
       "\n"
       "def go(v):\n"
       "    try:\n"
       "        y = json.dumps(v)\n"
       "    except TypeError:\n"
       "        y = None\n"
       "    return y\n"
       "\n"
       "\n"
       "def other(v):\n"
       "    return json.dumps([v])\n")
RUN = ("import sys\n"
       "sys.path.insert(0, {root!r})\n"
       "from app.m import go, other\n"
       "go({{'a': 1}})\n"
       "other(2)\n")
LINE = 6              # `y = json.dumps(v)`, the steps' site


class FakeTerminal:
    """A person on a terminal who types every nonce back (or never, with ``types=False``)."""

    def __init__(self, *, types: bool = True) -> None:
        self.types = types
        self.asked: list[str] = []

    def unavailable(self):
        return None

    def confirm(self, text: str, nonce: str) -> bool:
        self.asked.append(text)
        return self.types


def person(root: Path, *, types: bool = True) -> Attester:
    return Attester(prompter=FakeTerminal(types=types), store=AttestationStore())


def capture(root: Path, when: str):
    script = root / "run.py"
    script.write_text(RUN.format(root=str(root)), encoding="utf-8")
    return service.capture_named_trace(root, when, CaptureMode.LIVE_COMMAND,
                                       command=[sys.executable, str(script)], target="json",
                                       privacy="values")


def json_project(root: Path, *steps_args, save: bool = True):
    """A project with ``APP``, its baseline capture, and a plan bound to it whose steps are the
    env steps plus one ``manual_step`` per ``steps_args`` item (kwargs for it)."""
    project(root, baseline=False)
    (root / "app").mkdir(exist_ok=True)
    (root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (root / M).write_text(APP, encoding="utf-8")
    capture(root, "baseline")
    plan = create_migration_plan(
        root, [], [DependencyMigration("a", "1.0", "2.0")], target_python="3.12",
        source_manifest="requirements.txt", target_manifest_path=root / "requirements-target.txt",
        files_scanned=1, predict=False, recorder=StepRecorder())
    steps = [manual_step(root, M, kw.pop("line", LINE), text=APP,
                         symbol=kw.pop("symbol", ("json.dumps", "json.dumps")), **kw)
             for kw in (dict(a) for a in steps_args)]
    return with_steps(root, plan, *steps, save=save)


def edit(root: Path, old: str, new: str) -> None:
    path = root / M
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new), encoding="utf-8")


def verify(root: Path):
    """The contract report over the captures (as `pymolt verify` builds it)."""
    return service.build_contract_report_from_state(root)


def site_entries(report, line: int = LINE) -> list[dict]:
    """The fact entries at the steps' site (either side's line)."""
    entries = (report.all_fact_diff or {}).get("entries") or []
    out = []
    for e in entries:
        for side in ("old_site", "new_site"):
            site = e.get(side) or {}
            if site.get("line") == line and str(site.get("file", "")).endswith("m.py"):
                out.append(e)
                break
    return out
