#!/usr/bin/env python3
"""Recover the actual tool calls of each benchmark run from its session transcript.

`claude -p --output-format json` reports usage but not which tools were called;
the transcript under ~/.claude/projects/<slug>/<session_id>.jsonl does.
"""
from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

PROJECTS = Path.home() / ".claude" / "projects"


def tools_for(session_id: str) -> dict[str, int] | None:
    for f in PROJECTS.glob(f"*/{session_id}.jsonl"):
        names: list[str] = []
        for line in f.read_text(errors="ignore").splitlines():
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            content = (o.get("message") or {}).get("content")
            if isinstance(content, list):
                for it in content:
                    if isinstance(it, dict) and it.get("type") == "tool_use":
                        names.append(it["name"])
        return dict(collections.Counter(names))
    return None


def main(paths: list[str]) -> int:
    files = [Path(p) for p in paths] or sorted((Path(__file__).parent / "results").glob("mcp_*.json"))
    for f in files:
        d = json.loads(f.read_text())
        changed = False
        for r in d["runs"]:
            sid = r.get("session_id")
            if sid and not r.get("tools_called"):
                t = tools_for(sid)
                if t is not None:
                    r["tools_called"] = t
                    changed = True
        if changed:
            f.write_text(json.dumps(d, indent=2))
        print(f"\n== {f.name}")
        agg: dict[tuple[str, str], collections.Counter] = {}
        for r in d["runs"]:
            if "tools_called" not in r:
                continue
            agg.setdefault((r["task"], r["arm"]), collections.Counter()).update(r["tools_called"])
        for (task, arm), c in sorted(agg.items()):
            pym = sum(v for k, v in c.items() if k.startswith("mcp__pymolt"))
            tot = sum(c.values())
            print(f"  {task:<12}{arm:<10} {tot:>3} calls, {pym:>2} pymolt   {dict(c)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
