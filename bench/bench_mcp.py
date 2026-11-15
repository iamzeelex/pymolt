#!/usr/bin/env python3
"""PyMolt MCP A/B benchmark — token + latency cost of answering a migration
question WITH the PyMolt MCP server vs WITHOUT it (plain file tools).

Both arms run `claude -p --output-format json` headless against an isolated
COPY of a fixture repo under /tmp, so no CLAUDE.md / README from this repo can
leak into either arm. Both arms get Read/Grep/Glob/Bash; the pymolt arm also
gets mcp__pymolt__*. Usage numbers come straight from the CLI's own result
envelope (usage.*, total_cost_usd, num_turns, duration_ms).

Run:  python bench/bench_mcp.py --model sonnet --repeats 3
      python bench/bench_mcp.py --probe-isolation          # free: init-event + PATH check
      python bench/bench_mcp.py --model claude-sonnet-5-5 --tasks T3_contact,T4_contract_risk \
          --arms bare,plugin,cli_skill --repeats 3 --budget-usd 8 --tag round3

Round 3 adds the `cli_skill` arm (same guidance, CLI transport, no MCP server) and
three isolation controls applied to EVERY arm:

  * `--setting-sources project` — user settings, user skills (~/.claude/skills),
    user-installed plugins and their hooks stay out. (`--bare` is not usable: it
    needs ANTHROPIC_API_KEY auth, and on this host it still loaded installed
    plugins' skills.) Only `cli_skill` adds `--plugin-dir bench/plugins/pymolt-cli`.
  * a sanitised child env — the host session's CLAUDE*/CLAUDECODE variables
    (effort level, entrypoint, session ids) are not inherited.
  * PATH control — `pymolt` is removed from the agent's PATH for every arm except
    `cli_skill`, which gets the repo's .venv/bin/pymolt via a shim dir.
"""
from __future__ import annotations

import argparse
import importlib.util
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
CLI_PLUGIN_DIR = REPO / "bench" / "plugins" / "pymolt-cli"
E2E_PANDAS = REPO / "bench" / "e2e_pandas" / "run.py"
# Interpreters for T4 live under WS_ROOT, not in the repo, so a path named in the
# prompt does not lead the agent to this repo's docs or the e2e script's comments.
T4_ENVS = WS_ROOT / "_envs"
# Resolved against the PARENT's PATH: the child PATH below may hide its directory.
CLAUDE_BIN = shutil.which("claude") or "claude"

BASE_TOOLS = "Read,Grep,Glob,Bash"
PYMOLT_TOOLS = BASE_TOOLS + ",mcp__pymolt"
CLI_TOOLS = BASE_TOOLS + ",Skill"

ISOLATION = ["--strict-mcp-config", "--setting-sources", "project"]
CLI_ARMS = ("cli_skill",)
MCP_ARMS = ("mcp_only", "plugin")

# The `plugin` arm reproduces what installing the PyMolt plugin actually gives a
# user: the server PLUS a slash-command / subagent that tells the agent to use it
# (plugin/commands/migrate.md, plugin/agents/pymolt-migrator.md). The wording is
# the MCP server's own `_INSTRUCTIONS`, not a hint invented for the benchmark.
PLUGIN_SYSTEM = (
    "The pymolt MCP server is available. Prefer its tools over reading Dockerfiles, "
    "lockfiles, manifests or running analysis by hand: status -> scan -> setup -> assess "
    "-> capture -> plan -> verify. They mirror the pymolt "
    "CLI funnel and return authoritative results."
)

