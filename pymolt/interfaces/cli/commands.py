import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from pymolt.assess.service import (
    _resolve_baseline_graph,
    baseline_tests_passed,
    classify_baseline_tier,
    compatibility_verdict,
)
from pymolt.assess.service import build_comparison_rows as _build_comparison_rows
from pymolt.assess.service import resolve_chosen_source as _service_resolve_chosen_source
from pymolt.assess.service import resolve_target_graph as _resolve_target_graph
from pymolt.assess.service import write_target_manifest as _write_target_manifest
from pymolt.interfaces.cli.output import (
    EXIT_ENVIRONMENT,
    console,
    err,
    fail,
    next_step,
    require_project_dir,
    warn,
)
from pymolt.interfaces.cli.verify_cmd import app as verify_app
from pymolt.setup.pythons import (
    classify_eol,
    get_available_python_versions,
    get_eol_versions,
    version_floor,
)

app = typer.Typer(help="pymolt: Python migration orchestrator CLI", invoke_without_command=True)


def _version_callback(value: bool) -> None:
    if value:
        from importlib.metadata import version

        try:
            console.print(f"pymolt {version('pymolt')}")
        except Exception:
            from pymolt import __version__

            console.print(f"pymolt {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def _default(
    ctx: typer.Context,
    version: bool = typer.Option(
        False, "--version", "-V", callback=_version_callback, is_eager=True,
        help="Show the pymolt version and exit.",
    ),
) -> None:
    """Bare `pymolt` (no subcommand) displays help and usage."""
    if ctx.invoked_subcommand is not None:
        return
    console.print(ctx.get_help())


def _looks_like_python_version(value: str) -> bool:
    """`3`, `3.12`, `3.12.1` — and nothing else."""
    parts = value.split(".")
    return 1 <= len(parts) <= 3 and all(p.isdigit() for p in parts)


def _ask_until_valid(prompt: str, default: str, accept, complaint: str) -> str:
    """Re-ask until the answer is one pymolt can act on.

    Falling back to the default on unrecognized input is how the literal string
    `q` ended up saved as a project's base Python: the answer was discarded in
    silence, and the config that came out looked deliberate. An unusable answer
    has to be named as unusable — every later phase reads this file.
    """
    while True:
        answer = str(typer.prompt(prompt, default=default)).strip()
        resolved = accept(answer)
        if resolved is not None:
            return resolved
        err.print(f"[bold red]{complaint}[/bold red] [dim](got {answer!r})[/dim]")


def _ask_from_list(prompt: str, names: list[str], default: str) -> str:
    """Pick one of `names` by 1-based number or by exact name."""
    def accept(answer: str) -> str | None:
        if answer.isdigit() and 1 <= int(answer) <= len(names):
            return names[int(answer) - 1]
        return next((n for n in names if n.lower() == answer.lower()), None)

    return _ask_until_valid(
        prompt, default, accept,
        f"Enter a number 1-{len(names)} or one of: {', '.join(names)}.",
    )


def _ask_python_version(prompt: str, default: str) -> str:
    def accept(answer: str) -> str | None:
        return answer if _looks_like_python_version(answer) else None

    return _ask_until_valid(
        prompt, default, accept, "That is not a Python version (expected e.g. 3.11 or 3.6.1)."
    )


def _select_target_python_interactive(
    floor: list[int], default_basis: str | None, eol_map: dict[str, str]
) -> str:
    """List available target Pythons (>= floor) with EOL hints and prompt for one.

    The default comes from :func:`pymolt.setup.build_target_options` (shared
    across interfaces): the lowest non-EOL version — never a dead Python. Shared by
    ``setup`` and ``assess`` so the picker stays consistent.
    """
    versions = [
        v for v in get_available_python_versions()
        if [int(p) for p in v.split(".")] >= floor
    ]

    console.print("[bold yellow]Available Target Python Versions:[/bold yellow]")
    for i, v in enumerate(versions, 1):
        console.print(f"  {i}. {v}{format_eol_status(v, eol_map)}")

    default_choice = "1"
    if default_basis:
        from pymolt.setup import build_target_options

        default_version = next(
            (o.version for o in build_target_options(default_basis) if o.is_default), None
        )
        if default_version in versions:
            default_choice = str(versions.index(default_version) + 1)

    def accept(answer: str) -> str | None:
        if answer.isdigit() and 1 <= int(answer) <= len(versions):
            return versions[int(answer) - 1]
        # A well-formed version that is not on the list is still allowed (uv may
        # not list every interpreter) — but it is a choice, not a typo, so say so.
        if _looks_like_python_version(answer):
            if answer not in versions:
                warn(f"Python {answer} is not in the available list — "
                     "assess will fail if it cannot be provisioned.")
            return answer
        return None

    return _ask_until_valid(
        "Select target Python version (enter number or version string)",
        default_choice, accept,
        f"Enter a number 1-{len(versions)} or a version like 3.12.",
    )

# Phase 4: the behavioral contract — static contact map + dynamic capture + diff, all outside the target.
app.add_typer(verify_app, name="contract", rich_help_panel="The migration funnel")

# Target-env provisioner: turns an assess result into a runnable migration environment.
env_app = typer.Typer(
    help="Suggest a runnable target-Python environment (uv venv or Dockerfile) — builds nothing."
)
app.add_typer(env_app, name="env", rich_help_panel="Tools & account")


_STATE_MARK = {
    "done": ("✓", "green"),
    "todo": ("○", "dim"),
    # Not an error colour: work that no longer applies is a fact to act on, not a
    # failure. But it must not read as "done" either.
    "stale": ("!", "yellow"),
    "blocked": ("·", "yellow"),
}


@app.command(rich_help_panel="The migration funnel")
def status(
    project_dir: str = typer.Argument(".", help="The project directory to report on"),
    json_output: bool = typer.Option(False, "--json", help="Emit the funnel status as JSON"),
):
    """Where this migration stands, and the one command to run next.

    Reads only what is already on disk (config, captures, target manifest) —
    runs nothing, writes nothing. The funnel is projected, never enforced: every
    command stays runnable in any order.
    """
    import json as json_lib

    from pymolt.status import build_status

    project_path = require_project_dir(project_dir, json_output=json_output)
    report = build_status(project_path)

    if json_output:
        typer.echo(json_lib.dumps(report.model_dump(mode="json"), indent=2))
        return

    console.print(f"[bold]pymolt[/bold] [dim]{report.project_dir}[/dim]\n")
    for phase in report.phases:
        glyph, colour = _STATE_MARK.get(phase.state, ("○", "dim"))
        line = f"  [{colour}]{glyph}[/{colour}] [bold]{phase.phase:<15}[/bold]"
        detail = phase.blocker or phase.detail
        console.print(f"{line} [{'dim' if phase.state == 'todo' else colour}]{detail}[/]")

    for note in report.notes:
        console.print(f"  [dim]note: {note}[/dim]")

    console.print()
    if report.next_command:
        console.print(Panel(
            f"[white]{report.next_command}[/white]\n[dim]{report.next_reason}[/dim]",
            title="next", title_align="left", border_style="green", expand=False,
        ))
    else:
        console.print(f"[yellow]{report.next_reason}[/yellow]")


@app.command(rich_help_panel="The migration funnel")
def scan(
    project_dir: str = typer.Argument(".", help="The repo/project directory to scan"),
    json_output: bool = typer.Option(False, "--json", help="Emit the full scan as JSON on stdout"),
):
    """Phase 1 — the primary as-is scan: surfaces, version divergence, and dependency edges (offline, no resolution)."""
    from pymolt.scan import run_scan

    project_path = require_project_dir(project_dir, json_output=json_output)
    report = run_scan(project_path)
    if json_output:
        import json as json_lib
        typer.echo(json_lib.dumps(report.model_dump(mode="json"), indent=2))
        return

    if not report.surfaces.project_roots:
        err.print(f"[bold yellow]No Python project roots found under {project_path}.[/bold yellow]")
        return

    _render_surface_map(report.surfaces)

    # Per-root dependency edges (folded-in inventory).
    for root_path, inv in report.inventory_by_root.items():
        if not inv.edges and not inv.indexes:
            continue
        console.print(f"\n[bold]Dependency edges — [cyan]{root_path}[/cyan][/bold]")
        _render_inventory(inv)

    next_step(project_path)


@app.command(rich_help_panel="Tools & account", hidden=True, deprecated=True)
def init():
    """Deprecated: `pymolt setup` creates everything this used to.

    It only ever made a cache directory — in the *current* working directory, not
    the project's — which setup and the phases now do for themselves.
    """
    err.print(
        "[yellow]`pymolt init` is deprecated and does nothing.[/yellow] "
        "Start with [bold]pymolt status .[/bold] to see where you are, "
        "then [bold]pymolt setup .[/bold]."
    )


