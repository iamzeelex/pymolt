"""Optional stdio MCP adapter for shell-less agent clients (``pymolt[mcp]``).

Each tool returns exactly what ``pymolt <command> --agent`` prints: the same service
call, then the same builder in :mod:`pymolt.interfaces.views`. No analysis lives
here and no tool writes source. Exceptions come back as ``{"ok": false, ...}``.
Without the extra, the ``mcp`` import raises and ``pymolt mcp`` says what to install.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from pymolt.interfaces import views

# Every `project_dir` must resolve to this root or below it (PYMOLT_MCP_ROOT, else cwd).
_ROOT_ENV_VAR = "PYMOLT_MCP_ROOT"
_WORKSPACE_ROOT = (
    Path(os.environ[_ROOT_ENV_VAR]).expanduser().resolve()
    if os.environ.get(_ROOT_ENV_VAR)
    else Path.cwd().resolve()
)

# A tool that spawns a host process (the agent-supplied command) is opt-in only.
_EXEC_ENV_VAR = "PYMOLT_MCP_ALLOW_EXEC"

_INSTRUCTIONS = """\
PyMolt proves whether a Python migration changed your program's behaviour.
The tools have the names of the pymolt CLI commands and return what
`pymolt <command> --agent` prints.

Order (status says where the project is and which tool is next):
  scan -> setup -> assess -> capture(when='baseline')
  -> plan, or change the code -> capture(when='post-migration') -> verify
capture needs setup. Repeat code changes + post-migration capture until verify
says PASS. A successful assess is not evidence the program still works.

