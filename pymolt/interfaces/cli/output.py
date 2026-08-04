"""The CLI's output contract: which stream, which exit code, which shape.

Three rules every command here follows, so that a human, a shell pipeline and an
agent all get something they can trust:

* **stdout carries exactly one thing — the answer.** A rendered report, or the
  ``--json`` payload. Diagnostics, warnings, progress and errors go to stderr,
  so ``pymolt assess --json | jq`` never chokes on a warning and a human
  redirecting stdout still sees what went wrong.
* **a failure under ``--json`` is still JSON.** ``{"ok": false, "error", "hint"}``
  on stdout, mirroring the MCP server's envelope, so an agent parses one shape
  whatever happened rather than sniffing for a traceback.
* **exit codes mean something.** See the constants below; they are part of the
  documented contract and are what CI branches on.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import NoReturn

import typer
from rich.console import Console

#: The answer channel. Only a report or a JSON payload is ever written here.
console = Console()
#: The diagnostics channel: warnings, errors, progress, next-step hints.
err = Console(stderr=True)

EXIT_OK = 0
"""Ran, and the answer is positive (or purely informational)."""

EXIT_FINDING = 1
"""Ran fine, but the answer is negative — behavior changed, the user declined a
prompt. Not a malfunction: CI branches on this to fail a build deliberately."""

EXIT_USAGE = 2
"""The invocation or its inputs are wrong: no such directory, missing required
option, a manifest that isn't there. Nothing was attempted."""

EXIT_ENVIRONMENT = 3
"""The world outside pymolt failed: a service is down, docker is missing, an
optional extra isn't installed. Retrying after fixing the environment may work."""


def warn(message: str, *, hint: str | None = None) -> None:
    """A diagnostic the user should see but which is not the answer."""
    err.print(f"[bold yellow]⚠️ Warning:[/bold yellow] {message}")
    if hint:
        err.print(f"[dim]{hint}[/dim]")


def fail(
    message: str,
    *,
    hint: str | None = None,
    code: int = EXIT_USAGE,
    json_output: bool = False,
) -> NoReturn:
    """Report a failure on the right stream, in the right shape, and exit.

    Under ``--json`` the failure envelope goes to *stdout* — it is the
    machine-readable answer, and keeping stdout parseable in both directions is
    the whole point. In human mode it goes to stderr.
    """
    if json_output:
        payload: dict[str, object] = {"ok": False, "error": message}
        if hint:
            payload["hint"] = hint
        typer.echo(json.dumps(payload, indent=2))
    else:
        err.print(f"[bold red]Error:[/bold red] {message}")
        if hint:
            err.print(f"[dim]{hint}[/dim]")
    raise typer.Exit(code=code)


def require_project_dir(project_dir: str | Path, *, json_output: bool = False) -> Path:
    """Resolve a project directory argument, or fail with EXIT_USAGE.

    A mistyped path used to look exactly like a clean project — "no roots
    found", exit 0 — which is the one answer a migration tool must never give by
    accident. (The MCP server has always rejected this; the CLI now matches.)
    """
    path = Path(project_dir)
    if not path.exists():
        fail(
            f"no such directory: {path}",
            hint="Check the path — pymolt takes the project root as its argument.",
            json_output=json_output,
        )
    if not path.is_dir():
        fail(
            f"not a directory: {path}",
            hint="pymolt operates on a project root, not a single file.",
            json_output=json_output,
        )
    return path


def next_step(project_dir: str | Path, *, json_output: bool = False) -> None:
    """Print what to do after the command that just finished.

    Every phase used to end on its own output and stop, leaving "so what now?"
    to the reader. The answer is computed in one place (``pymolt.status``) rather
    than hardcoded per command, so the CLI cannot drift from what ``pymolt
    status`` says. Goes to stderr: it is guidance, not part of the answer, and
    must never land in a piped report.
    """
    if json_output:
        return
    try:
        from pymolt.status import build_status

        status = build_status(project_dir)
    except Exception:  # guidance must never break the command that produced a result
        return
    if status.next_command:
        err.print(f"\n[dim]next →[/dim] [white]{status.next_command}[/white]")


def require_file(path: str | Path, *, what: str = "file", json_output: bool = False) -> Path:
    """Resolve an input file argument, or fail with EXIT_USAGE.

    Used for recordings fed to ``contract diff``: a missing artifact used to fold
    into an empty diff and report "no behavior change" — a false all-clear.
    """
    resolved = Path(path)
    if not resolved.is_file():
        fail(f"no such {what}: {resolved}", json_output=json_output)
    return resolved