def _eol_hint(status: str, eol_date: str | None) -> str:
    """Rich-markup EOL hint from a precomputed (status, date)."""
    if status == "eol":
        return f" [red](EOL since {eol_date})[/red]"
    if status == "soon":
        return f" [yellow](EOL soon: {eol_date})[/yellow]"
    if status == "supported":
        return f" [green](Supported until {eol_date})[/green]"
    return ""


def format_eol_status(version: str, eol_map: dict[str, str]) -> str:
    """Rich-markup EOL hint for a version, built on the shared classifier."""
    eol_date, status = classify_eol(version, eol_map)
    return _eol_hint(status, eol_date)


def fetch_pypi_versions(package_name: str) -> list[str]:
    """Fetch the latest 5 release versions of a package from PyPI JSON API."""
    import json
    import urllib.request

    from packaging.version import parse as parse_version
    url = f"https://pypi.org/pypi/{package_name}/json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "PyMolt/1.0"})
        with urllib.request.urlopen(req, timeout=3) as response:
            data = json.loads(response.read().decode("utf-8"))
            releases = data.get("releases", {})
            versions = []
            for v in releases.keys():
                try:
                    parsed = parse_version(v)
                    if not parsed.is_prerelease and not parsed.is_devrelease:
                        versions.append((parsed, v))
                except Exception:
                    pass
            versions.sort(key=lambda x: x[0], reverse=True)
            return [v[1] for v in versions[:5]]
    except Exception:
        return []


@app.command(rich_help_panel="The migration funnel")
def setup(
    project_dir: str = typer.Argument(".", help="The target project directory"),
):
    """Configure the migration: pick manifest, toolset, and base/target Python (writes .pymolt/env_config.json)."""
    import sys

    from pymolt.ingestion.config import ToolChoice
    from pymolt.ingestion.detect import detect_local_environments
    from pymolt.setup import SetupChoices, apply_setup, gather_setup_options

    project_path = require_project_dir(project_dir)
    try:
        options = gather_setup_options(project_path)
    except Exception as e:
        fail(f"source detection failed: {e}", code=EXIT_ENVIRONMENT)

    if not options.manifests:
        fail(
            f"no Python dependency sources detected in {project_path}",
            hint="pymolt needs a manifest (requirements*.txt, pyproject.toml, "
                 "Pipfile, environment.yml, …) to configure a migration.",
        )

    # ── Display findings (CLI owns its rendering; the data is the shared model) ──
    console.print("[bold blue]pymolt setup - Initializing local configuration[/bold blue]")
    console.print(f"Scanning directory: {options.project_dir}\n")

    console.print("[bold cyan]Discovered Dependency Manifests:[/bold cyan]")
    for i, m in enumerate(options.manifests, 1):
        default_marker = " [green][default][/green]" if m.is_default else ""
        console.print(f"  {i}. {m.name} ({m.mode}, fixation: {m.fixation}){default_marker}")
    console.print()

    console.print("[bold cyan]Discovered Local Environments & System Tools:[/bold cyan]")
    console.print(
        f"  Local folders: {', '.join(options.local_envs) if options.local_envs else 'None detected'}"
    )
    tool_status = [
        f"{tool}: " + (f"[green]found[/green] ({path})" if path else "[red]not found[/red]")
        for tool, path in options.system_tools.items()
    ]
    console.print(f"  System tools : {', '.join(tool_status)}")
    console.print()

    if options.containers:
        console.print(
            "[bold cyan]Discovered Running Docker Containers (bind-mounting project):[/bold cyan]"
        )
        for c in options.containers:
            console.print(f"  Container {c.id} ({c.name}) - Python {c.python}")
        console.print()

    tool_names = [t.name for t in options.tools]
    container_id = None

    if sys.stdout.isatty():
        # 1. Manifest selection
        manifest_names = [m.name for m in options.manifests]
        default_manifest = options.default_manifest or manifest_names[0]
        selected_manifest = _ask_from_list(
            "Select manifest number or name as source of truth",
            manifest_names, str(manifest_names.index(default_manifest) + 1),
        )

        # 2. Tool selection
        console.print("\n[bold yellow]Available resolution tools/environments:[/bold yellow]")
        for i, t in enumerate(options.tools, 1):
            default_marker = " [green][default][/green]" if t.is_default else ""
            console.print(f"  {i}. {t.name} ({t.detail}){default_marker}")
        selected_tool = _ask_from_list(
            "Select resolution tool", tool_names,
            str(tool_names.index(options.default_tool) + 1),
        )

        # Container selection if "container" chosen
        if selected_tool == "container":
            if options.containers:
                console.print("\n[bold yellow]Select a running Docker container:[/bold yellow]")
                for idx, c in enumerate(options.containers, 1):
                    default_c = " [green][default][/green]" if idx == 1 else ""
                    console.print(f"  {idx}. Container {c.id} ({c.name}){default_c}")
                c_choice = str(typer.prompt("Select container number", default="1")).strip()
                if c_choice.isdigit() and 1 <= int(c_choice) <= len(options.containers):
                    container_id = options.containers[int(c_choice) - 1].id
                else:
                    container_id = options.containers[0].id
            else:
                container_id = typer.prompt("Enter Docker container ID or name")

        # 3. Base Python version
        base_python = _ask_python_version(
            "Confirm or enter base Python version (e.g. 3.6)",
            options.base_python_default,
        )

        # 4. Target Python version — recompute the list against the chosen base.
        target_python = _select_target_python_interactive(
            version_floor(base_python), base_python, get_eol_versions()
        )

        # 5. Target environment status
        target_env_created = typer.confirm(
            "\nHave you already created the new target environment?", default=False
        )
        target_env_path = None
        if target_env_created:
            scanned_envs = detect_local_environments(project_path)
            if scanned_envs:
                console.print("\n[bold yellow]Select your target environment folder:[/bold yellow]")
                for idx, folder in enumerate(scanned_envs, 1):
                    console.print(f"  {idx}. {folder}")
                console.print(f"  {len(scanned_envs) + 1}. Other path...")
                env_choice = str(typer.prompt("Select target environment option", default="1")).strip()
                if env_choice.isdigit() and 1 <= int(env_choice) <= len(scanned_envs):
                    target_env_path = scanned_envs[int(env_choice) - 1]
                else:
                    target_env_path = typer.prompt("Enter the custom path to the target environment")
            else:
                target_env_path = typer.prompt(
                    "Enter the path/name of your target environment (e.g. .venv_target)"
                )
    else:
        # Non-interactive fallback: take the service's defaults.
        selected_manifest = options.default_manifest
        selected_tool = options.default_tool
        if selected_tool == "container" and options.containers:
            container_id = options.containers[0].id
        base_python = options.base_python_default
        target_python = f"{sys.version_info.major}.{sys.version_info.minor}"
        target_env_created = False
        target_env_path = None

    choices = SetupChoices(
        selected_manifest=selected_manifest,
        selected_tool=ToolChoice(selected_tool),
        base_python=base_python,
        # Only a value the user left at the detected default keeps its provenance;
        # anything they typed is a stated fact about the project.
        base_python_source=(
            options.base_python_source if base_python == options.base_python_default
            else "declared"
        ),
        target_python=target_python,
        container_id=container_id,
        target_env_created=target_env_created,
        target_env_path=target_env_path,
    )
    apply_setup(project_path, choices)

    config_file = project_path / ".pymolt" / "env_config.json"
    console.print(f"\n[bold green]✓ Configuration saved to {config_file}[/bold green]")
    console.print(Panel(
        f"Selected Manifest  : {selected_manifest}\n"
        f"Resolution Tool    : {selected_tool}\n"
        f"Base Python        : {base_python}\n"
        f"Container ID       : {container_id or 'None'}\n"
        f"Target Python      : {target_python}\n"
        f"Target Env Created : {target_env_created}\n"
        f"Target Env Path    : {target_env_path or 'None'}",
        title="Saved Configuration",
        expand=False
    ))
    next_step(project_path)


# ── assess helpers ────────────────────────────────────────────────────────────
# `assess` orchestrates these focused steps; each owns one concern (config,
# version resolution, rendering, overrides) so the command stays readable and
# the pieces are independently testable.


