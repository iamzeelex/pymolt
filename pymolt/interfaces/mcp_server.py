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

START HERE — the two tools nothing else can replace:
  contract_map     every call site into a dependency, resolved through imports
                   and scopes (LibCST). Grepping imports finds modules, not the
                   symbols actually called, and undercounts the surface severalfold.
  contract_capture / contract_report
                   record what the code really calls at runtime before AND after
                   the migration, then report what changed: results, raises,
                   symbols that vanished. This cannot be derived by reading files
                   or by resolving dependencies — it requires the recorded traces.

Supporting phases, in order:  scan -> setup_options/setup_apply -> assess -> env_hint
  These prepare the migration; assess resolves baseline vs target and tells you
  which versions move. A successful resolve is NOT evidence the program still
  works — it is an input to the contract, never the conclusion.

Typical run:
  contract_capture(when='baseline')  BEFORE editing anything
  -> assess -> engineer/agent edits code and builds the target env
  -> contract_capture(when='post-migration') -> contract_report
  Repeat edits + post-migration capture until contract_report is clean.

codemods_preview is a DRY RUN: it never applies changes.
Applying codemods requires explicit human approval via the CLI,
outside this server.
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
    """Report where the project stands in the migration funnel and what single
    command to run next. Call this anytime to orient your agent workflow.

    Args:
        project_dir: Project directory (default: current directory).
    """
    try:
        path = _resolve_dir(project_dir)
        from pymolt.status import build_status

        funnel = build_status(str(path))
        full = funnel.model_dump(mode="json")
        full_path = _write_full_report(path, "status", full)

        phases_out = [
            {"phase": p.phase, "state": p.state, "detail": p.detail, "blocker": p.blocker}
            for p in funnel.phases
        ]
        data = {
            "phases": phases_out,
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
    evidence and dependency edges — offline, no resolution.

    Assume the repo is a monorepo until this tool says otherwise. A nested
    directory whose only marker is its own requirements.txt is a separate project
    root with its own dependency set, and it is exactly the kind that gets missed
    when the tree is walked by hand looking for setup.py/pyproject.toml. This also
    reports where roots *disagree* about the target Python — divergence a
    file-by-file read does not surface.

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
# 2. setup_options
# ─────────────────────────────────────────────────────────────────────────────


@mcp.tool()
def setup_options(project_dir: str = ".") -> dict:
    """List the exact choices setup_apply accepts: available manifests, resolution
    tools, interpreters, and candidate target Python versions. Call this instead
    of guessing manifest/tool/version strings.

    Args:
        project_dir: Project directory (default: current directory).

    Use before setup_apply — the returned string values are what you pass in.
    """
    try:
        path = _resolve_dir(project_dir)
        from pymolt.setup.service import gather_setup_options

        options = gather_setup_options(path)
        full = options.model_dump(mode="json")
        full_path = _write_full_report(path, "setup_options", full)

        manifests = [m.name for m in options.manifests]
        tools = [t.name for t in options.tools if t.available]
        target_versions = [v.version for v in options.target_versions]
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

        if not manifests:
            return _fail(
                "No Python dependency manifests detected.",
                hint="Point `project_dir` at a project with requirements.txt / pyproject.toml / "
                     "environment.yml, then re-run setup_options.",
            )

        summary = (
            f"Manifests: {', '.join(manifests)}.\n"
            f"Tools: {', '.join(tools)} (default {options.default_tool}).\n"
            f"Base Python: {options.base_python_default}.\n"
            f"Target options: {', '.join(target_versions) or 'none'}."
        )
        return _ok(
            summary,
            data,
            full_report_path=full_path,
            hint="Call setup_apply with a manifest, tool, base_python, and target_python "
                 "from these exact values.",
        )
    except Exception as e:  # noqa: BLE001
        return _fail(f"setup_options failed: {e}",
                     hint="Check that `project_dir` is a readable project directory.")


# ─────────────────────────────────────────────────────────────────────────────
# 3. setup_apply
# ─────────────────────────────────────────────────────────────────────────────


@mcp.tool()
def setup_apply(
    project_dir: str = ".",
    manifest: str = "",
    tool: str = "",
    base_python: str = "",
    target_python: str = "",
    container_id: str | None = None,
) -> dict:
    """Persist the migration configuration to .pymolt/env_config.json (manifest,
    tool, base+target Python). Deterministic: pass values from setup_options; an
    invalid choice returns ok:false listing the valid ones.

    Args:
        project_dir: Project directory (default: current directory).
        manifest: Manifest file name (from setup_options.manifests).
        tool: Resolution tool (from setup_options.tools, e.g. uv/conda/poetry/system).
        base_python: Baseline Python version (e.g. "3.6").
        target_python: Target Python version (from setup_options.target_python_options).
        container_id: Docker container id, required only when tool == "container".

    Use after setup_options, before assess.
    """
    try:
        path = _resolve_dir(project_dir)
        from pymolt.ingestion.config import ToolChoice
        from pymolt.setup.service import SetupChoices, apply_setup, gather_setup_options

        options = gather_setup_options(path)
        valid_manifests = [m.name for m in options.manifests]
        valid_tools = [t.name for t in options.tools if t.available]
        valid_targets = [v.version for v in options.target_versions]

        if not manifest or manifest not in valid_manifests:
            return _fail(
                f"Invalid or missing manifest {manifest!r}.",
                hint=f"Valid manifests: {valid_manifests}",
            )
        if not tool or tool not in valid_tools:
            return _fail(
                f"Invalid or missing tool {tool!r}.",
                hint=f"Valid tools: {valid_tools}",
            )
        if not base_python:
            return _fail(
                "base_python is required.",
                hint=f"Suggested base_python: {options.base_python_default}",
            )
        if not target_python or (valid_targets and target_python not in valid_targets):
            return _fail(
                f"Invalid or missing target_python {target_python!r}.",
                hint=f"Valid target_python values: {valid_targets}",
            )
        if tool == "container" and not container_id:
            return _fail(
                "container_id is required when tool == 'container'.",
                hint=f"Running containers: {[c.id for c in options.containers]}",
            )

        choices = SetupChoices(
            selected_manifest=manifest,
            selected_tool=ToolChoice(tool),
            base_python=base_python,
            target_python=target_python,
            container_id=container_id,
        )
        config = apply_setup(path, choices)
        config_path = str(path / ".pymolt" / "env_config.json")
        data = {"config": config.model_dump(mode="json"), "config_path": config_path}
        summary = (
            f"Saved config to {config_path}.\n"
            f"Manifest {manifest}, tool {tool}, {base_python} -> {target_python}."
        )
        return _ok(
            summary,
            data,
            full_report_path=config_path,
            hint="Run assess to compare baseline vs target dependency graphs.",
        )
    except Exception as e:  # noqa: BLE001
        return _fail(f"setup_apply failed: {e}", hint="Re-run setup_options for valid values.")


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
    """Resolve baseline vs target dependency graphs and compare them: which package
    versions move, which conflict, which enter the manual zone, and where the pinned
    target manifest lands. Faster and more reproducible than driving uv/pip by hand,
    and it records the honesty markers (fixation, resolution quality, baseline tier)
    that a hand-run resolve loses.

    A successful resolve is an INPUT to the migration, not evidence it worked.
    Never report "the migration is feasible" from this tool alone — cite the
    baseline tier, and get behavioural proof from contract_report.

    Args:
        project_dir: Project directory (default: current directory).
        target_python: Target Python (e.g. "3.13"); falls back to setup config.
        risk: When true, assess per-package migration risk (CVEs, wheels,
            abandonment). This does NETWORK calls (cached) and can take a while.
        no_cache: Bypass the resolved-graph cache and re-resolve from scratch.

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
                hint="Pass target_python (e.g. '3.13') or run setup_apply first.",
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
            "changes_format": "name baseline>target status" + (" risk=tier" if risk else ""),
            "changes": pkg_rows,
            "status_counts": counts,
            "conflicts": counts.get("conflict", 0),
            "manual_zone_count": len(result.manual_zone),
            "target_manifest_path": target_manifest,
        }
        if rows_trunc:
            data["truncated"] = rows_trunc

        if result.target_resolved:
            head = f"Target Python {effective_target}: resolution feasible."
        else:
            head = f"Target Python {effective_target}: BLOCKED — {result.target_error}"
        summary = (
            f"{head}\n"
            f"Upgrades: {counts.get('upgrade', 0)}, added: {counts.get('added', 0)}, "
            f"conflicts: {counts.get('conflict', 0)}, manual-zone: {len(result.manual_zone)}.\n"
            f"{len(rows_sorted)} package(s) differ between baseline and target. "
            + _truncation_note(len(pkg_rows), rows_trunc, full_path)
        )
        if target_manifest:
            summary += f"\nPinned target manifest written: {target_manifest}"
        return _ok(
            summary,
            data,
            full_report_path=full_path,
            hint="Capture a baseline contract (contract_capture when='baseline') before "
                 "editing code, then edit, then capture post-migration.",
        )
    except Exception as e:  # noqa: BLE001
        return _fail(
            f"assess failed: {e}",
            hint="Ensure `uv` is on PATH and setup is configured; try no_cache=true.",
        )


# ─────────────────────────────────────────────────────────────────────────────
# 5. contract_map
# ─────────────────────────────────────────────────────────────────────────────


@mcp.tool()
def contract_map(project_dir: str = ".") -> dict:
    """Every call site where this project's code enters a third-party dependency,
    resolved through imports and scopes with LibCST. Cheap, static, no runtime.

    Do not try to reconstruct this by reading files or grepping imports: an import
    tells you a module is present, not which symbols are called or how often, and
    counting imports undercounts the real contact surface severalfold. This is the
    denominator every coverage and trust number is measured against.

    Args:
        project_dir: Project directory (default: current directory).

    Returns total call sites, distinct symbols, and per-dependency ranking.
    Run it before migrating, to see the surface a change could break.
    """
    try:
        path = _resolve_dir(project_dir)
        from pymolt.verify.contact_map import build_contact_map

        cmap = build_contact_map(str(path))
        full = cmap.model_dump(mode="json")
        full_path = _write_full_report(path, "contract_map", full)

        per_dep = sorted(
            ((dep, len(targets)) for dep, targets in cmap.by_dep.items()),
            key=lambda kv: (-kv[1], kv[0]),
        )
        per_dep_capped, per_dep_trunc = _fit_answer([f"{d} {c}" for d, c in per_dep])
        data: dict[str, Any] = {
            "total_contacts": len(cmap.contacts),
            "distinct_symbols": len({c.target for c in cmap.contacts}),
            "files_scanned": cmap.files_scanned,
            "per_dependency_format": "dep distinct_symbol_count",
            "per_dependency": per_dep_capped,
        }
        if per_dep_trunc:
            data["truncated"] = per_dep_trunc

        summary = (
            f"{len(cmap.contacts)} static contact(s) — call sites — across "
            f"{len(cmap.by_dep)} dependency(ies) and "
            f"{len({c.target for c in cmap.contacts})} distinct symbol(s), "
            f"{cmap.files_scanned} file(s) scanned.\n"
            f"Ranked by distinct symbols per dependency. "
            + _truncation_note(len(per_dep_capped), per_dep_trunc, full_path)
        )
        return _ok(
            summary,
            data,
            full_report_path=full_path,
            hint="Capture baseline + post-migration traces, then run contract_report.",
        )
    except Exception as e:  # noqa: BLE001
        return _fail(f"contract_map failed: {e}",
                     hint="Check that `project_dir` is a readable project directory.")


# ─────────────────────────────────────────────────────────────────────────────
# 6. contract_capture
# ─────────────────────────────────────────────────────────────────────────────

_CAPTURE_WHEN = {"baseline": "baseline", "post-migration": "post_migration"}


@mcp.tool()
def contract_capture(
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

    Use baseline before editing code, post-migration after — contract_report diffs them.
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
                  if need_post else "Run contract_report to diff baseline vs post-migration."),
        )
    except Exception as e:  # noqa: BLE001
        return _fail(
            f"contract_capture failed: {e}",
            hint="Ensure `command` runs from `project_dir` and exits (e.g. a test suite).",
        )


# ─────────────────────────────────────────────────────────────────────────────
# 7. contract_report
# ─────────────────────────────────────────────────────────────────────────────


@mcp.tool()
def contract_report(project_dir: str = ".") -> dict:
    """The verdict on whether the migration changed your program's behaviour:
    static contact map × recorded runtime traces → confirmed / BLIND coverage,
    a trust %, and exactly which dependency calls now return something else,
    raise something else, or stopped happening.

    This is the one question no amount of file reading, type checking or
    dependency resolution can answer — it requires the baseline and
    post-migration traces from contract_capture. A green test suite and a clean
    resolve are not substitutes: they do not tell you a call started returning an
    empty generator instead of None.

    Args:
        project_dir: Project directory (default: current directory).

    Auto-sources baseline/post-migration from contract_capture. Repeat edits +
    post-migration capture until no result_changed remains.
    """
    try:
        path = _resolve_dir(project_dir)
        from pymolt.verify import service
        from pymolt.verify.diff import build_boundary_diff

        rep = service.build_contract_report_from_state(str(path))
        full = rep.model_dump(mode="json")
        full_path = _write_full_report(path, "contract_report", full)

        trust_pct = round(rep.trust * 100)
        data: dict[str, Any] = {
            "static_targets": rep.static_targets,
            "confirmed": rep.confirmed,
            "blind": rep.blind,
            "dynamic_only": rep.dynamic_only,
            "trust_pct": trust_pct,
        }

        # Behavioral verdicts + NEEDS_ACTION come from the baseline->post BoundaryDiff.
        # rep.diff carries only counts; rebuild the diff for the symbol lists,
        # mirroring how the CLI report auto-sources the two named slots.
        state = service.load_contract_state(str(path))
        post_captured = state.post_migration is not None
        if rep.diff is not None and state.baseline is not None and state.post_migration is not None:
            bdiff = build_boundary_diff(state.baseline.trace_path, state.post_migration.trace_path)

            def _names(entries: list[dict]) -> tuple[list[str], int]:
                names = [e.get("qualname", "?") for e in entries]
                return _fit_answer(names)

            rc, rc_t = _names(bdiff.result_changed)
            rk, rk_t = _names(bdiff.raise_changed)
            dp, dp_t = _names(bdiff.disappeared)
            nh, nh_t = _names(bdiff.skipped_opaque)
            # True totals, independent of what fit in the payload — the summary
            # below must never report a truncated length as the finding count.
            totals = {
                "result_changed": len(bdiff.result_changed),
                "raise_changed": len(bdiff.raise_changed),
                "disappeared": len(bdiff.disappeared),
                "needs_action": len(bdiff.skipped_opaque),
            }
            data["behavioral"] = {
                "result_changed": rc,
                "raise_changed": rk,
                "disappeared": dp,
                "needs_action": nh,
                "counts": totals,
                "clean": bdiff.is_clean(),
            }
            for key, trunc in (("result_changed", rc_t), ("raise_changed", rk_t),
                               ("disappeared", dp_t), ("needs_action", nh_t)):
                if trunc:
                    data["behavioral"][f"{key}_truncated"] = trunc

        summary_lines = [
            f"Static {rep.static_targets}, confirmed {rep.confirmed}, BLIND {rep.blind}, "
            f"dynamic-only {rep.dynamic_only}.",
            f"Contract trust: {trust_pct}%.",
        ]
        behavioral = data.get("behavioral")
        if behavioral is not None:
            c = behavioral["counts"]
            summary_lines.append(
                f"Behavior: result_changed {c['result_changed']}, "
                f"raise_changed {c['raise_changed']}, "
                f"disappeared {c['disappeared']} "
                f"({'clean' if behavioral['clean'] else 'CHANGED'})."
            )
            dropped = sum(t for t in (rc_t, rk_t, dp_t, nh_t))
            if dropped:
                summary_lines.append(
                    f"{dropped} symbol name(s) omitted from the lists below — "
                    f"full report: {full_path}"
                )

        hint = None
        if not post_captured:
            hint = ("No post-migration capture yet — edit the code, then "
                    "contract_capture when='post-migration'.")
        elif behavioral is not None and not behavioral["clean"]:
            hint = ("Behavior changed — fix the flagged symbols, re-capture "
                    "post-migration, re-report.")

        return _ok("\n".join(summary_lines), data, full_report_path=full_path, hint=hint)
    except Exception as e:  # noqa: BLE001
        return _fail(
            f"contract_report failed: {e}",
            hint="Capture at least a baseline trace with contract_capture first.",
        )


# ─────────────────────────────────────────────────────────────────────────────
# 8. codemods & codemods_preview
# ─────────────────────────────────────────────────────────────────────────────


@mcp.tool()
def codemods_preview(project_dir: str = ".", endpoint: str | None = None) -> dict:
    """Preview the LibCST codemods that would rewrite call sites for the upgrades
    found by assess (dry-run). Returns modified file diffs and affected packages.

    Args:
        project_dir: Project directory (default: current directory).
        endpoint: Custom Axiom Cloud Hub / On-Prem URL (default: configured endpoint).
    """
    return codemods(project_dir=project_dir, write=False, endpoint=endpoint)


@mcp.tool()
def codemods(
    project_dir: str = ".",
    write: bool = False,
    endpoint: str | None = None,
) -> dict:
    """Fetch AST transformation rules and return an exact local preview.

    MCP is intentionally read-only for source mutations: use the CLI's
    ``plan``/``apply`` pair after human review.

    Args:
        project_dir: Project directory (default: current directory).
        write: Deprecated mutation request; always rejected for safety.
        endpoint: Custom Axiom Cloud Hub / On-Prem URL (default: configured endpoint).
    """
    if write:
        return _fail(
            "MCP codemod writes are disabled: mutation requires the baseline-gated CLI.",
            hint="Create a plan with `pymolt plan .`, then run `pymolt apply .` in the project.",
        )
    try:
        import difflib
        path = _resolve_dir(project_dir)
        from pymolt.codemods.models import CodemodPattern
        from pymolt.codemods.service import (
            preview_codemods,
            resolve_codemod_migrations,
        )
        from pymolt.config import load_endpoint
        from pymolt.ingestion.config import EnvConfig

        effective_endpoint = load_endpoint(endpoint)
        cfg = EnvConfig.load(path / ".pymolt" / "env_config.json")
        config = cfg.model_dump(mode="json") if cfg else None
        try:
            migrations, source_desc = resolve_codemod_migrations(str(path), config=config)
        except ValueError as exc:
            return _fail(
                f"No migration set available: {exc}",
                hint="Run assess first so codemods can derive the changed packages.",
            )

        by_pkg, previews = preview_codemods(
            str(path), migrations, base_url=effective_endpoint, auto_apply_only=True
        )
        changed_files = sorted({p.path for p in previews if p.old_source != p.new_source})
        diffs = []
        for p in previews:
            if p.old_source != p.new_source:
                diff_lines = list(difflib.unified_diff(
                    p.old_source.splitlines(keepends=True),
                    p.new_source.splitlines(keepends=True),
                    fromfile=f"a/{p.path}",
                    tofile=f"b/{p.path}",
                ))
                diffs.append({"path": p.path, "diff": "".join(diff_lines)})

        files_capped, files_trunc = _fit_answer(changed_files)
        diffs_capped, diffs_trunc = _fit_answer(
            [json.dumps(d) for d in diffs], budget=ANSWER_BUDGET_CHARS * 2
        )
        diffs_capped = [json.loads(d) for d in diffs_capped]

        per_package = []
        for pkg, items in by_pkg.items():
            conf_mix: dict[str, int] = {}
            rules = patterns = 0
            for item in items:
                if isinstance(item, CodemodPattern):
                    patterns += 1
                else:
                    rules += 1
                conf = getattr(item, "confidence", "unknown")
                conf_mix[conf] = conf_mix.get(conf, 0) + 1
            per_package.append({
                "package": pkg,
                "rules": rules,
                "patterns": patterns,
                "confidence_mix": conf_mix,
            })
        per_package_capped, pkg_trunc = _fit_answer([
            f"{e['package']} rules={e['rules']} patterns={e['patterns']} "
            + ",".join(f"{k}:{v}" for k, v in sorted(e["confidence_mix"].items()))
            for e in per_package
        ])

        full = {
            "write": write,
            "source": source_desc,
            "per_package": per_package,
            "files_changed": changed_files,
            "diffs": diffs,
        }
        full_path = _write_full_report(path, "codemods", full)

        data: dict[str, Any] = {
            "write": write,
            "source": source_desc,
            "per_package": per_package_capped,
            "files_changed": files_capped,
            "diffs": diffs_capped,
        }
        if pkg_trunc:
            data["per_package_truncated"] = pkg_trunc
        if files_trunc:
            data["files_truncated"] = files_trunc
        if diffs_trunc:
            data["diffs_truncated"] = diffs_trunc

        action_desc = "DRY-RUN preview"
        summary = (
            f"Codemods ({action_desc}):\n"
            f"{len(per_package)} package(s) from {source_desc}; "
            f"{len(changed_files)} file(s) would change.\n"
            + _truncation_note(len(files_capped), files_trunc, full_path)
        )
        return _ok(
            summary,
            data,
            full_report_path=full_path,
            hint="Create a plan with `pymolt plan .`, review it, then apply via the baseline-gated CLI.",
        )
    except Exception as e:  # noqa: BLE001
        return _fail(
            f"codemods failed: {e}",
            hint="Check that Axiom Cloud Hub is reachable and API token is configured.",
        )


# ─────────────────────────────────────────────────────────────────────────────
# 9. migrate (end-to-end macro tool)
# ─────────────────────────────────────────────────────────────────────────────


@mcp.tool()
def migrate(
    project_dir: str = ".",
    target_python: str | None = None,
    manifest: str | None = None,
    write: bool = False,
    endpoint: str | None = None,
) -> dict:
    """Run the complete end-to-end Python migration workflow for an agent in one call.

    Orchestrates: Setup/Discovery -> Assess (dependency resolution) -> LibCST Codemods.

    Args:
        project_dir: Project directory (default: current directory).
        target_python: Target Python version (e.g. '3.12' or '3.13'). Defaults to 3.12.
        manifest: Manifest path (e.g. 'requirements.txt'). Defaults to detected manifest.
        write: Deprecated mutation request; always rejected. Use CLI plan/apply for writes.
        endpoint: Custom Axiom Cloud Hub URL.
    """
    if write:
        return _fail(
            "MCP migration writes are disabled: mutation requires the baseline-gated CLI.",
            hint="Capture a baseline, then run `pymolt plan .` and `pymolt apply .` after human review.",
        )
    try:
        path = _resolve_dir(project_dir)
        from pymolt.assess.service import run_assess
        from pymolt.codemods.service import preview_codemods, resolve_codemod_migrations
        from pymolt.config import load_endpoint
        from pymolt.ingestion.config import EnvConfig
        from pymolt.ingestion.detect import detect_sources

        sources = detect_sources(path)
        if not sources:
            return _fail(
                f"No Python dependency sources detected in {path}",
                hint="Add a requirements.txt, pyproject.toml, Pipfile, environment.yml or setup.py.",
            )

        config_file = path / ".pymolt" / "env_config.json"
        cfg = EnvConfig.load(config_file) if config_file.is_file() else None
        config = cfg.model_dump(mode="json") if cfg else {}

        target_py = target_python or config.get("target_python", "3.12")
        selected_manifest = manifest or config.get("selected_manifest")
        if not selected_manifest:
            lock_sources = [s for s in sources if s.is_lock]
            selected_manifest = lock_sources[0].path.name if lock_sources else sources[0].path.name

        tool = config.get("selected_tool", "uv")
        if hasattr(tool, "value"):
            tool = tool.value

        assess_res = run_assess(
            str(path),
            target_python=target_py,
            source_manifest=selected_manifest,
            config=config,
            tool=tool,
            base_python=config.get("base_python"),
            container_id=config.get("container_id"),
            write_manifest=False,
        )

        if not assess_res.target_resolved:
            return _fail(
                f"Target dependency resolution failed: {assess_res.target_error or 'Dependency conflict'}",
                hint="Check that packages support the target Python version.",
            )

        target_manifest = assess_res.target_manifest_path or "preview only — not written"
        degrade_warnings: list[str] = []
        try:
            migrations, source_desc = resolve_codemod_migrations(
                str(path),
                assess_result=assess_res,
                config=config,
                warnings=degrade_warnings,
            )
        except Exception:
            migrations = []

        effective_endpoint = load_endpoint(endpoint)
        by_pkg, previews = preview_codemods(
            str(path), migrations, base_url=effective_endpoint, auto_apply_only=True
        )

        total_patterns = sum(len(items) for items in by_pkg.values())
        changed_files = [
            preview.path for preview in previews
            if preview.old_source != preview.new_source
        ]
        full_data = {
            "target_python": target_py,
            "target_manifest": target_manifest,
            "packages_assessed": len(assess_res.rows),
            "upgrades": len(migrations),
            "codemods_count": total_patterns,
            "files_changed": changed_files,
            "write": write,
        }
        full_path = _write_full_report(path, "migrate", full_data)

        summary = (
            f"Migrate ({'APPLIED' if write else 'PREVIEW'}):\n"
            f"Target Python: {target_py} | Assessed: {len(assess_res.rows)} packages | "
            f"Upgrades: {len(migrations)} | Codemods: {total_patterns} across {len(changed_files)} file(s)."
        )
        return _ok(
            summary,
            full_data,
            full_report_path=full_path,
            hint="Capture baseline, then create a plan with `pymolt plan .` and apply it with `pymolt apply .`.",
        )
    except Exception as e:  # noqa: BLE001
        return _fail(f"migrate failed: {e}", hint="Ensure project directory and dependencies are accessible.")


# ─────────────────────────────────────────────────────────────────────────────
# 9. env_hint
# ─────────────────────────────────────────────────────────────────────────────


@mcp.tool()
def env_hint(
    project_dir: str = ".",
    target_python: str | None = None,
    manifest: str | None = None,
) -> dict:
    """Suggests how to build a runnable target-Python environment (a derived Dockerfile
    and/or `uv venv` + `uv pip install` commands) from the assess target manifest — run
    assess first. This is a SUGGESTION only: pymolt never builds the environment, it only
    tells you what to build. Build it yourself, then point pymolt at it (`pymolt setup`'s
    target environment prompt, or --container on contract capture).

    Args:
        project_dir: Project directory (default: current directory).
        target_python: Target Python (e.g. "3.13"); falls back to setup config.
        manifest: Target manifest path; defaults to requirements-target.txt.

    Use after assess, before contract_capture(when='post-migration'). Runs nothing — no
    exec opt-in required.
    """
    try:
        path = _resolve_dir(project_dir)
        from pymolt.ingestion.config import EnvConfig
        from pymolt.setup.target_hint import build_target_hint

        cfg = EnvConfig.load(path / ".pymolt" / "env_config.json")
        effective_target_python = target_python or (cfg.target_python if cfg else None)
        if not effective_target_python:
            return _fail(
                "target_python is required (no --target-python given and none configured).",
                hint="Run `pymolt setup` or `pymolt assess` first, or pass target_python "
                     "explicitly.",
            )

        effective_manifest = manifest
        if effective_manifest is None:
            default_target_manifest = path / "requirements-target.txt"
            if default_target_manifest.is_file():
                effective_manifest = default_target_manifest.name
            elif cfg is not None and cfg.selected_manifest:
                effective_manifest = cfg.selected_manifest

        hint = build_target_hint(path, effective_target_python, manifest=effective_manifest)
        full = hint.model_dump(mode="json")
        full_path = _write_full_report(path, "env_hint", full)

        data: dict[str, Any] = {
            "target_python": hint.target_python,
            "manifest": hint.manifest,
            "suggested_dockerfile": hint.suggested_dockerfile,
            "suggested_uv_commands": hint.suggested_uv_commands,
            "notes": hint.notes,
        }
        summary_lines = [
            f"Target-env suggestion for Python {hint.target_python} (build it yourself)."
        ]
        if hint.suggested_dockerfile:
            summary_lines.append("Derived Dockerfile suggested (see full_report_path).")
        if hint.notes:
            summary_lines.append("Notes: " + "; ".join(hint.notes))
        return _ok(
            "\n".join(summary_lines),
            data,
            full_report_path=full_path,
            hint=hint.capture_hint,
        )
    except Exception as e:  # noqa: BLE001
        return _fail(
            f"env_hint failed: {e}",
            hint="Run assess first so a target manifest exists, or pass target_python explicitly.",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────


def run() -> None:
    """Run the stdio MCP server (blocking)."""
    mcp.run("stdio")
