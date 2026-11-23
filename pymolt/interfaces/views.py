"""The agent surface: one compact, honest payload per command.

``pymolt <command> --agent`` prints these objects and the MCP server returns them.
Both interfaces call the same service function and then the builder here, so the
summary wording, the list budgets, the completeness flags and the failure shape are
decided once and cannot drift apart.

Success envelope (every builder)::

    {"ok": true, "summary": "<short plain text>", "data": {...},
     "full_report_path": "<path or null>", "hint": "<next step or null>"}

Failure envelope, the same shape ``--json`` failures have on the CLI::

    {"ok": false, "error": "<one line>", "hint": "<how to fix>"}   # hint only if known

Every ``data`` carries three keys besides its own:

* ``warnings``: what the service flagged. Never dropped; a failure the agent
  cannot see is a failure it will report as success.
* ``complete``: True when every list in ``data`` is shown whole.
* ``truncated``: ``{list name: number omitted}``, empty when complete.

Lists an agent answers *from* are budgeted by size (:func:`fit_answer`); lists that
are context around an answer are capped at :data:`LIST_CAP` items. Counts are always
the true totals, and the summary says how complete each answer list is. The rest is
written to ``.pymolt/agent/<command>.json`` (``full_report_path``).

Builders take what the services return and only read; the one thing they write is
that full report. Nothing here runs analysis.
"""

from __future__ import annotations

import difflib
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

#: Cap for lists that are context around an answer stated elsewhere.
LIST_CAP = 20

#: Lists that ARE the answer (which packages change, which calls changed) are
#: budgeted by size, not clipped at a fixed count. Measured on flasgger: 31 compact
#: assess rows cost 453 tokens, while 20 JSON rows cost 643, so a count clip made the
#: payload larger per unit of answer and agents answered from a partial list.
#: ~6000 chars is about 1.5k tokens.
ANSWER_BUDGET_CHARS = 6000

#: Where the complete payload behind a view is written, relative to the project.
AGENT_REPORT_DIR = Path(".pymolt") / "agent"

#: A capture whose program waited at least this long (seconds, in total) for the trace
#: writer says so: every event was kept, but the traced run was slower than an untraced one.
BACKPRESSURE_WARN_SECONDS = 1.0


class AgentInputError(ValueError):
    """An input the caller can fix. Rendered as the failure envelope."""

    def __init__(self, error: str, hint: str | None = None) -> None:
        super().__init__(error)
        self.error = error
        self.hint = hint


# ── envelope and budgets ─────────────────────────────────────────────────────


def ok(summary: str, data: dict, *, full_report_path: str | None = None,
       hint: str | None = None) -> dict:
    return {
        "ok": True,
        "summary": summary,
        "data": data,
        "full_report_path": full_report_path,
        "hint": hint,
    }


def failure(error: str, hint: str | None = None) -> dict:
    """The failure envelope. Mirrors ``cli.output.fail(..., json_output=True)``."""
    payload: dict[str, Any] = {"ok": False, "error": error}
    if hint:
        payload["hint"] = hint
    return payload


def dumps(payload: Any) -> str:
    """How ``--agent`` prints a view: one compact JSON line."""
    return json.dumps(payload, default=str)


def cap_list(items: list[Any], cap: int = LIST_CAP) -> tuple[list[Any], int]:
    """Return (the first ``cap`` items, number omitted)."""
    if len(items) <= cap:
        return list(items), 0
    return list(items[:cap]), len(items) - cap


def fit_answer(rows: list[str], budget: int = ANSWER_BUDGET_CHARS) -> tuple[list[str], int]:
    """Return (the rows that fit in ``budget`` chars, number omitted).

    Use for any list the agent answers *from*; use :func:`cap_list` only for lists
    that are context around an answer stated elsewhere. The first row always fits.
    """
    out: list[str] = []
    used = 0
    for row in rows:
        cost = len(row) + 1
        if out and used + cost > budget:
            return out, len(rows) - len(out)
        out.append(row)
        used += cost
    return out, 0


def truncation_note(shown: int, dropped: int, full_report_path: str | None) -> str:
    """One line stating how complete a list is.

    Silence about truncation is what makes an agent treat a partial list as the
    whole answer.
    """
    if not dropped:
        return f"All {shown} listed."
    where = f" Full list: {full_report_path}" if full_report_path else ""
    return f"Showing {shown} of {shown + dropped}, {dropped} omitted.{where}"


def write_full_report(project_dir: str | Path, name: str, payload: Any) -> str:
    """Write ``payload`` to ``.pymolt/agent/<name>.json`` and return the path."""
    from pymolt.state_io import atomic_write_json

    out_path = Path(project_dir) / AGENT_REPORT_DIR / f"{name}.json"
    atomic_write_json(out_path, payload, indent=2, default=str)
    return str(out_path)


class _Lists:
    """Records what a view cut, so ``complete``/``truncated`` are computed, not kept by hand."""

    def __init__(self) -> None:
        self.truncated: dict[str, int] = {}

    def fit(self, name: str, rows: list[str], budget: int = ANSWER_BUDGET_CHARS) -> list[str]:
        shown, dropped = fit_answer(rows, budget)
        if dropped:
            self.truncated[name] = dropped
        return shown

    def cap(self, name: str, items: list[Any], cap: int = LIST_CAP) -> list[Any]:
        shown, dropped = cap_list(items, cap)
        if dropped:
            self.truncated[name] = dropped
        return shown

    def finish(self, data: dict, warnings: list[str]) -> dict:
        data["warnings"] = self.cap("warnings", [str(w) for w in warnings])
        data["complete"] = not self.truncated
        data["truncated"] = dict(self.truncated)
        return data


def _note(lists: _Lists, name: str, shown: int, full_report_path: str | None) -> str:
    return truncation_note(shown, lists.truncated.get(name, 0), full_report_path)


# ── status ───────────────────────────────────────────────────────────────────


def status_view(report) -> dict:
    """``FunnelStatus`` -> view. Everything fits, so there is no full report."""
    lists = _Lists()
    phases = [
        {"phase": p.phase, "state": p.state, "detail": p.detail, "blocker": p.blocker,
         **({"problem": p.problem} if p.problem else {})}
        for p in report.phases
    ]
    checks = [check.model_dump(mode="json") for check in report.checks]
    done = sum(1 for p in report.phases if p.state == "done")
    lines = [
        f"Funnel status for {Path(report.project_dir).name}: "
        f"{done}/{len(report.phases)} phases done."
    ]
    for state, label in (("stale", "Stale, redo"), ("blocked", "Blocked")):
        names = [p.phase for p in report.phases if p.state == state]
        if names:
            lines.append(f"{label}: {', '.join(names)}.")
    warnings: list[str] = []
    for problem in report.problems:     # a damaged or newer state file: fix it first
        warnings.append(problem["error"])
        lines.append(f"PROBLEM {problem['error']}")
    for c in report.checks:
        if c.state != "ok":
            warnings.append(f"{c.name}: {c.detail}" + (f" ({c.hint})" if c.hint else ""))
            lines.append(f"{c.state.upper()} {warnings[-1]}")
    lines.append(f"Next: {report.next_command}" if report.next_command
                 else f"Next: nothing to run. {report.next_reason}")
    data = {
        "phases": phases,
        "checks": checks,
        "next_command": report.next_command,
        "next_reason": report.next_reason,
        "notes": list(report.notes),
    }
    if report.inconclusive_kind is not None:
        data["inconclusive_kind"] = report.inconclusive_kind
    if report.problems:
        data["problems"] = list(report.problems)
    if report.steps:
        data["steps"] = lists.cap("steps", list(report.steps))
        data["steps_total"] = len(report.steps)
        data["next_step"] = report.next_step
        data["next_brief"] = _next_brief(report)
    return ok("\n".join(lines), lists.finish(data, warnings),
              hint=report.next_reason or None)