def _load_or_autodetect_assess_config(project_path: Path, quiet: bool = False) -> dict:
    """Load + validate ``.pymolt/env_config.json``, or synthesize a best-effort config.

    Returns the config as a plain dict (the validated :class:`EnvConfig` dumped to
    JSON-safe values) so existing key-based access keeps working. Degrades rather
    than aborting when the file is missing or invalid. ``quiet`` suppresses the
    warnings entirely — for callers with no stderr to speak of, like the MCP
    server, which returns the same facts as structured data instead.
    """
    from pymolt.ingestion.config import EnvConfig, ToolChoice
    from pymolt.ingestion.detect import (
        detect_project_python_version,
        detect_sources,
        detect_system_tools,
    )

    config_file = project_path / ".pymolt" / "env_config.json"
    if config_file.is_file():
        config = EnvConfig.load(config_file)
        if config is not None:
            return config.model_dump(mode="json")
        if not quiet:
            warn(f"failed to parse configuration file {config_file}; using defaults.")
        return EnvConfig().model_dump(mode="json")

    if not quiet:
        warn(
            "No environment configuration found — auto-detecting.",
            hint="Run 'pymolt setup' to pin the manifest, toolset and base/target Python.",
        )
    try:
        sources = detect_sources(project_path)
    except Exception:
        sources = []
    if sources:
        lock_sources = [s for s in sources if s.is_lock]
        selected_manifest = lock_sources[0].path.name if lock_sources else sources[0].path.name
    else:
        selected_manifest = None

    sys_tools = detect_system_tools()
    if sources and sources[0].mode == "conda":
        selected_tool = ToolChoice.CONDA
    elif sys_tools.get("uv"):
        selected_tool = ToolChoice.UV
    elif sys_tools.get("poetry"):
        selected_tool = ToolChoice.POETRY
    else:
        selected_tool = ToolChoice.SYSTEM

    return EnvConfig(
        selected_manifest=selected_manifest,
        selected_tool=selected_tool,
        base_python=detect_project_python_version(project_path),
    ).model_dump(mode="json")


def _resolve_chosen_source(sources, manifest_name_to_use, project_dir, json_output=False):
    """CLI wrapper: pick the manifest via the shared service, failing cleanly on error."""
    try:
        return _service_resolve_chosen_source(sources, manifest_name_to_use)
    except ValueError as e:
        fail(str(e), hint=f"(searched in '{project_dir}')", json_output=json_output)


def _resolve_target_python(
    target_python_to_use, detected_python, current_python,
    project_version_list, min_target_list, eol_map, today_str, quiet=False,
    json_output=False,
):
    """Resolve (prompting if needed), validate against downgrades, and warn on EOL."""
    if target_python_to_use is None:
        if not sys.stdout.isatty():
            fail(
                "no target Python: pass --target-python, or run 'pymolt setup' to "
                "configure one (this is required outside an interactive terminal).",
                json_output=json_output,
            )
        target_python_to_use = _select_target_python_interactive(
            min_target_list, detected_python or current_python, eol_map
        )

    # Reject downgrades.
    if target_python_to_use:
        try:
            target_list = [int(p) for p in target_python_to_use.split(".")]
            if target_list < project_version_list:
                fail(
                    f"target Python {target_python_to_use} is lower than the project's "
                    f"{detected_python or current_python} — downgrades are not supported.",
                    json_output=json_output,
                )
        except ValueError:
            pass

    # Warn on an EOL target.
    target_eol_date = eol_map.get(target_python_to_use)
    if target_eol_date and target_eol_date < today_str and not quiet:
        warn(
            f"target Python {target_python_to_use} is End-of-Life (since {target_eol_date}) "
            "— consider a supported version."
        )

    return target_python_to_use


def _render_assess_header(
    project_dir, chosen_source, python_display, legacy_interpreter_display,
    target_python_to_use, target_env_display, target_interpreter_display, comp_env,
):
    """Render the assess summary panel (source/legacy/target/compilation environment)."""
    console.print(Panel(
        f"Directory:          [yellow]{project_dir}[/yellow]\n"
        f"Source manifest:    [magenta]{chosen_source.path.name}[/magenta] "
        f"[dim](as declared: {chosen_source.fixation.value})[/dim]\n"
        "\n"
        "[bold cyan]Baseline (the project as it is today)[/bold cyan]\n"
        f"  Python:           [green]{python_display}[/green]\n"
        f"  Interpreter:      [dim]{legacy_interpreter_display}[/dim]\n"
        "\n"
        "[bold cyan]Target (what it is being migrated to)[/bold cyan]\n"
        f"  Python:           [green]{target_python_to_use}[/green]\n"
        f"  Env folder:       [blue]{target_env_display}[/blue]\n"
        f"  Interpreter:      [dim]{target_interpreter_display}[/dim]\n"
        "\n"
        f"Resolved in:        [blue]{comp_env}[/blue]",
        title="pymolt assess",
        expand=False,
    ))
    console.print()


_PROVENANCE_LABELS = {
    "pypi": "PyPI (Standard)",
    "conda-forge": "Conda-Forge",
    "conda-only": "Conda Only (No PyPI Twin)",
    "pip-in-conda": "Pip inside Conda",
}


#: status -> (glyph, style). The move itself lives in the "baseline → target"
#: column, so this only has to say what kind of move it is.
_STATUS_MARK = {
    "upgrade": ("▲ upgrade", "bold blue"),
    "added": ("✚ added", "cyan"),
    "conflict": ("⚠ conflict", "bold red"),
    "removed": ("⚠ removed", "red"),
    "unchanged": ("✔ same", "green"),
}


def _render_comparison_table(rows, verbose):
    """Format comparison rows into a rich Table; return (table, all_package_names).

    Five columns, not seven: at 80 cells the old layout truncated its own headers
    ("Depende…", "Constra…") and wrapped every value, while "Upgrade Status"
    restated the two versions already shown beside it. The move is one column now,
    and ecosystem origin — almost always plain PyPI — is shown only when it is not.
    """
    table = Table(title="Resolved Comparative Baseline")
    table.add_column("Package", style="bold cyan", no_wrap=True)
    table.add_column("Declared", style="green")
    table.add_column("Baseline → Target", style="blue", no_wrap=True)
    table.add_column("Role")
    table.add_column("Status")

    exotic_origin = any(
        r["origin"] and r["origin"] != "pypi"
        for r in rows if r["direct"] or verbose or r["manual_bridge"]
    )
    if exotic_origin:
        table.add_column("Origin", style="magenta")

    for row in rows:
        if not (row["direct"] or verbose or row["manual_bridge"]):
            continue

        role_str = "[bold yellow]direct[/bold yellow]" if row["direct"] else "[dim]transitive[/dim]"
        b_ver = row["baseline_version"] or "-"
        t_raw = row["target_version"]
        status = row["status"]

        if status == "conflict":
            move = f"{b_ver} → [red]conflict[/red]"
        elif status == "removed":
            move = f"{b_ver} → [red]—[/red]"
        elif status == "unchanged":
            move = b_ver                       # a version that did not move, said once
        elif status == "baseline":
            move = b_ver
        else:
            move = f"{b_ver} → {t_raw or 'unpinned'}"

        label, style = _STATUS_MARK.get(status, ("✔ resolved", "green"))
        cells = [row["name"], row["declared_constraint"] or "-", move, role_str,
                 f"[{style}]{label}[/{style}]"]
        if exotic_origin:
            cells.append(_PROVENANCE_LABELS.get(row["origin"], row["origin"] or "-"))
        table.add_row(*cells)

    return table, [row["name"] for row in rows]


def _render_risk(risk_report):
    """Render the migration-risk summary panel and a HIGH/MEDIUM findings table."""
    from pymolt.core.enums import RiskTier

    high = risk_report.by_tier(RiskTier.HIGH)
    medium = risk_report.by_tier(RiskTier.MEDIUM)
    low = risk_report.by_tier(RiskTier.LOW)
    open_cves = sum(len(p.open_cves) for p in risk_report.packages)
    needs_comp = sum(1 for p in risk_report.packages if p.needs_compilation)
    abandoned = sum(1 for p in risk_report.packages if p.abandoned)
    border = "red" if high else ("yellow" if medium else "green")

    summary = f"Assessed {risk_report.assessed} package(s)"
    if risk_report.skipped:
        summary += f", {risk_report.skipped} unpinned (skipped)"
    if risk_report.errors:
        summary += f", {risk_report.errors} unreachable"
    console.print(Panel(
        f"{summary}\n"
        f"[red]HIGH: {len(high)}[/red]   [yellow]MEDIUM: {len(medium)}[/yellow]   "
        f"[green]LOW: {len(low)}[/green]\n"
        f"Open CVEs: {open_cves}   Needs compilation: {needs_comp}   Likely abandoned: {abandoned}",
        title="Migration Risk Assessment",
        border_style=border,
    ))

    flagged = high + medium
    if not flagged:
        console.print("[bold green]✔ No HIGH/MEDIUM migration risks detected.[/bold green]")
        return

    table = Table(title="Risk findings (HIGH / MEDIUM)")
    table.add_column("Package", style="bold cyan")
    table.add_column("Target", style="blue")
    table.add_column("Tier")
    table.add_column("Why")
    tier_markup = {RiskTier.HIGH: "[bold red]HIGH[/bold red]", RiskTier.MEDIUM: "[yellow]MEDIUM[/yellow]"}
    for p in sorted(flagged, key=lambda x: (x.tier != RiskTier.HIGH, x.name)):
        table.add_row(p.name, p.target_version or "-", tier_markup[p.tier], "; ".join(p.reasons))
    console.print(table)


