"""Shared fixtures for the stepwise apply and rollback tests: real recipes run through the
preview walk's observer, so each plan's steps, hunks and anchors are the builder's own."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from pymolt.codemods.apply import apply_patterns
from pymolt.codemods.client import DependencyMigration
from pymolt.codemods.models import CodemodPattern, FilePreview
from pymolt.ingestion.config import EnvConfig, ToolChoice
from pymolt.migration_plan import create_migration_plan
from pymolt.verify import service
from pymolt.verify.models import CaptureMode
from pymolt.workorder.builder import StepRecorder


def rename(old: str, new: str, *, claimed: bool = True) -> CodemodPattern:
    evidence = [f"pymolt.delta MOVED: {old} -> {new}"] if claimed else []
    return CodemodPattern(old_qualname=old, new_qualname=new, kind="rename-call",
                          evidence=evidence)


def project(root: Path, *, baseline: bool = True, manifest: str = "a\n",
            target: str = "a==2.0\n") -> Path:
    """A configured project, with a baseline capture when ``baseline``. Returns the target
    manifest."""
    (root / "requirements.txt").write_text(manifest, encoding="utf-8")
    target_path = root / "requirements-target.txt"
    target_path.write_text(target, encoding="utf-8")
    EnvConfig(selected_manifest="requirements.txt", selected_tool=ToolChoice.UV,
              base_python="3.11", target_python="3.12").save(
        root / ".pymolt" / "env_config.json")
    if baseline:
        script = root / "baseline_run.py"
        script.write_text("import json\njson.loads('{}')\n", encoding="utf-8")
        service.capture_named_trace(root, "baseline", CaptureMode.LIVE_COMMAND,
                                    command=[sys.executable, str(script)], target="json")
    return target_path


def plan_for(root: Path, files: dict[str, tuple[str, list[CodemodPattern]]], *,
             baseline: bool = True, manifest: str = "a\n", target: str = "a==2.0\n",
             save: bool = True):
    """A plan of the recipes ``files`` names, built from the preview walk's recorded chains."""
    target_path = project(root, baseline=baseline, manifest=manifest, target=target)
    recorder = StepRecorder()
    previews = []
    for name, (source, patterns) in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")

        def observe(recipe, before, after, path=path):
            recorder.record(recipe, str(path), before, after)

        new, hits = apply_patterns(source, patterns, on_step=observe)
        previews.append(FilePreview(path=str(path), old_source=source, new_source=new,
                                    sites=sum(n for _p, n in hits),
                                    patterns=[p for p, _n in hits]))
    plan = create_migration_plan(
        root, previews, [DependencyMigration("a", "1.0", "2.0")], target_python="3.12",
        source_manifest="requirements.txt", target_manifest_path=target_path,
        files_scanned=len(files), predict=False, recorder=recorder)
    if save:
        plan.save(root)
    return plan


def plan_next(root: Path, files: dict[str, list[CodemodPattern]], *, save: bool = True):
    """A new plan (a re-plan) over the files as they are now, with the same configuration and
    baseline: ``{path: [patterns]}``."""
    recorder = StepRecorder()
    previews = []
    for name, patterns in files.items():
        path = root / name
        source = path.read_text(encoding="utf-8")

        def observe(recipe, before, after, path=path):
            recorder.record(recipe, str(path), before, after)

        new, hits = apply_patterns(source, patterns, on_step=observe)
        previews.append(FilePreview(path=str(path), old_source=source, new_source=new,
                                    sites=sum(n for _p, n in hits),
                                    patterns=[p for p, _n in hits]))
    plan = create_migration_plan(
        root, previews, [DependencyMigration("a", "1.0", "2.0")], target_python="3.12",
        source_manifest="requirements.txt", target_manifest_path=root / "requirements-target.txt",
        files_scanned=len(files), predict=False, recorder=recorder)
    if save:
        plan.save(root)
    return plan


