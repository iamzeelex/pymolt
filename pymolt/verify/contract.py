"""The interaction contract: compare the boundary by SHAPE, not by concrete data.

A migration question is "given the same kind of input, does the dependency still produce the
same kind of output?" — *kind*, not value. A function that returned a ``str`` still returning a
``str`` is contract-stable even if the string itself changed (a venv path, a User-Agent, a
timestamp). What is invariant and migration-critical is the **shape**: did it return or raise,
the type of the result, the structure (dict fields / tuple arity / element types).

So this layer reduces every captured value to a *shape* (leaves -> type token; containers ->
structure of shapes; opaque/nondeterministic markers kept as-is), re-keys contacts by
``(qualname, input-shape)``, and diffs the output-shape across versions. Concrete values are
deliberately NOT part of the contract here (that is a later, opt-in "inject the data" step).

Built on the value-normalized records (``diff._load``) — capture stays value-rich, the contract
is derived at compare time. Returns the same ``BoundaryDiff`` shape so the renderer is reused.
"""
import json
from typing import Any

from pymolt.verify.diff import _load
from pymolt.verify.models import BoundaryDiff

_MARKERS = ("__opaque__", "__nondeterministic__", "__cycle__")


def shape_of(value: Any) -> Any:
    """Reduce a value-normalized structure to its shape (type/structure skeleton)."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if value is None:
        return "null"
    if isinstance(value, dict):
        for m in _MARKERS:
            if m in value:
                return value            # already type-level (opaque/nondet/cycle): keep
        if "__bytes__" in value:
            return "bytes"
        if "__list__" in value:
            return {"list": _union(value["__list__"])}
        if "__set__" in value:
            return {"set": _union(value["__set__"])}
        if "__tuple__" in value:
            return {"tuple": [shape_of(e) for e in value["__tuple__"]]}  # arity + positions
        if "__dict__" in value:
            # field NAMES are part of the contract; field VALUES become shapes.
            fields = {}
            for k, v in value["__dict__"]:
                fields[_canon(k)] = shape_of(v)
            return {"dict": fields}
    return "unknown"


def _union(elems: list) -> list:
    """Distinct element shapes of a sequence (so [1,2,3] and [1,2] share shape list<int>)."""
    seen = {json.dumps(shape_of(e), sort_keys=True) for e in elems}
    return sorted(json.loads(s) for s in seen) if seen else []


def _canon(k: Any) -> str:
    return k if isinstance(k, str) else json.dumps(shape_of(k), sort_keys=True)


def shape_str(shape: Any) -> str:
    """Compact human string for a shape, e.g. ``dict{a:int,b:str}`` / ``list[int]``."""
    if isinstance(shape, str):
        return shape
    if isinstance(shape, dict):
        if "__opaque__" in shape:
            return "opaque:" + shape["__opaque__"]
        if "__nondeterministic__" in shape:
            return "nondet"
        if "__cycle__" in shape:
            return "cycle"
        if "list" in shape:
            return "list[" + "|".join(shape_str(s) for s in shape["list"]) + "]"
        if "set" in shape:
            return "set[" + "|".join(shape_str(s) for s in shape["set"]) + "]"
        if "tuple" in shape:
            return "tuple(" + ",".join(shape_str(s) for s in shape["tuple"]) + ")"
        if "dict" in shape:
            return "dict{" + ",".join(f"{k}:{shape_str(v)}" for k, v in shape["dict"].items()) + "}"
    return "unknown"


def _input_shape(inputs: Any) -> Any:
    """Shape of the bound arguments (the input side of the contract)."""
    return shape_of(inputs)


def _fold_contracts(path: str) -> dict:
    """Re-key value-records by (qualname, input-shape); collect output-shapes + raised."""
    contracts: dict = {}
    for rec in _load(path).values():
        in_shape = _input_shape(rec.get("inputs"))
        ckey = json.dumps({"q": rec["qualname"], "in": in_shape}, sort_keys=True)
        c = contracts.get(ckey)
        if c is None:
            c = {"qualname": rec["qualname"], "in_shape": in_shape,
                 "out_shapes": set(), "raised": set(), "where": rec.get("where")}
            contracts[ckey] = c
        c["out_shapes"].add(json.dumps(shape_of(rec.get("result")), sort_keys=True))
        c["raised"].add(rec.get("raised"))
    return contracts


def build_contract_diff(old_path: str, new_path: str) -> BoundaryDiff:
    """Diff two recordings by interaction *shape* (type/structure), ignoring concrete data."""
    old = _fold_contracts(old_path)
    new = _fold_contracts(new_path)
    diff = BoundaryDiff()

    for k, o in old.items():
        n = new.get(k)
        base = {"qualname": o["qualname"], "in_shape": shape_str(o["in_shape"]),
                "where": o["where"]}
        if n is None:
            diff.disappeared.append(base)
            continue
        # ambiguous: same input-shape produced several output-shapes on a side -> can't compare
        if len(o["out_shapes"]) > 1 or len(n["out_shapes"]) > 1:
            diff.skipped_opaque.append({**base, "reason": "polymorphic-output-shape"})
            continue
        if o["raised"] != n["raised"]:
            diff.raise_changed.append({**base,
                                       "old_raised": _one(o["raised"]),
                                       "new_raised": _one(n["raised"])})
        elif o["out_shapes"] != n["out_shapes"]:
            diff.result_changed.append({**base,
                                        "old": _shape_token_str(o["out_shapes"]),
                                        "new": _shape_token_str(n["out_shapes"])})

    for k, n in new.items():
        if k not in old:
            diff.appeared.append({"qualname": n["qualname"],
                                  "in_shape": shape_str(n["in_shape"]), "where": n["where"]})
    return diff


def _one(s: set):
    return next(iter(s)) if len(s) == 1 else sorted(str(x) for x in s)


def _shape_token_str(tokens: set) -> str:
    return shape_str(json.loads(next(iter(tokens)))) if len(tokens) == 1 else "?"
