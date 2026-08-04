"""UI-agnostic orchestration for the Contract (verify) phase.

Everything that touches subprocesses/PYTHONPATH/env-var injection to capture a
dynamic boundary trace lives here — not in ``interfaces/cli/verify_cmd.py`` —
so the TUI can drive the exact same capture the CLI does (the "interfaces are
thin, each phase is a service module" rule this repo follows everywhere else).

Two concerns:

* **Capture** — ``capture_trace`` runs an arbitrary command with the watcher
  injected (locally or in an already-running container) and merges the
  per-process JSONL it produces into one artifact. This already covers "run
  alongside your test suite" (the command is just ``pytest ...``) and "run my
  app for a while" (the command is ``python app.py``, optionally cancelled
  early via ``cancel_event`` — the guided TUI's "Stop & Collect"). For a
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

Nothing here prompts or prints — that's the CLI/TUI's job. Bad input raises
``ValueError`` with a message worth surfacing verbatim.
"""

from __future__ import annotations

import glob
import hashlib
import importlib.util
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from pymolt.adapters.subprocess_runner import run_command
from pymolt.ingestion.config import EnvConfig
from pymolt.verify.coverage import run_coverage_json
from pymolt.verify.export_watcher import export_watcher
from pymolt.verify.gaps import classify_gaps, scan_usage_sites
from pymolt.verify.models import CaptureMode, ContractSlot, ContractState
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
    # trace artifacts (never inherited onto the parent tty — that garbles the TUI).
    command_log: str | None = None
    # Exit code of the traced command (None for the container path / attach).
    returncode: int | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Capture — inject the watcher, run a command, merge the JSONL it writes.
# ─────────────────────────────────────────────────────────────────────────────


def trace_env(target: str, backend: str, exclude: str, source: str, include_internal: bool) -> dict:
    """The PYMOLT_TRACE_* variables that drive the watcher (shared by local + container)."""
    env = {"PYMOLT_TRACE_TARGET": target, "PYMOLT_TRACE_BACKEND": backend}
    if exclude:
        env["PYMOLT_TRACE_EXCLUDE"] = exclude
    if source:
        env["PYMOLT_TRACE_SOURCE"] = source
    if include_internal:
        env["PYMOLT_TRACE_INTERNAL"] = "1"
    return env


def capture_trace_local(
    target: str, command: list[str], backend: str, bundle: Path, work: Path,
    exclude: str, source: str, include_internal: bool,
    cancel_event: threading.Event | None = None,
    cwd: str | Path | None = None,
    log_path: Path | None = None,
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
    never inherited onto the parent tty, which under ``pymolt ui`` would write
    raw pytest output straight into the Textual screen and garble it. Returns
    the produced per-process JSONL paths and the command's exit code.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(bundle), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)
    env["PYMOLT_TRACE_OUT"] = str(work / "trace-{pid}.jsonl")
    env.update(trace_env(target, backend, exclude, source, include_internal))

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
) -> list[str]:
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
    for key, val in trace_env(target, backend, exclude, source, include_internal).items():
        exec_args += ["-e", f"{key}={val}"]
    if workdir:
        exec_args += ["-w", workdir]
    exec_args += [container, *command]
    _docker(exec_args, check=False)  # don't fail on the app/suite's own exit code

    pull = work / "pull"
    _docker(["cp", f"{container}:{trace_in}", str(pull)])
    # best-effort cleanup so the container is left exactly as found
    _docker(["exec", container, "rm", "-rf", bundle_in, trace_in], check=False)
    return sorted(glob.glob(str(pull / "**" / "trace-*.jsonl"), recursive=True))


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
    SUGGESTION for callers (e.g. the TUI) to prefill a command with — this
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


