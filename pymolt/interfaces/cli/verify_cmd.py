"""CLI for verify/ — the external boundary tracer + diff (Component 6, interfaces layer).

Everything here runs *outside* the target project: the watcher is injected via PYTHONPATH +
PYMOLT_TRACE_* env (zero edits to the target), and rendering of the structured BoundaryDiff
lives here in interfaces/, not in the non-interactive core (spec: render is not the core's job).

Commands:
  pymolt contract export-watcher DEST          write the standalone stdlib bundle (no deps)
  pymolt contract trace --target P --out F -- CMD...   run CMD with the watcher injected
  pymolt contract capture --when W --mode M -- CMD...  run CMD, name it baseline/post-mig.
  pymolt contract diff OLD NEW [--json]        fold two recordings into a BoundaryDiff
  pymolt contract boundary                     interactive orchestrator (both ends + diff)
  pymolt contract report DIR                   unified report (auto-sources the named slots)

Capture/injection itself lives in ``pymolt.verify.service`` (so callers can drive the same
mechanism) — this file is CLI rendering + interactive prompting only.
"""
import json
import os
import shlex
import tempfile
from pathlib import Path

import typer
from rich.panel import Panel
from rich.table import Table

from pymolt.core.enums import Verdict
from pymolt.interfaces.cli.output import (
    EXIT_FINDING,
    EXIT_INCONCLUSIVE,
    EXIT_OK,
    console,
    err,
    fail,
    next_step,
    require_file,
    require_project_dir,
)
from pymolt.verify import service
from pymolt.verify.cascade import _verdict_from_boundary
from pymolt.verify.comparators import (
    ComparatorError,
    ComparatorProfile,
    compare_traces,
    load_custom_comparator,
)
from pymolt.verify.diff import build_boundary_diff
from pymolt.verify.export_watcher import export_watcher
from pymolt.verify.models import (
    BoundaryDiff,
    CaptureMode,
    CaptureValidity,
    VerificationVerdict,
)

app = typer.Typer(help="Behavioral boundary tracing & diff (runs entirely outside the target).")

_VERDICT_STYLE = {
    Verdict.BEHAVIOR_STABLE: "bold green",
    Verdict.BEHAVIOR_CHANGED: "bold red",
    Verdict.NEEDS_ACTION: "bold yellow",
}


def _verification_exit(verdict: VerificationVerdict) -> int:
    return {
        VerificationVerdict.PASS: EXIT_OK,
        VerificationVerdict.FAIL: EXIT_FINDING,
        VerificationVerdict.INCONCLUSIVE: EXIT_INCONCLUSIVE,
    }[verdict]


# ── shared helpers ────────────────────────────────────────────────────────────
def _trace_command(target: str, command: list[str], out_path: Path, backend: str,
                   container: str | None = None, workdir: str | None = None,
                   exclude: str = "", source: str = "", include_internal: bool = False,
                   privacy: str = "shape", sample_rate: float = 1.0,
                   impact_targets: list[str] | None = None,
                   deployment_id: str | None = None,
                   request_id: str | None = None,
                   correlation_id: str | None = None) -> int:
    """Render-and-run wrapper over ``service.capture_trace``: prints progress/result, returns
    the number of events captured (the CLI commands below all key off this)."""
    where = f"container [cyan]{container}[/cyan]" if container else "local"
    scope = "internal+boundary" if include_internal else "boundary"
    err.print(f"[dim]tracing[/dim] target=[cyan]{target}[/cyan] ({scope}) in {where} "
              f"cmd=[white]{' '.join(command)}[/white]")
    result = service.capture_trace(
        target, command, out_path, backend, container=container, workdir=workdir,
        exclude=exclude, source=source, include_internal=include_internal,
        privacy=privacy, sample_rate=sample_rate,
        impact_targets=impact_targets, deployment_id=deployment_id,
        request_id=request_id, correlation_id=correlation_id,
    )
    err.print(f"[green]captured[/green] {result.events} events from "
              f"{result.processes} process(es) -> {result.out_path}")
    return result.events


