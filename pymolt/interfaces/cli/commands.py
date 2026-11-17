import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from typer.core import TyperGroup

from pymolt.assess.service import (
    _resolve_baseline_graph,
    baseline_tests_passed,
    classify_baseline_tier,
    compatibility_verdict,
    failure_evidence,
)
from pymolt.assess.service import build_comparison_rows as _build_comparison_rows
from pymolt.assess.service import resolve_chosen_source as _service_resolve_chosen_source
from pymolt.assess.service import resolve_target_graph as _resolve_target_graph
from pymolt.assess.service import write_target_manifest as _write_target_manifest
from pymolt.interfaces.cli.output import (
    EXIT_ENVIRONMENT,
    EXIT_INCONCLUSIVE,
    EXIT_USAGE,
    console,
    err,
    fail,
    next_step,
    require_project_dir,
    warn,
)
from pymolt.interfaces.cli.verify_cmd import capture_cmd
from pymolt.setup.pythons import (
    classify_eol,
    get_available_python_versions,
    get_eol_versions,
    version_floor,
)


# Typer renders sub-app panels after command panels, so "Behavioural proof" lands at
# the bottom of --help no matter where it is registered. The help text carries the
# lead instead: the baseline capture is the one step that cannot be redone later.
class _FlowOrderGroup(TyperGroup):
    """List commands in the order a migration runs them, not the order they are defined."""

    def list_commands(self, ctx):
        names = super().list_commands(ctx)
        return sorted(names, key=lambda n: (
            _FLOW_ORDER.index(n) if n in _FLOW_ORDER else len(_FLOW_ORDER), names.index(n),
        ))


_FLOW_ORDER = [
    "status", "scan", "setup", "assess", "capture", "plan", "apply", "verify", "rollback",
]


app = typer.Typer(
    cls=_FlowOrderGroup,
    help=(
        "pymolt: prove a Python migration did not change what your code does.\n\n"
        "Order: setup → assess → capture baseline → plan → apply → "
        "capture post-migration → verify.\n"
        "`pymolt status` shows where you are and the next command.\n\n"
        "Capture the baseline before any code change; it cannot be recreated later:\n"
        "  pymolt capture baseline --mode tests -- pytest tests/"
    ),
    invoke_without_command=True,
)


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

# The behavioural proof — capture both sides, then verify. Its own help panel: this is the
# evidence the rest of the funnel exists to produce.
app.command("capture", rich_help_panel="Behavioural proof")(capture_cmd)


#: Every command's --agent: the compact payload in pymolt.interfaces.views, the same
#: object the MCP tool of that name returns. --json stays the full payload.
_AGENT_HELP = "Print the compact agent view as JSON (what the MCP tool returns)"

_STATE_MARK = {
    "done": ("✓", "green"),
    "todo": ("○", "dim"),
    # Not an error colour: work that no longer applies is a fact to act on, not a
    # failure. But it must not read as "done" either.
    "stale": ("!", "yellow"),
    "blocked": ("·", "yellow"),
}

_CHECK_MARK = {
    "ok": ("✓", "green"),
    "warning": ("!", "yellow"),
    "error": ("✗", "red"),
}


@app.command(rich_help_panel="Migration")
def status(
    project_dir: str = typer.Argument(".", help="Project directory"),
    json_output: bool = typer.Option(False, "--json", help="Print the status as JSON"),
    agent: bool = typer.Option(False, "--agent", help=_AGENT_HELP),
):
    """Show where the migration stands and the next command.

    Reads .pymolt/ and the target manifest, and checks that the configured
    environments exist. Writes nothing. The order is advice: every command can
    run at any time.
    """
    import json as json_lib

    from pymolt.status import build_status

    project_path = require_project_dir(project_dir, json_output=json_output or agent)
    if agent:
        project_path = project_path.resolve()
    report = build_status(project_path, check_environments=True)

    if agent:
        from pymolt.interfaces import views

        typer.echo(views.dumps(views.status_view(report)))
        return
    if json_output:
        typer.echo(json_lib.dumps(report.model_dump(mode="json"), indent=2))
        return

    console.print(f"[bold]pymolt[/bold] [dim]{report.project_dir}[/dim]\n")
    for phase in report.phases:
        glyph, colour = _STATE_MARK.get(phase.state, ("○", "dim"))
        line = f"  [{colour}]{glyph}[/{colour}] [bold]{phase.phase:<15}[/bold]"
        detail = phase.blocker or phase.detail
        console.print(f"{line} [{'dim' if phase.state == 'todo' else colour}]{detail}[/]")

    for check in report.checks:
        glyph, colour = _CHECK_MARK.get(check.state, ("!", "yellow"))
        detail = check.detail + (f" — {check.hint}" if check.hint else "")
        console.print(
            f"  [{colour}]{glyph}[/{colour}] [bold]{check.name:<15}[/bold] "
            f"[{'dim' if check.state == 'ok' else colour}]{detail}[/]"
        )

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


