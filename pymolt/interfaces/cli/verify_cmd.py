"""CLI for verify/ — `pymolt capture`, and the contract report `pymolt verify` renders.

Everything here runs *outside* the target project: the watcher is injected via PYTHONPATH +
PYMOLT_TRACE_* env (zero edits to the target), and rendering of the structured reports
lives here in interfaces/, not in the non-interactive core (spec: render is not the core's job).

Capture/injection itself lives in ``pymolt.verify.service`` (so callers can drive the same
mechanism) — this file is CLI rendering + interactive prompting only.
"""
import json

import typer
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from pymolt.interfaces.cli.output import (
    EXIT_FINDING,
    EXIT_INCONCLUSIVE,
    EXIT_OK,
    console,
    err,
    fail,
    next_step,
    require_project_dir,
)
from pymolt.verify import service
from pymolt.verify.models import (
    CaptureMode,
    CaptureValidity,
    VerificationVerdict,
)


def _verification_exit(verdict: VerificationVerdict) -> int:
    return {
        VerificationVerdict.PASS: EXIT_OK,
        VerificationVerdict.FAIL: EXIT_FINDING,
        VerificationVerdict.INCONCLUSIVE: EXIT_INCONCLUSIVE,
    }[verdict]


# ── capture ───────────────────────────────────────────────────────────────────
_CAPTURE_WHEN = {"baseline": "baseline", "post-migration": "post_migration"}
_CAPTURE_MODE = {
    "tests": CaptureMode.TEST_SUITE,
    "command": CaptureMode.LIVE_COMMAND,
    "attach": CaptureMode.LIVE_ATTACH,
}


def _emit_capture(slot, when: str, project_dir: str, *, agent: bool, invalid_code: int) -> None:
    """--json prints the capture's slot, --agent the view; exits as the human path does."""
    from pymolt.interfaces import views

    if agent:
        typer.echo(views.dumps(views.capture_view(
            slot, when=when, state=service.load_contract_state(project_dir))))
    else:
        typer.echo(json.dumps(slot.model_dump(mode="json"), indent=2))
    if slot.validity is not CaptureValidity.VALID:
        raise typer.Exit(code=invalid_code)


