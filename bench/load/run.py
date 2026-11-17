#!/usr/bin/env python3
"""PyMolt phase-2 load-test harness (see bench/load/README.md).

    python3 bench/load/run.py setup
    python3 bench/load/run.py nocap   --duration 60 --rate 0
    python3 bench/load/run.py capture --duration 60 --rate 0 [--env K=V ...]
                                      [-- <extra pymolt capture args>]
    python3 bench/load/run.py report  <run-dir | session-dir> [--nocap RUN_DIR] [--no-fold]
    python3 bench/load/run.py gate    [--duration 60] [--paced-rate 200]

Stdlib only (Python 3.9+). Every run gets a fresh project dir (a copy of service/) so its
sqlite db, .pymolt/ state and trace belong to that run alone; results land in
bench/load/results/<UTC timestamp>-<label>/<run-name>/ (gitignored).
"""
from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
VENV = HERE / ".venv"
SVC_PY = VENV / "bin" / "python"
# Which pymolt drives capture/fold: <repo>/.venv by default; BENCH_PYMOLT or --pymolt points
# at another install (e.g. a pristine snapshot of a commit while pymolt/ is being edited).
PYMOLT = Path(os.environ.get("BENCH_PYMOLT") or REPO / ".venv" / "bin" / "pymolt")
RESULTS = HERE / "results"
SERVICE_SRC = HERE / "service"
REQUIREMENTS = HERE / "requirements.txt"
SETUP_MARKER = VENV / ".bench-setup.json"
PYTHON_VERSION = "3.12"

SAT_THREADS = 16
PACED_THREADS = 64
LOAD_SEED = 42
SERVICE_SEED = 1234
WARMUP_S = 2.0
READY_TIMEOUT_S = 180.0
STOP_TIMEOUT_S = 60.0
FINALIZE_TIMEOUT_S = 900.0
FOLD_TIMEOUT_S = 1800.0
COOLDOWN_S = 3.0
SAMPLE_EVERY_S = 2.0
# Steady state (capture runs): long-run captures start in discover mode and switch to watch
# once no new dependency symbol appeared for PYMOLT_TRACE_DISCOVER_QUIET s (capped at
# PYMOLT_TRACE_DISCOVER_MAX s). The measured window starts STEADY_SETTLE_S after the switch.
DISCOVER_QUIET_S = 30.0
DISCOVER_MAX_S = 300.0
STEADY_SETTLE_S = 2.0
STEADY_MARGIN_S = 30.0      # fixed fallback = quiet + margin; max wait = discover max + margin
LIVE_META_GRACE_S = 20.0    # no live sidecar metadata by then -> the fixed fallback

GB = 1_000_000_000
MIB = 1024 * 1024
GATE = {
    "min_startup_ratio": 0.50,  # saturated rps over the first 30 s >= 50% of nocap
    "min_recall": 0.95,         # (sym, site) seen by default mode vs a discover reference
    "max_rps_loss": 0.20,       # saturated capture rps >= 80% of no-capture
    "max_dropped": 0,           # dropped calls/events across every capture run
    "max_bytes_per_day": 1 * GB,
    "max_p99_ratio": 2.0,       # paced p99 (capture) <= 2x paced p99 (no capture)
    "max_fold_rss": 500 * MIB,  # host `verify` peak RSS
}

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)  # noqa: UP017 - run.py stays Python 3.9-compatible


# ── child-process hygiene ────────────────────────────────────────────────────

_CHILDREN: list[subprocess.Popen] = []