ARMS = ("bare", "mcp_only", "plugin", "cli_skill")

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
    # T4 is the task that needs EXECUTION, which is where MCP's exec gate bites.
    # Fixture: bench/e2e_pandas/run.py's toy app, `change` variant, pre-migration.
    # Ground truth (score.py): toyapp/core.py Series.append (removed in pandas 2)
    # and DataFrame.mean (numeric_only default change: 1.5 drops the string
    # column with a FutureWarning, 2.x raises TypeError, which the app swallows).
    # Reachable through pymolt (contract map/capture/report) or by reading code.
    "T4_contract_risk": {
        "fixture": "toyapp",
        "prompt": (
            "This app is about to be upgraded from pandas 1.5 on Python 3.11 to pandas 2.2 "
            "on Python 3.12. Two interpreters are ready, both with the app's dependencies and "
            "pytest installed: the current environment `{old_python}` (Python 3.11, pandas "
            "1.5.3) and the target environment `{new_python}` (Python 3.12, pandas 2.2.3).\n\n"
            "Before upgrading, record what this app's dependency calls do under its test suite "
            "with the current interpreter, then list every dependency call site in the "
            "application code (toyapp/) whose behaviour is at risk under the target, and why. "
            "Do not edit the app's source files.\n\n"
            'Answer object: {"at_risk": [{"file": "<relative path>", "line": <int>, '
            '"symbol": "<dependency symbol called>", "reason": "<one sentence>"}]}'
        ),
        "answer_shape": "at_risk",
    },
}

# Fixtures generated rather than copied from tests/artifacts/.
GENERATED_FIXTURES = ("toyapp",)


def _e2e_module():
    spec = importlib.util.spec_from_file_location("bench_e2e_pandas_run", E2E_PANDAS)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def t4_python(which: str) -> Path:
    return T4_ENVS / {"old": "py311", "new": "py312"}[which] / "bin" / "python"


def _freeze(py: Path) -> str:
    return subprocess.run(["uv", "pip", "freeze", "-p", str(py)], capture_output=True,
                          text=True).stdout


def ensure_t4_envs() -> None:
    """Build (once) the two T4 interpreters from e2e_pandas's OLD/NEW specs, and
    rebuild one if a previous agent run changed what is installed in it."""
    e2e = _e2e_module()
    for which, spec in (("old", e2e.OLD), ("new", e2e.NEW)):
        py = t4_python(which)
        target = py.parent.parent
        snap = target / ".bench_freeze"
        if py.exists() and snap.exists() and _freeze(py) == snap.read_text():
            continue
        shutil.rmtree(target, ignore_errors=True)
        subprocess.run(["uv", "venv", "-q", "-p", spec["python"], str(target)], check=True)
        subprocess.run(["uv", "pip", "install", "-q", "-p", str(py), *spec["packages"]],
                       check=True)
        snap.write_text(_freeze(py))
        print(f"[t4 env] built {which}: {target}", file=sys.stderr, flush=True)


def render_prompt(task_id: str) -> str:
    return (TASKS[task_id]["prompt"]
            .replace("{old_python}", str(t4_python("old")))
            .replace("{new_python}", str(t4_python("new"))))


def prepare_workspace(fixture: str, force: bool = False) -> Path:
    """Isolated copy of the fixture outside the repo tree (no parent CLAUDE.md)."""
    dst = WS_ROOT / fixture
    if dst.exists() and not force:
        return dst
    shutil.rmtree(dst, ignore_errors=True)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if fixture in GENERATED_FIXTURES:
        # `change=True`: the scenario with a silent behaviour change (df.mean).
        _e2e_module().make_project(dst, change=True)
        return dst
    src = REPO / "tests" / "artifacts" / fixture
    shutil.copytree(
        src, dst,
        ignore=shutil.ignore_patterns(".git", ".pymolt", ".pymolt_cache", "__pycache__",
                                      "*.pyc", ".tox", ".venv", "CLAUDE.md", "AGENTS.md",
                                      ".claude"),
        symlinks=True,
    )
    return dst


def arm_path(arm: str, base_path: str | None = None) -> str:
    """The agent's PATH: no `pymolt` anywhere, except the CLI arms' shim.

    A PATH directory that holds a `pymolt` (e.g. ~/.local/bin from a `uv tool
    install`) is replaced by a shadow directory of symlinks to everything else in
    it, so `uv`, `python3.x` etc. stay reachable for every arm.
    """
    dirs = []
    for i, d in enumerate((base_path or os.environ.get("PATH", "")).split(os.pathsep)):
        if not d:
            continue
        p = Path(d)
        if (p / "pymolt").exists():
            shadow = WS_ROOT / "_path" / f"{i}_{p.name or 'root'}"
            if shadow.exists():
                shutil.rmtree(shadow)
            shadow.mkdir(parents=True)
            for entry in p.iterdir():
                if not entry.name.startswith("pymolt"):
                    (shadow / entry.name).symlink_to(entry)
            dirs.append(str(shadow))
        else:
            dirs.append(d)
    if arm in CLI_ARMS:
        shim = WS_ROOT / "_bin"
        shim.mkdir(parents=True, exist_ok=True)
        link = shim / "pymolt"
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(PYMOLT_BIN)
        dirs.insert(0, str(shim))
    return os.pathsep.join(dirs)


