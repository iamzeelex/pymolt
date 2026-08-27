#!/usr/bin/env python3
"""PyMolt MCP A/B benchmark — token + latency cost of answering a migration
question WITH the PyMolt MCP server vs WITHOUT it (plain file tools).

Both arms run `claude -p --output-format json` headless against an isolated
COPY of a fixture repo under /tmp, so no CLAUDE.md / README from this repo can
leak into either arm. Both arms get Read/Grep/Glob/Bash; the pymolt arm also
gets mcp__pymolt__*. Usage numbers come straight from the CLI's own result
envelope (usage.*, total_cost_usd, num_turns, duration_ms).

Run:  python bench/bench_mcp.py --model sonnet --repeats 3
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PYMOLT_BIN = str(REPO / ".venv" / "bin" / "pymolt")
RESULTS = REPO / "bench" / "results"
WS_ROOT = Path("/tmp/pymolt_bench_ws")

BASE_TOOLS = "Read,Grep,Glob,Bash"
PYMOLT_TOOLS = BASE_TOOLS + ",mcp__pymolt"

# The `plugin` arm reproduces what installing the PyMolt plugin actually gives a
# user: the server PLUS a slash-command / subagent that tells the agent to use it
# (plugin/commands/migrate.md, plugin/agents/pymolt-migrator.md). The wording is
# the MCP server's own `_INSTRUCTIONS`, not a hint invented for the benchmark.
PLUGIN_SYSTEM = (
    "The pymolt MCP server is available. Prefer its tools over reading Dockerfiles, "
    "lockfiles, manifests or running analysis by hand: scan -> setup_options/setup_apply "
    "-> assess -> contract_map/contract_capture/contract_report. They mirror the pymolt "
    "CLI funnel and return authoritative results."
)

ARMS = ("bare", "mcp_only", "plugin")

JSON_TAIL = (
    "\n\nEnd your reply with a single fenced ```json block containing ONLY the "
    "answer object described above. No prose inside the block."
)

TASKS = {
    # T0 measures the fixed cost of merely having the server attached.
    "T0_noop": {
        "fixture": "flasgger",
        "prompt": "Reply with exactly: OK",
        "answer_shape": None,
    },
    "T1_recon": {
        "fixture": "flasgger",
        "prompt": (
            "This repository may contain more than one Python project root. "
            "Identify every project root, the dependency manifests in each, and any "
            "evidence of which Python version each root targets (Dockerfiles, tox/nox, "
            "setup.py classifiers, .python-version, etc.).\n\n"
            'Answer object: {"root_count": <int>, "roots": [{"path": "<relative path>", '
            '"manifests": ["<filename>", ...], "python_versions_found": ["<version>", ...]}]}'
        ),
        "answer_shape": "roots",
    },
    "T2_upgrade": {
        "fixture": "flasgger",
        "prompt": (
            "Resolve this project's COMPLETE dependency set (direct and transitive) twice: "
            "once as it would resolve under Python 3.9, and once under Python 3.12. Then report "
            "every package that lands on a DIFFERENT version under 3.12 than under 3.9, giving "
            "both versions. A package that exists under one and not the other counts as changed "
            "(use null for the missing side).\n\n"
            'Answer object: {"total_packages": <int, size of the resolved set>, '
            '"changed": [{"name": "<pkg>", "py39": "<version or null>", "py312": "<version or null>"}], '
            '"method": "<one sentence: how you produced the two resolutions>"}'
        ),
        "answer_shape": "changed",
    },
    "T3_contact": {
        "fixture": "flasgger",
        "prompt": (
            "Before migrating, I need the dependency contact surface of this project: every "
            "distinct symbol belonging to a third-party dependency that this project's own "
            "code could call. Count them and rank the dependency modules by how many contact "
            "points the code has with each.\n\n"
            'Answer object: {"total_contact_points": <int>, "top_modules": '
            '[{"module": "<name>", "count": <int>}] (top 5)}'
        ),
        "answer_shape": "top_modules",
    },
}


def prepare_workspace(fixture: str, force: bool = False) -> Path:
    """Isolated copy of the fixture outside the repo tree (no parent CLAUDE.md)."""
    src = REPO / "tests" / "artifacts" / fixture
    dst = WS_ROOT / fixture
    if dst.exists() and not force:
        return dst
    shutil.rmtree(dst, ignore_errors=True)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        src, dst,
        ignore=shutil.ignore_patterns(".git", ".pymolt", ".pymolt_cache", "__pycache__",
                                      "*.pyc", ".tox", ".venv", "CLAUDE.md", "AGENTS.md"),
        symlinks=True,
    )
    return dst


def mcp_config(ws: Path) -> str:
    return json.dumps({
        "mcpServers": {
            "pymolt": {
                "command": PYMOLT_BIN,
                "args": ["mcp"],
                "env": {
                    "PYMOLT_MCP_ROOT": str(ws),
                    "PYMOLT_MCP_ALLOW_EXEC": "1",
                    "PATH": os.environ.get("PATH", ""),
                },
            }
        }
    })


def run_arm(task_id: str, arm: str, model: str, ws: Path, max_turns: int, timeout: int) -> dict:
    task = TASKS[task_id]
    prompt = task["prompt"]
    if task["answer_shape"]:
        prompt += JSON_TAIL

    argv = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--model", model,
        "--strict-mcp-config",
        "--permission-mode", "bypassPermissions",
        "--max-turns", str(max_turns),
    ]
    if arm in ("mcp_only", "plugin"):
        argv += ["--mcp-config", mcp_config(ws), "--allowedTools", PYMOLT_TOOLS]
        if arm == "plugin":
            argv += ["--append-system-prompt", PLUGIN_SYSTEM]
    else:
        argv += ["--allowedTools", BASE_TOOLS]

    # A previous run must leave nothing behind for the next one to read:
    # assess writes .pymolt/, MCP tools write .pymolt/mcp/ full reports.
    prepare_workspace(task["fixture"], force=True)

    t0 = time.perf_counter()
    proc = subprocess.run(argv, cwd=ws, capture_output=True, text=True, timeout=timeout)
    wall_ms = (time.perf_counter() - t0) * 1000

    try:
        env = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {"task": task_id, "arm": arm, "model": model, "error": "unparseable output",
                "raw_head": proc.stdout[:500], "stderr_head": proc.stderr[:500],
                "wall_ms": round(wall_ms, 1)}

    u = env.get("usage", {}) or {}
    inp = u.get("input_tokens", 0)
    cc = u.get("cache_creation_input_tokens", 0)
    cr = u.get("cache_read_input_tokens", 0)
    out = u.get("output_tokens", 0)
    tool_names = sorted({
        t for t in _tools_used(env)
    })
    return {
        "task": task_id,
        "arm": arm,
        "model": model,
        "is_error": env.get("is_error"),
        "terminal_reason": env.get("terminal_reason"),
        "session_id": env.get("session_id"),
        "num_turns": env.get("num_turns"),
        "duration_ms": env.get("duration_ms") or round(wall_ms, 1),
        "duration_api_ms": env.get("duration_api_ms"),
        "wall_ms": round(wall_ms, 1),
        "input_tokens": inp,
        "cache_creation_input_tokens": cc,
        "cache_read_input_tokens": cr,
        "output_tokens": out,
        "billed_input_total": inp + cc + cr,
        "total_tokens": inp + cc + cr + out,
        "total_cost_usd": env.get("total_cost_usd"),
        "tools_used": tool_names,
        "result_text": env.get("result", "")[-4000:],
    }


def _tools_used(env: dict) -> list[str]:
    """Best-effort tool extraction; the json envelope does not always carry it."""
    names: list[str] = []
    for key in ("permission_denials",):
        for d in env.get(key) or []:
            n = d.get("tool_name")
            if n:
                names.append(n)
    return names


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--tasks", default=",".join(TASKS))
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--max-turns", type=int, default=40)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    task_ids = [t.strip() for t in args.tasks.split(",") if t.strip()]
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    runs = []

    for task_id in task_ids:
        ws = prepare_workspace(TASKS[task_id]["fixture"])
        for i in range(args.repeats):
            for arm in arms:
                print(f"[{task_id}/{arm}/{args.model} #{i+1}] running...", file=sys.stderr, flush=True)
                try:
                    r = run_arm(task_id, arm, args.model, ws, args.max_turns, args.timeout)
                except subprocess.TimeoutExpired:
                    r = {"task": task_id, "arm": arm, "model": args.model,
                         "error": f"timeout after {args.timeout}s"}
                r["repeat"] = i + 1
                runs.append(r)
                if "error" in r:
                    print(f"    ERROR: {r['error']}", file=sys.stderr, flush=True)
                else:
                    print(f"    turns={r['num_turns']:>3} "
                          f"billed_in={r['billed_input_total']:>8} out={r['output_tokens']:>5} "
                          f"total={r['total_tokens']:>8} ${r['total_cost_usd']:.4f} "
                          f"{r['wall_ms']/1000:.1f}s", file=sys.stderr, flush=True)
                # write incrementally so a crash never loses completed runs
                _dump(runs, args)

    _dump(runs, args, final=True)
    return 0


def _dump(runs: list[dict], args, final: bool = False) -> None:
    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO,
                         capture_output=True, text=True).stdout.strip()
    payload = {
        "meta": {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "git_sha": sha,
            "platform": platform.platform(),
            "model": args.model,
            "repeats": args.repeats,
            "max_turns": args.max_turns,
            "base_tools": BASE_TOOLS,
            "pymolt_tools": PYMOLT_TOOLS,
            "complete": final,
        },
        "tasks": {k: {"fixture": v["fixture"], "prompt": v["prompt"]} for k, v in TASKS.items()},
        "runs": runs,
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    tag = f"_{args.tag}" if args.tag else ""
    (RESULTS / f"mcp_{args.model}{tag}.json").write_text(json.dumps(payload, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
