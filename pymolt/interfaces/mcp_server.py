"""stdio MCP server exposing PyMolt's migration funnel as agent tools.

This is a thin interface over the same service functions the CLI calls (the
"interfaces are thin; each phase is a service module" rule). It never
re-implements analysis — every tool mirrors a CLI command and reshapes the
result into a compact, token-disciplined dict an agent can act on.

Requires the optional ``mcp`` extra (``pip install 'pymolt[mcp]'``); importing
this module without it raises ``ModuleNotFoundError`` at the ``mcp`` import,
which the CLI catches to print an actionable message.

Every tool returns the universal contract:

    {"ok": True, "summary": "...", "data": {...},
     "full_report_path": "<path or null>", "hint": "<next step or null>"}

or, on any failure:

    {"ok": False, "error": "<one-line>", "hint": "<how to fix>"}

Exceptions never cross the MCP boundary — they are caught and shaped into
``ok: False``. Lists in ``data`` are capped at 20 items; the full payload is
written under ``.pymolt/mcp/`` and its path returned in ``full_report_path``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

# ─────────────────────────────────────────────────────────────────────────────
# Constants + helpers
# ─────────────────────────────────────────────────────────────────────────────

LIST_CAP = 20

# Lists that ARE the answer (which packages change, which symbols changed
# behaviour) are budgeted by size, not clipped at a fixed item count. Measured on
# flasgger: 31 compact assess rows cost 453 tokens, while the 20 JSON rows this
# used to send cost 643 — the clip made the payload *larger* per unit of answer
# and made agents answer from a partial list. ~6000 chars ≈ 1.5k tokens.
ANSWER_BUDGET_CHARS = 6000

# Workspace confinement: every `project_dir` argument must resolve to this root or one
# of its descendants. `PYMOLT_MCP_ROOT` lets an operator (or a test) pin the
# root explicitly; otherwise it is the server process's CWD at import/startup.
# Tests may monkeypatch this module-level constant directly to exercise a
# different root without relying on process env vars.
_ROOT_ENV_VAR = "PYMOLT_MCP_ROOT"
_WORKSPACE_ROOT = (
    Path(os.environ[_ROOT_ENV_VAR]).expanduser().resolve()
    if os.environ.get(_ROOT_ENV_VAR)
    else Path.cwd().resolve()
)

# Command execution gate: any tool that spawns a host process (docker/uv/the
# LLM-supplied test/app command) is opt-in only, off by default.
_EXEC_ENV_VAR = "PYMOLT_MCP_ALLOW_EXEC"

_INSTRUCTIONS = """\
PyMolt proves whether a Python migration changed your program's behaviour.
The tools have the names of the pymolt CLI commands.

Two answers only these tools give:
  scan             every project root, and every call site into a dependency,
                   resolved through imports and scopes (LibCST). Grepping imports
                   finds modules, not the symbols called, and undercounts the
                   surface severalfold.
  capture, verify  record what the code calls at runtime before AND after the
                   migration, then report what changed: results, raises, calls
                   that vanished. Reading files or resolving dependencies cannot
                   tell you this; it takes the two recordings.

Order (status says where the project is and which tool is next):
  scan -> setup -> assess -> capture(when='baseline')
  -> change the code -> capture(when='post-migration') -> verify
  capture needs setup: it records the environment it ran in. Repeat code
  changes + post-migration capture until verify says PASS. A successful assess
  is not evidence the program still works.