def _render_manual_zone(report):
    """Render the manual-zone panel (or the all-clear message)."""
    if report.manual_zone:
        console.print(Panel(
            f"[bold red]Manual Zone Alert ({len(report.manual_zone)} unresolved packages):[/bold red]\n"
            "The following packages require manual mapping configuration in "
            "[bold].pymolt/config.toml[/bold]:\n"
            f"[yellow]{', '.join(report.manual_zone)}[/yellow]\n\n"
            "This occurs when packages exist on Conda but have no direct metadata link "
            "to a PyPI twin package.",
            title="Manual Action Required",
            border_style="red",
        ))
    elif getattr(report, "resolution_quality", None) == "unresolved":
        # The baseline was never reconstructed, so there was nothing to map
        # twins *from*. Printing the all-clear here contradicted the panel at
        # the top of the same run and was the last thing the reader saw.
        console.print(
            "[yellow]No manual zone to resolve — there is no baseline to compare "
            "against (see above).[/yellow]"
        )
    else:
        console.print(
            "[bold green]✔ All package twins resolved automatically. "
            "No manual zone configuration required.[/bold green]"
        )


def _run_override_menu(config, config_file, baseline_graph, target_graph, all_packages):
    """Interactive per-package target-version override menu; mutates+persists config."""
    from pymolt.ingestion.config import EnvConfig

    while True:
        console.print("\n[bold yellow]Customize Target Version Overrides:[/bold yellow]")
        for idx, pkg_name in enumerate(all_packages, 1):
            b_node = baseline_graph.nodes.get(pkg_name)
            b_ver = b_node.version if (b_node and b_node.version) else "unpinned"
            t_node = target_graph.nodes.get(pkg_name) if target_graph else None
            t_ver = t_node.version if t_node else "-"
            override = config.get("target_overrides", {}).get(pkg_name, "")
            override_str = f" [cyan](override: {override})[/cyan]" if override else ""
            console.print(
                f"  {idx}. {pkg_name} (baseline: {b_ver}, resolved target: {t_ver}){override_str}"
            )

        choice_str = typer.prompt(
            "\nSelect package number to override (or press Enter/0 to finish)", default="0"
        ).strip()
        if not choice_str or choice_str == "0":
            break

        if choice_str.isdigit() and 1 <= int(choice_str) <= len(all_packages):
            pkg_to_override = all_packages[int(choice_str) - 1]
            current_override = config.get("target_overrides", {}).get(pkg_to_override, "")

            console.print(f"[dim]Fetching version suggestions for {pkg_to_override} from PyPI...[/dim]")
            suggestions = fetch_pypi_versions(pkg_to_override)
            if suggestions:
                console.print(f"[bold cyan]Suggestions: {', '.join(suggestions)}[/bold cyan]")
            else:
                console.print("[dim]No suggestions found on PyPI.[/dim]")

            new_ver_str = typer.prompt(
                f"Enter target version/constraint for {pkg_to_override} (empty to clear)",
                default=current_override,
            ).strip()

            config.setdefault("target_overrides", {})
            if new_ver_str:
                config["target_overrides"][pkg_to_override] = new_ver_str
            else:
                config["target_overrides"].pop(pkg_to_override, None)

            # Persist through the schema so the file stays valid and versioned.
            EnvConfig.model_validate(config).save(config_file)