@app.command("verify", rich_help_panel="Behavioural proof")
def verify_migration(
    project_dir: str = typer.Argument(".", help="Project directory"),
    profile: str = typer.Option(
        "exact", "--profile", help="How values are compared: exact | shape | custom"
    ),
    comparator: str | None = typer.Option(
        None, "--comparator", help="module:callable for --profile custom"
    ),
    probe_python: str | None = typer.Option(
        None, "--probe-python", help="Target interpreter to replay the recorded calls in"
    ),
    baseline_file: str | None = typer.Option(
        None, "--baseline",
        help="Use this recording as the baseline instead of the captured one (the verdict "
             "is then not saved)",
    ),
    post_file: str | None = typer.Option(
        None, "--post-migration",
        help="Use this recording as the post-migration side instead of the captured one "
             "(saved; status counts only a verdict on the captured one)",
    ),
    golden_before: str | None = typer.Option(
        None, "--golden-before", help="Golden snapshot JSON from before the migration"
    ),
    golden_after: str | None = typer.Option(
        None, "--golden-after", help="Golden snapshot JSON from after the migration"
    ),
    cascade_file: str | None = typer.Option(
        None, "--cascade", help="Cascade report JSON to include"
    ),
    policy_file: str | None = typer.Option(
        None, "--policy", help="Canary policy JSON"
    ),
    metrics_file: str | None = typer.Option(
        None, "--metrics", help="Canary metrics JSON (needs --policy)"
    ),
    shadow_file: str | None = typer.Option(
        None, "--shadow", help="Shadow operations JSON; unsafe writes are left out"
    ),
    auto_rollback: bool = typer.Option(
        False,
        "--auto-rollback",
        help="Run rollback when a required canary gate fails",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print the verdict as JSON"),
    agent: bool = typer.Option(False, "--agent", help=_AGENT_HELP),
    verbose: bool = typer.Option(
        False, "--verbose", "-v",
        help="Also show what was explained away (mechanism, count, example, rule)"),
):
    """Compare the baseline and post-migration captures. Prints PASS, FAIL or INCONCLUSIVE.

    The verdict is saved to .pymolt/ for `pymolt status`. Golden snapshots, a
    cascade report and canary metrics, when given, are folded into the same
    verdict. --baseline/--post-migration compare other recordings instead of the
    captures. A verdict against another --baseline is not saved; one on another
    --post-migration recording is saved, with what it compared, but `status` does
    not count it as the migration's verdict.
    """
    import json

    from pymolt.interfaces.cli.output import require_file
    from pymolt.interfaces.cli.verify_cmd import (
        _render_contract_report,
        _verification_exit,
    )
    from pymolt.migration_plan import MigrationPlanError, rollback_migration
    from pymolt.migration_state import MigrationReceipt
    from pymolt.verify import service
    from pymolt.verify.comparators import (
        ComparatorError,
        ComparatorProfile,
        load_custom_comparator,
    )
    from pymolt.verify.golden_master import diff_snapshots
    from pymolt.verify.models import VerificationVerdict, VerifyReport
    from pymolt.verify.policy import (
        AutoRollbackPolicy,
        CanaryMetrics,
        CanaryPolicy,
        decide_auto_rollback,
        evaluate_canary_policy,
    )
    from pymolt.verify.shadow import (
        ShadowOperation,
        ShadowPolicy,
        classify_shadow_operations,
    )
    from pymolt.verify.unified import build_unified_verification

    json_output = json_output or agent  # --agent is machine output too: failures stay JSON
    project_path = require_project_dir(project_dir, json_output=json_output)
    if agent:
        project_path = project_path.resolve()
    for path_value, what in ((baseline_file, "--baseline recording"),
                             (post_file, "--post-migration recording")):
        if path_value:
            require_file(path_value, what=what, json_output=json_output)
    if post_file and not baseline_file:
        # The other side still comes from the captures.
        slot = service.load_contract_state(project_path).baseline
        if slot is not None and slot.trace_path:
            captured = Path(slot.trace_path)
            baseline_file = str(captured if captured.is_absolute() else project_path / captured)

    def load_json(path_value: str, label: str):
        path = Path(path_value)
        if not path.is_file():
            fail(f"no such {label}: {path}", json_output=json_output)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            fail(f"cannot read {label}: {exc}", json_output=json_output)

    try:
        selected = ComparatorProfile(profile)
        if selected is ComparatorProfile.CUSTOM:
            if comparator is None:
                raise ComparatorError("--profile custom requires --comparator module:callable")
            load_custom_comparator(comparator)
        contract_report = service.build_contract_report_from_state(
            project_path,
            trace_override=post_file,
            against_override=baseline_file,
            probe_python=probe_python,
            comparator_profile=selected.value,
            custom_comparator=comparator,
        )

        if bool(golden_before) != bool(golden_after):
            raise ValueError("--golden-before and --golden-after must be supplied together")
        golden = None
        if golden_before and golden_after:
            golden = diff_snapshots(
                load_json(golden_before, "old golden snapshot"),
                load_json(golden_after, "new golden snapshot"),
            )
        cascade = (
            VerifyReport.model_validate(load_json(cascade_file, "cascade report"))
            if cascade_file else None
        )

        if metrics_file and not policy_file:
            raise ValueError("--metrics requires --policy")
        canary = None
        if policy_file:
            policy = CanaryPolicy.model_validate(load_json(policy_file, "canary policy"))
            metrics = CanaryMetrics.model_validate(
                load_json(metrics_file, "canary metrics") if metrics_file else {}
            )
            canary = evaluate_canary_policy(policy, metrics)

        shadow = None
        if shadow_file:
            payload = load_json(shadow_file, "shadow manifest")
            if isinstance(payload, list):
                operations_raw, shadow_policy_raw = payload, {}
            elif isinstance(payload, dict):
                operations_raw = payload.get("operations", [])
                shadow_policy_raw = payload.get("policy", {})
            else:
                raise ValueError("shadow manifest must be a list or object")
            operations = [ShadowOperation.model_validate(item) for item in operations_raw]
            shadow = classify_shadow_operations(
                operations, ShadowPolicy.model_validate(shadow_policy_raw)
            )

        receipt = MigrationReceipt.load(project_path)
        run_id = receipt.run_id if receipt is not None else None
        rollback_decision = None
        if auto_rollback:
            if canary is None:
                raise ValueError("--auto-rollback requires --policy and canary metrics")
            rollback_decision = decide_auto_rollback(
                canary,
                AutoRollbackPolicy(enabled=True),
                receipt=receipt,
                run_id=run_id,
            )

        unified = build_unified_verification(
            contract_report,
            run_id=run_id,
            comparator_profile=selected.value,
            cascade=cascade,
            golden=golden,
            canary=canary,
            shadow=shadow,
            rollback=rollback_decision,
        )
        if rollback_decision is not None and rollback_decision.should_rollback:
            rollback_migration(project_path, run_id=rollback_decision.run_id)
            unified.rollback_executed = True
        # Saved with what it compared (``contract.compared``), unless the baseline side is
        # another recording: a verdict against another baseline is not this project's.
        # ``status`` counts only a verdict on the post-migration capture.
        compared = contract_report.compared
        foreign_baseline = (compared is not None and compared.before is not None
                            and compared.before_capture != "baseline")
        saved_path = None if foreign_baseline else unified.save(project_path)
    except (ComparatorError, ValueError) as exc:
        fail(
            f"verification input is invalid: {exc}",
            code=EXIT_USAGE,
            json_output=json_output,
        )
    except (MigrationPlanError, OSError) as exc:
        fail(
            f"verification could not complete: {exc}",
            code=EXIT_ENVIRONMENT,
            json_output=json_output,
        )

    if agent:
        from pymolt.interfaces import views

        typer.echo(views.dumps(views.verify_view(unified, project_path, saved_path=saved_path)))
        raise typer.Exit(code=_verification_exit(unified.verdict))
    if json_output:
        typer.echo(json.dumps(unified.model_dump(mode="json"), indent=2))
        raise typer.Exit(code=_verification_exit(unified.verdict))

    _render_contract_report(contract_report, verbose=verbose)
    if canary is not None:
        table = Table(title="Canary policy", show_header=True)
        table.add_column("Metric")
        table.add_column("Observed")
        table.add_column("Maximum")
        table.add_column("Verdict")
        for item in canary.evidence:
            table.add_row(
                item.metric.value,
                "missing" if item.observed is None else str(item.observed),
                str(item.threshold),
                item.verdict.value,
            )
        console.print(table)
    if shadow is not None:
        console.print(
            f"[bold]Shadow safety:[/bold] {shadow.eligible_count} eligible, "
            f"[yellow]{shadow.suppressed_count} suppressed[/yellow]"
        )
    border = {
        VerificationVerdict.PASS: "green",
        VerificationVerdict.FAIL: "red",
        VerificationVerdict.INCONCLUSIVE: "yellow",
    }[unified.verdict]
    from pymolt.interfaces import views

    rollback_note = (
        "\n[bold red]Automatic rollback executed.[/bold red]"
        if unified.rollback_executed else ""
    )
    saved_lines = [f"saved: {saved_path}"] if saved_path else []
    if note := views.saved_verdict_note(contract_report.compared, saved_path):
        saved_lines.append(note)
    console.print(Panel(
        f"[bold {border}]{unified.verdict.value.upper()}[/bold {border}]"
        f"{rollback_note}\n[dim]{escape(chr(10).join(saved_lines))}[/dim]",
        title="Verdict", border_style=border, expand=False,
    ))
    raise typer.Exit(code=_verification_exit(unified.verdict))


@app.command(rich_help_panel="Migration")
def plan(
    project_dir: str = typer.Argument(".", help="Project directory"),
    target_file: str | None = typer.Option(
        None, "--target-manifest",
        help="Target manifest to plan for (default: the one assess wrote)",
    ),
    target_file_old: str | None = typer.Option(None, "--target-file", hidden=True),
    endpoint: str | None = typer.Option(
        None, "--endpoint",
        help="Use a remote Axiom at this URL (default: none, API changes are computed locally)",
    ),
    online: bool = typer.Option(
        False, "--online",
        help="Let the local engine read intermediate releases from PyPI for successor hints "
             "(package names and versions only)",
    ),
    output: str | None = typer.Option(
        None, "--output", "-o", help="Where to save the plan (default: .pymolt/migration_plan.json)"
    ),
    patch_file: str | None = typer.Option(
        None, "--patch", help="Also write the diff to this file"
    ),
    json_output: bool = typer.Option(False, "--json", help="Print the plan summary as JSON"),
    agent: bool = typer.Option(False, "--agent", help=_AGENT_HELP),
):
    """Save the codemods for the target manifest as a plan and show the diff.

    Writes no source file. `pymolt apply` applies the plan and refuses it if a
    planned file changed since.
    """
    import json

    from pymolt.codemods.client import AxiomGraphError
    from pymolt.codemods.service import PlanInputError, build_migration_plan
    from pymolt.config import load_endpoint

    json_output = json_output or agent  # --agent is machine output too: failures stay JSON
    project_path = require_project_dir(project_dir, json_output=json_output)
    if agent:
        project_path = project_path.resolve()
    target_file = target_file or target_file_old
    try:
        with err.status("Building the migration plan…", spinner="dots") as status:
            migration_plan, previews, source_desc, by_pkg = build_migration_plan(
                project_path,
                target_file=target_file,
                base_url=load_endpoint(endpoint),
                progress=lambda msg: status.update(f"[cyan]{msg}[/cyan]"),
                online=online,
            )
        saved_path = migration_plan.save(project_path, output)
    except PlanInputError as exc:
        fail(str(exc), hint=exc.hint or None, json_output=json_output)
    except (AxiomGraphError, OSError, RuntimeError, ValueError) as exc:
        fail(
            f"could not create migration plan: {exc}",
            hint="No source files were changed.",
            code=EXIT_ENVIRONMENT,
            json_output=json_output,
        )

    if patch_file:
        patches = _unified_patches(previews, project_path)
        patch_path = Path(patch_file)
        if not patch_path.is_absolute():
            patch_path = project_path / patch_path
        patch_path.parent.mkdir(parents=True, exist_ok=True)
        patch_path.write_text("\n".join(patches), encoding="utf-8")
    else:
        patch_path = None

    payload = {
        "plan_id": migration_plan.plan_id,
        "plan_path": str(saved_path),
        "source": source_desc,
        "dependencies": len(migration_plan.dependencies),
        "files_scanned": migration_plan.files_scanned,
        "files_to_change": len(migration_plan.changes),
        "rewrite_sites": sum(item.sites for item in migration_plan.changes),
        "advisories": len(migration_plan.advisories),
        "manual": [item.model_dump(mode="json") for item in migration_plan.manual],
        "api_impacts": len(migration_plan.impacts),
        "patch_path": str(patch_path) if patch_path else None,
        "risks": _risks_payload(migration_plan.risks),
        "python": migration_plan.python,
        "compile_findings": migration_plan.compile_findings,
    }
    if agent:
        from pymolt.interfaces import views

        typer.echo(views.dumps(views.plan_view(
            migration_plan, by_pkg, source_desc, plan_path=saved_path)))
        return
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
        return

    console.print(Panel(
        f"Plan [bold cyan]{migration_plan.plan_id[:8]}[/bold cyan] saved to\n"
        f"[green]{saved_path}[/green]\n\n"
        f"Dependencies: [bold]{payload['dependencies']}[/bold]   "
        f"files: [bold]{payload['files_to_change']}[/bold]   "
        f"rewrite sites: [bold]{payload['rewrite_sites']}[/bold]   "
        f"API impacts: [bold]{payload['api_impacts']}[/bold]   "
        f"advisories: [yellow]{payload['advisories']}[/yellow]   "
        f"manual: [yellow]{len(migration_plan.manual)}[/yellow]",
        title="Migration plan", border_style="cyan", expand=False,
    ))
    _render_recipes(by_pkg, previews, migration_plan.files_scanned, migration_plan.risks,
                    manual=migration_plan.manual)
    from pymolt.interfaces.views import dependency_risks

    deps = dependency_risks(migration_plan.risks)
    _render_risks(deps if deps is not None and (
        migration_plan.baseline_trace_path or deps.risks or deps.deltas) else None)
    _render_python(migration_plan)
    _render_or_write_patches(previews, project_path, None)
    console.print(
        f"\n[bold]Next:[/bold] pymolt apply {project_path} --plan {saved_path}"
    )


def _render_python(plan) -> None:
    """The python move's risks: one summary line, the sites, and what is listed unrated."""
    from pymolt.interfaces.views import _manual_hint, python_summary_line

    line = python_summary_line(plan)
    if line is None:
        return
    risks = [r for r in plan.risks.risks if r.scope == "python"] if plan.risks else []
    if risks:
        table = Table(title="Python risks", title_justify="left", border_style="yellow")
        for column in ("risk", "site", "symbol", "why"):
            table.add_column(column)
        for risk in risks:
            style = _TIER_STYLE.get(risk.tier.value, "")
            table.add_row(f"[{style}]{risk.tier.value}[/{style}]", risk.site.label(),
                          risk.symbol, risk.reason + (
                              " (fixed by the plan)" if risk.fixed_by_plan
                              else f" (manual: {_manual_hint(risk)})" if risk.fix == "advisory"
                              and _manual_hint(risk) else " (manual)" if risk.fix == "advisory"
                              else ""))
        console.print(table)
    console.print(f"\n[bold]{line}[/bold]")
    for listed in (plan.risks.guarded if plan.risks else []):
        console.print(f"[dim]  listed, not rated: {listed.site.label()} {listed.symbol} "
                      f"({listed.kind})[/dim]")
    if plan.compile_findings:
        console.print(f"[dim]  compile check: {len(plan.compile_findings)} finding(s) on "
                      f"Python {plan.python.get('to')} (see the saved plan)[/dim]")


def _risks_payload(risks) -> dict | None:
    """The plan's predicted risks for --json: counts, then both groups in full."""
    if risks is None:
        return None
    dump = risks.model_dump(mode="json")
    return {
        "status": risks.status,
        "counts": risks.counts(),
        "fixed_by_plan": [r for r in dump["risks"] if r["fixed_by_plan"]],
        "needs_attention": [r for r in dump["risks"] if not r["fixed_by_plan"]],
        "deltas": dump["deltas"],
        "notes": dump["notes"],
    }


_TIER_STYLE = {"certain": "bold red", "high": "red", "medium": "yellow", "low": "dim"}


def _render_risks(risks) -> None:
    """Predicted risks per call site, split into what the plan fixes and what it does not."""
    if risks is None:
        console.print("\n[dim]No predicted risks: there is no baseline capture to predict "
                      "from (`pymolt capture baseline`).[/dim]")
        return
    for title, group, border in (
        ("Needs attention", risks.needs_attention, "yellow"),
        ("Fixed by the plan", risks.fixed, "green"),
    ):
        if not group:
            continue
        table = Table(title=f"Predicted risks — {title.lower()}", title_justify="left",
                      border_style=border)
        table.add_column("risk")
        table.add_column("site")
        table.add_column("symbol")
        table.add_column("why")
        for risk in group:
            style = _TIER_STYLE.get(risk.tier.value, "")
            why = risk.reason + ("" if risk.basis == "trace" else " (static only)")
            table.add_row(f"[{style}]{risk.tier.value}[/{style}]", risk.site.label(),
                          risk.symbol, why)
        console.print(table)
    if not risks.risks:
        from pymolt.interfaces.views import inconclusive_deltas, unknown_api_phrase

        unknown, total = inconclusive_deltas(risks)
        if unknown:
            console.print(f"\n[yellow]Predicted risks: none rated[/yellow] — "
                          f"{unknown_api_phrase(unknown, total)} (inconclusive).")
        else:
            console.print("\n[green]No predicted risks[/green] at the call sites the baseline "
                          "exercised.")
    else:
        from pymolt.interfaces.views import inconclusive_deltas, unknown_api_phrase

        unknown, total = inconclusive_deltas(risks)
        if unknown:
            console.print(f"\n[yellow]Predicted risks[/yellow]: {len(risks.needs_attention)} "
                          f"need attention, {len(risks.fixed)} fixed by the plan; "
                          f"{unknown_api_phrase(unknown, total)} (inconclusive).")
    for note in risks.notes:
        console.print(f"[dim]  note: {note}[/dim]")


def _render_recipes(by_pkg, previews, files_scanned: int, risks=None, manual=()) -> None:
    """What the service returned per package, what needs a person, and whether it matched.
    ``manual``: the plan's manual items (rules that matched but are not applied)."""
    from pymolt.codemods.models import CodemodPattern

    for pkg, items in by_pkg.items():
        if not items:
            continue
        table = Table(title=f"Codemods — {pkg}")
        table.add_column("kind")
        table.add_column("change")
        table.add_column("conf")
        for item in items:
            if isinstance(item, CodemodPattern):
                change = f"{item.old_qualname} → {item.new_qualname}"
            elif item.old_qualname and item.new_qualname:
                change = f"{item.old_qualname} → {item.new_qualname}"
            else:
                change = f"{item.match} → {item.rewrite[0]}"
            table.add_row(item.kind or "template", change, item.confidence)
        console.print(table)

    advised = [p for p in previews if p.advisories]
    if advised or manual:
        console.print("\n[bold yellow]Manual review needed:[/bold yellow]")
        for preview in advised:
            for adv in preview.advisories:
                tag = "caveat" if adv.severity == "precondition" else "not applied"
                console.print(
                    f"  [yellow]{preview.path}:{adv.line}[/yellow] — [{tag}] {adv.reason}"
                )
        for item in manual:
            where = (f"{item.path}:{','.join(map(str, item.lines))}" if item.path and item.lines
                     else item.path or "no site")
            console.print(f"  [yellow]{escape(where)}[/yellow] — \\[blocked rule] "
                          f"{escape(item.rule)}: {escape(item.reason)}")
    if by_pkg and not any(p.old_source != p.new_source for p in previews):
        from pymolt.interfaces.views import inconclusive_deltas

        unknown, total = inconclusive_deltas(risks)
        unknown_tail = (f", but API changes are unknown for {unknown} of {total} package(s)"
                        if unknown else "")
        console.print(Panel(
            f"[bold]No call sites matched.[/bold]\n"
            f"Scanned {files_scanned} file(s); none of the returned recipes apply to "
            f"this codebase. The plan changes no file{unknown_tail}.",
            border_style="yellow", padding=(0, 1), expand=False,
        ))


@app.command("apply", rich_help_panel="Migration")
def apply_plan_cmd(
    project_dir: str = typer.Argument(".", help="Project directory"),
    plan_file: str | None = typer.Option(
        None, "--plan", help="Plan file (default: .pymolt/migration_plan.json)"
    ),
    allow_no_baseline: bool = typer.Option(
        False,
        "--allow-no-baseline",
        help="Apply without a baseline capture; verify will be INCONCLUSIVE",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print the receipt as JSON"),
):
    """Apply the saved plan. Keeps the original files for `pymolt rollback`.

    Refuses the plan if a planned file changed since `pymolt plan`, or if the
    baseline capture is missing or does not match the project.
    """
    import json

    from pymolt.migration_plan import (
        MigrationConflictError,
        MigrationEvidenceError,
        MigrationPlan,
        MigrationPlanError,
        apply_migration_plan,
        plan_path,
    )

    project_path = require_project_dir(project_dir, json_output=json_output)
    migration_plan = MigrationPlan.load(project_path, plan_file)
    if migration_plan is None:
        fail(
            f"migration plan is missing or invalid: {plan_path(project_path, plan_file)}",
            hint="Create a fresh plan with `pymolt plan .`.",
            json_output=json_output,
        )
    try:
        receipt = apply_migration_plan(
            project_path, migration_plan, allow_no_baseline=allow_no_baseline
        )
    except MigrationEvidenceError as exc:
        fail(
            f"migration plan needs evidence: {exc}",
            hint=exc.hint or "Capture a valid baseline and regenerate the plan.",
            code=EXIT_INCONCLUSIVE,
            json_output=json_output,
        )
    except MigrationConflictError as exc:
        fail(
            f"migration plan conflicts with current state: {exc}",
            hint=exc.hint or "Review current files/run state and regenerate the plan if needed.",
            code=EXIT_USAGE,
            json_output=json_output,
        )
    except (MigrationPlanError, OSError, RuntimeError) as exc:
        fail(
            f"migration plan was not applied: {exc}",
            hint=getattr(exc, "hint", None)
            or "Nothing was written. Run `pymolt plan .` again if files changed.",
            code=EXIT_ENVIRONMENT,
            json_output=json_output,
        )

    if json_output:
        typer.echo(json.dumps({**receipt.model_dump(mode="json"), "outcome": receipt.outcome},
                              indent=2))
        return
    plan_label = f"[bold cyan]{migration_plan.plan_id[:8]}[/bold cyan]"
    if receipt.run_id != migration_plan.plan_id:
        plan_label += f" as run [bold cyan]{receipt.run_id[:8]}[/bold cyan]"
    if receipt.outcome == "already_applied":
        console.print(Panel(
            f"Plan {plan_label} is already applied "
            f"(at {receipt.applied_at}): its {len(receipt.files_changed)} file(s) still hold "
            "the plan's changes. [bold]Nothing was written.[/bold]",
            title="Already applied", border_style="green", expand=False,
        ))
    elif not receipt.files_changed:
        console.print(Panel(
            f"Applied plan {plan_label}: "
            "[bold]it changed no file[/bold] (0 files, 0 rewrite sites).\n"
            "No call site needed a rewrite; the dependency moves still need testing: "
            "capture and verify decide.",
            title="Nothing to change", border_style="yellow", expand=False,
        ))
    else:
        console.print(Panel(
            f"Applied plan {plan_label}\n"
            f"Files changed: [bold]{len(receipt.files_changed)}[/bold]   "
            f"rewrite sites: [bold]{receipt.patterns_applied}[/bold]\n"
            f"Rollback: [bold]pymolt rollback {project_path}[/bold]",
            title="Migration applied", border_style="green", expand=False,
        ))
    next_step(project_path)


@app.command(rich_help_panel="Migration")
def rollback(
    project_dir: str = typer.Argument(".", help="Project directory"),
    run_id: str | None = typer.Option(
        None, "--run-id", help="Apply run to undo (default: the last)"
    ),
    force: bool = typer.Option(
        False, "--force", help="Also discard edits made after apply"
    ),
    json_output: bool = typer.Option(False, "--json", help="Print the result as JSON"),
):
    """Undo the last apply: restore the files it changed.

    Refuses if a file was edited after apply, unless --force.
    """
    import json

    from pymolt.migration_plan import (
        MigrationConflictError,
        MigrationPlanError,
        rollback_migration,
    )

    project_path = require_project_dir(project_dir, json_output=json_output)
    try:
        migration_plan, rolled_back_at, rolled_run = rollback_migration(
            project_path, run_id=run_id, force=force
        )
    except MigrationConflictError as exc:
        fail(
            f"migration rollback conflicts with current state: {exc}",
            hint="Post-apply edits are preserved unless you explicitly pass --force.",
            code=EXIT_USAGE,
            json_output=json_output,
        )
    except (MigrationPlanError, OSError, RuntimeError) as exc:
        fail(
            f"migration was not rolled back: {exc}",
            hint="Post-apply edits are preserved unless you explicitly pass --force.",
            code=EXIT_ENVIRONMENT,
            json_output=json_output,
        )
    from pymolt.verify.service import load_contract_state, set_aside_capture

    # What became of the post-migration capture: kept while the code it recorded is still on
    # disk (the run had changed no file), set aside (kept among the diagnostics) otherwise.
    contract = load_contract_state(project_path)
    aside = set_aside_capture(contract)
    post, post_trace = None, None
    if contract.post_migration is not None:
        post, post_trace = "kept", contract.post_migration.trace_path
    elif aside is not None and aside.set_aside_by == rolled_run:
        post, post_trace = "set_aside", aside.trace_path
    payload = {
        "run_id": rolled_run,
        "plan_id": migration_plan.plan_id,
        "rolled_back_at": rolled_back_at,
        "files_restored": len(migration_plan.changes),
        "post_migration_capture": post,
        "post_migration_trace": post_trace,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
        return
    post_line = {
        "kept": "\nPost-migration capture: kept (the run had changed no file)",
        "set_aside": f"\nPost-migration capture: set aside, kept at {post_trace}",
    }.get(post or "", "")
    console.print(Panel(
        f"Rolled back run [bold cyan]{rolled_run[:8]}[/bold cyan]\n"
        f"Files restored: [bold]{len(migration_plan.changes)}[/bold]{escape(post_line)}",
        title="Migration rolled back", border_style="yellow", expand=False,
    ))
    next_step(project_path)


@app.command(rich_help_panel="Migration")
def scan(
    project_dir: str = typer.Argument(".", help="Project or repository directory"),
    json_output: bool = typer.Option(False, "--json", help="Print the scan as JSON"),
    agent: bool = typer.Option(False, "--agent", help=_AGENT_HELP),
):
    """Find manifests, Python versions, dependencies, and where the code calls them. Offline."""
    from pymolt.scan import run_scan

    project_path = require_project_dir(project_dir, json_output=json_output or agent)
    if agent:
        project_path = project_path.resolve()
    report = run_scan(project_path)
    if agent:
        from pymolt.interfaces import views

        typer.echo(views.dumps(views.scan_view(report, project_path)))
        return
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

    if report.contact_map is not None:
        _render_contact_map(report.contact_map)
    if report.python_exposure is not None:
        _render_python_exposure(report.python_exposure, report.contact_map)

    next_step(project_path)


_CONTACT_ROWS = 15


def _render_python_exposure(exposure: dict, cmap) -> None:
    """Stdlib names the data says are gone, and how far up the code runs unchanged."""
    from pymolt.interfaces import views

    summary = views.exposure_summary(exposure)
    if summary:
        console.print(f"\n[bold]Python exposure[/bold] — {summary}")
        for row in exposure["unguarded_sites"][:_CONTACT_ROWS]:
            console.print(f"  [dim]{row}[/dim]")
        extra = len(exposure["unguarded_sites"]) - _CONTACT_ROWS
        if extra > 0:
            console.print(f"  [dim]… {extra} more (pymolt scan --json)[/dim]")
    if cmap is not None and cmap.unparsed_cython:
        console.print(
            f"[dim]{len(cmap.unparsed_cython)} .pyx/.pxd/.pxi file(s) are not parsed; names "
            "used only inside them are not counted.[/dim]"
        )


def _render_contact_map(cmap) -> None:
    """Where the code calls into each dependency: the static side of every report."""
    total = len(cmap.contacts)
    symbols = sum(len(targets) for targets in cmap.by_dep.values())
    console.print(
        f"\n[bold]Calls into dependencies[/bold] — {total} call site(s), {symbols} symbol(s), "
        f"{len(cmap.by_dep)} dependencies, {cmap.files_scanned} file(s) scanned"
    )
    for dep, targets in cmap.by_dep.items():
        rows = [c for c in cmap.contacts if c.dep == dep]
        table = Table(
            title=f"{dep} — {len(targets)} symbol(s)", title_justify="left", show_header=True,
        )
        table.add_column("Dependency symbol", style="cyan", overflow="fold")
        table.add_column("Called from (your code)", overflow="fold")
        table.add_column("File", style="dim", overflow="fold")
        for c in rows[:_CONTACT_ROWS]:
            table.add_row(c.target, c.caller, c.file or "—")
        if len(rows) > _CONTACT_ROWS:
            table.caption = f"… {len(rows) - _CONTACT_ROWS} more (pymolt scan --json)"
        console.print(table)
    for note in cmap.notes:
        console.print(f"[dim]Note: {note}[/dim]")


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


@app.command(rich_help_panel="Migration")
def setup(
    project_dir: str = typer.Argument(".", help="Project directory"),
    manifest: str | None = typer.Option(
        None, "--manifest", help="Manifest to migrate, e.g. requirements.txt"
    ),
    tool: str | None = typer.Option(
        None, "--tool", help="Resolution tool: uv | conda | poetry | container | system"
    ),
    base_python: str | None = typer.Option(
        None, "--base-python", help="The project's current Python, e.g. 3.8"
    ),
    target_python: str | None = typer.Option(
        None, "--target-python", help="The Python to migrate to, e.g. 3.12"
    ),
    container: str | None = typer.Option(
        None, "--container", help="Running container to resolve in (with --tool container)"
    ),
    target_env: str | None = typer.Option(
        None, "--target-env", help="Existing target environment, e.g. .venv-target"
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Print the options, or the saved config, as JSON (asks nothing)"
    ),
    agent: bool = typer.Option(False, "--agent", help=_AGENT_HELP),
):
    """Choose the manifest, tool and Python versions. Writes .pymolt/env_config.json.

    In a terminal it asks for what the options do not give; with --manifest,
    --tool, --base-python and --target-python it asks nothing. Without a
    terminal, missing values take the detected defaults and the output names them.
    With --json or --agent it never asks or defaults: no choice lists the valid
    ones and saves nothing, all four save, anything in between is an error.
    """
    import sys

    from pymolt.ingestion.config import ToolChoice
    from pymolt.ingestion.detect import detect_local_environments
    from pymolt.setup import (
        SetupChoices,
        SetupError,
        apply_setup,
        check_setup_values,
        gather_setup_options,
    )

    machine = json_output or agent
    project_path = require_project_dir(project_dir, json_output=machine)
    if agent:
        project_path = project_path.resolve()
    try:
        options = gather_setup_options(project_path)
    except Exception as e:
        fail(f"source detection failed: {e}", code=EXIT_ENVIRONMENT, json_output=machine)

    if machine:
        import json

        from pymolt.interfaces import views

        try:
            choices = views.setup_choices(
                options, manifest=manifest, tool=tool, base_python=base_python,
                target_python=target_python, container_id=container, target_env=target_env,
            )
        except views.AgentInputError as exc:
            fail(exc.error, hint=exc.hint, json_output=True)
        side_effects: list[str] = []
        config = (apply_setup(project_path, choices, notes=side_effects)
                  if choices is not None else None)
        if agent:  # the agent view carries the side effects as warnings
            typer.echo(views.dumps(views.setup_view(options, project_path, config=config,
                                                    side_effects=side_effects)))
        else:
            for note in side_effects:  # stderr: stdout stays pure JSON
                warn(note)
            typer.echo(json.dumps(
                views.setup_payload(options, project_path, config=config), indent=2))
        return

    if not options.manifests:
        fail(
            f"no Python dependency sources detected in {project_path}",
            hint="pymolt needs a manifest (requirements*.txt, pyproject.toml, "
                 "Pipfile, environment.yml, …) to configure a migration.",
        )
    try:
        check_setup_values(
            options, manifest=manifest, tool=tool, base_python=base_python,
            target_python=target_python, container_id=container,
        )
    except SetupError as exc:
        fail(str(exc), hint=exc.hint)

    # ── Display findings (CLI owns its rendering; the data is the shared model) ──
    console.print("[bold]pymolt setup[/bold]")
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
    else:
        console.print("[bold cyan]Running Docker Containers:[/bold cyan]")
        console.print("  None mounts this project.\n")

    tool_names = [t.name for t in options.tools]
    container_id = container
    defaulted: list[str] = []
    given_base, given_target = base_python, target_python

    # Every required value given: nothing to ask, even in a terminal.
    if sys.stdout.isatty() and not (manifest and tool and base_python and target_python):
        # 1. Manifest selection
        manifest_names = [m.name for m in options.manifests]
        default_manifest = options.default_manifest or manifest_names[0]
        selected_manifest = manifest or _ask_from_list(
            "Select manifest number or name as source of truth",
            manifest_names, str(manifest_names.index(default_manifest) + 1),
        )

        # 2. Tool selection
        if tool:
            selected_tool = tool
        else:
            console.print("\n[bold yellow]Available resolution tools/environments:[/bold yellow]")
            for i, t in enumerate(options.tools, 1):
                default_marker = " [green][default][/green]" if t.is_default else ""
                console.print(f"  {i}. {t.name} ({t.detail}){default_marker}")
            selected_tool = _ask_from_list(
                "Select resolution tool", tool_names,
                str(tool_names.index(options.default_tool) + 1),
            )

        # Container selection if "container" chosen
        if selected_tool == "container" and not container_id:
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
        base_python = base_python or _ask_python_version(
            "Confirm or enter base Python version (e.g. 3.6)",
            options.base_python_default,
        )

        # 4. Target Python version — recompute the list against the chosen base.
        target_python = target_python or _select_target_python_interactive(
            version_floor(base_python), base_python, get_eol_versions()
        )

        # 5. Target environment status
        target_env_path = target_env
        target_env_created = bool(target_env) or typer.confirm(
            "\nHave you already created the new target environment?", default=False
        )
        if target_env_created and not target_env_path:
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
        # No terminal: what was not given takes the detected default, and is named.
        def pick(given, label, default):
            if given:
                return given
            defaulted.append(label)
            return default

        selected_manifest = pick(manifest, "manifest", options.default_manifest)
        selected_tool = pick(tool, "tool", options.default_tool)
        if selected_tool == "container" and not container_id and options.containers:
            container_id = options.containers[0].id
        base_python = pick(base_python, "base Python", options.base_python_default)
        target_python = pick(
            target_python, "target Python",
            options.default_target or f"{sys.version_info.major}.{sys.version_info.minor}",
        )
        target_env_path = target_env
        target_env_created = bool(target_env)

    if selected_tool == "container" and not container_id:
        fail("a container is required with the container tool",
             hint="Start one, or pass --container.")
    offered = [v.version for v in options.target_versions]
    if given_target and offered and target_python not in offered:
        warn(f"Python {target_python} is not in the available list — "
             "assess will fail if it cannot be provisioned.")

    choices = SetupChoices(
        selected_manifest=selected_manifest,
        selected_tool=ToolChoice(selected_tool),
        base_python=base_python,
        # Only a value the user left at the detected default keeps its provenance;
        # anything they typed is a stated fact about the project.
        base_python_source=(
            options.base_python_source
            if base_python == options.base_python_default and not given_base
            else "declared"
        ),
        target_python=target_python,
        container_id=container_id,
        target_env_created=target_env_created,
        target_env_path=target_env_path,
    )
    side_effects: list[str] = []
    apply_setup(project_path, choices, notes=side_effects)

    config_file = project_path / ".pymolt" / "env_config.json"
    console.print(f"\n[bold green]✓ Configuration saved to {config_file}[/bold green]")
    for note in side_effects:
        console.print(f"[yellow]{note}[/yellow]")
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
    if defaulted:
        console.print(f"[dim]Detected defaults used for: {', '.join(defaulted)}. "
                      "Pass them as options to choose.[/dim]")
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
        return _service_resolve_chosen_source(sources, manifest_name_to_use, project_dir)
    except ValueError as e:
        fail(str(e), hint=f"(searched in '{project_dir}')", json_output=json_output)


def _resolve_target_python(
    target_python_to_use, detected_python, current_python,
    project_version_list, min_target_list, eol_map, today_str, quiet=False,
    json_output=False,
):
    """Resolve (prompting if needed), validate against downgrades, and warn on EOL."""
    if target_python_to_use is None:
        # Machine output never prompts: a prompt on stdout would corrupt the JSON.
        if json_output or not sys.stdout.isatty():
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
        "[bold cyan]Base (the project as it is today)[/bold cyan]\n"
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
    "unresolved": ("? unresolved", "yellow"),
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
    table = Table(title="Base vs target")
    table.add_column("Package", style="bold cyan", no_wrap=True)
    table.add_column("Declared", style="green")
    table.add_column("Base → Target", style="blue", no_wrap=True)
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
        elif status == "unresolved":
            move = f"{b_ver} → [yellow]unresolved[/yellow]"
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
            "[yellow]No manual zone to resolve — there is no base to compare "
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
                f"  {idx}. {pkg_name} (base: {b_ver}, target: {t_ver}){override_str}"
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


@app.command(rich_help_panel="Migration")
def assess(
    project_dir: str = typer.Argument(".", help="Project directory"),
    target_python: str | None = typer.Option(
        None, "--target-python", help="The Python to migrate to, e.g. 3.13 (default: setup's)"
    ),
    manifest: str | None = typer.Option(
        None, "--manifest", help="Manifest to resolve, e.g. requirements.txt (default: setup's)"
    ),
    source_manifest: str | None = typer.Option(None, "--source-manifest", hidden=True),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Also show transitive dependencies"
    ),
    container: str | None = typer.Option(
        None, "--container", help="Running container to resolve the base dependencies in"
    ),
    base_python: str | None = typer.Option(
        None, "--base-python", help="The project's current Python, e.g. 3.8 (default: setup's)"
    ),
    current_python_old: str | None = typer.Option(None, "--current-python", hidden=True),
    upgrade_constraint: str | None = typer.Option(
        None, "--upgrade-constraint",
        help="Constraint file with the target versions you want, e.g. pandas<2.1",
    ),
    upgrade_constraint_old: str | None = typer.Option(None, "-c", hidden=True),
    json_output: bool = typer.Option(
        False, "--json", help="Print the assessment as JSON (no tables, no prompts)"
    ),
    no_cache: bool = typer.Option(
        False, "--no-cache", help="Resolve again instead of using the cache"
    ),
    no_hashes: bool = typer.Option(
        False, "--no-hashes", help="Write the target manifest without PyPI hashes"
    ),
    no_write: bool = typer.Option(False, "--no-write", help="Do not write the target manifest"),
    risk: bool = typer.Option(
        False, "--risk", help="Also rate each package: CVEs, wheels, abandonment (network)"
    ),
    agent: bool = typer.Option(False, "--agent", help=_AGENT_HELP),
):
    """Resolve the dependencies for the target Python and write requirements-target.txt.

    Compares base and target versions per package and prints how to build the
    target environment. A resolve that succeeds does not show the code still works.
    """
    import sys

    from pymolt.ingestion.config import EnvConfig
    from pymolt.ingestion.detect import detect_sources
    from pymolt.ingestion.fallback_compiler import find_python_interpreter

    current_python = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    json_output = json_output or agent  # --agent is machine output too: no tables, no prompts
    project_path = require_project_dir(project_dir, json_output=json_output)
    if agent:
        project_path = project_path.resolve()
        project_dir = str(project_path)

    # 1. Load (or auto-detect) the environment configuration.
    config_file = project_path / ".pymolt" / "env_config.json"
    # Not gated on --json any more: diagnostics live on stderr, so an agent gets
    # pure JSON on stdout *and* keeps the signal that no config was found.
    config = _load_or_autodetect_assess_config(project_path)

    # Override config defaults with CLI overrides if provided
    # Older option names, kept hidden for one release.
    source_manifest = manifest or source_manifest
    base_python = base_python or current_python_old
    upgrade_constraint = upgrade_constraint or upgrade_constraint_old
    manifest_name_to_use = source_manifest or config.get("selected_manifest")
    detected_python = base_python or config.get("base_python")
    container_id = container or config.get("container_id")
    target_python_to_use = target_python or config.get("target_python")

    # Detect all available manifests
    try:
        sources = detect_sources(project_path, include_subroots=True)
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
            f"no Python version is declared in this project — resolving the base "
            f"dependencies against {detected_python or current_python}, the interpreter "
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
        warnings = list(report.warnings)
        if target_python_to_use and target_resolved and not no_write:
            try:
                written = _write_target_manifest(
                    project_path, target_graph, config.get("selected_tool", "uv"),
                    target_python_to_use, with_hashes=not no_hashes,
                )
                if written is not None:
                    target_manifest_path = str(written)
            except OSError as exc:
                target_manifest_path = None
                warnings.append(f"the pinned target manifest was not written: {exc}")
        risk_report = None
        if risk and baseline_graph is not None:
            from pymolt.risk.assess import assess_risk
            risk_report = assess_risk(baseline_graph, target_graph, target_python_to_use, project_path)

        from pymolt.assess.service import AssessResult
        from pymolt.interfaces import views

        # One result, two renderings: --json is the full payload, --agent the view.
        result = AssessResult(
            project_dir=str(project_dir),
            source_manifest=chosen_source.path.name,
            source_fixation=chosen_source.fixation.value,
            resolution_quality=report.resolution_quality,
            detected_python=report.detected_python,
            target_python=target_python_to_use,
            target_resolved=target_resolved,
            target_error=None if target_error is None else str(target_error),
            target_error_detail=failure_evidence(target_error)[0],
            target_culprits=failure_evidence(target_error)[1],
            baseline_unresolved=baseline_unresolved,
            baseline_tier=baseline_tier.value,
            target_manifest_path=target_manifest_path,
            warnings=warnings,
            manual_zone=report.manual_zone,
            rows=rows,
            risk=risk_report,
        )
        target_hint = _target_hint(
            project_path, target_python_to_use, target_resolved, target_manifest_path,
        )
        if agent:
            typer.echo(views.dumps(views.assess_view(
                result, project_path, risk_requested=risk,
                baseline_hint=baseline_hint, target_hint=target_hint,
            )))
            return
        typer.echo(json_lib.dumps(views.assess_payload(
            result, baseline_hint=baseline_hint, target_hint=target_hint,
        ), indent=2))
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
            culprits = failure_evidence(target_error)[1]
            if culprits:
                console.print(f"[bold]Culprit(s):[/bold] {', '.join(culprits)}")
            if target_error:
                detail = failure_evidence(target_error)[0]
                if detail:
                    console.print(Panel(escape(detail), title="Resolver output (tail)",
                                        border_style="dim"))
            console.print()

        if not verbose:
            details_msg = "Showing direct dependencies and unresolved packages. Use --verbose (-v) to view all transitive dependencies."
        else:
            details_msg = "Showing all direct and transitive dependencies."

        console.print(
            f"[bold]Base vs target dependencies (Python {target_python_to_use or ''}):[/bold]\n"
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
                        "— target-only snapshot; no base was resolved, so "
                        "this is not a comparison.[/yellow]"
                    )
                else:
                    console.print(
                        f"[bold green]✓ Target manifest written: {written.name}[/bold green]"
                    )
        except OSError as e:
            err.print(f"[bold red]Failed to write target manifest:[/bold red] {e}")

    target_manifest = next((
        name for name in ("requirements-target.txt", "environment-target.yml")
        if (project_path / name).is_file()
    ), None)
    target_hint = _target_hint(
        project_path, target_python_to_use,
        target_graph is not None and not target_error, target_manifest,
    )
    if target_hint is not None:
        _render_target_hint(target_hint)

    next_step(project_path)


def _target_hint(project_path: Path, target_python, resolved: bool, manifest):
    """How to build the target environment, once the target side resolved."""
    if not target_python or not resolved:
        return None
    from pymolt.setup.target_hint import build_target_hint

    return build_target_hint(project_path, target_python, manifest=manifest)


def _render_target_hint(hint) -> None:
    """The target environment the post-migration capture runs in. pymolt does not build it."""
    console.print(
        f"\n[bold]Target environment[/bold] (Python {hint.target_python}; "
        "pymolt does not build it)"
    )
    if hint.suggested_dockerfile:
        console.print("[bold]Dockerfile.pymolt-target:[/bold]")
        console.print(hint.suggested_dockerfile)
    for cmd in hint.suggested_uv_commands:
        console.print(f"  [white]{cmd}[/white]")
    for note in hint.notes:
        console.print(f"  [dim]note: {note}[/dim]")
    console.print(
        "  [dim]Once it exists: pymolt setup . --target-env PATH "
        "(or --container on the post-migration capture).[/dim]"
    )


def _render_baseline_hint(hint) -> None:
    """Show how to build the base environment pymolt could not resolve.

    Two audiences: a copy-paste Docker recipe for a human, and a self-contained
    brief an LLM agent can execute to stand up the interpreter and hand control back.
    """
    console.print(Panel(
        f"[yellow]{hint.reason}[/yellow]\n\n"
        "pymolt will not fabricate the legacy graph (resolving your unpinned constraints to "
        "their latest releases would be a fiction). Build the base environment, then re-run.\n\n"
        "[bold]1) Save this as Dockerfile.pymolt-legacy:[/bold]\n"
        f"[dim]{hint.suggested_dockerfile}[/dim]"
        "[bold]2) Build, run, and re-point assess:[/bold]\n"
        + "\n".join(f"  [white]{c}[/white]" for c in hint.suggested_commands),
        title=f"Base environment needed — Python {hint.base_python}",
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

def _unified_patches(previews, project_path: Path) -> list[str]:
    """Build portable patches whose paths are relative to the project root."""
    import difflib

    patches: list[str] = []
    for preview in previews:
        if preview.old_source == preview.new_source:
            continue
        preview_path = Path(preview.path)
        try:
            diff_path = preview_path.resolve().relative_to(project_path.resolve())
        except ValueError:
            diff_path = preview_path
        patches.append("".join(difflib.unified_diff(
            preview.old_source.splitlines(keepends=True),
            preview.new_source.splitlines(keepends=True),
            fromfile=f"a/{diff_path}",
            tofile=f"b/{diff_path}",
        )))
    return patches


def _render_or_write_patches(
    previews,
    project_path: Path,
    patch_file: str | None,
) -> None:
    patches = _unified_patches(previews, project_path)
    for patch in patches:
        console.print(Syntax(patch, "diff", word_wrap=False))
    if patch_file:
        output = Path(patch_file)
        if not output.is_absolute():
            output = project_path / output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("\n".join(patches), encoding="utf-8")
        err.print(f"[dim]patch written → {output}[/dim]")


@app.command(rich_help_panel="Tools")
def mcp(
    serve: bool = typer.Option(
        False, "--serve", help="Run even in a terminal (for testing)"
    ),
):
    """Run the MCP server on stdio, for AI agents.

    An agent client starts it; you register it once:

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


@app.command(rich_help_panel="Experimental")
def succession(
    project_dir: str = typer.Argument(".", help="Project directory"),
    endpoint: str | None = typer.Option(
        None, "--endpoint",
        help="Ask a remote Axiom at this URL (default: none, pymolt's curated knowledge)",
    ),
    save_plan: bool = typer.Option(
        False, "--plan", help="Save the in-place codemods as the plan for `pymolt apply`"
    ),
    write: bool = typer.Option(False, "--write", hidden=True),
    json_output: bool = typer.Option(False, "--json", help="Print the result as JSON"),
):
    """Find where a deprecated framework's code should go (e.g. TF1/Keras).

    Detects the frameworks and looks up where they went in pymolt's curated
    knowledge (or a remote Axiom given with --endpoint). In-place paths (e.g.
    keras → tensorflow.keras) are shown as file changes; --plan saves them as the
    plan for `pymolt apply`. Transplant paths (e.g. keras → torch) are shown as
    steps to follow, never applied. Writes no source file.
    """
    from pymolt.config import load_endpoint
    from pymolt.strategic.succession.client import SuccessionError
    from pymolt.strategic.succession.detect import (
        detect_frameworks,
        knowledge_coverage,
        plausible_frameworks,
    )
    from pymolt.strategic.succession.service import run_succession

    require_project_dir(project_dir, json_output=json_output)
    if write:
        fail("`pymolt succession --write` was removed",
             hint="Use --plan, then `pymolt apply`.", json_output=json_output)
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
    declared = frameworks
    frameworks = plausible_frameworks(declared)
    covered = knowledge_coverage()
    if not json_output:
        if frameworks:
            console.print(f"[bold]Frameworks detected:[/bold] {', '.join(frameworks)}")
        else:
            console.print(f"[bold]No framework among the {len(declared)} declared "
                          "dependencies[/bold] (all were checked).")

    try:
        with err.status("Looking up framework succession…", spinner="dots") as status:
            edges, result = run_succession(
                project_dir, base_url=load_endpoint(endpoint),
                progress=lambda m: status.update(f"[cyan]{m}[/cyan]"),
            )
    except SuccessionError as exc:
        fail(
            f"Axiom Graph unavailable: {exc}",
            hint="Check the --endpoint URL (or PYMOLT_AXIOM_ENDPOINT); without one, pymolt "
                 "uses its curated knowledge.",
            code=EXIT_ENVIRONMENT,
            json_output=json_output,
        )

    from pymolt.strategic.succession.transplant import build_transplant_plan

    saved_plan = None
    if save_plan:
        saved_plan = _save_succession_plan(
            project_dir, edges, result.files_scanned if result else 0, json_output,
        )

    if json_output:
        import json as _json
        payload = {
            "frameworks": frameworks,
            "checked_dependencies": len(declared),
            "knowledge_covers": covered,
            "edges": [e.model_dump(mode="json") for e in edges],
            "transplant_plans": [
                build_transplant_plan(e, project_dir).model_dump(mode="json")
                for e in edges if e.is_transplant
            ],
            "in_place_files_changed": result.files_changed if result else 0,
            "dry_run": True,
        }
        if save_plan:
            payload["plan_path"] = str(saved_plan) if saved_plan else None
        typer.echo(_json.dumps(payload, indent=2))
        return

    if not edges:
        named = ", ".join(frameworks) if frameworks else None
        source = ("Axiom Graph" if load_endpoint(endpoint) else "pymolt's succession knowledge")
        if named:
            console.print(
                f"[yellow]{named}: not in {source}, which covers {', '.join(covered)}.[/yellow]\n"
                "[dim]That is a gap in what pymolt knows, not a finding that no successor "
                "exists.[/dim]"
            )
        else:
            console.print(
                f"[yellow]Checked all {len(declared)} declared dependencies: none is a "
                f"framework {source} has a succession path for (it covers "
                f"{', '.join(covered)}).[/yellow]"
            )
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
        console.print(
            f"\n[bold]In-place shims — would change {result.files_changed} file(s):[/bold]"
        )
        for change in result.changes:
            console.print(f"  [green]{change.path}[/green]  ({change.sites} site(s))")
        if saved_plan:
            console.print(f"\nPlan saved to [green]{saved_plan}[/green].")
            console.print(f"[bold]Next:[/bold] pymolt apply {project_dir}")
        elif save_plan:
            console.print("\n[yellow]No in-place shim changes a file: nothing to plan.[/yellow]")
        else:
            console.print("\n[dim]Nothing was written. Save these as a plan with "
                          "[bold]--plan[/bold], then run `pymolt apply`.[/dim]")


def _save_succession_plan(project_dir: str, edges, files_scanned: int, json_output: bool):
    """Freeze the in-place shims as the migration plan. None when no shim changes a file."""
    from pymolt.codemods.service import PlanInputError, plan_inputs
    from pymolt.migration_plan import MigrationPlanError, create_migration_plan
    from pymolt.strategic.succession.service import in_place_previews

    try:
        config, target_path = plan_inputs(project_dir)
    except PlanInputError as exc:
        fail(str(exc), hint=exc.hint or None, json_output=json_output)
    previews = in_place_previews(project_dir, edges)
    if not any(p.old_source != p.new_source for p in previews):
        return None
    try:
        migration_plan = create_migration_plan(
            project_dir, previews, [],
            target_python=config.target_python or "unknown",
            source_manifest=config.selected_manifest or "unknown",
            target_manifest_path=target_path,
            files_scanned=files_scanned,
        )
        return migration_plan.save(project_dir)
    except (MigrationPlanError, OSError) as exc:
        fail(f"could not save the plan: {exc}", hint="No source files were changed.",
             code=EXIT_ENVIRONMENT, json_output=json_output)


def _render_transplant_plan(plan) -> None:
    """Render an organ-transplant plan: scaffold → map → port weights → contract-verify.

    A transplant is guidance, never auto-applied — the last step is the honest gate
    (`pymolt capture` + `pymolt verify` proving the ported model matches the original).
    Edge-supplied text (scaffold code, mappings, notes) is markup-escaped: it may contain
    brackets (e.g. `x[0]`) that would otherwise be parsed as Rich markup."""
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


_PORTED_STYLE = {"confirmed": "green", "likely": "yellow", "unknown": "dim"}


@app.command(rich_help_panel="Experimental")
def forks(
    repo: str = typer.Argument(..., help="Repository, as owner/name"),
    project_dir: str = typer.Option(
        ".", "--project-dir",
        help="Where to keep the fork cache (default: the current directory)"),
    online: bool = typer.Option(
        False, "--online", help="Query the GitHub API (cached); default: cache only"
    ),
    cutoff_months: int = typer.Option(
        18, "--cutoff-months", help="Ignore forks not pushed to within this many months"
    ),
    top: int = typer.Option(
        25, "--top", help="How many top forks get the slower compare and manifest checks"
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Print the report as JSON"
    ),
):
    """Rank the active forks of an abandoned repository.

    Reads a cached --online run by default. Ranks by recent pushes, stars,
    divergence from the original, and whether the fork's declared dependencies
    look ported. A high rank does not mean the fork works.
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



# ── removed commands ──────────────────────────────────────────────────────────
# Kept for one release so an old script or habit gets the replacement instead
# of "No such command". Hidden from --help; any arguments are accepted.


def _removed(name: str, replacement: str | dict[str, str]) -> None:
    """Register ``pymolt NAME`` as removed: print what replaces it and exit 2.

    ``replacement`` is the new command, or, for a removed group, a map from each
    old subcommand to its replacement.
    """

    def stub(ctx: typer.Context) -> None:
        sub = ctx.args[0] if ctx.args and isinstance(replacement, dict) else ""
        if isinstance(replacement, dict):
            use = replacement.get(sub) or "; ".join(
                f"{old} → {new}" for old, new in replacement.items()
            )
            if sub not in replacement:
                sub = ""
        else:
            use = replacement
        old = f"pymolt {name}{' ' + sub if sub else ''}"
        fail(f"`{old}` was removed", hint=f"Use {use}.")

    app.command(
        name,
        hidden=True,
        add_help_option=False,
        context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
    )(stub)


_removed("doctor", "`pymolt status`")
_removed("contract", {
    "capture": "`pymolt capture baseline|post-migration`",
    "trace": "`pymolt capture baseline|post-migration`",
    "export-watcher": "`pymolt capture … --mode attach` (it writes the watcher bundle)",
    "boundary": "`pymolt capture baseline`, `pymolt capture post-migration`, then "
                "`pymolt verify`",
    "map": "`pymolt scan`",
    "report": "`pymolt verify`",
    "diff": "`pymolt verify --baseline OLD --post-migration NEW`",
})
_removed("env", {"hint": "`pymolt assess` (it prints how to build the target environment)"})
# Axiom is no longer a service pymolt needs: `pymolt plan` computes API changes locally, and a
# remote endpoint is used only when given explicitly (--endpoint / PYMOLT_AXIOM_ENDPOINT).
_AXIOM_STATUS = ("`pymolt plan` (it computes API changes locally; a remote Axiom is used only "
                 "with `--endpoint URL` or `PYMOLT_AXIOM_ENDPOINT`)")
_AXIOM_ENDPOINT = "`pymolt plan --endpoint URL` or `PYMOLT_AXIOM_ENDPOINT` (none is the default)"
_AXIOM_LOGIN = ("`PYMOLT_API_TOKEN` (sent only to an endpoint given with `--endpoint` or "
                "`PYMOLT_AXIOM_ENDPOINT`)")
_AXIOM_LOGOUT = "`unset PYMOLT_API_TOKEN`"
_AXIOM_CACHE = "`rm -r .pymolt/cache/delta` in the project (the local API-change cache)"
_removed("axiom", {
    "status": _AXIOM_STATUS,
    "endpoint": _AXIOM_ENDPOINT,
    "login": _AXIOM_LOGIN,
    "logout": _AXIOM_LOGOUT,
    "clear-cache": _AXIOM_CACHE,
})
_removed("auth", {"login": _AXIOM_LOGIN, "status": _AXIOM_STATUS, "logout": _AXIOM_LOGOUT})
_removed("config", {
    "show": _AXIOM_STATUS,
    "set-endpoint": _AXIOM_ENDPOINT,
    "reset-endpoint": "`unset PYMOLT_AXIOM_ENDPOINT` (no endpoint is the default)",
    "clear-cache": _AXIOM_CACHE,
})
_removed("login", _AXIOM_LOGIN)
_removed("logout", _AXIOM_LOGOUT)
_removed("migrate", "`pymolt plan`, then `pymolt apply` (status shows the steps before them)")
_removed("codemods", "`pymolt plan` to see the changes, then `pymolt apply`")
