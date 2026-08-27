"""
axiom_graph/cli.py

Command-line interface for Axiom Graph.
Uses `rich` for live progress display: overall step progress bar +
per-step phase indicator with elapsed-time counter.

Usage:
    python -m axiom_graph pandas 1.3.5 2.0.0
    python -m axiom_graph pandas 1.3.5 2.0.0 --json /tmp/out.json
    python -m axiom_graph pandas 1.3.5 2.0.0 --use-git
    python -m axiom_graph pandas 1.3.5 2.0.0 --include-prereleases
    python -m axiom_graph pandas 1.3.5 2.0.0 --resume          (default)
    python -m axiom_graph pandas 1.3.5 2.0.0 --no-resume       (force fresh run)
    python -m axiom_graph pandas 1.3.5 2.0.0 --verbose
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from pathlib import Path

try:
    from rich.console import Console
    from rich.live import Live
    from rich.panel import Panel
    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TaskID,
        TextColumn,
        TimeElapsedColumn,
        TimeRemainingColumn,
    )
    from rich.table import Table
    from rich.text import Text
    _RICH_AVAILABLE = True
except ImportError:
    _RICH_AVAILABLE = False

from axiom_graph.core.pipeline import StepProgress

console = Console(stderr=True) if _RICH_AVAILABLE else None


# ---------------------------------------------------------------------------
# Rich progress implementation of StepProgress
# ---------------------------------------------------------------------------

class RichProgress(StepProgress):
    """
    Live rich UI: two progress bars + a status line.

    Layout:
    ┌─────────────────────────────────────────────────┐
    │  Step  [████████░░░░░░░░░] 3/8  1m 20s          │
    │  Phase: 🔍 griffe structural diff                │
    └─────────────────────────────────────────────────┘
    """

    def __init__(self, package: str, from_v: str, to_v: str) -> None:
        self.package = package
        self.from_v = from_v
        self.to_v = to_v
        self._lock = threading.Lock()

        # State
        self._total_steps = 0
        self._current_step = 0
        self._current_phase = ""
        self._step_start_time: float | None = None
        self._completed: list[tuple[str, str, int, float, bool]] = []
        # (from_v, to_v, n_changes, elapsed, from_checkpoint)

        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(bar_width=30),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=Console(stderr=True),
        )
        self._step_task: TaskID | None = None

        self._live = Live(
            self._render(),
            console=Console(stderr=True),
            refresh_per_second=4,
            transient=False,
        )
        self._live.start()

    def _render(self):
        """Build the rich renderable for the current state."""
        table = Table.grid(padding=(0, 1))
        table.add_column()

        # Progress bars
        if self._step_task is not None:
            table.add_row(self._progress)

        # Phase line
        if self._current_phase:
            table.add_row(Text(f"  {self._current_phase}", style="dim"))

        # Completed steps (last 3)
        for from_v, to_v, n, elapsed, from_ckpt in self._completed[-3:]:
            ckpt_badge = " [dim](checkpoint)[/dim]" if from_ckpt else ""
            line = (
                f"  ✓ [green]{from_v} → {to_v}[/green]  "
                f"[bold]{n}[/bold] changes  "
                f"[dim]{elapsed:.0f}s[/dim]{ckpt_badge}"
            )
            table.add_row(Text.from_markup(line))

        return Panel(
            table,
            title=f"[bold]Axiom Graph[/bold]  {self.package}: "
                  f"[cyan]{self.from_v}[/cyan] → [cyan]{self.to_v}[/cyan]",
            border_style="blue",
        )

    def _refresh(self) -> None:
        with self._lock:
            if self._live:
                self._live.update(self._render())

    def on_chain_built(self, chain: list[str], skipped_count: int) -> None:
        with self._lock:
            self._total_steps = len(chain)
            # Steps task
            self._step_task = self._progress.add_task(
                "[bold]Steps[/bold]",
                total=self._total_steps,
                completed=skipped_count,
            )
            if skipped_count:
                self._current_phase = (
                    f"✓ Resuming — {skipped_count}/{self._total_steps} "
                    f"steps already in checkpoint"
                )
        self._refresh()

    def on_step_start(
        self,
        step_index: int,
        total_steps: int,
        from_v: str,
        to_v: str,
        resumed: bool,
    ) -> None:
        with self._lock:
            self._current_step = step_index
            self._step_start_time = time.monotonic()
            if not resumed:
                self._current_phase = f"Step {step_index + 1}/{total_steps}: {from_v} → {to_v}"
        self._refresh()

    def on_step_phase(self, phase: str) -> None:
        with self._lock:
            self._current_phase = phase
        self._refresh()

    def on_step_done(
        self, delta, elapsed: float, from_checkpoint: bool
    ) -> None:
        with self._lock:
            step_elapsed = (
                time.monotonic() - self._step_start_time
                if self._step_start_time and not from_checkpoint
                else 0.0
            )
            self._completed.append((
                delta.from_version,
                delta.to_version,
                len(delta.changes),
                step_elapsed,
                from_checkpoint,
            ))
            self._current_phase = ""
            if self._step_task is not None:
                self._progress.advance(self._step_task, 1)
        self._refresh()

    def on_done(self, full_delta, total_elapsed: float) -> None:
        with self._lock:
            self._current_phase = (
                f"✓ Done in {total_elapsed:.1f}s — "
                f"{full_delta.total_breaking} breaking changes"
            )
        self._refresh()
        if self._live:
            self._live.stop()

    def stop(self) -> None:
        """Force stop the live display (on error/interrupt)."""
        try:
            if self._live:
                self._live.stop()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Fallback plain-text progress
# ---------------------------------------------------------------------------

class PlainProgress(StepProgress):
    """Simple stderr text output when rich is not available."""

    def __init__(self, package: str, from_v: str, to_v: str) -> None:
        self.package = package
        self._step_start: float = 0.0

    def on_chain_built(self, chain: list[str], skipped_count: int) -> None:
        msg = f"  Chain: {len(chain)} step(s)"
        if skipped_count:
            msg += f"  ({skipped_count} already checkpointed)"
        print(msg, file=sys.stderr)

    def on_step_start(self, step_index, total_steps, from_v, to_v, resumed) -> None:
        self._step_start = time.monotonic()
        status = "  [checkpoint]" if resumed else ""
        print(
            f"  [{step_index+1}/{total_steps}] {from_v} → {to_v}{status}",
            file=sys.stderr,
            flush=True,
        )

    def on_step_phase(self, phase: str) -> None:
        print(f"    {phase}", file=sys.stderr, flush=True)

    def on_step_done(self, delta, elapsed, from_checkpoint) -> None:
        ckpt = " (checkpoint)" if from_checkpoint else ""
        t = f"{elapsed:.0f}s" if not from_checkpoint else "-"
        print(f"    ✓ {len(delta.changes)} changes  {t}{ckpt}", file=sys.stderr)

    def on_done(self, full_delta, total_elapsed) -> None:
        print(
            f"  Done in {total_elapsed:.1f}s: "
            f"{full_delta.total_breaking} breaking changes",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Result output formatting
# ---------------------------------------------------------------------------

_RISK_COLORS = {
    "structural": "\033[91m",
    "behavioral": "\033[93m",
    "mechanical": "\033[92m",
}
_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_STATE_ICONS = {
    "removed":    "✗",
    "deprecated": "⚠",
    "moved":      "→",
    "active":     "✓",
}


def _supports_color() -> bool:
    return sys.stdout.isatty()


def _color(text: str, code: str) -> str:
    if _supports_color():
        return f"{code}{text}{_RESET}"
    return text


def print_report(delta, *, verbose: bool = False) -> None:
    use_color = _supports_color()

    def bold(t: str) -> str:
        return f"{_BOLD}{t}{_RESET}" if use_color else t

    def dim(t: str) -> str:
        return f"{_DIM}{t}{_RESET}" if use_color else t

    print()
    print(bold(
        f"  Axiom Graph — {delta.package}: {delta.from_version}  →  {delta.to_version}"
    ))

    if delta.skipped:
        print(f"  ⚠ Skipped: {delta.skip_reason}")
        return

    chain_str = " → ".join(delta.release_chain) if delta.release_chain else "(direct)"
    print(dim(f"  Chain: {chain_str}"))
    print(f"  {delta.total_breaking} breaking change(s)\n")

    s = delta.summary_by_risk
    for risk in ("structural", "behavioral", "mechanical"):
        count = s.get(risk, 0)
        label = f"    {risk.upper():<12}: {count:>4}"
        if use_color and count > 0:
            print(_color(label, _RISK_COLORS[risk]))
        else:
            print(label)
    print()

    current_risk = None
    for c in delta.changes:
        if c.risk.value != current_risk:
            current_risk = c.risk.value
            header = f"  [{current_risk.upper()}]"
            print(bold(_color(header, _RISK_COLORS.get(current_risk, ""))))

        icon = _STATE_ICONS.get(c.state.value, "?")
        state_str = f"[{c.state.value}]" if c.state.value != "removed" else ""

        temporal = ""
        if c.deprecated_since and c.removed_in:
            temporal = dim(
                f"  deprecated since {c.deprecated_since}, removed in {c.removed_in}"
            )
        elif c.deprecated_since:
            temporal = dim(f"  deprecated since {c.deprecated_since}")
        elif c.removed_in:
            temporal = dim(f"  removed in {c.removed_in}")

        print(f"    {icon} {c.path} {state_str}")
        print(f"        {c.explanation}")
        if temporal:
            print(f"        {temporal}")
        if c.deprecation_hint:
            hint_preview = c.deprecation_hint[:120].replace("\n", " ")
            print(dim(f"        💡 {hint_preview}"))
        if c.migration_patterns:
            print(dim("        🔄 Generalized migration patterns:"))
            for pat in c.migration_patterns[:3]:  # print up to 3 patterns
                print(bold(f"            {pat['before']}  ➔  {pat['after']}"))
        if verbose and c.test_examples:
            print(dim(f"        📋 {len(c.test_examples)} test migration example(s):"))
            for ex in c.test_examples[:1]:
                for line in ex.splitlines()[:8]:
                    print(dim(f"            {line}"))

    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="axiom_graph",
        description="Axiom Graph — chronological API delta engine for Python packages.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python -m axiom_graph pandas 1.3.5 2.0.0
  python -m axiom_graph pandas 1.3.5 2.0.0 --json out.json
  python -m axiom_graph requests 2.25.0 2.32.0 --use-git
  python -m axiom_graph flask 1.0.0 3.0.0 --no-resume     (fresh run)
  python -m axiom_graph flask 1.0.0 3.0.0 --no-resume --verbose
        """,
    )
    parser.add_argument("package", help="PyPI package name (e.g. pandas)")
    parser.add_argument("from_version", help="Source version (e.g. 1.3.5)")
    parser.add_argument("to_version", help="Target version (e.g. 2.0.0)")
    parser.add_argument(
        "--json", metavar="FILE",
        help="Write full delta as JSON to FILE"
    )
    parser.add_argument(
        "--use-git", action="store_true", default=False,
        help="Also fetch release versions from git tags (slower, more complete)"
    )
    parser.add_argument(
        "--include-prereleases", action="store_true", default=False,
        help="Include alpha/beta/rc versions in the release chain"
    )
    parser.add_argument(
        "--resume", action="store_true", default=True,
        help="Resume from checkpoint if available (default: on)"
    )
    parser.add_argument(
        "--no-resume", dest="resume", action="store_false",
        help="Ignore existing checkpoint and re-run from scratch"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", default=False,
        help="Show test migration examples in output"
    )
    parser.add_argument(
        "--log-level", default="WARNING",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Set logging verbosity (default: WARNING)"
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(levelname)s %(name)s: %(message)s",
    )

    from axiom_graph.acquisition.checkpoint import checkpoint_path
    from axiom_graph.core.pipeline import compute_full_delta

    # Show checkpoint path upfront so user knows where to look
    ckpt = checkpoint_path(args.package, args.from_version, args.to_version)
    if ckpt.exists() and args.resume:
        lines = sum(1 for _ in ckpt.read_text().splitlines() if _.strip())
        if _RICH_AVAILABLE:
            console.print(
                f"[dim]  Checkpoint found:[/dim] [cyan]{ckpt}[/cyan] "
                f"[dim]({lines} step(s) cached)[/dim]"
            )
        else:
            print(f"  Checkpoint: {ckpt} ({lines} step(s) cached)", file=sys.stderr)

    # Build progress handler
    prog: StepProgress
    if _RICH_AVAILABLE:
        prog = RichProgress(args.package, args.from_version, args.to_version)
    else:
        prog = PlainProgress(args.package, args.from_version, args.to_version)

    try:
        delta = compute_full_delta(
            args.package,
            args.from_version,
            args.to_version,
            use_git=args.use_git,
            include_prereleases=args.include_prereleases,
            progress=prog,
            resume=args.resume,
            force_restart=not args.resume,
        )
    except KeyboardInterrupt:
        if isinstance(prog, RichProgress):
            prog.stop()
        ckpt = checkpoint_path(args.package, args.from_version, args.to_version)
        print(
            f"\n  Interrupted. Progress saved to checkpoint:\n  {ckpt}",
            file=sys.stderr,
        )
        print(
            f"\n  Resume with:\n"
            f"  python -m axiom_graph {args.package} {args.from_version} {args.to_version}",
            file=sys.stderr,
        )
        sys.exit(1)
    except Exception as exc:
        if isinstance(prog, RichProgress):
            prog.stop()
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    print_report(delta, verbose=args.verbose)

    if args.json:
        out_path = Path(args.json)
        out_path.write_text(
            json.dumps(delta.model_dump(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"  Full delta written to {out_path}\n")

    # Print checkpoint location for reference
    ckpt = checkpoint_path(args.package, args.from_version, args.to_version)
    print(f"  Checkpoint: {ckpt}\n")