def _next_brief(report) -> dict | None:
    """The brief of the step the next command is about (untrusted text under ``data``)."""
    if not report.next_step:
        return None
    from pymolt.migration_plan import MigrationPlan
    from pymolt.workorder.render import brief_data

    plan = MigrationPlan.load(report.project_dir)
    step = plan.step(report.next_step) if plan is not None else None
    return brief_data(plan, step, report.project_dir) if step is not None else None


# ── scan ─────────────────────────────────────────────────────────────────────


def exposure_summary(exposure: dict) -> str | None:
    """One line for ``python_exposure``: what the data says is gone, and how far up it runs."""
    total = exposure["sites_total"]
    guarded = exposure["guarded_total"]
    if not total and not guarded:
        return None
    parts: list[str] = []
    if total:
        by = max(exposure["by_version"], key=lambda v: tuple(int(p) for p in v.split(".")))
        parts.append(
            f"{total} site(s) use stdlib names removed by {by} (data only; `pymolt plan` confirms)"
            f" - runs unchanged up to {exposure['ceiling']}"
        )
    else:
        parts.append("No unguarded use of stdlib names the data says were removed")
    if guarded:
        parts.append(f"{guarded} more guarded by a fallback or version check")
    return "; ".join(parts) + "."


def scan_view(report, project_dir: str | Path) -> dict:
    """``ScanReport`` -> view. The full report is the ``scan --json`` payload."""
    full_path = write_full_report(project_dir, "scan", report.model_dump(mode="json"))
    lists = _Lists()
    surfaces = report.surfaces

    roots: list[dict] = []
    divergences: list[str] = []
    for root in surfaces.project_roots:
        if root.is_divergent:
            divergences.append(f"{root.path}: {', '.join(root.runtime_versions()) or '?'}")
        roots.append({
            "path": root.path,
            "chosen_manifest": root.manifests[0] if root.manifests else None,
            "manifests": list(root.manifests),
            "python_version_evidence": [
                {"source": e.source, "version": e.version, "kind": e.kind}
                for e in root.version_evidence
            ],
            "divergent": root.is_divergent,
        })

    by_group: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    total_edges = 0
    for inv in report.inventory_by_root.values():
        total_edges += len(inv.edges)
        for group, count in inv.counts_by_group().items():
            by_group[group] = by_group.get(group, 0) + count
        for kind, count in inv.counts_by_kind().items():
            by_kind[kind] = by_kind.get(kind, 0) + count

    data: dict[str, Any] = {
        "root_count": len(roots),
        "roots": lists.cap("roots", roots),
        "excluded_roots": lists.cap("excluded_roots", list(surfaces.excluded_roots)),
        "edge_totals": {"total": total_edges, "by_group": by_group, "by_kind": by_kind},
        "divergences": lists.cap("divergences", divergences),
        "contacts": None,
        "python_exposure": None,
    }
    lines = [f"{len(roots)} project root(s), {total_edges} dependency edge(s)."]
    if "roots" in lists.truncated:
        lines.append("Roots: " + _note(lists, "roots", len(data["roots"]), full_path))
    if surfaces.excluded_roots:
        lines.append(f"{len(surfaces.excluded_roots)} manifest directory(ies) deliberately "
                     "excluded (see excluded_roots).")

    warnings = list(surfaces.notes)
    cmap = report.contact_map
    if cmap is not None:
        warnings.extend(cmap.notes)
        per_dep = sorted(((dep, len(t)) for dep, t in cmap.by_dep.items()),
                         key=lambda kv: (-kv[1], kv[0]))
        shown = lists.fit("per_dependency", [f"{dep} {count}" for dep, count in per_dep])
        distinct = len({c.target for c in cmap.contacts})
        data["contacts"] = {
            "total": len(cmap.contacts),
            "distinct_symbols": distinct,
            "dependencies": len(cmap.by_dep),
            "files_scanned": cmap.files_scanned,
            "per_dependency_format": "dep distinct_symbol_count",
            "per_dependency": shown,
        }
        lines.append(
            f"{len(cmap.contacts)} call site(s) into {len(cmap.by_dep)} dependency(ies), "
            f"{distinct} distinct symbol(s), {cmap.files_scanned} file(s) scanned. "
            + _note(lists, "per_dependency", len(shown), full_path)
        )
    exposure = report.python_exposure
    if exposure is not None:
        rows = lists.fit("python_exposure_sites", list(exposure["unguarded_sites"]))
        data["python_exposure"] = {**exposure, "unguarded_sites": rows}
        summary = exposure_summary(exposure)
        if summary:
            lines.append(summary)
            if lists.truncated.get("python_exposure_sites"):
                lines.append("Exposed sites: "
                             + _note(lists, "python_exposure_sites", len(rows), full_path))
    if cmap is not None and cmap.unparsed_cython:
        data["unparsed_cython"] = lists.cap("unparsed_cython", list(cmap.unparsed_cython))
        lines.append(f"{len(cmap.unparsed_cython)} .pyx/.pxd/.pxi file(s) are not parsed; "
                     "names used only inside them are not counted.")
    lines.append(f"{len(divergences)} root(s) with Python-version divergence."
                 if divergences else "No Python-version divergence detected.")
    if by_kind:
        kinds = ", ".join(f"{k}:{v}" for k, v in sorted(by_kind.items()))
        lines.append(f"Edge kinds: {kinds}.")
    if warnings:
        lines.append(f"{len(warnings)} warning(s), see data.warnings.")
    return ok("\n".join(lines), lists.finish(data, warnings), full_report_path=full_path,
              hint="Next: setup (manifest, tool, base and target Python), then assess.")


# ── setup ────────────────────────────────────────────────────────────────────


def setup_choices(options, *, manifest: str | None, tool: str | None,
                  base_python: str | None, target_python: str | None,
                  container_id: str | None = None, target_env: str | None = None):
    """The non-interactive setup request -> ``SetupChoices``, or None to list options.

    No choice at all lists the valid values and saves nothing; all four save; a
    partial or invalid set raises :class:`AgentInputError` naming what is wrong.
    """
    from pymolt.ingestion.config import ToolChoice
    from pymolt.setup.service import SetupChoices, SetupError, check_setup_values

    if not options.manifests:
        raise AgentInputError(
            "no Python dependency manifests detected",
            "Point project_dir at a project with requirements*.txt, pyproject.toml, "
            "Pipfile or environment.yml.",
        )
    given = {"manifest": manifest, "tool": tool, "base_python": base_python,
             "target_python": target_python}
    if not any(given.values()):
        return None
    missing = [name for name, value in given.items() if not value]
    if missing:
        raise AgentInputError(f"Missing: {', '.join(missing)}.",
                              "Run setup with no choices to list the valid values.")
    try:
        check_setup_values(options, manifest=manifest, tool=tool, base_python=base_python,
                           target_python=target_python, container_id=container_id)
    except SetupError as exc:
        raise AgentInputError(f"{exc}.", exc.hint) from exc
    return SetupChoices(
        selected_manifest=manifest,
        selected_tool=ToolChoice(tool),
        base_python=base_python,
        # Echoing the detected default back is not a statement about the project: an
        # assumed base Python stays assumed, so assess keeps capping its tier.
        base_python_source=(options.base_python_source
                            if base_python == options.base_python_default else "declared"),
        target_python=target_python,
        container_id=container_id,
        target_env_created=bool(target_env),
        target_env_path=target_env,
    )


def setup_payload(options, project_dir: str | Path, *, config=None) -> dict:
    """The ``setup --json`` payload: the full options, and the config if one was saved."""
    return {
        "saved": config is not None,
        "config_path": (str(Path(project_dir) / ".pymolt" / "env_config.json")
                        if config is not None else None),
        "config": config.model_dump(mode="json") if config is not None else None,
        "options": options.model_dump(mode="json"),
    }


