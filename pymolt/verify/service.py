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
import hashlib
import importlib.util
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from pymolt.adapters.subprocess_runner import run_command
from pymolt.ingestion.config import EnvConfig
from pymolt.verify.coverage import run_coverage_json
from pymolt.verify.export_watcher import export_watcher
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
# contract_capture, which never passes cancel_event) so a command that never
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


# ─────────────────────────────────────────────────────────────────────────────
# Capture — inject the watcher, run a command, merge the JSONL it writes.
# ─────────────────────────────────────────────────────────────────────────────


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
) -> dict:
    """The PYMOLT_TRACE_* variables that drive the watcher (shared by local + container)."""
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
    return env


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


def _capture_identity(supplied: str | None, env_name: str) -> str | None:
    value = supplied if supplied is not None else os.environ.get(env_name)
    return value or None


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
) -> tuple[list[str], int | None]:
    """Inject the watcher into a locally-run command via PYTHONPATH + env.

    Blocks until ``command`` exits, unless ``cancel_event`` is given and gets
    set from another thread first — then the process is terminated (SIGTERM,
    then SIGKILL after a grace period) and whatever it had already written is
    kept. This is what powers the guided flow's "Stop & Collect" for a
    long-running app (a dev server that never exits on its own).

    When ``cancel_event`` is ``None`` (e.g. the MCP server's ``contract_capture``,
    which never passes one) there is no other thread able to cancel a command
    that never exits on its own — so this still enforces ``_CAPTURE_TIMEOUT``
    (``PYMOLT_CAPTURE_TIMEOUT`` env var, default 1800s): on expiry the process is
    terminated/killed the same way, and ``ValueError`` is raised.

    ``cwd`` runs the command in the target project (default: the parent's cwd).
    The child's stdout+stderr are ALWAYS redirected (merged) to ``log_path`` —
    never inherited onto the parent tty. Returns
    the produced per-process JSONL paths and the command's exit code.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(bundle), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env["PYMOLT_TRACE_OUT"] = str(work / "trace-{pid}.jsonl")
    env.update(trace_env(
        target, backend, exclude, source, include_internal, privacy, sample_rate,
        impact_targets, deployment_id, request_id, correlation_id,
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
        proc = subprocess.Popen(
            command, env=env, cwd=str(cwd) if cwd is not None else None,
            stdout=log_file, stderr=subprocess.STDOUT,
        )
        if cancel_event is None:
            try:
                returncode = proc.wait(timeout=_CAPTURE_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                # Whatever the watcher had already flushed to work/trace-*.jsonl before the
                # kill is left on disk untouched — we just never reach the glob/return below
                # to hand it back to capture_trace() for merging on this (failed) call.
                raise ValueError(
                    f"capture timed out after {_CAPTURE_TIMEOUT}s: {command}"
                ) from None
        else:
            try:
                while proc.poll() is None:
                    if cancel_event.is_set():
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                        break
                    time.sleep(0.2)
            finally:
                if proc.poll() is None:
                    proc.kill()
            returncode = proc.poll()
    finally:
        if log_file is not None:
            log_file.close()
    return sorted(glob.glob(str(work / "trace-*.jsonl"))), returncode


def capture_trace_in_container(
    target: str, command: list[str], backend: str, bundle: Path, work: Path,
    container: str, workdir: str | None, exclude: str, source: str, include_internal: bool,
    privacy: str = "values", sample_rate: float = 1.0,
    impact_targets: list[str] | None = None,
    deployment_id: str | None = None,
    request_id: str | None = None,
    correlation_id: str | None = None,
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
    produced = sorted(glob.glob(str(pull / "**" / "trace-*.jsonl"), recursive=True))
    return produced, command_result.returncode


def _docker(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    res = run_command(["docker", *args], check=False)
    if check and res.returncode != 0:
        raise ValueError(f"docker {' '.join(args[:2])} failed: {res.stderr.strip()}")
    return res


def container_is_running(container_id: str) -> bool:
    """True if a container with this id/name is currently running (best-effort).

    Shared by the SetupPanel STALE check and ``resolve_capture_env`` below —
    one docker-liveness mechanism, not two.
    """
    import shutil

    if not shutil.which("docker"):
        return False
    try:
        res = run_command(
            ["docker", "ps", "-q", "--no-trunc", "--filter", f"id={container_id}"],
            check=False, timeout=5,
        )
        if res.stdout.strip():
            return True
        # id filter misses name-based configs; fall back to a name filter.
        res = run_command(
            ["docker", "ps", "-q", "--filter", f"name={container_id}"],
            check=False, timeout=5,
        )
        return bool(res.stdout.strip())
    except Exception:
        return False


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
                "note": f"baseline → container {config.container_id[:12]}",
            }
        return {
            "container": None, "workdir": None, "command_prefix": None,
            "note": (
                f"configured container {config.container_id[:12]} not running — "
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
        for candidate in (venv / "bin" / "python", venv / "Scripts" / "python.exe"):
            if candidate.exists():
                return {
                    "container": None, "workdir": None,
                    "command_prefix": [str(candidate.resolve()), "-m"],
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
            with open(f) as fh:
                for line in fh:
                    if line.strip():
                        out.write(line)
                        events += 1
    return events


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
) -> TraceCaptureResult:
    """Run ``command`` with the watcher injected; merge produced JSONL into ``out_path``.

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
    bundle = export_watcher(work / "bundle")

    command_log: str | None = None
    returncode: int | None = None
    if container:
        produced, returncode = capture_trace_in_container(
            target, command, backend, bundle, work,
            container, workdir, exclude, source, include_internal,
            privacy, sample_rate, impact_targets,
            deployment_id, request_id, correlation_id,
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
        )
        command_log = str(log_path) if log_path.exists() else None

    events = _merge_traces(produced, Path(out_path))
    runtime = _aggregate_runtime_metadata(produced)
    counters = runtime["counters"]
    runtime_backends = runtime["backends"]
    effective_backend = ",".join(runtime_backends) if runtime_backends else backend
    # Sidecars from older/interrupted watcher bundles may be absent. Written
    # events remain useful, but loss cannot then be claimed to be zero.
    metadata_complete = runtime["metadata_complete"]
    events_seen = (
        max(counters["events_seen"], events + counters["dropped_events"])
        if metadata_complete else None
    )
    dropped_events = counters["dropped_events"] if metadata_complete else None
    result = TraceCaptureResult(
        out_path=str(out_path), events=events, processes=len(produced),
        where=f"container:{container}" if container else "local",
        command_log=command_log, returncode=returncode,
        cancelled=bool(cancel_event is not None and cancel_event.is_set()),
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
    )
    metadata = {
        "schema_version": 1,
        "duration_seconds": result.duration_seconds,
        "deployment_id": deployment_id,
        "request_id": request_id,
        "correlation_id": correlation_id,
        "sample_rate": sample_rate,
        "backend": effective_backend,
        "impact_targets": impact_targets,
        "events": events,
        "events_seen": events_seen,
        "runtime_metadata_complete": metadata_complete,
        "counters": counters,
        "instrumentation_skipped": result.instrumentation_skipped,
        "processes": runtime["process_metadata"],
    }
    result.metadata_path = _write_trace_metadata(out_path, metadata)
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Named state — baseline / post_migration slots.
# ─────────────────────────────────────────────────────────────────────────────


