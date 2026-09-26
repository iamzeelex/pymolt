"""Probe captured contacts under a target dependency version (sandbox → report).

For each contact in a trace whose inputs are *reconstructible* (JSON-able values,
or — later — a dill snapshot), re-invoke it under the target venv's interpreter
(:func:`reinvoke`) and compare the result to what was captured. This turns BLIND/
confirmed contacts into an active what-if verdict per dependency symbol:

* ``stable``        — same outcome and value under the new version
* ``changed``       — value differs, or a return<->raise flip
* ``error``         — could not run (import/timeout/signature mismatch)
* ``opaque-inputs`` — inputs aren't reconstructible from the normalized capture
  (needs a dill snapshot or fork-from-live-state)

Inverting the tracer's normalization (``_normalize``) reconstructs simple values;
anything opaque collapses to ``opaque-inputs`` honestly rather than guessing.
"""

import json
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from pymolt.verify.reinvoke import reinvoke

logger = logging.getLogger(__name__)

_STATUS_RANK = {"stable": 0, "opaque-inputs": 1, "error": 2, "changed": 3}


class ProbeResult(BaseModel):
    status: str            # stable | changed | error | opaque-inputs
    probed_contacts: int = 0


def _reconstruct(value: Any) -> tuple[Any, bool]:
    """Invert ``_normalize``. Returns (value, reconstructible?)."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value, True
    if not isinstance(value, dict):
        return None, False
    if {"__opaque__", "__cycle__", "__nondeterministic__", "__truncated__"} & value.keys():
        return None, False
    if "__bytes__" in value:
        try:
            return bytes.fromhex(value["__bytes__"]), True
        except (ValueError, TypeError):
            return None, False
    if "__list__" in value or "__tuple__" in value:
        key = "__list__" if "__list__" in value else "__tuple__"
        items = []
        for x in value[key]:
            rv, ok = _reconstruct(x)
            if not ok:
                return None, False
            items.append(rv)
        return (items if key == "__list__" else tuple(items)), True
    if "__set__" in value:
        items = []
        for x in value["__set__"]:
            rv, ok = _reconstruct(x)
            if not ok:
                return None, False
            items.append(rv)
        try:
            return set(items), True
        except TypeError:
            return None, False
    if "__dict__" in value:
        out: dict = {}
        for pair in value["__dict__"]:
            rk, ok1 = _reconstruct(pair[0])
            rv, ok2 = _reconstruct(pair[1])
            if not (ok1 and ok2):
                return None, False
            try:
                out[rk] = rv
            except TypeError:
                return None, False
        return out, True
    return None, False  # a plain dict without a marker shouldn't occur in normalized data


def _reconstruct_bound(bound: dict) -> tuple[dict, bool]:
    out: dict = {}
    for name, normalized in bound.items():
        val, ok = _reconstruct(normalized)
        if not ok:
            return {}, False
        out[name] = val
    return out, True


def _fold(trace_path: str | Path) -> list[tuple[str, dict, Any, str]]:
    """Distinct (qualname, bound-inputs, captured-result, kind) contacts."""
    seen: set[str] = set()
    contacts: list[tuple[str, dict, Any, str]] = []
    try:
        lines = Path(trace_path).read_text(encoding="utf-8").splitlines()
    except OSError as e:
        logger.warning("Could not read trace %s: %s", trace_path, e)
        return contacts
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        q = rec.get("q")
        if not q:
            continue
        bound = (rec.get("in") or {}).get("bound") or {}
        key = q + "|" + json.dumps(bound, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        kind = rec.get("t", "return")
        captured = rec.get("raised") if kind == "raise" else rec.get("result")
        contacts.append((q, bound, captured, kind))
    return contacts


def _classify(captured_kind: str, captured_result: Any, res) -> str:
    if res.outcome in ("error", "timeout"):
        return "error"
    new_kind = "return" if res.outcome == "returned" else "raise"
    if new_kind != captured_kind:
        return "changed"
    if new_kind == "return":
        return "stable" if res.value == captured_result else "changed"
    return "stable" if res.error == captured_result else "changed"


def probe_trace(trace_path: str | Path, target_python: str, timeout: float = 20.0) -> dict[str, ProbeResult]:
    """Re-invoke each reconstructible contact under ``target_python``; aggregate per symbol."""
    results: dict[str, ProbeResult] = {}
    for q, bound, captured_result, kind in _fold(trace_path):
        kwargs, ok = _reconstruct_bound(bound)
        if not ok:
            status, probed = "opaque-inputs", False
        else:
            res = reinvoke(target_python, q, kwargs=kwargs, timeout=timeout)
            status, probed = _classify(kind, captured_result, res), True

        cur = results.get(q)
        if cur is None:
            results[q] = ProbeResult(status=status, probed_contacts=1 if probed else 0)
        else:
            if _STATUS_RANK[status] > _STATUS_RANK[cur.status]:
                cur.status = status
            if probed:
                cur.probed_contacts += 1
    return results