def setup_view(options, project_dir: str | Path, *, config=None,
               side_effects: Iterable[str] = ()) -> dict:
    """``SetupOptions`` (+ the saved ``EnvConfig``, if any) -> view. ``side_effects``: what
    saving changed besides the config (e.g. an edited ``.gitignore``), said, not hidden."""
    full = setup_payload(options, project_dir, config=config)
    full_path = write_full_report(project_dir, "setup", full)
    lists = _Lists()
    offered = [v.version for v in options.target_versions]
    data: dict[str, Any] = {k: full[k] for k in ("saved", "config_path", "config")}
    data["options"] = None
    if config is not None:
        # The option notes describe defaults ("assuming X"); once explicit choices
        # are saved only what still holds is said (the notes stay in the full report).
        warnings = []
        if config.base_python_source == "assumed":
            warnings.append(f"base Python {config.base_python} is assumed (the interpreter "
                            "pymolt runs on), not declared by the project.")
        if offered and config.target_python not in offered:
            warnings.append(f"Python {config.target_python} is not in the available list; "
                            "assess fails if it cannot be provisioned.")
        warnings.extend(side_effects)
        tool = getattr(config.selected_tool, "value", config.selected_tool)
        summary = (f"Saved {full['config_path']}.\nManifest {config.selected_manifest}, tool "
                   f"{tool}, Python {config.base_python} -> {config.target_python}.")
        hint = "Next: assess (resolves the dependencies for the target Python)."
    else:
        warnings = list(options.notes)
        tools = [t.name for t in options.tools if t.available]
        data["options"] = {
            "manifests": lists.cap("manifests", [m.name for m in options.manifests]),
            "default_manifest": options.default_manifest,
            "tools": tools,
            "default_tool": options.default_tool,
            "containers": lists.cap("containers", [c.id for c in options.containers]),
            "interpreters": lists.cap("interpreters", list(options.interpreters)),
            "base_python_default": options.base_python_default,
            "base_python_source": options.base_python_source,
            "target_python_options": lists.cap("target_python_options", offered),
            "default_target_python": options.default_target,
        }
        summary = (
            f"Nothing saved. Manifests: {', '.join(m.name for m in options.manifests)}.\n"
            f"Tools: {', '.join(tools) or 'none available'} (default {options.default_tool}).\n"
            f"Base Python: {options.base_python_default} ({options.base_python_source}).\n"
            f"Target options: {', '.join(offered) or 'none'}."
        )
        hint = ("Run setup again with manifest, tool, base_python and target_python taken "
                "from these values (CLI: --manifest --tool --base-python --target-python).")
    return ok(summary, lists.finish(data, warnings), full_report_path=full_path, hint=hint)


# ── assess ───────────────────────────────────────────────────────────────────

_ROW_PRIORITY = {"conflict": 0, "unresolved": 1, "removed": 2, "upgrade": 3, "added": 4}
_VERDICT_LEVEL = {"red": "blocked", "yellow": "unproven", "green": "resolves"}


def assess_payload(result, *, baseline_hint=None, target_hint=None) -> dict:
    """The ``assess --json`` payload, built from an ``AssessResult``."""
    payload: dict[str, Any] = {
        "project_dir": result.project_dir,
        "source_manifest": result.source_manifest,
        "source_fixation": result.source_fixation,
        "resolution_quality": result.resolution_quality,
        "detected_python": result.detected_python,
        "target_python": result.target_python,
        "target_resolved": result.target_resolved,
        "target_error": result.target_error,
        "target_error_detail": result.target_error_detail,
        "target_culprits": list(result.target_culprits),
        "baseline_unresolved": result.baseline_unresolved,
        "baseline_tier": result.baseline_tier,
        "target_manifest_path": result.target_manifest_path,
        "warnings": list(result.warnings),
        "manual_zone": list(result.manual_zone),
        "packages": list(result.rows),
    }
    if baseline_hint is not None:
        payload["baseline_hint"] = baseline_hint.model_dump(mode="json")
    if target_hint is not None:
        payload["target_hint"] = target_hint.model_dump(mode="json")
    if result.risk is not None:
        payload["risk"] = result.risk.model_dump(mode="json")
    return payload


def assess_hints(project_dir: str | Path, result) -> dict:
    """How to build the environment assess could not resolve in, and the target one."""
    baseline_hint = target_hint = None
    if result.baseline_unresolved and result.detected_python:
        from pymolt.setup.baseline_hint import build_baseline_hint

        baseline_hint = build_baseline_hint(
            project_dir, result.detected_python,
            reason=result.warnings[0] if result.warnings else None,
        )
    if result.target_python and result.target_resolved:
        from pymolt.setup.target_hint import build_target_hint

        target_hint = build_target_hint(project_dir, result.target_python,
                                        manifest=result.target_manifest_path)
    return {"baseline_hint": baseline_hint, "target_hint": target_hint}


def assess_view(result, project_dir: str | Path, *, risk_requested: bool = False,
                baseline_hint=None, target_hint=None) -> dict:
    """``AssessResult`` -> view. The verdict wording is the service's own."""
    from pymolt.assess.service import BaselineTier, compatibility_verdict

    full_path = write_full_report(project_dir, "assess", assess_payload(
        result, baseline_hint=baseline_hint, target_hint=target_hint))
    lists = _Lists()
    warnings = list(result.warnings)

    risk_by_pkg: dict[str, str] = {}
    risk_data = None
    if result.risk is not None:
        tiers: dict[str, int] = {}
        for pkg in result.risk.packages:
            tier = getattr(pkg.tier, "value", str(pkg.tier))
            risk_by_pkg[pkg.name] = tier
            tiers[tier] = tiers.get(tier, 0) + 1
        risk_data = {"assessed": result.risk.assessed, "skipped": result.risk.skipped,
                     "errors": result.risk.errors, "by_tier": tiers}
        if result.risk.errors:
            warnings.append(f"risk: {result.risk.errors} package(s) unreachable; "
                            "their risk is unknown, not low.")
    elif risk_requested:
        warnings.append("risk was requested but not assessed: there is no resolved base "
                        "to compare against.")

    def row(r: dict) -> str:
        line = (f"{r['name']} {r['baseline_version'] or '-'}>{r['target_version'] or '-'} "
                f"{r['status']}")
        return line + (f" risk={risk_by_pkg[r['name']]}" if r["name"] in risk_by_pkg else "")

    moved = sorted((r for r in result.rows if r["status"] not in ("unchanged", "baseline")),
                   key=lambda r: (_ROW_PRIORITY.get(r["status"], 9), r["name"]))
    changes = lists.fit("changes", [row(r) for r in moved])
    counts = result.counts_by_status()
    manual_zone = [str(item) for item in result.manual_zone]

    verdict = None
    found = compatibility_verdict(result.target_python, result.target_error,
                                  BaselineTier(result.baseline_tier))
    if found is not None:
        title, body, style = found
        verdict = {"title": re.sub(r"^\W+", "", title), "detail": body,
                   "level": _VERDICT_LEVEL.get(style, style)}

    data: dict[str, Any] = {
        "target_python": result.target_python,
        "verdict": verdict,
        "baseline_tier": result.baseline_tier,
        "baseline_unresolved": result.baseline_unresolved,
        "source_manifest": result.source_manifest,
        "source_fixation": result.source_fixation,
        "resolution_quality": result.resolution_quality,
        "target_resolved": result.target_resolved,
        "target_error": result.target_error,
        "target_error_detail": result.target_error_detail,
        "target_culprits": list(result.target_culprits),
        "changes_format": "name base>target status" + (" risk=tier" if risk_by_pkg else ""),
        "changes": changes,
        "changes_total": len(moved),
        "unchanged": counts.get("unchanged", 0),
        "status_counts": counts,
        "manual_zone": lists.cap("manual_zone", manual_zone),
        "manual_zone_count": len(manual_zone),
        "risk": risk_data,
        "target_manifest_path": result.target_manifest_path,
        "target_env": None,
        "baseline_env": None,
    }
    if target_hint is not None:
        data["target_env"] = {"suggested_uv_commands": target_hint.suggested_uv_commands,
                              "suggested_dockerfile": target_hint.suggested_dockerfile,
                              "notes": target_hint.notes}
    if baseline_hint is not None:
        data["baseline_env"] = {"base_python": baseline_hint.base_python,
                                "reason": baseline_hint.reason,
                                "suggested_commands": baseline_hint.suggested_commands,
                                "rerun_command": baseline_hint.rerun_command,
                                "notes": baseline_hint.notes}

    lines = [verdict["title"] if verdict else "No target Python: nothing was compared."]
    lines.append(
        f"Base tier: {result.baseline_tier}. Upgrades: {counts.get('upgrade', 0)}, "
        f"added: {counts.get('added', 0)}, removed: {counts.get('removed', 0)}, "
        f"conflicts: {counts.get('conflict', 0)}, "
        + (f"unresolved: {counts['unresolved']}, " if counts.get("unresolved") else "")
        + f"manual-zone: {len(manual_zone)}."
    )
    lines.append(f"{len(moved)} package(s) differ, {counts.get('unchanged', 0)} unchanged. "
                 + _note(lists, "changes", len(changes), full_path))
    if result.baseline_unresolved:
        lines.append("No base was resolved: every row is target-only ('added' means not "
                     "compared). See data.baseline_env.")
    if result.target_manifest_path:
        lines.append(f"Pinned target manifest written: {result.target_manifest_path}")
    if warnings:
        lines.append(f"{len(warnings)} warning(s), see data.warnings.")
    lines.append("A resolve is not evidence the code still works: capture and verify decide.")

    if result.target_error:
        culprits = ", ".join(result.target_culprits) or "none named by the resolver"
        hint = (f"The target does not resolve: {result.target_error}. Culprit(s): {culprits}. "
                "Fix or loosen them, then assess again.")
    elif result.baseline_unresolved:
        hint = "Build the base environment (data.baseline_env), then assess again inside it."
    else:
        hint = "Next: capture the baseline before any code edit, then plan."
    return ok("\n".join(lines), lists.finish(data, warnings), full_report_path=full_path,
              hint=hint)


