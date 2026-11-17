"""UI-agnostic orchestration for the Contract (verify) phase.

Everything that touches subprocesses/PYTHONPATH/env-var injection to capture a
dynamic boundary trace lives here — not in ``interfaces/cli/verify_cmd.py`` —
so callers can drive the exact same capture the CLI does (the "interfaces are
thin, each phase is a service module" rule this repo follows everywhere else).

Two concerns:

* **Capture** — ``capture_trace`` runs an arbitrary command with the watcher
  injected (locally or in an already-running container) and merges the
  per-process JSONL it produces into one artifact. This already covers "run
  alongside your test suite" (the command is just ``pytest ...``) and "run my
  app for a while" (the command is ``python app.py``, optionally cancelled
  early via ``cancel_event``). For a
  process the ENGINEER runs themselves (can't/won't hand control to pymolt —
  a server they want to click around in a browser), ``start_attached_capture``/
  ``poll_attached_capture``/``finalize_attached_capture`` hand back a ready
  command to copy-paste and just watch the output file grow.

* **Named state** — ``capture_named_trace``/``finalize_attached_capture``
  persist what was captured as a :class:`~pymolt.verify.models.ContractSlot`
  under ``baseline`` or ``post_migration`` in ``.pymolt/contract_state.json``
  (mirrors ``.pymolt/env_config.json``), so ``build_contract_report_from_state``
  never makes the engineer retype raw file paths, and a report can name what
  fed it.

Nothing here prompts or prints — that's the callers' job. Bad input raises
``ValueError`` with a message worth surfacing verbatim.
"""

from __future__ import annotations

import glob
import gzip
import hashlib
import importlib.util
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from pymolt.adapters.python_runner import to_host
from pymolt.adapters.subprocess_runner import run_command
from pymolt.ingestion.config import EnvConfig
from pymolt.verify.coverage import run_coverage_json
from pymolt.verify.export_watcher import export_watcher
from pymolt.verify.facts import (
    find_facts_snapshot,
    iter_snapshot,
    iter_trace_records,
    load_site_text,
    merge_fact_snapshots,
    project_snapshot,
    read_snapshot_header,
    record_site_text,
    snapshot_path,
)
from pymolt.verify.gaps import classify_gaps, scan_usage_sites
from pymolt.verify.models import (
    CaptureMode,
    CaptureValidity,
    ContractSlot,
    ContractState,
    TraceArtifactQuality,
    VerificationVerdict,
)
from pymolt.verify.report import ContractReport, build_contract_report

logger = logging.getLogger(__name__)

CONTRACT_STATE_PATH = Path(".pymolt") / "contract_state.json"
ENV_CONFIG_PATH = Path(".pymolt") / "env_config.json"

# Hard ceiling on an un-cancellable local capture (e.g. the MCP server's
# capture tool, which never passes cancel_event) so a command that never
# exits (a dev server, a hung test) can't hang the caller forever.
_CAPTURE_TIMEOUT = int(os.environ.get("PYMOLT_CAPTURE_TIMEOUT", "1800"))


class TraceCaptureResult(BaseModel):
    """What one `capture_trace` run produced — the data a caller renders/stores."""

    out_path: str
    events: int
    processes: int
    where: str  # "local" | "container:<name>"
    # Merged stdout+stderr of the traced command, captured to a file next to the
    # trace artifacts (never inherited onto the parent tty).
    command_log: str | None = None
    # Exit code of the traced command (None for the container path / attach).
    returncode: int | None = None
    # A user-requested Stop & Collect normally terminates a live command with a
    # non-zero signal code; distinguish that expected stop from command failure.
    cancelled: bool = False
    # Optional/defaulted for compatibility with older callers and test doubles.
    duration_seconds: float | None = None
    deployment_id: str | None = None
    request_id: str | None = None
    correlation_id: str | None = None
    sample_rate: float = 1.0
    backend: str | None = None
    impact_targets: list[str] = Field(default_factory=list)
    metadata_path: str | None = None
    events_seen: int | None = None
    dropped_events: int | None = None
    sampling_dropped: int | None = None
    backpressure_dropped: int | None = None
    write_failures: int | None = None
    sink_failures: int | None = None
    instrumentation_skipped: list[dict[str, str]] = Field(default_factory=list)
    # Interpreters where tracing was requested but the watcher could not load (each wrote
    # ``<trace>.activation-error.json``): those processes ran UNTRACED.
    activation_failures: list[dict] = Field(default_factory=list)
    # Facts sink (phase 2): the merged per-process snapshots, ``<out_path>.facts.json``.
    facts_path: str | None = None
    # Stdlib top-level modules traced as dependencies (PYMOLT_TRACE_STDLIB); empty = none.
    trace_stdlib: list[str] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Capture — inject the watcher, run a command, merge the JSONL it writes.
# ─────────────────────────────────────────────────────────────────────────────


STDLIB_ENV = "PYMOLT_TRACE_STDLIB"
_STDLIB_OFF = {"none", "off", "0"}


def trace_env(
    target: str,
    backend: str,
    exclude: str,
    source: str,
    include_internal: bool,
    privacy: str = "values",
    sample_rate: float = 1.0,
    impact_targets: list[str] | tuple[str, ...] | None = None,
    deployment_id: str | None = None,
    request_id: str | None = None,
    correlation_id: str | None = None,
    sink: str | None = None,
    trace_mode: str | None = None,
    sample_first: int | None = None,
    widen_targets: bool = False,
    trace_stdlib: list[str] | tuple[str, ...] | None = None,
) -> dict:
    """The PYMOLT_TRACE_* variables that drive the watcher (shared by local + container).

    Below a sample rate of 1 the novelty-first count ``PYMOLT_TRACE_SAMPLE_FIRST`` is set too
    (``sample_first``, default ``LONG_RUN_SAMPLE_FIRST``; the caller's environment wins).

    ``sink`` / ``trace_mode`` are this capture's defaults for ``PYMOLT_TRACE_SINK`` /
    ``PYMOLT_TRACE_MODE`` (``TEST_SINK``/``TEST_TRACE_MODE`` for a test suite,
    ``LONG_RUN_SINK``/``LONG_RUN_TRACE_MODE`` for command/attach); a value set in the
    caller's own environment wins. With impact targets the mode is left to the caller's
    environment (unset = the watcher's targeted capture), unless ``widen_targets``: then the
    default mode stays and the targets are armed on top of it (what a post-migration capture
    needs to diff against a full baseline). The facts sink's tunables are forwarded when
    set, so a container capture honours them too.

    ``trace_stdlib`` are stdlib top-level modules to treat as dependencies for this capture
    (``PYMOLT_TRACE_STDLIB``, comma-separated); the caller's own ``PYMOLT_TRACE_STDLIB`` wins
    and is forwarded (so a container capture honours it); ``none`` there disables it."""
    if privacy not in {"values", "shape"}:
        raise ValueError("privacy must be 'values' or 'shape'")
    if not 0 < sample_rate <= 1:
        raise ValueError("sample_rate must be greater than 0 and at most 1")
    env = {
        "PYMOLT_TRACE_TARGET": target,
        "PYMOLT_TRACE_BACKEND": backend,
        "PYMOLT_TRACE_PRIVACY": privacy,
        "PYMOLT_TRACE_SAMPLE_RATE": str(sample_rate),
    }
    normalized_targets = _normalize_impact_targets(impact_targets)
    if normalized_targets:
        env["PYMOLT_TRACE_IMPACT_TARGETS"] = json.dumps(
            normalized_targets, separators=(",", ":"),
        )
    identities = {
        "PYMOLT_TRACE_DEPLOYMENT_ID": deployment_id,
        "PYMOLT_TRACE_REQUEST_ID": request_id,
        "PYMOLT_TRACE_CORRELATION_ID": correlation_id,
    }
    for key, supplied in identities.items():
        value = supplied if supplied is not None else os.environ.get(key)
        if value:
            env[key] = value
    if exclude:
        env["PYMOLT_TRACE_EXCLUDE"] = exclude
    if source:
        env["PYMOLT_TRACE_SOURCE"] = source
    if include_internal:
        env["PYMOLT_TRACE_INTERNAL"] = "1"
    if normalized_targets and not widen_targets:
        # Impact targets with no explicit mode keep meaning a targeted capture from the start
        # (the watcher's own rule for an unset PYMOLT_TRACE_MODE): no default mode here.
        trace_mode = None
    for key, default in (("PYMOLT_TRACE_SINK", sink), ("PYMOLT_TRACE_MODE", trace_mode)):
        value = os.environ.get(key) or default
        if value:
            env[key] = value
    if sample_rate < 1:
        env["PYMOLT_TRACE_SAMPLE_FIRST"] = os.environ.get("PYMOLT_TRACE_SAMPLE_FIRST") or str(
            sample_first if sample_first is not None else LONG_RUN_SAMPLE_FIRST)
    for key in _FORWARDED_TUNABLES:
        if os.environ.get(key):
            env[key] = os.environ[key]
    user_stdlib = (os.environ.get(STDLIB_ENV) or "").strip()
    if user_stdlib:
        if user_stdlib.lower() not in _STDLIB_OFF:
            env[STDLIB_ENV] = user_stdlib
    elif trace_stdlib:
        env[STDLIB_ENV] = ",".join(trace_stdlib)
    return env


def sampling_policy(env: dict, source: str) -> dict:
    """The value-sampling policy a capture ran with (recorded in its metadata)."""
    rate = float(env.get("PYMOLT_TRACE_SAMPLE_RATE", "1"))
    first = int(env.get("PYMOLT_TRACE_SAMPLE_FIRST", "0")) if rate < 1 else 0
    return {"rate": rate, "first": first, "source": source}


def default_sample_rate(mode: CaptureMode) -> float:
    """Value-sampling rate when the caller passes none: every call of a test suite (the
    oracle); a tenth of a long run's repetitions, after each call site's first
    ``LONG_RUN_SAMPLE_FIRST`` calls, with every call still counted in the tallies."""
    return 1.0 if mode is CaptureMode.TEST_SUITE else LONG_RUN_SAMPLE_RATE


# Sink and discover/watch mode per capture kind (docs/design/tier1-observation-substrate.md):
# a test suite is the oracle and must see everything; a long run aggregates in-process.
TEST_SINK, TEST_TRACE_MODE = "jsonl", "discover"
LONG_RUN_SINK, LONG_RUN_TRACE_MODE = "facts", "auto"
# Measured (bench/load, phase 2): every call sampled costs ~40% rps in watch mode; 10% of
# the repetitions, all calls tallied, ~14%.
LONG_RUN_SAMPLE_RATE, LONG_RUN_SAMPLE_FIRST = 0.1, 32
_FORWARDED_TUNABLES = (
    "PYMOLT_TRACE_SAMPLE_KEYS",
    "PYMOLT_TRACE_FLUSH_SECONDS", "PYMOLT_TRACE_FACTS_PER_SITE", "PYMOLT_TRACE_MAX_FACTS",
    "PYMOLT_TRACE_MAX_TALLIES", "PYMOLT_TRACE_MAX_FACT_BYTES", "PYMOLT_TRACE_FACT_VALUE_BYTES",
    "PYMOLT_TRACE_DISCOVER_QUIET", "PYMOLT_TRACE_DISCOVER_MAX",
)


def _process_traces(directory: Path, recursive: bool = False) -> list[str]:
    """One trace path per traced process: its JSONL, or for a facts sink the JSONL path its
    ``trace-<pid>.facts.json`` stands in for (whose ``.meta.json`` holds its counters)."""
    pattern = "**/trace-*" if recursive else "trace-*"
    out = set(glob.glob(str(directory / f"{pattern}.jsonl"), recursive=recursive))
    for found in glob.glob(str(directory / f"{pattern}.facts.json"), recursive=recursive):
        out.add(found[:-len(".facts.json")] + ".jsonl" if found.endswith(".facts.json")
                else found)
    return sorted(out)


def _process_snapshot(trace_path: str) -> str | None:
    """The facts snapshot a per-process trace path stands for (``trace-1.facts.json``)."""
    if not trace_path.endswith(".jsonl"):
        return None
    candidate = trace_path[:-len(".jsonl")] + ".facts.json"
    return candidate if os.path.isfile(candidate) else None


def _normalize_impact_targets(values: list[str] | tuple[str, ...] | None) -> list[str]:
    if values is None:
        raw = os.environ.get("PYMOLT_TRACE_IMPACT_TARGETS", "")
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = raw.split(",")
            values = parsed if isinstance(parsed, list) else raw.split(",")
    result: list[str] = []
    for value in values or ():
        if not isinstance(value, str):
            raise ValueError("impact_targets must contain only strings")
        value = value.strip()
        if value and value not in result:
            result.append(value)
    return result


def _applied_plan_impact_targets(project_dir: str | Path) -> list[str]:
    """Resolve old and replacement API paths from the currently applied plan."""
    from pymolt.migration_plan import MigrationPlan
    from pymolt.migration_state import MigrationReceipt

    receipt = MigrationReceipt.load(project_dir)
    if receipt is None or receipt.status != "applied" or not receipt.plan_path:
        return []
    plan = MigrationPlan.load(project_dir, receipt.plan_path)
    if plan is None:
        return []
    paths: list[str] = []
    for impact in plan.impacts:
        for value in (impact.path, impact.replacement_path):
            if value and value not in paths:
                paths.append(value)
    return paths


def _effective_stdlib(trace_stdlib: list[str] | None) -> list[str]:
    """The stdlib modules a capture really traced: the caller's own variable wins."""
    user = (os.environ.get(STDLIB_ENV) or "").strip()
    if user:
        if user.lower() in _STDLIB_OFF:
            return []
        return sorted({m.strip() for m in user.split(",") if m.strip()})
    return sorted(set(trace_stdlib or ()))


def _is_python_move(project_dir: str | Path) -> bool:
    """True when setup's base and target Python differ in major.minor."""
    config = EnvConfig.load(Path(project_dir) / ENV_CONFIG_PATH)
    if config is None or not config.base_python or not config.target_python:
        return False

    def _mm(version: str) -> tuple[str, ...] | None:
        parts = version.strip().split(".")
        return tuple(parts[:2]) if len(parts) >= 2 and parts[0].isdigit() else None

    base, target = _mm(config.base_python), _mm(config.target_python)
    return base is not None and target is not None and base != target


def _stdlib_top(name: str) -> str | None:
    top = name.split(".")[0]
    return top if top in sys.stdlib_module_names and top != "__future__" else None