Changing the code: plan saves the codemods as the migration plan and returns
the diffs; it writes no source file. A person applies the plan with
`pymolt apply` in the project, outside this server. You may also edit the code
yourself.
"""

mcp = FastMCP("pymolt", instructions=_INSTRUCTIONS)


def _resolve_dir(project_dir: str) -> Path:
    """Resolve ``project_dir`` to a Path, raising ValueError with an actionable message
    when it does not exist, is not a directory, or escapes the workspace root
    (``_WORKSPACE_ROOT`` — see ``PYMOLT_MCP_ROOT``)."""
    path = Path(project_dir).expanduser().resolve()
    if path != _WORKSPACE_ROOT and _WORKSPACE_ROOT not in path.parents:
        raise ValueError(
            f"Path {path} is outside the workspace root {_WORKSPACE_ROOT}; "
            f"paths must be inside the root (set {_ROOT_ENV_VAR} to change it)."
        )
    if not path.exists():
        raise ValueError(f"Directory does not exist: {path}")
    if not path.is_dir():
        raise ValueError(f"Not a directory: {path}")
    return path


def _require_exec_allowed() -> dict | None:
    """Gate for any tool that spawns a host process (docker/uv/an LLM-supplied
    command). Returns a ``_fail`` result-dict to short-circuit the caller
    unless ``PYMOLT_MCP_ALLOW_EXEC`` is set to a truthy value; returns ``None``
    when execution is permitted."""
    value = os.environ.get(_EXEC_ENV_VAR, "").strip().lower()
    if value in ("1", "true", "yes"):
        return None
    return _fail(
        "Execution is disabled: this operation runs host commands (docker/uv/your "
        "command) and is opt-in only, off by default.",
        hint=f"Set {_EXEC_ENV_VAR}=1 to enable, and only connect this MCP server "
             "to trusted clients.",
    )


def _cap_list(items: list[Any]) -> tuple[list[Any], int]:
    """Return (first LIST_CAP items, number remaining/truncated)."""
    if len(items) <= LIST_CAP:
        return list(items), 0
    return list(items[:LIST_CAP]), len(items) - LIST_CAP


def _fit_answer(rows: list[str], budget: int = ANSWER_BUDGET_CHARS) -> tuple[list[str], int]:
    """Return (rows that fit in ``budget`` chars, number dropped).

    Use for any list the agent is expected to answer *from*; use ``_cap_list``
    only for lists that are context around an answer stated elsewhere.
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


def _truncation_note(shown: int, dropped: int, full_report_path: str | None) -> str:
    """One line stating how complete a list is — appended to the summary.

    Silence about truncation is what makes an agent treat a partial list as the
    whole answer.
    """
    if not dropped:
        return f"All {shown} listed."
    where = f" Full list: {full_report_path}" if full_report_path else ""
    return f"Showing {shown} of {shown + dropped} — {dropped} omitted.{where}"


def _write_full_report(path: Path, name: str, payload: Any) -> str:
    """Serialize ``payload`` to ``.pymolt/mcp/<name>.json`` and return its path."""
    out_dir = path / ".pymolt" / "mcp"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{name}.json"
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return str(out_path)


def _ok(summary: str, data: dict, full_report_path: str | None = None,
        hint: str | None = None) -> dict:
    return {
        "ok": True,
        "summary": summary,
        "data": data,
        "full_report_path": full_report_path,
        "hint": hint,
    }


def _fail(error: str, hint: str | None = None) -> dict:
    return {"ok": False, "error": error, "hint": hint}


# ─────────────────────────────────────────────────────────────────────────────
# 0. status
# ─────────────────────────────────────────────────────────────────────────────


@mcp.tool()
def status(project_dir: str = ".") -> dict:
    """Report where the project stands in the migration funnel, whether the
    configured environments exist, and the single command to run next. Call this
    anytime to orient your agent workflow.

    Args:
        project_dir: Project directory (default: current directory).
    """
    try:
        path = _resolve_dir(project_dir)
        from pymolt.status import build_status

        funnel = build_status(str(path), check_environments=True)
        full = funnel.model_dump(mode="json")
        full_path = _write_full_report(path, "status", full)

        phases_out = [
            {"phase": p.phase, "state": p.state, "detail": p.detail, "blocker": p.blocker}
            for p in funnel.phases
        ]
        data = {
            "phases": phases_out,
            "checks": [check.model_dump(mode="json") for check in funnel.checks],
            "next_command": funnel.next_command,
            "next_reason": funnel.next_reason,
            "notes": funnel.notes,
        }
        done_count = sum(1 for p in funnel.phases if p.state == "done")
        summary = (
            f"Funnel status for {path.name}: {done_count}/{len(funnel.phases)} phases complete.\n"
            f"Next step: {funnel.next_command or 'migration complete'}"
        )
        return _ok(summary, data, full_report_path=full_path, hint=funnel.next_reason)
    except Exception as e:  # noqa: BLE001
        return _fail(f"status failed: {e}", hint="Ensure project_dir is a readable directory.")


# ─────────────────────────────────────────────────────────────────────────────
# 1. scan
# ─────────────────────────────────────────────────────────────────────────────