def _state_path(project_dir: str | Path) -> Path:
    return Path(project_dir) / CONTRACT_STATE_PATH


def load_contract_state(project_dir: str | Path) -> ContractState:
    """The current capture state, or a fresh empty one if none is persisted yet."""
    return ContractState.load(_state_path(project_dir)) or ContractState()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def inspect_trace_artifact(path: str | Path) -> TraceArtifactQuality:
    """Validate that a trace exists, is parseable, and contains usable events."""
    trace = Path(path)
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


def _capture_validity(
    result: TraceCaptureResult, staged_path: Path,
) -> tuple[CaptureValidity, str | None]:
    if result.returncode not in (None, 0) and not result.cancelled:
        return CaptureValidity.COMMAND_FAILED, f"command exited with code {result.returncode}"
    if result.events == 0:
        return CaptureValidity.EMPTY, "capture produced zero events"
    quality = inspect_trace_artifact(staged_path)
    return quality.validity, quality.reason


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
    sample_rate: float = 1.0,
    impact_targets: list[str] | None = None,
    deployment_id: str | None = None,
    request_id: str | None = None,
    correlation_id: str | None = None,
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

    Never prompts; raises ``ValueError`` on bad input (unknown ``when``, no
    ``command``, or an unsupported ``mode`` for this function — see
    ``start_attached_capture`` for ``LIVE_ATTACH``).
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

    project_dir = Path(project_dir)
    if privacy is None:
        privacy = "values" if mode is CaptureMode.TEST_SUITE else "shape"
    if (
        impact_targets is None
        and when == "post_migration"
        and not os.environ.get("PYMOLT_TRACE_IMPACT_TARGETS")
    ):
        impact_targets = _applied_plan_impact_targets(project_dir)
    normalized_targets = _normalize_impact_targets(impact_targets)
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
        if deployment_id is not None:
            capture_kwargs["deployment_id"] = deployment_id
        if request_id is not None:
            capture_kwargs["request_id"] = request_id
        if correlation_id is not None:
            capture_kwargs["correlation_id"] = correlation_id
        result = capture_trace(target, command, staged_path, backend, **capture_kwargs)
    except BaseException:
        # A timeout/start failure may happen after the command log or a partial
        # trace was created.  They were never committed, so do not leave them
        # looking like a recoverable capture.
        for pending in (staged_path, staged_log, _trace_metadata_path(staged_path)):
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
    validity, validity_reason = _capture_validity(result, staged_path)
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
            for pending in (staged_path, staged_log):
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
        coverage_pct=coverage_pct, covered_sites=covered_sites, blind_sites=blind_sites,
        env_fingerprint=environment_fingerprint(project_dir),
        archived_previous=archived_previous,
        validity=validity, validity_reason=validity_reason,
        privacy_profile=privacy, sample_rate=sample_rate,
        duration_seconds=result.duration_seconds,
        deployment_id=result.deployment_id,
        request_id=result.request_id,
        correlation_id=result.correlation_id,
        backend=result.backend,
        impact_targets=result.impact_targets,
        metadata_path=result.metadata_path,
        events_seen=result.events_seen,
        dropped_events=result.dropped_events,
        sampling_dropped=result.sampling_dropped,
        backpressure_dropped=result.backpressure_dropped,
        write_failures=result.write_failures,
        sink_failures=result.sink_failures,
        instrumentation_skipped=result.instrumentation_skipped,
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
    if config.selected_manifest:
        manifest = project_dir / config.selected_manifest
        try:
            parts.append("content=" + hashlib.sha256(manifest.read_bytes()).hexdigest())
        except OSError:
            parts.append("content=<unreadable>")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