def child_env(arm: str) -> dict:
    """Parent env minus the host Claude session's variables, with the arm's PATH.

    Claude Code's Bash tool replays a snapshot of the user's login shell, and that
    snapshot ends in `export PATH=...` from ~/.zshrc — which on this host puts
    ~/.local/bin (a global `pymolt`) straight back. A per-arm ZDOTDIR whose rc files
    re-export the arm's PATH keeps the user's zsh config out of every arm.
    """
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith("CLAUDE") or k == "CLAUDECODE")}
    path = arm_path(arm)
    env["PATH"] = path
    zdot = WS_ROOT / "_zdotdir" / arm
    zdot.mkdir(parents=True, exist_ok=True)
    export = f"export PATH={json.dumps(path)}\n"
    for rc in (".zshenv", ".zprofile", ".zshrc"):
        (zdot / rc).write_text(export)
    env["ZDOTDIR"] = str(zdot)
    env["SHELL"] = "/bin/zsh"
    return env


def build_argv(task_id: str, arm: str, model: str, ws: Path, max_turns: int,
               prompt: str, output_format: str = "json") -> list[str]:
    argv = [
        CLAUDE_BIN, "-p", prompt,
        "--output-format", output_format,
        "--model", model,
        *ISOLATION,
        "--permission-mode", "bypassPermissions",
        "--max-turns", str(max_turns),
    ]
    if output_format == "stream-json":
        argv.append("--verbose")
    if arm in MCP_ARMS:
        argv += ["--mcp-config", mcp_config(ws), "--allowedTools", PYMOLT_TOOLS]
        if arm == "plugin":
            argv += ["--append-system-prompt", PLUGIN_SYSTEM]
    elif arm in CLI_ARMS:
        argv += ["--plugin-dir", str(CLI_PLUGIN_DIR), "--allowedTools", CLI_TOOLS]
    else:
        argv += ["--allowedTools", BASE_TOOLS]
    return argv


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
    prompt = render_prompt(task_id)
    if task["answer_shape"]:
        prompt += JSON_TAIL

    argv = build_argv(task_id, arm, model, ws, max_turns, prompt)

    # A previous run must leave nothing behind for the next one to read:
    # assess writes .pymolt/, MCP tools write .pymolt/mcp/ full reports.
    prepare_workspace(task["fixture"], force=True)
    if task["fixture"] == "toyapp":
        ensure_t4_envs()

    t0 = time.perf_counter()
    proc = subprocess.run(argv, cwd=ws, capture_output=True, text=True, timeout=timeout,
                          env=child_env(arm), stdin=subprocess.DEVNULL)
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
        "permission_denials": env.get("permission_denials") or [],
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