@mcp.tool()
def scan(project_dir: str = ".") -> dict:
    """Find EVERY project root in the repo, with its manifests, Python-version
    evidence and dependency edges, and every call site where the code enters a
    third-party dependency — offline, no resolution, no runtime.

    Assume the repo is a monorepo until this tool says otherwise. A nested
    directory whose only marker is its own requirements.txt is a separate project
    root with its own dependency set, and it is exactly the kind that gets missed
    when the tree is walked by hand looking for setup.py/pyproject.toml. This also
    reports where roots *disagree* about the target Python — divergence a
    file-by-file read does not surface.

    Call sites are resolved through imports and scopes with LibCST. Do not
    reconstruct them by grepping imports: an import says a module is present, not
    which symbols are called, and undercounts the contact surface severalfold. They
    are the denominator every coverage and trust number is measured against.

    Args:
        project_dir: Project/repo directory to scan (default: current directory).

    Run this before setup/assess, and before answering any question about what the
    project depends on or which Python it targets.
    """
    try:
        path = _resolve_dir(project_dir)
        from pymolt.scan import run_scan

        report = run_scan(str(path))
        full = report.model_dump(mode="json")
        full_path = _write_full_report(path, "scan", full)

        surfaces = report.surfaces
        roots_out: list[dict] = []
        divergences: list[str] = []
        for root in surfaces.project_roots:
            ev = [
                {"source": e.source, "version": e.version, "kind": e.kind}
                for e in root.version_evidence
            ]
            divergent = root.is_divergent
            if divergent:
                divergences.append(
                    f"{root.path}: {', '.join(root.runtime_versions()) or '?'}"
                )
            manifests = list(root.manifests)
            roots_out.append({
                "path": root.path,
                "chosen_manifest": manifests[0] if manifests else None,
                "manifests": manifests,
                "python_version_evidence": ev,
                "divergent": divergent,
            })
        roots_capped, roots_trunc = _cap_list(roots_out)

        # Edge totals aggregated across roots.
        by_group: dict[str, int] = {}
        by_kind: dict[str, int] = {}
        total_edges = 0
        for inv in report.inventory_by_root.values():
            total_edges += len(inv.edges)
            for g, c in inv.counts_by_group().items():
                by_group[g] = by_group.get(g, 0) + c
            for k, c in inv.counts_by_kind().items():
                by_kind[k] = by_kind.get(k, 0) + c

        data: dict[str, Any] = {
            "root_count": len(surfaces.project_roots),
            "roots": roots_capped,
            "edge_totals": {"total": total_edges, "by_group": by_group, "by_kind": by_kind},
            "top_divergences": divergences[:LIST_CAP],
        }
        if roots_trunc:
            data["truncated"] = roots_trunc

        summary_lines = [
            f"{len(surfaces.project_roots)} project root(s), {total_edges} dependency edge(s).",
        ]
        cmap = report.contact_map
        if cmap is not None:
            per_dep = sorted(
                ((dep, len(targets)) for dep, targets in cmap.by_dep.items()),
                key=lambda kv: (-kv[1], kv[0]),
            )
            per_dep_capped, per_dep_trunc = _fit_answer([f"{d} {c}" for d, c in per_dep])
            distinct = len({c.target for c in cmap.contacts})
            data["contacts"] = {
                "total": len(cmap.contacts),
                "distinct_symbols": distinct,
                "files_scanned": cmap.files_scanned,
                "per_dependency_format": "dep distinct_symbol_count",
                "per_dependency": per_dep_capped,
            }
            if per_dep_trunc:
                data["contacts"]["truncated"] = per_dep_trunc
            summary_lines.append(
                f"{len(cmap.contacts)} call site(s) into {len(cmap.by_dep)} dependency(ies), "
                f"{distinct} distinct symbol(s), {cmap.files_scanned} file(s) scanned. "
                + _truncation_note(len(per_dep_capped), per_dep_trunc, full_path)
            )
        if divergences:
            summary_lines.append(f"{len(divergences)} root(s) with Python-version divergence.")
        else:
            summary_lines.append("No Python-version divergence detected.")
        if by_kind:
            kinds = ", ".join(f"{k}:{v}" for k, v in sorted(by_kind.items()))
            summary_lines.append(f"Edge kinds: {kinds}.")

        return _ok(
            "\n".join(summary_lines),
            data,
            full_report_path=full_path,
            hint="Run assess (with a target Python) to check upgrade feasibility.",
        )
    except Exception as e:  # noqa: BLE001 — never cross the MCP boundary
        return _fail(f"scan failed: {e}", hint="Check that `project_dir` is a readable directory.")


