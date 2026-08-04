"""L2 — golden-master value snapshots.

Capture normalized values at caller-registered points under a version, and diff two snapshots.
Shares the normalization layer with the boundary tracer (``_normalize``) so a value compared
here (L2) and the same value seen in a trace (L3) are represented identically.

Registration points are caller-supplied: a mapping ``{name: value-or-zero-arg-callable}``.
Auto-discovery of good snapshot points is out of scope (Known gaps).
"""
from collections.abc import Callable, Mapping
from typing import Any

from pymolt.verify._normalize import normalize
from pymolt.verify.models import GoldenDiff

Point = Any | Callable[[], Any]  # a value, or a zero-arg callable producing one


def snapshot(points: Mapping[str, Point], version: str) -> dict[str, Any]:
    """Capture normalized values at each registration point under ``version``.

    A point may be a value or a zero-arg callable (evaluated now). A point that raises is
    recorded as a typed opaque marker rather than aborting the whole snapshot.
    """
    values: dict[str, Any] = {}
    for name, point in points.items():
        try:
            raw = point() if callable(point) else point
            values[name] = normalize(raw)
        except Exception as exc:
            values[name] = {"__opaque__": "raised:" + type(exc).__name__}
    return {"version": version, "values": values}


def _undecidable(value: Any) -> bool:
    return isinstance(value, dict) and ("__nondeterministic__" in value or "__opaque__" in value)


def diff_snapshots(old: dict[str, Any], new: dict[str, Any]) -> GoldenDiff:
    """Value-level diff of two snapshots, reusing the tracer's normalization semantics."""
    diff = GoldenDiff()
    old_vals = old.get("values", {})
    new_vals = new.get("values", {})
    for name, ov in old_vals.items():
        if name not in new_vals:
            diff.missing.append(name)
            continue
        nv = new_vals[name]
        if _undecidable(ov) or _undecidable(nv):
            diff.skipped.append(name)
            continue
        if ov != nv:
            diff.changed.append({"point": name, "old": ov, "new": nv})
    for name in new_vals:
        if name not in old_vals:
            diff.missing.append(name)
    return diff