def render_boundary_diff(diff: BoundaryDiff, full: bool = False) -> None:
    """Render a BoundaryDiff for humans (the formatting the core deliberately omits)."""
    verdict = _verdict_from_boundary(diff)
    counts = diff.counts()
    console.print(Panel(
        f"verdict: [{_VERDICT_STYLE[verdict]}]{verdict.value}[/]\n"
        f"comparable behavior changed: "
        f"{'[red]yes[/]' if not diff.is_clean() else '[green]no[/]'}\n"
        f"disappeared={counts['disappeared']}  result_changed={counts['result_changed']}  "
        f"raise_changed={counts['raise_changed']}  appeared={counts['appeared']}  "
        f"[yellow]skipped_opaque={counts['skipped_opaque']}[/]",
        title="Boundary diff", expand=False,
    ))

    def _cell(r, c):
        if c == "where":
            w = r.get("where")
            return f"{os.path.basename(w['file'])}:{w['line']}" if isinstance(w, dict) else ""
        return str(r.get(c, ""))

    def _table(title, rows, cols, style):
        if not rows:
            return
        shown = rows if full else rows[:15]
        t = Table(title=f"{title} ({len(rows)})", header_style=style)
        for c in cols:
            t.add_column("call site" if c == "where" else c)
        for r in shown:
            t.add_row(*[_cell(r, c) for c in cols])
        if not full and len(rows) > len(shown):
            t.caption = f"… {len(rows) - len(shown)} more (use --full)"
        console.print(t)

    _table("BREAKING — disappeared", diff.disappeared, ["qualname", "where"], "red")
    _table("BREAKING — result_changed", diff.result_changed,
           ["qualname", "where", "old", "new"], "red")
    _table("BREAKING — raise_changed", diff.raise_changed,
           ["qualname", "where", "old_raised", "new_raised"], "red")
    _table("NEEDS-ACTION — skipped_opaque", diff.skipped_opaque, ["qualname", "where", "reason"],
           "yellow")
    _table("INFO — appeared", diff.appeared, ["qualname", "where"], "cyan")


# ── composable subcommands ────────────────────────────────────────────────────
@app.command("export-watcher")
def export_watcher_cmd(
    dest: str = typer.Argument(..., help="Directory to write the standalone bundle into"),
):
    """Write the pure-stdlib watcher bundle to drop onto PYTHONPATH (no pymolt/pydantic needed)."""
    out = export_watcher(dest)
    console.print(Panel(
        f"Bundle written to [cyan]{out}[/cyan]\n\n"
        f"Use it anywhere (venv, Docker image, the 3.6 devcontainer):\n"
        f"  [white]PYTHONPATH={out} PYMOLT_TRACE_TARGET=flask \\\n"
        f"  PYMOLT_TRACE_OUT=/tmp/trace-{{pid}}.jsonl  <your command>[/white]\n\n"
        f"Pure stdlib, Python 3.6+; the target app is never modified.",
        title="export-watcher", expand=False, border_style="green",
    ))