@app.command(rich_help_panel="The migration funnel")
def assess(
    project_dir: str = typer.Argument(".", help="The target project directory"),
    target_python: str | None = typer.Option(None, help="Target Python version (e.g. 3.13)"),
    source_manifest: str | None = typer.Option(None, help="Explicit target source manifest to parse (e.g. requirements.txt)"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Show all transitive dependencies"),
    container: str | None = typer.Option(None, "--container", help="Specify Docker container ID or name to run compilation inside"),
    base_python: str | None = typer.Option(
        None, "--base-python", "--current-python",
        help="The project's own Python, for baseline resolution "
             "(--current-python is the old name for this)"),
    upgrade_constraint: str | None = typer.Option(None, "--upgrade-constraint", "-c", help="Path to a constraint file containing custom upgrade targets (e.g. pandas<2.1)"),
    json_output: bool = typer.Option(False, "--json", help="Emit the assessment as machine-readable JSON on stdout (suppresses tables/prompts)"),
    no_cache: bool = typer.Option(False, "--no-cache", help="Bypass the resolved-graph cache and re-resolve from scratch"),
    no_hashes: bool = typer.Option(False, "--no-hashes", help="Do not fetch PyPI hashes when writing the target requirements file"),
    no_write: bool = typer.Option(False, "--no-write", help="Answer only: do not write the pinned target manifest into the project"),
    risk: bool = typer.Option(False, "--risk", help="Assess migration risk per package (CVEs, wheels, abandonment) — opt-in, network"),
):
    """Assess feasibility against a target Python: resolve lock-first, compare baseline vs target, rank risk."""
    import sys

    from pymolt.ingestion.config import EnvConfig
    from pymolt.ingestion.detect import detect_sources
    from pymolt.ingestion.fallback_compiler import find_python_interpreter

    current_python = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    project_path = require_project_dir(project_dir, json_output=json_output)

    # 1. Load (or auto-detect) the environment configuration.
    config_file = project_path / ".pymolt" / "env_config.json"
    # Not gated on --json any more: diagnostics live on stderr, so an agent gets
    # pure JSON on stdout *and* keeps the signal that no config was found.
    config = _load_or_autodetect_assess_config(project_path)

    # Override config defaults with CLI overrides if provided
    manifest_name_to_use = source_manifest or config.get("selected_manifest")
    detected_python = base_python or config.get("base_python")
    container_id = container or config.get("container_id")
    target_python_to_use = target_python or config.get("target_python")

    # Detect all available manifests
    try:
        sources = detect_sources(project_path)
    except Exception as e:
        fail(f"source detection failed: {e}", code=EXIT_ENVIRONMENT, json_output=json_output)

    if not sources:
        fail(
            f"no Python dependency sources detected in {project_path}",
            hint="pymolt resolves from a manifest (requirements*.txt, pyproject.toml, "
                 "Pipfile, environment.yml, …); none was found here.",
            json_output=json_output,
        )

    # 2. Resolve the source manifest to assess.
    chosen_source = _resolve_chosen_source(
        sources, manifest_name_to_use, project_dir, json_output=json_output
    )

    # Resolve python versions & labels for display. A version the project never
    # declared is a fallback to pymolt's own interpreter — the label has to say so
    # wherever it came from, including out of a config that recorded the guess.
    base_python_assumed = False
    if base_python:
        python_display = f"{detected_python} (CLI override)"
    elif config.get("base_python"):
        base_python_assumed = config.get("base_python_source") == "assumed"
        origin = "assumed — pymolt's own" if base_python_assumed else "cached config"
        python_display = f"{detected_python} ({origin})"
    elif detected_python:
        python_display = f"{detected_python} (project config)"
    else:
        base_python_assumed = True
        python_display = f"{current_python} (assumed — pymolt's own interpreter)"
        detected_python = current_python
    if base_python_assumed:
        warn(
            f"no Python version is declared in this project — resolving the legacy "
            f"baseline against {detected_python or current_python}, the interpreter "
            "pymolt runs on.",
            hint="Pass --base-python (or run 'pymolt setup') if the project targets another.",
        )
            
    if detected_python:
        try:
            parts = [int(p) for p in detected_python.split(".")]
            project_version_list = parts[:2]
        except Exception:
            project_version_list = [sys.version_info.major, sys.version_info.minor]
    else:
        project_version_list = [sys.version_info.major, sys.version_info.minor]

    # Target versions must be >= project_version_list AND >= [3, 7] (as uv/fallback resolver requires >= 3.7)
    min_target_list = project_version_list
    if min_target_list < [3, 7]:
        min_target_list = [3, 7]

    eol_map = get_eol_versions()
    from datetime import datetime
    today_str = datetime.now().strftime("%Y-%m-%d")

    # 3. Resolve, validate and EOL-check the target Python version.
    target_python_to_use = _resolve_target_python(
        target_python_to_use, detected_python, current_python,
        project_version_list, min_target_list, eol_map, today_str,
        json_output=json_output,
    )

    # Dynamic environment paths lookup
    legacy_interpreter = find_python_interpreter(detected_python, project_dir=project_path)
    legacy_interpreter_display = str(legacy_interpreter) if legacy_interpreter else "Not found (searching system PATH)"
    
    target_interpreter = None
    target_env_path_val = config.get("target_env_path")
    if target_env_path_val:
        is_windows = sys.platform == "win32"
        sub_dir = "Scripts" if is_windows else "bin"
        exe_name = "python.exe" if is_windows else "python"
        target_path_abs = project_path / target_env_path_val / sub_dir / exe_name
        if target_path_abs.is_file():
            target_interpreter = target_path_abs
            
    if not target_interpreter and target_python_to_use:
        target_interpreter = find_python_interpreter(target_python_to_use, project_dir=project_path)
        
    target_interpreter_display = str(target_interpreter) if target_interpreter else "Not found (will resolve via package compiler)"
    comp_env = "Local host" if not container_id else f"Docker Container ({container_id})"
    target_env_display = config.get("target_env_path") or "Local host/compiler"

    if not json_output:
        _render_assess_header(
            project_dir, chosen_source, python_display, legacy_interpreter_display,
            target_python_to_use, target_env_display, target_interpreter_display, comp_env,
        )

    selected_tool = config.get("selected_tool", "uv")
    # The container/legacy interpreter is the BASELINE env only. The target graph
    # is resolved fresh for the new Python via uv (cross-version resolution needs
    # neither that interpreter nor a container) — so container_id is dropped and
    # the uv strategy forced here, mirroring assess.service.run_assess.
    target_tool = "uv" if selected_tool == "container" else selected_tool
    try:
        baseline_graph, report, _ = _resolve_baseline_graph(
            project_dir=project_dir, chosen=chosen_source, target_python=target_python_to_use,
            container_id=container_id, base_python=detected_python,
            use_cache=not no_cache, tool=selected_tool,
        )
    except Exception as e:
        fail(f"ingestion failed: {e}", code=EXIT_ENVIRONMENT, json_output=json_output)

    # Baseline could not be resolved (no legacy interpreter) — degrade honestly
    # instead of crashing: keep the target side, emit a build-it hint, and let the
    # verdict say plainly that there's nothing to compare against.
    baseline_unresolved = baseline_graph is None
    baseline_tier = classify_baseline_tier(
        baseline_resolved=baseline_graph is not None,
        source_fixation=chosen_source.fixation,
        tests_passed=baseline_tests_passed(project_dir),
        base_python_assumed=base_python_assumed,
    )
    baseline_hint = None
    if baseline_unresolved:
        from pymolt.setup.baseline_hint import build_baseline_hint

        baseline_hint = build_baseline_hint(
            project_dir, detected_python,
            reason=report.warnings[0] if report.warnings else None,
        )
        if not json_output:
            _render_baseline_hint(baseline_hint)

    # Machine-readable path: resolve once, emit JSON, done (no prompts/tables).
    if json_output:
        import json as json_lib

        target_graph, target_error = _resolve_target_graph(
            project_dir, project_path, chosen_source, target_python_to_use,
            None, upgrade_constraint, config.get("target_overrides", {}),
            use_cache=not no_cache, tool=target_tool,
        )
        rows = _build_comparison_rows(
            baseline_graph, target_graph, target_python_to_use, target_error
        )
        target_resolved = target_error is None and target_graph is not None
        target_manifest_path = None
        if target_python_to_use and target_resolved and not no_write:
            try:
                written = _write_target_manifest(
                    project_path, target_graph, config.get("selected_tool", "uv"),
                    target_python_to_use, with_hashes=not no_hashes,
                )
                if written is not None:
                    target_manifest_path = str(written)
            except OSError:
                target_manifest_path = None
        payload = {
            "project_dir": str(project_dir),
            "source_manifest": chosen_source.path.name,
            "source_fixation": report.source_fixation,
            "resolution_quality": report.resolution_quality,
            "detected_python": report.detected_python,
            "target_python": target_python_to_use,
            "target_resolved": target_resolved,
            "target_error": target_error,
            "baseline_unresolved": baseline_unresolved,
            "baseline_tier": baseline_tier.value,
            "target_manifest_path": target_manifest_path,
            "warnings": report.warnings,
            "manual_zone": report.manual_zone,
            "packages": rows,
        }
        if baseline_hint is not None:
            payload["baseline_hint"] = baseline_hint.model_dump(mode="json")
        if risk and baseline_graph is not None:
            from pymolt.risk.assess import assess_risk
            risk_report = assess_risk(baseline_graph, target_graph, target_python_to_use, project_path)
            payload["risk"] = risk_report.model_dump(mode="json")
        typer.echo(json_lib.dumps(payload, indent=2))
        return

    # Interactive customize loop
    risk_report = None  # computed once (network-heavy); reflects the initial resolution
    while True:
        target_graph, target_error = _resolve_target_graph(
            project_dir, project_path, chosen_source, target_python_to_use,
            None, upgrade_constraint, config.get("target_overrides", {}),
            use_cache=not no_cache, tool=target_tool,
        )

        # Two different facts that were both labelled "fixation" and read as a
        # contradiction on one screen: what the manifest declares, and what came
        # out of resolving it.
        console.print(
            f"[bold]After resolution:[/bold] {report.source_fixation} "
            f"[dim](manifest declares: {chosen_source.fixation.value})[/dim]"
        )
        console.print(f"[bold]Resolution quality:[/bold] {report.resolution_quality}")
        console.print()

        # Diagnostics belong on stderr — the answer below is what stdout carries.
        for warning in report.warnings:
            warn(warning)

        # Dynamic target Python verdict — bounded by the baseline's medallion tier
        # (Gold/Silver green, Bronze/None amber), never claiming feasibility from a
        # resolve alone.
        verdict = compatibility_verdict(target_python_to_use, target_error, baseline_tier)
        if verdict is not None:
            title, body, style = verdict
            console.print(Panel(
                f"[bold {style}]{title}[/bold {style}]\n{body}",
                title="Compatibility Report", border_style=style,
            ))
            console.print()

        if not verbose:
            details_msg = "Showing direct dependencies and unresolved packages. Use --verbose (-v) to view all transitive dependencies."
        else:
            details_msg = "Showing all direct and transitive dependencies."

        console.print(
            f"[bold]Comparative Dependency Map (Baseline vs Target Python {target_python_to_use or ''}):[/bold]\n"
            f"This side-by-side view compares the resolved versions in your legacy environment against target resolution.\n"
            f"[dim]{details_msg}[/dim]"
        )
        console.print()

        # Print the side-by-side comparison table.
        rows = _build_comparison_rows(
            baseline_graph, target_graph, target_python_to_use, target_error
        )
        table, all_packages = _render_comparison_table(rows, verbose)
        console.print(table)
        console.print()

        _render_manual_zone(report)

        # Migration risk (opt-in, network). Computed once on the first pass.
        # Skipped when the baseline is unresolved — risk compares the two graphs.
        if risk and risk_report is None and baseline_graph is not None:
            from pymolt.risk.assess import assess_risk
            console.print()
            console.print(f"[dim]Assessing migration risk for {len(all_packages)} package(s) "
                          "(querying OSV + PyPI, cached)…[/dim]")
            risk_report = assess_risk(baseline_graph, target_graph, target_python_to_use, project_path)
            _render_risk(risk_report)

        # Interactive package target version override menu. Overrides are keyed to
        # the baseline graph, so skip the customize loop when it's unresolved.
        if baseline_graph is None or not sys.stdout.isatty():
            break

        adjust = typer.confirm("\nDo you want to customize target versions for specific packages?", default=False)
        if not adjust:
            break

        _run_override_menu(config, config_file, baseline_graph, target_graph, all_packages)

        console.print("[bold green]Overrides saved. Re-running target dependency resolution...[/bold green]\n")
        # Reload updated overrides config for the next loop iteration.
        reloaded = EnvConfig.load(config_file)
        if reloaded is not None:
            config = reloaded.model_dump(mode="json")

    # Write the pinned target manifest (PyPI hashes fetched by default).
    if target_python_to_use and target_graph is not None and not target_error and not no_write:
        try:
            written = _write_target_manifest(
                project_path, target_graph, config.get("selected_tool", "uv"),
                target_python_to_use, with_hashes=not no_hashes,
            )
            if written is not None:
                # The target side resolves independently of the baseline, so the
                # file is real — but with no baseline it is a target snapshot,
                # not the result of a comparison, and must not read as one.
                if getattr(report, "resolution_quality", None) == "unresolved":
                    console.print(
                        f"[yellow]✓ Target manifest written: {written.name}  "
                        "— target-only snapshot; no baseline was resolved, so "
                        "this is not a comparison.[/yellow]"
                    )
                else:
                    console.print(
                        f"[bold green]✓ Target manifest written: {written.name}[/bold green]"
                    )
        except OSError as e:
            err.print(f"[bold red]Failed to write target manifest:[/bold red] {e}")

    next_step(project_path)


def _render_baseline_hint(hint) -> None:
    """Show how to establish the legacy baseline env pymolt could not resolve.

    Two audiences: a copy-paste Docker recipe for a human, and a self-contained
    brief an LLM agent can execute to stand up the interpreter and hand control back.
    """
    console.print(Panel(
        f"[yellow]{hint.reason}[/yellow]\n\n"
        "pymolt will not fabricate the legacy graph (resolving your unpinned constraints to "
        "their latest releases would be a fiction). Establish the real baseline, then re-run.\n\n"
        "[bold]1) Save this as Dockerfile.pymolt-legacy:[/bold]\n"
        f"[dim]{hint.suggested_dockerfile}[/dim]"
        "[bold]2) Build, run, and re-point assess:[/bold]\n"
        + "\n".join(f"  [white]{c}[/white]" for c in hint.suggested_commands),
        title=f"Baseline environment needed — Python {hint.base_python}",
        border_style="yellow", expand=False,
    ))
    console.print(Panel(
        hint.agent_brief,
        title="Delegating to an LLM agent? Hand it this brief",
        border_style="cyan", expand=False,
    ))
    for note in hint.notes:
        console.print(f"  [dim]note: {note}[/dim]")
    console.print()


def _render_inventory(inv) -> None:
    """Render the dependency edge inventory (summary panel + non-PyPI table + indexes)."""
    kinds = inv.counts_by_kind()
    groups = inv.counts_by_group()
    manifests = ", ".join(dict.fromkeys(inv.manifests)) or "none"
    kinds_str = ", ".join(f"{k}:{v}" for k, v in sorted(kinds.items())) or "none"
    groups_str = ", ".join(f"{g}:{c}" for g, c in sorted(groups.items())) or "none"

    console.print(Panel(
        f"Manifests scanned: [yellow]{manifests}[/yellow]\n"
        f"Edges: [bold]{len(inv.edges)}[/bold]  ({kinds_str})\n"
        f"Groups: {groups_str}\n"
        f"Includes (-r): {len(inv.includes)}   Constraints (-c): {len(inv.constraints)}   "
        f"Custom indexes: {len(inv.indexes)}",
        title="Dependency Edge Inventory",
        expand=False,
    ))

    # The names, not just the counts. A command whose promise is "gather
    # everything scattered, in one place" that prints `Edges: 27 (pypi:25)`
    # cannot answer "am I on flask?" — the one question it is asked first.
    direct = [e for e in inv.edges if e.group == "main"] or list(inv.edges)
    if direct:
        table = Table(title=f"Declared dependencies ({len(direct)})", show_header=True)
        table.add_column("Name", style="bold cyan", no_wrap=True)
        table.add_column("Specifier", style="green")
        table.add_column("Group", style="magenta")
        table.add_column("From", style="dim")
        for e in sorted(direct, key=lambda x: (x.name or x.raw or "").lower()):
            table.add_row(e.name or "—", e.raw or "—", e.group, e.source_file or "—")
        console.print(table)

    non_pypi = inv.non_pypi_edges()
    if non_pypi:
        table = Table(title="Non-PyPI edges (need special handling)")
        table.add_column("Name", style="bold cyan")
        table.add_column("Kind")
        table.add_column("Group", style="magenta")
        table.add_column("Spec", overflow="fold")
        kind_markup = {
            "vcs": "[bold red]vcs[/bold red]",
            "url": "[yellow]url[/yellow]",
            "local": "[blue]local[/blue]",
            "unknown": "[red]unknown[/red]",
        }
        for e in sorted(non_pypi, key=lambda x: (x.kind.value, x.name or "")):
            # A name read off the URL is a guess about someone else's repo layout.
            label = f"{e.name}[dim]?[/dim]" if (e.name and e.name_inferred) else (e.name or "-")
            table.add_row(label, kind_markup.get(e.kind.value, e.kind.value), e.group, e.raw)
        console.print(table)
        if any(e.name_inferred for e in non_pypi):
            console.print("[dim]? = name inferred from the URL, not declared[/dim]")
        for e in non_pypi:
            if e.blocker:
                console.print(f"  [bold red]⚠ {e.name or e.raw}:[/bold red] {e.blocker}")
    else:
        console.print("[bold green]✔ All declared dependencies are ordinary PyPI edges.[/bold green]")

    if inv.indexes:
        console.print(Panel(
            "\n".join(f"[{i.kind}] {i.value}   [dim]({i.source_file})[/dim]" for i in inv.indexes),
            title="Custom indexes / find-links (access & supply-chain trust)",
            border_style="yellow",
        ))

    extra_groups = sorted(g for g in groups if g != "main")
    if extra_groups:
        console.print(f"[dim]Optional/dev groups: {', '.join(extra_groups)} "
                      "(migrated/tested separately from main).[/dim]")
    for note in inv.notes:
        console.print(f"[dim]Note: {note}[/dim]")


def _render_surface_map(smap) -> None:
    """Render the discovery surface map (per project root: version evidence, Dockerfiles, tox/nox)."""
    console.print(Panel(
        f"Repo: [yellow]{smap.root_dir}[/yellow]\nProject roots: [bold]{len(smap.project_roots)}[/bold]",
        title="Discovery — Surface Map",
        expand=False,
    ))
    for r in smap.project_roots:
        divergent = r.is_divergent
        console.print(f"\n[bold cyan]{r.path}[/bold cyan]   manifests: {', '.join(r.manifests) or '—'}")

        if r.version_evidence:
            table = Table(title="Python version evidence" + (" — ⚠ DIVERGENT" if divergent else ""))
            table.add_column("Source", style="green")
            table.add_column("Version", style="blue")
            table.add_column("Confidence")
            table.add_column("Note")
            for ev in r.version_evidence:
                conf = ev.confidence.value if hasattr(ev.confidence, "value") else str(ev.confidence)
                table.add_row(ev.source, ev.version or "—", conf, ev.note or "")
            console.print(table)
            if divergent:
                console.print(f"[bold yellow]⚠ Runtime divergence across sources: "
                              f"{', '.join(r.runtime_versions())}[/bold yellow]")
            for rv, constraint in r.floor_violations():
                console.print(f"[bold red]⚠ Runtime {rv} is below the declared floor "
                              f"{constraint}[/bold red]")

        for d in r.dockerfiles:
            tools = ", ".join(d.install_tools) or "—"
            conf = d.runtime_confidence.value if hasattr(d.runtime_confidence, "value") else str(d.runtime_confidence)
            console.print(f"  [magenta]{d.path}[/magenta]: {len(d.stages)} stage(s), runtime Python "
                          f"[bold]{d.runtime_python or '?'}[/bold] ({conf}), os {d.os_hint or '?'}, install: {tools}")
            for note in d.notes:
                console.print(f"    [dim]• {note}[/dim]")

        for tn in r.tox_nox:
            console.print(f"  [blue]{tn.path}[/blue] ({tn.kind}): "
                          f"versions {', '.join(tn.declared_versions) or '—'}")

    for note in smap.notes:
        console.print(f"[dim]Note: {note}[/dim]")




@app.command(rich_help_panel="The migration funnel")
def codemods(
    project_dir: str = typer.Argument(".", help="The repo/project directory to rewrite"),
    package: str = typer.Option(None, "--package", "-p", help="Dependency name (e.g. flask)"),
    from_version: str = typer.Option(None, "--from", help="Current version"),
    to_version: str = typer.Option(None, "--to", help="Target version"),
    target_file: str = typer.Option(
        None,
        "--target-file",
        help="Target dependency file to compare against the current config",
    ),
    axiom_url: str = typer.Option(
        "http://localhost:8000", "--axiom-url", help="Axiom Graph service base URL"
    ),
    write: bool = typer.Option(
        False, "--write", help="Apply changes to disk (default: dry-run preview)"
    ),
):
    """Fetch codemod patterns from Axiom Graph and apply them locally (dry-run by default).

    Preferred flow: point at a target dependency file (requirements-target.txt or
    environment-target.yml) and let pymolt derive migrations from the cached
    comparison data. Legacy per-package --package/--from/--to remains available
    as a fallback.
    """
    from pymolt.codemods.client import AxiomGraphError, DependencyMigration
    from pymolt.codemods.models import CodemodPattern
    from pymolt.codemods.service import resolve_codemod_migrations, run_codemods

    require_project_dir(project_dir)
    config = None
    # No flags at all: fall back to the manifest assess already wrote.
    # `pymolt codemods .` is the documented flow, and it used to exit 2.
    if not target_file and not (package or from_version or to_version):
        for candidate in ("requirements-target.txt", "environment-target.yml"):
            if (Path(project_dir) / candidate).is_file():
                target_file = candidate
                err.print(f"[dim]using the target manifest from assess: {candidate}[/dim]")
                break
    if target_file:
        from pymolt.ingestion.config import EnvConfig

        cfg = EnvConfig.load(Path(project_dir) / ".pymolt" / "env_config.json")
        config = cfg.model_dump(mode="json") if cfg else None
        degrade_warnings: list[str] = []
        try:
            migrations, source_desc = resolve_codemod_migrations(
                project_dir,
                target_dependency_file=target_file,
                config=config,
                warnings=degrade_warnings,
            )
        except ValueError as exc:
            fail(str(exc), hint="cannot resolve this migration")
        for warning in dict.fromkeys(degrade_warnings):  # the service also logs it
            warn(warning)
    else:
        if not (package and from_version and to_version):
            fail(
                "nothing to migrate from: no target manifest, and no explicit package.",
                hint="Run 'pymolt assess . --target-python X.Y' first — codemods then "
                     "picks up requirements-target.txt on its own.\n"
                     "Or name one: pymolt codemods DIR -p flask --from 2.0.3 --to 3.0.0",
            )
        migrations = [DependencyMigration(package, from_version, to_version)]
        source_desc = f"{package} {from_version} → {to_version}"

    console.print(
        f"[bold]Requesting codemods[/bold] for [cyan]{source_desc}[/cyan] via {axiom_url}"
    )
    if not migrations:
        console.print("[yellow]No dependency migrations to request (nothing changed).[/yellow]")
        return

    try:
        with err.status("Connecting to Axiom Graph…", spinner="dots") as status:
            by_pkg, result = run_codemods(
                project_dir,
                migrations,
                base_url=axiom_url,
                write=write,
                progress=lambda msg: status.update(f"[cyan]{msg}[/cyan]"),
            )
    except AxiomGraphError as exc:
        fail(
            f"Axiom Graph unavailable: {exc}",
            hint="Is the service running?  docker run -p 8000:8000 axiom-graph",
            code=EXIT_ENVIRONMENT,
        )

    total_patterns = sum(len(items) for items in by_pkg.values())
    if total_patterns == 0:
        console.print("[yellow]No codemod patterns returned for this migration.[/yellow]")
        return

    console.print(
        f"[bold]{total_patterns}[/bold] recipe(s) returned for "
        f"[cyan]{source_desc}[/cyan] — applying to {project_dir}…"
    )
    for pkg, items in by_pkg.items():
        if not items:
            continue
        table = Table(title=f"Codemods — {pkg}")
        table.add_column("kind")
        table.add_column("change")
        table.add_column("conf")
        for item in items:
            if isinstance(item, CodemodPattern):
                table.add_row(item.kind, f"{item.old_qualname} → {item.new_qualname}", item.confidence)
            elif item.old_qualname and item.new_qualname:
                table.add_row(
                    item.kind or "legacy", f"{item.old_qualname} → {item.new_qualname}", item.confidence
                )
            else:
                table.add_row(
                    item.kind or "template", f"{item.match} → {item.rewrite[0]}", item.confidence
                )
        console.print(table)

    if result.downgraded:
        console.print(
            "\n[bold yellow]Downgraded to heuristic[/bold yellow] "
            "(server claimed verified, local re-verification failed):"
        )
        for label in result.downgraded:
            console.print(f"  [yellow]{label}[/yellow]")

    if result.advisories_by_file:
        console.print("\n[bold yellow]Manual review needed:[/bold yellow]")
        for path, advisories in result.advisories_by_file.items():
            for adv in advisories:
                tag = "caveat" if adv.severity == "precondition" else "not applied"
                console.print(f"  [yellow]{path}:{adv.line}[/yellow] — [{tag}] {adv.reason}")

    if not result.changes:
        # This is the answer, not a footnote. Printing it under a 15-row recipe
        # table taught the reader to study a table that turned out to be
        # irrelevant to their code.
        console.print()
        console.print(Panel(
            f"[bold]No call sites matched.[/bold]\n"
            f"Scanned {result.files_scanned} file(s); none of the "
            f"{total_patterns} returned recipe(s) apply to this codebase.\n"
            "[dim]The table above lists what the service knows about this "
            "upgrade — it is reference, not work you owe.[/dim]",
            border_style="yellow", padding=(0, 1), expand=False,
        ))
        return

    verb = "Rewrote" if write else "Would rewrite"
    console.print(f"\n[bold]{verb} {result.files_changed} file(s):[/bold]")
    for change in result.changes:
        console.print(f"  [green]{change.path}[/green]  ({change.sites} site(s))")
    if not write:
        console.print("\n[dim]Dry run — re-run with [bold]--write[/bold] to apply.[/dim]")


@app.command(rich_help_panel="Tools & account")
def mcp(
    serve: bool = typer.Option(
        False, "--serve", help="Run the stdio server even in a terminal (for manual testing)."
    ),
):
    """Run the stdio MCP server — expose the migration funnel as agent tools.

    This is a JSON-RPC-over-stdio server meant to be launched BY an agent client,
    not run by hand. Register it once, then the client spawns it per session:

      Claude Code:  claude mcp add pymolt -- pymolt mcp
      Codex:        add an [mcp_servers.pymolt] entry to ~/.codex/config.toml
    """
    # All human-facing output goes to STDERR: stdout is the JSON-RPC channel and a
    # single stray byte there corrupts the protocol.
    err = Console(stderr=True)
    try:
        from pymolt.interfaces.mcp_server import run as run_mcp
    except ModuleNotFoundError:
        err.print(
            "[bold red]MCP support is not installed.[/bold red] Reinstall with the extra: "
            "uv tool install 'pymolt[mcp]'  (or: pipx install 'pymolt[mcp]')"
        )
        raise typer.Exit(code=1) from None

    # A human ran this in a terminal — it would otherwise block silently on stdin
    # forever (a stdio server waiting for JSON-RPC). Explain + how to register instead.
    if (sys.stdin.isatty() or sys.stdout.isatty()) and not serve:
        err.print(
            "[bold]pymolt mcp[/bold] is a stdio MCP server — it speaks JSON-RPC over "
            "stdin/stdout and is launched [bold]by an agent client[/bold], not run directly.\n"
        )
        err.print("[bold]Register it once, then your client starts it automatically:[/bold]")
        err.print("  Claude Code   [cyan]claude mcp add pymolt -- pymolt mcp[/cyan]")
        err.print(
            "  Codex         add [cyan][mcp_servers.pymolt][/cyan] to ~/.codex/config.toml "
            '([cyan]command = "pymolt", args = ["mcp"][/cyan])'
        )
        err.print('  Other         command [cyan]pymolt[/cyan], args [cyan]["mcp"][/cyan]')
        err.print(
            "\nTo run it here anyway (e.g. to test), use [bold]pymolt mcp --serve[/bold]."
        )
        raise typer.Exit(code=0)

    # Spawned by a client (stdin piped) or --serve: run. A one-line lifecycle note to
    # STDERR so it never looks dead; stdout stays clean for the protocol.
    err.print("[dim]pymolt mcp: stdio server ready (JSON-RPC on stdin/stdout; Ctrl-C).[/dim]")
    run_mcp()


@app.command(rich_help_panel="Strategic Migrations")
def succession(
    project_dir: str = typer.Argument(".", help="The project directory to migrate"),
    axiom_url: str = typer.Option(
        "http://localhost:8000", "--axiom-url", help="Axiom Graph service base URL"
    ),
    write: bool = typer.Option(
        False, "--write", help="Apply in-place shims to disk (default: dry-run preview)"
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit the result as JSON on stdout"),
):
    """Find framework-succession paths for a dead-framework project (e.g. TF1/Keras).

    Detects the project's frameworks, asks Axiom Graph where they should go, and for
    IN_PLACE paths (e.g. keras → tensorflow.keras) applies the shims via LibCST —
    dry-run by default. TRANSPLANT paths (e.g. keras → torch) are shown as a plan to
    follow, never auto-applied.
    """
    from pymolt.strategic.succession.client import SuccessionError
    from pymolt.strategic.succession.detect import detect_frameworks
    from pymolt.strategic.succession.service import run_succession

    require_project_dir(project_dir, json_output=json_output)
    frameworks = detect_frameworks(project_dir)
    if not frameworks:
        # A real answer, not a failure: nothing declared, so nothing to succeed.
        # Under --json it must still be JSON.
        if json_output:
            import json as _json
            typer.echo(_json.dumps(
                {"frameworks": [], "edges": [], "transplant_plans": [],
                 "in_place_files_changed": 0, "dry_run": True},
                indent=2,
            ))
        else:
            err.print("[yellow]No dependency manifest found — nothing to detect.[/yellow]")
        raise typer.Exit(code=0)
    if not json_output:
        console.print(f"[bold]Frameworks detected:[/bold] {', '.join(frameworks)}")

    try:
        with err.status("Querying Axiom Graph for succession…", spinner="dots") as status:
            edges, result = run_succession(
                project_dir, base_url=axiom_url, write=write,
                progress=lambda m: status.update(f"[cyan]{m}[/cyan]"),
            )
    except SuccessionError as exc:
        fail(
            f"Axiom Graph unavailable: {exc}",
            hint="Is the service running?  docker compose up --build",
            code=EXIT_ENVIRONMENT,
            json_output=json_output,
        )

    from pymolt.strategic.succession.transplant import build_transplant_plan

    if json_output:
        import json as _json
        payload = {
            "frameworks": frameworks,
            "edges": [e.model_dump(mode="json") for e in edges],
            "transplant_plans": [
                build_transplant_plan(e, project_dir).model_dump(mode="json")
                for e in edges if e.is_transplant
            ],
            "in_place_files_changed": result.files_changed if result else 0,
            "dry_run": result.dry_run if result else True,
        }
        typer.echo(_json.dumps(payload, indent=2))
        return

    if not edges:
        console.print("[yellow]No known succession path for these frameworks.[/yellow]")
        return

    for e in edges:
        if e.is_in_place:
            console.print(Panel(
                f"[bold]{e.from_framework} → {e.to_framework}[/bold]  [green]in-place[/green]\n"
                f"{e.summary}\n"
                f"[dim]{len(e.patterns)} import shim(s), {len(e.rules)} rule(s)[/dim]",
                title="Succession — living corpse", border_style="green", expand=False,
            ))
        else:
            _render_transplant_plan(build_transplant_plan(e, project_dir))

    if result is not None:
        verb = "Rewrote" if write else "Would rewrite"
        console.print(f"\n[bold]In-place shims — {verb} {result.files_changed} file(s):[/bold]")
        for change in result.changes:
            console.print(f"  [green]{change.path}[/green]  ({change.sites} site(s))")
        if not write:
            console.print("\n[dim]Dry run — re-run with [bold]--write[/bold] to apply.[/dim]")


def _render_transplant_plan(plan) -> None:
    """Render an organ-transplant plan: scaffold → map → port weights → contract-verify.

    A transplant is guidance, never auto-applied — the last step is the honest gate
    (`pymolt contract` proving the ported model matches the original). Edge-supplied
    text (scaffold code, mappings, notes) is markup-escaped: it may contain brackets
    (e.g. `x[0]`) that would otherwise be parsed as Rich markup."""
    from rich.markup import escape

    console.print(Panel(
        f"[bold]{plan.from_framework} → {plan.to_framework}[/bold]  [yellow]transplant[/yellow]\n"
        f"{escape(plan.summary)}\n[dim]A plan to follow — pymolt does not auto-apply this.[/dim]",
        title="Succession — organ transplant", border_style="yellow", expand=False,
    ))
    for step in plan.steps:
        console.print(f"\n[bold yellow]{step.n}. {escape(step.title)}[/bold yellow]")
        if step.detail:
            console.print(escape(step.detail))
        if step.code:
            console.print(Panel(escape(step.code), border_style="dim", expand=False))
        for cmd in step.commands:
            style = "dim" if cmd.lstrip().startswith("#") else "white"
            console.print(f"  [{style}]{escape(cmd)}[/{style}]")
    for note in plan.notes:
        console.print(f"[dim]note: {escape(note)}[/dim]")


@app.command(rich_help_panel="Tools & account")
def login(
    token: str | None = typer.Option(
        None, "--token", help="API token (pmk_…); omit to be prompted (hidden input)"),
):
    """Store your pymolt API token for the Axiom Graph codemod service.

    Create one at https://pymolt.zeelex.me (Account → API tokens). The token is
    saved to your user config (chmod 600) and sent by `pymolt codemods`. The
    PYMOLT_API_TOKEN env var overrides the saved token when set.
    """
    from pymolt.config import save_token

    tok = (token or typer.prompt("Paste your pymolt API token", hide_input=True)).strip()
    if not tok:
        fail("no token provided.",
             hint="Create one at https://pymolt.zeelex.me (Account → API tokens).")
    path = save_token(tok)
    console.print(f"[green]Saved[/green] API token to [cyan]{path}[/cyan]")


@app.command(rich_help_panel="Tools & account")
def logout():
    """Remove the stored pymolt API token."""
    from pymolt.config import clear_token

    if clear_token():
        console.print("[green]Removed[/green] the stored API token.")
    else:
        console.print("[dim]No stored API token to remove.[/dim]")


@env_app.command("hint")
def env_hint(
    project_dir: str = typer.Argument(".", help="The target project directory"),
    target_python: str | None = typer.Option(
        None, "--target-python",
        help="Target Python version (e.g. 3.13); falls back to setup config"),
    manifest: str | None = typer.Option(
        None, "--manifest", help="Target manifest path; defaults to requirements-target.txt"),
    json_output: bool = typer.Option(False, "--json", help="Emit the result as JSON on stdout"),
):
    """Suggest how to build a runnable target-Python environment — pymolt does not build it.

    Prints a derived ``Dockerfile`` (base-image version swap, target manifest) when the
    project has one, plus the ``uv venv`` / ``uv pip install`` commands that would create
    an equivalent venv, and the ready-to-copy post-migration capture command. Nothing is
    written or run — build the environment yourself, then point pymolt at it (the target
    environment prompt in `pymolt setup`, or `--container` on `pymolt contract capture`).

    Run `pymolt assess` first so a target manifest exists.
    """
    from pymolt.ingestion.config import EnvConfig
    from pymolt.setup.target_hint import build_target_hint

    project_path = require_project_dir(project_dir, json_output=json_output)
    config = EnvConfig.load(project_path / ".pymolt" / "env_config.json")

    effective_target_python = target_python or (config.target_python if config else None)
    if not effective_target_python:
        fail(
            "no target Python to build an environment for.",
            hint="Pass --target-python, or run 'pymolt setup' / 'pymolt assess' first "
                 "so one is configured.",
            json_output=json_output,
        )

    effective_manifest = manifest
    if effective_manifest is None:
        default_target_manifest = project_path / "requirements-target.txt"
        if default_target_manifest.is_file():
            effective_manifest = default_target_manifest.name
        elif config is not None and config.selected_manifest:
            effective_manifest = config.selected_manifest

    hint = build_target_hint(project_path, effective_target_python, manifest=effective_manifest)

    if json_output:
        typer.echo(hint.model_dump_json(indent=2))
        return

    console.print(
        f"[bold blue]pymolt env hint[/bold blue] — target Python [cyan]{hint.target_python}[/cyan]"
    )
    if hint.suggested_dockerfile:
        console.print("\n[bold]Suggested Dockerfile.pymolt-target:[/bold]")
        console.print(hint.suggested_dockerfile)
    console.print("\n[bold]Suggested uv commands:[/bold]")
    for cmd in hint.suggested_uv_commands:
        console.print(f"  [white]{cmd}[/white]")
    for note in hint.notes:
        console.print(f"  [dim]note: {note}[/dim]")

    console.print(Panel(
        f"[bold]Next step[/bold] — build the environment above yourself, then capture the "
        f"post-migration contract:\n\n"
        f"  [white]{hint.capture_hint}[/white]",
        title="next step", expand=False, border_style="green",
    ))


_PORTED_STYLE = {"confirmed": "green", "likely": "yellow", "unknown": "dim"}


@app.command(rich_help_panel="Strategic Migrations")
def forks(
    repo: str = typer.Argument(..., help="Base repo to triage, as owner/name"),
    project_dir: str = typer.Option(
        ".", "--project-dir",
        help="Where to keep the fork cache (default: the current directory)"),
    online: bool = typer.Option(
        False, "--online", help="Hit the GitHub API (cached); default reads the cache only"
    ),
    cutoff_months: int = typer.Option(
        18, "--cutoff-months", help="Ignore forks not pushed to within this many months"
    ),
    top: int = typer.Option(
        25, "--top", help="How many top survivors get the expensive compare/manifest passes"
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Emit the full report as JSON on stdout"
    ),
):
    """Strategic fork-network triage — rank the live/ported successor forks of a dead repo.

    Offline by default (reads a previously cached ``--online`` run). Ranks by recency,
    star gravity, divergence from base, and a declared-dependency "already ported?"
    signal — never a claim that a fork *works*.
    """
    from pymolt.strategic.forknet.service import run_forknet

    # Cache with the project, like every other phase — keyed to CWD it silently
    # became a fresh cache (and a fresh round of GitHub API calls) whenever the
    # command was run from somewhere else.
    report = run_forknet(
        repo,
        cache_root=require_project_dir(project_dir, json_output=json_output),
        online=online,
        cutoff_months=cutoff_months,
        top_n=top,
    )

    if json_output:
        typer.echo(report.model_dump_json(indent=2))
        return

    console.print(Panel(
        f"[bold]Fork-network triage[/bold] — [cyan]{report.base_repo}[/cyan]  "
        f"auth: [white]{report.auth_mode}[/white]",
        expand=False, border_style="blue",
    ))

    if not report.candidates:
        console.print("[yellow]No fork candidates to show.[/yellow]")
    else:
        table = Table(title=f"Top {len(report.candidates)} candidate(s)")
        table.add_column("#", justify="right")
        table.add_column("fork")
        table.add_column("pushed")
        table.add_column("stars", justify="right")
        table.add_column("ahead", justify="right")
        table.add_column("ported")
        table.add_column("score", justify="right")
        for i, c in enumerate(report.candidates, start=1):
            style = _PORTED_STYLE.get(c.ported_signal.value, "dim")
            table.add_row(
                str(i),
                c.name_with_owner,
                c.pushed_at.date().isoformat(),
                str(c.stars),
                str(c.ahead_by) if c.ahead_by is not None else "—",
                f"[{style}]{c.ported_signal.value}[/{style}]",
                f"{c.score:.2f}",
            )
        console.print(table)

    for note in report.notes:
        console.print(f"[dim]note: {note}[/dim]")