def capture_cmd(
    when: str = typer.Argument(..., help="baseline (before the migration) | post-migration"),
    command: list[str] = typer.Argument(
        None, help="Command to run after --, e.g. -- pytest tests/ (omit for --mode attach)"),
    mode: str = typer.Option(
        "command", "--mode", help="tests (your test suite) | command (your app) | attach "
                                   "(you run the command; pymolt records)"),
    project_dir: str = typer.Option(
        ".", "--project-dir", help="Project directory"),
    target: str = typer.Option(
        "all", "--target", "-t",
        help="Dependencies to record: all (default), or a list such as flask,werkzeug"),
    backend: str = typer.Option(
        "auto", "--backend", help="Tracer: auto | setprofile | monitoring | wrap | hybrid"),
    exclude: str = typer.Option(
        "", "--exclude", "-x", help="Top-level packages not to record, comma-separated"),
    source: str = typer.Option(
        "", "--source", "-s", help="Your code's top-level packages (default: detected)"),
    container: str | None = typer.Option(
        None, "--container", "-c",
        help="Running container to run CMD in (default: the one setup configured)"),
    workdir: str | None = typer.Option(
        None, "--workdir", "-w", help="Working directory inside the container"),
    collect: bool = typer.Option(
        False, "--collect", help="Finish a --mode attach capture"),
    force: bool = typer.Option(
        False, "--force", help="Replace an existing capture without asking"),
    accept_failing: bool = typer.Option(
        False, "--accept-failing",
        help="Keep the capture even if CMD exits non-zero (failing tests), provided it "
             "recorded calls; it is stamped RED. A run that recorded nothing is still refused"),
    privacy: str = typer.Option(
        "auto", "--privacy",
        help="auto (values for tests, shape otherwise) | values | shape (types, no data)",
    ),
    sample_rate: float | None = typer.Option(
        None, "--sample-rate", min=0.000001, max=1.0,
        help="Fraction of calls whose values are kept. Every call is still counted. Default: "
             "1.0 for tests; 0.1 for command/attach, with the first 32 calls of each call "
             "site always kept",
    ),
    impact_targets: str | None = typer.Option(
        None, "--impact-targets",
        help="Dependency symbols to focus on (post-migration default: the applied plan's)",
    ),
    deployment_id: str | None = typer.Option(
        None, "--deployment-id", help="Deployment id to store with the capture (stage/canary)"),
    request_id: str | None = typer.Option(
        None, "--request-id", help="Request id to store with the capture"),
    correlation_id: str | None = typer.Option(
        None, "--correlation-id", help="Correlation id to store with the capture"),
    json_output: bool = typer.Option(
        False, "--json", help="Print the capture as JSON (asks nothing)"),
    agent: bool = typer.Option(
        False, "--agent", help="Print the compact agent view as JSON (what the MCP tool returns)"),
):
    """Record the calls your code makes into its dependencies while CMD runs.

    `baseline` is recorded before the migration, `post-migration` after it;
    `pymolt verify` compares the two. --mode tests and --mode command run CMD here
    and wait for it to exit (Ctrl+C ends a long-running app and keeps what was
    recorded). --mode attach prints a command to run yourself, for example in a
    container or on a server; finish it with --mode attach --collect.
    With --json or --agent it never asks: an existing capture needs --force.
    """
    machine = json_output or agent
    require_project_dir(project_dir, json_output=machine)
    if agent:
        from pathlib import Path

        project_dir = str(Path(project_dir).resolve())
    when_key = _CAPTURE_WHEN.get(when)
    if when_key is None:
        fail(f"capture what: 'baseline' or 'post-migration' (got {when!r})", json_output=machine)
    capture_mode = _CAPTURE_MODE.get(mode)
    if capture_mode is None:
        fail(f"--mode must be 'tests', 'command', or 'attach' (got {mode!r})",
             json_output=machine)
    if privacy not in {"auto", "values", "shape"}:
        fail(f"--privacy must be 'auto', 'values', or 'shape' (got {privacy!r})",
             json_output=machine)
    effective_privacy = (
        "values" if capture_mode is CaptureMode.TEST_SUITE else "shape"
    ) if privacy == "auto" else privacy
    if service.environment_fingerprint(project_dir) is None:
        # Without a config the capture records no environment, so nothing can later
        # tell whether it still describes the project, and `apply` rejects it.
        fail(
            "pymolt is not configured for this project",
            hint="Run `pymolt setup .` first. A capture records the environment it was "
                 "taken in.",
            json_output=machine,
        )

    if collect:
        out_path = service.pending_attach_path(project_dir, when_key)
        if not out_path.is_file():
            fail(
                f"no pending attach capture at {out_path}",
                hint=f"Start one first: pymolt capture {when} --mode attach",
                json_output=machine,
            )
        slot = service.finalize_attached_capture(
            project_dir, when_key, out_path, target=target,
            privacy=effective_privacy, sample_rate=sample_rate,
        )
        if machine:
            _emit_capture(slot, when, project_dir, agent=agent, invalid_code=EXIT_INCONCLUSIVE)
            return
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
    if existing is not None and not force and machine:
        fail(
            f"'{when}' already has a capture from {existing.captured_at}",
            hint="Pass --force to replace it (the old one is archived, not deleted).",
            json_output=True,
        )
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
        collect_cmd = (
            f"pymolt capture {when} --mode attach --collect --privacy {effective_privacy}"
            f"{f' --sample-rate {sample_rate}' if sample_rate is not None else ''}"
        )
        if machine:
            from pymolt.interfaces import views

            view = views.capture_attach_view(instr, when=when, collect_command=collect_cmd)
            typer.echo(views.dumps(view) if agent else json.dumps(view["data"], indent=2))
            return
        console.print(Panel(
            f"Run this yourself, then finalize with:\n"
            f"  [white]pymolt capture {when} --mode attach --collect "
            f"--privacy {effective_privacy}"
            f"{f' --sample-rate {sample_rate}' if sample_rate is not None else ''}[/white]\n\n"
            f"  [white]{instr.command_hint}[/white]",
            title="capture (attach)", expand=False, border_style="cyan",
        ))
        return

    if not command:
        fail("provide a command after --, e.g. -- pytest tests/", json_output=machine)
    err.print(f"[dim]capturing[/dim] when=[cyan]{when}[/cyan] mode=[cyan]{mode}[/cyan] "
              f"cmd=[white]{' '.join(command)}[/white]")
    slot = service.capture_named_trace(
        project_dir, when_key, capture_mode, command=command, target=target, backend=backend,
        container=container, workdir=workdir, exclude=exclude, source=source,
        privacy=effective_privacy, sample_rate=sample_rate,
        accept_failing=accept_failing,
        impact_targets=(
            [item.strip() for item in impact_targets.split(",") if item.strip()]
            if impact_targets else None
        ),
        deployment_id=deployment_id,
        request_id=request_id,
        correlation_id=correlation_id,
    )
    if machine:
        _emit_capture(slot, when, project_dir, agent=agent, invalid_code=(
            EXIT_FINDING if slot.validity is CaptureValidity.COMMAND_FAILED
            else EXIT_INCONCLUSIVE))
        return
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
    if slot.red:
        failed = slot.red.get("failed")
        err.print(
            f"[bold red]RED[/bold red] the command exited {slot.red.get('returncode')} "
            f"({failed if failed is not None else 'some'} test(s) failed); kept because of "
            "--accept-failing. The calls from failing tests are compared as usual."
        )
    if slot.duration_seconds is not None:
        err.print(
            f"[dim]duration={slot.duration_seconds:.3f}s  "
            f"dropped={slot.dropped_events if slot.dropped_events is not None else 'unknown'}  "
            f"backend={slot.backend or backend}[/dim]"
        )
    for failure in getattr(slot, "activation_failures", []) or []:
        err.print(
            "[yellow]warning[/yellow] the watcher failed to activate in pid "
            f"{failure.get('pid')} (python {failure.get('python')}): "
            f"{escape(str(failure.get('error')))} — that process ran UNTRACED, "
            "so this capture cannot support a PASS"
        )
    if slot.archived_previous:
        err.print(f"[dim]previous {when} recording kept at {slot.archived_previous}[/dim]")
    next_step(project_dir)