plan saves the codemods as the migration plan and returns the diffs; it writes no
source file. A person applies it with `pymolt apply` in the project.
"""

mcp = FastMCP("pymolt", instructions=_INSTRUCTIONS)


def _resolve_dir(project_dir: str) -> Path:
    """``project_dir`` as a resolved directory inside the workspace root, or AgentInputError."""
    path = Path(project_dir).expanduser().resolve()
    if path != _WORKSPACE_ROOT and _WORKSPACE_ROOT not in path.parents:
        raise views.AgentInputError(
            f"Path {path} is outside the workspace root {_WORKSPACE_ROOT}",
            f"Paths must be inside the root (set {_ROOT_ENV_VAR} to change it).",
        )
    if not path.exists():
        raise views.AgentInputError(f"no such directory: {path} (it does not exist)",
                                    "Check the path: pymolt takes the project root.")
    if not path.is_dir():
        raise views.AgentInputError(f"not a directory: {path}",
                                    "pymolt operates on a project root, not a single file.")
    return path


def _require_exec_allowed() -> dict | None:
    """None when PYMOLT_MCP_ALLOW_EXEC is truthy; otherwise the failure to return."""
    if os.environ.get(_EXEC_ENV_VAR, "").strip().lower() in ("1", "true", "yes"):
        return None
    return views.failure(
        "Execution is disabled: capture runs your command on this host, which is opt-in.",
        f"Set {_EXEC_ENV_VAR}=1 to enable it, and connect this server only to trusted "
        "clients.",
    )


def _run(tool: str, project_dir: str, build: Callable[[Path], dict], hint: str) -> dict:
    try:
        return build(_resolve_dir(project_dir))
    except views.AgentInputError as exc:
        return views.failure(exc.error, exc.hint)
    except Exception as exc:  # noqa: BLE001 - never cross the MCP boundary
        return views.failure(f"{tool} failed: {exc}", hint)


@mcp.tool()
def status(project_dir: str = ".") -> dict:
    """Where the project stands in the migration, whether the configured environments
    exist, and the one command to run next. Call it anytime to orient.
    """
    from pymolt.status import build_status

    return _run("status", project_dir,
                lambda p: views.status_view(build_status(p, check_environments=True)),
                "Ensure project_dir is a readable directory.")


@mcp.tool()
def scan(project_dir: str = ".") -> dict:
    """Find EVERY project root in the repo, with its manifests, Python-version
    evidence and dependency edges, and every call site where the code enters a
    third-party dependency. Offline: no resolution, no runtime.

    Assume a monorepo until this says otherwise: a nested directory whose only
    marker is its own requirements.txt is a separate root with its own dependencies,
    the kind missed when walking the tree for setup.py/pyproject.toml. It also
    reports roots that disagree about the target Python.

    Call sites are resolved through imports and scopes (LibCST). Do not grep
    imports instead: an import names a module, not the symbols called, and
    undercounts the contact surface severalfold. Run it before setup/assess, and
    before answering what the project depends on.
    """
    from pymolt.scan import run_scan

    return _run("scan", project_dir, lambda p: views.scan_view(run_scan(p), p),
                "Check that project_dir is a readable directory.")


@mcp.tool()
def setup(project_dir: str = ".", manifest: str = "", tool: str = "",
          base_python: str = "", target_python: str = "",
          container_id: str | None = None, target_env: str | None = None) -> dict:
    """Choose the manifest, resolution tool and Python versions; saves them to
    .pymolt/env_config.json, which every later tool reads.

    With no choices it returns the valid ones (manifests, tools, interpreters,
    target Pythons, defaults) and saves nothing. With manifest, tool, base_python
    and target_python it checks and saves them. An invalid value returns ok:false
    naming the valid ones. Do not guess the strings.

    Args:
        manifest: Manifest file name, from the returned manifests.
        tool: Resolution tool, from the returned tools (uv/conda/poetry/container/system).
        base_python: The project's current Python (e.g. "3.6").
        target_python: Python to migrate to, from the returned target_python_options.
        container_id: Docker container id; required only when tool == "container".
        target_env: Path of an existing target environment (venv), if one is built.
    """
    def build(p: Path) -> dict:
        from pymolt.setup.service import apply_setup, gather_setup_options

        options = gather_setup_options(p)
        choices = views.setup_choices(
            options, manifest=manifest or None, tool=tool or None,
            base_python=base_python or None, target_python=target_python or None,
            container_id=container_id, target_env=target_env)
        notes: list[str] = []
        config = apply_setup(p, choices, notes=notes) if choices is not None else None
        return views.setup_view(options, p, config=config, side_effects=notes)

    return _run("setup", project_dir, build, "Call setup with no choices for valid values.")


@mcp.tool()
def assess(project_dir: str = ".", target_python: str | None = None,
           risk: bool = False, no_cache: bool = False) -> dict:
    """Resolve base vs target dependency graphs and compare them: which versions
    move, which conflict, what enters the manual zone, and the pinned target
    manifest it writes. Records the honesty markers (fixation, resolution quality,
    base tier) a hand-run resolve loses. Requires `uv` on PATH; use after setup.

    A successful resolve is an INPUT to the migration, not evidence it worked.
    Never call the migration "feasible" from this tool: cite `baseline_tier` and the
    verdict, and get behavioural proof from capture + verify.

    Args:
        target_python: Target Python (e.g. "3.13"); defaults to setup's.
        risk: Also rate each package (CVEs, wheels, abandonment). NETWORK, cached.
        no_cache: Resolve again instead of using the cache.

    On a successful resolve `target_env` says how to build the target environment
    (uv commands, a Dockerfile derived from the project's); pymolt builds nothing.
    """
    def build(p: Path) -> dict:
        from pymolt.assess.service import run_assess
        from pymolt.interfaces.cli.commands import _load_or_autodetect_assess_config

        config = _load_or_autodetect_assess_config(p, quiet=True)
        target = target_python or config.get("target_python")
        if not target:
            raise views.AgentInputError("No target_python given and none configured.",
                                        "Pass target_python (e.g. '3.13') or run setup first.")
        result = run_assess(
            str(p), target_python=target, config=config,
            base_python=config.get("base_python"), container_id=config.get("container_id"),
            use_cache=not no_cache, tool=config.get("selected_tool") or "uv", with_risk=risk,
        )
        return views.assess_view(result, p, risk_requested=risk, **views.assess_hints(p, result))

    return _run("assess", project_dir, build,
                "Ensure `uv` is on PATH and setup is configured; try no_cache=true.")


_CAPTURE_WHEN = {"baseline": "baseline", "post-migration": "post_migration"}


@mcp.tool()
def capture(project_dir: str = ".", when: str = "baseline", mode: str = "tests",
            command: list[str] | None = None, force: bool = False,
            impact_targets: list[str] | None = None, deployment_id: str | None = None,
            request_id: str | None = None, correlation_id: str | None = None,
            accept_failing: bool = False) -> dict:
    """Run YOUR command with the boundary tracer injected (zero edits to the project)
    and keep what the code called in its dependencies as the baseline or
    post-migration capture. Call this instead of wiring a tracer yourself.

    It runs in the environment setup saved for that side (the configured container
    for the baseline, the target env after), else locally. `ok: false` means the run
    cannot stand as evidence (failed command, empty trace); the hint follows the cause.

    Args:
        when: "baseline" (BEFORE migrating) or "post-migration" (AFTER).
        mode: "tests" (your suite) or "command" (your app).
        command: The command to run, as a list (e.g. ["pytest", "tests/"]).
        force: Replace an existing capture for this side (it is archived, not deleted).
        impact_targets: API symbols to focus on; post-migration defaults to the applied plan.
        deployment_id, request_id, correlation_id: Identities stored for provenance.
        accept_failing: Keep a run that exited non-zero if it recorded calls (stamped red).
    """
    gate = _require_exec_allowed()
    if gate is not None:
        return gate

    def build(p: Path) -> dict:
        from pymolt.verify import service
        from pymolt.verify.models import CaptureMode

        when_key = _CAPTURE_WHEN.get(when)
        capture_mode = {"tests": CaptureMode.TEST_SUITE, "command": CaptureMode.LIVE_COMMAND}
        if when_key is None or mode not in capture_mode or not command:
            raise views.AgentInputError(
                f"Invalid capture request (when={when!r}, mode={mode!r}, command={command!r}).",
                "when: baseline | post-migration; mode: tests | command; command: a list.")
        if service.environment_fingerprint(p) is None:
            raise views.AgentInputError("pymolt is not configured for this project.",
                                        "Run setup first: a capture records its environment.")
        existing = getattr(service.load_contract_state(p), when_key)
        if existing is not None and not force:
            raise views.AgentInputError(
                f"'{when}' already has a capture from {existing.captured_at}.",
                "Pass force=true to replace it.")
        slot = service.capture_named_trace(
            p, when_key, capture_mode[mode], command=list(command), impact_targets=impact_targets,
            deployment_id=deployment_id, request_id=request_id, correlation_id=correlation_id,
            accept_failing=accept_failing,
        )
        return views.capture_view(slot, when=when, state=service.load_contract_state(p),
                                  project_dir=p)

    return _run("capture", project_dir, build,
                "Ensure the command runs from project_dir and exits (e.g. a test suite).")


@mcp.tool()
def plan(project_dir: str = ".", endpoint: str | None = None) -> dict:
    """Save the code changes for the target manifest as the migration plan and
    return them as diffs. Writes .pymolt/migration_plan.json and no source file.

    The changes are LibCST codemods for the dependency versions assess pinned. Only
    `pymolt apply`, run in the project by a person, writes them; it refuses the plan
    if a planned file changed since. `advisories` are sites that need a person.
    Use after setup, assess and the baseline capture. `endpoint`: codemod endpoint
    URL (default: the configured one).
    """
    def build(p: Path) -> dict:
        from pymolt.codemods.service import PlanInputError, build_migration_plan
        from pymolt.config import load_endpoint

        try:
            migration_plan, _, source_desc, by_pkg = build_migration_plan(
                p, base_url=load_endpoint(endpoint))
        except PlanInputError as exc:
            raise views.AgentInputError(str(exc), exc.hint or None) from exc
        return views.plan_view(migration_plan, by_pkg, source_desc,
                               plan_path=migration_plan.save_planned(p))

    return _run("plan", project_dir, build,
                "Check that setup and assess ran and the codemod endpoint answers.")


@mcp.tool()
def verify(project_dir: str = ".") -> dict:
    """The verdict on whether the migration changed your program's behaviour: PASS,
    FAIL or INCONCLUSIVE, with the reasons, the coverage of the static contact map
    (confirmed / BLIND; coverage and recall %), and which dependency calls now return or raise
    something else, or stopped happening.

    No file reading, type checking or dependency resolution answers this; it needs
    the baseline and post-migration captures. A green test suite is not a
    substitute: it does not tell you a call started returning an empty generator.
    Same verdict as `pymolt verify`, saved to .pymolt/ so status counts it.
    """
    def build(p: Path) -> dict:
        from pymolt.migration_state import MigrationReceipt
        from pymolt.verify import service
        from pymolt.verify.unified import build_unified_verification

        receipt = MigrationReceipt.load(p)
        unified = build_unified_verification(
            service.build_contract_report_from_state(p),
            run_id=receipt.run_id if receipt is not None else None,
        )
        return views.verify_view(unified, p, saved_path=unified.save(p))

    return _run("verify", project_dir, build, "Capture at least a baseline first.")


def run() -> None:
    """Run the stdio MCP server (blocking)."""
    mcp.run("stdio")