def _stdlib_modules_on_lines(source: str, lines: set[int]) -> set[str]:
    """Stdlib top-level modules imported on, or used by name on, the given 1-based lines."""
    import ast

    if not lines:
        return set()
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return set()
    bound: dict[str, str] = {}  # local name -> stdlib top-level module
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = _stdlib_top(alias.name)
                if top:
                    bound[(alias.asname or alias.name).split(".")[0]] = top
                    if node.lineno in lines:
                        found.add(top)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            top = _stdlib_top(node.module)
            if top:
                for alias in node.names:
                    bound[alias.asname or alias.name] = top
                if node.lineno in lines:
                    found.add(top)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.lineno in lines and node.id in bound:
            found.add(bound[node.id])
    return found


def _changed_lines(old: str, new: str) -> set[int]:
    """1-based line numbers of ``new`` that differ from ``old`` (replaced or inserted)."""
    import difflib

    matcher = difflib.SequenceMatcher(None, old.splitlines(), new.splitlines(), autojunk=False)
    changed: set[int] = set()
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "insert"):
            changed.update(range(j1 + 1, j2 + 1))
    return changed


def _read_project_text(root: Path, rel: str) -> str | None:
    try:
        return (root / rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _edited_stdlib_modules(project_dir: str | Path) -> set[str]:
    """Stdlib modules imported on lines that changed since the baseline.

    Where an apply receipt exists the plan's frozen ``old_source`` is the baseline text of
    every file the plan touched. Any other file is compared best effort against the baseline
    trace's recorded site text: a recorded call line that no longer reads the same marks the
    nearby line that now reads most like it as edited."""
    root = Path(os.path.abspath(project_dir))
    found: set[str] = set()
    covered: set[str] = set()
    try:
        from pymolt.migration_plan import MigrationPlan
        from pymolt.migration_state import MigrationReceipt

        receipt = MigrationReceipt.load(root)
        plan = (MigrationPlan.load(root, receipt.plan_path)
                if receipt is not None and receipt.status == "applied" and receipt.plan_path
                else None)
    except Exception as exc:  # noqa: BLE001 - best effort
        logger.info("stdlib tracing: could not read the apply receipt: %s", exc)
        plan = None
    for change in (plan.changes if plan is not None else ()):
        current = _read_project_text(root, change.path)
        covered.add(change.path)
        if current is not None:
            found |= _stdlib_modules_on_lines(current, _changed_lines(change.old_source, current))
    baseline = load_contract_state(root).baseline
    if baseline is None:
        return found
    wanted: dict[str, set[int]] = {}
    for key, text in load_site_text(baseline.trace_path).items():
        file, _, line = key.rpartition(":")
        if not line.isdigit():
            continue
        rel = os.path.relpath(file, root) if os.path.isabs(file) else file
        if rel in covered or rel.startswith(".."):
            continue
        current = _read_project_text(root, rel)
        if current is None:
            continue
        lines = [entry.strip()[:240] for entry in current.splitlines()]
        if text in lines:
            continue
        # The recorded line was edited: find where it went (edits shift line numbers) as
        # the nearby current line that reads most like it.
        import difflib

        origin = int(line)
        best = max(
            (i for i in range(len(lines)) if abs(i + 1 - origin) <= 40),
            key=lambda i: (difflib.SequenceMatcher(None, text, lines[i]).ratio(),
                           -abs(i + 1 - origin)),
            default=None,
        )
        if best is not None and difflib.SequenceMatcher(None, text, lines[best]).ratio() >= 0.5:
            wanted.setdefault(rel, set()).add(best + 1)
    for rel, numbers in wanted.items():
        current = _read_project_text(root, rel)
        if current is not None:
            found |= _stdlib_modules_on_lines(current, numbers)
    return found


# Stdlib modules never worth tracing during a Python move: they cannot change behaviour in
# a way a call trace would show, or they only add noise (volume, or recursion into pymolt's
# own recording).
_STDLIB_NOT_TRACED = frozenset({
    "__future__",  # compiler directives: imports bind no callable and nothing is ever called
    "typing",      # annotations and aliases; its calls (cast, TypeVar, NewType) are identity-like
                   # and appear on every class and function definition
    "abc",         # ABCMeta machinery runs at every class definition, not at the project's calls
    "builtins",    # the interpreter's own names; they are never "imported" behaviour
    "warnings",    # the runtime's warning observer already records them; tracing the call
                   # would record the recorder
})
_IMPORT_LINE = re.compile(
    r"^\s*(?:import\s+([\w., \t]+?)\s*(?:[#;].*)?$|from\s+([A-Za-z_]\w*)[\w.]*\s+import\b)")


def project_stdlib_imports(project_dir: str | Path) -> set[str]:
    """Every stdlib top-level module the project's own files import (any depth, any syntax:
    a line scan, so a Python 2 file counts too). Relative imports are the project's."""
    from pymolt.core import stdlib as stdlib_data
    from pymolt.verify.contact_map import _source_files

    root = Path(os.path.abspath(project_dir))
    found: set[str] = set()
    for path in _source_files(root)[0]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            m = _IMPORT_LINE.match(line)
            if not m:
                continue
            names = (m.group(2),) if m.group(2) else (
                part.split(" as ")[0].strip().split(".")[0] for part in m.group(1).split(","))
            found.update(n for n in names if n and stdlib_data.is_stdlib(n))
    return found - _STDLIB_NOT_TRACED


def stdlib_trace_modules(project_dir: str | Path, when: str) -> list[str]:
    """Stdlib top-level modules to trace for a capture of a Python-version move.

    ``[]`` for a capture that is not part of a move. Baseline: every stdlib module the
    project imports (``project_stdlib_imports``) plus those of its ``stdlib_sites`` (the
    contact map), less ``_STDLIB_NOT_TRACED``. Post-migration: the same set plus the stdlib
    modules imported on lines the applied plan or the user changed since the baseline
    (``shutil`` after ``click.get_terminal_size`` became ``shutil.get_terminal_size``)."""
    if not _is_python_move(project_dir):
        return []
    from pymolt.verify.contact_map import build_contact_map

    modules = {site.module for site in build_contact_map(project_dir).stdlib_sites if site.module}
    modules |= project_stdlib_imports(project_dir)
    if when == "post_migration":
        modules |= _edited_stdlib_modules(project_dir)
    return sorted(modules - _STDLIB_NOT_TRACED)


def _resolve_stdlib_option(project_dir: Path, when: str, stdlib: str | None) -> list[str]:
    """``stdlib``: None = computed, ``"none"`` = off, else an explicit comma list."""
    if stdlib is not None:
        if stdlib.strip().lower() in _STDLIB_OFF:
            return []
        return sorted({m.strip() for m in stdlib.split(",") if m.strip()})
    if (os.environ.get(STDLIB_ENV) or "").strip():
        return []  # the caller's own variable wins; trace_env forwards it
    try:
        return stdlib_trace_modules(project_dir, when)
    except Exception as exc:  # noqa: BLE001 - never fail a capture over this
        logger.warning("could not compute the stdlib modules to trace: %s", exc)
        return []


def _capture_identity(supplied: str | None, env_name: str) -> str | None:
    value = supplied if supplied is not None else os.environ.get(env_name)
    return value or None


_INTERRUPT_GRACE_SECONDS = 10.0


class _SigintShield:
    """Survive Ctrl+C while a captured child runs.

    A terminal Ctrl+C signals the whole process group, so the child receives SIGINT too.
    pymolt must not die with it: it keeps waiting for the child, then finalizes the capture.
    ``count`` is the number of SIGINTs seen. Outside the main thread ``signal.signal`` raises
    ValueError; the shield is then inert (``active`` False) and the previous behaviour stays.
    """

    def __init__(self) -> None:
        self.count = 0
        self.active = False
        self._previous = None

    def _handle(self, signum, frame) -> None:  # noqa: ARG002
        self.count += 1

    def __enter__(self) -> _SigintShield:
        try:
            self._previous = signal.signal(signal.SIGINT, self._handle)
            self.active = True
        except (ValueError, OSError):
            self.active = False
        return self

    def __exit__(self, *exc) -> None:
        if self.active:
            try:
                signal.signal(
                    signal.SIGINT,
                    self._previous if self._previous is not None else signal.SIG_DFL,
                )
            except (ValueError, OSError):
                pass
            self.active = False


def _stop_process(proc: subprocess.Popen) -> None:
    """SIGTERM, then SIGKILL after a short grace period."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def capture_trace_local(
    target: str, command: list[str], backend: str, bundle: Path, work: Path,
    exclude: str, source: str, include_internal: bool,
    cancel_event: threading.Event | None = None,
    cwd: str | Path | None = None,
    log_path: Path | None = None,
    privacy: str = "values",
    sample_rate: float = 1.0,
    impact_targets: list[str] | None = None,
    deployment_id: str | None = None,
    request_id: str | None = None,
    correlation_id: str | None = None,
    interrupted: threading.Event | None = None,
    sink: str | None = None,
    trace_mode: str | None = None,
    sample_first: int | None = None,
    widen_targets: bool = False,
    trace_stdlib: list[str] | None = None,
) -> tuple[list[str], int | None]:
    """Inject the watcher into a locally-run command via PYTHONPATH + env.

    Blocks until ``command`` exits, unless ``cancel_event`` is given and gets
    set from another thread first — then the process is terminated (SIGTERM,
    then SIGKILL after a grace period) and whatever it had already written is
    kept. This is what powers the guided flow's "Stop & Collect" for a
    long-running app (a dev server that never exits on its own).

    When ``cancel_event`` is ``None`` (e.g. the MCP server's ``capture`` tool,
    which never passes one) there is no other thread able to cancel a command
    that never exits on its own — so this still enforces ``_CAPTURE_TIMEOUT``
    (``PYMOLT_CAPTURE_TIMEOUT`` env var, default 1800s): on expiry the process is
    terminated/killed the same way, and ``ValueError`` is raised.

    ``cwd`` runs the command in the target project (default: the parent's cwd).
    The child's stdout+stderr are ALWAYS redirected (merged) to ``log_path`` —
    never inherited onto the parent tty. Returns
    the produced per-process JSONL paths and the command's exit code.

    Ctrl+C (SIGINT) does not abort pymolt: the first one starts a grace period
    (``_INTERRUPT_GRACE_SECONDS``) while the child, which got the same signal via the process
    group, winds down; a second one, or the grace expiring, terminates the child. Either way
    the capture is finalized and ``interrupted`` (if given) is set.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(bundle), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env["PYMOLT_TRACE_OUT"] = str(work / "trace-{pid}.jsonl")
    env.update(trace_env(
        target, backend, exclude, source, include_internal, privacy, sample_rate,
        impact_targets, deployment_id, request_id, correlation_id,
        sink=sink or TEST_SINK, trace_mode=trace_mode or TEST_TRACE_MODE,
        sample_first=sample_first, widen_targets=widen_targets,
        trace_stdlib=trace_stdlib,
    ))

    log_file = None
    returncode: int | None = None
    try:
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            # merged stdout+stderr, line-buffered append; never the tty
            log_file = open(log_path, "a", buffering=1, encoding="utf-8", errors="replace")
        # Popen (not adapters/subprocess_runner.run_command) is a deliberate exception here:
        # this call needs both cancellation (cancel_event, polled below) and live merged-log
        # streaming to log_path while the command runs — run_command is a blocking
        # subprocess.run wrapper and exposes neither.
        with _SigintShield() as shield:
            proc = subprocess.Popen(
                command, env=env, cwd=str(cwd) if cwd is not None else None,
                stdout=log_file, stderr=subprocess.STDOUT,
            )
            deadline = time.monotonic() + _CAPTURE_TIMEOUT if cancel_event is None else None
            grace_deadline: float | None = None
            try:
                while proc.poll() is None:
                    now = time.monotonic()
                    if shield.count:
                        if interrupted is not None:
                            interrupted.set()
                        if grace_deadline is None:
                            grace_deadline = now + _INTERRUPT_GRACE_SECONDS
                        if shield.count >= 2 or now >= grace_deadline:
                            _stop_process(proc)
                            break
                    if cancel_event is not None and cancel_event.is_set():
                        _stop_process(proc)
                        break
                    if deadline is not None and now >= deadline:
                        _stop_process(proc)
                        # Whatever the watcher had already flushed to work/trace-*.jsonl before
                        # the kill is left on disk untouched — we never reach the glob/return
                        # below to hand it back to capture_trace() for merging on this (failed)
                        # call.
                        raise ValueError(
                            f"capture timed out after {_CAPTURE_TIMEOUT}s: {command}"
                        ) from None
                    time.sleep(0.05)
            finally:
                if proc.poll() is None:
                    proc.kill()
            if shield.count and interrupted is not None:
                interrupted.set()
            returncode = proc.poll()
    finally:
        if log_file is not None:
            log_file.close()
    return _process_traces(work), returncode


def capture_trace_in_container(
    target: str, command: list[str], backend: str, bundle: Path, work: Path,
    container: str, workdir: str | None, exclude: str, source: str, include_internal: bool,
    privacy: str = "values", sample_rate: float = 1.0,
    impact_targets: list[str] | None = None,
    deployment_id: str | None = None,
    request_id: str | None = None,
    correlation_id: str | None = None,
    sink: str | None = None,
    trace_mode: str | None = None,
    sample_first: int | None = None,
    widen_targets: bool = False,
    trace_stdlib: list[str] | None = None,
) -> tuple[list[str], int]:
    """Inject the watcher into an ALREADY-RUNNING container via `docker cp` + `docker exec`.

    No `docker run` from scratch and no new volume mounts: the standalone bundle is copied in,
    the command runs under it with PYMOLT_TRACE_* env, and the JSONL is copied back out. The
    target container and the app inside it are never modified (the bundle is temp + removed).
    """
    tag = uuid.uuid4().hex[:8]
    bundle_in = f"/tmp/pymolt_bundle_{tag}"
    trace_in = f"/tmp/pymolt_trace_{tag}"
    _docker(["cp", str(bundle), f"{container}:{bundle_in}"])
    _docker(["exec", container, "mkdir", "-p", trace_in])

    exec_args = ["exec",
                 "-e", f"PYTHONPATH={bundle_in}",
                 "-e", f"PYMOLT_TRACE_OUT={trace_in}/trace-{{pid}}.jsonl"]
    for key, val in trace_env(
        target, backend, exclude, source, include_internal, privacy, sample_rate,
        impact_targets, deployment_id, request_id, correlation_id,
        sink=sink or TEST_SINK, trace_mode=trace_mode or TEST_TRACE_MODE,
        sample_first=sample_first, widen_targets=widen_targets,
        trace_stdlib=trace_stdlib,
    ).items():
        exec_args += ["-e", f"{key}={val}"]
    if workdir:
        exec_args += ["-w", workdir]
    exec_args += [container, *command]
    command_result = _docker(exec_args, check=False)

    pull = work / "pull"
    _docker(["cp", f"{container}:{trace_in}", str(pull)])
    # best-effort cleanup so the container is left exactly as found
    _docker(["exec", container, "rm", "-rf", bundle_in, trace_in], check=False)
    return _process_traces(pull, recursive=True), command_result.returncode


def _docker(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    res = run_command(["docker", *args], check=False)
    if check and res.returncode != 0:
        raise ValueError(f"docker {' '.join(args[:2])} failed: {res.stderr.strip()}")
    return res


def _container_runner_spec(container: str | None):
    """The RunnerSpec of the container a capture ran in (None = it ran locally)."""
    from pymolt.adapters.python_runner import inspect_container

    return inspect_container(container) if container else None


def container_is_running(container_id: str) -> bool:
    """True if a container with this id/name is currently running (best-effort).

    Shared by the SetupPanel STALE check and ``resolve_capture_env`` below. The one docker
    liveness check lives in ``pymolt.adapters.python_runner``; this delegates to it.
    """
    from pymolt.adapters.python_runner import is_running

    return is_running(container_id)


def display_container(container_id: str | None) -> str:
    """A container name in full; only a 64-character hex id is shortened (to 12)."""
    value = container_id or ""
    if len(value) == 64 and all(c in "0123456789abcdefABCDEF" for c in value):
        return value[:12]
    return value


def resolve_capture_env(project_dir: str | Path, when: str) -> dict:
    """What environment a capture for this slot should default to, per the
    project's saved :class:`EnvConfig` — the fix for the root-cause bug where a
    baseline capture of a legacy (container-based) project silently ran against
    the local interpreter instead of the configured legacy container.

    Returns ``{"container": str|None, "workdir": str|None,
    "command_prefix": list[str]|None, "note": str}``. ``command_prefix`` is a
    SUGGESTION for callers to prefill a command with — this
    function never rewrites anyone's command.
    """
    config = EnvConfig.load(Path(project_dir) / ENV_CONFIG_PATH)
    if config is None:
        return {"container": None, "workdir": None, "command_prefix": None,
                "note": "no saved config — running locally"}

    if when == "baseline" and config.selected_tool.value == "container" and config.container_id:
        if container_is_running(config.container_id):
            return {
                "container": config.container_id, "workdir": None,
                "command_prefix": None,
                "note": f"baseline → container {display_container(config.container_id)}",
            }
        return {
            "container": None, "workdir": None, "command_prefix": None,
            "note": (
                f"configured container {display_container(config.container_id)} not running — "
                "baseline ran locally; re-run setup to refresh"
            ),
        }

    # ``target_env_path`` is purely user-declared (set via `pymolt setup`'s target-env
    # prompt, or hand-edited) — pymolt never builds this env itself (see
    # `pymolt.setup.target_hint`, which only *suggests* one). An explicit `container` from
    # the caller (bypassing this function entirely — see `capture_named_trace`) always wins;
    # this is the fallback when the caller left it unset.
    if when == "post_migration" and config.target_env_path:
        venv = Path(config.target_env_path)
        if not venv.is_absolute():
            venv = Path(os.path.abspath(project_dir)) / venv
        for candidate in (venv / "bin" / "python", venv / "Scripts" / "python.exe"):
            if candidate.exists():
                # Absolute, never resolved: a venv's bin/python is a symlink to the base
                # interpreter, and running the link target loses the venv (and its pytest).
                return {
                    "container": None, "workdir": None,
                    "command_prefix": [os.path.abspath(candidate), "-m"],
                    "note": f"post-migration → target env {config.target_env_path}",
                }

    return {"container": None, "workdir": None, "command_prefix": None,
            "note": "running locally"}


def _command_in_configured_env(command: list[str], prefix: list[str] | None) -> list[str]:
    """Resolve a post-capture command into the configured target virtualenv."""
    if not prefix or not command:
        return list(command)
    python = Path(prefix[0])
    executable = Path(command[0])
    if executable.name.lower().startswith("python"):
        return [str(python), *command[1:]]
    if executable.parent == Path("."):
        venv_executable = python.parent / executable.name
        if venv_executable.is_file():
            return [str(venv_executable), *command[1:]]
        return [str(python), "-m", *command]
    # An explicit path is an explicit environment choice; do not reinterpret it.
    return list(command)


def _merge_traces(produced: list[str], out_path: Path) -> int:
    """The watcher writes one file per process ({pid}); merge children into one artifact."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    events = 0
    with out_path.open("w") as out:
        for f in produced:
            if not os.path.isfile(f):
                continue  # a facts-sink process: its evidence is the snapshot
            with open(f) as fh:
                for line in fh:
                    if line.strip():
                        out.write(line)
                        events += 1
    return events


def _sink_kwargs(sink: str | None, trace_mode: str | None,
                 sample_first: int | None = None, widen_targets: bool = False,
                 trace_stdlib: list[str] | None = None) -> dict:
    """Only pass the phase-2 keywords when set (test doubles predate them)."""
    out: dict = {}
    if trace_stdlib:
        out["trace_stdlib"] = list(trace_stdlib)
    if widen_targets:
        out["widen_targets"] = True
    if sink is not None:
        out["sink"] = sink
    if trace_mode is not None:
        out["trace_mode"] = trace_mode
    if sample_first is not None:
        out["sample_first"] = sample_first
    return out


def _merge_process_snapshots(produced: list[str], out_path: Path) -> dict | None:
    """Merge the per-process facts snapshots into ``<out_path>.facts.json`` and project its
    facts into ``out_path`` (the JSONL readers keep working). None when no process ran the
    facts sink. Streaming: one snapshot is read at a time (``merge_fact_snapshots``)."""
    snapshots = [s for s in (_process_snapshot(p) for p in produced) if s]
    if not snapshots:
        return None
    target = snapshot_path(out_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    header = merge_fact_snapshots(snapshots, target)
    if header is None:
        return None
    projected = project_snapshot(target, out_path)
    counters = header.get("counters") or {}
    return {
        "path": str(target),
        "processes": len(snapshots),
        "projected_events": projected,
        "facts": counters.get("facts", 0),
        "calls": counters.get("calls"),
        "sampled": counters.get("sampled"),
        "evicted": counters.get("evicted", 0),
        "evicted_facts": counters.get("evicted_facts", 0),
        "widened": counters.get("widened", 0),
        "outcomes_other": counters.get("outcomes_other", 0),
        "tally_overflow": counters.get("tally_overflow", 0),
        "mode": header.get("mode"),
    }


def _record_sampling(trace_path: str | Path, sample_rate: float, source: str) -> None:
    """Stamp the effective value-sampling policy (and whether it was the mode's default)
    into the capture metadata."""
    metadata = _read_trace_metadata(trace_path)
    if not metadata:
        return
    sampling = metadata.get("sampling") if isinstance(metadata.get("sampling"), dict) else {}
    first = sampling.get("first")
    metadata["sampling"] = {
        "rate": sample_rate,
        "first": (first if isinstance(first, int) else LONG_RUN_SAMPLE_FIRST)
        if sample_rate < 1 else 0,
        "source": source,
    }
    _write_trace_metadata(trace_path, metadata)


def _move_facts_snapshot(source: str | Path, destination: str | Path) -> str | None:
    """Move ``<source>.facts.json`` along with its trace (None when there is none) and point
    the destination's metadata sidecar at its new place."""
    source_path = snapshot_path(source)
    if not source_path.is_file():
        return None
    destination_path = snapshot_path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source_path, destination_path)
    metadata = _read_trace_metadata(destination)
    if metadata:
        metadata["facts_path"] = str(destination_path)
        if isinstance(metadata.get("facts"), dict):
            metadata["facts"]["path"] = str(destination_path)
        _write_trace_metadata(destination, metadata)
    return str(destination_path)


_DROP_COUNTERS = (
    "sampling_dropped", "backpressure_dropped", "write_failures", "shutdown_dropped",
)
_RUNTIME_COUNTERS = (
    "events_seen",
    "events_written",
    "sampling_dropped",
    "backpressure_dropped",
    "write_failures",
    "sink_failures",
    "shutdown_dropped",
)


def _runtime_counters_complete(counters: object) -> bool:
    return isinstance(counters, dict) and all(
        isinstance(counters.get(name), int) and counters[name] >= 0
        for name in _RUNTIME_COUNTERS
    )


def _trace_metadata_path(trace_path: str | Path) -> Path:
    return Path(str(trace_path) + ".meta.json")


def _read_trace_metadata(trace_path: str | Path) -> dict:
    path = _trace_metadata_path(trace_path)
    try:
        with path.open(encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _aggregate_runtime_metadata(produced: list[str]) -> dict:
    counters = {
        "events_seen": 0,
        "events_written": 0,
        "sampling_dropped": 0,
        "backpressure_dropped": 0,
        "write_failures": 0,
        "sink_failures": 0,
        "shutdown_dropped": 0,
    }
    processes: list[dict] = []
    backends: list[str] = []
    skipped: list[dict[str, str]] = []
    metadata_complete = True
    for trace_path in produced:
        metadata = _read_trace_metadata(trace_path)
        if not metadata:
            metadata_complete = False
            continue
        processes.append(metadata)
        backend = metadata.get("backend")
        if isinstance(backend, str) and backend not in backends:
            backends.append(backend)
        for item in metadata.get("instrumentation_skipped", ()):
            if isinstance(item, dict) and isinstance(item.get("target"), str):
                normalized = {
                    "target": item["target"],
                    "reason": str(item.get("reason", "unknown")),
                }
                if normalized not in skipped:
                    skipped.append(normalized)
        raw_counters = metadata.get("counters")
        if not isinstance(raw_counters, dict):
            metadata_complete = False
            continue
        process_counters_complete = _runtime_counters_complete(raw_counters)
        for name in counters:
            value = raw_counters.get(name)
            if isinstance(value, int) and value >= 0:
                counters[name] += value
        metadata_complete = metadata_complete and process_counters_complete
    if len(processes) != len(produced):
        metadata_complete = False
    counters["dropped_events"] = sum(counters[name] for name in _DROP_COUNTERS)
    return {
        "counters": counters,
        "metadata_complete": metadata_complete,
        "process_metadata": processes,
        "backends": backends,
        "instrumentation_skipped": skipped,
    }


def _write_trace_metadata(trace_path: str | Path, payload: dict) -> str | None:
    path = _trace_metadata_path(trace_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(f".{path.name}.{uuid.uuid4().hex}.pending")
    try:
        with pending.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.flush()
        os.replace(pending, path)
    except OSError as exc:
        logger.warning("could not persist trace metadata %s: %s", path, exc)
        try:
            pending.unlink()
        except OSError:
            pass
        return None
    return str(path)


def _move_trace_metadata(source: str | Path, destination: str | Path) -> str | None:
    source_path = _trace_metadata_path(source)
    if not source_path.is_file():
        return None
    destination_path = _trace_metadata_path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source_path, destination_path)
    return str(destination_path)


_ACTIVATION_ERROR_SUFFIX = ".activation-error.json"


def _collect_activation_failures(work: Path) -> list[dict]:
    """Read the markers a watcher leaves when it was requested but could not load.

    They live in the ephemeral work dir, so they must be read before it is removed: a
    process that never traced is otherwise indistinguishable from one that never called
    a dependency.
    """
    failures: list[dict] = []
    for marker in sorted(glob.glob(str(work / "**" / f"trace-*{_ACTIVATION_ERROR_SUFFIX}"),
                                   recursive=True)):
        try:
            payload = json.loads(Path(marker).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {"error": "unreadable activation-error marker"}
        if not isinstance(payload, dict):
            payload = {"error": str(payload)}
        failures.append({
            "pid": payload.get("pid"),
            "python": payload.get("python"),
            "executable": payload.get("executable"),
            "error": str(payload.get("error", "unknown")),
        })
    return failures


def capture_trace(
    target: str, command: list[str], out_path: str | Path, backend: str,
    container: str | None = None, workdir: str | None = None,
    exclude: str = "", source: str = "", include_internal: bool = False,
    cancel_event: threading.Event | None = None,
    cwd: str | Path | None = None,
    privacy: str = "values",
    sample_rate: float = 1.0,
    impact_targets: list[str] | None = None,
    deployment_id: str | None = None,
    request_id: str | None = None,
    correlation_id: str | None = None,
    sink: str | None = None,
    trace_mode: str | None = None,
    sample_first: int | None = None,
    widen_targets: bool = False,
    trace_stdlib: list[str] | None = None,
) -> TraceCaptureResult:
    """Run ``command`` with the watcher injected; merge produced JSONL into ``out_path``.

    ``sink`` / ``trace_mode`` default to the test-suite pair (``jsonl`` + ``discover``);
    ``capture_named_trace`` passes ``facts`` + ``auto`` for a long-running command. Processes
    that ran the facts sink leave ``trace-<pid>.facts.json``: they are merged into
    ``<out_path>.facts.json`` (counts summed, caps re-applied) and projected into the JSONL
    as counted events, before the work dir is removed.

    Injection is purely external: a standalone bundle on PYTHONPATH + PYMOLT_TRACE_* env, with
    the target (local process, or an already-running ``--container``) left untouched.

    ``cwd`` runs a local command in the target project rather than pymolt's own
    working directory (so e.g. ``pytest tests/`` collects the right tests). The
    local command's merged stdout+stderr land in ``command.log`` next to the
    trace artifacts (surfaced via ``command_log`` / ``returncode``), never on the
    parent tty.
    """
    started = time.monotonic()
    impact_targets = _normalize_impact_targets(impact_targets)
    deployment_id = _capture_identity(deployment_id, "PYMOLT_TRACE_DEPLOYMENT_ID")
    request_id = _capture_identity(request_id, "PYMOLT_TRACE_REQUEST_ID")
    correlation_id = _capture_identity(correlation_id, "PYMOLT_TRACE_CORRELATION_ID")

    work = Path(tempfile.mkdtemp(prefix="pymolt-trace-"))
    complete = False
    try:
        bundle = export_watcher(work / "bundle")

        command_log: str | None = None
        returncode: int | None = None
        interrupted = threading.Event()
        if container:
            produced, returncode = capture_trace_in_container(
                target, command, backend, bundle, work,
                container, workdir, exclude, source, include_internal,
                privacy, sample_rate, impact_targets,
                deployment_id, request_id, correlation_id,
                **_sink_kwargs(sink, trace_mode, sample_first, widen_targets, trace_stdlib),
            )
        else:
            # Keep the log next to the durable trace artifact (out_path), not the
            # ephemeral work tempdir, so the path stored on the slot stays valid.
            out_p = Path(out_path)
            log_path = out_p.parent / f"{out_p.stem}.command.log"
            produced, returncode = capture_trace_local(
                target, command, backend, bundle, work,
                exclude, source, include_internal, cancel_event=cancel_event,
                cwd=cwd, log_path=log_path, privacy=privacy, sample_rate=sample_rate,
                impact_targets=impact_targets, deployment_id=deployment_id,
                request_id=request_id, correlation_id=correlation_id,
                interrupted=interrupted,
                **_sink_kwargs(sink, trace_mode, sample_first, widen_targets, trace_stdlib),
            )
            command_log = str(log_path) if log_path.exists() else None

        events = _merge_traces(produced, Path(out_path))
        facts = _merge_process_snapshots(produced, Path(out_path))
        # A facts process's events are the calls it folded into facts (its JSONL meaning:
        # events_written), not the projected lines that summarize them.
        events += (facts.get("sampled") or 0) if facts else 0
        runtime = _aggregate_runtime_metadata(produced)
        activation_failures = _collect_activation_failures(work)
        counters = runtime["counters"]
        runtime_backends = runtime["backends"]
        effective_backend = ",".join(runtime_backends) if runtime_backends else backend
        # Sidecars from older/interrupted watcher bundles may be absent. Written
        # events remain useful, but loss cannot then be claimed to be zero. Likewise
        # when some interpreter never traced at all.
        metadata_complete = runtime["metadata_complete"] and not activation_failures
        events_seen = (
            max(counters["events_seen"], events + counters["dropped_events"])
            if metadata_complete else None
        )
        dropped_events = counters["dropped_events"] if metadata_complete else None
        result = TraceCaptureResult(
            out_path=str(out_path), events=events, processes=len(produced),
            where=f"container:{container}" if container else "local",
            command_log=command_log, returncode=returncode,
            cancelled=bool(
                (cancel_event is not None and cancel_event.is_set()) or interrupted.is_set()
            ),
            duration_seconds=round(max(0.0, time.monotonic() - started), 6),
            deployment_id=deployment_id, request_id=request_id,
            correlation_id=correlation_id, sample_rate=sample_rate,
            backend=effective_backend, impact_targets=impact_targets,
            events_seen=events_seen, dropped_events=dropped_events,
            sampling_dropped=(counters["sampling_dropped"] if metadata_complete else None),
            backpressure_dropped=(
                counters["backpressure_dropped"] if metadata_complete else None
            ),
            write_failures=(counters["write_failures"] if metadata_complete else None),
            sink_failures=(counters["sink_failures"] if metadata_complete else None),
            instrumentation_skipped=runtime["instrumentation_skipped"],
            activation_failures=activation_failures,
            facts_path=facts["path"] if facts else None,
            trace_stdlib=_effective_stdlib(trace_stdlib),
        )
        metadata = {
            "schema_version": 1,
            "duration_seconds": result.duration_seconds,
            "deployment_id": deployment_id,
            "request_id": request_id,
            "correlation_id": correlation_id,
            "sample_rate": sample_rate,
            "sampling": {"rate": sample_rate,
                         "first": (sample_first if sample_first is not None
                                   else LONG_RUN_SAMPLE_FIRST) if sample_rate < 1 else 0},
            "backend": effective_backend,
            "impact_targets": impact_targets,
            "trace_stdlib": result.trace_stdlib,
            "events": events,
            "events_seen": events_seen,
            "runtime_metadata_complete": metadata_complete,
            "counters": counters,
            "instrumentation_skipped": result.instrumentation_skipped,
            "activation_failures": activation_failures,
            "processes": runtime["process_metadata"],
        }
        if facts:
            metadata["facts_path"] = facts["path"]
            metadata["facts"] = facts
        result.metadata_path = _write_trace_metadata(out_path, metadata)
        complete = True
        return result
    finally:
        # Everything in ``work`` has been merged into ``out_path`` / its metadata sidecar by
        # now, so it is disposable. On an error path (e.g. timeout) keep it only if the
        # watcher left raw per-process traces that were never copied anywhere.
        if complete or not _process_traces(work, recursive=True):
            shutil.rmtree(work, ignore_errors=True)
        else:
            logger.warning(
                "capture did not finish; raw per-process traces were kept (not merged) in %s",
                work,
            )


# ─────────────────────────────────────────────────────────────────────────────
# Named state — baseline / post_migration slots.
# ─────────────────────────────────────────────────────────────────────────────


def _state_path(project_dir: str | Path) -> Path:
    return Path(project_dir) / CONTRACT_STATE_PATH


def _anchor_slot_path(root: Path, value: str | None) -> str | None:
    """A stored slot path as an absolute one, anchored to the project ``root``.

    Slot paths are stored relative to the project root. A legacy relative path that only
    resolves from the current directory (written by an older pymolt run with a relative
    ``--project-dir``) is still accepted, with a warning.
    """
    if not value:
        return value
    path = Path(value)
    if path.is_absolute():
        return value
    anchored = root / path
    if anchored.exists():
        return str(anchored)
    if path.exists():
        logger.warning(
            "contract state path %r only resolves relative to the current directory; "
            "using it, but capture again to store it relative to the project", value)
        return os.path.abspath(path)
    return str(anchored)


def load_contract_state(project_dir: str | Path) -> ContractState:
    """The current capture state, or a fresh empty one if none is persisted yet.

    Every slot path comes back absolute (anchored to the project root), whatever the
    current directory is; they are stored relative (see ``ContractState.save``).
    """
    state = ContractState.load(_state_path(project_dir)) or ContractState()
    root = Path(os.path.abspath(project_dir))
    for slot in (state.baseline, state.post_migration, *state.diagnostic_captures):
        if slot is not None:
            for field in ("trace_path", "metadata_path", "command_log"):
                setattr(slot, field, _anchor_slot_path(root, getattr(slot, field)))
    return state


def witnessed_distributions(
    project_dir: str | Path, slot: str = "baseline",
) -> dict[str, str] | None:
    """``{normalized distribution name: version}`` the slot's capture actually ran against.

    Read from the per-process record the runtime writes at exit (``distributions`` in the
    capture's metadata). That is witnessed state, unlike a manifest re-resolved today: an
    unpinned ``pandas`` resolves to the newest release, while the baseline may have run
    1.5. A distribution that processes loaded at different versions is left out
    (ambiguous). None when there is no such capture or it predates the record.
    """
    from packaging.utils import canonicalize_name

    root = Path(project_dir)
    captured = getattr(load_contract_state(root), slot, None)
    if captured is None or not captured.metadata_path:
        return None
    meta_path = Path(captured.metadata_path)
    if not meta_path.is_absolute():
        meta_path = root / meta_path
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    seen: dict[str, set[str]] = {}
    recorded = False
    for process in (meta.get("processes") or []) if isinstance(meta, dict) else []:
        dists = process.get("distributions") if isinstance(process, dict) else None
        if not isinstance(dists, dict):
            continue
        recorded = True
        for name, version in dists.items():
            if isinstance(name, str) and isinstance(version, str) and version:
                seen.setdefault(canonicalize_name(name), set()).add(version)
    if not recorded:
        return None
    return {name: versions.pop() for name, versions in seen.items() if len(versions) == 1}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def inspect_trace_artifact(path: str | Path) -> TraceArtifactQuality:
    """Validate that a trace exists, is parseable, and contains usable events."""
    trace = Path(path)
    snap = find_facts_snapshot(trace) if not trace.name.endswith(".facts.json") else (
        trace if trace.is_file() else None)
    if snap is not None and (snap == trace or _blank_file(trace)):
        return _inspect_snapshot(trace, snap)
    if not trace.is_file():
        return TraceArtifactQuality(
            path=str(trace), validity=CaptureValidity.MISSING,
            reason="trace artifact does not exist",
        )
    try:
        text = trace.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return TraceArtifactQuality(
            path=str(trace), validity=CaptureValidity.CORRUPT,
            reason=f"trace artifact is unreadable: {exc}",
        )
    if not text.strip():
        return TraceArtifactQuality(
            path=str(trace), validity=CaptureValidity.EMPTY,
            reason="trace contains no events",
        )

    records: list[object] = []
    invalid_lines = 0
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            aggregate = json.loads(text)
        except json.JSONDecodeError:
            aggregate = None
        if isinstance(aggregate, dict) and isinstance(aggregate.get("records"), list):
            records = list(aggregate["records"])
        else:
            for line in text.splitlines():
                if not line.strip():
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    invalid_lines += 1
    else:
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                invalid_lines += 1

    usable = [record for record in records if isinstance(record, dict) and (
        record.get("q") or record.get("qualname")
    )]
    malformed_records = len(records) - len(usable)
    invalid_lines += malformed_records
    if invalid_lines:
        return TraceArtifactQuality(
            path=str(trace), validity=CaptureValidity.CORRUPT,
            events=len(usable), comparable_events=len(usable), invalid_lines=invalid_lines,
            reason=f"trace contains {invalid_lines} malformed record(s)",
        )
    if not usable:
        return TraceArtifactQuality(
            path=str(trace), validity=CaptureValidity.CORRUPT,
            reason="trace contains no records with a dependency symbol",
        )
    return TraceArtifactQuality(
        path=str(trace), validity=CaptureValidity.VALID,
        events=len(usable), comparable_events=len(usable),
    )


def _blank_file(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            while chunk := fh.read(65536):
                if chunk.strip():
                    return False
    except OSError:
        pass
    return True


def _inspect_snapshot(trace: Path, snap: Path) -> TraceArtifactQuality:
    """Validity of a trace whose only evidence is its facts snapshot."""
    header = read_snapshot_header(snap)
    if not header:
        return TraceArtifactQuality(
            path=str(trace), validity=CaptureValidity.CORRUPT,
            reason=f"facts snapshot {snap.name} is unreadable",
        )
    try:
        facts = sum(1 for _ in iter_snapshot(snap, kinds=("fact",)))
    except OSError as exc:
        return TraceArtifactQuality(
            path=str(trace), validity=CaptureValidity.CORRUPT,
            reason=f"facts snapshot is unreadable: {exc}",
        )
    if not facts:
        return TraceArtifactQuality(
            path=str(trace), validity=CaptureValidity.EMPTY,
            reason="facts snapshot holds no facts",
        )
    return TraceArtifactQuality(
        path=str(trace), validity=CaptureValidity.VALID, events=facts, comparable_events=facts,
    )


def _capture_validity(
    result: TraceCaptureResult, staged_path: Path, *, accept_failing: bool = False,
) -> tuple[CaptureValidity, str | None]:
    if result.returncode not in (None, 0) and not result.cancelled:
        # A red run is accepted only on request, and only when it recorded events: a run
        # that called nothing has nothing to compare, red or not.
        if not (accept_failing and result.events > 0):
            return CaptureValidity.COMMAND_FAILED, f"command exited with code {result.returncode}"
    if result.events == 0:
        return CaptureValidity.EMPTY, "capture produced zero events"
    quality = inspect_trace_artifact(staged_path)
    return quality.validity, quality.reason


_FAILED_COUNT = re.compile(r"(\d+) failed")


def parse_failed_count(command_log: str | Path | None) -> tuple[int | None, str]:
    """``(N, summary line)`` from a pytest-style ``N failed`` line in a command log;
    ``(None, "")`` when the log is missing or has none."""
    if not command_log:
        return None, ""
    try:
        lines = Path(command_log).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None, ""
    for line in reversed(lines):
        match = _FAILED_COUNT.search(line)
        if match:
            return int(match.group(1)), line.strip(" =")
    return None, ""


def static_contact_count(project_dir: str | Path) -> int | None:
    """How many static contacts the project has (None when they cannot be counted)."""
    try:
        from pymolt.verify.contact_map import build_contact_map

        return len(build_contact_map(project_dir).contacts)
    except Exception as exc:  # noqa: BLE001 - a hint detail, never a gate
        logger.info("could not count static contacts: %s", exc)
        return None


def _red_stamp(result: TraceCaptureResult) -> dict | None:
    """``{"returncode", "failed", "summary"}`` for a non-zero run, else None.

    ``failed`` and ``summary`` are parsed from a pytest-style summary line in the command
    log (``N failed``); ``failed`` is None when the log has none.
    """
    if result.returncode in (None, 0) or result.cancelled:
        return None
    failed, summary = parse_failed_count(result.command_log)
    return {"returncode": result.returncode, "failed": failed, "summary": summary}


def _diagnostic_path(project_dir: Path, when: str) -> Path:
    directory = project_dir / ".pymolt" / "contract_traces" / "diagnostics"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = re.sub(r"[^0-9A-Za-z]", "-", _now())
    return directory / f"{when}-{stamp}-{uuid.uuid4().hex[:8]}.jsonl"


def _coverage_available() -> bool:
    """Whether the optional ``coverage`` package is importable — a thin seam so tests can
    monkeypatch the degrade path without depending on the real environment."""
    return importlib.util.find_spec("coverage") is not None


def _build_coverage_data(project_dir: Path, pytest_args: list[str]) -> dict:
    """Thin seam over ``coverage.run_coverage_json`` so tests can inject a fake report
    instead of shelling out to a real ``coverage run``."""
    return run_coverage_json(str(project_dir), pytest_args)


def _coverage_gap_summary(
    project_dir: Path, command: list[str],
) -> tuple[float | None, int, int, list[str]]:
    """Best-effort coverage-based gap summary for a TEST_SUITE capture: a SECOND
    ``coverage run`` pass over the project's own test suite, overlaid onto every static
    dependency-usage site (``gaps.scan_usage_sites`` / ``classify_gaps``), folded into
    ``(coverage_pct, covered_sites, blind_sites, notes)``.

    Degrades to ``(None, 0, 0, [note])`` rather than raising — a capture must never fail
    just because ``coverage`` isn't installed or this second pass errors for any reason.
    """
    if not _coverage_available():
        return None, 0, 0, [
            "coverage package not installed — skipping coverage-based gap summary"
        ]
    try:
        pytest_args = list(command[1:])
        coverage_data = _build_coverage_data(project_dir, pytest_args)
        sites = scan_usage_sites(str(project_dir))
        report = classify_gaps(sites, coverage_data)
        total = report.covered + report.blind
        pct = round(report.covered / total * 100.0, 2) if total else None
        return pct, report.covered, report.blind, []
    except Exception as e:  # noqa: BLE001 — coverage is best-effort, never fails the capture
        return None, 0, 0, [f"coverage-based gap summary failed: {e}"]


def capture_named_trace(
    project_dir: str | Path, when: str, mode: CaptureMode, *,
    command: list[str] | None = None, target: str = "all", backend: str = "auto",
    container: str | None = None, workdir: str | None = None,
    exclude: str = "", source: str = "", include_internal: bool = False,
    cancel_event: threading.Event | None = None,
    cwd: str | Path | None = None,
    privacy: str | None = None,
    sample_rate: float | None = None,
    impact_targets: list[str] | None = None,
    deployment_id: str | None = None,
    request_id: str | None = None,
    correlation_id: str | None = None,
    accept_failing: bool = False,
    stdlib: str | None = None,
) -> ContractSlot:
    """Capture a trace and persist it as the named slot (``"baseline"`` or
    ``"post_migration"``). For ``mode in (TEST_SUITE, LIVE_COMMAND)`` — pymolt
    runs ``command`` itself, blocking until it exits (or is cancelled).

    When the caller passes no explicit ``container`` (the default), the
    execution environment defaults to whatever the project's saved
    ``.pymolt/env_config.json`` says — see ``resolve_capture_env`` — so a
    baseline capture of a legacy project runs in its configured container
    instead of silently running against the local interpreter. Pass
    ``container`` explicitly to override.

    For ``mode == TEST_SUITE``, also computes a coverage-based gap summary (what fraction
    of the project's static dependency-usage sites the suite actually exercised — see
    ``_coverage_gap_summary``) and stamps it onto the slot's ``coverage_pct``/
    ``covered_sites``/``blind_sites``. Best-effort: degrades to ``None``/``0``/``0`` (logged,
    never raised) when ``coverage`` isn't installed or the second pass fails.

    A command that exits non-zero is refused as evidence unless ``accept_failing`` is set
    and the run recorded events; the slot is then stamped ``red`` (still ``valid``).

    Never prompts; raises ``ValueError`` on bad input (unknown ``when``, no
    ``command``, or an unsupported ``mode`` for this function — see
    ``start_attached_capture`` for ``LIVE_ATTACH``).

    During a Python-version move (setup's base and target differ in major.minor) the stdlib
    modules the project's stdlib sites touch are traced as dependencies
    (``PYMOLT_TRACE_STDLIB``, see ``stdlib_trace_modules``); ``stdlib="none"`` turns that off,
    an explicit comma list replaces it, and the caller's own variable wins. An explicit
    ``target`` (not ``"all"``) gets no automatic stdlib modules unless ``stdlib`` is given.
    What was traced is stamped on the slot.
    """
    if when not in ("baseline", "post_migration"):
        raise ValueError(f"when must be 'baseline' or 'post_migration', got {when!r}")
    if mode not in (CaptureMode.TEST_SUITE, CaptureMode.LIVE_COMMAND):
        raise ValueError(
            f"capture_named_trace handles TEST_SUITE/LIVE_COMMAND only, got {mode!r} "
            "— use start_attached_capture for LIVE_ATTACH"
        )
    if not command:
        raise ValueError("command is required for TEST_SUITE/LIVE_COMMAND capture")

    # Anchor everything to the project root: a relative project dir would otherwise put
    # stored paths (and the target env lookup) relative to whichever directory pymolt ran in.
    project_dir = Path(os.path.abspath(project_dir))
    if privacy is None:
        privacy = "values" if mode is CaptureMode.TEST_SUITE else "shape"
    sampling_source = "caller"
    if sample_rate is None:
        sample_rate, sampling_source = default_sample_rate(mode), "default"
    widen_targets = False
    if (
        impact_targets is None
        and when == "post_migration"
        and not os.environ.get("PYMOLT_TRACE_IMPACT_TARGETS")
    ):
        # The applied plan's symbols are armed from the start ON TOP of the usual
        # observation, never instead of it: the post-migration capture is diffed against a
        # full baseline, and a capture narrowed to the plan's symbols would report every
        # other baseline call as DISAPPEARED (and hide what nobody predicted).
        impact_targets = _applied_plan_impact_targets(project_dir)
        widen_targets = bool(impact_targets)
    normalized_targets = _normalize_impact_targets(impact_targets)
    # An explicit --target stays exactly what the caller named: the stdlib modules are added
    # automatically only for the default target ("all"), or when ``stdlib`` is given too.
    trace_stdlib = ([] if stdlib is None and target != "all"
                    else _resolve_stdlib_option(project_dir, when, stdlib))
    # Validate before creating staging paths or spawning the target.
    trace_env(
        target, backend, exclude, source, include_internal, privacy, sample_rate,
        normalized_targets, deployment_id, request_id, correlation_id,
    )

    # No explicit container from the caller -> default from the project's saved
    # EnvConfig (baseline -> its configured container if still running;
    # post-migration -> no container, just a note/prefill suggestion). An
    # explicit container passed by the caller always wins.
    env_note = None
    command_prefix = None
    if container is None:
        resolved = resolve_capture_env(project_dir, when)
        container = resolved["container"]
        command_prefix = resolved["command_prefix"]
        if workdir is None:
            workdir = resolved["workdir"]
        env_note = resolved["note"]

    command = _command_in_configured_env(list(command), command_prefix)

    # Local commands run in the target project by default (not pymolt's cwd), so
    # a relative command like `pytest tests/` collects the target's tests.
    if cwd is None and not container:
        cwd = project_dir
    runner_spec = _container_runner_spec(container)
    out_path = project_dir / ".pymolt" / "contract_traces" / f"{when}.jsonl"
    # Capture into a sibling first.  A subprocess can fail to start or time out;
    # archiving the current slot before that succeeds leaves contract_state
    # pointing at a missing trace.  A sibling also makes the final promotion an
    # atomic rename on the same filesystem.
    staged_path = out_path.with_name(
        f".{out_path.stem}-{uuid.uuid4().hex}.pending{out_path.suffix}"
    )
    staged_log = staged_path.parent / f"{staged_path.stem}.command.log"
    try:
        capture_kwargs = {
            "container": container,
            "workdir": workdir,
            "exclude": exclude,
            "source": source,
            "include_internal": include_internal,
            "cancel_event": cancel_event,
            "cwd": cwd,
            "privacy": privacy,
            "sample_rate": sample_rate,
        }
        # Keep compatibility with third-party/test capture_trace adapters that
        # predate P2: only send the new keyword arguments when actually used.
        if normalized_targets:
            capture_kwargs["impact_targets"] = normalized_targets
        if widen_targets:
            capture_kwargs["widen_targets"] = True
        if trace_stdlib:
            capture_kwargs["trace_stdlib"] = trace_stdlib
        if deployment_id is not None:
            capture_kwargs["deployment_id"] = deployment_id
        if request_id is not None:
            capture_kwargs["request_id"] = request_id
        if correlation_id is not None:
            capture_kwargs["correlation_id"] = correlation_id
        if mode is CaptureMode.LIVE_COMMAND:
            capture_kwargs.update(sink=LONG_RUN_SINK, trace_mode=LONG_RUN_TRACE_MODE)
            if sample_rate < 1:
                capture_kwargs["sample_first"] = LONG_RUN_SAMPLE_FIRST
        result = capture_trace(target, command, staged_path, backend, **capture_kwargs)
        _record_sampling(staged_path, sample_rate, sampling_source)
        _record_site_text(staged_path, project_dir, runner_spec)
        _record_sources_for(staged_path, project_dir, when, runner_spec)
    except BaseException:
        # A timeout/start failure may happen after the command log or a partial
        # trace was created.  They were never committed, so do not leave them
        # looking like a recoverable capture.
        for pending in (staged_path, staged_log, _trace_metadata_path(staged_path),
                        snapshot_path(staged_path)):
            try:
                pending.unlink()
            except OSError:
                pass
        raise

    # ``capture_trace`` normally creates this while merging per-process files.
    # Materialize an empty artifact as well: alternate backends and test doubles
    # are allowed to report a successful zero-event capture, and the persisted
    # slot must never point at a path that does not exist.
    if not staged_path.exists():
        staged_path.parent.mkdir(parents=True, exist_ok=True)
        staged_path.touch()
    validity, validity_reason = _capture_validity(
        result, staged_path, accept_failing=accept_failing)
    state = load_contract_state(project_dir)
    archived_previous = None

    if validity is not CaptureValidity.VALID:
        # Keep failed evidence for diagnosis, but never let it displace the last
        # valid baseline/post slot.  Its distinct path also makes accidental
        # auto-sourcing impossible.
        diagnostic = _diagnostic_path(project_dir, when)
        os.replace(staged_path, diagnostic)
        result.out_path = str(diagnostic)
        try:
            result.metadata_path = _move_trace_metadata(staged_path, diagnostic)
            result.facts_path = _move_facts_snapshot(staged_path, diagnostic)
            _move_sources(staged_path, diagnostic)
        except OSError as exc:
            logger.warning("could not move diagnostic trace metadata: %s", exc)
        if result.command_log:
            pending_log = Path(result.command_log)
            diagnostic_log = diagnostic.with_suffix(".command.log")
            if pending_log.is_file():
                os.replace(pending_log, diagnostic_log)
                result.command_log = str(diagnostic_log)
    else:
        # Displace only a fully valid recording. If promotion fails after the
        # archive move, restore the previous active artifact before propagating.
        previous = getattr(state, when, None)
        had_previous_trace = previous is not None and Path(previous.trace_path).is_file()
        archived_previous = archive_existing_capture(project_dir, when)
        if had_previous_trace and archived_previous is None:
            for pending in (staged_path, staged_log, snapshot_path(staged_path)):
                try:
                    pending.unlink()
                except OSError:
                    pass
            raise RuntimeError(f"could not safely replace existing {when} capture")
        try:
            os.replace(staged_path, out_path)
        except BaseException:
            if archived_previous is not None and not out_path.exists():
                shutil.move(archived_previous, out_path)
            raise
        result.out_path = str(out_path)
        try:
            result.metadata_path = _move_trace_metadata(staged_path, out_path)
            result.facts_path = _move_facts_snapshot(staged_path, out_path)
            _move_sources(staged_path, out_path)
        except OSError as exc:
            logger.warning("could not promote trace metadata: %s", exc)
        if result.command_log:
            pending_log = Path(result.command_log)
            final_log = out_path.parent / f"{out_path.stem}.command.log"
            if pending_log.is_file():
                os.replace(pending_log, final_log)
                result.command_log = str(final_log)
    coverage_pct: float | None = None
    covered_sites = blind_sites = 0
    if mode == CaptureMode.TEST_SUITE and validity is CaptureValidity.VALID:
        coverage_pct, covered_sites, blind_sites, coverage_notes = _coverage_gap_summary(
            project_dir, list(command),
        )
        for note in coverage_notes:
            logger.info("capture_named_trace(%s): %s", when, note)

    slot = ContractSlot(
        trace_path=result.out_path, captured_at=_now(), mode=mode,
        command=list(command), target=target, events=result.events, processes=result.processes,
        command_log=result.command_log, returncode=result.returncode, env_note=env_note,
        runner=runner_spec,
        coverage_pct=coverage_pct, covered_sites=covered_sites, blind_sites=blind_sites,
        env_fingerprint=environment_fingerprint(project_dir),
        env_setup_fingerprint=environment_setup_fingerprint(project_dir),
        archived_previous=archived_previous,
        validity=validity, validity_reason=validity_reason,
        privacy_profile=privacy, sample_rate=sample_rate,
        duration_seconds=result.duration_seconds,
        deployment_id=result.deployment_id,
        request_id=result.request_id,
        correlation_id=result.correlation_id,
        backend=result.backend,
        impact_targets=result.impact_targets,
        traced_stdlib=getattr(result, "trace_stdlib", None) or _effective_stdlib(trace_stdlib),
        metadata_path=result.metadata_path,
        sources_path=_existing(sources_path(result.out_path)),
        events_seen=result.events_seen,
        dropped_events=result.dropped_events,
        sampling_dropped=result.sampling_dropped,
        backpressure_dropped=result.backpressure_dropped,
        write_failures=result.write_failures,
        sink_failures=result.sink_failures,
        instrumentation_skipped=result.instrumentation_skipped,
        activation_failures=getattr(result, "activation_failures", []),
        red=_red_stamp(result) if validity is CaptureValidity.VALID else None,
    )
    if validity is CaptureValidity.VALID:
        setattr(state, when, slot)
    else:
        state.diagnostic_captures.append(slot)
    state.save(_state_path(project_dir))
    return slot


def archive_existing_capture(project_dir: str | Path, when: str) -> str | None:
    """Move the recording currently in ``when``'s slot aside; return where it went.

    A capture is a recording of a world that stops existing the moment you
    migrate — re-capturing a baseline after the fact is not merely inconvenient,
    it is impossible. So an overwrite never destroys: the old JSONL moves to
    ``.pymolt/contract_traces/archive/`` under the timestamp it was taken at, and
    the caller is told the path. Returns None when there was nothing to displace.
    """
    project_dir = Path(project_dir)
    existing = getattr(load_contract_state(project_dir), when, None)
    if existing is None:
        return None
    current = Path(existing.trace_path)
    if not current.is_file():
        return None

    archive_dir = project_dir / ".pymolt" / "contract_traces" / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    # The timestamp is the capture's own, not now(): the archived file is named
    # for the moment it describes.
    stamp = re.sub(r"[^0-9A-Za-z]", "-", existing.captured_at or _now())
    destination = archive_dir / f"{when}-{stamp}.jsonl"
    counter = 2
    while destination.exists():
        destination = archive_dir / f"{when}-{stamp}-{counter}.jsonl"
        counter += 1
    try:
        shutil.move(str(current), str(destination))
    except OSError as e:
        # Never let bookkeeping block a capture — but say so, loudly enough to log.
        logger.warning("could not archive previous %s capture: %s", when, e)
        return None
    current_metadata = _trace_metadata_path(current)
    if current_metadata.is_file():
        try:
            shutil.move(str(current_metadata), str(_trace_metadata_path(destination)))
        except OSError as exc:
            logger.warning("could not archive previous %s trace metadata: %s", when, exc)
    current_snapshot = snapshot_path(current)
    if current_snapshot.is_file():
        try:
            shutil.move(str(current_snapshot), str(snapshot_path(destination)))
        except OSError as exc:
            logger.warning("could not archive previous %s facts snapshot: %s", when, exc)
    _move_sources(current, destination)
    return str(destination)


def environment_fingerprint(project_dir: str | Path) -> str | None:
    """A short digest of the inputs that define *which world* a capture describes.

    Manifest contents plus the resolved-environment choices (base Python,
    toolset, container). Two captures with the same fingerprint were taken
    against the same declared world; a difference means the recording predates a
    change and may no longer describe this project.

    Returns None when there is nothing to fingerprint (no config, no manifest) —
    "unknown", which callers must not treat as "fresh".
    """
    inputs = _fingerprint_inputs(project_dir)
    if inputs is None:
        return None
    parts, content = inputs
    if content is not None:
        parts = [*parts, content]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def environment_setup_fingerprint(project_dir: str | Path) -> str | None:
    """``environment_fingerprint`` without the manifest's *contents*.

    Which manifest file, base Python, toolset and container a capture was taken
    against — the setup, as opposed to what the manifest currently pins. When only
    the contents of the same manifest moved since a baseline, that move *is* the
    migration (bumped pins), so the baseline is still its starting point; when the
    setup moved (another manifest, another base Python), it is not. None means
    unknown, as for the full fingerprint.
    """
    inputs = _fingerprint_inputs(project_dir)
    if inputs is None:
        return None
    parts, _content = inputs
    return hashlib.sha256("\n".join(["setup", *parts]).encode("utf-8")).hexdigest()[:16]


def _fingerprint_inputs(project_dir: str | Path) -> tuple[list[str], str | None] | None:
    """(setup parts, manifest-content part) of the environment fingerprint."""
    from pymolt.ingestion.config import EnvConfig

    project_dir = Path(project_dir)
    config = EnvConfig.load(project_dir / ".pymolt" / "env_config.json")
    if config is None:
        return None

    parts = [
        f"base={config.base_python or ''}",
        f"tool={config.selected_tool.value if config.selected_tool else ''}",
        f"container={config.container_id or ''}",
        f"manifest={config.selected_manifest or ''}",
    ]
    content: str | None = None
    if config.selected_manifest:
        manifest = project_dir / config.selected_manifest
        try:
            content = "content=" + hashlib.sha256(manifest.read_bytes()).hexdigest()
        except OSError:
            content = "content=<unreadable>"
    return parts, content


class AttachInstructions(BaseModel):
    """What to show the engineer for a LIVE_ATTACH capture: run this yourself."""

    command_hint: str   # a ready-to-copy `PYTHONPATH=... PYMOLT_TRACE_*=... <your command>` line
    out_path: str        # where the trace will accumulate as they exercise the app
    metadata_path: str | None = None


def pending_attach_path(project_dir: str | Path, when: str) -> Path:
    return Path(project_dir) / ".pymolt" / "contract_traces" / f".{when}.attach-pending.jsonl"


def start_attached_capture(
    project_dir: str | Path, when: str, *, target: str = "all", backend: str = "auto",
    privacy: str = "shape", sample_rate: float | None = None,
    impact_targets: list[str] | None = None,
    deployment_id: str | None = None,
    request_id: str | None = None,
    correlation_id: str | None = None,
) -> AttachInstructions:
    """Prepare (but do not launch) a LIVE_ATTACH capture: export the watcher bundle
    and pick a stable output path, returning the command the engineer runs THEMSELVES
    in their own terminal/devcontainer/server process."""
    if when not in ("baseline", "post_migration"):
        raise ValueError(f"when must be 'baseline' or 'post_migration', got {when!r}")

    project_dir = Path(os.path.abspath(project_dir))
    if sample_rate is None:
        sample_rate = default_sample_rate(CaptureMode.LIVE_ATTACH)
    watcher_env = trace_env(
        target, backend, "", "", False, privacy, sample_rate,
        impact_targets, deployment_id, request_id, correlation_id,
        sink=LONG_RUN_SINK, trace_mode=LONG_RUN_TRACE_MODE,
        trace_stdlib=_resolve_stdlib_option(project_dir, when, None),
    )
    bundle_dir = project_dir / ".pymolt" / "contract_bundle"
    out_path = pending_attach_path(project_dir, when)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        raise ValueError(
            f"an unfinished {when} attach capture already exists at {out_path}; "
            "collect it before starting another"
        )
    bundle = export_watcher(bundle_dir)
    if watcher_env.get("PYMOLT_TRACE_SINK") == "facts":
        # A facts sink writes only its snapshot (``.attach-pending.facts.json``); the pending
        # JSONL is what marks the capture as started (and what --collect looks for).
        out_path.touch()

    exported = {"PYTHONPATH": str(bundle), **watcher_env, "PYMOLT_TRACE_OUT": str(out_path)}
    hint = " ".join(f"{key}={shlex.quote(value)}" for key, value in exported.items())
    hint += " <your command>"
    return AttachInstructions(
        command_hint=hint,
        out_path=str(out_path),
        metadata_path=str(_trace_metadata_path(out_path)),
    )


def poll_attached_capture(out_path: str | Path) -> int:
    """Cheap line count on the (append-only) JSONL — how many events so far. A facts sink
    writes no events: its snapshot's recorded-call count is added."""
    path = Path(out_path)
    lines = 0
    if path.is_file():
        with path.open() as f:
            # Projected snapshot facts summarize calls the snapshot already counts.
            lines = sum(1 for line in f if line.strip() and _PROJECTED not in line)
    snap = find_facts_snapshot(path)
    if snap is None:
        return lines
    counters = read_snapshot_header(snap).get("counters") or {}
    sampled = counters.get("sampled")
    return lines + (sampled if isinstance(sampled, int) and sampled > 0 else 0)


_PROJECTED = '"src": "facts"'  # how project_snapshot marks its lines


SOURCES_SCHEMA = "pymolt.sources/1"
SOURCES_MAX_BYTES = 1_000_000
SOURCES_TOTAL_BUDGET = 32_000_000  # raw bytes of text in one snapshot's whole-tree part
_NOT_PROJECT_DIRS = frozenset({"site-packages", ".pymolt", ".venv", "venv", ".tox", ".git",
                               "node_modules", "__pycache__"})


def _existing(path: Path) -> str | None:
    return str(path) if path.is_file() else None


def sources_path(trace_path: str | Path) -> Path:
    """Where a capture keeps its source snapshot: ``<trace>.sources.json.gz``."""
    return Path(str(trace_path) + ".sources.json.gz")


def _move_sources(source: str | Path, destination: str | Path) -> None:
    src, dst = sources_path(source), sources_path(destination)
    if not src.is_file():
        return
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dst)
    except OSError as exc:
        logger.warning("could not move the source snapshot %s: %s", src, exc)


def _read_snapshot(path: str | Path | None) -> dict | None:
    if not path:
        return None
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) and isinstance(data.get("files"), dict) else None
    except (OSError, ValueError):
        return None


def record_sources(
    trace_path: str | Path, project_dir: str | Path, runner=None, *,
    extra_files: Iterable[str] = (), whole_tree: bool = False,
) -> str | None:
    """Snapshot the text and sha256 of project files, gzipped next to the trace.

    The files are those with a fact in the trace (``fact_files``), plus ``extra_files``
    (a post-migration capture passes the baseline's, so a file whose calls all vanished, or
    that only gained calls, has text on both sides), plus, with ``whole_tree`` (a baseline),
    every other project ``.py`` file under the root up to ``SOURCES_TOTAL_BUDGET`` bytes
    (facts first, then the rest smallest first; what was left out is recorded). Files are read
    on the host after the run (a container path maps through ``to_host``) and keyed by the
    project-relative path. Over ``SOURCES_MAX_BYTES``, unreadable, outside the project, or
    inside a virtualenv / site-packages: skipped (the last two silently). A file in neither
    snapshot stays unknown. Format::

        {"schema": "pymolt.sources/1", "root": ..., "fact_files": [...],
         "files": {"app/x.py": {"sha256": ..., "text": ...}},
         "skipped": [{"file": ..., "reason": ...}], "left_out": [...], "left_out_total": N}

    Returns the sidecar path, or None when it could not be written (never raises)."""
    root = os.path.realpath(str(project_dir))
    seen: set[str] = set()
    try:
        for rec in iter_trace_records(trace_path):
            where = rec.get("where")
            file = where.get("file") if isinstance(where, dict) else None
            if isinstance(file, str) and file:
                seen.add(file)
    except OSError:
        return None

    def relative(file: str) -> tuple[str, str] | None:
        path = file if os.path.isabs(file) else os.path.join(root, file)
        if runner is not None and os.path.isabs(file):
            real = os.path.realpath(path)
            if real != root and not real.startswith(root + os.sep):
                path = to_host(runner, file) or path
        real = os.path.realpath(path)
        if real != root and not real.startswith(root + os.sep):
            return None                     # not a project file: the runtime's or a dependency's
        rel = Path(os.path.relpath(real, root)).as_posix()
        if _NOT_PROJECT_DIRS & set(rel.split("/")[:-1]):
            return None
        return rel, real

    fact_files: dict[str, str] = {}
    for file in sorted(seen):
        found = relative(file)
        if found:
            fact_files[found[0]] = found[1]
    wanted = dict(fact_files)
    for file in sorted(extra_files):
        found = relative(file)
        if found:
            wanted.setdefault(found[0], found[1])
    rest: list[tuple[int, str, str]] = []
    if whole_tree:
        from pymolt.verify.contact_map import _source_files

        for path in _source_files(Path(root))[0]:
            found = relative(str(path))
            if found and found[0] not in wanted:
                try:
                    rest.append((os.path.getsize(found[1]), found[0], found[1]))
                except OSError:
                    continue
        rest.sort()

    files: dict[str, dict[str, str]] = {}
    skipped: list[dict[str, str]] = []
    left_out: list[str] = []
    total = 0

    def take(rel: str, real: str, budgeted: bool) -> None:
        nonlocal total
        try:
            raw = Path(real).read_bytes()
        except OSError as exc:
            skipped.append({"file": rel, "reason": f"unreadable: {exc.__class__.__name__}"})
            return
        if len(raw) > SOURCES_MAX_BYTES:
            skipped.append({"file": rel, "reason": f"over {SOURCES_MAX_BYTES} bytes ({len(raw)})"})
            return
        if budgeted and total + len(raw) > SOURCES_TOTAL_BUDGET:
            left_out.append(rel)
            return
        total += len(raw)
        files[rel] = {"sha256": hashlib.sha256(raw).hexdigest(),
                      "text": raw.decode("utf-8", errors="replace")}

    for rel, real in wanted.items():
        take(rel, real, False)
    for _size, rel, real in rest:
        take(rel, real, True)
    out = sources_path(trace_path)
    pending = out.with_name(f".{out.name}.{uuid.uuid4().hex}.pending")
    try:
        with gzip.open(pending, "wt", encoding="utf-8", compresslevel=6) as fh:
            json.dump({"schema": SOURCES_SCHEMA, "root": root,
                       "fact_files": sorted(fact_files), "files": files, "skipped": skipped,
                       "left_out": left_out[:500], "left_out_total": len(left_out)}, fh)
        os.replace(pending, out)
    except OSError as exc:
        logger.warning("could not write the source snapshot for %s: %s", trace_path, exc)
        try:
            pending.unlink()
        except OSError:
            pass
        return None
    return str(out)


def load_sources(path: str | Path | None) -> dict[str, str] | None:
    """``{relative path: text}`` from a source snapshot; None when absent or unreadable
    (an older capture: callers fall back to line-text rules). A file not in the result is
    unknown, not empty."""
    data = _read_snapshot(path)
    if data is None:
        return None
    try:
        return {k: v["text"] for k, v in data["files"].items()}
    except (KeyError, TypeError):
        return None


def _record_sources_for(trace_path: str | Path, project_dir: str | Path, when: str,
                        runner=None) -> None:
    """The snapshot of one slot: a baseline stores the whole tree (within budget); a
    post-migration capture stores its own fact files plus the baseline's, so every file that
    had facts on either side has text on both."""
    try:
        if when == "baseline":
            record_sources(trace_path, project_dir, runner, whole_tree=True)
        else:
            record_sources(trace_path, project_dir, runner,
                           extra_files=_baseline_fact_files(project_dir))
    except Exception as exc:  # noqa: BLE001 - never fail a capture over provenance
        logger.warning("could not record the source snapshot for %s: %s", trace_path, exc)


def _baseline_fact_files(project_dir: str | Path) -> list[str]:
    """The files that had facts in the baseline capture (from its snapshot; [] if none)."""
    baseline = load_contract_state(project_dir).baseline
    data = _read_snapshot(baseline.sources_path) if baseline is not None else None
    return list(data.get("fact_files") or ()) if data else []


def _record_site_text(trace_path: str | Path, project_dir: str | Path, runner=None) -> None:
    """Keep the source text of every call site with the trace.

    The project's code changes between the baseline and the post-migration capture, so
    the line text has to be read now: it is what lets the fact diff tell a rewritten call
    site (``s1.append(s2)`` -> ``pd.concat([s1, s2])``) from a call that vanished. Best
    effort — without it the diff falls back to line numbers.
    """
    try:
        record_site_text(trace_path, project_dir, runner)
    except Exception as exc:  # noqa: BLE001 - never fail a capture over annotation
        logger.warning("could not record call-site text for %s: %s", trace_path, exc)


def _collect_attached_snapshot(out_path: Path) -> None:
    """Normalize an attach capture's runtime snapshot (``X.facts.json`` next to ``X.jsonl``)
    into the host form ``X.jsonl.facts.json`` and project it into the JSONL, as
    ``capture_trace`` does for a command it ran."""
    target = snapshot_path(out_path)
    raw = find_facts_snapshot(out_path)
    stem = out_path.name[:-len(".jsonl")] if out_path.name.endswith(".jsonl") else out_path.name
    # Forked workers of an attached server write ``<stem>.<pid>.facts.json`` (no {pid} in an
    # attach path), so they never overwrite the parent's snapshot.
    raws = [] if raw is None or raw == target else [raw]
    raws += [Path(p) for p in sorted(glob.glob(str(out_path.with_name(f"{stem}.*.facts.json"))))
             if Path(p) != target and Path(p) not in raws]
    if not raws:
        return
    try:
        if merge_fact_snapshots(raws, target) is None:
            return
        for path in raws:
            path.unlink()
        if not out_path.exists():
            out_path.touch()
        project_snapshot(target, out_path)
    except OSError as exc:
        logger.warning("could not collect the facts snapshot of %s: %s", out_path, exc)


def finalize_attached_capture(
    project_dir: str | Path, when: str, out_path: str | Path, *,
    target: str = "all", privacy: str = "shape", sample_rate: float | None = None,
    impact_targets: list[str] | None = None,
    deployment_id: str | None = None,
    request_id: str | None = None,
    correlation_id: str | None = None,
) -> ContractSlot:
    """Called when the engineer signals "I'm done" on a LIVE_ATTACH capture:
    reads whatever landed on disk and persists it as the named slot.

    The slot's sample rate is the one the watcher ran with (its sidecar), else
    ``sample_rate``, else the attach default."""
    project_dir = Path(os.path.abspath(project_dir))
    out_path = Path(out_path)
    _collect_attached_snapshot(out_path)
    events = poll_attached_capture(out_path)
    _record_site_text(out_path, project_dir)
    _record_sources_for(out_path, project_dir, when)
    metadata = _read_trace_metadata(out_path)
    ran_with = metadata.get("sample_rate")
    if isinstance(ran_with, int | float) and not isinstance(ran_with, bool) and 0 < ran_with <= 1:
        sample_rate = float(ran_with)
    elif sample_rate is None:
        sample_rate = default_sample_rate(CaptureMode.LIVE_ATTACH)
    raw_counters = metadata.get("counters")
    counters = raw_counters if isinstance(raw_counters, dict) else {}
    runtime_metadata_complete = metadata.get("runtime_metadata_complete")
    if runtime_metadata_complete is None:
        runtime_metadata_complete = _runtime_counters_complete(counters)
    else:
        runtime_metadata_complete = (
            runtime_metadata_complete is True
            and _runtime_counters_complete(counters)
        )
    normalized_targets = _normalize_impact_targets(impact_targets)
    if not normalized_targets and isinstance(metadata.get("impact_targets"), list):
        normalized_targets = _normalize_impact_targets(metadata["impact_targets"])
    deployment_id = (
        _capture_identity(deployment_id, "PYMOLT_TRACE_DEPLOYMENT_ID")
        or metadata.get("deployment_id")
    )
    request_id = (
        _capture_identity(request_id, "PYMOLT_TRACE_REQUEST_ID")
        or metadata.get("request_id")
    )
    correlation_id = (
        _capture_identity(correlation_id, "PYMOLT_TRACE_CORRELATION_ID")
        or metadata.get("correlation_id")
    )
    dropped_events = (
        sum(counters[name] for name in _DROP_COUNTERS)
        if runtime_metadata_complete else None
    )
    events_seen = counters.get("events_seen") if runtime_metadata_complete else None
    quality = inspect_trace_artifact(out_path)
    validity = quality.validity
    validity_reason = quality.reason
    state = load_contract_state(project_dir)
    archived_previous = None
    committed_path = out_path
    if validity is CaptureValidity.VALID:
        final_path = project_dir / ".pymolt" / "contract_traces" / f"{when}.jsonl"
        previous = getattr(state, when, None)
        had_previous_trace = previous is not None and Path(previous.trace_path).is_file()
        archived_previous = archive_existing_capture(project_dir, when)
        if had_previous_trace and archived_previous is None:
            raise RuntimeError(f"could not safely replace existing {when} capture")
        try:
            os.replace(out_path, final_path)
        except BaseException:
            if archived_previous is not None and not final_path.exists():
                shutil.move(archived_previous, final_path)
            raise
        try:
            metadata_path = _move_trace_metadata(out_path, final_path)
            _move_facts_snapshot(out_path, final_path)
            _move_sources(out_path, final_path)
        except OSError as exc:
            logger.warning("could not promote attached trace metadata: %s", exc)
            metadata_path = None
        committed_path = final_path
    else:
        diagnostic = _diagnostic_path(project_dir, when)
        if out_path.exists():
            os.replace(out_path, diagnostic)
        try:
            metadata_path = _move_trace_metadata(out_path, diagnostic)
            _move_facts_snapshot(out_path, diagnostic)
            _move_sources(out_path, diagnostic)
        except OSError as exc:
            logger.warning("could not move attached diagnostic metadata: %s", exc)
            metadata_path = None
        committed_path = diagnostic
    slot = ContractSlot(
        trace_path=str(committed_path), captured_at=_now(), mode=CaptureMode.LIVE_ATTACH,
        command=[], target=target, events=events, processes=1,
        env_fingerprint=environment_fingerprint(project_dir),
        env_setup_fingerprint=environment_setup_fingerprint(project_dir),
        archived_previous=archived_previous,
        validity=validity, validity_reason=validity_reason,
        privacy_profile=privacy, sample_rate=sample_rate,
        duration_seconds=metadata.get("duration_seconds"),
        deployment_id=deployment_id,
        request_id=request_id,
        correlation_id=correlation_id,
        backend=metadata.get("backend"),
        impact_targets=normalized_targets,
        metadata_path=metadata_path,
        sources_path=_existing(sources_path(committed_path)),
        events_seen=events_seen,
        dropped_events=dropped_events,
        sampling_dropped=(
            counters.get("sampling_dropped") if runtime_metadata_complete else None
        ),
        backpressure_dropped=(
            counters.get("backpressure_dropped") if runtime_metadata_complete else None
        ),
        write_failures=(
            counters.get("write_failures") if runtime_metadata_complete else None
        ),
        sink_failures=(
            counters.get("sink_failures") if runtime_metadata_complete else None
        ),
        instrumentation_skipped=metadata.get("instrumentation_skipped", []),
    )
    if validity is CaptureValidity.VALID:
        setattr(state, when, slot)
    else:
        state.diagnostic_captures.append(slot)
    state.save(_state_path(project_dir))
    return slot


def build_contract_report_from_state(
    project_dir: str | Path, *,
    trace_override: str | None = None, against_override: str | None = None,
    probe_python: str | None = None,
    changed_api_paths=None,
    comparator_profile: str = "exact",
    custom_comparator=None,
) -> ContractReport:
    """The unified report, sourcing trace/against from the persisted state when
    not explicitly overridden.

    Resolution: explicit overrides always win. Otherwise — both slots captured
    -> trace=post_migration, against=baseline (this is what answers "what
    changed across the migration"); only one captured -> that one as trace,
    no diff; neither -> trace=None (today's all-BLIND behavior, unchanged).
    """
    state = load_contract_state(project_dir)

    if changed_api_paths is None:
        from pymolt.migration_plan import MigrationPlan
        from pymolt.migration_state import MigrationReceipt

        receipt = MigrationReceipt.load(project_dir)
        if receipt is not None and receipt.status == "applied" and receipt.plan_path:
            applied_plan = MigrationPlan.load(project_dir, receipt.plan_path)
            if applied_plan is not None and applied_plan.impacts:
                changed_api_paths = applied_plan.impacts

    def _slot_trace(slot) -> str | None:
        """A slot's trace, resolved against the project.

        Slots have been written with both absolute and project-relative paths.
        A relative one resolved against the *process* CWD points nowhere as
        soon as pymolt is run from anywhere but the project root — which
        silently cost the before/after diff, the one thing the report exists
        for. Anchor it to the project instead.
        """
        if slot is None or not slot.trace_path:
            return None
        path = Path(slot.trace_path)
        return str(path if path.is_absolute() else Path(project_dir) / path)

    trace = trace_override
    against = against_override
    used_slots: list[tuple[str, object]] = []
    if trace is None:
        if state.post_migration is not None:
            trace = _slot_trace(state.post_migration)
            used_slots.append(("post-migration", state.post_migration))
            if against is None and state.baseline is not None:
                against = _slot_trace(state.baseline)
                used_slots.append(("baseline", state.baseline))
        elif state.baseline is not None:
            trace = _slot_trace(state.baseline)
            used_slots.append(("baseline", state.baseline))

    predictions = None
    if trace and against:
        try:
            predictions = _predictions_to_calibrate(project_dir, state, against)
        except Exception as exc:  # noqa: BLE001 - calibration is a report section, not a gate
            logger.warning("could not load the predicted risks: %s", exc)
            predictions = {"risks": [], "source": "baseline", "plan_id": None,
                           "notes": [f"predicted risks unavailable: {exc}"]}

    report = build_contract_report(
        project_dir,
        trace=trace,
        against=against,
        probe_python=probe_python,
        changed_api_paths=changed_api_paths,
        comparator_profile=comparator_profile,
        custom_comparator=custom_comparator,
        predictions=predictions,
        diff_context=build_diff_context(project_dir, trace, against) if trace and against
        else None,
    )
    _stamp_staleness(project_dir, report, used_slots)
    _stamp_verdict(report, trace, against, used_slots)
    return report


def build_diff_context(project_dir: str | Path, trace: str | Path, against: str | Path):
    """What the fact diff needs to judge an edit by its provenance (``facts.DiffContext``).

    Snapshots come from the two captures' ``.sources.json`` (None for an older capture: the
    diff then falls back to line-text rules); the apply ledger from ``.pymolt/apply_ledger.json``;
    the static contact maps are built from the snapshots; ``successors`` (old -> new symbol)
    from the applied plan's impacts, its risks' delta facts and the ledger; and
    ``type_relocations`` from the plan when it carries them. Never raises: a part that
    cannot be built is left None."""
    from pymolt.apply_ledger import ledger_path, load_ledger
    from pymolt.verify.contact_map import contacts_by_line
    from pymolt.verify.facts import DiffContext

    root = Path(os.path.abspath(project_dir))
    ctx = DiffContext()
    ctx.old_sources = load_sources(sources_path(against))
    ctx.new_sources = load_sources(sources_path(trace))
    if ledger_path(root).is_file():
        ctx.edits = load_ledger(root)
    for side, sources in (("old", ctx.old_sources), ("new", ctx.new_sources)):
        if sources:
            try:
                setattr(ctx, f"{side}_contacts", contacts_by_line(sources, root))
            except Exception as exc:  # noqa: BLE001 - the diff degrades without contacts
                logger.warning("could not build the %s contact map: %s", side, exc)
    successors: dict[str, str] = {}
    for entry in ctx.edits or ():
        if entry.get("old_symbol") and entry.get("new_symbol"):
            successors.setdefault(entry["old_symbol"], entry["new_symbol"])
    try:
        from pymolt.migration_plan import MigrationPlan
        from pymolt.migration_state import MigrationReceipt

        receipt = MigrationReceipt.load(root)
        plan = (MigrationPlan.load(root, receipt.plan_path)
                if receipt is not None and receipt.status == "applied" and receipt.plan_path
                else None)
        if plan is not None:
            for impact in plan.impacts:
                if impact.replacement_path:
                    successors.setdefault(impact.path, impact.replacement_path)
            for risk in plan.risks.risks if plan.risks is not None else ():
                for fact in [risk.fact, *risk.facts]:
                    if fact and fact.get("symbol") and fact.get("successor"):
                        successors.setdefault(fact["symbol"], fact["successor"])
            relocations = getattr(plan, "type_relocations", None)
            if relocations:
                ctx.type_relocations = _relocation_map(relocations)
    except Exception as exc:  # noqa: BLE001 - provenance is best effort
        logger.warning("could not read the applied plan for the diff context: %s", exc)
    ctx.successors = successors or None
    return ctx


def _relocation_map(relocations: object) -> dict[str, str] | None:
    """The plan's ``type_relocations`` ([{"old", "new", "via"}, ...], as dicts or models) as an
    old type -> new type map. An entry without both names is skipped."""
    if isinstance(relocations, Mapping):
        return {str(k): str(v) for k, v in relocations.items()} or None
    out: dict[str, str] = {}
    for item in relocations if isinstance(relocations, Iterable) else ():
        old = item.get("old") if isinstance(item, Mapping) else getattr(item, "old", None)
        new = item.get("new") if isinstance(item, Mapping) else getattr(item, "new", None)
        if old and new:
            out[str(old)] = str(new)
    return out or None


def _predictions_to_calibrate(project_dir: str | Path, state, against: str) -> dict:
    """The predicted risks verify checks against the fact diff.

    The plan's stored predictions when the comparison's baseline side is the baseline that
    plan was made from (the applied plan first, then the saved one); otherwise predictions
    from the baseline side's own runtime warnings (no delta facts), marked ``source:
    "baseline"`` and made by the report from the facts it loads anyway (``risks: None``).
    Fixed-by-plan comes from the plan's changes either way."""
    from pymolt.migration_plan import MigrationPlan
    from pymolt.migration_state import MigrationReceipt

    root = Path(project_dir)
    notes: list[str] = []
    baseline = state.baseline
    is_baseline = False
    if baseline is not None and baseline.trace_path:
        slot = Path(baseline.trace_path)
        slot = slot if slot.is_absolute() else root / slot
        is_baseline = slot.resolve() == Path(against).resolve()
    candidates = []
    receipt = MigrationReceipt.load(root)
    if receipt is not None and receipt.status == "applied" and receipt.plan_path:
        candidates.append(MigrationPlan.load(root, receipt.plan_path))
    candidates.append(MigrationPlan.load(root))
    bound = [plan for plan in candidates if plan is not None and is_baseline
             and plan.baseline_trace_path == baseline.trace_path
             and plan.baseline_captured_at == baseline.captured_at]
    for plan in bound:
        if plan.risks is not None and plan.risks.status != "unavailable":
            return {"risks": plan.risks.risks, "source": "plan", "plan_id": plan.plan_id,
                    "notes": list(plan.risks.notes)}
    if any(plan is not None and plan.risks is not None for plan in candidates) and not bound:
        notes.append("the saved plan's predictions were made from another baseline capture: "
                     "not used")
    notes.append("no plan predictions for this baseline: checking the predictions its own "
                 "runtime warnings make (no delta facts)")
    # ``risks: None``: the report predicts from the baseline facts it loads for the diff.
    return {"risks": None, "source": "baseline", "changes": bound[0].changes if bound else (),
            "plan_id": bound[0].plan_id if bound else None, "notes": notes}


def _missing_side_reason(trace: str | None, used_slots: list[tuple[str, object]]) -> str:
    """Why there is nothing to diff against, naming the side that is actually missing."""
    names = {name for name, _ in used_slots}
    if names == {"baseline"}:
        return "no post-migration capture yet (only the baseline is recorded)"
    if names == {"post-migration"}:
        return "no baseline capture"
    if not names and trace is None:
        return "no captures"
    return "no baseline trace is available for a version diff"


def _stamp_verdict(
    report: ContractReport,
    trace: str | None,
    against: str | None,
    used_slots: list[tuple[str, object]],
) -> None:
    """Fold evidence into PASS/FAIL/INCONCLUSIVE without averaging uncertainty."""
    # The verdict's FAIL evidence is the WHOLE surface (``all_*``): an impact filter is a focus
    # for the report, not a limit on what can fail it, or a change nobody predicted could
    # never be found. Coverage (BLIND, dynamic-only, trust, static targets) stays scoped to the
    # impacted surface below: "what the migration touches was exercised" is the meaningful
    # claim there, and the whole-surface BLIND count would drown it.
    whole_diff = report.all_fact_diff or report.fact_diff
    whole_clean = report.all_diff_clean if report.all_diff_clean is not None \
        else report.diff_clean
    whole_counts = report.all_diff if report.all_diff is not None else report.diff
    for warning in (whole_diff or {}).get("warnings") or ():
        # Snapshot caps that lower the comparison level without blocking PASS.
        if str(warning).startswith(("widened:", "outcomes-capped:", "unsampled:")):
            if warning not in report.notes:
                report.notes.append(str(warning))
    comparability: list[str] = []
    if trace is None:
        comparability.append("no current trace is available")
    else:
        quality = inspect_trace_artifact(trace)
        if quality.validity is not CaptureValidity.VALID:
            comparability.append(
                quality.reason or f"current trace is {quality.validity.value}"
            )
    if against is None:
        comparability.append(_missing_side_reason(trace, used_slots))
    else:
        quality = inspect_trace_artifact(against)
        if quality.validity is not CaptureValidity.VALID:
            comparability.append(
                quality.reason or f"baseline trace is {quality.validity.value}"
            )

    slots = {name: slot for name, slot in used_slots}
    for name, slot in used_slots:
        validity = getattr(slot, "validity", CaptureValidity.UNKNOWN)
        if validity is not CaptureValidity.VALID:
            comparability.append(f"{name} capture validity is {validity.value}")
        red = getattr(slot, "red", None)
        if red:
            failed = red.get("failed")
            report.notes.append(
                f"RED: the {name} capture was taken with "
                f"{failed if failed is not None else 'some'} failing test(s) "
                f"(exit {red.get('returncode')}); the calls from failing tests are compared "
                "as usual")
        dropped = getattr(slot, "dropped_events", None)
        if dropped is None:
            comparability.append(f"{name} capture has no event-loss counters")
        elif dropped:
            comparability.append(f"{name} capture dropped {dropped} event(s)")
        skipped = getattr(slot, "instrumentation_skipped", [])
        if skipped:
            comparability.append(
                f"{name} capture skipped {len(skipped)} instrumentation target(s)"
            )
        failed = getattr(slot, "activation_failures", [])
        if failed:
            comparability.append(
                f"{name} capture: the watcher failed to activate in {len(failed)} "
                "process(es), which ran untraced"
            )
    baseline = slots.get("baseline")
    post = slots.get("post-migration")
    if baseline is not None and post is not None:
        def _workload(command: list[str]) -> list[str]:
            if not command:
                return []
            executable = Path(command[0]).name
            if executable.lower().startswith("python") and command[1:2] == ["-m"]:
                return command[2:]
            return [executable, *command[1:]]

        if _workload(baseline.command) != _workload(post.command):
            comparability.append("baseline and post-migration used different commands")
        if baseline.target != post.target:
            comparability.append("baseline and post-migration traced different targets")
        if baseline.mode != post.mode:
            comparability.append("baseline and post-migration used different capture modes")
        if baseline.privacy_profile != post.privacy_profile:
            comparability.append("baseline and post-migration used different privacy profiles")
        if baseline.sample_rate != post.sample_rate:
            comparability.append("baseline and post-migration used different sample rates")
        if (baseline.sample_rate < 1 or post.sample_rate < 1) and not _tallies_complete(
                whole_diff):
            comparability.append(
                "sampled captures cannot prove that unmatched interactions disappeared"
            )

    if report.baseline_stale is True:
        comparability.append("capture evidence is stale")
    elif used_slots and report.baseline_stale is None:
        comparability.append("capture freshness could not be established")

    # A negative oracle is authoritative only over a valid, comparable pair.
    # Changed output from stale or mismatched workloads is evidence to inspect,
    # but it cannot honestly describe the current migration.
    if comparability:
        report.verdict = VerificationVerdict.INCONCLUSIVE
        report.verdict_reasons = list(dict.fromkeys(comparability))
        return
    if whole_clean is False or report.probe_changed:
        report.verdict = VerificationVerdict.FAIL
        if whole_clean is False:
            report.verdict_reasons.append("behavioral boundary changes were detected")
            if report.unpredicted_changes:
                n = len(report.unpredicted_changes)
                names = ", ".join(sorted({str(e.get("symbol")) for e in
                                          report.unpredicted_changes})[:5])
                report.verdict_reasons.append(
                    f"{n} behaviour change(s) outside the plan's predicted impacts: {names}")
        if report.probe_changed:
            report.verdict_reasons.append("sandbox replay changed behavior")
        return

    reasons: list[str] = []
    if whole_counts is None:
        reasons.append("no before/after diff was produced")
    elif whole_counts.get("skipped_opaque", 0):
        reasons.append("some boundary interactions were opaque or nondeterministic")
    reasons.extend(_fact_diff_reasons(whole_diff))
    if report.blind:
        reasons.append(f"{report.blind} static dependency symbol(s) were not exercised")
    if report.dynamic_only:
        unverified = _unverified_dynamic_only(report)
        if unverified:
            reasons.append(
                f"{len(unverified)} observed symbol(s) are missing from the static map"
                + (" and were not compared equal on both sides" if report.fact_diff else "")
            )
        else:
            report.notes.append(
                f"{report.dynamic_only} observed symbol(s) are missing from the static map but "
                "every one of their call facts compared equal on both sides: a static "
                "under-approximation, not an unverified contact"
            )
    if report.static_targets == 0:
        reasons.append("the static contract surface is empty")

    # Keep output stable and readable when two checks identify the same cause.
    report.verdict_reasons = list(dict.fromkeys(reasons))
    report.verdict = (
        VerificationVerdict.INCONCLUSIVE if report.verdict_reasons
        else VerificationVerdict.PASS
    )


def _tallies_complete(fact_diff: dict | None) -> bool:
    """Both sides' snapshots counted every call (sampled or not): presence and absence of a
    call site are then proven by the tallies, so value sampling does not weaken them."""
    if not fact_diff:
        return False
    return all(isinstance(fact_diff.get(side), dict)
               and fact_diff[side].get("tallies_complete") is True
               for side in ("old", "new"))


def _unverified_dynamic_only(report: ContractReport) -> list[str]:
    """Dynamic-only symbols that still block PASS.

    A symbol the static map missed but the trace observed is only a coverage gap if its
    behaviour was not verified. With a fact diff, those whose every fact compared equal on
    both sides are verified; without one (custom comparator, single trace) none are.
    """
    visible = report.impacted_symbols if report.impact_filter_active else report.symbols
    dynamic = [s.target for s in visible if s.status == "dynamic-only"]
    if report.fact_diff is None:
        return dynamic
    verified = set(report.fact_equal_symbols)
    return [target for target in dynamic if target not in verified]


def _fact_diff_reasons(fact_diff: dict | None) -> list[str]:
    """Why a clean fact diff still cannot prove equivalence (empty when it can)."""
    if not fact_diff or fact_diff.get("verdict") != VerificationVerdict.INCONCLUSIVE.value:
        return []
    entries = fact_diff.get("entries") or []
    counts = fact_diff.get("counts") or {}
    reasons: list[str] = []
    for side, label in (("old", "baseline"), ("new", "post-migration")):
        summary = fact_diff.get(side) or {}
        evicted = summary.get("evicted") if isinstance(summary, dict) else None
        if isinstance(evicted, int) and evicted > 0:
            reasons.append(
                f"the {label} facts snapshot reached its fact cap and evicted {evicted} "
                "call(s): the evidence is incomplete"
            )
    if not any(counts.values()):
        reasons.append("no comparable call facts were recorded on both sides")
    # An unverified edited site is counted in the summary but never blocks PASS; a review
    # item blocks with its own reason (it is INCONCLUSIVE, not FAIL).
    entries = [e for e in entries if not e.get("unverified")]
    reviews = [e for e in entries if e.get("review")]
    if reviews:
        names = ", ".join(sorted({str(e.get("symbol")) for e in reviews})[:5])
        reasons.append(f"{len(reviews)} edited call(s) need review: {names}")
    entries = [e for e in entries if not e.get("review")]
    unpaired = sum(
        1 for e in entries
        if e.get("category") == "rewritten" and e.get("comparison") in ("missing", "uncomparable")
    )
    if unpaired:
        reasons.append(
            f"{unpaired} rewritten call site(s) could not be paired with a post-migration call"
        )
    evicted_entries = sum(
        1 for e in entries
        if any(str(n).startswith("possibly-evicted") for n in e.get("notes") or ())
    )
    uncomparable = sum(1 for e in entries if e.get("category") == "uncomparable") \
        - evicted_entries
    if evicted_entries:
        reasons.append(
            f"{evicted_entries} call fact(s) are missing on a side that evicted facts and "
            "cannot be compared"
        )
    if uncomparable:
        reasons.append(
            f"{uncomparable} call fact(s) lost their outcome distribution and cannot be compared"
        )
    return reasons or ["the fact diff could not establish equivalence"]



def baseline_setup_unchanged(slot, current_setup: str | None) -> bool:
    """True when the baseline's setup (manifest file, base Python, toolset, container)
    is the current one, so any fingerprint difference is the manifest's contents.
    Unknown on either side (old slots, no config) is never "unchanged"."""
    recorded = getattr(slot, "env_setup_fingerprint", None)
    return recorded is not None and current_setup is not None and recorded == current_setup


def _applied_migration(project_dir: str | Path):
    """The receipt of the migration currently applied, or None (none, or rolled back)."""
    from pymolt.migration_state import MigrationReceipt

    receipt = MigrationReceipt.load(project_dir)
    if receipt is None or receipt.status != "applied":
        return None
    return receipt


def _stamp_staleness(project_dir: str | Path, report: ContractReport, used_slots) -> None:
    """Mark the report when a capture does not describe the world it stands for.

    A verdict built on a capture taken against a different manifest still *looks*
    authoritative — same tables, same percentages — which is exactly why it has
    to say so itself. Each capture is judged against its own world:

    * the current-side trace (post-migration, or a lone baseline) against the
      project as it is on disk now;
    * a baseline on the "before" side of a diff against the environment the
      recorded migration started from. A migration that upgrades the manifest
      moves "now" away from the baseline by design — that is the migration, not
      staleness. A baseline taken against some *other* starting point (setup
      re-run with another manifest before migrating) is still stale. With no
      recorded start (a migration done by hand, without ``pymolt apply``), a
      baseline whose *setup* (manifest file, base Python, toolset, container) is
      unchanged and only the manifest's contents moved is the starting point too:
      bumping pins is the migration.

    Absence of evidence is reported as unknown, never as fresh: a capture from
    before fingerprints existed, or a baseline whose starting point no recorded
    migration pins down, has not been vouched for.
    """
    if not used_slots:
        return
    current = environment_fingerprint(project_dir)
    if current is None:
        report.notes.append(
            "staleness unchecked: no saved config to fingerprint the captures against "
            "(run `pymolt setup` so future captures can be verified)."
        )
        return

    names = {name for name, _ in used_slots}
    run: str | None = None  # the applied migration's short id, when there is one
    start: str | None = None
    if "baseline" in names and "post-migration" in names:
        migration = _applied_migration(project_dir)
        if migration is not None:
            run = migration.run_id[:8]
            start = migration.starting_fingerprint(project_dir)

    current_setup = environment_setup_fingerprint(project_dir)
    stale_now: list[str] = []
    stale_start: list[str] = []
    unstamped: list[str] = []
    unpinned: list[str] = []
    content_moved: list[str] = []
    for name, slot in used_slots:
        fingerprint = getattr(slot, "env_fingerprint", None)
        if fingerprint is None:
            unstamped.append(name)
        elif name == "baseline" and "post-migration" in names:
            if start is not None:
                if fingerprint != start:
                    stale_start.append(name)
            elif fingerprint != current:
                # Nothing pins where the migration started, and the world has moved
                # since this baseline. If only the contents of the same manifest
                # moved (same file, base Python, toolset, container), that move is
                # the migration — bumped pins — and the baseline is its starting
                # point. Otherwise it drifted before migrating or the migration
                # changed the setup itself: can't tell.
                if baseline_setup_unchanged(slot, current_setup):
                    content_moved.append(name)
                else:
                    unpinned.append(name)
            # else: nothing moved since the baseline, so it describes the start too.
        elif fingerprint != current:
            stale_now.append(name)

    if stale_now:
        report.notes.append(
            f"STALE EVIDENCE: the {', '.join(stale_now)} capture was taken against a different "
            "manifest/environment than the one on disk now — this verdict describes the "
            "world as it was then. Re-capture to judge the current code."
        )
    if stale_start:
        report.notes.append(
            f"STALE EVIDENCE: the {', '.join(stale_start)} capture was taken against a "
            f"different manifest/environment than the one migration run {run} started "
            "from — it is not this migration's starting point, so the before/after diff "
            "compares the wrong worlds."
        )
    if stale_now or stale_start:
        report.baseline_stale = True
    elif not (unstamped or unpinned):
        report.baseline_stale = False
    if content_moved:
        report.notes.append(
            f"the {', '.join(content_moved)} capture predates a change to the contents of "
            "the same manifest (same file, base Python, toolset and container) — taken as "
            "the migration's starting point, since bumping pins is the migration."
        )
    if unstamped:
        report.notes.append(
            f"staleness unknown for the {', '.join(unstamped)} capture "
            "(taken before pymolt stamped capture environments)."
        )
    if unpinned:
        why = (
            f"migration run {run} predates recording its starting environment"
            if run is not None
            else "no applied migration (`pymolt apply`) records where it started"
        )
        report.notes.append(
            f"staleness unknown for the {', '.join(unpinned)} capture: the "
            f"manifest/environment changed since it was taken and {why}, so it cannot be "
            "told apart from a baseline taken against a different starting point."
        )