# ── capture ──────────────────────────────────────────────────────────────────


def capture_refusal_hint(slot, static_contacts: int | None) -> str:
    """The hint for a capture that was not accepted, by what went wrong. A failed run names
    the failing tests the command log shows, and the log itself."""
    from pymolt.verify.service import failed_tests, parse_failed_count

    rc = slot.returncode
    log = f" (see {slot.command_log})" if slot.command_log else ""
    if rc not in (None, 0) and slot.events > 0:
        failed, _ = parse_failed_count(slot.command_log)
        names, total = failed_tests(slot.command_log)
        which = ""
        if names:
            more = f" and {total - len(names)} more" if total > len(names) else ""
            which = f": {', '.join(names)}{more}"
        return (f"{failed if failed is not None else 'Some'} test(s) failed (exit {rc}){which}"
                f"{log}. Fix or deselect them, or keep a red baseline with --accept-failing.")
    if rc not in (None, 0):
        return (f"The command failed (exit {rc}) and recorded no dependency call"
                f"{log}. Fix the command and capture again; the slot is unchanged.")
    if slot.events == 0:
        contacts = f" ({static_contacts} static contacts)" if static_contacts is not None else ""
        return (f"The command ran (exit {rc if rc is not None else 0}) but called no recorded "
                f"dependency{contacts}. Run a command that exercises them, or pass --target.")
    return (f"The recording is not usable: {slot.validity_reason or 'invalid trace'}{log}. "
            "Fix the command and capture again; the slot is unchanged.")


def capture_view(slot, *, when: str, state, static_contacts: int | None = None,
                 project_dir: str | Path | None = None) -> dict:
    """A capture's ``ContractSlot`` (+ the contract state after it) -> view.

    ``when`` is the CLI word: ``baseline`` or ``post-migration``. A capture that is
    not accepted as evidence is ``ok: false`` with ``accepted: false``: the command
    ran, but what it recorded cannot stand for that side, so no one should read "ok"
    as "evidence recorded". The hint follows the cause.
    """
    from pymolt.verify.models import CaptureValidity

    lists = _Lists()
    accepted = slot.validity is CaptureValidity.VALID
    filled = [name for name in ("baseline", "post_migration") if getattr(state, name) is not None]
    warnings: list[str] = []
    for item in slot.activation_failures or []:
        warnings.append(f"the watcher failed to activate in pid {item.get('pid')} (python "
                        f"{item.get('python')}): {item.get('error')}. That process ran "
                        "UNTRACED, so this capture cannot support a PASS.")
    for label, value in (("dropped event(s)", slot.dropped_events),
                         ("trace write failure(s)", slot.write_failures),
                         ("sink failure(s)", slot.sink_failures)):
        if value:
            warnings.append(f"{value} {label}: the recording is incomplete.")
    waited = getattr(slot, "backpressure_wait_seconds", None)
    if waited is not None and waited >= BACKPRESSURE_WARN_SECONDS:
        warnings.append(f"the program waited {waited:.1f} s in total for the trace writer to "
                        "catch up: no event was lost to it, but timing-sensitive code ran "
                        "that much slower than it would untraced.")
    if slot.instrumentation_skipped:
        warnings.append(f"{len(slot.instrumentation_skipped)} instrumentation target(s) skipped.")

    validity = getattr(slot.validity, "value", str(slot.validity))
    data = {
        "when": when,
        "accepted": accepted,
        "validity": validity,
        "validity_reason": slot.validity_reason,
        "capture_id": slot.capture_id,
        "events": slot.events,
        "processes": slot.processes,
        "returncode": slot.returncode,
        "trace_path": slot.trace_path,
        "command_log": slot.command_log,
        "filled_slots": filled,
        "env_note": slot.env_note,
        "duration_seconds": slot.duration_seconds,
        "dropped_events": slot.dropped_events,
        "archived_previous": slot.archived_previous,
        "impact_targets": lists.cap("impact_targets", list(slot.impact_targets)),
        "deployment_id": slot.deployment_id,
        "request_id": slot.request_id,
        "correlation_id": slot.correlation_id,
        "metadata_path": slot.metadata_path,
        "red": getattr(slot, "red", None),
    }
    if accepted:
        lines = [f"Recorded {slot.events} call(s) in {slot.processes} process(es) as the "
                 f"'{when}' capture.", f"Filled: {', '.join(filled) or 'none'}."]
        if slot.env_note:
            lines.append(f"Env: {slot.env_note}")
        red = getattr(slot, "red", None)
        if red:
            failed = red.get("failed")
            lines.append(f"RED: the command exited {red.get('returncode')} "
                         f"({failed if failed is not None else 'some'} test(s) failed); kept "
                         "because --accept-failing was given. The calls from failing tests are "
                         "compared as usual.")
        hint = ("Next: change the code (or apply a plan), then capture post-migration."
                if "post_migration" not in filled else
                "Next: verify (compares baseline and post-migration).")
    else:
        lines = [f"NOT accepted as '{when}' evidence: {slot.validity_reason or validity}.",
                 f"Diagnostic artifact: {slot.trace_path}"]
        if static_contacts is None and project_dir is not None and slot.events == 0:
            from pymolt.verify.service import static_contact_count

            static_contacts = static_contact_count(project_dir)
        hint = capture_refusal_hint(slot, static_contacts)
    if warnings:
        lines.append(f"{len(warnings)} warning(s), see data.warnings.")
    view = ok("\n".join(lines), lists.finish(data, warnings),
              full_report_path=slot.trace_path, hint=hint)
    view["ok"] = accepted
    return view