@app.command("trace")
def trace_cmd(
    command: list[str] = typer.Argument(..., help="Command to run after --, e.g. -- python app.py"),
    target: str = typer.Option(
        "all", "--target", "-t",
        help="Dependency to trace: 'all'/'*' (every installed dep), 'flask', or 'flask,werkzeug'"),
    out: str = typer.Option(..., "--out", "-o", help="Output JSONL artifact path"),
    backend: str = typer.Option(
        "auto", "--backend", help="auto | setprofile | monitoring | wrap | hybrid"),
    wrap: str = typer.Option(
        "", "--wrap",
        help="Targeted-wrap mode: comma-list of dependency symbols to wrap "
             "(e.g. flask.jsonify,flask.views.MethodView,flask.app.Flask.add_url_rule). Implies "
             "--backend wrap (low overhead). Add --backend hybrid to also run setprofile for "
             "completeness (the C-remainder + dynamic usage)."),
    impact_targets: str | None = typer.Option(
        None, "--impact-targets",
        help="Comma-separated affected API symbols; exact paths use low-overhead wrapping",
    ),
    exclude: str = typer.Option(
        "", "--exclude", "-x", help="Comma-separated top-levels to skip (e.g. your own package)"),
    source: str = typer.Option(
        "", "--source", "-s",
        help="YOUR code (near side, caller). Default: auto-detect. Records only 'source -> dep'"),
    include_internal: bool = typer.Option(
        False, "--include-internal",
        help="Also record internal dependency<->dependency calls (full audit; default off)"),
    container: str | None = typer.Option(
        None, "--container", "-c",
        help="Run CMD inside an ALREADY-RUNNING container (docker exec), not locally"),
    workdir: str | None = typer.Option(
        None, "--workdir", "-w", help="Working dir inside the container (default: image WORKDIR)"),
    privacy: str = typer.Option(
        "shape", "--privacy", help="shape (safe default) | values (exact, may contain sensitive data)"
    ),
    sample_rate: float = typer.Option(
        1.0, "--sample-rate", min=0.000001, max=1.0,
        help="Fraction of completed calls to retain",
    ),
    deployment_id: str | None = typer.Option(None, "--deployment-id"),
    request_id: str | None = typer.Option(None, "--request-id"),
    correlation_id: str | None = typer.Option(None, "--correlation-id"),
):
    """Run a command with the watcher injected externally; write a JSONL recording.

    Records the boundary YOUR code -> dependency. By default --target (the dependency side, 'all')
    is observed via sys.setprofile. With --wrap, only the listed symbols are wrapped (low,
    targeted overhead — fit for prod). With --container the command runs via `docker exec` in a
    running container (bundle copied in and removed; no `docker run`).
    """
    if privacy not in {"values", "shape"}:
        fail(f"--privacy must be 'values' or 'shape' (got {privacy!r})")
    if wrap:
        target = wrap                       # wrap list goes through PYMOLT_TRACE_TARGET
        backend = "hybrid" if backend == "hybrid" else "wrap"
    parsed_impacts = (
        [item.strip() for item in impact_targets.split(",") if item.strip()]
        if impact_targets else None
    )
    _trace_command(target, command, Path(out), backend, container=container, workdir=workdir,
                   exclude=exclude, source=source, include_internal=include_internal,
                   privacy=privacy, sample_rate=sample_rate,
                   impact_targets=parsed_impacts, deployment_id=deployment_id,
                   request_id=request_id, correlation_id=correlation_id)


_CAPTURE_WHEN = {"baseline": "baseline", "post-migration": "post_migration"}
_CAPTURE_MODE = {
    "tests": CaptureMode.TEST_SUITE,
    "command": CaptureMode.LIVE_COMMAND,
    "attach": CaptureMode.LIVE_ATTACH,
}