# ─────────────────────────────────────────────────────────────────────────────
# 2. setup
# ─────────────────────────────────────────────────────────────────────────────


@mcp.tool()
def setup(
    project_dir: str = ".",
    manifest: str = "",
    tool: str = "",
    base_python: str = "",
    target_python: str = "",
    container_id: str | None = None,
    target_env: str | None = None,
) -> dict:
    """Choose the manifest, resolution tool and Python versions, and save them to
    .pymolt/env_config.json. Every later tool reads this file.

    Called with no choices, it returns the valid ones (manifests, tools,
    interpreters, target Pythons, with defaults) and saves nothing. Called with
    manifest, tool, base_python and target_python, it checks them and saves. An
    invalid value returns ok:false naming the valid ones. Do not guess the strings.

    Args:
        project_dir: Project directory (default: current directory).
        manifest: Manifest file name, from the returned manifests.
        tool: Resolution tool, from the returned tools (uv/conda/poetry/container/system).
        base_python: The project's current Python (e.g. "3.6").
        target_python: Python to migrate to, from the returned target_python_options.
        container_id: Docker container id; required only when tool == "container".
        target_env: Path of an existing target environment (venv), if one is built.
    """
    try:
        path = _resolve_dir(project_dir)
        from pymolt.ingestion.config import ToolChoice
        from pymolt.setup.service import (
            SetupChoices,
            SetupError,
            apply_setup,
            check_setup_values,
            gather_setup_options,
        )

        options = gather_setup_options(path)
        manifests = [m.name for m in options.manifests]
        if not manifests:
            return _fail(
                "No Python dependency manifests detected.",
                hint="Point `project_dir` at a project with requirements.txt / pyproject.toml / "
                     "environment.yml.",
            )
        tools = [t.name for t in options.tools if t.available]
        target_versions = [v.version for v in options.target_versions]

        given = {"manifest": manifest, "tool": tool, "base_python": base_python,
                 "target_python": target_python}
        if not any(given.values()):
            full_path = _write_full_report(path, "setup", options.model_dump(mode="json"))
            data = {
                "manifests": manifests,
                "default_manifest": options.default_manifest,
                "tools": tools,
                "default_tool": options.default_tool,
                "interpreters": options.interpreters[:LIST_CAP],
                "base_python_default": options.base_python_default,
                "target_python_options": target_versions[:LIST_CAP],
                "default_target_python": options.default_target,
            }
            summary = (
                f"Nothing saved. Manifests: {', '.join(manifests)}.\n"
                f"Tools: {', '.join(tools)} (default {options.default_tool}).\n"
                f"Base Python: {options.base_python_default}.\n"
                f"Target options: {', '.join(target_versions) or 'none'}."
            )
            return _ok(summary, data, full_report_path=full_path,
                       hint="Call setup again with manifest, tool, base_python and "
                            "target_python from these values.")

        missing = [name for name, value in given.items() if not value]
        if missing:
            return _fail(f"Missing: {', '.join(missing)}.",
                         hint="Call setup with no choices to list the valid values.")
        try:
            check_setup_values(
                options, manifest=manifest, tool=tool, base_python=base_python,
                target_python=target_python, container_id=container_id,
            )
        except SetupError as exc:
            return _fail(f"{exc}.", hint=exc.hint)
        if target_versions and target_python not in target_versions:
            return _fail(
                f"Invalid target_python {target_python!r}.",
                hint=f"Valid target_python values: {target_versions}",
            )

        config = apply_setup(path, SetupChoices(
            selected_manifest=manifest,
            selected_tool=ToolChoice(tool),
            base_python=base_python,
            target_python=target_python,
            container_id=container_id,
            target_env_created=bool(target_env),
            target_env_path=target_env,
        ))
        config_path = str(path / ".pymolt" / "env_config.json")
        return _ok(
            f"Saved config to {config_path}.\n"
            f"Manifest {manifest}, tool {tool}, {base_python} -> {target_python}.",
            {"config": config.model_dump(mode="json"), "config_path": config_path},
            full_report_path=config_path,
            hint="Run assess to compare base vs target dependency graphs.",
        )
    except Exception as e:  # noqa: BLE001
        return _fail(f"setup failed: {e}", hint="Call setup with no choices for valid values.")