def capture_attach_view(instructions, *, when: str, collect_command: str) -> dict:
    """``--mode attach``: what to run, and how to finish. Nothing is recorded yet."""
    data = {"when": when, "attach_command": instructions.command_hint,
            "collect_command": collect_command}
    return ok(f"Attach capture for '{when}' started; nothing recorded yet.\n"
              f"Run: {instructions.command_hint}",
              _Lists().finish(data, []), hint=f"Then finish it: {collect_command}")


# ── plan ─────────────────────────────────────────────────────────────────────


def inconclusive_deltas(risks) -> tuple[int, int]:
    """``(K, T)``: the plan's API deltas that were inconclusive, and all of them."""
    deltas = [d for d in (getattr(risks, "deltas", None) or ())
              if getattr(d, "subject", "dependency") == "dependency"]
    return sum(1 for d in deltas if getattr(d, "quality", None) == "inconclusive"), len(deltas)


def unknown_api_phrase(inconclusive: int, total: int) -> str:
    return f"API changes unknown for {inconclusive} of {total} package(s)"


def step_row(step) -> str:
    """One step as a line: id, type, who, claim, effort, title, dependencies."""
    from pymolt.workorder.render import claim_label, clean

    deps = f" after {', '.join(step.depends_on)}" if step.depends_on else ""
    claim = claim_label(step) if step.claim else "no claim"
    return (f"{step.id} {step.type} [{step.executor}, {claim}, effort "
            f"{step.effort.size}] {clean(' '.join(step.title.split()), 160)}{deps}")


def steps_summary(plan) -> str:
    """"N steps: a by pymolt, b for an agent, c for a person"."""
    counts = {who: sum(1 for s in plan.steps if s.executor == who)
              for who in ("pymolt", "agent", "person")}
    label = {"pymolt": "by pymolt", "agent": "for an agent", "person": "for a person"}
    parts = ", ".join(f"{n} {label[who]}" for who, n in counts.items() if n)
    return f"{len(plan.steps)} step(s)" + (f": {parts}" if parts else "")


def _next_for(project_dir, plan) -> dict | None:
    """``{command, step, brief}``: what `status` names next, with the step's brief when an agent
    or a person does it (its text from dependencies and the project is under ``data``)."""
    if project_dir is None:
        return None
    from pymolt.status import build_status
    from pymolt.workorder.render import brief_data

    try:
        status = build_status(project_dir)
    except Exception:  # noqa: BLE001 - the view stands without the next command
        return None
    step = plan.step(status.next_step) if status.next_step else None
    brief = brief_data(plan, step, project_dir) if step is not None \
        and step.executor != "pymolt" else None
    return {"command": status.next_command, "step": status.next_step,
            "reason": status.next_reason, "brief": brief}


def apply_view(result, plan, project_dir=None) -> dict:
    """An ``apply`` invocation (``workorder.apply.ApplyResult``) -> view (design 3.6):
    ``applied`` (``rebased``: re-based onto another text), ``noop``, ``present``, ``refused``
    (none: a refused invocation is a failure), ``checks`` per step, ``remaining`` by status,
    and ``next``: the next command, its step and that step's brief. A dry run adds the diff it
    would write; a record its files and the changes it left unrecorded."""
    lists = _Lists()
    titles = {s.id: s.title for s in plan.steps}
    done = {"applied": "applied", "already_applied": "already in effect", "nothing":
            "nothing applied", "present": "already present by hand", "dry_run": "dry run",
            "skipped": "skipped", "in_progress": "record open", "recorded": "recorded",
            "aborted": "record aborted"}[result.outcome]
    summary = f"{done}: " + (", ".join(result.applied) or ", ".join(result.recorded)
                             or ", ".join(result.in_progress) or ", ".join(result.noop)
                             or ", ".join(result.present) or ", ".join(result.skipped)
                             or "no step")
    remaining = {status: lists.cap(f"remaining.{status}", ids)
                 for status, ids in result.remaining.items()}
    data = {
        "outcome": result.outcome, "plan_id": result.plan_id, "run_id": result.run_id,
        "applied": [{"step": sid, "title": titles.get(sid), "rebased": sid in result.rebased,
                     "wrote": sid not in result.unwritten} for sid in result.applied],
        "rebased": list(result.rebased), "noop": list(result.noop),
        "present": list(result.present), "skipped": list(result.skipped),
        "blocked": dict(result.blocked), "checks": dict(result.checks),
        "refused": {}, "next": _next_for(project_dir, plan),
        "recorded": list(result.recorded), "in_progress": list(result.in_progress),
        "unrecorded": lists.cap("unrecorded", list(result.unrecorded)),
        "attestation": result.attestation, "check_failed": result.check_failed,
        "remaining": remaining, "files": lists.cap("files", list(result.files)),
        "diff": [{"file": d["file"], "diff": d["diff"][:ANSWER_BUDGET_CHARS]}
                 for d in lists.cap("diff", list(result.diff))],
    }
    return ok(summary, lists.finish(data, list(result.notes)))


def rollback_view(result) -> dict:
    """A ``rollback`` (``workorder.rollback.RollbackResult``) -> view: the steps it rolled back
    (``kept``: in effect still, they changed no file), the files restored, and what became of
    the post-migration capture."""
    summary = (f"rolled back {', '.join(result.steps) or 'no step'} of run {result.run_id[:8]}"
               f"; {len(result.files_restored)} file(s) restored")
    data = {"run_id": result.run_id, "plan_id": result.plan.plan_id,
            "rolled_back_at": result.rolled_back_at, "steps": list(result.steps),
            "kept": list(result.kept), "files_restored": list(result.files_restored),
            "post_migration_capture": result.post, "post_migration_trace": result.post_trace}
    return ok(summary, _Lists().finish(data, list(result.notes)))