class AttachInstructions(BaseModel):
    """What to show the engineer for a LIVE_ATTACH capture: run this yourself."""

    command_hint: str   # a ready-to-copy `PYTHONPATH=... PYMOLT_TRACE_*=... <your command>` line
    out_path: str        # where the trace will accumulate as they exercise the app
    metadata_path: str | None = None


def pending_attach_path(project_dir: str | Path, when: str) -> Path:
    return Path(project_dir) / ".pymolt" / "contract_traces" / f".{when}.attach-pending.jsonl"


def start_attached_capture(
    project_dir: str | Path, when: str, *, target: str = "all", backend: str = "auto",
    privacy: str = "shape", sample_rate: float = 1.0,
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

    project_dir = Path(project_dir)
    watcher_env = trace_env(
        target, backend, "", "", False, privacy, sample_rate,
        impact_targets, deployment_id, request_id, correlation_id,
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

    exported = {"PYTHONPATH": str(bundle), **watcher_env, "PYMOLT_TRACE_OUT": str(out_path)}
    hint = " ".join(f"{key}={shlex.quote(value)}" for key, value in exported.items())
    hint += " <your command>"
    return AttachInstructions(
        command_hint=hint,
        out_path=str(out_path),
        metadata_path=str(_trace_metadata_path(out_path)),
    )


def poll_attached_capture(out_path: str | Path) -> int:
    """Cheap line count on the (append-only) JSONL — how many events so far."""
    path = Path(out_path)
    if not path.is_file():
        return 0
    with path.open() as f:
        return sum(1 for line in f if line.strip())


def finalize_attached_capture(
    project_dir: str | Path, when: str, out_path: str | Path, *,
    target: str = "all", privacy: str = "shape", sample_rate: float = 1.0,
    impact_targets: list[str] | None = None,
    deployment_id: str | None = None,
    request_id: str | None = None,
    correlation_id: str | None = None,
) -> ContractSlot:
    """Called when the engineer signals "I'm done" on a LIVE_ATTACH capture:
    reads whatever landed on disk and persists it as the named slot."""
    project_dir = Path(project_dir)
    out_path = Path(out_path)
    events = poll_attached_capture(out_path)
    metadata = _read_trace_metadata(out_path)
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
        except OSError as exc:
            logger.warning("could not move attached diagnostic metadata: %s", exc)
            metadata_path = None
        committed_path = diagnostic
    slot = ContractSlot(
        trace_path=str(committed_path), captured_at=_now(), mode=CaptureMode.LIVE_ATTACH,
        command=[], target=target, events=events, processes=1,
        env_fingerprint=environment_fingerprint(project_dir),
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

    report = build_contract_report(
        project_dir,
        trace=trace,
        against=against,
        probe_python=probe_python,
        changed_api_paths=changed_api_paths,
        comparator_profile=comparator_profile,
        custom_comparator=custom_comparator,
    )
    _stamp_staleness(project_dir, report, used_slots)
    _stamp_verdict(report, trace, against, used_slots)
    return report


def _stamp_verdict(
    report: ContractReport,
    trace: str | None,
    against: str | None,
    used_slots: list[tuple[str, object]],
) -> None:
    """Fold evidence into PASS/FAIL/INCONCLUSIVE without averaging uncertainty."""
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
        comparability.append("no baseline trace is available for a version diff")
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
        if baseline.sample_rate < 1 or post.sample_rate < 1:
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
    if report.diff_clean is False or report.probe_changed:
        report.verdict = VerificationVerdict.FAIL
        if report.diff_clean is False:
            report.verdict_reasons.append("behavioral boundary changes were detected")
        if report.probe_changed:
            report.verdict_reasons.append("sandbox replay changed behavior")
        return

    reasons: list[str] = []
    if report.diff is None:
        reasons.append("no before/after diff was produced")
    elif report.diff.get("skipped_opaque", 0):
        reasons.append("some boundary interactions were opaque or nondeterministic")
    if report.blind:
        reasons.append(f"{report.blind} static dependency symbol(s) were not exercised")
    if report.dynamic_only:
        reasons.append(f"{report.dynamic_only} observed symbol(s) are missing from the static map")
    if report.static_targets == 0:
        reasons.append("the static contract surface is empty")

    # Keep output stable and readable when two checks identify the same cause.
    report.verdict_reasons = list(dict.fromkeys(reasons))
    report.verdict = (
        VerificationVerdict.INCONCLUSIVE if report.verdict_reasons
        else VerificationVerdict.PASS
    )


def _stamp_staleness(project_dir: str | Path, report: ContractReport, used_slots) -> None:
    """Mark the report when its evidence predates the project's current state.

    A verdict built on a capture taken against a different manifest still *looks*
    authoritative — same tables, same percentages — which is exactly why it has
    to say so itself. Absence of a fingerprint is reported as unknown, never as
    fresh: a capture from before this check existed has not been vouched for.
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

    stale: list[str] = []
    unknown: list[str] = []
    for name, slot in used_slots:
        fingerprint = getattr(slot, "env_fingerprint", None)
        if fingerprint is None:
            unknown.append(name)
        elif fingerprint != current:
            stale.append(name)

    if stale:
        report.baseline_stale = True
        report.notes.append(
            f"STALE EVIDENCE: the {', '.join(stale)} capture was taken against a different "
            "manifest/environment than the one on disk now — this verdict describes the "
            "world as it was then. Re-capture to judge the current code."
        )
    elif not unknown:
        report.baseline_stale = False
    if unknown:
        report.notes.append(
            f"staleness unknown for the {', '.join(unknown)} capture "
            "(taken before pymolt stamped capture environments)."
        )
