"""Fold two boundary recordings into a typed ``BoundaryDiff`` (host side, >=3.12).

Accepts either the aggregated JSON produced by ``MemorySink.dump`` (Mode A) or the streaming
JSONL produced by ``JsonlSink`` (Mode B) — both are folded to one shape, so watcher snapshots
and test-run snapshots compare uniformly (DoD: JSONL<->JSON fold equivalence).

This is the analysis layer, not the injected runtime: it never runs inside the target app, so
it may use the full host stack. It returns structured data only — formatting lives in
``interfaces/``.
"""
import json
from typing import Any

from pymolt.verify.models import BoundaryDiff

_NONDET = {"__nondeterministic__": True}


def _key(qualname: str, inputs: Any) -> str:
    return json.dumps({"q": qualname, "in": inputs}, sort_keys=True, default=str)


def _fold_jsonl(path: str) -> dict[str, dict]:
    """Collapse an NDJSON event stream into {key: aggregated record}, matching MemorySink."""
    records: dict[str, dict] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            ev = json.loads(line)
            if ev.get("t") not in ("return", "raise"):
                continue
            qual, inputs = ev["q"], ev["in"]
            result = ev.get("result")
            raised = ev.get("raised")
            key = _key(qual, inputs)
            if key in records:
                rec = records[key]
                rec["count"] += 1
                if rec["result"] != result or rec["raised"] != raised:
                    rec["result"] = dict(_NONDET)  # identical inputs, differing outcome
            else:
                records[key] = {"qualname": qual, "inputs": inputs, "result": result,
                                "raised": raised, "count": 1, "where": ev.get("where")}
    return records


def _load(path: str) -> dict[str, dict]:
    """Load a recording in either format into {key: record}."""
    with open(path) as f:
        text = f.read()
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None  # concatenated objects -> it's JSONL, fall through
        if isinstance(data, dict) and "records" in data:
            out: dict[str, dict] = {}
            for r in data["records"]:
                out[_key(r["qualname"], r["inputs"])] = r
            return out
    return _fold_jsonl(path)


def _undecidable(result: Any) -> bool:
    """True when a comparison must be declined: nondeterministic, or a top-level opaque
    marker (a value we could only ever compare by type — surfaced, not smoothed)."""
    return isinstance(result, dict) and ("__nondeterministic__" in result or "__opaque__" in result)


def build_boundary_diff(old_path: str, new_path: str) -> BoundaryDiff:
    """Diff two boundary recordings. ``old_path``/``new_path`` may each be JSON or JSONL."""
    old = _load(old_path)
    new = _load(new_path)
    diff = BoundaryDiff()

    for k, orec in old.items():
        nrec = new.get(k)
        if nrec is None:
            diff.disappeared.append(orec)
            continue
        where = orec.get("where") or nrec.get("where")  # call site in our code
        if _undecidable(orec.get("result")) or _undecidable(nrec.get("result")):
            diff.skipped_opaque.append({"qualname": orec["qualname"], "inputs": orec["inputs"],
                                        "where": where, "reason": "opaque-or-nondeterministic"})
            continue
        if orec.get("raised") != nrec.get("raised"):
            diff.raise_changed.append({"qualname": orec["qualname"], "inputs": orec["inputs"],
                                       "where": where, "old_raised": orec.get("raised"),
                                       "new_raised": nrec.get("raised")})
        elif orec.get("result") != nrec.get("result"):
            diff.result_changed.append({"qualname": orec["qualname"], "inputs": orec["inputs"],
                                        "where": where, "old": orec.get("result"),
                                        "new": nrec.get("result")})

    for k, nrec in new.items():
        if k not in old:
            diff.appeared.append(nrec)

    return diff