# ─────────────────────────────────────────────────────────────────────────────
# 4. assess
# ─────────────────────────────────────────────────────────────────────────────


@mcp.tool()
def assess(
    project_dir: str = ".",
    target_python: str | None = None,
    risk: bool = False,
    no_cache: bool = False,
) -> dict:
    """Resolve base vs target dependency graphs and compare them: which package
    versions move, which conflict, which enter the manual zone, and where the pinned
    target manifest lands. Faster and more reproducible than driving uv/pip by hand,
    and it records the honesty markers (fixation, resolution quality, base tier)
    that a hand-run resolve loses.

    A successful resolve is an INPUT to the migration, not evidence it worked.
    Never report "the migration is feasible" from this tool alone — cite the
    base tier (`baseline_tier`), and get behavioural proof from verify.

    Args:
        project_dir: Project directory (default: current directory).
        target_python: Target Python (e.g. "3.13"); falls back to setup config.
        risk: When true, assess per-package migration risk (CVEs, wheels,
            abandonment). This does NETWORK calls (cached) and can take a while.
        no_cache: Bypass the resolved-graph cache and re-resolve from scratch.

    On a successful resolve it also returns `target_env`: the uv commands (and a
    Dockerfile derived from the project's, if it has one) that build the target
    environment. pymolt builds nothing; you or the engineer build it.

    Use after setup. Requires `uv` on PATH.
    """
    try:
        path = _resolve_dir(project_dir)
        from pymolt.assess.service import run_assess
        from pymolt.interfaces.cli.commands import _load_or_autodetect_assess_config

        config = _load_or_autodetect_assess_config(path, quiet=True)
        effective_target = target_python or config.get("target_python")
        if not effective_target:
            return _fail(
                "No target_python given and none configured.",
                hint="Pass target_python (e.g. '3.13') or run setup first.",
            )

        tool = config.get("selected_tool", "uv")
        if hasattr(tool, "value"):
            tool = tool.value

        result = run_assess(
            str(path),
            target_python=effective_target,
            config=config,
            base_python=config.get("base_python"),
            container_id=config.get("container_id"),
            use_cache=not no_cache,
            tool=tool,
            with_risk=risk,
        )

        full = result.model_dump(mode="json")
        full_path = _write_full_report(path, "assess", full)

        # Blockers/upgrades first, then the rest.
        priority = {"conflict": 0, "removed": 1, "upgrade": 2, "added": 3}
        rows_sorted = sorted(
            result.rows, key=lambda r: (priority.get(r["status"], 9), r["name"])
        )
        risk_by_pkg: dict[str, str] = {}
        if risk and result.risk is not None:
            for p in result.risk.packages:
                tier = p.tier.value if hasattr(p.tier, "value") else str(p.tier)
                risk_by_pkg[p.name] = tier

        def _fmt_row(r: dict) -> str:
            line = (f"{r['name']} {r['baseline_version'] or '-'}"
                    f">{r['target_version'] or '-'} {r['status']}")
            if risk:
                tier = risk_by_pkg.get(r["name"])
                if tier:
                    line += f" risk={tier}"
            return line

        pkg_rows, rows_trunc = _fit_answer([_fmt_row(r) for r in rows_sorted])

        counts = result.counts_by_status()
        # run_assess writes the pinned target manifest on a successful resolve and
        # records its path — no need to probe the filesystem.
        target_manifest = result.target_manifest_path

        data: dict[str, Any] = {
            "target_python": effective_target,
            "target_resolved": result.target_resolved,
            "target_error": result.target_error,
            "changes_format": "name base>target status" + (" risk=tier" if risk else ""),
            "changes": pkg_rows,
            "status_counts": counts,
            "conflicts": counts.get("conflict", 0),
            "manual_zone_count": len(result.manual_zone),
            "target_manifest_path": target_manifest,
        }
        if rows_trunc:
            data["truncated"] = rows_trunc
        if result.target_resolved:
            from pymolt.setup.target_hint import build_target_hint

            hint = build_target_hint(path, effective_target, manifest=target_manifest)
            data["target_env"] = {
                "suggested_uv_commands": hint.suggested_uv_commands,
                "suggested_dockerfile": hint.suggested_dockerfile,
                "notes": hint.notes,
            }

        if result.target_resolved:
            head = f"Target Python {effective_target}: resolution feasible."
        else:
            head = f"Target Python {effective_target}: BLOCKED — {result.target_error}"
        summary = (
            f"{head}\n"
            f"Upgrades: {counts.get('upgrade', 0)}, added: {counts.get('added', 0)}, "
            f"conflicts: {counts.get('conflict', 0)}, manual-zone: {len(result.manual_zone)}.\n"
            f"{len(rows_sorted)} package(s) differ between base and target. "
            + _truncation_note(len(pkg_rows), rows_trunc, full_path)
        )
        if target_manifest:
            summary += f"\nPinned target manifest written: {target_manifest}"
        return _ok(
            summary,
            data,
            full_report_path=full_path,
            hint="Capture a baseline contract (capture when='baseline') before "
                 "editing code, then edit, then capture post-migration.",
        )
    except Exception as e:  # noqa: BLE001
        return _fail(
            f"assess failed: {e}",
            hint="Ensure `uv` is on PATH and setup is configured; try no_cache=true.",
        )


