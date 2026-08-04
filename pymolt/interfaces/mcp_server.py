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
PyMolt migration funnel as tools. Workflow:
  scan  ->  (setup_options -> setup_apply)  ->  assess  ->  env_hint
  ->  [engineer builds the target env themselves; human/agent edits the project's code]
  ->  contract_capture(when='baseline' BEFORE editing, when='post-migration' AFTER)
  ->  contract_report  (repeat edits + post-migration capture until no result_changed)
codemods_preview is a DRY RUN: it never applies changes and never spends money.
Purchases / applying codemods always require explicit human approval via the CLI,
outside this server. Prefer these tools over reading Dockerfiles, lockfiles, or
running tracers by hand.
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
# 1. scan
# ─────────────────────────────────────────────────────────────────────────────


@mcp.tool()
def scan(project_dir: str = ".") -> dict:
    """Reconnaissance of a repo: project roots, Python-version evidence + divergence,
    and dependency edges — offline, no resolution. Call this instead of reading
    Dockerfiles, .python-version, tox.ini, or lockfiles by hand.

    Args:
        project_dir: Project/repo directory to scan (default: current directory).

    Use first, before setup/assess, to understand what you are migrating.
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
    """Resolve baseline vs target dependency graphs and compare them: per-package
    upgrades/blockers/conflicts, and where the pinned target manifest is written.
    Call this instead of running uv/pip resolutions by hand.

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

        rows_capped, rows_trunc = _cap_list(rows_sorted)
        pkg_rows = []
        for r in rows_capped:
            row = {
                "name": r["name"],
                "baseline": r["baseline_version"],
                "target": r["target_version"],
                "status": r["status"],
            }
            if risk:
                row["risk_tier"] = risk_by_pkg.get(r["name"])
            pkg_rows.append(row)

        counts = result.counts_by_status()
        # run_assess writes the pinned target manifest on a successful resolve and
        # records its path — no need to probe the filesystem.
        target_manifest = result.target_manifest_path

        data: dict[str, Any] = {
            "target_python": effective_target,
            "target_resolved": result.target_resolved,
            "target_error": result.target_error,
            "packages": pkg_rows,
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
            f"conflicts: {counts.get('conflict', 0)}, manual-zone: {len(result.manual_zone)}."
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
    """Static contact map: every place your code calls into a third-party dependency
    (cheap, no runtime). Call this instead of grepping imports by hand.

    Args:
        project_dir: Project directory (default: current directory).

    Use to see the denominator the contract report measures dynamic coverage against.
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
        per_dep_capped, per_dep_trunc = _cap_list(per_dep)
        data: dict[str, Any] = {
            "total_contacts": len(cmap.contacts),
            "files_scanned": cmap.files_scanned,
            "per_dependency": [{"dep": d, "count": c} for d, c in per_dep_capped],
        }
        if per_dep_trunc:
            data["truncated"] = per_dep_trunc

        summary = (
            f"{len(cmap.contacts)} static contact(s) across {len(cmap.by_dep)} "
            f"dependency(ies), {cmap.files_scanned} file(s) scanned."
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
    """The verification oracle: static contact map × dynamic traces → confirmed /
    BLIND coverage + trust %, plus the behavioral verdict (which dependency
    results/raises changed, which disappeared) across the migration. Call this to
    decide whether a migration is behaviorally safe.

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

        # Behavioral verdicts + NEEDS_HUMAN come from the baseline->post BoundaryDiff.
        # rep.diff carries only counts; rebuild the diff for the symbol lists,
        # mirroring how the CLI report auto-sources the two named slots.
        state = service.load_contract_state(str(path))
        post_captured = state.post_migration is not None
        if rep.diff is not None and state.baseline is not None and state.post_migration is not None:
            bdiff = build_boundary_diff(state.baseline.trace_path, state.post_migration.trace_path)

            def _names(entries: list[dict]) -> tuple[list[str], int]:
                names = [e.get("qualname", "?") for e in entries]
                return _cap_list(names)

            rc, rc_t = _names(bdiff.result_changed)
            rk, rk_t = _names(bdiff.raise_changed)
            dp, dp_t = _names(bdiff.disappeared)
            nh, nh_t = _names(bdiff.skipped_opaque)
            data["behavioral"] = {
                "result_changed": rc,
                "raise_changed": rk,
                "disappeared": dp,
                "needs_human": nh,
                "clean": bdiff.is_clean(),
            }
            for key, trunc in (("result_changed", rc_t), ("raise_changed", rk_t),
                               ("disappeared", dp_t), ("needs_human", nh_t)):
                if trunc:
                    data["behavioral"][f"{key}_truncated"] = trunc

        summary_lines = [
            f"Static {rep.static_targets}, confirmed {rep.confirmed}, BLIND {rep.blind}, "
            f"dynamic-only {rep.dynamic_only}.",
            f"Contract trust: {trust_pct}%.",
        ]
        behavioral = data.get("behavioral")
        if behavioral is not None:
            summary_lines.append(
                f"Behavior: result_changed {len(behavioral['result_changed'])}, "
                f"raise_changed {len(behavioral['raise_changed'])}, "
                f"disappeared {len(behavioral['disappeared'])} "
                f"({'clean' if behavioral['clean'] else 'CHANGED'})."
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
# 8. codemods_preview
# ─────────────────────────────────────────────────────────────────────────────


@mcp.tool()
def codemods_preview(project_dir: str = ".", axiom_url: str = "http://localhost:8000") -> dict:
    """DRY-RUN preview of the codemods that would rewrite your call sites for the
    upgrades assess found. This NEVER writes files and NEVER spends slots/money —
    applying requires the human-driven CLI. Call this to see the blast radius.

    Args:
        project_dir: Project directory (default: current directory).
        axiom_url: Axiom Graph service base URL (default: http://localhost:8000).

    Requires a prior assess (its cached comparison feeds the migration set).
    """
    try:
        path = _resolve_dir(project_dir)
        from pymolt.codemods.models import CodemodPattern
        from pymolt.codemods.service import preview_codemods, resolve_codemod_migrations
        from pymolt.ingestion.config import EnvConfig

        cfg = EnvConfig.load(path / ".pymolt" / "env_config.json")
        config = cfg.model_dump(mode="json") if cfg else None
        try:
            migrations, source_desc = resolve_codemod_migrations(str(path), config=config)
        except ValueError as exc:
            return _fail(
                f"No migration set available: {exc}",
                hint="Run assess first so codemods can derive the changed packages.",
            )

        by_pkg, previews = preview_codemods(str(path), migrations, base_url=axiom_url)

        # Files that would change, unique across previews.
        changed_files = sorted({p.path for p in previews})
        files_capped, files_trunc = _cap_list(changed_files)

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
        per_package_capped, pkg_trunc = _cap_list(per_package)

        full = {
            "source": source_desc,
            "per_package": per_package,
            "files_that_would_change": changed_files,
        }
        full_path = _write_full_report(path, "codemods_preview", full)

        data: dict[str, Any] = {
            "dry_run": True,
            "source": source_desc,
            "per_package": per_package_capped,
            "files_that_would_change": files_capped,
        }
        if pkg_trunc:
            data["per_package_truncated"] = pkg_trunc
        if files_trunc:
            data["files_truncated"] = files_trunc

        summary = (
            "DRY-RUN preview only — no files written, no money/slots spent.\n"
            f"{len(per_package)} package(s) from {source_desc}; "
            f"{len(changed_files)} file(s) would change."
        )
        return _ok(
            summary,
            data,
            full_report_path=full_path,
            hint="Applying is human-only: review, then run "
                 "`pymolt codemods ... --write` in the CLI.",
        )
    except Exception as e:  # noqa: BLE001
        return _fail(
            f"codemods_preview failed: {e}",
            hint="Is the Axiom Graph service reachable at axiom_url? Run assess first.",
        )


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