def probe_isolation(arms: list[str], model: str) -> int:
    """Check every arm's isolation from the session's init event, plus its PATH.

    The init event (skills, plugins, MCP servers) is emitted before the first API
    call, so those checks cost $0 even when auth fails. When auth works, the agent
    also runs `command -v pymolt` through its own Bash tool — the only check that
    sees the PATH after Claude Code's shell snapshot — at ~2 short turns per arm.
    Asserts: only `cli_skill` sees a pymolt skill; no user skill/plugin or hook
    leaks; `pymolt` resolves only for the CLI arms, in the env AND in the agent's Bash.
    """
    ws = WS_ROOT / "_probe"
    ws.mkdir(parents=True, exist_ok=True)
    user_skills = {p.name for p in (Path.home() / ".claude" / "skills").glob("*")}
    ok, spent, unverified = True, 0.0, []
    for arm in arms:
        argv = build_argv("T0_noop", arm, model, ws, 3, PROBE_PROMPT,
                          output_format="stream-json")
        env = child_env(arm)
        proc = subprocess.run(argv, cwd=ws, capture_output=True, text=True, timeout=300,
                              env=env, stdin=subprocess.DEVNULL)
        init, hooks, result = {}, [], {}
        for line in proc.stdout.splitlines():
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            if o.get("type") == "system" and o.get("subtype") == "init":
                init = o
            elif o.get("type") == "system" and o.get("subtype") == "hook_started":
                hooks.append(o.get("hook_name"))
            elif o.get("type") == "result":
                result = o
        spent += result.get("total_cost_usd") or 0.0
        skills = init.get("skills") or []
        plugins = [p.get("name") for p in init.get("plugins") or []
                   if p.get("path") != "builtin"]
        mcp = [m.get("name") + ":" + str(m.get("status")) for m in init.get("mcp_servers") or []]
        on_path = shutil.which("pymolt", path=env["PATH"])
        pym_skills = [s for s in skills if "pymolt" in s]
        answer = result.get("result") or ""
        authed = bool(result) and not result.get("is_error")
        checks = {
            "init seen": bool(init),
            "pymolt skill iff cli arm": bool(pym_skills) == (arm in CLI_ARMS),
            "no user skills": not (set(skills) & user_skills),
            "no user plugins": set(plugins) <= ({"pymolt-cli"} if arm in CLI_ARMS else set()),
            "no hooks": not hooks,
            "pymolt on PATH iff cli arm": bool(on_path) == (arm in CLI_ARMS),
            "pymolt MCP iff mcp arm": any(m.startswith("pymolt") for m in mcp) == (arm in MCP_ARMS),
        }
        if authed:
            checks["agent Bash: pymolt iff cli arm"] = (
                ("/pymolt" in answer and "NONE" not in answer) == (arm in CLI_ARMS))
        else:
            unverified.append(arm)
            print(f"    (not authenticated — agent-side PATH check skipped: {answer[:60]!r})")
        ok &= all(checks.values())
        print(f"[{arm}] skills(pymolt)={pym_skills} plugins={plugins} mcp={mcp} "
              f"hooks={hooks} PATH pymolt={on_path} cost=${result.get('total_cost_usd') or 0:.4f}"
              f" result={answer[:60]!r}")
        for name, passed in checks.items():
            print(f"    {'ok  ' if passed else 'FAIL'} {name}")
    if not ok:
        verdict, rc = "FAILED", 1
    elif unverified:
        verdict, rc = (f"OK for init + env PATH; agent-side PATH UNVERIFIED for "
                       f"{unverified} (not authenticated)"), 2
    else:
        verdict, rc = "OK", 0
    print(f"isolation {verdict}; spent ${spent:.4f}")
    return rc


PROBE_PROMPT = ("Run exactly this shell command with your Bash tool: "
                "command -v pymolt || echo NONE\nThen reply with only its output.")


def _schedule(task_ids: list[str], arms: list[str], repeats: int):
    """Repeat-major, task-interleaved, arm order rotated each repeat (a Latin
    square over position), so API drift and a mid-run budget stop both hit every
    arm and task evenly."""
    for i in range(repeats):
        for t, task_id in enumerate(task_ids):
            k = (i + t) % len(arms)
            for arm in arms[k:] + arms[:k]:
                yield i, task_id, arm


