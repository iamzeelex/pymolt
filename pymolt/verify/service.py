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
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from pymolt.adapters.python_runner import to_host
from pymolt.adapters.subprocess_runner import run_command
from pymolt.ingestion.config import EnvConfig
from pymolt.state_io import atomic_write_bytes, atomic_write_json, state_lock
from pymolt.verify.export_watcher import export_watcher
from pymolt.verify.facts import (
    BACKEND_UNOBSERVABLE,
    C_SITE_CAPPED,
    CALL_SHAPE,
    CONTENT_UNSEEN,
    ENVIRONMENT_DIFFERS,
    INPUT_CHANGED,
    INPUT_ORDER_CHANGED,
    ORDER_CHANGED,
    UNMEASURED_NOISE,
    UNPAIRED_SITE,
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
from pymolt.verify.inconclusive import (
    NEEDS_EVIDENCE,
    NEEDS_REVIEW,
    SETTLE_ENVIRONMENT,
    SETTLE_NOISE,
    SETTLE_NOT_EXERCISED,
    SETTLE_REVIEW,
    SETTLE_UNCOMPARABLE,
    cause,
    entry_cause,
    settle_uncomparable,
)
from pymolt.verify.models import (
    CaptureMode,
    CaptureValidity,
    ContractSlot,
    ContractState,
    InconclusiveCause,
    TraceArtifactQuality,
    VerificationVerdict,
)
from pymolt.verify.report import ComparedRecordings, ContractReport, build_contract_report

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
    # Seconds the traced program waited, summed over its threads and processes, for a JSONL
    # trace writer that fell behind (``backpressure_wait_ms``): the price of keeping every
    # event. None when no process reported it (an older runtime, a facts sink).
    backpressure_wait_seconds: float | None = None
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
    "PYMOLT_TRACE_QUEUE_SIZE", "PYMOLT_TRACE_CLOSE_TIMEOUT", "PYMOLT_TRACE_BACKPRESSURE",
    "PYMOLT_TRACE_STALL_SECONDS",
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
    """Resolve old and replacement API paths from the plans whose code is on disk (every run
    in effect, latest first)."""
    from pymolt.migration_state import MigrationReceipt, plans_in_effect

    receipt = MigrationReceipt.load(project_dir)
    paths: list[str] = []
    for plan in plans_in_effect(project_dir, receipt):
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

    Where an apply receipt exists the frozen ``old_source`` of the oldest plan in effect that
    touched a file is the baseline text of that file. Any other file is compared best effort
    against the baseline trace's recorded site text: a recorded call line that no longer reads
    the same marks the nearby line that now reads most like it as edited."""
    root = Path(os.path.abspath(project_dir))
    found: set[str] = set()
    covered: set[str] = set()
    try:
        from pymolt.migration_state import MigrationReceipt, plans_in_effect

        plans = plans_in_effect(root, MigrationReceipt.load(root))
    except Exception as exc:  # noqa: BLE001 - best effort
        logger.info("stdlib tracing: could not read the apply receipt: %s", exc)
        plans = []
    for plan in reversed(plans):                          # oldest first: the baseline text
        for change in plan.changes:
            if change.path in covered:
                continue
            current = _read_project_text(root, change.path)
            covered.add(change.path)
            if current is not None:
                found |= _stdlib_modules_on_lines(current,
                                                  _changed_lines(change.old_source, current))
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
    # Modules gevent / eventlet monkeypatch, or the runtime itself relies on: tracing them
    # puts the recorder on the primitives it is built on (the writer, its locks, the clock).
    "threading", "_thread", "socket", "ssl", "select", "selectors", "signal", "time",
    "queue", "subprocess", "os", "sys", "gc", "atexit", "io", "weakref",
    # Pervasive infrastructure: its calls dominate the event volume (every log line, every
    # decorator, every context manager), while what a Python move breaks in it is spelled
    # module-first (``inspect.getargspec``), so the static delta finds it. Not ``importlib``:
    # its breakage sits on loader and finder objects (``SourceFileLoader(...).load_module()``),
    # which only the trace can name.
    "logging", "traceback", "types", "functools", "operator", "itertools", "copy",
    "contextlib", "inspect",
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
    log_path: Path | None = None,
) -> tuple[list[str], int]:
    """Inject the watcher into an ALREADY-RUNNING container via `docker cp` + `docker exec`.

    No `docker run` from scratch and no new volume mounts: the standalone bundle is copied in,
    the command runs under it with PYMOLT_TRACE_* env, and the JSONL is copied back out. The
    target container and the app inside it are never modified (the bundle is temp + removed).

    The command's output (stdout, then stderr) is written to ``log_path`` when given, as the
    local path does: without it a failed run says nothing about which test failed.
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
    if log_path is not None:
        _write_command_log(log_path, command_result.stdout, command_result.stderr)

    pull = work / "pull"
    _docker(["cp", f"{container}:{trace_in}", str(pull)])
    # best-effort cleanup so the container is left exactly as found
    _docker(["exec", container, "rm", "-rf", bundle_in, trace_in], check=False)
    return _process_traces(pull, recursive=True), command_result.returncode


def _write_command_log(log_path: Path, stdout: str | None, stderr: str | None) -> None:
    """A finished command's captured output as its command log: stdout, then stderr (the
    two streams were captured apart, so their interleaving is lost). Never fails the capture."""
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8", errors="replace") as log:
            log.write(stdout or "")
            if stderr:
                if stdout and not stdout.endswith("\n"):
                    log.write("\n")
                log.write(stderr)
    except OSError as exc:
        logger.warning("could not write the command log %s: %s", log_path, exc)


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
    # What a full JSONL queue cost (runtimes that report it): producer waits, their total
    # time, and writer stalls. Informational, never part of the loss accounting.
    backpressure = {"waits": 0, "wait_ms": 0, "writer_stalls": 0, "reported": False}
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
        for name, key in (("backpressure_waits", "waits"), ("backpressure_wait_ms", "wait_ms"),
                          ("writer_stalls", "writer_stalls")):
            value = raw_counters.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                backpressure[key] += value
                backpressure["reported"] = True
        metadata_complete = metadata_complete and process_counters_complete
    if len(processes) != len(produced):
        metadata_complete = False
    counters["dropped_events"] = sum(counters[name] for name in _DROP_COUNTERS)
    return {
        "counters": counters,
        "backpressure": backpressure,
        "metadata_complete": metadata_complete,
        "process_metadata": processes,
        "backends": backends,
        "instrumentation_skipped": skipped,
    }


def _backpressure_wait_seconds(backpressure: dict) -> float | None:
    """The producers' total wait for a JSONL writer, in seconds (None: nobody reported)."""
    if not backpressure.get("reported"):
        return None
    return round(backpressure["wait_ms"] / 1000.0, 3)


def _write_trace_metadata(trace_path: str | Path, payload: dict) -> str | None:
    path = _trace_metadata_path(trace_path)
    try:
        atomic_write_json(path, payload, indent=2, sort_keys=True)
    except OSError as exc:
        logger.warning("could not persist trace metadata %s: %s", path, exc)
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
    command's output lands in ``<trace stem>.command.log`` next to the trace
    artifacts (surfaced via ``command_log`` / ``returncode``), never on the parent
    tty: merged as it runs for a local command, stdout then stderr for one run in a
    ``container``.
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

        returncode: int | None = None
        interrupted = threading.Event()
        # Keep the log next to the durable trace artifact (out_path), not the
        # ephemeral work tempdir, so the path stored on the slot stays valid.
        out_p = Path(out_path)
        log_path = out_p.parent / f"{out_p.stem}.command.log"
        if container:
            produced, returncode = capture_trace_in_container(
                target, command, backend, bundle, work,
                container, workdir, exclude, source, include_internal,
                privacy, sample_rate, impact_targets,
                deployment_id, request_id, correlation_id,
                **_sink_kwargs(sink, trace_mode, sample_first, widen_targets, trace_stdlib),
                log_path=log_path,
            )
        else:
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
            backpressure_wait_seconds=_backpressure_wait_seconds(runtime["backpressure"]),
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
        if runtime["backpressure"]["reported"]:
            metadata["backpressure"] = {k: v for k, v in runtime["backpressure"].items()
                                        if k != "reported"}
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


def slot_path(project_dir: str | Path, value: str | None) -> str | None:
    """A slot's stored path as an absolute one: a relative path is the project's (slots are
    saved project-relative), never the current directory's."""
    if not value:
        return value
    path = Path(value)
    return str(path if path.is_absolute() else Path(os.path.abspath(project_dir)) / path)


def same_slot_path(project_dir: str | Path, a: str | None, b: str | None) -> bool:
    """Two stored paths name the same file (one may be absolute, the other relative)."""
    if not a or not b:
        return a == b
    return os.path.realpath(slot_path(project_dir, a)) == os.path.realpath(
        slot_path(project_dir, b))


def load_contract_state(project_dir: str | Path) -> ContractState:
    """The current capture state, or a fresh empty one if none is persisted yet. A damaged
    file, or one written by a newer pymolt, raises ``StateFileError``: it is never read as
    empty (the next save would drop every slot's pointer).

    Every slot path comes back absolute (anchored to the project root), whatever the
    current directory is; they are stored relative (see ``ContractState.save``).
    """
    state = ContractState.load(_state_path(project_dir)) or ContractState()
    root = Path(os.path.abspath(project_dir))
    for slot in (state.baseline, state.post_migration, state.baseline_replicate,
                 *state.diagnostic_captures):
        if slot is not None:
            for field in ("trace_path", "metadata_path", "command_log", "sources_path"):
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


_FAILED_TEST = re.compile(r"^(?:FAILED|ERROR) (\S+)")


def failed_tests(command_log: str | Path | None, limit: int = 3) -> tuple[list[str], int]:
    """``(first ``limit`` test ids, how many)`` from pytest's short summary (``FAILED
    tests/x.py::test_y - ...`` and ``ERROR ...`` lines) in a command log; ``([], 0)`` when the
    log is missing or has none."""
    if not command_log:
        return [], 0
    try:
        lines = Path(command_log).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return [], 0
    found = [m.group(1) for m in map(_FAILED_TEST.match, lines) if m]
    return found[:limit], len(found)


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

    ``when="baseline_replicate"`` records the baseline's command a second time into its own
    slot (see ``capture_baseline_replicate``, which checks it may); it runs in the baseline's
    environment, and replacing the baseline archives it.
    """
    if when not in ("baseline", "post_migration", REPLICATE):
        raise ValueError(
            f"when must be 'baseline', 'post_migration' or {REPLICATE!r}, got {when!r}")
    env_when = "baseline" if when == REPLICATE else when
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
    # A state file the capture could not save into refuses it now (``StateFileError``), not
    # after the command ran for minutes.
    load_contract_state(project_dir)
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
                    else _resolve_stdlib_option(project_dir, env_when, stdlib))
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
        resolved = resolve_capture_env(project_dir, env_when)
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
        _record_sources_for(staged_path, project_dir, env_when, runner_spec)
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
    with state_lock(project_dir, operation=f"capture {when}"):
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

        slot = ContractSlot(
            trace_path=result.out_path, captured_at=_now(), mode=mode,
            command=list(command), target=target, events=result.events, processes=result.processes,
            command_log=result.command_log, returncode=result.returncode, env_note=env_note,
            runner=runner_spec,
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
            backpressure_wait_seconds=getattr(result, "backpressure_wait_seconds", None),
            instrumentation_skipped=result.instrumentation_skipped,
            activation_failures=getattr(result, "activation_failures", []),
            red=_red_stamp(result) if validity is CaptureValidity.VALID else None,
            replicate_of=(state.baseline.captured_at if when == REPLICATE and state.baseline
                          else None),
        )
        if validity is CaptureValidity.VALID:
            setattr(state, when, slot)
            if when == "baseline" and state.baseline_replicate is not None:
                # A replicate measures the noise of the baseline it re-ran: a new baseline makes
                # it stale. Archived, not deleted.
                archive_existing_capture(project_dir, REPLICATE)
                state.baseline_replicate = None
        else:
            state.diagnostic_captures.append(slot)
        state.save(_state_path(project_dir))
    return slot


REPLICATE = "baseline_replicate"


def sources_changed(project_dir: str | Path, snapshot: str | Path | None) -> list[str] | None:
    """The files of a capture's source snapshot whose bytes differ now (or that are gone);
    None when the snapshot is missing or unreadable."""
    data = _read_snapshot(slot_path(project_dir, str(snapshot) if snapshot else None))
    if data is None:
        return None
    root = Path(project_dir)
    changed: list[str] = []
    for rel, entry in sorted(data["files"].items()):
        try:
            digest = hashlib.sha256((root / rel).read_bytes()).hexdigest()
        except OSError:
            changed.append(rel)
            continue
        if not isinstance(entry, dict) or digest != entry.get("sha256"):
            changed.append(rel)
    return changed


def capture_baseline_replicate(
    project_dir: str | Path, *, cancel_event: threading.Event | None = None,
    accept_failing: bool = False, command: list[str] | None = None,
) -> ContractSlot:
    """Run the baseline's command a second time, on the same code in the same environment,
    into the ``baseline_replicate`` slot: whatever differs between the two runs is run-to-run
    noise (``verify.replicate``), which ``pymolt verify`` then tells apart from the migration.

    Everything is taken from the baseline slot: command, mode, target, privacy, sample rate,
    tracer backend, stdlib modules, container. Refused (``ValueError``, before anything runs)
    without a valid baseline pymolt ran itself, when ``command`` differs from the baseline's,
    when the environment fingerprint moved, or when any file of the baseline's source snapshot
    changed (or the baseline has none, so this cannot be checked). Settings a slot does not
    record (``--exclude``, ``--source``) take their defaults."""
    project_dir = Path(os.path.abspath(project_dir))
    base = load_contract_state(project_dir).baseline
    if base is None or base.validity is not CaptureValidity.VALID:
        raise ValueError("no valid baseline to replicate: capture the baseline first")
    if base.mode not in (CaptureMode.TEST_SUITE, CaptureMode.LIVE_COMMAND) or not base.command:
        raise ValueError(
            "the baseline was recorded by attaching to a process pymolt did not run, so pymolt "
            "cannot run it again")
    if command and list(command) != list(base.command):
        raise ValueError(
            "a replicate runs the baseline's own command; omit the command (the baseline ran: "
            f"{' '.join(base.command)})")
    now = environment_fingerprint(project_dir)
    if base.env_fingerprint is None or now != base.env_fingerprint:
        raise ValueError(
            "the environment changed since the baseline (its fingerprint differs): a replicate "
            "must run where the baseline ran")
    changed = sources_changed(project_dir, base.sources_path)
    if changed is None:
        raise ValueError(
            "the baseline has no source snapshot, so a replicate cannot be checked to run the "
            "same code: capture the baseline again")
    if changed:
        raise ValueError(
            f"the project's code changed since the baseline ({', '.join(changed[:5])}"
            f"{f' and {len(changed) - 5} more' if len(changed) > 5 else ''}): a replicate must "
            "run the same code")
    container = base.runner.name if base.runner is not None and base.runner.kind == "container" \
        else None
    workdir = base.runner.workdir if container else None
    return capture_named_trace(
        project_dir, REPLICATE, base.mode, command=list(base.command), target=base.target,
        backend=base.backend or "auto", container=container, workdir=workdir,
        cancel_event=cancel_event, privacy=base.privacy_profile, sample_rate=base.sample_rate,
        accept_failing=accept_failing or base.red is not None,
        stdlib=",".join(base.traced_stdlib) if base.traced_stdlib else "none",
        impact_targets=list(base.impact_targets) or None,
    )


def usable_replicate(project_dir: str | Path, state: ContractState | None = None,
                     ) -> tuple[ContractSlot | None, str | None]:
    """The baseline replicate when it can measure the baseline's noise, else ``(None, why)``
    (``(None, None)``: there is none). It must re-run the current baseline (``replicate_of``),
    with the same command, target, mode, privacy, sample rate, stdlib modules and environment
    fingerprint, on the same code (the two source snapshots agree), and be valid."""
    state = state or load_contract_state(project_dir)
    base, rep = state.baseline, state.baseline_replicate
    if rep is None:
        return None, None
    if base is None:
        return None, "baseline replicate ignored: there is no baseline"
    rep_trace = slot_path(project_dir, rep.trace_path)
    if rep.validity is not CaptureValidity.VALID or not rep_trace or not Path(rep_trace).is_file():
        return None, "baseline replicate ignored: it is not a valid capture"
    if rep.replicate_of != base.captured_at:
        return None, ("baseline replicate ignored: it re-ran an earlier baseline "
                      f"({rep.replicate_of}), not the current one ({base.captured_at})")
    for name in ("command", "target", "mode", "privacy_profile", "sample_rate",
                 "env_fingerprint", "traced_stdlib"):
        if getattr(rep, name) != getattr(base, name):
            return None, f"baseline replicate ignored: its {name.replace('_', ' ')} differs"
    a = _read_snapshot(slot_path(project_dir, base.sources_path))
    b = _read_snapshot(slot_path(project_dir, rep.sources_path))
    if a is None or b is None:
        return None, "baseline replicate ignored: a source snapshot is missing"
    differ = sorted(f for f in a["files"].keys() & b["files"].keys()
                    if (a["files"][f] or {}).get("sha256") != (b["files"][f] or {}).get("sha256"))
    if differ:
        return None, ("baseline replicate ignored: the code differs between the two runs "
                      f"({', '.join(differ[:3])})")
    env_differs = _replicate_environment_differs(slot_path(project_dir, base.trace_path),
                                                 rep_trace)
    if env_differs:
        return None, ("baseline replicate ignored: its runtime environment differs from the "
                      f"baseline's ({env_differs}): it would measure the environment, not run "
                      "noise")
    return rep.model_copy(update={"trace_path": rep_trace}), None


def _replicate_environment_differs(base_trace: str | Path | None,
                                   rep_trace: str | Path | None) -> str | None:
    """The aspects in which the replicate's recorded runtime environment differs from the
    baseline's (None when they agree, or when either did not record one: unknown is accepted
    as before)."""
    from pymolt.verify import environment as env

    def envs(trace: str | Path | None) -> list | None:
        if not trace:
            return None
        meta = _read_trace_metadata(trace)
        procs = [p for p in (meta.get("processes") or ()) if isinstance(p, dict)] or (
            [meta] if meta else [])
        records = [p.get("environment") for p in procs]
        return records if records and all(isinstance(r, dict) for r in records) else None

    cmp = env.compare(envs(base_trace), envs(rep_trace))
    if cmp is None or cmp.same:
        return None
    return cmp.describe(list(cmp.differs)[:4])


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
    current = Path(slot_path(project_dir, existing.trace_path) or "")
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


def restore_archived_capture(archived: str | Path, original: str | Path) -> None:
    """Undo ``archive_existing_capture``: move a recording and its sidecars back."""
    for src, dst in ((Path(archived), Path(original)),
                     (_trace_metadata_path(archived), _trace_metadata_path(original)),
                     (snapshot_path(archived), snapshot_path(original))):
        if src.is_file():
            shutil.move(str(src), str(dst))
    _move_sources(archived, original)


def set_aside_post_migration(project_dir: str | Path, state: ContractState,
                             run_id: str) -> str | None:
    """Take the post-migration capture out of its slot because a rollback restored the code
    it recorded. It is kept, never deleted: in ``state.diagnostic_captures``, stamped
    ``set_aside_by``, with its recording moved to the archive so the next post-migration
    capture cannot overwrite what the entry points at. Changes ``state`` in place (the caller
    saves it) and returns where the recording went (None: there was no slot, or the file
    could not be moved and stays where it was)."""
    slot = state.post_migration
    if slot is None:
        return None
    archived = archive_existing_capture(project_dir, "post_migration")
    update: dict = {
        "validity": CaptureValidity.UNKNOWN,
        "validity_reason": (f"invalidated by rollback of migration {run_id[:8]}: the code "
                            "it recorded is no longer on disk"),
        "set_aside_by": run_id,
    }
    if archived is not None:
        update["trace_path"] = archived
        if slot.metadata_path:
            update["metadata_path"] = str(_trace_metadata_path(archived))
        if slot.sources_path:
            update["sources_path"] = str(sources_path(archived))
    state.diagnostic_captures.append(slot.model_copy(update=update))
    state.post_migration = None
    return archived


def set_aside_capture(state: ContractState) -> ContractSlot | None:
    """The post-migration capture the last rollback set aside, while no newer one fills the
    slot (None otherwise)."""
    if state.post_migration is not None:
        return None
    for slot in reversed(state.diagnostic_captures):
        if slot.set_aside_by:
            return slot
    return None


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
    load_contract_state(project_dir)        # refuses a state file collect could not save into
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
    snapshot = json.dumps({"schema": SOURCES_SCHEMA, "root": root,
                           "fact_files": sorted(fact_files), "files": files,
                           "skipped": skipped, "left_out": left_out[:500],
                           "left_out_total": len(left_out)})
    try:
        atomic_write_bytes(out, gzip.compress(snapshot.encode("utf-8"), compresslevel=6))
    except OSError as exc:
        logger.warning("could not write the source snapshot for %s: %s", trace_path, exc)
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
    data = (_read_snapshot(slot_path(project_dir, baseline.sources_path))
            if baseline is not None else None)
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
    load_contract_state(project_dir)        # refuses before anything moves
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
    with state_lock(project_dir, operation=f"capture {when} --collect"):
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
    strict: bool = False,
) -> ContractReport:
    """The unified report, sourcing trace/against from the persisted state when
    not explicitly overridden. ``strict``: every ACCEPTED change is a review item.

    Resolution: explicit overrides always win. Otherwise — both slots captured
    -> trace=post_migration, against=baseline (this is what answers "what
    changed across the migration"); only one captured -> that one as trace,
    no diff; neither -> trace=None (today's all-BLIND behavior, unchanged).
    """
    from pymolt.migration_state import MigrationReceipt
    from pymolt.verify.unified import UnifiedVerificationReport

    state = load_contract_state(project_dir)
    receipt = MigrationReceipt.load(project_dir)
    # verify saves its verdict over the saved one: a damaged or newer file refuses it now
    # (``StateFileError``), before the comparison.
    UnifiedVerificationReport.load(project_dir)

    if changed_api_paths is None:
        from pymolt.migration_state import plans_in_effect

        impacts = [impact for applied_plan in plans_in_effect(project_dir, receipt)
                   for impact in applied_plan.impacts]
        if impacts:
            changed_api_paths = impacts

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
    elif against is None and state.baseline is not None:
        # An explicit post-migration recording: the "before" side is still the baseline slot.
        against = _slot_trace(state.baseline)
    # The baseline slot's recording on the "before" side, however verify got its path (the CLI
    # fills ``--baseline`` from the slot): its own loss counters and freshness are evidence.
    if (against and state.baseline is not None
            and not any(name == "baseline" for name, _ in used_slots)
            and same_slot_path(project_dir, against, state.baseline.trace_path)):
        used_slots.append(("baseline", state.baseline))

    noise_profile, replicate_note = None, None
    # The replicate measures the BASELINE's noise: it applies whenever the "before" side is the
    # baseline slot's recording, whether verify took it from the slot or was handed its path
    # (``--post-migration PATH`` alone fills ``--baseline`` from the slot), whatever the
    # "after" side is.
    if trace and against and state.baseline is not None and same_slot_path(
            project_dir, against, state.baseline.trace_path):
        replicate, replicate_note = usable_replicate(project_dir, state)
        if replicate is not None and state.baseline is not None:
            try:
                from pymolt.verify.replicate import load_or_build_profile

                noise_profile = (load_or_build_profile(
                    project_dir, against, state.baseline.captured_at,
                    replicate.trace_path, replicate.captured_at),
                    f"the baseline replicate of {replicate.captured_at}")
            except (OSError, ValueError) as exc:
                replicate_note = f"baseline replicate ignored: it could not be read ({exc})"

    predictions = None
    if trace and against:
        try:
            predictions = _predictions_to_calibrate(project_dir, state, against)
        except Exception as exc:  # noqa: BLE001 - calibration is a report section, not a gate
            logger.warning("could not load the predicted risks: %s", exc)
            predictions = {"risks": [], "source": "baseline", "plan_id": None,
                           "notes": [f"predicted risks unavailable: {exc}"]}

    diff_context = build_diff_context(project_dir, trace, against, strict=strict) \
        if trace and against else None
    report = build_contract_report(
        project_dir,
        trace=trace,
        against=against,
        probe_python=probe_python,
        changed_api_paths=changed_api_paths,
        comparator_profile=comparator_profile,
        custom_comparator=custom_comparator,
        predictions=predictions,
        diff_context=diff_context,
        noise_profile=noise_profile,
    )
    if replicate_note:
        report.notes.append(replicate_note)
    report.notes.extend(diff_context.notes if diff_context is not None else ())
    if (receipt is not None and receipt.status == "rolled_back" and receipt.in_effect
            and any(name == "post-migration" for name, _ in used_slots)):
        report.notes.append(
            f"migration run {receipt.run_id[:8]} was rolled back, but it had changed no file: "
            "the post-migration capture still records the code on disk, and verify still "
            "reads that run's plan")
    report.compared = _compared_recordings(project_dir, state, trace, against)
    _stamp_staleness(project_dir, report, used_slots, before_side=bool(trace and against))
    _stamp_verdict(report, trace, against, used_slots,
                   set_aside=set_aside_capture(state) if trace_override is None else None)
    return report


def _compared_recordings(project_dir: str | Path, state: ContractState, trace: str | None,
                         against: str | None) -> ComparedRecordings:
    """Which recordings a report compared, and which capture each one is (by path: a
    recording named on the command line that is a capture's own file is that capture)."""
    def capture(path: str | None, *names: str) -> str | None:
        for name in names:
            slot = getattr(state, name.replace("-", "_"))
            if path and slot is not None and same_slot_path(project_dir, path, slot.trace_path):
                return name
        return None

    return ComparedRecordings(
        after=trace, after_capture=capture(trace, "post-migration", "baseline"),
        before=against, before_capture=capture(against, "baseline"),
    )


def build_diff_context(project_dir: str | Path, trace: str | Path, against: str | Path, *,
                       strict: bool = False):
    """What the fact diff needs to judge an edit by its provenance (``facts.DiffContext``).

    Snapshots come from the two captures' ``.sources.json`` (None for an older capture: the
    diff then falls back to line-text rules). The apply ledger's in-effect entries (the
    current epochs of the runs whose code is on disk, ``apply_ledger.in_effect_entries``) are
    located in the two snapshots once, through the one line alignment provenance uses
    (``workorder.coverage.ledger_coverage``); without snapshots, by the lines they were
    written at. The static contact maps are built from the snapshots; ``successors`` (old ->
    new symbol) from the applied plans' impacts, their risks' delta facts and the ledger; and
    ``type_relocations`` from the plans when they carry them. Never raises: a part that
    cannot be built is left None."""
    from pymolt.migration_state import MigrationReceipt
    from pymolt.verify.contact_map import contacts_by_line
    from pymolt.verify.facts import DiffContext

    root = Path(os.path.abspath(project_dir))
    ctx = DiffContext()
    ctx.old_sources = load_sources(sources_path(against))
    ctx.new_sources = load_sources(sources_path(trace))
    receipt = MigrationReceipt.load(root)
    try:
        _ledger_context(root, receipt, ctx)
    except Exception as exc:  # noqa: BLE001 - provenance degrades to hand edits
        logger.warning("could not read the apply ledger: %s", exc, exc_info=True)
        ctx.edits = None
        ctx.notes.append(f"the apply ledger could not be read ({type(exc).__name__}): edits are "
                         "judged as hand edits")
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
        from pymolt.migration_state import plans_in_effect

        predicted: set[str] = set()
        relocated: dict[str, str] = {}
        for plan in plans_in_effect(root, receipt):       # every run in effect, latest first
            for impact in plan.impacts:
                if impact.replacement_path:
                    successors.setdefault(impact.path, impact.replacement_path)
            for risk in plan.risks.risks if plan.risks is not None else ():
                if getattr(risk, "symbol", None):
                    predicted.add(risk.symbol)
                for fact in [risk.fact, *risk.facts]:
                    if fact and fact.get("symbol"):
                        predicted.add(fact["symbol"])
                    if fact and fact.get("symbol") and fact.get("successor"):
                        successors.setdefault(fact["symbol"], fact["successor"])
            relocations = getattr(plan, "type_relocations", None)
            for old, new in (_relocation_map(relocations) or {}).items() if relocations else ():
                relocated.setdefault(old, new)
        ctx.predicted_symbols = predicted or None
        if relocated:
            ctx.type_relocations = relocated
    except Exception as exc:  # noqa: BLE001 - provenance is best effort
        logger.warning("could not read the applied plan for the diff context: %s", exc)
    ctx.successors = successors or None
    try:
        from pymolt.verify.accepts import context_for

        ctx.accepts = context_for(root, trace, against, old_sources=ctx.old_sources,
                                  new_sources=ctx.new_sources, strict=strict)
    except Exception as exc:  # noqa: BLE001 - no accept is honoured, the diff stands
        logger.warning("could not read the accepted findings: %s", exc, exc_info=True)
        ctx.notes.append(f"accepted findings could not be read ({type(exc).__name__}): none "
                         "is honoured")
    return ctx


def _ledger_context(root: Path, receipt, ctx) -> None:
    """``ctx.edits`` (the in-effect ledger entries, located), ``ctx.not_in_capture`` and the
    notes that say what the ledger could not do."""
    from pymolt.apply_ledger import in_effect_entries
    from pymolt.workorder.coverage import (
        ledger_coverage,
        located_by_recorded_lines,
        setup_pythons,
    )

    entries, note, ledger = in_effect_entries(root, receipt)
    if note:
        ctx.notes.append(note)
    if entries is None:
        ctx.edits = None
        return
    ctx.notes.extend(ledger.notes if ledger is not None else ())
    if ctx.old_sources is None or ctx.new_sources is None:
        ctx.edits = located_by_recorded_lines(entries)
        if entries:
            ctx.notes.append("the captures carry no source snapshot, so the apply ledger's "
                             "entries are located by the lines they were written at")
        return
    base, target = setup_pythons(root)
    attested, attest_notes = record_attestations(root, ledger)
    coverage = ledger_coverage(ctx.old_sources, ctx.new_sources, entries, python_old=base,
                               python_new=target, attested=attested)
    ctx.notes.extend(attest_notes)
    ctx.edits = coverage.entries
    ctx.not_in_capture = [f"{run}:{step}" for run, step in coverage.not_in_capture]
    ctx.notes.extend(coverage.notes)


def record_attestations(root: Path, ledger):
    """``(check, notes)``: ``check(entry)`` is the person attestation an ``intended`` entry's
    record event carries, verified against this machine's out-of-tree log (``pymolt.attest``):
    bound to the project, the run, the step, the claim and expectation, and the recorded
    files. None when it has none or it does not verify; ``notes`` say why for the latter. The
    ledger's own fields are never trusted for it."""
    from pymolt.attest import AttestationStore, files_digest

    store = AttestationStore()
    by_seq = {e.seq: e for e in (ledger.events if ledger is not None else ())}
    cache: dict[tuple, dict | None] = {}
    notes: list[str] = []

    def check(entry):
        event = by_seq.get(entry.epoch)
        if (event is None or event.event != "record" or event.run_id != entry.run_id
                or event.step_id != entry.step_id or not event.attestation):
            return None
        key = (event.seq, entry.claim, entry.expect)
        if key not in cache:
            found = store.check(event.attestation, kind="record", project=root,
                                run_id=event.run_id, step=event.step_id, claim=entry.claim,
                                expect=entry.expect, files=files_digest(event.files))
            cache[key] = found.info()
            if not found.ok:
                notes.append(f"step {event.step_id}'s record names attestation "
                             f"{event.attestation}, but {found.reason}: its intended claim is "
                             "judged as unattested")
        return cache[key]

    return check, notes


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
    from pymolt.migration_state import plans_in_effect

    candidates = list(plans_in_effect(root, MigrationReceipt.load(root)))
    candidates.append(MigrationPlan.load(root))
    bound = [plan for plan in candidates if plan is not None and is_baseline
             and same_slot_path(root, plan.baseline_trace_path, baseline.trace_path)
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


def _missing_side_reason(trace: str | None, used_slots: list[tuple[str, object]],
                         set_aside: ContractSlot | None = None) -> str:
    """Why there is nothing to diff against, naming the side that is actually missing.

    ``set_aside``: the post-migration capture a rollback took out of its slot. It exists, so
    "no capture yet" would be false: the reason names it, where its recording is, and how to
    compare against it anyway."""
    names = {name for name, _ in used_slots}
    if names == {"baseline"}:
        if set_aside is not None:
            return (
                f"no post-migration capture: the one of {set_aside.captured_at} was set aside "
                f"by the rollback of migration {(set_aside.set_aside_by or '')[:8]}, which "
                f"restored the code it recorded (kept at {set_aside.trace_path}); apply again "
                "and capture post-migration, or pass that recording as --post-migration")
        return "no post-migration capture yet (only the baseline is recorded)"
    if names == {"post-migration"}:
        return "no baseline capture"
    if not names and trace is None:
        return "no captures"
    return "no baseline trace is available for a version diff"


def _missing_side_settle(used_slots: list[tuple[str, object]]) -> str:
    """What settles ``_missing_side_reason``: capturing the side that is missing."""
    names = {name for name, _ in used_slots}
    if names == {"baseline"}:
        return "capture post-migration"
    if names == {"post-migration"}:
        return "capture baseline (it records the code before the migration)"
    return "capture baseline, then post-migration"


def _stamp_verdict(
    report: ContractReport,
    trace: str | None,
    against: str | None,
    used_slots: list[tuple[str, object]],
    *,
    set_aside: ContractSlot | None = None,
) -> None:
    """Fold evidence into PASS/FAIL/INCONCLUSIVE without averaging uncertainty.

    Every INCONCLUSIVE reason carries its kind (``report.inconclusive_causes``, design decision
    7): a capture missing, invalid or unlike the other, BLIND symbols, uncomparable facts and
    unmeasured noise need evidence; review items need a person.

    ``set_aside``: the post-migration capture a rollback set aside (see
    ``_missing_side_reason``)."""
    # The verdict reads the WHOLE surface: an impact filter (an applied plan's paths) is a focus
    # for the report, not a limit on what can fail it or on what PASS must cover. FAIL takes
    # the whole diff (``all_*``), or a change nobody predicted could never be found; coverage
    # (BLIND, dynamic-only, static targets) is the whole surface in the headline fields, so the
    # same captures get the same verdict whether the migration came from ``apply`` or by hand.
    whole_diff = report.all_fact_diff or report.fact_diff
    whole_clean = report.all_diff_clean if report.all_diff_clean is not None \
        else report.diff_clean
    whole_counts = report.all_diff if report.all_diff is not None else report.diff
    for warning in (whole_diff or {}).get("warnings") or ():
        # Snapshot caps that lower the comparison level without blocking PASS.
        if str(warning).startswith(("widened:", "outcomes-capped:", "unsampled:",
                                    "volatile-sites:")):
            if warning not in report.notes:
                report.notes.append(str(warning))
    # A comparability reason is settled by capturing again (needs evidence), never by a person.
    comparability: dict[str, InconclusiveCause] = {}

    def unlike(reason: str, settle: str) -> None:
        comparability.setdefault(reason, cause(reason, NEEDS_EVIDENCE, "capture", settle))

    if trace is None:
        unlike("no current trace is available", "capture post-migration")
    else:
        quality = inspect_trace_artifact(trace)
        if quality.validity is not CaptureValidity.VALID:
            unlike(quality.reason or f"current trace is {quality.validity.value}",
                   "capture that side again")
    if against is None:
        unlike(_missing_side_reason(trace, used_slots, set_aside),
               _missing_side_settle(used_slots))
    else:
        quality = inspect_trace_artifact(against)
        if quality.validity is not CaptureValidity.VALID:
            unlike(quality.reason or f"baseline trace is {quality.validity.value}",
                   "capture the baseline again")

    slots = {name: slot for name, slot in used_slots}
    for name, slot in used_slots:
        validity = getattr(slot, "validity", CaptureValidity.UNKNOWN)
        if validity is not CaptureValidity.VALID:
            unlike(f"{name} capture validity is {validity.value}", f"capture {name} again")
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
            unlike(f"{name} capture has no event-loss counters",
                   f"capture {name} again with this pymolt")
        elif dropped:
            unlike(f"{name} capture dropped {dropped} event(s)", f"capture {name} again")
        skipped = getattr(slot, "instrumentation_skipped", [])
        if skipped:
            unlike(f"{name} capture skipped {len(skipped)} instrumentation target(s)",
                   f"capture {name} again once those targets can be instrumented (the "
                   "capture lists them)")
        failed = getattr(slot, "activation_failures", [])
        if failed:
            unlike(f"{name} capture: the watcher failed to activate in {len(failed)} "
                   "process(es), which ran untraced",
                   f"capture {name} again with the watcher active in every process")
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

        for differs, reason, what in (
                (_workload(baseline.command) != _workload(post.command),
                 "baseline and post-migration used different commands", "command"),
                (baseline.target != post.target,
                 "baseline and post-migration traced different targets", "target"),
                (baseline.mode != post.mode,
                 "baseline and post-migration used different capture modes", "capture mode"),
                (baseline.privacy_profile != post.privacy_profile,
                 "baseline and post-migration used different privacy profiles",
                 "privacy profile"),
                (baseline.sample_rate != post.sample_rate,
                 "baseline and post-migration used different sample rates", "sample rate")):
            if differs:
                unlike(reason, f"capture post-migration again with the baseline's {what}")
        if (baseline.sample_rate < 1 or post.sample_rate < 1) and not _tallies_complete(
                whole_diff):
            unlike("sampled captures cannot prove that unmatched interactions disappeared",
                   "capture both sides again without `--sample-rate`")

    if report.baseline_stale is True:
        unlike("capture evidence is stale", "capture the stale side again (the notes name it)")
    elif used_slots and report.baseline_stale is None:
        unlike("capture freshness could not be established",
               "capture again: the capture records no environment to check it against")

    # A negative oracle is authoritative only over a valid, comparable pair.
    # Changed output from stale or mismatched workloads is evidence to inspect,
    # but it cannot honestly describe the current migration.
    if comparability:
        report.verdict = VerificationVerdict.INCONCLUSIVE
        report.verdict_reasons = list(comparability)
        report.inconclusive_causes = list(comparability.values())
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

    causes: list[InconclusiveCause] = []
    if whole_counts is None:
        causes.append(cause("no before/after diff was produced", NEEDS_EVIDENCE, "capture",
                            "the version diff failed (a note says why): capture the side it "
                            "could not read again, then verify"))
    elif whole_counts.get("skipped_opaque", 0):
        causes.append(_opaque_cause(whole_diff))
    causes.extend(_fact_diff_causes(whole_diff))
    causes.extend(_blind_causes(report))
    if report.dynamic_only:
        unverified = _unverified_dynamic_only(report)
        if unverified:
            causes.append(cause(
                f"{len(unverified)} observed symbol(s) are missing from the static map"
                + (" and were not compared equal on both sides" if report.fact_diff else ""),
                NEEDS_EVIDENCE, "uncomparable",
                "once every call fact of theirs compares equal on both sides they stop "
                "blocking (settle those facts' own reasons)" if report.fact_diff else
                "only a fact diff compares them (`--profile exact`)"))
        else:
            report.notes.append(
                f"{report.dynamic_only} observed symbol(s) are missing from the static map but "
                "every one of their call facts compared equal on both sides: a static "
                "under-approximation, not an unverified contact"
            )
    if report.static_targets == 0:
        causes.append(cause("the static contract surface is empty", NEEDS_EVIDENCE,
                            "static-map", "verify the project's root: the static contact map "
                            "found no dependency call to judge"))

    # Keep output stable and readable when two checks identify the same cause.
    unique: dict[str, InconclusiveCause] = {}
    for c in causes:
        unique.setdefault(c.reason, c)
    report.inconclusive_causes = list(unique.values())
    report.verdict_reasons = list(unique)
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


def _blind_split(symbols: list, report: ContractReport) -> str:
    """Why these BLIND symbols are BLIND: not run by the workload (a test gap), ran but not
    recorded (a tracer gap), or unobservable by any trace (``SymbolStatus.blind_reason``).
    A "ran" coarser than line is said: at file granularity "ran" means "its file ran"."""
    counts = Counter(s.blind_reason for s in symbols)
    targets = {s.target for s in symbols}
    coarse = {c.granularity for c in report.contacts
              if c.target in targets and c.status == "ran-not-recorded"} - {"line", None}
    parts = []
    for key, label in (("not-run", "not run by the workload"),
                       ("ran-not-recorded", "ran but not recorded"),
                       ("unobservable", "unobservable")):
        if counts[key]:
            part = f"{counts[key]} {label}"
            if key == "ran-not-recorded" and coarse:
                part += " at " + "/".join(sorted(coarse)) + " granularity"
            parts.append(part)
    return "; ".join(parts)


def _blind_settle(symbols: list) -> str:
    """What settles these BLIND symbols: a test reaches those the workload did not run; those
    that ran but were not recorded, or that no trace can observe, are a watcher limit."""
    if not symbols or any(s.blind_reason in ("not-run", None) for s in symbols):
        return SETTLE_NOT_EXERCISED
    return ("no new test settles them: they ran but the watcher did not record them, or no "
            "trace can observe them")


def _blind_reasons(report: ContractReport) -> list[str]:
    """The BLIND reasons' texts (``_blind_causes``)."""
    return [c.reason for c in _blind_causes(report)]


def _blind_causes(report: ContractReport) -> list[InconclusiveCause]:
    """Why BLIND symbols block PASS, split by why each is BLIND. After ``apply`` the reasons
    name those inside the plan's predicted impacts apart from those outside them; both block.
    An unobservable symbol blocks too (the owner's open decision). BLIND needs evidence."""
    if not report.blind:
        return []
    blind = [s for s in report.symbols if s.status == "blind"]
    if not report.impact_filter_active or not blind:
        split = _blind_split(blind, report)
        return [cause(f"{report.blind} static dependency symbol(s) were not exercised"
                      + (f" ({split})" if split else ""),
                      NEEDS_EVIDENCE, "not-exercised", _blind_settle(blind))]
    causes = []
    for where, members in (("inside", [s for s in blind if s.impacted]),
                           ("outside", [s for s in blind if not s.impacted])):
        if members:
            targets = sorted(s.target for s in members)
            names = ", ".join(targets[:5]) + (", …" if len(targets) > 5 else "")
            causes.append(cause(f"{len(members)} static dependency symbol(s) {where} the "
                                f"plan's predicted impacts were not exercised "
                                f"({_blind_split(members, report)}): "
                                f"{names}",
                                NEEDS_EVIDENCE, "not-exercised", _blind_settle(members)))
    return causes


def _unverified_dynamic_only(report: ContractReport) -> list[str]:
    """Dynamic-only symbols that still block PASS, on the whole surface (an applied plan's
    paths do not narrow it).

    A symbol the static map missed but the trace observed is only a coverage gap if its
    behaviour was not verified. With a fact diff, those whose every fact compared equal on
    both sides are verified; without one (custom comparator, single trace) none are.
    """
    dynamic = [s.target for s in report.symbols if s.status == "dynamic-only"]
    if report.fact_diff is None:
        return dynamic
    verified = set(report.fact_equal_symbols)
    return [target for target in dynamic if target not in verified]


def _fact_diff_reasons(fact_diff: dict | None) -> list[str]:
    """The fact diff's INCONCLUSIVE reasons' texts (``_fact_diff_causes``)."""
    return [c.reason for c in _fact_diff_causes(fact_diff)]


def _fact_diff_causes(fact_diff: dict | None) -> list[InconclusiveCause]:
    """Why a clean fact diff still cannot prove equivalence (empty when it can), each reason
    with its kind: a review item a person must look at needs review; unmeasured noise, an
    environment difference and a fact that could not be compared need evidence."""
    if not fact_diff or fact_diff.get("verdict") != VerificationVerdict.INCONCLUSIVE.value:
        return []
    entries = fact_diff.get("entries") or []
    counts = fact_diff.get("counts") or {}
    measured = bool(fact_diff.get("noise_measured"))
    causes: list[InconclusiveCause] = []
    for side, label in (("old", "baseline"), ("new", "post-migration")):
        summary = fact_diff.get(side) or {}
        evicted = summary.get("evicted") if isinstance(summary, dict) else None
        if isinstance(evicted, int) and evicted > 0:
            causes.append(cause(
                f"the {label} facts snapshot reached its fact cap and evicted {evicted} "
                "call(s): the evidence is incomplete",
                NEEDS_EVIDENCE, "uncomparable", SETTLE_UNCOMPARABLE["evicted"]))
    if not any(counts.values()):
        causes.append(cause("no comparable call facts were recorded on both sides",
                            NEEDS_EVIDENCE, "not-exercised",
                            "add a test that reaches the project's dependency calls, then "
                            "capture both sides"))
    # An unverified edited site is counted in the summary but never blocks PASS; a review
    # item blocks with its own reason (it is INCONCLUSIVE, not FAIL).
    entries = [e for e in entries if not e.get("unverified")]
    reviews = [e for e in entries if e.get("review")]

    def review_cause(reason: str, settle: str) -> InconclusiveCause:
        # A replicate settles unmeasured noise; once one was measured, what it did not settle
        # is a person's (``inconclusive.entry_cause``).
        if measured:
            return cause(reason, NEEDS_REVIEW, "review", SETTLE_REVIEW)
        return cause(reason, NEEDS_EVIDENCE, "noise", settle)

    classes = (
        (UNMEASURED_NOISE, "value difference(s) may be run-to-run noise: their input is not "
                           "fully observed and the workload varies between runs"),
        ("masked input", "value difference(s) where the inputs matched only once volatile "
                         "values were masked"),
        (INPUT_CHANGED, "call(s) whose input changed with no visible cause"),
        (UNPAIRED_SITE, "call site(s) whose inputs changed with no visible cause"),
    )
    settle = ("the baseline replicate did not settle them (they vary there in another way, or "
              "it did not reach them)" if fact_diff.get("noise_measured")
              else "a baseline replicate would tell run noise from a real change")
    order_settle = ("the baseline replicate could not tell (too few keys compared, or a pinned "
                    "hash seed)" if fact_diff.get("noise_measured")
                    else "a baseline replicate with random hash seeds would tell a hash-seeded "
                         "order from a deterministic one")
    for prefix, text in classes:
        hit = [e for e in reviews if str(e.get("review")).startswith(prefix)]
        reviews = [e for e in reviews if not str(e.get("review")).startswith(prefix)]
        if hit:
            names = ", ".join(sorted({str(e.get("symbol")) for e in hit})[:5])
            causes.append(review_cause(f"{len(hit)} {text} ({names}); {settle}", SETTLE_NOISE))
    for prefix, text in (
            (ORDER_CHANGED, "returned dict(s) whose key order changed (same keys and values)"),
            (INPUT_ORDER_CHANGED, "call(s) whose output changed as the dict key order of "
                                  "their input did (same keys and values)")):
        hit = [e for e in reviews if str(e.get("review")).startswith(prefix)]
        reviews = [e for e in reviews if not str(e.get("review")).startswith(prefix)]
        if hit:
            names = ", ".join(sorted({str(e.get("symbol")) for e in hit})[:5])
            causes.append(review_cause(
                f"{len(hit)} {text} ({names}); {order_settle}",
                "capture a baseline replicate with random hash seeds (`pymolt capture "
                "baseline --replicate`), then verify again"))
    hit = [e for e in reviews if str(e.get("review")).startswith(ENVIRONMENT_DIFFERS)]
    reviews = [e for e in reviews if not str(e.get("review")).startswith(ENVIRONMENT_DIFFERS)]
    if hit:
        env = ((fact_diff.get("summary") or {}).get("quality") or {}).get("environment")
        aspects = ", ".join((env.get("differs") or ())[:6]) if isinstance(env, dict) else "?"
        names = ", ".join(sorted({str(e.get("symbol")) for e in hit})[:5])
        causes.append(cause(
            f"the captures ran in different environments ({aspects}): {len(hit)} "
            f"difference(s) at calls that read them are reviews ({names}); capture "
            "both sides in the same environment to settle",
            NEEDS_EVIDENCE, "environment", SETTLE_ENVIRONMENT))
    if reviews:
        names = ", ".join(sorted({str(e.get("symbol")) for e in reviews})[:5])
        causes.append(cause(f"{len(reviews)} edited call(s) need review: {names}",
                            NEEDS_REVIEW, "review", SETTLE_REVIEW))
    entries = [e for e in entries if not e.get("review")]

    def uncomparable(reason: str, mechanism: str) -> InconclusiveCause:
        return cause(reason, NEEDS_EVIDENCE, "uncomparable", SETTLE_UNCOMPARABLE[mechanism])

    unseen = sum(1 for e in entries
                 if any(str(n).startswith(CONTENT_UNSEEN) for n in e.get("notes") or ()))
    if unseen:
        causes.append(uncomparable(
            f"{unseen} edited call(s) whose attested expectation cannot be checked: only the "
            "types of their outcomes are visible, not the values", "content-unseen"))
    unpaired = sum(
        1 for e in entries
        if e.get("category") == "rewritten" and e.get("comparison") in ("missing", "uncomparable")
    ) - unseen
    if unpaired:
        causes.append(uncomparable(
            f"{unpaired} rewritten call site(s) could not be paired with a post-migration call",
            "unpaired"))
    evicted_entries = sum(
        1 for e in entries
        if any(str(n).startswith("possibly-evicted") for n in e.get("notes") or ())
    )
    dropped_entries = sum(
        1 for e in entries
        if any(str(n).startswith("possibly-dropped") for n in e.get("notes") or ())
    )
    unobservable = sum(1 for e in entries if BACKEND_UNOBSERVABLE in (e.get("notes") or ()))
    capped_entries = sum(
        1 for e in entries
        if any(str(n).startswith(C_SITE_CAPPED) for n in e.get("notes") or ())
    )
    shape_entries = sum(
        1 for e in entries
        if any(str(n).startswith(CALL_SHAPE) for n in e.get("notes") or ())
        and not any(str(n).startswith(C_SITE_CAPPED) for n in e.get("notes") or ())
    )
    lost = sum(1 for e in entries if e.get("category") == "uncomparable") \
        - evicted_entries - dropped_entries - unobservable - capped_entries - shape_entries
    if unobservable:
        causes.append(uncomparable(
            f"{unobservable} compiled call(s) raise on the side whose backend can observe them; "
            "the other side's backend could not record them, so they cannot be compared",
            "backend-unobservable"))
    if dropped_entries:
        causes.append(uncomparable(
            f"{dropped_entries} call fact(s) are missing on a side that dropped events and "
            "cannot be compared", "dropped"))
    if capped_entries:
        causes.append(uncomparable(
            f"{capped_entries} call fact(s) are missing at compiled-call sites where the other "
            "capture recorded only the first calls and counted the rest; they cannot be compared",
            "c-site-capped"))
    if shape_entries:
        causes.append(uncomparable(
            f"{shape_entries} call fact(s) were recorded as compiled calls (no inputs) on one "
            "side and with their inputs on the other, so they cannot be paired by input",
            "call-shape"))
    if evicted_entries:
        causes.append(uncomparable(
            f"{evicted_entries} call fact(s) are missing on a side that evicted facts and "
            "cannot be compared", "evicted"))
    if lost:
        causes.append(uncomparable(
            f"{lost} call fact(s) lost their outcome distribution and cannot be compared",
            "distribution-lost"))
    # Not reached by any entry the diff makes (every inconclusive entry is named above); kept
    # so the verdict never stays unexplained.
    return causes or [cause("the fact diff could not establish equivalence", NEEDS_EVIDENCE,
                            "uncomparable", "read the fact diff (`pymolt verify . --json`)")]


def _opaque_cause(fact_diff: dict | None) -> InconclusiveCause:
    """The boundary diff's "opaque or nondeterministic" interactions: the fact diff's
    inconclusive entries that are not a change of presence. They need evidence when one of
    them is uncomparable, else (all review items) a person."""
    measured = bool((fact_diff or {}).get("noise_measured"))
    found = [(e, entry_cause(e, noise_measured=measured))
             for e in (fact_diff or {}).get("entries") or ()
             if e.get("category") not in ("appeared", "disappeared")
             and e.get("comparison") != "changed"]
    evidence = [e for e, f in found if f is not None and f[0] is NEEDS_EVIDENCE]
    reason = "some boundary interactions were opaque or nondeterministic"
    if evidence or not any(f is not None for _e, f in found):
        uncomparable = [e for e, f in found if f is not None and f[1] == "uncomparable"]
        return cause(reason, NEEDS_EVIDENCE, "uncomparable",
                     settle_uncomparable(uncomparable) if uncomparable else
                     SETTLE_UNCOMPARABLE["distribution-lost"])
    return cause(reason, NEEDS_REVIEW, "review", SETTLE_REVIEW)


def baseline_setup_unchanged(slot, current_setup: str | None) -> bool:
    """True when the baseline's setup (manifest file, base Python, toolset, container)
    is the current one, so any fingerprint difference is the manifest's contents.
    Unknown on either side (old slots, no config) is never "unchanged"."""
    recorded = getattr(slot, "env_setup_fingerprint", None)
    return recorded is not None and current_setup is not None and recorded == current_setup


def _applied_migration(project_dir: str | Path):
    """The receipt of the migration whose code is on disk (applied, or rolled back without
    restoring a file: ``MigrationReceipt.in_effect``), or None."""
    from pymolt.migration_state import MigrationReceipt

    receipt = MigrationReceipt.load(project_dir)
    if receipt is None or not receipt.in_effect:
        return None
    return receipt


def _stamp_staleness(project_dir: str | Path, report: ContractReport, used_slots, *,
                     before_side: bool = False) -> None:
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
    # The baseline is the "before" side of a diff: against a post-migration slot, or against
    # an explicit post-migration recording (``before_side``).
    before = "post-migration" in names or before_side
    run: str | None = None  # the applied migration's short id, when there is one
    start: str | None = None
    start_setup: str | None = None
    if "baseline" in names and before:
        migration = _applied_migration(project_dir)
        if migration is not None:
            run = migration.run_id[:8]
            start = migration.starting_fingerprint(project_dir)
            start_setup = migration.environment_setup_fingerprint

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
        elif name == "baseline" and before:
            if start is not None:
                if fingerprint != start:
                    # Pins bumped in the same manifest between the baseline and the apply
                    # are part of the migration; another setup is another starting point.
                    if baseline_setup_unchanged(slot, start_setup):
                        content_moved.append(name)
                    else:
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
