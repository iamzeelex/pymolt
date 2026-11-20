#!/usr/bin/env python3
"""Cache-jitter-free metrics, recovered from each run's session transcript.

`total_tokens` from the result envelope is dominated by cache_read, which swings
with prompt-cache hits between runs and says nothing about how much work the
agent did. These do not:

  peak_context_tokens  — largest context the model held (max over assistant
                         turns of input+cache_read+cache_creation). This is the
                         real "how full did the window get" number.
  tool_result_tokens   — total volume the agent pulled off disk into the
                         context (sum of every tool_result block). This is the
                         number PyMolt is supposed to shrink.
  tool_calls           — which tools were actually used.
  pymolt_calls         — PyMolt use over BOTH transports: MCP tool calls
                         (mcp__pymolt__*) plus Bash commands that invoke the
                         `pymolt` CLI (pymolt_mcp_calls / pymolt_cli_calls, and
                         pymolt_cmds: which subcommands).
  pymolt_result_tokens — the part of tool_result_tokens that PyMolt returned.
"""
from __future__ import annotations

import collections
import json
import re
import statistics
import sys
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"
RESULTS = Path(__file__).resolve().parent / "results"
ARMS = ("bare", "mcp_only", "plugin", "cli_skill")

# A shell segment boundary: ; && || | newline ( $( `
_SEGMENTS = re.compile(r"\$\(|&&|\|\||[;|\n(`]")
_PREFIXES = {"time", "env", "nohup", "command", "exec", "uv", "run", "uvx"}


def pymolt_subcommand(cmd: str) -> str | None:
    """`pymolt contract map . --json` -> 'contract map'; None unless some shell
    segment of `cmd` actually runs pymolt (so `which pymolt` / `grep pymolt` don't count)."""
    for seg in _SEGMENTS.split(cmd or ""):
        words = seg.split()
        while words and ("=" in words[0] or words[0] in _PREFIXES
                         or words[0].isdigit() or words[0] == "timeout"):
            words.pop(0)
        if not words or words[0].rsplit("/", 1)[-1] != "pymolt":
            continue
        sub = []
        for w in words[1:]:
            if w.startswith("-") or w in (".", ">", "2>&1") or "/" in w:
                break
            sub.append(w)
            if sub[0] not in ("contract", "env", "config", "auth") or len(sub) == 2:
                break
        return " ".join(sub) or "(bare)"
    return None


def _enc():
    try:
        import tiktoken
        e = tiktoken.get_encoding("cl100k_base")
        return lambda s: len(e.encode(s))
    except Exception:
        return lambda s: max(1, len(s) // 4)


TOK = _enc()


def _text_of(block) -> str:
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(x.get("text", "") if isinstance(x, dict) else str(x) for x in c)
    return json.dumps(c) if c is not None else ""


def metrics_for(session_id: str) -> dict | None:
    for f in PROJECTS.glob(f"*/{session_id}.jsonl"):
        peak = 0
        tool_result_tokens = 0
        tool_result_blocks = 0
        names: list[str] = []
        cli_cmds: list[str] = []
        pymolt_ids: set[str] = set()
        pymolt_result_tokens = 0
        for line in f.read_text(errors="ignore").splitlines():
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = o.get("message") or {}
            u = msg.get("usage") or {}
            if u:
                ctx = (u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
                       + u.get("cache_creation_input_tokens", 0))
                peak = max(peak, ctx)
            content = msg.get("content")
            if isinstance(content, list):
                for it in content:
                    if not isinstance(it, dict):
                        continue
                    if it.get("type") == "tool_use":
                        names.append(it["name"])
                        if it["name"].startswith("mcp__pymolt"):
                            pymolt_ids.add(it.get("id"))
                        elif it["name"] == "Bash":
                            sub = pymolt_subcommand((it.get("input") or {}).get("command", ""))
                            if sub:
                                cli_cmds.append(sub)
                                pymolt_ids.add(it.get("id"))
                    elif it.get("type") == "tool_result":
                        n = TOK(_text_of(it))
                        tool_result_tokens += n
                        tool_result_blocks += 1
                        if it.get("tool_use_id") in pymolt_ids:
                            pymolt_result_tokens += n
        return {
            "peak_context_tokens": peak,
            "tool_result_tokens": tool_result_tokens,
            "tool_result_blocks": tool_result_blocks,
            "tool_calls": dict(collections.Counter(names)),
            "pymolt_mcp_calls": (mcp := sum(1 for n in names if n.startswith("mcp__pymolt"))),
            "pymolt_cli_calls": len(cli_cmds),
            "pymolt_calls": mcp + len(cli_cmds),
            "pymolt_cmds": dict(collections.Counter(cli_cmds)),
            "pymolt_result_tokens": pymolt_result_tokens,
        }
    return None


def main(paths: list[str]) -> int:
    files = [Path(p) for p in paths] or sorted(RESULTS.glob("mcp_*.json"))
    for f in files:
        d = json.loads(f.read_text())
        for r in d["runs"]:
            sid = r.get("session_id")
            if sid and "pymolt_cli_calls" not in r:
                m = metrics_for(sid)
                if m:
                    r.update(m)
        f.write_text(json.dumps(d, indent=2))

        print(f"\n{'='*100}\n{f.name}   model={d['meta']['model']}\n{'='*100}")
        print(f"{'task':<12}{'arm':<10}{'n':>2}{'peak ctx':>11}{'tool data':>11}{'turns':>7}"
              f"{'out tok':>9}{'wall s':>8}{'pymolt':>8}  tools")
        rows = collections.defaultdict(list)
        for r in d["runs"]:
            if "error" not in r and "peak_context_tokens" in r:
                rows[(r["task"], r["arm"])].append(r)
        for task in sorted({k[0] for k in rows}):
            for arm in ARMS:
                rs = rows.get((task, arm))
                if not rs:
                    continue
                m = lambda k: statistics.median([x[k] for x in rs])
                tools = collections.Counter()
                for x in rs:
                    tools.update(x["tool_calls"])
                short = {k.replace("mcp__pymolt__", "P:"): v for k, v in tools.items()}
                print(f"{task:<12}{arm:<10}{len(rs):>2}{m('peak_context_tokens'):>11,.0f}"
                      f"{m('tool_result_tokens'):>11,.0f}{m('num_turns'):>7.0f}"
                      f"{m('output_tokens'):>9,.0f}{m('wall_ms')/1000:>8.1f}"
                      f"{m('pymolt_calls'):>8.0f}  {short}")
            print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