def plan_view(plan, by_pkg: dict, source_desc: str, *, plan_path: str | Path,
              plan_md: str | Path | None = None, project_dir: str | Path | None = None) -> dict:
    """A saved ``MigrationPlan`` -> view. The full report is the plan file itself.
    ``plan_md``: where PLAN.md was written; ``project_dir``: to resolve the next brief's lines."""
    from pymolt.codemods.models import CodemodPattern
    from pymolt.workorder.render import brief_data, next_agent_step, next_command

    full_path = str(plan_path)
    lists = _Lists()
    diffs = [json.dumps({"path": change.path, "diff": "".join(difflib.unified_diff(
        change.old_source.splitlines(keepends=True),
        change.new_source.splitlines(keepends=True),
        fromfile=f"a/{change.path}", tofile=f"b/{change.path}",
    ))}) for change in plan.changes]
    files = lists.fit("files_to_change", sorted(change.path for change in plan.changes))
    advisories = [
        f"{a.path}:{a.line} [{'caveat' if a.severity == 'precondition' else 'not applied'}] "
        f"{a.reason}" for a in plan.advisories
    ]
    manual = [
        (f"{m.path}:{','.join(map(str, m.lines))}" if m.path and m.lines else m.path or "no site")
        + f" [blocked rule] {m.rule}: {m.reason}" for m in getattr(plan, "manual", None) or ()
    ]
    impacts = [
        f"{i.package} {i.path} {i.kind} risk={i.risk}"
        + (f" -> {i.replacement_path}" if i.replacement_path else "")
        for i in plan.impacts
    ]
    per_package = []
    for pkg, items in by_pkg.items():
        patterns = sum(1 for item in items if isinstance(item, CodemodPattern))
        per_package.append(f"{pkg} rules={len(items) - patterns} patterns={patterns}")
    sites = sum(change.sites for change in plan.changes)
    inconclusive, delta_total = inconclusive_deltas(getattr(plan, "risks", None))
    risks_report = getattr(plan, "risks", None)
    # With no baseline the report may hold python risks only: the dependency line is then
    # not printed (it would read "0 need attention" for risks nobody predicted).
    dependency_predicted = risks_report is not None and (
        plan.baseline_trace_path is not None or delta_total > 0
        or any(getattr(r, "scope", "dependency") == "dependency" for r in risks_report.risks))

    data: dict[str, Any] = {
        "plan_id": plan.plan_id,
        "plan_path": full_path,
        "source": source_desc,
        "dependencies": lists.cap("dependencies", [
            f"{d.name} {d.from_version}>{d.to_version}" for d in plan.dependencies]),
        "per_package": lists.cap("per_package", per_package),
        "files_to_change": files,
        "files_total": len(plan.changes),
        "rewrite_sites": sites,
        "diffs": [json.loads(d) for d in lists.fit("diffs", diffs, ANSWER_BUDGET_CHARS * 2)],
        "advisories": lists.fit("advisories", advisories),
        "advisories_total": len(advisories),
        "manual": lists.fit("manual", manual),
        "manual_total": len(manual),
        "api_impacts": lists.fit("api_impacts", impacts),
        "api_impacts_total": len(impacts),
        "baseline_bound": plan.baseline_trace_path is not None,
        "api_deltas_inconclusive": inconclusive,
        "api_deltas_total": delta_total,
        **_risk_data(lists, risks_report if dependency_predicted else None),
        "python": _python_data(lists, plan),
        "compile_findings": lists.cap("compile_findings",
                                      list(getattr(plan, "compile_findings", None) or [])),
        "compile_findings_total": len(getattr(plan, "compile_findings", None) or []),
        "steps": lists.fit("steps", [step_row(s) for s in plan.steps]),
        "steps_total": len(plan.steps),
        "steps_by_executor": {who: sum(1 for s in plan.steps if s.executor == who)
                              for who in ("pymolt", "agent", "person")},
        "plan_md": str(plan_md) if plan_md else None,
    }
    agent_step = next_agent_step(plan)
    data["next"] = {
        "command": next_command(plan),
        "step": agent_step.id if agent_step else None,
        "brief": brief_data(plan, agent_step, project_dir) if agent_step else None,
    }
    warnings: list[str] = []
    if plan.baseline_trace_path is None:
        warnings.append("No baseline capture is bound to this plan: apply refuses it without "
                        "--allow-no-baseline and verify could only be INCONCLUSIVE. Capture "
                        "the baseline, then plan again.")
    lines = [
        f"Plan {plan.plan_id[:8]} saved: {len(plan.changes)} file(s) to change, {sites} "
        f"site(s), {len(plan.dependencies)} dependency move(s), from {source_desc}.",
        "Files: " + _note(lists, "files_to_change", len(files), full_path),
    ]
    if advisories:
        lines.append(f"{len(advisories)} site(s) need manual review (data.advisories).")
    if manual:
        lines.append(f"{len(manual)} rule match(es) are not applied and need a person "
                     "(data.manual).")
    if impacts:
        lines.append(f"{len(impacts)} API impact(s) (data.api_impacts).")
    python_line = python_summary_line(plan)
    if python_line:
        lines.append(python_line)
    if data["risks_status"] is not None:
        attention = data["risks_attention_total"]
        fixed = data["risks_fixed_total"]
        unknown = unknown_api_phrase(inconclusive, delta_total)
        # Next to a python line these counts are the dependencies' only: say so, or
        # "0 need attention" reads as contradicting "8 certain" python risks.
        label = "Dependency risks" if python_line else "Predicted risks"
        if inconclusive and inconclusive == delta_total:
            lines.append(f"{label}: none rated — {unknown} (inconclusive)")
        elif inconclusive:
            lines.append(f"{label}: {attention} need attention, {fixed} fixed by the "
                         f"plan; {unknown} (inconclusive)")
        else:
            lines.append(f"{label}: {attention} need attention, {fixed} fixed by the "
                         f"plan (data.risks_attention, data.risks_fixed; evidence "
                         f"{data['risks_status']}).")
    if by_pkg and not plan.changes:
        unknown_tail = (f", but API changes are unknown for {inconclusive} of "
                        f"{delta_total} package(s)" if inconclusive else "")
        lines.append(f"No call sites matched: none of the returned recipes apply in the "
                     f"{plan.files_scanned} file(s) scanned. The plan changes no file"
                     f"{unknown_tail}.")
    if "diffs" in lists.truncated:
        lines.append(f"{lists.truncated['diffs']} diff(s) omitted; the plan file has every change.")
    if plan.steps:
        lines.append(f"{steps_summary(plan)} (data.steps"
                     + (f"; PLAN.md: {plan_md}" if plan_md else "") + ")."
                     + (f" The first for an agent is {agent_step.id}; its brief is "
                        "data.next.brief." if agent_step else ""))
    if warnings:
        lines.append(f"{len(warnings)} warning(s), see data.warnings.")
    return ok("\n".join(lines), lists.finish(data, warnings), full_report_path=full_path,
              hint="Review the diffs; a person then runs `pymolt apply` in the project "
                   "(it refuses the plan if a planned file changed since).")


def _risk_label(risk) -> str:
    fixed = " [fixed by the plan]" if risk.fixed_by_plan else ""
    return f"{risk.tier.value} {risk.site.label()} {risk.symbol}: {risk.reason}{fixed}"


def _python_risks(plan) -> list:
    report = getattr(plan, "risks", None)
    return [r for r in (report.risks if report is not None else ())
            if getattr(r, "scope", "dependency") == "python"]


def _manual_hint(risk) -> str:
    """The curated hint for a manual (advisory) python risk, "" when there is none."""
    for fact in (risk.fact, *(risk.facts or ())):
        for item in (fact or {}).get("evidence") or ():
            if isinstance(item, str) and item.startswith("hint: "):
                return item[6:].strip()
    return ""


def _manual_label(risk) -> str:
    hint = _manual_hint(risk)
    hint = hint if len(hint) <= 70 else hint[:67] + "..."
    return f"{risk.symbol} (manual{': ' + hint if hint else ''})"


def python_summary_line(plan) -> str | None:
    """"Python 3.6→3.12: N risk(s) (k certain); 0 fixed by the plan; needs attention: ..."."""
    python = getattr(plan, "python", None)
    if not python:
        return None
    risks = _python_risks(plan)
    head = f"Python {python.get('from')}→{python.get('to')}"
    quality = python.get("quality")
    certain = sum(1 for r in risks if r.tier.value == "certain")
    fixed = [r for r in risks if r.fixed_by_plan]
    attention = [r for r in risks if not r.fixed_by_plan]
    line = f"{head}: {len(risks)} risk(s) ({certain} certain); {len(fixed)} fixed by the plan"
    if attention:
        names: list[str] = []
        for r in attention:
            label = _manual_label(r) if r.fix == "advisory" else r.symbol
            if label not in names:
                names.append(label)
        shown = ", ".join(names[:5]) + (f", +{len(names) - 5} more" if len(names) > 5 else "")
        line += f"; needs attention: {shown}"
    elif quality != "complete":
        line += "; none rated, the evidence is incomplete"
    report = getattr(plan, "risks", None)
    guarded = len(getattr(report, "guarded", None) or ()) if report is not None else 0
    pre = len(getattr(report, "pre_existing", None) or ()) if report is not None else 0
    if guarded or pre:
        line += f"; listed, not rated: {guarded} guarded, {pre} pre-existing"
    if quality != "complete":
        line += f" (evidence {quality}: see data.python.notes)"
    return line