def _auth_failed(r: dict) -> bool:
    return bool(r.get("is_error")) and "authenticate" in (r.get("result_text") or "").lower()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--tasks", default=",".join(TASKS))
    ap.add_argument("--arms", default=",".join(ARMS))
    ap.add_argument("--max-turns", type=int, default=40)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--tag", default="")
    ap.add_argument("--budget-usd", type=float, default=None,
                    help="hard cap on summed total_cost_usd; no run starts once "
                         "spent + the costliest run so far would exceed it")
    ap.add_argument("--est-run-usd", type=float, default=0.75,
                    help="assumed cost of a run before one has been observed "
                         "(round 1-2 Sonnet T3 bare median: $0.69)")
    ap.add_argument("--spent-usd", type=float, default=0.0,
                    help="already spent against the cap (smoke/probe runs)")
    ap.add_argument("--probe-isolation", action="store_true",
                    help="verify skill/plugin/hook/PATH isolation per arm and exit")
    args = ap.parse_args()

    task_ids = [t.strip() for t in args.tasks.split(",") if t.strip()]
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    if args.probe_isolation:
        return probe_isolation(arms, args.model)
    runs = []
    spent = args.spent_usd
    costliest = args.est_run_usd

    for i, task_id, arm in _schedule(task_ids, arms, args.repeats):
        if args.budget_usd is not None and spent + costliest > args.budget_usd:
            print(f"BUDGET STOP: spent ${spent:.4f}, next run could cost up to "
                  f"${costliest:.4f}, cap ${args.budget_usd:.2f}", file=sys.stderr, flush=True)
            args.stopped_by_budget = True
            break
        ws = prepare_workspace(TASKS[task_id]["fixture"])
        print(f"[{task_id}/{arm}/{args.model} #{i+1}] running...", file=sys.stderr, flush=True)
        try:
            r = run_arm(task_id, arm, args.model, ws, args.max_turns, args.timeout)
        except subprocess.TimeoutExpired:
            r = {"task": task_id, "arm": arm, "model": args.model,
                 "error": f"timeout after {args.timeout}s"}
        r["repeat"] = i + 1
        cost = r.get("total_cost_usd") or 0.0
        spent += cost
        costliest = max(costliest, cost)
        r["spent_usd_cumulative"] = round(spent, 4)
        if r.get("session_id"):
            r.update(_transcript_metrics(r["session_id"]) or {})
        runs.append(r)
        if "error" in r:
            print(f"    ERROR: {r['error']}", file=sys.stderr, flush=True)
        else:
            print(f"    turns={r['num_turns']} out={r['output_tokens']} "
                  f"peak={r.get('peak_context_tokens')} tool_data={r.get('tool_result_tokens')} "
                  f"pymolt={r.get('pymolt_calls')} (mcp {r.get('pymolt_mcp_calls')}, "
                  f"cli {r.get('pymolt_cli_calls')}) ${cost:.4f} (spent ${spent:.4f}) "
                  f"{r['wall_ms']/1000:.1f}s", file=sys.stderr, flush=True)
        # write incrementally so a crash never loses completed runs
        args.spent_total = round(spent, 4)
        _dump(runs, args)
        if _auth_failed(r):
            print("AUTH FAILURE: `claude -p` is not logged in (run `claude auth login` "
                  "or set ANTHROPIC_API_KEY); stopping.", file=sys.stderr, flush=True)
            args.stopped_by_auth = True
            break

    args.spent_total = round(spent, 4)
    _dump(runs, args, final=True)
    return 0


def _transcript_metrics(session_id: str) -> dict | None:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from transcript_metrics import metrics_for
    finally:
        sys.path.pop(0)
    return metrics_for(session_id)


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
            "cli_tools": CLI_TOOLS,
            "isolation": ISOLATION,
            "cli_plugin_dir": str(CLI_PLUGIN_DIR.relative_to(REPO)),
            "arms": args.arms,
            "tasks_run": args.tasks,
            "schedule": "repeat-major, tasks interleaved, arm order rotated per repeat",
            "budget_usd": args.budget_usd,
            "spent_usd_start": args.spent_usd,
            "spent_usd_total": getattr(args, "spent_total", None),
            "stopped_by_budget": getattr(args, "stopped_by_budget", False),
            "stopped_by_auth": getattr(args, "stopped_by_auth", False),
            "complete": final,
        },
        "tasks": {k: {"fixture": v["fixture"], "prompt": render_prompt(k)}
                  for k, v in TASKS.items()},
        "runs": runs,
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    tag = f"_{args.tag}" if args.tag else ""
    (RESULTS / f"mcp_{args.model}{tag}.json").write_text(json.dumps(payload, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
