#!/usr/bin/env python3
"""PyMolt CLI benchmark — wall-clock time and JSON payload size per phase.

Run:  uv run --with tiktoken python bench/bench_cli.py [--quick]

Every measured command is offline and deterministic except ``assess``, which is
run twice per fixture (cold cache / warm cache) and is skipped with --offline-only.
Emits bench/results/cli_<timestamp>.json plus a markdown table on stdout.
"""
from __future__ import annotations

import argparse
import json
import platform
import shutil
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYMOLT = str(REPO / ".venv" / "bin" / "pymolt")
RESULTS = REPO / "bench" / "results"

# fixture -> extra args that make assess non-interactive for that fixture
FIXTURES = {
    "pyfolio": {"py_files": None, "manifest": "requirements.txt", "target": "3.12", "base": "3.9"},
    "flasgger": {"py_files": None, "manifest": "requirements.txt", "target": "3.12", "base": "3.9"},
    "blaze": {"py_files": None, "manifest": "requirements.txt", "target": "3.12", "base": "3.9"},
}


def count_tokens(text: str) -> int | None:
    try:
        import tiktoken
    except ImportError:
        return None
    return len(tiktoken.get_encoding("cl100k_base").encode(text))


def loc(fixture_dir: Path) -> dict:
    files = list(fixture_dir.rglob("*.py"))
    files = [f for f in files if ".git" not in f.parts and ".pymolt" not in str(f)]
    total = 0
    for f in files:
        try:
            total += sum(1 for _ in f.open("rb"))
        except OSError:
            pass
    return {"py_files": len(files), "py_loc": total}


def run_once(argv: list[str], cwd: Path) -> dict:
    t0 = time.perf_counter()
    proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    wall_ms = (time.perf_counter() - t0) * 1000
    out = proc.stdout
    return {
        "wall_ms": round(wall_ms, 1),
        "exit_code": proc.returncode,
        "stdout_bytes": len(out.encode("utf-8")),
        "stdout_tokens": count_tokens(out),
        "stderr_head": proc.stderr[:300] if proc.returncode != 0 else "",
    }


def measure(name: str, argv: list[str], cwd: Path, repeats: int, warmup: bool = True) -> dict:
    if warmup:
        run_once(argv, cwd)
    samples = [run_once(argv, cwd) for _ in range(repeats)]
    times = [s["wall_ms"] for s in samples]
    ok = samples[-1]
    return {
        "command": name,
        "argv": argv,
        "repeats": repeats,
        "exit_code": ok["exit_code"],
        "wall_ms_min": min(times),
        "wall_ms_p50": round(statistics.median(times), 1),
        "wall_ms_max": max(times),
        "wall_ms_stdev": round(statistics.stdev(times), 1) if len(times) > 1 else 0.0,
        "stdout_bytes": ok["stdout_bytes"],
        "stdout_tokens": ok["stdout_tokens"],
        "stderr_head": ok["stderr_head"],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--offline-only", action="store_true")
    ap.add_argument("--fixtures", default=",".join(FIXTURES))
    args = ap.parse_args()

    wanted = [f.strip() for f in args.fixtures.split(",") if f.strip()]
    runs = []

    for fx in wanted:
        fdir = REPO / "tests" / "artifacts" / fx
        if not fdir.is_dir():
            print(f"skip {fx}: missing", file=sys.stderr)
            continue
        size = loc(fdir)
        print(f"### {fx}  ({size['py_files']} .py / {size['py_loc']} LOC)", file=sys.stderr)

        offline = [
            ("status", [PYMOLT, "status", str(fdir), "--json"]),
            ("scan", [PYMOLT, "scan", str(fdir), "--json"]),
            ("contract map", [PYMOLT, "contract", "map", str(fdir), "--json"]),
        ]
        for name, argv in offline:
            r = measure(name, argv, REPO, args.repeats)
            r.update(fixture=fx, cache="n/a", **size)
            runs.append(r)
            print(f"  {name:<14} p50={r['wall_ms_p50']:>8.1f}ms  "
                  f"{r['stdout_bytes']:>7}B  {r['stdout_tokens']} tok  exit={r['exit_code']}",
                  file=sys.stderr)

        if args.offline_only:
            continue

        cfg = FIXTURES.get(fx, {})
        a_argv = [PYMOLT, "assess", str(fdir), "--json", "--no-write",
                  "--target-python", cfg.get("target", "3.12"),
                  "--base-python", cfg.get("base", "3.9")]
        cache = fdir / ".pymolt_cache"

        # cold: no resolver cache at all
        shutil.rmtree(cache, ignore_errors=True)
        cold = run_once(a_argv, REPO)
        cold.update(command="assess", argv=a_argv, fixture=fx, cache="cold", repeats=1,
                    wall_ms_min=cold["wall_ms"], wall_ms_p50=cold["wall_ms"],
                    wall_ms_max=cold["wall_ms"], wall_ms_stdev=0.0, **size)
        runs.append(cold)
        print(f"  {'assess (cold)':<14} {cold['wall_ms']:>10.1f}ms  "
              f"{cold['stdout_bytes']:>7}B  {cold['stdout_tokens']} tok  exit={cold['exit_code']}",
              file=sys.stderr)
        if cold["exit_code"] != 0:
            print(f"    stderr: {cold['stderr_head']}", file=sys.stderr)

        # warm: cache populated by the cold run
        warm = measure("assess", a_argv, REPO, max(2, args.repeats // 2), warmup=False)
        warm.update(fixture=fx, cache="warm", **size)
        runs.append(warm)
        print(f"  {'assess (warm)':<14} p50={warm['wall_ms_p50']:>8.1f}ms  "
              f"{warm['stdout_bytes']:>7}B  {warm['stdout_tokens']} tok  exit={warm['exit_code']}",
              file=sys.stderr)

    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO,
                         capture_output=True, text=True).stdout.strip()
    payload = {
        "meta": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "git_sha": sha,
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "repeats": args.repeats,
            "tokenizer": "tiktoken/cl100k_base",
        },
        "runs": runs,
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"cli_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(payload, indent=2))
    print(json.dumps(payload, indent=2))
    print(f"\nwrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
