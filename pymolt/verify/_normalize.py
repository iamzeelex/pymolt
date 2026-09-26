"""Normalize runtime values into a stable, comparable, JSON-safe form.

INJECTED RUNTIME — pure stdlib, Python 3.6+. This module is loaded *inside the
target application's interpreter* (which may be Python 3.6 in the flasgger
devcontainer), so it must import nothing beyond the standard library: no pydantic,
no ``pymolt.*``. Do not add ``from __future__ import annotations`` (PEP 563 is 3.7+),
walrus, or PEP 604 ``X | Y`` unions in runtime positions.

Principle: capture what is observable and deterministic. Opaque or unstable values
collapse to a *typed honesty marker* rather than a raw ``repr`` (which would carry
memory addresses and cause false-positive diffs). This same normalization is shared
by the boundary tracer (L3) and the golden-master snapshots (L2) so a value compared
in one is represented identically in the other.

Markers emitted:
    {"__bytes__": "<hex>"}            bytes, stably encoded
    {"__opaque__": "<typename>"}      non-serializable / too-deep / non-finite float
    {"__cycle__": "<typename>"}       reference cycle broken here
    {"__nondeterministic__": true}    set by the sinks when identical inputs differ
"""
import json
import os

_SAFE_SCALARS = (type(None), bool, int, float, str, bytes)

# Generous limits: the environment is CI/staging where overhead is tolerable. These
# are a guard against infinite / cyclic structures, not a means of saving cycles.
MAX_DEPTH = 6
MAX_SEQ = 200


def typename(value):
    """Stable type label, e.g. ``flask.app.Flask`` or ``int``."""
    t = type(value)
    mod = getattr(t, "__module__", "")
    if mod and mod != "builtins":
        return "{0}.{1}".format(mod, t.__qualname__)
    return t.__qualname__


def normalize(value, depth=0, _seen=frozenset(), _privacy=None):
    # type: (Any, int, frozenset) -> Any
    """Reduce ``value`` to a deterministic JSON-compatible structure.

    Cycles are broken via ``id()`` tracking in ``_seen``. Anything not safely
    representable becomes ``{"__opaque__": typename}`` — never a raw repr.
    """
    if _privacy is None:
        _privacy = os.environ.get("PYMOLT_TRACE_PRIVACY", "values").strip().lower()

    if isinstance(value, _SAFE_SCALARS):
        if _privacy == "shape":
            return {"__shape__": typename(value)}
        if isinstance(value, bytes):
            return {"__bytes__": value.hex()}
        if isinstance(value, float):
            # NaN / +-inf are not JSON-stable and not meaningfully comparable.
            if value != value or value in (float("inf"), float("-inf")):
                return {"__opaque__": "float-nonfinite"}
        return value

    if depth >= MAX_DEPTH:
        return {"__opaque__": typename(value)}

    vid = id(value)
    if vid in _seen:
        return {"__cycle__": typename(value)}
    seen = _seen | {vid}

    if isinstance(value, (list, tuple)):
        seq = [
            normalize(v, depth + 1, seen, _privacy)
            for v in list(value)[:MAX_SEQ]
        ]
        tag = "tuple" if isinstance(value, tuple) else "list"
        out = {"__{0}__".format(tag): seq}
        if len(value) > MAX_SEQ:
            out["__truncated__"] = True
        return out

    if isinstance(value, dict):
        items = []
        for k in list(value.keys())[:MAX_SEQ]:
            if isinstance(k, _SAFE_SCALARS):
                nk = normalize(k, depth + 1, seen, _privacy)
            else:
                nk = {"__opaque__": typename(k)}
            items.append([nk, normalize(value[k], depth + 1, seen, _privacy)])
        items.sort(key=_stable_key)
        return {"__dict__": items}

    if isinstance(value, (set, frozenset)):
        norm = sorted(
            (
                normalize(v, depth + 1, seen, _privacy)
                for v in list(value)[:MAX_SEQ]
            ),
            key=_stable_key,
        )
        return {"__set__": norm}

    return {"__opaque__": typename(value)}


def _stable_key(obj):
    return json.dumps(obj, sort_keys=True, default=str)