_TRUST_TIER_LABELS = {
    "exact": "exact-join",
    "canonical": "canonical",
    "behavioural": "behavioural",
    "executed": "executed files",
}


_FACT_LABEL = {
    "changed": "[bold red]CHANGED[/bold red]",
    "disappeared": "[bold red]DISAPPEARED[/bold red]",
    "appeared": "[bold red]APPEARED[/bold red]",
    "rewritten": "[cyan]REWRITTEN[/cyan]",
    "uncomparable": "[yellow]UNCOMPARABLE[/yellow]",
    "drift": "[dim]DRIFT[/dim]",
    "equal": "[green]equal[/green]",
}


def _fmt_outcomes(outcomes: list[dict] | None, limit: int = 70) -> str:
    """``[{kind, value, count}]`` -> ``returns {...}`` / ``raises TypeError`` (compact)."""
    if not outcomes:
        return "—"
    parts = []
    for outcome in outcomes:
        if outcome.get("kind") == "raise":
            text = f"raises {outcome.get('value') or '?'}"
        else:
            value = json.dumps(outcome.get("value"), sort_keys=True, default=str)
            text = "returns " + (value if len(value) <= limit else value[: limit - 1] + "…")
        if outcome.get("count", 1) > 1 and len(outcomes) > 1:
            text += f" ×{outcome['count']}"
        parts.append(text)
    return " | ".join(parts)


def _fmt_site(site: dict | None) -> str:
    if not site:
        return "—"
    where = f"{site.get('file', '?')}:{site.get('line', '?')}"
    return f"{where} {site.get('func')}()" if site.get("func") else where


def _render_fact_changes(fact_diff: dict | None) -> None:
    """Every non-equal call fact: what was called, where, and old -> new outcome."""
    if not fact_diff:
        return
    entries = [e for e in fact_diff.get("entries", []) if e.get("category") != "equal"]
    if not entries:
        return
    order = list(_FACT_LABEL)
    entries.sort(key=lambda e: (not e.get("fails"), order.index(e.get("category", "equal"))
                                if e.get("category") in order else len(order)))
    t = Table(title=f"Behaviour diff — call facts that are not equal ({len(entries)})",
              header_style="bold")
    t.add_column("Change", no_wrap=True)
    t.add_column("Dependency call", style="cyan", overflow="fold", ratio=2)
    t.add_column("Call site (your code)", overflow="fold", ratio=2)
    t.add_column("Before → after", overflow="fold", ratio=3)
    for e in entries:
        symbol = e.get("symbol", "?")
        if e.get("new_symbol"):
            symbol = f"{symbol} → {e['new_symbol']}"
        site = escape(_fmt_site(e.get("old_site") or e.get("new_site")))
        old_text = (e.get("old_site") or {}).get("text")
        new_text = (e.get("new_site") or {}).get("text")
        if e.get("category") == "rewritten" and old_text and new_text and old_text != new_text:
            site += f"\n[dim]{escape(old_text)}\n→ {escape(new_text)}[/dim]"
        if e.get("category") == "drift":
            change = escape(f"defaults {e.get('old_defaulted') or []} → "
                            f"{e.get('new_defaulted') or []}")
        else:
            change = escape(f"{_fmt_outcomes(e.get('old'))}\n→ {_fmt_outcomes(e.get('new'))}")
            if e.get("level") == "type":
                change += "  [dim](compared by type)[/dim]"
        t.add_row(_FACT_LABEL.get(e.get("category"), e.get("category", "?")),
                  escape(symbol), site, change)
    t.caption = ("REWRITTEN: the call site's code changed between captures; DRIFT: the "
                 "library's defaults changed (reported, not failing)")
    console.print(t)