@app.command("capture")
def capture_cmd(
    command: list[str] = typer.Argument(
        None, help="Command to run after --, e.g. -- pytest tests/ (omit for --mode attach)"),
    when: str = typer.Option(..., "--when", help="'baseline' (pre-migration) or 'post-migration'"),
    mode: str = typer.Option(
        "command", "--mode", help="tests (run your suite) | command (run your app) | attach "
                                   "(you run it yourself, pymolt just watches)"),
    project_dir: str = typer.Option(
        ".", "--project-dir", help="Project root (state lives in .pymolt/)"),
    target: str = typer.Option(
        "all", "--target", "-t", help="Dependency to trace (see `trace --help`)"),
    backend: str = typer.Option(
        "auto", "--backend", help="auto | setprofile | monitoring | wrap | hybrid"),
    exclude: str = typer.Option("", "--exclude", "-x", help="Comma-separated top-levels to skip"),
    source: str = typer.Option(
        "", "--source", "-s", help="YOUR code (near side). Default: auto-detect"),
    container: str | None = typer.Option(
        None, "--container", "-c",
        help="Run CMD in a running container. Defaults to the configured env for "
             "this slot (.pymolt/env_config.json) when omitted."),
    workdir: str | None = typer.Option(
        None, "--workdir", "-w", help="Working dir inside the container"),
    collect: bool = typer.Option(
        False, "--collect", help="Finalize a --mode attach capture (reads whatever was written)"),
    force: bool = typer.Option(
        False, "--force", help="Overwrite an existing baseline/post-migration capture, no prompt"),
    privacy: str = typer.Option(
        "auto", "--privacy",
        help="auto (values for tests, shape for live) | values | shape",
    ),
    sample_rate: float = typer.Option(
        1.0, "--sample-rate", min=0.000001, max=1.0,
        help="Fraction of completed calls to retain (sampled reports stay inconclusive)",
    ),
    impact_targets: str | None = typer.Option(
        None, "--impact-targets",
        help="Affected API symbols; post-migration defaults to the applied Axiom plan",
    ),
    deployment_id: str | None = typer.Option(None, "--deployment-id"),
    request_id: str | None = typer.Option(None, "--request-id"),
    correlation_id: str | None = typer.Option(None, "--correlation-id"),
):
    """Capture a dynamic trace and name it 'baseline' or 'post-migration' — the same watcher
    injection `trace` uses, but persisted to .pymolt/contract_state.json so `report` (run with
    no flags) can auto-diff baseline against post-migration without you tracking file paths.

    Three ways to capture: --mode tests/command run CMD yourself here and block until it exits
    (a test suite naturally exits on its own; for a long-running app, Ctrl+C ends it and whatever
    was captured up to that point is kept). --mode attach instead prints a command for YOU to run
    in your own terminal/devcontainer/server — call `capture --when W --mode attach --collect`
    afterwards to finalize whatever it wrote.
    """
    require_project_dir(project_dir)
    when_key = _CAPTURE_WHEN.get(when)
    if when_key is None:
        fail(f"--when must be 'baseline' or 'post-migration' (got {when!r})")
    capture_mode = _CAPTURE_MODE.get(mode)
    if capture_mode is None:
        fail(f"--mode must be 'tests', 'command', or 'attach' (got {mode!r})")
    if privacy not in {"auto", "values", "shape"}:
        fail(f"--privacy must be 'auto', 'values', or 'shape' (got {privacy!r})")
    effective_privacy = (
        "values" if capture_mode is CaptureMode.TEST_SUITE else "shape"
    ) if privacy == "auto" else privacy

    if collect:
        out_path = service.pending_attach_path(project_dir, when_key)
        if not out_path.is_file():
            fail(
                f"no pending attach capture at {out_path}",
                hint=f"Start one first: pymolt contract capture --when {when} --mode attach",
            )
        slot = service.finalize_attached_capture(
            project_dir, when_key, out_path, target=target,
            privacy=effective_privacy, sample_rate=sample_rate,
        )
        if slot.validity is CaptureValidity.VALID:
            err.print(f"[green]collected[/green] {slot.events} events -> {slot.trace_path}")
            next_step(project_dir)
            return
        err.print(
            f"[bold yellow]capture not accepted as {when} evidence[/bold yellow]: "
            f"{slot.validity_reason or slot.validity.value}\n"
            f"diagnostic artifact: {slot.trace_path}"
        )
        raise typer.Exit(code=EXIT_INCONCLUSIVE)

    existing = getattr(service.load_contract_state(project_dir), when_key)
    if existing is not None and not force:
        # A baseline is a recording of a world that no longer exists once you
        # migrate — say what is being destroyed before destroying it.
        overwrite = typer.confirm(
            f"'{when}' already has a capture from {existing.captured_at} "
            f"({' '.join(existing.command) or existing.mode.value}, "
            f"{existing.events} events). Overwrite?"
        )
        if not overwrite:
            raise typer.Exit(code=EXIT_FINDING)

    if capture_mode is CaptureMode.LIVE_ATTACH:
        instr = service.start_attached_capture(
            project_dir, when_key, target=target, backend=backend,
            privacy=effective_privacy, sample_rate=sample_rate,
        )
        console.print(Panel(
            f"Run this yourself, then finalize with:\n"
            f"  [white]pymolt contract capture --when {when} --mode attach --collect "
            f"--privacy {effective_privacy} --sample-rate {sample_rate}[/white]\n\n"
            f"  [white]{instr.command_hint}[/white]",
            title="capture (attach)", expand=False, border_style="cyan",
        ))
        return

    if not command:
        fail("provide a command after --, e.g. -- pytest tests/")
    err.print(f"[dim]capturing[/dim] when=[cyan]{when}[/cyan] mode=[cyan]{mode}[/cyan] "
              f"cmd=[white]{' '.join(command)}[/white]")
    slot = service.capture_named_trace(
        project_dir, when_key, capture_mode, command=command, target=target, backend=backend,
        container=container, workdir=workdir, exclude=exclude, source=source,
        privacy=effective_privacy, sample_rate=sample_rate,
        impact_targets=(
            [item.strip() for item in impact_targets.split(",") if item.strip()]
            if impact_targets else None
        ),
        deployment_id=deployment_id,
        request_id=request_id,
        correlation_id=correlation_id,
    )
    if slot.validity is not CaptureValidity.VALID:
        err.print(
            f"[bold yellow]capture not accepted as {when} evidence[/bold yellow]: "
            f"{slot.validity_reason or slot.validity.value}\n"
            f"diagnostic artifact: {slot.trace_path}"
        )
        code = (
            EXIT_FINDING
            if slot.validity is CaptureValidity.COMMAND_FAILED
            else EXIT_INCONCLUSIVE
        )
        raise typer.Exit(code=code)
    err.print(f"[green]captured[/green] {slot.events} events -> {slot.trace_path}")
    if slot.duration_seconds is not None:
        err.print(
            f"[dim]duration={slot.duration_seconds:.3f}s  "
            f"dropped={slot.dropped_events if slot.dropped_events is not None else 'unknown'}  "
            f"backend={slot.backend or backend}[/dim]"
        )
    if slot.archived_previous:
        err.print(f"[dim]previous {when} recording kept at {slot.archived_previous}[/dim]")
    next_step(project_dir)