def capture_trace(
    target: str, command: list[str], out_path: str | Path, backend: str,
    container: str | None = None, workdir: str | None = None,
    exclude: str = "", source: str = "", include_internal: bool = False,
    cancel_event: threading.Event | None = None,
    cwd: str | Path | None = None,
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
    work = Path(tempfile.mkdtemp(prefix="pymolt-trace-"))
    bundle = export_watcher(work / "bundle")

    command_log: str | None = None
    returncode: int | None = None
    if container:
        produced = capture_trace_in_container(
            target, command, backend, bundle, work,
            container, workdir, exclude, source, include_internal,
        )
    else:
        # Keep the log next to the durable trace artifact (out_path), not the
        # ephemeral work tempdir, so the path stored on the slot stays valid.
        out_p = Path(out_path)
        log_path = out_p.parent / f"{out_p.stem}.command.log"
        produced, returncode = capture_trace_local(
            target, command, backend, bundle, work,
            exclude, source, include_internal, cancel_event=cancel_event,
            cwd=cwd, log_path=log_path,
        )
        command_log = str(log_path) if log_path.exists() else None

    events = _merge_traces(produced, Path(out_path))
    return TraceCaptureResult(
        out_path=str(out_path), events=events, processes=len(produced),
        where=f"container:{container}" if container else "local",
        command_log=command_log, returncode=returncode,
    )


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

    # No explicit container from the caller -> default from the project's saved
    # EnvConfig (baseline -> its configured container if still running;
    # post-migration -> no container, just a note/prefill suggestion). An
    # explicit container passed by the caller always wins.
    env_note = None
    if container is None:
        resolved = resolve_capture_env(project_dir, when)
        container = resolved["container"]
        if workdir is None:
            workdir = resolved["workdir"]
        env_note = resolved["note"]

    # Local commands run in the target project by default (not pymolt's cwd), so
    # a relative command like `pytest tests/` collects the target's tests.
    if cwd is None and not container:
        cwd = project_dir
    # Displace, never destroy: the recording being replaced can never be retaken.
    archived_previous = archive_existing_capture(project_dir, when)
    out_path = project_dir / ".pymolt" / "contract_traces" / f"{when}.jsonl"
    result = capture_trace(
        target, command, out_path, backend, container=container, workdir=workdir,
        exclude=exclude, source=source, include_internal=include_internal,
        cancel_event=cancel_event, cwd=cwd,
    )
    coverage_pct: float | None = None
    covered_sites = blind_sites = 0
    if mode == CaptureMode.TEST_SUITE:
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
    )
    state = load_contract_state(project_dir)
    setattr(state, when, slot)
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


def start_attached_capture(
    project_dir: str | Path, when: str, *, target: str = "all", backend: str = "auto",
) -> AttachInstructions:
    """Prepare (but do not launch) a LIVE_ATTACH capture: export the watcher bundle
    and pick a stable output path, returning the command the engineer runs THEMSELVES
    in their own terminal/devcontainer/server process."""
    if when not in ("baseline", "post_migration"):
        raise ValueError(f"when must be 'baseline' or 'post_migration', got {when!r}")

    project_dir = Path(project_dir)
    bundle_dir = project_dir / ".pymolt" / "contract_bundle"
    out_path = project_dir / ".pymolt" / "contract_traces" / f"{when}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Displace any previous recording now, before the engineer's own run starts
    # appending to this path — by --collect time it is too late to tell the two
    # apart.
    archive_existing_capture(project_dir, when)
    bundle = export_watcher(bundle_dir)

    hint = (
        f"PYTHONPATH={bundle} PYMOLT_TRACE_TARGET={target} "
        f"PYMOLT_TRACE_OUT={out_path} <your command>"
    )
    return AttachInstructions(command_hint=hint, out_path=str(out_path))


def poll_attached_capture(out_path: str | Path) -> int:
    """Cheap line count on the (append-only) JSONL — how many events so far."""
    path = Path(out_path)
    if not path.is_file():
        return 0
    with path.open() as f:
        return sum(1 for line in f if line.strip())


def finalize_attached_capture(
    project_dir: str | Path, when: str, out_path: str | Path, *,
    target: str = "all",
) -> ContractSlot:
    """Called when the engineer signals "I'm done" on a LIVE_ATTACH capture:
    reads whatever landed on disk and persists it as the named slot."""
    events = poll_attached_capture(out_path)
    slot = ContractSlot(
        trace_path=str(out_path), captured_at=_now(), mode=CaptureMode.LIVE_ATTACH,
        command=[], target=target, events=events, processes=1,
        env_fingerprint=environment_fingerprint(project_dir),
    )
    state = load_contract_state(project_dir)
    setattr(state, when, slot)
    state.save(_state_path(project_dir))
    return slot


def build_contract_report_from_state(
    project_dir: str | Path, *,
    trace_override: str | None = None, against_override: str | None = None,
    probe_python: str | None = None,
) -> ContractReport:
    """The unified report, sourcing trace/against from the persisted state when
    not explicitly overridden.

    Resolution: explicit overrides always win. Otherwise — both slots captured
    -> trace=post_migration, against=baseline (this is what answers "what
    changed across the migration"); only one captured -> that one as trace,
    no diff; neither -> trace=None (today's all-BLIND behavior, unchanged).
    """
    state = load_contract_state(project_dir)

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
        project_dir, trace=trace, against=against, probe_python=probe_python,
    )
    _stamp_staleness(project_dir, report, used_slots)
    return report


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