def _python_data(lists: _Lists, plan) -> dict[str, Any] | None:
    python = getattr(plan, "python", None)
    if not python:
        return None
    risks = _python_risks(plan)
    report = getattr(plan, "risks", None)
    ordered = sorted(risks, key=lambda r: (r.fixed_by_plan, _tier_rank(r), r.site.label()))
    return {
        "from": python.get("from"), "to": python.get("to"), "quality": python.get("quality"),
        "sides": python.get("sides") or {}, "notes": lists.cap("python.notes",
                                                               list(python.get("notes") or [])),
        "risks_total": len(risks),
        "certain": sum(1 for r in risks if r.tier.value == "certain"),
        "fixed_by_plan": sum(1 for r in risks if r.fixed_by_plan),
        "risks": lists.fit("python.risks", [_risk_label(r) + (f" [{r.fix}]" if r.fix else "")
                                            for r in ordered]),
        "manual": lists.fit("python.manual", [
            f"{r.site.label()} {_manual_label(r)}" for r in ordered
            if not r.fixed_by_plan and r.fix == "advisory"]),
        "guarded": lists.fit("python.guarded", [
            f"{g.kind} {g.site.label()} {g.symbol}: {g.reason}"
            for g in (getattr(report, "guarded", None) or ())]),
        "guarded_total": len(getattr(report, "guarded", None) or ()),
        "pre_existing_total": len(getattr(report, "pre_existing", None) or ()),
    }


def _tier_rank(risk) -> int:
    from pymolt.verify.predict import TIER_ORDER

    return TIER_ORDER[risk.tier]


def dependency_risks(report):
    """The plan's risk report without its python-scope risks (None stays None)."""
    if report is None:
        return None
    from pymolt.verify.predict import PredictionReport

    return PredictionReport(status=report.status, deltas=report.deltas, notes=report.notes,
                            risks=[r for r in report.risks
                                   if getattr(r, "scope", "dependency") == "dependency"])


def _risk_data(lists: _Lists, report) -> dict[str, Any]:
    """A plan's predicted dependency risks, most severe first, split by whether the plan
    fixes them (python risks are reported under ``python``)."""
    report = dependency_risks(report)
    if report is None:
        return {"risks_status": None, "risks_attention": [], "risks_attention_total": 0,
                "risks_fixed": [], "risks_fixed_total": 0}
    from pymolt.verify.predict import TIER_ORDER

    ordered = sorted(report.risks, key=lambda r: (TIER_ORDER[r.tier], r.site.label()))
    attention = [_risk_label(r) for r in ordered if not r.fixed_by_plan]
    fixed = [_risk_label(r) for r in ordered if r.fixed_by_plan]
    return {
        "risks_status": report.status,
        "risks_attention": lists.fit("risks_attention", attention),
        "risks_attention_total": len(attention),
        "risks_fixed": lists.fit("risks_fixed", fixed),
        "risks_fixed_total": len(fixed),
    }


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{round(value * 100)}%"


def coverage_lines(rep) -> list[str]:
    """The coverage and recall lines, in the words ``pymolt verify`` prints them: coverage =
    contacts that ran over observable contacts, recall = recorded over those that ran, how
    "ran" was known, the BLIND symbols split by why, and the unobservable contacts."""
    reached = rep.contacts_recorded + rep.contacts_ran_not_recorded
    observable = reached + rep.contacts_not_run
    granularity = rep.coverage_granularity or "n/a"
    counts = rep.granularity_counts or {}
    if len(counts) > 1:
        granularity += " (" + ", ".join(f"{n} by {g}" for g, n in counts.items()) + ")"
    lines = [f"Coverage: {_pct(rep.coverage)} ({reached} of {observable} contacts ran; "
             f"granularity {granularity})   Recall: {_pct(rep.recall)} "
             f"({rep.contacts_recorded} of {reached} recorded)"]
    if rep.blind:
        lines.append(f"BLIND symbols: not run {rep.blind_not_run}, ran but not recorded "
                     f"{rep.blind_ran_not_recorded}, unobservable {rep.blind_unobservable}")
    if rep.contacts_unobservable:
        why = ", ".join(f"{k} {v}" for k, v in rep.unobservable_by_reason.items())
        lines.append(f"Unobservable contacts: {rep.contacts_unobservable} ({why}), left out of "
                     "coverage and recall")
    return lines


def impact_focus(rep) -> dict | None:
    """After ``apply``: how many API paths the plan found changed, and the static and
    dynamic-only symbols on them. The headline counts and the verdict stay the whole surface;
    this is what to look at first. None without an applied plan."""
    if not getattr(rep, "impact_filter_active", False):
        return None
    coverage = getattr(rep, "impacted_coverage", None)
    return {"paths": len(rep.impact_paths), "static_targets": rep.impacted_static_targets,
            "confirmed": rep.impacted_confirmed, "blind": rep.impacted_blind,
            "dynamic_only": rep.impacted_dynamic_only,
            "coverage_pct": None if coverage is None else round(coverage * 100)}


def _site_text(site: dict | None) -> str:
    if not site:
        return "?"
    return f"{site.get('file', '?')}:{site.get('line', '?')}"


def _calibration_data(lists: _Lists, calibration) -> dict[str, Any] | None:
    """Predicted vs observed, as counts plus the hits and the unpredicted changes."""
    if calibration is None:
        return None
    hits = [f"{r.tier.value} {r.site.label()} {r.symbol}" for r in calibration.risks
            if r.result.value == "hit"]
    unpredicted = [f"{o.category} {o.site.label()} {o.symbol}" for o in calibration.unpredicted]
    return {
        "source": calibration.source,
        "predicted": calibration.predicted,
        "hit": calibration.hit,
        "prevented": calibration.prevented,
        "miss": calibration.miss,
        "not_exercised": calibration.not_exercised,
        "hits": lists.fit("calibration.hits", hits),
        "unpredicted": lists.fit("calibration.unpredicted", unpredicted),
        "unpredicted_total": len(unpredicted),
    }


# ── verify ───────────────────────────────────────────────────────────────────

_CHANGE_CATEGORIES = ("changed", "disappeared", "appeared", "uncomparable", "rewritten")


def _findings_data(summary: dict) -> dict:
    """The fact diff's findings for an agent: failing findings (why each is not noise),
    review reasons with the one action that settles each, and the explained mechanisms."""
    def compact(f: dict) -> dict:
        return {k: f[k] for k in ("id", "symbol", "site", "entries", "change", "why",
                                  "explained", "accepted")
                if f.get(k) is not None}
    return {
        "ledger": summary.get("ledger"),
        "failing": [compact(f) for f in (summary.get("failing") or [])[:20]],
        "review": [{"reason": r["reason"], "entries": r["entries"], "action": r["action"],
                    "sites": [compact(f) for f in r["findings"][:5]]}
                   for r in summary.get("review") or []],
        "explained": [{"mechanism": t["mechanism"], "entries": t["entries"]}
                      for t in summary.get("explained") or []],
        "accepted": [compact(f) for f in (summary.get("accepted") or [])[:20]],
        "quality": summary.get("quality"),
    }


def inconclusive_line(unified) -> str | None:
    """One line for an INCONCLUSIVE verdict: whether it needs a person or evidence (its
    ``inconclusive_kind``) and what settles the first such reason. None otherwise."""
    kind = getattr(unified, "inconclusive_kind", None)
    if kind is None:
        return None
    causes = list(getattr(unified, "inconclusive_causes", None) or ())
    settle = next((c.settle for c in causes if c.kind == kind), None)
    line = ("Needs a person" if kind.value == "needs_review" else "Needs evidence") \
        + (f": {settle}." if settle else ".")
    if kind.value == "needs_review":
        more = sum(1 for c in causes if c.kind.value == "needs_evidence")
        if more:
            line += f" It also needs evidence ({more} reason(s))."
    return line


def compared_captures_line(compared) -> str | None:
    """``baseline <id> → post-migration <id>``: the captures a report compared, by id (a side
    that is no capture, or whose id is unknown, is left out). None when neither side is."""
    if compared is None:
        return None
    sides = [f"{name} {ident}" for name, ident in (
        (compared.before_capture, compared.before_capture_id),
        (compared.after_capture, compared.after_capture_id)) if name and ident]
    return " → ".join(sides) or None