# ─────────────────────────────────────────────────────────────────────────────
# 6. capture
# ─────────────────────────────────────────────────────────────────────────────

_CAPTURE_WHEN = {"baseline": "baseline", "post-migration": "post_migration"}


@mcp.tool()
def capture(
    project_dir: str = ".",
    when: str = "baseline",
    mode: str = "tests",
    command: list[str] | None = None,
    force: bool = False,
    impact_targets: list[str] | None = None,
    deployment_id: str | None = None,
    request_id: str | None = None,
    correlation_id: str | None = None,
) -> dict:
    """Runs YOUR command with the boundary tracer injected — zero edits to the
    project — and records the observed dependency calls as a named baseline /
    post-migration slot. Call this instead of wiring a tracer yourself.

    Baseline runs in the configured container, post-migration under the target
    env, automatically — sourced from the project's saved setup config
    (.pymolt/env_config.json) unless there is none, in which case it runs locally.

    Args:
        project_dir: Project directory (default: current directory).
        when: "baseline" (capture BEFORE migrating) or "post-migration" (AFTER).
        mode: "tests" (run your suite) or "command" (run your app).
        command: The command to run, as a list (e.g. ["pytest", "tests/"]).
        force: Overwrite an existing capture in this slot without prompting.
        impact_targets: Affected API symbols; post-migration defaults to the applied plan.
        deployment_id: Stage/canary deployment identity for provenance.
        request_id: Request identity for correlation.
        correlation_id: Cross-system correlation identity.

    Use baseline before editing code, post-migration after — verify compares them.
    """
    gate = _require_exec_allowed()
    if gate is not None:
        return gate
    try:
        path = _resolve_dir(project_dir)
        from pymolt.verify import service
        from pymolt.verify.models import CaptureMode

        when_key = _CAPTURE_WHEN.get(when)
        if when_key is None:
            return _fail(
                f"Invalid when {when!r}.",
                hint="when must be 'baseline' or 'post-migration'.",
            )
        capture_mode = {
            "tests": CaptureMode.TEST_SUITE,
            "command": CaptureMode.LIVE_COMMAND,
        }.get(mode)
        if capture_mode is None:
            return _fail(
                f"Invalid mode {mode!r}.",
                hint="mode must be 'tests' or 'command'.",
            )
        if not command:
            return _fail(
                "command is required.",
                hint="Provide a command list, e.g. ['pytest', 'tests/'].",
            )
        if service.environment_fingerprint(path) is None:
            return _fail(
                "pymolt is not configured for this project.",
                hint="Run setup first. A capture records the environment it was taken in.",
            )

        existing = getattr(service.load_contract_state(str(path)), when_key)
        if existing is not None and not force:
            return _fail(
                f"'{when}' already has a capture from {existing.captured_at}.",
                hint="Pass force=true to overwrite it.",
            )

        slot = service.capture_named_trace(
            str(path), when_key, capture_mode, command=list(command),
            impact_targets=impact_targets,
            deployment_id=deployment_id,
            request_id=request_id,
            correlation_id=correlation_id,
        )

        state = service.load_contract_state(str(path))
        filled = [
            name for name in ("baseline", "post_migration") if getattr(state, name) is not None
        ]
        data = {
            "when": when,
            "events": slot.events,
            "processes": slot.processes,
            "trace_path": slot.trace_path,
            "filled_slots": filled,
            "env_note": slot.env_note,
            "duration_seconds": slot.duration_seconds,
            "deployment_id": slot.deployment_id,
            "request_id": slot.request_id,
            "correlation_id": slot.correlation_id,
            "impact_targets": slot.impact_targets,
            "dropped_events": slot.dropped_events,
            "metadata_path": slot.metadata_path,
        }
        summary = (
            f"Recorded {slot.events} contact(s) into the '{when}' slot.\n"
            f"Filled slots: {', '.join(filled)}."
            + (f"\nEnv: {slot.env_note}" if slot.env_note else "")
        )
        need_post = "post_migration" not in filled
        return _ok(
            summary,
            data,
            full_report_path=slot.trace_path,
            hint=("Edit the project's code, then capture when='post-migration'."
                  if need_post else "Run verify to compare baseline and post-migration."),
        )
    except Exception as e:  # noqa: BLE001
        return _fail(
            f"capture failed: {e}",
            hint="Ensure `command` runs from `project_dir` and exits (e.g. a test suite).",
        )