@app.command("diff")
def diff_cmd(
    old: str = typer.Argument(..., help="OLD recording (JSON or JSONL)"),
    new: str = typer.Argument(..., help="NEW recording (JSON or JSONL)"),
    contract: bool = typer.Option(
        False, "--contract", "-C",
        help="Compare by interaction SHAPE (type/structure), ignoring concrete data values"),
    profile: str = typer.Option(
        "exact", "--profile", help="Comparator: exact | shape | custom"
    ),
    comparator: str | None = typer.Option(
        None, "--comparator", help="Custom comparator as module:callable"
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit the BoundaryDiff as JSON"),
    full: bool = typer.Option(False, "--full", help="Show all entries, not just the first 15"),
):
    """Fold two boundary recordings into a BoundaryDiff and report it.

    Default compares concrete values; ``--contract`` compares only the interaction shape
    (returned-vs-raised, result type/structure) — robust to volatile data (paths, timestamps).
    """
    # A missing recording used to fold into an empty diff and report "no behavior
    # change" — a false all-clear, the one answer this command must never invent.
    require_file(old, what="OLD recording", json_output=as_json)
    require_file(new, what="NEW recording", json_output=as_json)

    if contract:
        if profile != "exact":
            fail("--contract cannot be combined with --profile", json_output=as_json)
        profile = "shape"
    try:
        selected = ComparatorProfile(profile)
        if selected is ComparatorProfile.CUSTOM:
            if comparator is None:
                raise ComparatorError("--profile custom requires --comparator module:callable")
            load_custom_comparator(comparator)
        diff = compare_traces(old, new, profile=selected, custom=comparator)
    except ComparatorError as exc:
        fail(str(exc), json_output=as_json)
    if as_json:
        console.print_json(diff.model_dump_json())
    else:
        render_boundary_diff(diff, full=full)
    raise typer.Exit(code=_verification_exit(diff.verdict))


# ── interactive orchestrator ──────────────────────────────────────────────────
def _obtain_recording(side, target, work, backend, exclude, source):
    """Interactively get one side's recording: an existing artifact, or a command to trace
    (locally or inside an already-running container — old/new can even be different containers)."""
    mode = typer.prompt(
        f"[{side}] (p)ath to an existing recording or (c)ommand to run",
        default="c",
    ).strip().lower()
    if mode.startswith("p"):
        return Path(typer.prompt(f"[{side}] path to recording"))
    cmd = shlex.split(typer.prompt(f"[{side}] command to run (e.g. /venv/bin/python app.py)"))
    container = typer.prompt(f"[{side}] container id/name (blank = local)", default="").strip()
    workdir = None
    if container:
        workdir = typer.prompt(f"[{side}] workdir in container (blank = image default)",
                               default="").strip() or None
    out = work / f"{side}.jsonl"
    _trace_command(target, cmd, out, backend, container=container or None,
                   workdir=workdir, exclude=exclude, source=source)
    return out


@app.command("boundary")
def boundary_cmd(
    backend: str = typer.Option("auto", "--backend", help="auto | setprofile | monitoring"),
    full: bool = typer.Option(False, "--full", help="Show all diff entries"),
):
    """Interactive: capture (or load) both versions and show the boundary diff in one flow."""
    console.print(Panel(
        "Captures the dependency boundary under two versions and diffs them.\n"
        "Each side is either an existing recording or a command this runs with the watcher "
        "injected externally (zero edits to the target).",
        title="verify boundary", expand=False,
    ))
    target = typer.prompt("Dependency side ('all'/'*' or 'flask')", default="all")
    source = typer.prompt("Your code (caller) — blank = auto-detect", default="").strip()
    exclude = typer.prompt("Top-levels to exclude (blank = none)", default="").strip()
    work = Path(tempfile.mkdtemp(prefix="pymolt-boundary-"))
    old = _obtain_recording("old", target, work, backend, exclude, source)
    new = _obtain_recording("new", target, work, backend, exclude, source)
    console.print()
    diff = build_boundary_diff(str(old), str(new))
    render_boundary_diff(diff, full=full)
    raise typer.Exit(code=_verification_exit(diff.verdict))



@app.command("map")
def map_cmd(
    project_dir: str = typer.Argument(".", help="The project directory to analyze"),
    json_output: bool = typer.Option(False, "--json", help="Emit the contact map as JSON"),
):
    """Static contact map: where our code calls into third-party dependencies (cheap, no runtime)."""
    from pymolt.verify.contact_map import build_contact_map

    project_path = require_project_dir(project_dir, json_output=json_output)
    cmap = build_contact_map(project_path)
    if json_output:
        typer.echo(json.dumps(cmap.model_dump(mode="json"), indent=2))
        return

    total = len(cmap.contacts)
    console.print(Panel(
        f"Scanned [bold]{cmap.files_scanned}[/bold] file(s) — "
        f"[bold]{total}[/bold] call site(s) reaching [bold]{sum(len(t) for t in cmap.by_dep.values())}[/bold] symbol(s) across [bold]{len(cmap.by_dep)}[/bold] dependencies",
        title="Static Contact Map", expand=False,
    ))
    for dep, targets in cmap.by_dep.items():
        console.print(f"\n[bold magenta]{dep}[/bold magenta] — {len(targets)} symbol(s)")
        table = Table(show_header=True)
        table.add_column("Dependency symbol", style="cyan")
        table.add_column("Called from (your code)")
        table.add_column("File", style="dim")
        for c in cmap.contacts:
            if c.dep == dep:
                table.add_row(c.target, c.caller, c.file or "—")
        console.print(table)
    for note in cmap.notes:
        console.print(f"[dim]Note: {note}[/dim]")


def _render_contract_report(rep) -> None:
    trust_pct = round(rep.trust * 100)
    border = {
        VerificationVerdict.PASS: "green",
        VerificationVerdict.FAIL: "red",
        VerificationVerdict.INCONCLUSIVE: "yellow",
    }[rep.verdict]
    if rep.baseline_stale:
        # Above the report, not inside it: every number below describes the old
        # world, and that has to be read first or not at all.
        console.print(Panel(
            "[bold yellow]The evidence below predates the project's current state.[/bold yellow]\n"
            "A capture was taken against a different manifest/environment, so this verdict "
            "describes the world as it was then — not the code you have now.\n"
            "[dim]Re-capture (pymolt contract capture) to judge the current code.[/dim]",
            title="⚠ stale evidence", title_align="left", border_style="yellow", expand=False,
        ))
        border = "yellow"
    head = (
        f"Static symbols: [bold]{rep.static_targets}[/bold]   "
        f"[green]confirmed: {rep.confirmed}[/green]   "
        f"[yellow]BLIND: {rep.blind}[/yellow]   "
        f"[red]dynamic-only: {rep.dynamic_only}[/red]\n"
        f"Contract trust (dynamic coverage of the static map): [bold]{trust_pct}%[/bold]"
    )
    if rep.impact_filter_active:
        head += (
            f"\nImpact focus: [bold]{len(rep.impact_paths)}[/bold] changed API path(s); "
            f"full surface: {rep.all_static_targets} static / {rep.all_blind} blind"
        )
    if rep.probed:
        head += (f"\nSandbox-probed: [bold]{rep.probed}[/bold] symbol(s) re-invoked under target — "
                 f"[{'bold red' if rep.probe_changed else 'green'}]{rep.probe_changed} changed[/]")
    if rep.diff is not None:
        if rep.verdict is VerificationVerdict.FAIL:
            verdict = "[bold red]⚠ behavior changed[/bold red]"
        elif rep.verdict is VerificationVerdict.PASS:
            verdict = "[green]✔ no observed change on the covered surface[/green]"
        else:
            verdict = "[bold yellow]? inconclusive[/bold yellow]"
        # Spell the counts out: `{'disappeared': 235, 'result_changed': 0, …}` was a
        # raw dict repr leaking into an otherwise composed report.
        parts = [f"{name.replace('_', ' ')} {count}"
                 for name, count in rep.diff.items() if count]
        detail = ", ".join(parts) if parts else "nothing moved"
        head += f"\nVersion diff: {verdict}  [dim]({detail})[/dim]"
    head = f"VERDICT: [bold {border}]{rep.verdict.value.upper()}[/bold {border}]\n" + head
    if rep.verdict_reasons:
        head += "\n" + "\n".join(f"• {reason}" for reason in rep.verdict_reasons)
    console.print(Panel(head, title="Contract Report", border_style=border, expand=False))

    visible_symbols = rep.impacted_symbols if rep.impact_filter_active else rep.symbols
    probed = [s for s in visible_symbols if s.probed]
    if probed:
        t = Table(title=f"Sandbox probes (re-invoked under target version) ({len(probed)})")
        t.add_column("Dependency symbol", style="cyan")
        t.add_column("Probe verdict")
        _pv = {"stable": "[green]stable[/green]", "changed": "[bold red]CHANGED[/bold red]",
               "error": "[dim]error[/dim]", "opaque-inputs": "[dim]opaque inputs[/dim]"}
        for s in probed:
            t.add_row(s.target, _pv.get(s.probe_status, s.probe_status or "—"))
        console.print(t)

    blind = [s for s in visible_symbols if s.status == "blind"]
    if blind:
        # The symbol name is the deliverable — it is what you go write a test for,
        # grep, or paste into --wrap. Truncating it to `apispec.ext.marshmallo…`
        # left a table that could only be looked at, never used. It folds now, and
        # the supporting columns give up their width first.
        t = Table(title=f"BLIND — static contacts no trace exercised ({len(blind)})",
                  header_style="yellow")
        t.add_column("Dependency symbol", style="cyan", overflow="fold", ratio=2)
        t.add_column("Called from (your code)", overflow="fold", ratio=2)
        t.add_column("File", style="dim", overflow="fold", ratio=1)
        for s in blind:
            t.add_row(s.target, ", ".join(s.callers) or "—", ", ".join(s.files) or "—")
        t.caption = "candidates for the sandbox (probe in isolation under old vs new)"
        console.print(t)

    dyn = [s for s in visible_symbols if s.status == "dynamic-only"]
    if dyn:
        t = Table(title=f"DYNAMIC-ONLY — observed but the static map missed ({len(dyn)})",
                  header_style="red")
        t.add_column("Dependency symbol", style="cyan", overflow="fold", ratio=2)
        t.add_column("Observed at", style="dim", overflow="fold", ratio=1)
        for s in dyn:
            t.add_row(s.target, ", ".join(s.observed_at[:3]) or "—")
        t.caption = "static under-approximation (dynamic dispatch / monkeypatch / getattr)"
        console.print(t)

    for note in rep.notes:
        console.print(f"[dim]Note: {note}[/dim]")


@app.command("report")
def report_cmd(
    project_dir: str = typer.Argument(".", help="Project root to analyze"),
    trace: str = typer.Option(None, "--trace", help="Observed trace JSONL (the dynamic numerator)"),
    against: str = typer.Option(None, "--against", help="Older trace to diff the --trace against (version diff)"),
    probe_python: str = typer.Option(None, "--probe-python", help="Target venv interpreter: sandbox-probe captured contacts under it"),
    profile: str = typer.Option(
        "exact", "--profile", help="Comparator: exact | shape | custom"
    ),
    comparator: str | None = typer.Option(
        None, "--comparator", help="Custom comparator as module:callable"
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit the report as JSON"),
):
    """Unified contract report: static map × dynamic trace → confirmed / BLIND / dynamic-only
    (+ sandbox probe, version diff).

    When --trace is omitted, auto-sources from .pymolt/contract_state.json (baseline/
    post-migration captured via `capture`) — explicit --trace/--against always override.
    """
    project_path = require_project_dir(project_dir, json_output=json_output)
    if trace:
        require_file(trace, what="--trace recording", json_output=json_output)
    if against:
        require_file(against, what="--against recording", json_output=json_output)

    try:
        selected = ComparatorProfile(profile)
        if selected is ComparatorProfile.CUSTOM:
            if comparator is None:
                raise ComparatorError("--profile custom requires --comparator module:callable")
            load_custom_comparator(comparator)
        rep = service.build_contract_report_from_state(
            project_path,
            trace_override=trace,
            against_override=against,
            probe_python=probe_python,
            comparator_profile=selected.value,
            custom_comparator=comparator,
        )
    except (ComparatorError, ValueError) as exc:
        fail(str(exc), json_output=json_output)
    if json_output:
        typer.echo(json.dumps(rep.model_dump(mode="json"), indent=2))
        raise typer.Exit(code=_verification_exit(rep.verdict))
    _render_contract_report(rep)
    next_step(project_path)
    raise typer.Exit(code=_verification_exit(rep.verdict))