def _spawn(cmd, *, cwd=None, env=None, log: Path | None = None) -> subprocess.Popen:
    """Start a child in its own session/process group, output appended to ``log``."""
    out = open(log, "ab") if log is not None else subprocess.DEVNULL
    try:
        proc = subprocess.Popen(
            [str(c) for c in cmd], cwd=str(cwd) if cwd else None, env=env,
            stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        if log is not None:
            out.close()
    _CHILDREN.append(proc)
    return proc


def _killpg(pgid: int, sig: int) -> bool:
    try:
        os.killpg(pgid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _stop_group(proc: subprocess.Popen, grace: float = 5.0) -> None:
    """SIGTERM the child's whole group, SIGKILL after ``grace``; reap the leader."""
    if proc.poll() is None:
        _killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
    # The leader may be gone while members (e.g. the service under pymolt) live on.
    _killpg(proc.pid, signal.SIGKILL)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def _cleanup() -> None:
    while _CHILDREN:
        _stop_group(_CHILDREN.pop(), grace=3.0)


atexit.register(_cleanup)


def _on_sigterm(signum, _frame):
    raise SystemExit(128 + signum)


signal.signal(signal.SIGTERM, _on_sigterm)


def _pid_alive(pid: int) -> bool:
    """kill(0) probe. A zombie still answers; callers reap their own children via poll()."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _tail(path: Path, n: int = 25) -> str:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(lines[-n:])


def _wait_ready(path: Path, watch: list[tuple[str, subprocess.Popen, Path]],
                timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            try:
                return json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                pass
        for name, proc, log in watch:
            if proc.poll() is not None:
                raise RuntimeError(f"{name} exited ({proc.returncode}) before it was ready:\n"
                                   f"{_tail(log)}")
        time.sleep(0.1)
    raise RuntimeError(f"timed out after {timeout}s waiting for {path.name}")


# ── environment / provenance ─────────────────────────────────────────────────


def _clean_env(overrides: dict[str, str]) -> dict[str, str]:
    drop = {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PYTHONSTARTUP", "PYTHONINSPECT"}
    env = {k: v for k, v in os.environ.items()
           if k not in drop and not k.startswith(("PYMOLT_", "BENCH_"))}
    env.update(overrides)
    return env


def _run_text(cmd, cwd=None, timeout=30) -> str:
    try:
        return subprocess.run([str(c) for c in cmd], cwd=cwd, capture_output=True, text=True,
                              timeout=timeout).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _pymolt_source() -> dict:
    """Where the pymolt that runs captures imports from, and which commit that tree is."""
    module = _run_text([PYMOLT.parent / "python", "-c",
                        "import pymolt, os; print(os.path.dirname(pymolt.__file__))"], cwd="/")
    root = Path(module).parent if module else None
    head_file = root / ".git-head" if root else None
    if root and head_file and head_file.is_file():  # snapshot made by `git archive`
        return {"pymolt_exe": str(PYMOLT), "pymolt_root": str(root), "snapshot": True,
                "git_head": head_file.read_text().strip()}
    return {"pymolt_exe": str(PYMOLT), "pymolt_root": str(root) if root else None,
            "snapshot": False}


def _provenance() -> dict:
    src = _pymolt_source()
    git_root = Path(src["pymolt_root"]) if src.get("pymolt_root") else REPO
    in_repo = not src.get("snapshot") and (git_root / ".git").exists()
    dirty: list[str] = []
    tree_hash = None
    if in_repo:
        dirty = [line for line in _run_text(
            ["git", "status", "--porcelain", "--", "pymolt"], cwd=git_root).splitlines() if line]
        if dirty:
            h = hashlib.sha1(_run_text(["git", "diff", "HEAD", "--", "pymolt"], cwd=git_root,
                                       timeout=60).encode())
            for rel in sorted(line[3:] for line in dirty if line.startswith("??")):
                try:
                    h.update((git_root / rel).read_bytes())
                except OSError:
                    pass
            tree_hash = h.hexdigest()[:12]
    return {
        **src,
        "git_head": src.get("git_head") or _run_text(["git", "rev-parse", "HEAD"], cwd=git_root),
        "git_branch": _run_text(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=REPO),
        "pymolt_dirty_files": dirty,
        "pymolt_tree_hash": tree_hash,
        "pymolt_version": _run_text([PYMOLT, "--version"]),
        "service_python": _run_text([SVC_PY, "-c", "import sys; print(sys.version.split()[0])"]),
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpus": os.cpu_count(),
            "cpu": _run_text(["sysctl", "-n", "machdep.cpu.brand_string"])
            if sys.platform == "darwin" else platform.processor(),
            "loadavg": [round(x, 2) for x in os.getloadavg()],
        },
    }


def _host_load() -> dict:
    """What else is burning CPU right now (ps %CPU is a short decaying average)."""
    own = {os.getpid()} | {p.pid for p in _CHILDREN}
    rows = []
    for line in _run_text(["ps", "-Ao", "pcpu=,pid=,ppid=,comm="]).splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            cpu, pid, ppid = float(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        if pid in own or ppid in own:
            continue
        rows.append((cpu, pid, parts[3].rsplit("/", 1)[-1][:40]))
    rows.sort(reverse=True)
    return {
        "loadavg": [round(x, 2) for x in os.getloadavg()],
        "others_cpu_pct": round(sum(r[0] for r in rows), 1),
        "top": [{"cpu": r[0], "pid": r[1], "comm": r[2]} for r in rows[:5]],
    }


def _wait_quiet(max_wait: float, threshold: float = 150.0) -> dict:
    """Wait (up to ``max_wait`` s) until other processes use < ``threshold`` % CPU twice."""
    started = time.monotonic()
    calm = 0
    load = _host_load()
    while max_wait > 0 and time.monotonic() - started < max_wait:
        calm = calm + 1 if load["others_cpu_pct"] < threshold else 0
        if calm >= 2:
            break
        time.sleep(5)
        load = _host_load()
    load["waited_s"] = round(time.monotonic() - started, 1)
    return load


def _session_dir(label: str, given: str | None) -> Path:
    if given:
        path = Path(given).resolve()
    else:
        stamp = _utcnow().strftime("%Y%m%dT%H%M%SZ")
        path = RESULTS / f"{stamp}-{label}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _require_setup() -> None:
    if not SVC_PY.exists():
        sys.exit(f"service venv missing: run `python3 {HERE / 'run.py'} setup` first")


def _require_pymolt() -> None:
    if not PYMOLT.exists():
        sys.exit(f"pymolt not found at {PYMOLT} (create the repo venv: `uv sync` in {REPO})")


# ── setup ────────────────────────────────────────────────────────────────────


def cmd_setup(_args) -> int:
    uv = shutil.which("uv")
    if not uv:
        sys.exit("uv is not on PATH (https://docs.astral.sh/uv/)")
    req_hash = hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()
    try:
        marker = json.loads(SETUP_MARKER.read_text())
    except (OSError, json.JSONDecodeError):
        marker = {}
    current = _run_text([SVC_PY, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"]) \
        if SVC_PY.exists() else ""
    if current and current != PYTHON_VERSION:
        print(f"service venv is Python {current}, want {PYTHON_VERSION}: recreating")
        shutil.rmtree(VENV)
        current = ""
    if not current:
        subprocess.run([uv, "venv", str(VENV), "--python", PYTHON_VERSION, "-q"], check=True)
    if marker.get("requirements_sha256") != req_hash or not current:
        subprocess.run([uv, "pip", "sync", "-q", "--python", str(SVC_PY), str(REQUIREMENTS)],
                       check=True)
    versions = _run_text([SVC_PY, "-c", (
        "import importlib.metadata as m, sys; print(sys.version.split()[0], *("
        "f'{d}=={m.version(d)}' for d in ('flask','sqlalchemy','pydantic','requests',"
        "'waitress')))")])
    if not versions:
        sys.exit("service venv is broken: imports failed")
    SETUP_MARKER.write_text(json.dumps({"requirements_sha256": req_hash, "versions": versions}))
    print(f"service venv ready: {VENV}\n  python {versions}")
    if not PYMOLT.exists():
        print(f"warning: {PYMOLT} not found; capture/report/gate need the repo venv")
    return 0


# ── one run ──────────────────────────────────────────────────────────────────


def _out_files(svc_dir: Path) -> list[dict]:
    """Everything the capture left in the project's .pymolt/, minus logs and bookkeeping."""
    root = svc_dir / ".pymolt"
    skip_names = {"contract_state.json", "env_config.json"}
    files = []
    if root.is_dir():
        for p in sorted(root.rglob("*")):
            rel = p.relative_to(root)
            if not p.is_file() or p.name in skip_names or p.name.endswith(".log"):
                continue
            if rel.parts and rel.parts[0] == "contract_bundle":
                continue
            files.append({"path": str(rel), "bytes": p.stat().st_size})
    return files


def _live_bytes(tmp_dir: Path, svc_dir: Path) -> int:
    """Bytes the watcher has written so far (pymolt's per-capture work dir + the project)."""
    total = 0
    roots = list(tmp_dir.glob("pymolt-trace-*")) if tmp_dir.is_dir() else []
    roots.append(svc_dir / ".pymolt" / "contract_traces")
    for root in roots:
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            try:
                rel = p.relative_to(root)
                if rel.parts[0] == "bundle" or p.name.endswith(".log") or not p.is_file():
                    continue
                total += p.stat().st_size
            except (OSError, IndexError):
                continue
    return total


def _first_key(obj, key: str, depth: int = 0):
    """First non-None value of ``key`` anywhere in nested dicts/lists (layouts vary)."""
    if depth > 6:
        return None
    if isinstance(obj, dict):
        if obj.get(key) is not None:
            return obj[key]
        values = list(obj.values())
    elif isinstance(obj, list):
        values = obj
    else:
        return None
    for value in values:
        found = _first_key(value, key, depth + 1)
        if found is not None:
            return found
    return None


def _read_header(path: Path) -> dict | None:
    """A live sidecar's metadata: a whole ``.meta.json``, or a facts snapshot's first line
    (the snapshot writes its header keys on line one: ``{...,`` then the facts)."""
    try:
        if path.name.endswith(".meta.json"):
            data = json.loads(path.read_text())
            return data if isinstance(data, dict) else None
        with open(path) as fh:
            line = fh.readline(1 << 20).rstrip().rstrip(",")
        return json.loads(line + "}") if line.startswith("{") else None
    except (OSError, ValueError):
        return None


def _live_metadata(tmp_dir: Path, svc_dir: Path) -> dict | None:
    """Metadata the watcher has published so far (facts snapshots are rewritten every flush
    interval; the JSONL sink only writes its .meta.json at exit). Prefers one that records
    the discover -> watch switch."""
    roots = list(tmp_dir.glob("pymolt-trace-*")) if tmp_dir.is_dir() else []
    roots.append(svc_dir / ".pymolt" / "contract_traces")
    first = None
    for root in roots:
        if not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            try:
                if p.relative_to(root).parts[0] == "bundle" or not p.is_file():
                    continue
            except IndexError:
                continue
            if not p.name.endswith((".facts.json", ".meta.json")):
                continue
            head = _read_header(p)
            if not head:
                continue
            head["_source"] = p.name
            if _first_key(head, "switched_at") is not None:
                return head
            first = first or head
    return first


_SWITCH_KEYS = ("mode", "requested_mode", "switched_at", "switch_reason", "discovered",
                "discover_quiet", "discover_max", "started_at")


def _switch_info(meta: dict | None) -> dict:
    info = {k: _first_key(meta or {}, k) for k in _SWITCH_KEYS}
    for k in ("switched_at", "started_at"):
        if not isinstance(info[k], (int, float)):
            info[k] = None
    return info


def _bytes_at(samples: list, wall: float | None) -> int | None:
    """Live output bytes at wall time ``wall``, interpolated from (rel, bytes, wall) samples."""
    pts = [(x[2], x[1]) for x in samples if len(x) >= 3]
    if wall is None or not pts:
        return None
    if wall <= pts[0][0]:
        return pts[0][1]
    for (w0, b0), (w1, b1) in zip(pts, pts[1:]):
        if w0 <= wall <= w1:
            return int(b0 + (b1 - b0) * ((wall - w0) / (w1 - w0) if w1 > w0 else 0))
    return pts[-1][1]


def _run_name(session: Path, kind: str, rate: float) -> str:
    base = f"{kind}-{'sat' if rate == 0 else f'paced{rate:g}'}"
    name, n = base, 2
    while (session / name).exists():
        name, n = f"{base}-{n}", n + 1
    return name


def run_one(kind: str, session: Path, *, duration: float, rate: float, threads: int | None,
            seed: int = LOAD_SEED, warmup: float = WARMUP_S, env_overrides: dict | None = None,
            capture_args: list[str] | None = None, name: str | None = None,
            mix: str | None = None, wait_quiet: float = 0.0,
            steady: str | None = None) -> Path:
    """Run the service bare (nocap), under a no-op profile hook (hook, a control) or under
    `pymolt capture` (capture), and drive load.

    ``steady`` (capture only): None/"off" measures right after the short warmup; "auto"
    keeps the load running until the watcher has switched discover -> watch (live sidecar
    metadata; a fixed quiet + margin fallback when there is none) and measures after that;
    a number is a fixed warmup in seconds."""
    assert kind in ("nocap", "hook", "capture")
    _require_setup()
    if kind == "capture":
        _require_pymolt()
    threads = threads or (SAT_THREADS if rate == 0 else PACED_THREADS)
    env_overrides = dict(env_overrides or {})
    capture_args = list(capture_args or [])
    steady_cfg: dict | None = None
    if kind == "capture" and steady not in (None, "", "off", "0"):
        quiet = float(env_overrides.get("PYMOLT_TRACE_DISCOVER_QUIET", DISCOVER_QUIET_S))
        dmax = float(env_overrides.get("PYMOLT_TRACE_DISCOVER_MAX", DISCOVER_MAX_S))
        if steady == "auto":
            steady_cfg = {"mode": "auto", "quiet_s": quiet, "discover_max_s": dmax,
                          "fallback_s": quiet + STEADY_MARGIN_S,
                          "max_warmup_s": dmax + STEADY_MARGIN_S, "settle_s": STEADY_SETTLE_S}
        else:
            steady_cfg = {"mode": "fixed", "warmup_s": float(steady)}
    max_warmup = (steady_cfg["max_warmup_s"] if steady_cfg and steady_cfg["mode"] == "auto"
                  else max(warmup, float(steady_cfg["warmup_s"]) if steady_cfg else warmup))
    run_dir = session / (name or _run_name(session, kind, rate))
    run_dir.mkdir(parents=True)
    svc_dir = run_dir / "svc"
    svc_dir.mkdir()
    shutil.copy2(SERVICE_SRC / "app.py", svc_dir / "app.py")
    shutil.copy2(HERE / "requirements.in", svc_dir / "requirements.txt")
    # `pymolt capture` refuses to run before setup: the capture records the
    # environment it was taken in.
    (svc_dir / ".pymolt").mkdir()
    (svc_dir / ".pymolt" / "env_config.json").write_text(json.dumps({
        "selected_manifest": "requirements.txt", "selected_tool": "uv",
        "base_python": PYTHON_VERSION, "target_python": PYTHON_VERSION,
    }))
    tmp_dir = run_dir / "tmp"
    tmp_dir.mkdir()

    record: dict = {
        "schema": 1,
        "kind": kind,
        "name": run_dir.name,
        "started_at": _utcnow().isoformat(timespec="seconds"),
        "config": {
            "duration_s": duration, "rate": rate, "threads": threads, "seed": seed,
            "warmup_s": warmup, "service_seed": SERVICE_SEED, "service_threads": 8,
            "env": env_overrides, "capture_args": capture_args, "mix": mix,
            "steady": steady_cfg,
        },
        "provenance": _provenance(),
    }
    record["host_load"] = _wait_quiet(wait_quiet)
    busy = record["host_load"]["others_cpu_pct"]
    print(f"[{run_dir.name}] {kind} rate={rate:g} threads={threads} "
          f"duration={duration:g}s (other processes {busy:.0f}% CPU) ...", flush=True)
    t: dict[str, float] = {}
    procs: list[subprocess.Popen] = []
    try:
        stub = _spawn([SVC_PY, HERE / "stub_upstream.py", "--ready-file", run_dir / "stub.ready"],
                      log=run_dir / "stub.log")
        procs.append(stub)
        stub_info = _wait_ready(run_dir / "stub.ready", [("stub", stub, run_dir / "stub.log")],
                                30)
        env = _clean_env({
            "BENCH_READY_FILE": str(run_dir / "svc.ready"),
            "BENCH_UPSTREAM": f"http://127.0.0.1:{stub_info['port']}",
            "BENCH_SEED": str(SERVICE_SEED),
            "BENCH_THREADS": "8",
            "BENCH_DB": "orders.db",
        })
        if kind == "capture":
            env["TMPDIR"] = str(tmp_dir)  # pymolt's work dir becomes observable + contained
            env["PYMOLT_CAPTURE_TIMEOUT"] = str(int(max(1800, max_warmup + duration + 900)))
        env.update(env_overrides)

        t["spawn"] = time.monotonic()
        wall = {"spawn": time.time()}
        record["wall"] = wall
        if kind == "hook":
            env["PYTHONPATH"] = str(HERE / "control")
        if kind in ("nocap", "hook"):
            log = run_dir / "service.log"
            cmd = [SVC_PY, "app.py"]
        else:
            log = run_dir / "capture.log"
            cmd = [PYMOLT, "capture", "baseline", "--mode", "command",
                   "--project-dir", svc_dir, "--force", *capture_args, "--", SVC_PY, "app.py"]
        record["command"] = [str(c) for c in cmd]
        # The load generator starts together with the service: it builds its request
        # sequence, waits for the ready file and starts the load the moment the service
        # answers, so the per-second counts show the startup as the clients see it.
        go_file = run_dir / "measure.go"
        lg_cmd = [SVC_PY, HERE / "loadgen.py", f"{duration:g}", str(threads), f"{rate:g}",
                  "--ready-file", run_dir / "svc.ready",
                  "--ready-timeout", f"{READY_TIMEOUT_S:g}", "--seed", str(seed),
                  "--out", run_dir / "loadgen.json"]
        if steady_cfg and steady_cfg["mode"] == "auto":
            lg_cmd += ["--warmup", f"{warmup:g}", "--warmup-until", go_file,
                       "--max-warmup", f"{max_warmup:g}"]
        else:
            lg_cmd += ["--warmup", f"{max_warmup:g}"]
        if mix:
            lg_cmd += ["--mix", mix]
        record["load_avg_before"] = [round(x, 2) for x in os.getloadavg()]
        main = _spawn(cmd, cwd=svc_dir, env=env, log=log)
        procs.append(main)
        t["load_start"] = time.monotonic()
        lg_proc = _spawn(lg_cmd, log=run_dir / "loadgen.log")
        procs.append(lg_proc)
        svc_info = _wait_ready(run_dir / "svc.ready",
                               [(kind, main, log), ("loadgen", lg_proc, run_dir / "loadgen.log")],
                               READY_TIMEOUT_S)
        t["ready"] = time.monotonic()
        wall["ready"] = time.time()
        svc_pid = int(svc_info["pid"])
        if os.getpgid(svc_pid) != main.pid:
            raise RuntimeError(f"service pid {svc_pid} is not in our process group")
        record["service"] = svc_info
        if kind == "capture" and not svc_info.get("watcher_modules"):
            print(f"[{run_dir.name}] warning: watcher modules not loaded in the service",
                  flush=True)

        # Drive load, sampling the watcher's live output meanwhile.
        samples: list[tuple[float, int, float]] = []
        stop_sampling = threading.Event()

        def sampler() -> None:
            while True:
                samples.append((round(time.monotonic() - t["ready"], 2),
                                _live_bytes(tmp_dir, svc_dir), round(time.time(), 3)))
                if stop_sampling.wait(SAMPLE_EVERY_S):
                    return

        # Steady state: tell the load generator when the measured window may start.
        steady_state: dict = {}

        def steady_watcher() -> None:
            started = time.time()
            fallback_at = None
            while not stop_sampling.is_set():
                now = time.time()
                meta = _live_metadata(tmp_dir, svc_dir)
                if meta is not None:
                    fallback_at = None
                    info = _switch_info(meta)
                    steady_state["live_source"] = meta.get("_source")
                    if info["mode"] == "incremental":
                        # Incremental mode has no discover window (switched_at = start),
                        # but it settles: its startup-only instrumentation turns off once
                        # nothing new has been armed for incremental_settle seconds. Steady
                        # state begins after that, unless settling is disabled (0).
                        settle_cfg = _first_key(meta, "incremental_settle")
                        settled = _first_key(meta, "settled_at")
                        if isinstance(settled, (int, float)):
                            steady_state.update(signal="settle", settled_at=settled,
                                                switched_at=info["switched_at"])
                            target = settled + STEADY_SETTLE_S
                            while time.time() < target and not stop_sampling.is_set():
                                time.sleep(0.05)
                            break
                        if settle_cfg not in (0, 0.0, "0") and \
                                now - started < float(settle_cfg or 30) + 90:
                            stop_sampling.wait(1.0)
                            continue
                    if info["switched_at"] is not None:
                        steady_state.update(signal="switch", switch_reason=info["switch_reason"],
                                            switched_at=info["switched_at"])
                        target = info["switched_at"] + STEADY_SETTLE_S
                        while time.time() < target and not stop_sampling.is_set():
                            time.sleep(0.05)
                        break
                    if info["mode"] and info["mode"] != "auto":
                        steady_state["signal"] = f"no switch (mode={info['mode']})"
                        break
                elif "live_source" not in steady_state and fallback_at is None \
                        and now - started >= LIVE_META_GRACE_S:
                    fallback_at = started + steady_cfg["fallback_s"]
                    steady_state["signal"] = "fixed fallback (no live metadata)"
                if fallback_at is not None and now >= fallback_at:
                    break
                stop_sampling.wait(1.0)
            if not stop_sampling.is_set():
                go_file.touch()
                steady_state["go_wall"] = round(time.time(), 3)

        helpers = []
        if kind == "capture":
            helpers.append(threading.Thread(target=sampler, daemon=True))
            if steady_cfg and steady_cfg["mode"] == "auto":
                helpers.append(threading.Thread(target=steady_watcher, daemon=True))
        for helper in helpers:
            helper.start()
        if steady_cfg:
            print(f"[{run_dir.name}] steady state: {steady_cfg['mode']} warmup (up to "
                  f"{max_warmup:g} s) before the {duration:g} s window", flush=True)
        try:
            lg_rc = lg_proc.wait(timeout=max_warmup + duration + 240)
        except subprocess.TimeoutExpired:
            raise RuntimeError("loadgen did not finish in time") from None
        t["load_end"] = time.monotonic()
        stop_sampling.set()
        for helper in helpers:
            helper.join()
        if steady_cfg:
            record["steady"] = steady_state
        if lg_rc != 0:
            raise RuntimeError(f"loadgen failed ({lg_rc}):\n"
                               f"{_tail(run_dir / 'loadgen.log')}")
        record["load"] = json.loads((run_dir / "loadgen.json").read_text())

        # Let a lagging writer drain before stopping, so "streamed" bytes are not
        # misattributed to shutdown. Idle = growth below 64 KiB/s or 2% of the load rate.
        if kind == "capture":
            live_ready = samples[0][1] if samples else 0
            live = _live_bytes(tmp_dir, svc_dir)
            load_rate = max(0.0, (live - live_ready) / max(1e-9, t["load_end"] - t["ready"]))
            idle = max(64 * 1024, 0.02 * load_rate)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                time.sleep(1.0)
                now_bytes = _live_bytes(tmp_dir, svc_dir)
                grew, live = now_bytes - live, now_bytes
                if grew < idle:
                    break
            t["drained"] = time.monotonic()
            record["live_output"] = {
                "samples": samples,
                "at_ready": live_ready,
                "at_drained": live,
                "drain_s": round(t["drained"] - t["load_end"], 2),
            }

        # Stop: SIGINT to the service process only, never to pymolt.
        def service_gone() -> bool:
            if main.poll() is not None:  # nocap/hook: the service is our child (reaped here)
                return True
            # capture: the service is pymolt's child; pymolt reaps it within ~50 ms.
            return kind == "capture" and not _pid_alive(svc_pid)

        t["sigint"] = time.monotonic()
        os.kill(svc_pid, signal.SIGINT)
        stop_how = "sigint"
        for sig, wait_s in ((None, STOP_TIMEOUT_S), (signal.SIGTERM, 15), (signal.SIGKILL, 10)):
            if sig is not None:
                stop_how = signal.Signals(sig).name.lower()
                try:
                    os.kill(svc_pid, sig)
                except ProcessLookupError:
                    pass
            end = time.monotonic() + wait_s
            while time.monotonic() < end and not service_gone():
                time.sleep(0.05)
            if service_gone():
                break
        t["service_exit"] = time.monotonic()
        record["service_stop"] = stop_how
        try:
            main.wait(timeout=FINALIZE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"{kind} did not exit {FINALIZE_TIMEOUT_S}s after the service")
        t["exit"] = time.monotonic()
        record["returncode"] = main.returncode
        record["timings_s"] = {
            "startup": round(t["ready"] - t["spawn"], 3),
            "load": round(t["load_end"] - t["load_start"], 3),
            "service_stop": round(t["service_exit"] - t["sigint"], 3),
            "finalize": round(t["exit"] - t["service_exit"], 3),
            "total": round(t["exit"] - t["spawn"], 3),
        }
        if kind == "capture":
            record["capture"] = _collect_capture(run_dir, svc_dir, record)
    except Exception as exc:  # noqa: BLE001 - record the failure, keep the harness alive
        record["error"] = f"{type(exc).__name__}: {exc}"
        print(f"[{run_dir.name}] ERROR {record['error']}", flush=True)
    finally:
        for proc in reversed(procs):
            _stop_group(proc)
            if proc in _CHILDREN:
                _CHILDREN.remove(proc)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        (run_dir / "run.json").write_text(json.dumps(record, indent=2) + "\n")
    _print_run_line(record)
    return run_dir


def _collect_capture(run_dir: Path, svc_dir: Path, record: dict) -> dict:
    state_path = svc_dir / ".pymolt" / "contract_state.json"
    try:
        state = json.loads(state_path.read_text())
    except (OSError, json.JSONDecodeError):
        state = {}
    slot = state.get("baseline") or {}
    meta: dict = {}
    trace_path = slot.get("trace_path")
    if trace_path and not os.path.isabs(trace_path):
        # contract_state.json stores project-relative paths (since Wave G)
        trace_path = str(svc_dir / trace_path)
    if trace_path:
        try:
            meta = json.loads(Path(trace_path + ".meta.json").read_text())
        except (OSError, json.JSONDecodeError):
            meta = {}
    counters = meta.get("counters") or {}
    files = _out_files(svc_dir)
    out_bytes = sum(f["bytes"] for f in files)
    trace_bytes = Path(trace_path).stat().st_size if trace_path and Path(trace_path).is_file() \
        else 0
    load = record.get("load") or {}
    load_s = load.get("wall_s") or record["timings_s"]["load"]
    live = record.get("live_output", {})
    samples = live.get("samples") or []
    at_ready = int(live.get("at_ready", 0))
    at_drained = int(live.get("at_drained", 0))
    at_mark = _bytes_at(samples, load.get("mark_wall"))
    at_mark = at_ready if at_mark is None else min(max(at_mark, at_ready), at_drained)
    window_s = float(load.get("duration_s") or load_s)
    in_window = max(0, at_drained - at_mark)  # incl. a lagging writer's backlog, drained
    at_exit = max(0, out_bytes - at_drained)
    day = 86400.0
    dropped = counters.get("dropped_events")
    if dropped is None and meta:
        dropped = meta.get("dropped_events")
    calls_key = next((k for k in ("calls_counted", "calls_seen", "calls", "tallied")
                      if isinstance(counters.get(k), int)), "events_seen")

    # Discover window: when the switch happened, and how the service did until then.
    info = _switch_info(meta)
    steady = record.get("steady") or {}
    if info["switched_at"] is None and steady.get("switched_at") is not None:
        info["switched_at"] = steady["switched_at"]
    discover = {k: info[k] for k in ("mode", "requested_mode", "switch_reason", "discovered",
                                     "discover_quiet", "discover_max", "switched_at")}
    discover.update({
        "steady_signal": steady.get("signal"),
        "warmup_s": load.get("warmup_s"),
        "warmup_end": load.get("warmup_end"),
        "steady_state": None,
    })
    sw = info["switched_at"]
    if sw is not None:
        tracer_start = info["started_at"] or (record.get("wall") or {}).get("spawn")
        if tracer_start:
            discover["discover_s"] = round(sw - tracer_start, 2)
        t0w = load.get("t0_wall")
        if t0w is not None:
            rel = sw - t0w
            discover["switch_after_load_start_s"] = round(rel, 2)
            per_second = load.get("per_second") or []
            if rel >= 1 and per_second:
                whole = int(rel)
                n = sum(per_second[:whole])
                if whole < len(per_second):
                    n += per_second[whole] * (rel - whole)
                discover["discover_rps"] = round(n / rel, 2)
        if load.get("mark_wall") is not None:
            discover["steady_state"] = load["mark_wall"] >= sw
        at_switch = _bytes_at(samples, sw)
        if at_switch is not None:
            discover["discover_bytes"] = max(0, at_switch - at_ready)
    return {
        "validity": slot.get("validity"),
        "validity_reason": slot.get("validity_reason"),
        "backend": meta.get("backend"),
        "runtime_metadata_complete": meta.get("runtime_metadata_complete"),
        "events_written": meta.get("events", slot.get("events")),
        "events_seen": meta.get("events_seen"),
        "calls_counted": counters.get(calls_key),
        "calls_counted_from": calls_key,
        "dropped": dropped,
        "counters": counters,
        "processes": slot.get("processes"),
        "trace_path": trace_path,
        "trace_bytes": trace_bytes,
        "out_files": files,
        "out_bytes": out_bytes,
        "bytes_startup": at_ready,
        "bytes_before_window": max(0, at_mark - at_ready),
        "bytes_in_window": in_window,
        "bytes_at_exit": at_exit,
        # Output before the window (startup, discover) and at exit happens once; what the
        # watcher writes during the steady-state window scales with time.
        "bytes_per_day": int(at_mark + in_window * day / max(window_s, 1e-9) + at_exit),
        # Naive upper bound: everything scales with the whole load time.
        "bytes_per_day_linear": int(out_bytes * day / max(load_s, 1e-9)),
        "bytes_per_event": round(trace_bytes / meta["events"], 1)
        if trace_bytes and meta.get("events") else None,
        "discover": discover,
        "watcher_started_at": info["started_at"],
    }


def site_keys(svc_dir: Path) -> tuple[set, str | None]:
    """(sym, call-site file relative to the project, line) of every call the capture
    recorded or counted: facts + tallies of the merged facts snapshot, else the JSONL."""
    ct = svc_dir / ".pymolt" / "contract_traces"
    root = svc_dir.resolve()
    keys: set = set()

    def add(item: dict) -> None:
        where = item.get("where") or {}
        path = where.get("file")
        if path:
            try:
                path = str(Path(path).resolve().relative_to(root))
            except (ValueError, OSError):
                pass
        keys.add((item.get("sym") or item.get("q"), path, where.get("line")))

    snapshots = sorted(ct.glob("*.facts.json")) if ct.is_dir() else []
    if snapshots:
        for snap in snapshots:
            try:
                data = json.loads(snap.read_text())
            except (OSError, ValueError):
                continue
            for item in list(data.get("facts") or []) + list(data.get("tallies") or []):
                if isinstance(item, dict):
                    add(item)
        return keys, "facts"
    traces = sorted(ct.glob("*.jsonl")) if ct.is_dir() else []
    for trace in traces:
        with open(trace, errors="replace") as fh:
            for line in fh:
                try:
                    add(json.loads(line))
                except ValueError:
                    continue
    return keys, ("jsonl" if traces else None)


STARTUP_WINDOW_S = 30.0
RECALL_REFERENCE = "capture-discover"


def startup_curve(run: dict) -> dict | None:
    """The first STARTUP_WINDOW_S seconds from the watcher start (capture) or the process
    spawn (nocap/hook): rps over them (seconds before the service answered count as 0), the
    slowest 5-s window of the load, and when a 5-s window first reached 80% of the run's
    steady-state rps."""
    load = run.get("load") or {}
    per_second = load.get("per_second")
    t0w = load.get("t0_wall")
    if not per_second or t0w is None or not load.get("rps"):
        return None
    cap = run.get("capture") or {}
    origin = cap.get("watcher_started_at")
    label = "watcher start"
    if origin is None:
        origin = (run.get("wall") or {}).get("spawn")
        label = "process spawn" if run.get("kind") != "capture" else "process spawn (no "\
            "watcher start in the metadata)"
    if origin is None:
        return None
    boot = max(0.0, t0w - origin)

    def started_before(rel: float) -> float:
        if rel <= 0:
            return 0.0
        whole = int(rel)
        n = float(sum(per_second[:whole]))
        if whole < len(per_second):
            n += per_second[whole] * (rel - whole)
        return n

    full = per_second[:-1] if len(per_second) > 6 else per_second  # last second is partial
    windows = [sum(full[i:i + 5]) / 5.0 for i in range(0, max(1, len(full) - 4))]
    target = 0.8 * load["rps"]
    reach = next((i for i, w in enumerate(windows) if w >= target), None)
    covered = boot + len(full) >= STARTUP_WINDOW_S  # the load lasted past the window
    return {
        "origin": label,
        "boot_s": round(boot, 2),
        "window_s": STARTUP_WINDOW_S,
        "rps_first_window": round(started_before(STARTUP_WINDOW_S - boot) / STARTUP_WINDOW_S, 2)
        if covered else None,
        "note": None if covered else "load ended before the startup window did",
        "min_5s_rps": round(min(windows), 2) if windows else None,
        "t80_s": round(boot + reach + 5, 1) if reach is not None else None,
        "steady_rps": load["rps"],
    }


def _print_run_line(record: dict) -> None:
    load = record.get("load") or {}
    lat = load.get("latency_co_ms") or load.get("latency_ms") or {}
    line = (f"[{record['name']}] rps={load.get('rps')} p50={lat.get('p50')} "
            f"p99={lat.get('p99')}ms")
    cap = record.get("capture")
    if cap:
        line += (f" events={cap.get('events_written')} dropped={cap.get('dropped')} "
                 f"out={_fmt_bytes(cap.get('out_bytes'))} "
                 f"(~{_fmt_bytes(cap.get('bytes_per_day'))}/day)")
        disc = cap.get("discover") or {}
        if (record.get("config") or {}).get("steady"):
            line += (f" | window after {disc.get('warmup_s')} s ({disc.get('warmup_end')}, "
                     f"{disc.get('steady_signal')}); switch {disc.get('switch_reason')} at "
                     f"{disc.get('switch_after_load_start_s')} s, discover rps "
                     f"{disc.get('discover_rps')}, steady={disc.get('steady_state')}")
    if record.get("error"):
        line += f" ERROR={record['error']}"
    print(line, flush=True)


# ── host fold ────────────────────────────────────────────────────────────────


def measure_fold(run_dir: Path) -> dict:
    """`pymolt verify <svc> --json` under /usr/bin/time: peak RSS and seconds."""
    _require_pymolt()
    svc_dir = run_dir / "svc"
    flag = "-l" if sys.platform == "darwin" else "-v"
    cmd = ["/usr/bin/time", flag, str(PYMOLT), "verify", str(svc_dir), "--json"]
    out_path, err_path = run_dir / "fold.json", run_dir / "fold.time"
    started = time.monotonic()
    try:
        with open(out_path, "wb") as out, open(err_path, "wb") as err:
            proc = subprocess.run(cmd, cwd=svc_dir, stdout=out, stderr=err,
                                  env=_clean_env({}), timeout=FOLD_TIMEOUT_S)
        returncode = proc.returncode
    except subprocess.TimeoutExpired:
        returncode = None
    wall = time.monotonic() - started
    text = err_path.read_text(errors="replace") if err_path.exists() else ""
    rss = seconds = footprint = None
    m = re.search(r"^\s*(\d+)\s+maximum resident set size", text, re.M)
    if m:
        rss = int(m.group(1))  # bytes on macOS
    m = re.search(r"^\s*(\d+)\s+peak memory footprint", text, re.M)
    if m:
        footprint = int(m.group(1))
    m = re.search(r"^\s*([\d.]+)\s+real\s", text, re.M)
    if m:
        seconds = float(m.group(1))
    m = re.search(r"Maximum resident set size \(kbytes\):\s*(\d+)", text)
    if m:
        rss = int(m.group(1)) * 1024
    if seconds is None:
        seconds = round(wall, 3)
    try:
        report = json.loads(out_path.read_text())
        parsed = True
    except (OSError, json.JSONDecodeError):
        report, parsed = None, False
    return {
        "returncode": returncode,
        "max_rss_bytes": rss,
        "peak_footprint_bytes": footprint,
        "seconds": seconds,
        "json_ok": parsed,
        "json_bytes": out_path.stat().st_size if out_path.exists() else 0,
        "report_keys": sorted(report)[:20] if isinstance(report, dict) else None,
        "timed_out": returncode is None,
    }


# ── report ───────────────────────────────────────────────────────────────────


def _fmt_bytes(n) -> str:
    if n is None:
        return "n/a"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000.0
    return f"{n:.1f} TB"


def _fmt_mib(n) -> str:
    return "n/a" if n is None else f"{n / MIB:.0f} MiB"


def _load_run(run_dir: Path) -> dict:
    return json.loads((run_dir / "run.json").read_text())


def _matching_nocap(run_dir: Path, run: dict) -> list[Path]:
    """Every nocap run in the same session with the same rate, duration and threads."""
    cfg = run["config"]
    found = []
    for sib in sorted(run_dir.parent.iterdir()):
        if sib == run_dir or not (sib / "run.json").is_file():
            continue
        other = _load_run(sib)
        oc = other.get("config", {})
        if other.get("kind") == "nocap" and not other.get("error") and all(
            oc.get(k) == cfg.get(k) for k in ("rate", "duration_s", "threads")
        ):
            found.append(sib)
    return found


def _mean(values: list[float]) -> float | None:
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _median(values: list[float]) -> float | None:
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def summarize_run(run_dir: Path, nocap_dirs: list[Path] | None = None, fold: bool = True,
                  refold: bool = False) -> dict:
    """One run's summary. A capture/hook run is compared against the MEAN of ``nocap_dirs``
    (default: every matching nocap run in its session, e.g. the ones before and after it)."""
    run = _load_run(run_dir)
    if run.get("kind") == "capture" and fold and not run.get("error") and (
        refold or "fold" not in run
    ):
        print(f"[{run_dir.name}] host fold: pymolt verify ...", flush=True)
        run["fold"] = measure_fold(run_dir)
        (run_dir / "run.json").write_text(json.dumps(run, indent=2) + "\n")
    load = run.get("load") or {}
    lat = load.get("latency_ms") or {}
    co = load.get("latency_co_ms")
    s: dict = {
        "name": run["name"],
        "kind": run["kind"],
        "error": run.get("error"),
        "rate": run["config"]["rate"],
        "duration_s": run["config"]["duration_s"],
        "threads": run["config"]["threads"],
        "steady": run["config"].get("steady"),
        "rps": load.get("rps"),
        "latency_ms": lat,
        "latency_co_ms": co,
        "p99_ms": (co or lat).get("p99"),
        "status_counts": load.get("status_counts"),
        "per_minute": load.get("per_minute"),
        "warmup_s": load.get("warmup_s"),
        "warmup_rps": (load.get("warmup") or {}).get("rps"),
        "errors": load.get("errors"),
        "missed_slots": load.get("missed_slots"),
        "client_cpu_pct": load.get("client_cpu_pct"),
        "timings_s": run.get("timings_s"),
        "load_avg_before": run.get("load_avg_before"),
        "host_load": run.get("host_load"),
    }
    cap = run.get("capture")
    if cap:
        s.update({k: cap.get(k) for k in (
            "backend", "validity", "events_written", "events_seen", "calls_counted",
            "calls_counted_from", "dropped", "out_bytes", "trace_bytes", "bytes_per_event",
            "bytes_startup", "bytes_before_window", "bytes_in_window", "bytes_at_exit",
            "bytes_per_day", "bytes_per_day_linear", "out_files", "discover")})
        s["capture_wall_s"] = (run.get("timings_s") or {}).get("total")
        s["capture_finalize_s"] = (run.get("timings_s") or {}).get("finalize")
    if run.get("fold"):
        s["fold"] = run["fold"]
    if run["config"]["rate"] == 0:
        s["startup"] = startup_curve(run)
    if nocap_dirs is None:
        nocap_dirs = _matching_nocap(run_dir, run) if run["kind"] != "nocap" else []
    if nocap_dirs:
        bases = [_load_run(d).get("load") or {} for d in nocap_dirs]
        rps = [b.get("rps") for b in bases if b.get("rps") is not None]
        p99 = [(b.get("latency_co_ms") or b.get("latency_ms") or {}).get("p99") for b in bases]
        s["nocap_runs"] = [d.name for d in nocap_dirs]
        s["nocap_rps_samples"] = rps
        s["nocap_rps"] = round(_mean(rps), 2) if rps else None
        s["nocap_spread"] = (round((max(rps) - min(rps)) / _mean(rps), 4)
                             if len(rps) > 1 and _mean(rps) else None)
        s["nocap_p99_ms"] = round(_mean(p99), 3) if _mean(p99) is not None else None
        if s["nocap_rps"] and load.get("rps") is not None:
            s["rps_loss"] = round(1.0 - load["rps"] / s["nocap_rps"], 4)
        if s["nocap_p99_ms"] and s["p99_ms"] is not None:
            s["p99_ratio"] = round(s["p99_ms"] / s["nocap_p99_ms"], 2)
    return s


def _run_md(s: dict) -> str:
    lat = s.get("latency_ms") or {}
    co = s.get("latency_co_ms") or {}
    lines = [f"### {s['name']} ({s['kind']}, rate={s['rate']:g}, threads={s['threads']}, "
             f"{s['duration_s']:g}s)", ""]
    if s.get("error"):
        lines += [f"**ERROR:** {s['error']}", ""]
    lines += [
        f"- rps: **{s.get('rps')}**" + (
            f" (no-capture mean {s['nocap_rps']} of {s.get('nocap_rps_samples')}"
            + (f", spread {100 * s['nocap_spread']:.1f}%" if s.get("nocap_spread") is not None
               else "")
            + f"; loss {100 * s['rps_loss']:.1f}%)" if "rps_loss" in s else ""),
        f"- latency ms (from send): p50 {lat.get('p50')} / p95 {lat.get('p95')} / "
        f"p99 {lat.get('p99')} / max {lat.get('max')}",
    ]
    if co:
        lines.append(f"- latency ms (from schedule): p50 {co.get('p50')} / p95 {co.get('p95')} "
                     f"/ p99 {co.get('p99')}" + (f" (p99 x{s['p99_ratio']} vs no-capture)"
                                                 if 'p99_ratio' in s else ""))
    lines.append(f"- status codes: {s.get('status_counts')}; per minute: {s.get('per_minute')}; "
                 f"client CPU {s.get('client_cpu_pct')}%")
    st = s.get("startup") or {}
    if st:
        first = (f"{st['rps_first_window']} rps" if st.get("rps_first_window") is not None
                 else f"n/a ({st.get('note')})")
        lines.append(f"- startup (from {st['origin']}): {first} over the "
                     f"first {st['window_s']:g} s (service answered after {st['boot_s']} s), "
                     f"slowest 5-s window {st['min_5s_rps']} rps, 80% of steady "
                     f"({st['steady_rps']} rps) after {st['t80_s']} s")
    host = s.get("host_load") or {}
    if host:
        top = ", ".join(f"{t['comm']} {t['cpu']:.0f}%" for t in host.get("top", [])[:3])
        lines.append(f"- host before run: load {host.get('loadavg')}, other processes "
                     f"{host.get('others_cpu_pct')}% CPU ({top})")
    if s["kind"] == "capture":
        lines += [
            f"- backend {s.get('backend')}, validity {s.get('validity')}: events written "
            f"{s.get('events_written')}, seen {s.get('events_seen')}, calls counted "
            f"{s.get('calls_counted')} ({s.get('calls_counted_from')}), dropped "
            f"**{s.get('dropped')}**",
            f"- output {_fmt_bytes(s.get('out_bytes'))} (trace {_fmt_bytes(s.get('trace_bytes'))}"
            f", {s.get('bytes_per_event')} B/event; startup {_fmt_bytes(s.get('bytes_startup'))}"
            f", before window {_fmt_bytes(s.get('bytes_before_window'))}, in window "
            f"{_fmt_bytes(s.get('bytes_in_window'))}, at exit "
            f"{_fmt_bytes(s.get('bytes_at_exit'))}) -> **{_fmt_bytes(s.get('bytes_per_day'))}"
            f"/day** (linear {_fmt_bytes(s.get('bytes_per_day_linear'))}/day)",
            f"- capture wall {s.get('capture_wall_s')} s (finalize after service exit "
            f"{s.get('capture_finalize_s')} s)",
        ]
        d = s.get("discover") or {}
        lines.append(
            f"- discover -> watch: mode {d.get('mode')}, switch "
            f"{d.get('switch_reason') or 'none recorded'}"
            + (f" {d.get('discover_s')} s after the watcher started "
               f"({d.get('switch_after_load_start_s')} s into the load, "
               f"{d.get('discovered')} symbols); discover-window rps {d.get('discover_rps')}"
               if d.get("switched_at") else "")
            + f"; measured window after {d.get('warmup_s')} s of warmup ({d.get('warmup_end')}"
            + (f", {d.get('steady_signal')}" if d.get("steady_signal") else "")
            + f"); steady state: {d.get('steady_state')}")
        fold = s.get("fold")
        if fold:
            lines.append(f"- host fold: {_fmt_mib(fold.get('max_rss_bytes'))} max RSS, "
                         f"{fold.get('seconds')} s (exit {fold.get('returncode')}, json "
                         f"{'ok' if fold.get('json_ok') else 'NOT PARSED'})")
    return "\n".join(lines) + "\n"


def cmd_report(args) -> int:
    target = Path(args.path).resolve()
    run_dirs = [target] if (target / "run.json").is_file() else sorted(
        p for p in target.iterdir() if (p / "run.json").is_file())
    if not run_dirs:
        sys.exit(f"no run.json under {target}")
    nocaps = [Path(n).resolve() for n in args.nocap] if args.nocap else None
    out = []
    for rd in run_dirs:
        s = summarize_run(rd, nocap_dirs=nocaps, fold=not args.no_fold, refold=args.refold)
        (rd / "report.json").write_text(json.dumps(s, indent=2) + "\n")
        md = _run_md(s)
        (rd / "summary.md").write_text(md)
        out.append(md)
    print("\n".join(out))
    return 0


# ── gate ─────────────────────────────────────────────────────────────────────


def _triplet_stats(runs: dict[str, dict], triplets: list[dict]) -> list[dict]:
    """Per saturated capture: the nocap runs right before and after it, their mean and
    spread, and the capture's (and control's) loss against that mean."""
    out = []
    for tr in triplets:
        pre, post = runs.get(tr["pre"], {}), runs.get(tr["post"], {})
        nocap = [x.get("rps") for x in (pre, post) if x.get("rps") is not None]
        mean = _mean(nocap)
        cap = runs.get(tr["capture"], {})
        hook = runs.get(tr["hook"], {}) if tr.get("hook") else {}

        def loss(x):
            return round(1 - x["rps"] / mean, 4) if mean and x.get("rps") is not None else None

        out.append({
            **tr,
            "nocap_rps": nocap,
            "nocap_mean": round(mean, 2) if mean else None,
            "nocap_spread": round((max(nocap) - min(nocap)) / mean, 4)
            if len(nocap) == 2 and mean else None,
            "capture_rps": cap.get("rps"),
            "loss": loss(cap),
            "steady_state": (cap.get("discover") or {}).get("steady_state"),
            "hook_rps": hook.get("rps"),
            "hook_loss": loss(hook) if hook else None,
            "startup": cap.get("startup"),
            "startup_ratio": round((cap.get("startup") or {}).get("rps_first_window") / mean, 4)
            if mean and (cap.get("startup") or {}).get("rps_first_window") is not None
            else None,
        })
    return out


def recall_check(session: Path, default_runs: list[str], reference: str) -> dict | None:
    """|default ∩ reference| / |reference| over (sym, site file, line), where the reference
    is a capture in discover mode (wide for the whole run)."""
    ref_dir = session / reference / "svc"
    if not ref_dir.is_dir():
        return None
    ref, ref_src = site_keys(ref_dir)
    union: set = set()
    sources = {}
    for name in default_runs:
        if (session / name / "svc").is_dir():
            keys, src = site_keys(session / name / "svc")
            union |= keys
            sources[name] = src
    if not ref:
        return {"reference": reference, "reference_keys": 0, "recall": None,
                "reference_source": ref_src, "default_sources": sources, "missing": []}
    hit = ref & union
    missing = sorted(ref - union, key=lambda k: tuple("" if v is None else str(v) for v in k))
    return {
        "reference": reference,
        "reference_source": ref_src,
        "reference_keys": len(ref),
        "default_runs": default_runs,
        "default_sources": sources,
        "default_keys": len(union),
        "matched": len(hit),
        "recall": round(len(hit) / len(ref), 4),
        "missing": [list(k) for k in missing],
        "extra_in_default": len(union - ref),
    }


def evaluate_gate(runs: dict[str, dict], triplets: list[dict], paced: tuple[str, str],
                  recall: dict | None = None) -> tuple[list[dict], list[dict]]:
    stats = _triplet_stats(runs, triplets)
    np_, cp = runs.get(paced[0], {}), runs.get(paced[1], {})
    caps = [runs[t["capture"]] for t in triplets if t["capture"] in runs] + ([cp] if cp else [])
    rows = []

    def row(name, threshold, measured, ok, info=False):
        result = "INFO" if info else ("ERROR" if ok is None else ("PASS" if ok else "FAIL"))
        rows.append({"criterion": name, "threshold": threshold, "measured": measured,
                     "result": result})

    def pct(x):
        return "n/a" if x is None else f"{100 * x:.1f}%"

    losses = [t["loss"] for t in stats]
    loss = _median(losses)
    spreads = [t["nocap_spread"] for t in stats if t["nocap_spread"] is not None]
    n = len(stats)
    row("1 throughput (saturated rps loss, steady state)",
        f"<= {100 * GATE['max_rps_loss']:.0f}%",
        f"nocap {_median([t['nocap_mean'] for t in stats])} rps (before/after spread "
        f"{pct(max(spreads)) if spreads else 'n/a'}) -> capture "
        f"{_median([t['capture_rps'] for t in stats])} rps: {pct(loss)} loss"
        + (f" (median of {n}: {', '.join(pct(x) for x in losses)})" if n > 1 else ""),
        None if loss is None or any(x is None for x in losses) else loss <= GATE["max_rps_loss"])
    unsteady = [t["capture"] for t in stats if t["steady_state"] is not True]
    if unsteady:
        row("1 steady-state check", "window after the switch",
            "not confirmed for " + ", ".join(
                f"{t['capture']} ({t['steady_state']})" for t in stats
                if t["steady_state"] is not True), None, info=True)
    discs = [(t["capture"], runs.get(t["capture"], {}).get("discover") or {}) for t in stats]
    row("discover window (reported, not gated)", "-",
        " / ".join(f"{name}: {d.get('discover_s')} s to {d.get('switch_reason') or 'no switch'}"
                   f", {d.get('discover_rps')} rps" for name, d in discs) or "n/a",
        None, info=True)
    if any(t.get("hook") for t in stats):
        row("control: no-op profile hook loss (not gated)", "-",
            ", ".join(f"{t['hook_rps']} rps ({pct(t['hook_loss'])})" for t in stats),
            None, info=True)
    drops = [c.get("dropped") for c in caps]
    row("2 completeness (dropped calls)", "0",
        " / ".join(f"{c['name']}: {c.get('dropped')}" for c in caps) or "n/a",
        None if not caps or any(d is None for d in drops)
        else all(d <= GATE["max_dropped"] for d in drops))
    vols = [c.get("bytes_per_day") for c in caps]
    row("3a volume (extrapolated / day)", f"< {_fmt_bytes(GATE['max_bytes_per_day'])}",
        " / ".join(f"{c['name']}: {_fmt_bytes(c.get('bytes_per_day'))} "
                   f"(linear {_fmt_bytes(c.get('bytes_per_day_linear'))})" for c in caps)
        or "n/a",
        None if not caps or any(v is None for v in vols)
        else max(vols) < GATE["max_bytes_per_day"])
    folds = [(c["name"], (c.get("fold") or {}).get("max_rss_bytes"),
              (c.get("fold") or {}).get("seconds")) for c in caps]
    row("3b host fold (verify max RSS)", f"< {_fmt_mib(GATE['max_fold_rss'])}",
        " / ".join(f"{n}: {_fmt_mib(r)}, {sec} s" for n, r, sec in folds) or "n/a",
        None if not folds or any(r is None for _, r, _ in folds)
        else max(r for _, r, _ in folds) < GATE["max_fold_rss"])
    ratio = cp.get("p99_ratio")
    row("4 tail latency (paced p99, from schedule)", f"<= {GATE['max_p99_ratio']:g}x",
        f"{np_.get('p99_ms')} -> {cp.get('p99_ms')} ms (x{ratio})" if ratio is not None
        else "n/a",
        None if ratio is None else ratio <= GATE["max_p99_ratio"])
    ratios = [t["startup_ratio"] for t in stats]
    startup = _median(ratios)
    row(f"6 startup (saturated rps, first {STARTUP_WINDOW_S:g} s from watcher start)",
        f">= {100 * GATE['min_startup_ratio']:.0f}% of nocap",
        " / ".join(
            f"{t['capture']}: "
            + (f"{(t['startup'] or {}).get('rps_first_window')} rps = {pct(t['startup_ratio'])}"
               if t["startup_ratio"] is not None
               else f"n/a ({(t['startup'] or {}).get('note') or 'no per-second counts'})")
            + f" (slowest 5 s {(t['startup'] or {}).get('min_5s_rps')} rps, 80% of steady after "
            f"{(t['startup'] or {}).get('t80_s')} s)" for t in stats) or "n/a",
        None if startup is None or any(r is None for r in ratios)
        else startup >= GATE["min_startup_ratio"])
    if recall is not None:
        missing = recall.get("missing") or []
        shown = ", ".join(f"{k[0]} @ {k[1]}:{k[2]}" for k in missing[:5])
        row("7 recall ((sym, site) vs discover-mode reference)",
            f">= {100 * GATE['min_recall']:.0f}%",
            f"{recall.get('matched')}/{recall.get('reference_keys')} = {pct(recall.get('recall'))}"
            + (f"; missing {len(missing)}: {shown}" + (" ..." if len(missing) > 5 else "")
               if missing else ""),
            None if recall.get("recall") is None else recall["recall"] >= GATE["min_recall"])
    return rows, stats


def _verdict(rows: list[dict]) -> str:
    return "PASS" if all(r["result"] in ("PASS", "INFO") for r in rows) else "FAIL"


def _gate_md(session: Path, rows: list[dict], stats: list[dict], runs: dict[str, dict],
             order: list[str], prov: dict, recall: dict | None = None) -> str:
    host = prov.get("host", {})
    dirty = prov.get("pymolt_dirty_files") or []
    state = (f"dirty ({len(dirty)} files, tree {prov.get('pymolt_tree_hash')})" if dirty
             else "clean")
    lines = [
        f"# PyMolt phase-2 load gate: {_verdict(rows)}",
        "",
        f"- session: `{session.name}`",
        f"- code: `{prov.get('git_branch')}` @ `{(prov.get('git_head') or '')[:10]}`, "
        f"pymolt/ {state}" + (f" (snapshot at `{prov.get('pymolt_root')}`)"
                              if prov.get("snapshot") else ""),
        f"- host: {host.get('cpu')} ({host.get('cpus')} cpus), {host.get('platform')}, "
        f"service Python {prov.get('service_python')}",
        "",
        "| criterion | threshold | measured | result |",
        "|---|---|---|---|",
    ]
    lines += [f"| {r['criterion']} | {r['threshold']} | {r['measured']} | **{r['result']}** |"
              for r in rows]
    lines += ["", "| saturated triplet | nocap before | nocap after | spread | capture "
              "(steady) | loss | steady | control | control loss |",
              "|---|---|---|---|---|---|---|---|---|"]
    for t in stats:
        nb = runs.get(t["pre"], {}).get("rps")
        na = runs.get(t["post"], {}).get("rps")
        sp = f"{100 * t['nocap_spread']:.1f}%" if t["nocap_spread"] is not None else ""
        lo = f"{100 * t['loss']:.1f}%" if t["loss"] is not None else ""
        hl = f"{100 * t['hook_loss']:.1f}%" if t.get("hook_loss") is not None else ""
        lines.append(f"| {t['capture']} | {nb} | {na} | {sp} | {t['capture_rps']} | {lo} | "
                     f"{t['steady_state']} | {t.get('hook_rps') or ''} | {hl} |")
    if recall and recall.get("missing"):
        lines += ["", f"Missing from the default-mode captures (in `{recall['reference']}`):", ""]
        lines += [f"- `{k[0]}` at `{k[1]}:{k[2]}`" for k in recall["missing"]]
    lines += [
        "",
        "| run | rps | p50 ms | p95 ms | p99 ms | p99 sched ms | 5xx | warmup s | events | "
        "dropped | output | per day | fold RSS | fold s | finalize s | other CPU % |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name in order:
        s = runs.get(name)
        if not s:
            continue
        lat = s.get("latency_ms") or {}
        co = s.get("latency_co_ms") or {}
        five = sum(n for k, n in (s.get("status_counts") or {}).items()
                   if k.isdigit() and int(k) >= 500)
        fold = s.get("fold") or {}
        lines.append(
            f"| {name} | {s.get('rps')} | {lat.get('p50')} | {lat.get('p95')} | {lat.get('p99')}"
            f" | {co.get('p99', '')} | {five} | {s.get('warmup_s', '')} | "
            f"{s.get('events_written', '')} | "
            f"{s.get('dropped', '')} | {_fmt_bytes(s.get('out_bytes')) if 'out_bytes' in s else ''}"
            f" | {_fmt_bytes(s.get('bytes_per_day')) if 'bytes_per_day' in s else ''} | "
            f"{_fmt_mib(fold.get('max_rss_bytes')) if fold else ''} | {fold.get('seconds', '')} | "
            f"{s.get('capture_finalize_s', '')} | "
            f"{(s.get('host_load') or {}).get('others_cpu_pct', '')} |"
            + (f" ERROR: {s['error']}" if s.get("error") else ""))
    lines.append("")
    lines += [_run_md(runs[n]) for n in order if n in runs]
    return "\n".join(lines)


def gate_plan(repeat: int, paced_rate: float, control: bool,
              recall: bool = True) -> tuple[list, list, tuple]:
    """nocap-sat-0, capture-sat-1, [hook-sat-1], nocap-sat-1, capture-sat-2, ... so every
    saturated capture has a nocap run right before and right after it; then the paced pair."""
    plan = [("nocap-sat-0", "nocap", 0.0)]
    triplets = []
    for i in range(1, repeat + 1):
        plan.append((f"capture-sat-{i}", "capture", 0.0))
        if control:  # informational: a no-op profile hook, not part of PASS/FAIL
            plan.append((f"hook-sat-{i}", "hook", 0.0))
        plan.append((f"nocap-sat-{i}", "nocap", 0.0))
        triplets.append({"pre": f"nocap-sat-{i - 1}", "capture": f"capture-sat-{i}",
                         "hook": f"hook-sat-{i}" if control else None,
                         "post": f"nocap-sat-{i}"})
    plan += [("nocap-paced", "nocap", paced_rate), ("capture-paced", "capture", paced_rate)]
    if recall:  # reference for criterion 7: wide (discover) for the whole run, paced
        plan.append((RECALL_REFERENCE, "capture", paced_rate))
    return plan, triplets, ("nocap-paced", "capture-paced")


def cmd_gate(args) -> int:
    _require_setup()
    _require_pymolt()
    if args.repeat < 1:
        sys.exit("--repeat must be >= 1")
    session = _session_dir("gate", args.session)
    env = _parse_env(args.env)
    plan, triplets, paced = gate_plan(args.repeat, args.paced_rate, args.control,
                                      recall=not args.no_recall)
    print(f"gate session: {session}", flush=True)
    for i, (name, kind, rate) in enumerate(plan):
        if i:
            time.sleep(COOLDOWN_S)
        run_env = env if kind == "capture" else {}
        if name == RECALL_REFERENCE:
            run_env = {**env, "PYMOLT_TRACE_MODE": "discover"}
        run_one(kind, session, duration=args.duration, rate=rate, threads=None,
                seed=args.seed, warmup=args.warmup, name=name,
                env_overrides=run_env,
                capture_args=args.capture_args if kind == "capture" else [],
                wait_quiet=args.wait_quiet,
                steady=args.steady_warmup if kind == "capture" else None)
    neighbours: dict[str, list[str]] = {}
    for tr in triplets:
        for key in ("capture", "hook"):
            if tr.get(key):
                neighbours[tr[key]] = [tr["pre"], tr["post"]]
    neighbours[paced[1]] = [paced[0]]
    runs: dict[str, dict] = {}
    for name, _kind, _rate in plan:
        rd = session / name
        if not (rd / "run.json").is_file():
            continue
        nocaps = [session / n for n in neighbours.get(name, [])
                  if (session / n / "run.json").is_file()]
        runs[name] = summarize_run(rd, nocap_dirs=nocaps, fold=name != RECALL_REFERENCE)
        (rd / "report.json").write_text(json.dumps(runs[name], indent=2) + "\n")
    defaults = [t["capture"] for t in triplets] + [paced[1]]
    recall = None if args.no_recall else recall_check(session, defaults, RECALL_REFERENCE)
    rows, stats = evaluate_gate(runs, triplets, paced, recall)
    prov = _provenance()
    order = [name for name, _k, _r in plan]
    md = _gate_md(session, rows, stats, runs, order, prov, recall)
    verdict = _verdict(rows)
    (session / "gate.json").write_text(json.dumps({
        "verdict": verdict, "thresholds": GATE, "criteria": rows, "triplets": stats,
        "recall": recall, "runs": runs, "provenance": prov,
        "config": {"duration_s": args.duration, "paced_rate": args.paced_rate,
                   "repeat": args.repeat, "steady_warmup": args.steady_warmup,
                   "seed": args.seed, "warmup_s": args.warmup, "env": env,
                   "capture_args": args.capture_args},
    }, indent=2) + "\n")
    (session / "summary.md").write_text(md)
    print("\n" + md.split("\n### ")[0])
    print(f"results: {session}")
    return 0 if verdict == "PASS" else 1


# ── CLI ──────────────────────────────────────────────────────────────────────


def _steady_arg(value: str) -> str:
    value = value.strip().lower()
    if value in ("auto", "off"):
        return value
    try:
        if float(value) < 0:
            raise ValueError
    except ValueError:
        raise argparse.ArgumentTypeError("expected auto, off or seconds >= 0") from None
    return value


def _parse_env(items: list[str] | None) -> dict[str, str]:
    env = {}
    for item in items or []:
        if "=" not in item:
            sys.exit(f"--env expects KEY=VAL, got {item!r}")
        key, val = item.split("=", 1)
        env[key] = val
    return env


def cmd_run(args) -> int:
    session = _session_dir(args.kind, args.session)
    run_dir = run_one(
        args.kind, session, duration=args.duration, rate=args.rate, threads=args.threads,
        seed=args.seed, warmup=args.warmup, env_overrides=_parse_env(args.env),
        capture_args=args.capture_args if args.kind == "capture" else [], name=args.name,
        mix=args.mix, wait_quiet=args.wait_quiet,
        steady=getattr(args, "steady_warmup", None) if args.kind == "capture" else None,
    )
    print(f"results: {run_dir}")
    return 1 if _load_run(run_dir).get("error") else 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    capture_args: list[str] = []
    if "--" in argv:
        i = argv.index("--")
        argv, capture_args = argv[:i], argv[i + 1:]
    ap = argparse.ArgumentParser(prog="run.py", description="PyMolt phase-2 load harness")
    ap.add_argument("--pymolt", default=None,
                    help="pymolt executable for capture/fold (default: $BENCH_PYMOLT or "
                         "<repo>/.venv/bin/pymolt)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("setup", help="create bench/load/.venv (uv, Python 3.12) with service deps")

    def load_opts(p, default_rate=0.0):
        p.add_argument("--duration", type=float, default=60.0, help="measured seconds")
        p.add_argument("--rate", type=float, default=default_rate, help="req/s; 0 = saturated")
        p.add_argument("--threads", type=int, default=None,
                       help=f"client threads (default {SAT_THREADS} saturated, "
                            f"{PACED_THREADS} paced)")
        p.add_argument("--seed", type=int, default=LOAD_SEED)
        p.add_argument("--warmup", type=float, default=WARMUP_S)
        p.add_argument("--session", default=None, help="results dir to add this run to")
        p.add_argument("--name", default=None, help="run name inside the session")
        p.add_argument("--env", action="append", default=[], metavar="KEY=VAL")
        p.add_argument("--mix", default=None,
                       help="diagnostics: override the endpoint mix, e.g. get_order=1")
        p.add_argument("--wait-quiet", type=float, default=0.0, metavar="S",
                       help="wait up to S s for other processes to drop below 150%% CPU")

    p = sub.add_parser("nocap", help="run the service without capture and drive load")
    load_opts(p)
    p = sub.add_parser("hook", help="control: the service under a no-op sys.setprofile hook")
    load_opts(p)
    p = sub.add_parser("capture", help="run the service under `pymolt capture`")
    load_opts(p)
    p.add_argument("--steady-warmup", default="off", metavar="auto|S|off", type=_steady_arg,
                   help="measure steady state: auto = keep the load running until the watcher "
                        "switched discover -> watch, S = fixed warmup seconds (default off)")
    p = sub.add_parser("report", help="summarize a run (or every run in a session)")
    p.add_argument("path")
    p.add_argument("--nocap", action="append", default=None, metavar="RUN_DIR",
                   help="no-capture run(s) to compare against (repeatable; their mean is "
                        "used). Default: every matching nocap run in the same session")
    p.add_argument("--no-fold", action="store_true", help="skip the host fold measurement")
    p.add_argument("--refold", action="store_true", help="re-measure the host fold")
    p = sub.add_parser("gate", help="the gate runs + PASS/FAIL table")
    p.add_argument("--duration", type=float, default=60.0)
    p.add_argument("--paced-rate", type=float, default=200.0)
    p.add_argument("--seed", type=int, default=LOAD_SEED)
    p.add_argument("--warmup", type=float, default=WARMUP_S)
    p.add_argument("--session", default=None)
    p.add_argument("--env", action="append", default=[], metavar="KEY=VAL",
                   help="extra env for the capture runs")
    p.add_argument("--wait-quiet", type=float, default=0.0, metavar="S",
                   help="before each run wait up to S s for other processes to drop below "
                        "150%% CPU (the wait and the load are recorded)")
    p.add_argument("--control", action="store_true",
                   help="also run a saturated no-op-profile-hook control (informational)")
    p.add_argument("--no-recall", action="store_true",
                   help="skip the discover-mode reference run (criterion 7)")
    p.add_argument("--repeat", type=int, default=1, metavar="N",
                   help="saturated captures, each between two nocap runs (medians are gated)")
    p.add_argument("--steady-warmup", default="auto", metavar="auto|S|off", type=_steady_arg,
                   help="capture runs measure steady state: auto = until the discover -> watch "
                        "switch (fixed quiet + 30 s fallback when no live metadata), S = fixed "
                        "warmup seconds, off = 2 s warmup (default auto)")

    args = ap.parse_args(argv)
    args.capture_args = capture_args
    if args.pymolt:
        global PYMOLT
        PYMOLT = Path(args.pymolt).expanduser().absolute()
    if capture_args and args.cmd not in ("capture", "gate"):
        ap.error("arguments after -- are only for capture/gate")
    try:
        if args.cmd == "setup":
            return cmd_setup(args)
        if args.cmd in ("nocap", "hook", "capture"):
            args.kind = args.cmd
            return cmd_run(args)
        if args.cmd == "report":
            return cmd_report(args)
        if args.cmd == "gate":
            return cmd_gate(args)
    except KeyboardInterrupt:
        print("interrupted; stopping children", file=sys.stderr)
        return 130
    finally:
        _cleanup()
    return 2


if __name__ == "__main__":
    sys.exit(main())