# ─────────────────────────────────────────────────────────────────────────────
# 7. verify
# ─────────────────────────────────────────────────────────────────────────────

_CHANGE_CATEGORIES = ("changed", "disappeared", "appeared", "uncomparable", "rewritten")


@mcp.tool()
def verify(project_dir: str = ".") -> dict:
    """The verdict on whether the migration changed your program's behaviour:
    PASS, FAIL or INCONCLUSIVE, with the reasons, the coverage of the static
    contact map (confirmed / BLIND, trust %), and exactly which dependency calls
    now return something else, raise something else, or stopped happening.

    This is the one question no amount of file reading, type checking or
    dependency resolution can answer — it requires the baseline and
    post-migration captures from capture. A green test suite and a
    clean resolve are not substitutes: they do not tell you a call started
    returning an empty generator instead of None.

    Args:
        project_dir: Project directory (default: current directory).

    Same verdict as `pymolt verify`, saved to .pymolt/ so status counts it.
    Repeat edits + post-migration capture until the verdict is PASS.
    """
    try:
        path = _resolve_dir(project_dir)
        from pymolt.migration_state import MigrationReceipt
        from pymolt.verify import service
        from pymolt.verify.unified import build_unified_verification

        rep = service.build_contract_report_from_state(str(path))
        receipt = MigrationReceipt.load(path)
        unified = build_unified_verification(
            rep, run_id=receipt.run_id if receipt is not None else None,
        )
        unified.save(path)
        full_path = _write_full_report(path, "verify", rep.model_dump(mode="json"))

        trust_pct = round(rep.trust * 100)
        data: dict[str, Any] = {
            "verdict": unified.verdict.value,
            "reasons": rep.verdict_reasons,
            "static_targets": rep.static_targets,
            "confirmed": rep.confirmed,
            "blind": rep.blind,
            "dynamic_only": rep.dynamic_only,
            "trust_pct": trust_pct,
        }
        summary_lines = [
            f"VERDICT: {unified.verdict.value.upper()}"
            + (f" — {'; '.join(rep.verdict_reasons)}" if rep.verdict_reasons else ""),
            f"Static {rep.static_targets}, confirmed {rep.confirmed}, BLIND {rep.blind}, "
            f"dynamic-only {rep.dynamic_only}. Contract trust: {trust_pct}%.",
        ]

        fact_diff = rep.fact_diff or {}
        entries = [e for e in fact_diff.get("entries", []) if e.get("category") != "equal"]
        if fact_diff:
            changes: dict[str, Any] = {}
            dropped = 0
            for category in _CHANGE_CATEGORIES:
                names = [e.get("symbol", "?") for e in entries if e.get("category") == category]
                if names:
                    shown, cut = _fit_answer(names)
                    changes[category] = shown
                    dropped += cut
            data["changes"] = changes
            data["change_counts"] = {
                k: v for k, v in (fact_diff.get("counts") or {}).items() if v
            }
            counted = ", ".join(f"{k} {v}" for k, v in data["change_counts"].items())
            summary_lines.append(f"Calls: {counted or 'nothing moved'}.")
            if dropped:
                summary_lines.append(
                    f"{dropped} symbol name(s) omitted from the lists — full report: {full_path}"
                )

        state = service.load_contract_state(str(path))
        hint = None
        if state.post_migration is None:
            hint = ("No post-migration capture yet — edit the code, then "
                    "capture when='post-migration'.")
        elif unified.verdict.value == "fail":
            hint = ("Behaviour changed — fix the listed calls, re-capture "
                    "post-migration, verify again.")

        return _ok("\n".join(summary_lines), data, full_report_path=full_path, hint=hint)
    except Exception as e:  # noqa: BLE001
        return _fail(
            f"verify failed: {e}",
            hint="Capture at least a baseline with capture first.",
        )