_CALIBRATION_LABEL = {
    "hit": "[bold red]hit[/bold red]",
    "prevented": "[green]prevented by the plan[/green]",
    "miss": "[green]miss[/green]",
    "not_exercised": "[dim]not exercised[/dim]",
}


def _render_calibration(cal) -> None:
    """Predicted vs observed: each predicted risk against the fact diff, then the failing
    changes no prediction covered."""
    if cal is None:
        return
    source = ("the plan's predictions" if cal.source == "plan"
              else "predictions from the baseline's runtime warnings")
    if cal.risks:
        prevented = f"{cal.prevented} prevented by the plan, " if cal.prevented else ""
        t = Table(title=f"Calibration — {source}: {cal.hit} hit, {prevented}{cal.miss} miss, "
                        f"{cal.not_exercised} not exercised", header_style="bold")
        t.add_column("Prediction", no_wrap=True)
        t.add_column("Result", no_wrap=True)
        t.add_column("Dependency call", style="cyan", overflow="fold", ratio=2)
        t.add_column("Call site (your code)", overflow="fold", ratio=2)
        t.add_column("Observed", overflow="fold", ratio=2)
        for risk in cal.risks:
            observed = ", ".join(f"{o.category} {o.symbol.rsplit('.', 1)[-1]}"
                                 for o in risk.observed) or (risk.note or "—")
            if risk.fixed_by_plan:
                observed += " [dim](fixed by the plan)[/dim]"
            t.add_row(risk.tier.value, _CALIBRATION_LABEL.get(risk.result.value,
                                                              risk.result.value),
                      escape(risk.symbol), escape(risk.site.label()), observed)
        console.print(t)
    else:
        console.print(f"[dim]Calibration: {source} named no risk.[/dim]")
    if cal.unpredicted:
        console.print(f"[bold yellow]{len(cal.unpredicted)} observed change(s) no prediction "
                      "covered:[/bold yellow]")
        for item in cal.unpredicted[:20]:
            console.print(f"  {item.category} {escape(item.symbol)} at "
                          f"{escape(item.site.label())}")
    for note in cal.notes:
        console.print(f"[dim]  note: {escape(note)}[/dim]")



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
            "[bold yellow]The evidence below does not describe the world it stands "
            "for.[/bold yellow]\n"
            "A capture was taken against a different manifest/environment than the one it "
            "must describe — the project on disk now, or, for the baseline of a before/after "
            "diff, the one the migration started from. The notes below say which.\n"
            "[dim]Re-capture (pymolt capture) to judge the current code.[/dim]",
            title="⚠ stale evidence", title_align="left", border_style="yellow", expand=False,
        ))
        border = "yellow"
    head = (
        f"Static symbols: [bold]{rep.static_targets}[/bold]   "
        f"[green]confirmed: {rep.confirmed}[/green]   "
        f"[yellow]BLIND: {rep.blind}[/yellow]   "
        f"[red]dynamic-only: {rep.dynamic_only}[/red]\n"
        f"Contract trust: [bold]{trust_pct}%[/bold]  "
        f"[dim]({rep.trust_confirmed}/{rep.trust_static} {escape(rep.trust_basis)})[/dim]"
    )
    if rep.trust_tiers:
        # Every step from the old exact-string join to the headline, so a jump in
        # trust is auditable rather than a single opaque number.
        tiers = " → ".join(
            f"{_TRUST_TIER_LABELS.get(t.name, t.name)} {round(t.trust * 100)}% "
            f"({t.confirmed}/{t.static})"
            for t in rep.trust_tiers
        )
        head += f"\n[dim]Trust tiers: {tiers}[/dim]"
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
        fact_counts = (rep.fact_diff or {}).get("counts")
        source = fact_counts if fact_counts else rep.diff
        parts = [f"{name.replace('_', ' ')} {count}"
                 for name, count in source.items() if count]
        detail = ", ".join(parts) if parts else "nothing moved"
        head += f"\nVersion diff: {verdict}  [dim]({detail})[/dim]"
    head = f"VERDICT: [bold {border}]{rep.verdict.value.upper()}[/bold {border}]\n" + head
    if rep.verdict_reasons:
        head += "\n" + "\n".join(f"• {reason}" for reason in rep.verdict_reasons)
    console.print(Panel(head, title="Contract Report", border_style=border, expand=False))
    _render_fact_changes(rep.fact_diff)
    _render_calibration(getattr(rep, "calibration", None))

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