def saved_verdict_note(compared, saved_path) -> str | None:
    """What saving a verdict meant, when it is not the usual verdict on the two captures.

    ``compared`` is the report's ``ComparedRecordings``; ``saved_path`` None means it was not
    saved (its baseline side was another recording)."""
    if saved_path is None:
        return "Not saved: the baseline side is a recording other than the baseline capture."
    if compared is not None and compared.after is not None and compared.after_capture is None:
        return (f"Saved, but `pymolt status` does not count it as the migration's verdict: the "
                f"post-migration side is {compared.after}, not the post-migration capture.")
    return None


def verify_view(unified, project_dir: str | Path, *, saved_path: str | Path | None) -> dict:
    """``UnifiedVerificationReport`` -> view.

    ``saved_path`` is where the verdict was saved (None: its baseline side was a recording
    other than the baseline capture, so nothing was saved and the full report is written
    instead). ``data.compared_recordings`` names the two recordings and which capture each
    one is (``data.compared`` says whether anything was compared at all).
    """
    rep = unified.contract
    full_path = (str(saved_path) if saved_path is not None
                 else write_full_report(project_dir, "verify", unified.model_dump(mode="json")))
    lists = _Lists()
    verdict = unified.verdict.value
    data: dict[str, Any] = {
        "verdict": verdict,
        "reasons": list(unified.reasons),
        # INCONCLUSIVE only: needs_review (a person must look) or needs_evidence (more or
        # better captures); each reason with its kind and what settles it.
        "inconclusive_kind": (unified.inconclusive_kind.value
                              if unified.inconclusive_kind is not None else None),
        "inconclusive_causes": [c.model_dump(mode="json")
                                for c in unified.inconclusive_causes],
        "saved_path": str(saved_path) if saved_path is not None else None,
        "compared_recordings": (rep.compared.model_dump(mode="json")
                                if getattr(rep, "compared", None) is not None else None),
        "baseline_stale": rep.baseline_stale,
        "static_targets": rep.static_targets,
        "confirmed": rep.confirmed,
        "blind": rep.blind,
        "dynamic_only": rep.dynamic_only,
        # coverage: contacts that ran / observable contacts; recall: recorded / ran.
        # ``granularity`` says how "ran" was known (line | function | file | symbol).
        "coverage_pct": None if rep.coverage is None else round(rep.coverage * 100),
        "recall_pct": None if rep.recall is None else round(rep.recall * 100),
        "granularity": rep.coverage_granularity,
        "blind_split": {"not_run": rep.blind_not_run,
                        "ran_not_recorded": rep.blind_ran_not_recorded,
                        "unobservable": rep.blind_unobservable},
        "unobservable_contacts": rep.contacts_unobservable,
        "compared": rep.diff is not None or bool(rep.fact_diff),
        "changes": {},
        "change_counts": {},
        "calibration": _calibration_data(lists, getattr(rep, "calibration", None)),
    }
    lines = [f"VERDICT: {verdict.upper()}"
             + (f" ({'; '.join(unified.reasons)})" if unified.reasons else "")]
    settles = inconclusive_line(unified)
    if settles:
        lines.append(settles)
    if rep.baseline_stale:
        lines.append("STALE EVIDENCE: a capture was taken against another manifest or "
                     "environment than the one it must describe. Capture again before "
                     "trusting this verdict.")
    lines.append(f"Static {rep.static_targets}, confirmed {rep.confirmed}, BLIND {rep.blind}, "
                 f"dynamic-only {rep.dynamic_only}.")
    lines.extend(coverage_lines(rep))
    focus = impact_focus(rep)
    data["impact_focus"] = focus
    if focus is not None:
        lines.append(f"Impact focus: {focus['paths']} changed API path(s); on them "
                     f"{focus['static_targets']} static, {focus['confirmed']} confirmed, "
                     f"{focus['blind']} BLIND, {focus['dynamic_only']} dynamic-only"
                     + (f"; coverage {focus['coverage_pct']}%"
                        if focus["coverage_pct"] is not None else "")
                     + " (data.impact_focus).")
    lines.extend(f"{note}" for note in rep.notes if note.startswith("RED:"))
    if verdict == "pass" and rep.blind:
        lines.append(f"PASS covers the exercised calls only: {rep.blind} BLIND symbol(s) "
                     "were never run.")

    fact_diff = rep.fact_diff or {}
    if fact_diff:
        entries = [e for e in fact_diff.get("entries", []) if e.get("category") != "equal"]
        for category in _CHANGE_CATEGORIES:
            names = [e.get("symbol", "?") for e in entries if e.get("category") == category]
            if names:
                data["changes"][category] = lists.fit(f"changes.{category}", names)
        data["change_counts"] = {k: v for k, v in (fact_diff.get("counts") or {}).items() if v}
        summary = (rep.all_fact_diff or fact_diff).get("summary")
        if summary:
            data["findings"] = _findings_data(summary)
            failing = data["findings"]["failing"]
            total = summary.get("failing_total", len(summary.get("failing") or []))
            for f in failing[:3]:
                lines.append(f"FAIL {f['symbol']} at {f['site']}: {f['why']}.")
            if total > 3:   # the agent must not read three as all of them
                lines.append(f"FAIL ... and {total - 3} more ({total} in all; "
                             f"data.findings.failing lists up to {len(failing)}).")
            for r in data["findings"]["review"]:
                lines.append(f"REVIEW {r['entries']} {r['reason']}: {r['action']}.")
        counted = ", ".join(f"{k} {v}" for k, v in data["change_counts"].items())
        lines.append(f"Calls: {counted or 'nothing moved'}.")
        dropped = sum(v for k, v in lists.truncated.items() if k.startswith("changes."))
        if dropped:
            lines.append(f"{dropped} symbol name(s) omitted from data.changes; full report: "
                         f"{full_path}")
    edited = (rep.all_fact_diff or rep.fact_diff or {})
    if (edited.get("edited_sites") or {}).get("sites") and edited.get("edited_summary"):
        lines.append(f"{edited['edited_summary']}.")
        data["edited_sites"] = edited["edited_sites"]
    outside = list(getattr(rep, "unpredicted_changes", None) or ())
    if outside:
        data["changes_outside_plan"] = [
            {"symbol": e.get("symbol"), "category": e.get("category"),
             "site": _site_text(e.get("old_site") or e.get("new_site"))} for e in outside]
        lines.append(f"{len(outside)} behaviour change(s) outside the plan's predicted impacts: "
                     + "; ".join(f"{e['symbol']} at {e['site']}"
                                 for e in data["changes_outside_plan"][:5])
                     + (" ..." if len(outside) > 5 else "")
                     + " (data.changes_outside_plan).")
    cal = data["calibration"]
    if cal is not None and cal["predicted"]:
        prevented = f"{cal['prevented']} prevented by the plan, " if cal["prevented"] else ""
        lines.append(f"Predictions ({cal['source']}): {cal['hit']} hit, {prevented}"
                     f"{cal['miss']} miss, {cal['not_exercised']} not exercised; "
                     f"{cal['unpredicted_total']} change(s) nobody predicted "
                     "(data.calibration).")
    note = saved_verdict_note(getattr(rep, "compared", None), saved_path)
    if note:
        lines.append(note)

    if not data["compared"]:
        hint = ("Only one side is recorded, so nothing was compared: change the code, then "
                "capture post-migration.")
    elif verdict == "fail":
        hint = "Behaviour changed: fix the listed calls, capture post-migration again, verify."
    elif verdict == "inconclusive":
        hint = settles or ("Read the reasons: usually a fresh capture or more coverage (BLIND) "
                           "is needed.")
    else:
        hint = None
    return ok("\n".join(lines), lists.finish(data, list(rep.notes)),
              full_report_path=full_path, hint=hint)
