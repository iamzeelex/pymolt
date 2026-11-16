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
                      previews: list[Any] | None = None) -> PythonStage:
    """The python move for a plan. With ``previews`` (the dependency rewrites) the python
    fixes are layered onto them and the risks they fix are marked ``fixed_by_plan``.
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
    report = python_delta(root, sites, base_python, target_python, target_executable=exe,
                          base_executable=base_exe, base_runner=base_runner)
    findings: list[dict[str, Any]] = []
    if exe:
        checked = compile_check(root, exe, base_python=base_python)
        findings = checked["findings"]
        if checked.get("failure"):
            notes.append(f"compile check: {checked['failure']}")
    else:
        notes.append("compile check skipped: no interpreter for the target Python")
    out_previews = list(previews) if previews is not None else []
    fixes: dict[str, Any] = {"patterns": 0, "files": 0}
    try:
        patterns = python_patterns(report, sites)
        if patterns:
            outcome = apply_python_fixes(
                root, out_previews, patterns, sites, target=target_python,
                target_executable=exe, base_executable=base_exe, base_runner=base_runner)
            out_previews = outcome.previews
            notes.extend(outcome.notes)
            fixes = {"patterns": len(outcome.patterns), "files": len(outcome.files)}
    except Exception as exc:  # noqa: BLE001 - the plan stands without python fixes
        notes.append(f"python fixes failed: {type(exc).__name__}: {exc}")
    risks = predict_python_risks(report, sites, base=base_python, target=target_python,
                                 compile_findings=findings, changes=_changes(root, out_previews))
    risks.notes.extend(notes)
    python = {
        "from": base_python, "to": target_python, "quality": report.quality,
        "sides": dict(report.sides), "sites": len(sites), "fixes": fixes,
        "notes": [*report.notes, *notes],
    }
    return PythonStage(python, risks, findings, out_previews)


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
                    "recipes": [recipe_key(i) for i in [*p.rules, *p.patterns]]})
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
    try:
        from pymolt.ingestion.fallback_compiler import find_python_interpreter

        found = find_python_interpreter(base_python, project_dir=root)
    except Exception:  # noqa: BLE001
        found = None
    return (str(found) if found else None), None
