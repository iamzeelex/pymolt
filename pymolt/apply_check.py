"""apply's self-check: compile before writing, and a smoke import around the write.

**Compile** (always, before the first write). The frozen new source of every changed file is
compiled on the target Python, and on the base Python for a change that claims to run there
(``PlannedFileChange.runs_on_base``). Dependency rewrites are included. A file whose new source
fails where its old source compiled is a failure the plan introduced: apply refuses and writes
nothing. A file that already failed before the rewrite is recorded as pre-existing; the plan's
compile findings report it, and the rewrite did not cause it.

**Smoke** (around the write; on by default in the CLI, ``apply --no-smoke`` turns it off). The
touched modules are imported in the target environment that setup names, once on the old code
and once on the new. A module that imported before and fails after is a failure the plan
introduced: apply restores the files. One that failed both times is pre-existing.

A check that cannot run (no interpreter, no target environment, a crashed or timed-out
harness) is ``skipped`` with the reason: never "passed". Interpreters run through
``pymolt.adapters.python_runner`` (a container base through its runner); nothing here prompts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

SMOKE_TIMEOUT = 300.0

_SMOKE_SCRIPT = r'''
import importlib, json, sys
request = json.loads(sys.stdin.read())
for entry in reversed(request["paths"]):
    if entry not in sys.path:
        sys.path.insert(0, entry)
failed = {}
for name in request["modules"]:
    try:
        importlib.import_module(name)
    except BaseException as exc:
        failed[name] = "%s: %s" % (type(exc).__name__, str(exc)[:300])
print("@@pymolt-smoke@@ " + json.dumps({"python": "%d.%d" % sys.version_info[:2],
                                       "failed": failed}))
'''


class CompileCheck(BaseModel):
    """The compile check on one side (``target`` or ``base``)."""

    side: Literal["target", "base"]
    status: Literal["passed", "failed", "skipped"]
    python: str | None = None        # the interpreter's own major.minor
    executable: str | None = None
    files: list[str] = Field(default_factory=list)
    failed: dict[str, str] = Field(default_factory=dict)        # introduced by the plan
    pre_existing: dict[str, str] = Field(default_factory=dict)  # failed before it too
    reason: str | None = None        # why it was skipped


class SmokeCheck(BaseModel):
    """The import of the touched modules in the target environment, before and after."""

    status: Literal["passed", "failed", "skipped"]
    python: str | None = None
    executable: str | None = None
    modules: list[str] = Field(default_factory=list)
    failed: dict[str, str] = Field(default_factory=dict)        # imported before, not after
    pre_existing: dict[str, str] = Field(default_factory=dict)  # failed before too (after's error)
    not_imported: dict[str, str] = Field(default_factory=dict)  # file -> why it was left out
    reason: str | None = None


class SelfCheck(BaseModel):
    """What apply checked about its own write (recorded on the receipt)."""

    compile: list[CompileCheck] = Field(default_factory=list)
    smoke: SmokeCheck | None = None  # None: not run (apply records why as a skipped check)

    def summary(self) -> str:
        """One line for status and the apply summary."""
        parts = []
        for check in self.compile:
            where = "the base" if check.side == "base" else "the target"
            if check.status == "skipped":
                parts.append(f"compile on {where} not checked ({check.reason})")
                continue
            text = (f"compile on {where} {check.python or '?'}: {len(check.files)} file(s) "
                    + ("ok" if check.status == "passed" else f"{len(check.failed)} failed"))
            if check.pre_existing:
                text += f", {len(check.pre_existing)} already failing before"
            parts.append(text)
        if not self.compile:
            parts.append("compile: no file changed")
        smoke = self.smoke
        if smoke is None:
            parts.append("smoke import not run")
        elif smoke.status == "skipped":
            parts.append(f"smoke import not checked ({smoke.reason})")
        else:
            text = (f"smoke import on {smoke.python or '?'}: {len(smoke.modules)} module(s) "
                    + ("ok" if smoke.status == "passed" else f"{len(smoke.failed)} failed"))
            if smoke.pre_existing:
                text += f", {len(smoke.pre_existing)} already failing before"
            parts.append(text)
        return "; ".join(parts)


# ── compile ───────────────────────────────────────────────────────────────────────────────


def compile_changes(root: Path, plan: Any) -> list[CompileCheck]:
    """Compile every changed file's new source on the target, and on the base where the change
    claims ``runs_on_base``. Writes nothing; never raises."""
    changes = {c.path: c for c in plan.changes}
    if not changes:
        return []
    from pymolt.pyversion.facts import find_target_interpreter

    try:
        target_exe = find_target_interpreter(root, plan.target_python)
    except Exception:  # noqa: BLE001 - no interpreter is an outcome, not a crash
        target_exe = None
    checks = [_compile_side("target", target_exe, None, changes,
                            missing=f"no interpreter for Python {plan.target_python}")]
    on_base = {path: c for path, c in changes.items() if c.runs_on_base is True}
    if on_base:
        base_python = _base_python(root)
        exe, runner = _base_side(root, base_python)
        checks.append(_compile_side("base", exe, runner, on_base,
                                    missing=f"no interpreter or container for the base "
                                            f"Python {base_python or '?'}"))
    return checks


def _base_python(root: Path) -> str | None:
    from pymolt.ingestion.config import EnvConfig

    config = EnvConfig.load(root / ".pymolt" / "env_config.json")
    return config.base_python if config is not None else None


def _base_side(root: Path, base_python: str | None):
    if not base_python:
        return None, None
    try:
        from pymolt.pyversion.plan import _base_side as base_side

        return base_side(root, base_python)
    except Exception:  # noqa: BLE001
        return None, None


def _compile_side(side: Literal["target", "base"], exe: str | None, runner: Any,
                  changes: dict[str, Any], *, missing: str) -> CompileCheck:
    from pymolt.pyversion.fixes import recompile_detailed

    if not exe and runner is None:
        return CompileCheck(side=side, status="skipped", reason=missing)
    executable = exe or getattr(runner, "executable", "python")
    shown = (f"{executable} in container {runner.name}" if runner is not None
             else str(executable))
    bad, failure, python = recompile_detailed(
        {path: c.new_source for path, c in changes.items()}, str(executable), runner=runner)
    if failure:
        return CompileCheck(side=side, status="skipped", executable=shown,
                            reason=f"the check did not run: {failure}")
    pre: dict[str, str] = {}
    if bad:
        before, failure, _ = recompile_detailed(
            {path: changes[path].old_source for path in bad}, str(executable), runner=runner)
        if not failure:
            pre = {path: error for path, error in bad.items() if path in before}
            bad = {path: error for path, error in bad.items() if path not in before}
    return CompileCheck(side=side, status="failed" if bad else "passed", python=python,
                        executable=shown, files=sorted(changes), failed=bad, pre_existing=pre)


# ── smoke ─────────────────────────────────────────────────────────────────────────────────


def module_for(root: Path, relative: str) -> tuple[str, str] | str:
    """``(sys.path entry, module name)`` of a project file, or why it is not imported."""
    path = (root / relative).resolve()
    if path.suffix != ".py":
        return "not a .py file"
    if path.stem in ("setup", "__main__"):
        return f"{path.name} runs as a program when imported"
    parts = [] if path.stem == "__init__" else [path.stem]
    folder = path.parent
    while (folder / "__init__.py").is_file() and folder.parent != folder:
        parts.insert(0, folder.name)
        folder = folder.parent
    if not parts or not all(part.isidentifier() for part in parts):
        return "its path is not an importable module name"
    return str(folder), ".".join(parts)


def smoke_target(root: Path) -> tuple[str | None, str | None]:
    """``(python of the target environment setup names, None)``, or ``(None, why not)``."""
    from pymolt.delta.envs import python_in
    from pymolt.ingestion.config import EnvConfig

    config = EnvConfig.load(root / ".pymolt" / "env_config.json")
    if config is None or not config.target_env_path:
        return None, ("setup names no target environment "
                      "(`pymolt setup . --target-env PATH`)")
    env = Path(config.target_env_path).expanduser()
    env = env if env.is_absolute() else root / env
    exe = python_in(env)
    if exe is None:
        return None, f"no interpreter in the target environment {env}"
    return str(exe), None


def smoke_modules(root: Path, plan: Any) -> tuple[list[tuple[str, str]], dict[str, str]]:
    """The touched modules ``[(entry, module)]`` and the files left out ``{file: why}``."""
    modules: list[tuple[str, str]] = []
    left_out: dict[str, str] = {}
    for change in plan.changes:
        found = module_for(root, change.path)
        if isinstance(found, str):
            left_out[change.path] = found
        else:
            modules.append(found)
    return sorted(set(modules), key=lambda item: item[1]), left_out


def import_modules(exe: str, root: Path, modules: list[tuple[str, str]], *,
                   timeout: float = SMOKE_TIMEOUT) -> tuple[dict[str, str] | None, str | None,
                                                            str | None]:
    """Import ``modules`` with ``exe`` (cwd: the project). Returns ``(failed, python, why)``:
    ``failed`` is None, and ``why`` says why, when the import harness itself did not run."""
    from pymolt.adapters.python_runner import run_in

    request = json.dumps({"paths": sorted({entry for entry, _ in modules}),
                          "modules": [name for _, name in modules]})
    try:
        proc = run_in(None, [exe, "-B", "-c", _SMOKE_SCRIPT], stdin=request, cwd=str(root),
                      timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - a harness that did not run is a reason
        return None, None, f"{type(exc).__name__}: {exc}"
    for line in (proc.stdout or "").splitlines():
        if line.startswith("@@pymolt-smoke@@ "):
            try:
                data = json.loads(line[len("@@pymolt-smoke@@ "):])
                return dict(data["failed"]), data.get("python"), None
            except (ValueError, KeyError, TypeError):
                break
    tail = ((proc.stderr or "").strip().splitlines() or [""])[-1]
    return None, None, f"exit {proc.returncode}: {tail[:160]}"


def smoke_verdict(modules: list[tuple[str, str]], before: dict[str, str] | None,
                  after: dict[str, str] | None, *, exe: str, python: str | None,
                  before_why: str | None, after_why: str | None,
                  left_out: dict[str, str]) -> SmokeCheck:
    """Compare the two import runs. A module that imported before and fails after failed;
    one that failed both times is pre-existing; a run that did not happen skips the check,
    except an after-run that broke down where the before-run worked, which fails it."""
    names = [name for _, name in modules]
    base = {"executable": exe, "python": python, "modules": names, "not_imported": left_out}
    if before is None:
        return SmokeCheck(status="skipped", reason=f"the import on the old code did not run "
                                                   f"({before_why})", **base)
    if after is None:
        # The new code broke the harness itself (it exited, hung or crashed): every module
        # that imported before counts as failed.
        failed = {name: f"the import did not finish ({after_why})" for name in names
                  if name not in before}
        if not failed:
            return SmokeCheck(status="skipped", pre_existing=dict(before),
                              reason=f"the import on the new code did not run ({after_why})",
                              **base)
        return SmokeCheck(status="failed", failed=failed, pre_existing=dict(before), **base)
    failed = {name: error for name, error in after.items() if name not in before}
    pre = {name: error for name, error in after.items() if name in before}
    return SmokeCheck(status="failed" if failed else "passed", failed=failed,
                      pre_existing=pre, **base)