def post_capture(root: Path, *recorded: str):
    """A post-migration capture whose source snapshot records ``recorded`` as they are now (a
    real capture records the files whose calls it saw)."""
    import gzip
    import hashlib

    script = root / "post_run.py"
    script.write_text("import json\njson.loads('{}')\n", encoding="utf-8")
    slot = service.capture_named_trace(root, "post_migration", CaptureMode.LIVE_COMMAND,
                                       command=[sys.executable, str(script)], target="json")
    with gzip.open(slot.sources_path, "rt", encoding="utf-8") as fh:
        data = json.load(fh)
    for name in recorded:
        text = (root / name).read_text(encoding="utf-8")
        data["files"][name] = {"sha256": hashlib.sha256(text.encode()).hexdigest(),
                               "text": text}
    with gzip.open(slot.sources_path, "wt", encoding="utf-8") as fh:
        json.dump(data, fh)
    return slot


def code_steps(plan) -> list:
    return [s for s in plan.steps if s.type in ("codemod", "python-fix")]


def ledger(root: Path) -> dict:
    path = root / ".pymolt" / "apply_ledger.json"
    return json.loads(path.read_text()) if path.is_file() else {"events": [], "entries": []}


def events(root: Path) -> list[tuple]:
    return [(e["seq"], e["event"], e["step_id"], e.get("wrote"), e.get("restored"))
            for e in ledger(root)["events"]]


def read(root: Path, name: str) -> str:
    return (root / name).read_text(encoding="utf-8")


# A file three recipes change: mech-1 and mech-2 meet different statements; mech-3 rewrites
# what mech-1 wrote, so it depends on it.
APP = ("import a\n"
       "\n"
       "\n"
       "def run(v):\n"
       "    x = a.f(v)\n"
       "    z = 1\n"
       "    return a.h(x)\n")
RECIPES = [rename("a.f", "a.g"), rename("a.h", "a.k"), rename("a.g", "a.m")]


def manual_step(root: Path, file: str, line: int, *, sid: str = "sig-1",
                step_type: str = "api-change", executor: str = "agent",
                claim: str | None = "preserve", expect: str | None = None,
                symbol: tuple[str | None, str | None] = ("a.h", "a.k"), text: str | None = None,
                risk_ids: list[str] | None = None, depends_on: list[str] | None = None):
    """An agent or person step whose one site is the statement on ``line`` of ``file`` (its
    anchor made on ``text``, default the file as it is now)."""
    from pymolt.anchors import make_anchor
    from pymolt.workorder.models import (
        ManualDo,
        PlanStep,
        StepDo,
        StepSymbol,
        StepVerify,
        StepWhy,
        step_key,
    )

    source = text if text is not None else (root / file).read_text(encoding="utf-8")
    where = make_anchor(source, line, file=file, python="3.11", symbol=symbol[0], id="a1")
    phase = {"api-change": "signature", "python-manual": "signature",
             "manual-rewrite": "mechanical", "behaviour-change": "behaviour",
             "review": "behaviour"}[step_type]
    return PlanStep(
        id=sid, key=step_key(step_type, f"{sid}|{symbol[0]}"), phase=phase, type=step_type,
        title=f"{symbol[0]} by hand", executor=executor, claim=claim,
        basis="manual" if claim else None, symbol=StepSymbol(old=symbol[0], new=symbol[1]),
        where=[where], depends_on=list(depends_on or ()),
        why=StepWhy(risk_ids=list(risk_ids or ())),
        do=StepDo(manual=ManualDo(summary="make it by hand")),
        verify=StepVerify(judge="strict" if claim == "preserve" else "expectation",
                          expect=expect))


def with_steps(root: Path, plan, *steps, save: bool = True):
    """``plan`` with ``steps`` added after its own (saved, when ``save``)."""
    out = plan.model_copy(update={"steps": [*plan.steps, *steps]})
    if save:
        out.save(root)
    return out
