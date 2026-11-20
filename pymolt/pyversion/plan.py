"""The plan's python part: the move, its delta, the compile check and the rated risks.

``plan_python`` never raises: a failed piece becomes a note and a lower quality, because a
plan stands without its python prediction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pymolt.pyversion.guards import parse_version


def same_python_minor(a: str | None, b: str | None) -> bool:
    return parse_version(a) is not None and parse_version(a) == parse_version(b)


@dataclass
class PythonStage:
    """The python part of a plan: its summary, rated risks, compile findings, and the
    previews with the python fixes layered on (see ``pyversion.fixes``)."""

    python: dict[str, Any]
    risks: Any
    findings: list[dict[str, Any]]
    previews: list[Any] = field(default_factory=list)
    # Per fixed file, the plan-time re-compile results (``FixOutcome.checks``).
    checks: dict[str, dict[str, str]] = field(default_factory=dict)


def plan_python(project_dir: str | Path, base_python: str, target_python: str,
                sites: list[Any] | None = None):
    """``(python, risks, compile_findings)``: ``python`` is ``{from, to, quality, sides, ...}``,
    ``risks`` the python-scope ``PredictionReport``, ``compile_findings`` the target's
    SyntaxError / SyntaxWarning findings. (No previews: use :func:`plan_python_stage` to also
    get the fixes.)"""
    stage = plan_python_stage(project_dir, base_python, target_python, sites)
    return stage.python, stage.risks, stage.findings


def plan_python_stage(project_dir: str | Path, base_python: str, target_python: str,
                      sites: list[Any] | None = None,
                      previews: list[Any] | None = None,
                      on_step: Any = None) -> PythonStage:
    """The python move for a plan. With ``previews`` (the dependency rewrites) the python
    fixes are layered onto them and the risks they fix are marked ``fixed_by_plan``.
    ``on_step`` observes each fix that changed a kept file (``apply_python_fixes``).
    Never raises: a failed piece becomes a note and a lower quality."""
    from pymolt.pyversion.compile_check import compile_check
    from pymolt.pyversion.facts import find_target_interpreter, python_delta
    from pymolt.pyversion.fixes import apply_python_fixes, python_patterns
    from pymolt.verify.predict import predict_python_risks

    root = Path(project_dir).resolve()
    notes: list[str] = []
    if sites is None:
        try:
            from pymolt.verify.contact_map import build_contact_map

            sites = list(getattr(build_contact_map(root), "stdlib_sites", None) or [])
        except Exception as exc:  # noqa: BLE001 - the plan stands without python sites
            sites = []
            notes.append(f"stdlib sites unavailable: {type(exc).__name__}: {exc}")
    exe = find_target_interpreter(root, target_python)
    base_exe, base_runner = _base_side(root, base_python)
    runtime_facts = _baseline_stdlib_calls(root, notes)
    report = python_delta(root, sites, base_python, target_python, target_executable=exe,
                          base_executable=base_exe, base_runner=base_runner,
                          runtime_symbols={f.symbol for f in runtime_facts}
                          | _inherited_stdlib_spellings(root, notes))
    findings: list[dict[str, Any]] = []
    if exe:
        # A local base interpreter proves which target errors are new and which were there
        # before (a Python 2 leftover); a base in a container cannot run this check, so the
        # base side stays an estimate from the base version's grammar.
        checked = compile_check(root, exe, base_python=base_python,
                                base_executable=base_exe if base_runner is None else None)
        findings = checked["findings"]
        if checked.get("failure"):
            notes.append(f"compile check: {checked['failure']}")
    else:
        notes.append("compile check skipped: no interpreter for the target Python")
    out_previews = list(previews) if previews is not None else []
    fixes: dict[str, Any] = {"patterns": 0, "files": 0}
    checks: dict[str, dict[str, str]] = {}
    try:
        patterns = python_patterns(report, sites)
        if patterns:
            outcome = apply_python_fixes(
                root, out_previews, patterns, sites, target=target_python,
                target_executable=exe, base_executable=base_exe, base_runner=base_runner,
                on_step=on_step)
            out_previews = outcome.previews
            checks = outcome.checks
            notes.extend(outcome.notes)
            fixes = {"patterns": len(outcome.patterns), "files": len(outcome.files)}
    except Exception as exc:  # noqa: BLE001 - the plan stands without python fixes
        notes.append(f"python fixes failed: {type(exc).__name__}: {exc}")
    risks = predict_python_risks(report, sites, base=base_python, target=target_python,
                                 compile_findings=findings, changes=_changes(root, out_previews),
                                 runtime_facts=runtime_facts, root=root)
    risks.notes.extend(notes)
    python = {
        "from": base_python, "to": target_python, "quality": report.quality,
        "sides": dict(report.sides), "sites": len(sites), "fixes": fixes,
        "notes": [*report.notes, *notes],
    }
    return PythonStage(python, risks, findings, out_previews, checks)


def _inherited_stdlib_spellings(root: Path, notes: list[str]) -> set[str]:
    """Stdlib attributes reached through project classes or under an alias the runtime does not
    record (``self.assertEquals`` on a ``unittest.TestCase`` subclass): the spellings the project
    wrote, from ``pymolt.delta.touched``. ``set()`` when the scan fails."""
    try:
        from pymolt.delta import touched
        from pymolt.delta.envs import _baseline_trace

        found = set(touched.inherited_spellings(root))
        trace = _baseline_trace(root)
        if trace is not None and (trace.exists()
                                  or Path(str(trace) + ".facts.json").exists()):
            from pymolt.verify.facts import load_facts

            found |= touched.alias_spellings(load_facts(trace, root=root).facts, root)
        return {s for s in found if touched.is_stdlib(s)}
    except Exception as exc:  # noqa: BLE001 - an enrichment, never a failure
        notes.append(f"inherited stdlib spellings unavailable: {type(exc).__name__}: {exc}")
        return set()


def _baseline_stdlib_calls(root: Path, notes: list[str]) -> list[Any]:
    """The baseline capture's calls into the stdlib from the project's own files: the python
    delta checks those symbols too, and rates each observed call site. ``[]`` with no
    baseline (a note says why when it exists but cannot be read)."""
    try:
        from pymolt.delta.envs import _baseline_trace
        from pymolt.verify.facts import load_facts
        from pymolt.verify.predict import python_runtime_facts

        trace = _baseline_trace(root)
        if trace is None or not (trace.exists() or Path(str(trace) + ".facts.json").exists()):
            return []
        return python_runtime_facts(load_facts(trace, root=root), root)
    except Exception as exc:  # noqa: BLE001 - the plan stands on the static sites alone
        notes.append(f"baseline stdlib calls unavailable: {type(exc).__name__}: {exc}")
        return []


def _changes(root: Path, previews: list[Any]) -> list[dict[str, Any]]:
    """The previews as plan-style changes (project-relative path, sources, recipe keys)."""
    from pymolt.codemods.models import recipe_key

    out = []
    for p in previews:
        if p.old_source == p.new_source:
            continue
        try:
            rel = Path(p.path).resolve().relative_to(root).as_posix()
        except ValueError:
            continue
        out.append({"path": rel, "old_source": p.old_source, "new_source": p.new_source,
                    "recipes": [recipe_key(i) for i in p.applied_recipes()]})
    return out


def _base_side(root: Path, base_python: str):
    """``(executable, runner)`` for probing the base: the baseline's container when one is
    running (a 3.6 base lives there), else a host interpreter of that version, else none
    (base presence then comes from data and the delta says so)."""
    try:
        from pymolt.delta.envs import baseline_runner

        spec = baseline_runner(root)
    except Exception:  # noqa: BLE001 - no runner is an ordinary outcome
        spec = None
    if spec is not None:
        return spec.executable, spec
    try:  # the interpreter the baseline was captured with (recorded in its metadata)
        from pymolt.delta.envs import _baseline_trace
        from pymolt.verify.identity_resolver import select_interpreter

        trace = _baseline_trace(root)
        choice = select_interpreter(trace=trace) if trace is not None else None
        if choice is not None and choice.source == "trace-metadata" and choice.executable \
                and python_minor_of(str(choice.executable)) == _minor_of(base_python):
            return str(choice.executable), None
    except Exception:  # noqa: BLE001 - fall through to a host interpreter
        pass
    try:
        from pymolt.ingestion.fallback_compiler import find_python_interpreter

        found = find_python_interpreter(base_python, project_dir=root)
    except Exception:  # noqa: BLE001
        found = None
    return (str(found) if found else None), None


def _minor_of(version: str | None) -> tuple[int, int] | None:
    return parse_version(version)


def python_minor_of(executable: str) -> tuple[int, int] | None:
    """``(major, minor)`` of an interpreter, asked of it; None when it cannot be run."""
    import subprocess

    try:
        out = subprocess.run([executable, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
                             capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_version(out)
