"""Test helper for verify's comparison semantics on library objects: one project file, two
captures (JSONL events written to ``tmp_path``) and a ``DiffContext`` carrying both sides'
source snapshots, the way ``verify`` builds it from a capture's ``.sources.json``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pymolt.verify.facts import DiffContext, FactDiff, diff_facts, load_facts

ROOT = "/proj"
FILE = "app.py"


def ev(sym: str, bound: dict | None = None, *, result: Any = None, raised: str | None = None,
       line: int = 3, func: str = "main", file: str = FILE, count: int | None = None,
       extra_in: dict | None = None, **rec: Any) -> dict:
    """One v2 JSONL event of a call to ``sym`` at ``file:line``."""
    e: dict[str, Any] = {"t": "raise" if raised else "return", "q": sym, "sym": sym, "v": 2,
                         "in": {"bound": dict(bound or {}), **(extra_in or {})},
                         "where": {"file": f"{ROOT}/{file}", "line": line, "func": func},
                         "ts": 0.0}
    if raised:
        e["raised"] = raised
    else:
        e["result"] = result
    if count is not None:
        e["count"] = count
    e.update(rec)
    return e


def _write(tmp: Path, name: str, events: list[dict], source: dict[str, str]) -> Path:
    p = tmp / name
    p.write_text("".join(json.dumps(e) + "\n" for e in events))
    site_text = {}
    for e in events:
        w = e["where"]
        rel = w["file"][len(ROOT) + 1:]
        lines = (source.get(rel) or "").splitlines()
        if 0 < w["line"] <= len(lines):
            site_text[f"{w['file']}:{w['line']}"] = lines[w["line"] - 1].strip()
    (tmp / f"{name}.meta.json").write_text(json.dumps({"site_text": site_text}))
    return p


def run(tmp: Path, old: list[dict], new: list[dict], source: str | dict[str, str],
        new_source: str | dict[str, str] | None = None, *, context: bool = True,
        **ctx: Any) -> FactDiff:
    """Diff two captures of the same project. ``source`` is ``app.py``'s text (or a
    ``{path: text}`` map); ``new_source`` the post side's when it differs. ``context=False``
    diffs without a ``DiffContext`` (no source snapshots: today's full comparison)."""
    old_src = {FILE: source} if isinstance(source, str) else dict(source)
    ns = source if new_source is None else new_source
    new_src = {FILE: ns} if isinstance(ns, str) else dict(ns)
    o = load_facts(_write(tmp, "old.jsonl", old, old_src), root=ROOT)
    n = load_facts(_write(tmp, "new.jsonl", new, new_src), root=ROOT)
    dc = None
    if context:
        dc = DiffContext(old_sources=old_src, new_sources=new_src, edits=[])
        for k, v in ctx.items():
            setattr(dc, k, v)
    return diff_facts(o, n, context=dc)


def entries_for(d: FactDiff, symbol: str) -> list:
    return [e for e in d.entries if e.symbol == symbol or e.new_symbol == symbol]


def one(d: FactDiff, symbol: str):
    found = entries_for(d, symbol)
    assert len(found) == 1, [(e.symbol, e.category, e.notes) for e in d.entries]
    return found[0]