# ─────────────────────────────────────────────────────────────────────────────
# 8. plan
# ─────────────────────────────────────────────────────────────────────────────


@mcp.tool()
def plan(project_dir: str = ".", endpoint: str | None = None) -> dict:
    """Save the verified code changes for the target manifest as the migration plan,
    and return them as diffs. Writes .pymolt/migration_plan.json and no source file.

    The changes are LibCST codemods fetched for the dependency versions assess
    pinned. Only `pymolt apply`, run in the project by a person, writes them: it
    applies exactly this plan and refuses it if a planned file changed since.

    Args:
        project_dir: Project directory (default: current directory).
        endpoint: Custom Axiom Cloud Hub / On-Prem URL (default: configured endpoint).

    Use after setup, assess and the baseline capture.
    """
    try:
        import difflib
        path = _resolve_dir(project_dir)
        from pymolt.codemods.models import CodemodPattern
        from pymolt.codemods.service import PlanInputError, build_migration_plan
        from pymolt.config import load_endpoint

        try:
            migration_plan, previews, source_desc, by_pkg = build_migration_plan(
                path, base_url=load_endpoint(endpoint),
            )
        except PlanInputError as exc:
            return _fail(str(exc), hint=exc.hint or None)
        plan_path = migration_plan.save(path)

        changed = [p for p in previews if p.old_source != p.new_source]
        diffs = []
        for p in changed:
            diffs.append({"path": p.path, "diff": "".join(difflib.unified_diff(
                p.old_source.splitlines(keepends=True),
                p.new_source.splitlines(keepends=True),
                fromfile=f"a/{p.path}", tofile=f"b/{p.path}",
            ))})
        files_capped, files_trunc = _fit_answer(sorted(p.path for p in changed))
        diffs_capped, diffs_trunc = _fit_answer(
            [json.dumps(d) for d in diffs], budget=ANSWER_BUDGET_CHARS * 2
        )
        per_package = []
        for pkg, items in by_pkg.items():
            patterns = sum(1 for item in items if isinstance(item, CodemodPattern))
            per_package.append(f"{pkg} rules={len(items) - patterns} patterns={patterns}")
        full_path = _write_full_report(path, "plan", {
            "plan_id": migration_plan.plan_id, "source": source_desc,
            "per_package": per_package, "diffs": diffs,
        })

        data: dict[str, Any] = {
            "plan_id": migration_plan.plan_id,
            "plan_path": str(plan_path),
            "source": source_desc,
            "per_package": per_package,
            "files_to_change": files_capped,
            "rewrite_sites": sum(item.sites for item in migration_plan.changes),
            "diffs": [json.loads(d) for d in diffs_capped],
        }
        if files_trunc:
            data["files_truncated"] = files_trunc
        if diffs_trunc:
            data["diffs_truncated"] = diffs_trunc
        summary = (
            f"Plan {migration_plan.plan_id[:8]} saved: {len(changed)} file(s) to change, "
            f"{data['rewrite_sites']} site(s), from {source_desc}.\n"
            + _truncation_note(len(files_capped), files_trunc, full_path)
        )
        return _ok(
            summary, data, full_report_path=full_path,
            hint="Review the diffs, then run `pymolt apply .` in the project.",
        )
    except Exception as e:  # noqa: BLE001
        return _fail(
            f"plan failed: {e}",
            hint="Check that Axiom Cloud Hub is reachable and an API token is configured.",
        )


def run() -> None:
    """Run the stdio MCP server (blocking)."""
    mcp.run("stdio")
