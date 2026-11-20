"""Dict key insertion order (value format ``dict_order`` 1): the host side channel.

HOST side (>=3.12). Never imported by the injected runtime.

The runtime keeps a dict's pairs sorted (``{"__dict__": [[k, v], ...]}``) and, for a dict of
2..64 keys whose insertion order is not its sorted order, adds ``"__order__": [s0, s1, ...]``:
``s_j`` is the sorted slot of the j-th inserted key. The host strips it at load
(:func:`strip`) so that keys, pairing, masks, groups and value comparison see exactly the
values they always saw; the order rides next to the value as ``{path: permutation}``.

A *path* names a dict node by its position in the (stripped) value: JSON object keys and list
indices from the root, e.g. ``["kwargs"]`` or ``["__list__", 0]``. Positions survive masking
(masks rewrite leaves, never structure), so a path found in the raw value finds the same node
in the masked one, whose keys are then the masked keys.

Per fact, the orders of the calls folded into it merge (:func:`merge`): equal permutations
stay, different ones (including a permutation against none, which is the sorted order) become
``VARIES`` (``"*"``): the order varied within one run, so it is never compared there.
"""
from __future__ import annotations

import json
import math
from collections.abc import Iterator, Mapping
from typing import Any

ORDER_KEY = "__order__"
ORDER_MARK = '"__order__"'  # the raw-line test of the load fast path
VARIES = "*"     # seen in two orders within one side's fact
UNKNOWN = "?"    # a malformed permutation: not compared
MIN_KEYS, MAX_KEYS = 2, 64

Orders = dict[str, Any]   # path key -> permutation (list[int]) | VARIES | UNKNOWN


def path_key(path: list) -> str:
    return json.dumps(path, separators=(",", ":"))


def strip(value: Any) -> tuple[Any, Orders | None]:
    """``value`` without any ``__order__``, and its orders (None when it carried none)."""
    orders: Orders = {}
    out = _strip(value, [], orders)
    return out, (orders or None)


def _strip(v: Any, path: list, orders: Orders) -> Any:
    if isinstance(v, dict):
        if ORDER_KEY in v:
            perm = v[ORDER_KEY]
            pairs = v.get("__dict__")
            if _valid(perm, pairs):
                if perm != list(range(len(perm))):
                    orders[path_key(path)] = list(perm)
            else:
                orders[path_key(path)] = UNKNOWN
        out = {}
        for k, x in v.items():
            if k == ORDER_KEY:
                continue
            path.append(k)
            out[k] = _strip(x, path, orders)
            path.pop()
        return out
    if isinstance(v, list):
        out_list = []
        for i, x in enumerate(v):
            path.append(i)
            out_list.append(_strip(x, path, orders))
            path.pop()
        return out_list
    return v


def _valid(perm: Any, pairs: Any) -> bool:
    if not isinstance(perm, list) or not isinstance(pairs, list):
        return False
    n = len(pairs)
    if len(perm) != n or not MIN_KEYS <= n <= MAX_KEYS:
        return False
    if not all(isinstance(i, int) and not isinstance(i, bool) for i in perm):
        return False
    return sorted(perm) == list(range(n))


def merge(a: Orders | None, b: Orders | None) -> Orders | None:
    """The orders of one fact seen twice (same stripped value): equal stays, else VARIES."""
    if a == b:
        return a
    if not a or not b:
        base = a or b or {}
        return {p: (UNKNOWN if o == UNKNOWN else VARIES) for p, o in base.items()}
    out: Orders = {}
    for p in a.keys() | b.keys():
        x, y = a.get(p), b.get(p)
        if UNKNOWN in (x, y):
            out[p] = UNKNOWN
        elif x == y:
            out[p] = x
        else:
            out[p] = VARIES
    return out


def merge_all(items: list[Orders | None]) -> Orders | None:
    """``merge`` over several occurrences (the first one's object is kept when all agree)."""
    if not items:
        return None
    acc = items[0]
    for o in items[1:]:
        acc = merge(acc, o)
    return acc


def positions(value: Any) -> dict[str, list]:
    """Every dict node of 2..64 keys in a stripped value: ``{path key: its pairs}``."""
    out: dict[str, list] = {}
    _positions(value, [], out)
    return out


def _positions(v: Any, path: list, out: dict[str, list]) -> None:
    if isinstance(v, dict):
        pairs = v.get("__dict__")
        if isinstance(pairs, list) and MIN_KEYS <= len(pairs) <= MAX_KEYS \
                and all(isinstance(p, list) and len(p) == 2 for p in pairs):
            out[path_key(path)] = pairs
        for k, x in v.items():
            if isinstance(x, dict | list):
                path.append(k)
                _positions(x, path, out)
                path.pop()
    elif isinstance(v, list):
        for i, x in enumerate(v):
            if isinstance(x, dict | list):
                path.append(i)
                _positions(x, path, out)
                path.pop()


def has_dict(value: Any, min_keys: int = MIN_KEYS) -> bool:
    """Whether a value holds a dict of at least ``min_keys`` keys anywhere."""
    if isinstance(value, dict):
        pairs = value.get("__dict__")
        if isinstance(pairs, list) and len(pairs) >= min_keys:
            return True
        return any(has_dict(x, min_keys) for x in value.values() if isinstance(x, dict | list))
    if isinstance(value, list):
        return any(has_dict(x, min_keys) for x in value if isinstance(x, dict | list))
    return False


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def key_label(canon_key: str) -> str:
    """A canonical key as a person reads it: a str key bare, anything else as JSON."""
    try:
        v = json.loads(canon_key)
    except ValueError:
        return canon_key
    return v if isinstance(v, str) else canon_key


class Compared:
    """What comparing the orders of one value on two sides found."""

    __slots__ = ("diffs", "agreed", "varies", "collisions", "unknown")

    def __init__(self) -> None:
        # (path, old keys in insertion order, new keys in insertion order) where they differ
        self.diffs: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []
        self.agreed: list[tuple[str, ...]] = []   # sorted key sets whose orders agreed
        self.varies: list[str] = []               # paths whose order varies within one run
        self.collisions = 0                       # masked keys that collide: not comparable
        self.unknown = 0

    def extend(self, other: Compared) -> None:
        self.diffs.extend(other.diffs)
        self.agreed.extend(other.agreed)
        self.varies.extend(other.varies)
        self.collisions += other.collisions
        self.unknown += other.unknown


def sequence(pairs: list, perm: Any) -> tuple[str, ...]:
    """A dict node's keys (canonical JSON) in insertion order."""
    keys = [_canon(p[0]) for p in pairs]
    if isinstance(perm, list) and len(perm) == len(keys):
        return tuple(keys[i] for i in perm)
    return tuple(keys)


def compare(old_value: Any, old_orders: Orders | None, new_value: Any,
            new_orders: Orders | None) -> Compared:
    """The order differences between two (masked, stripped) values: at every dict node present
    on both sides at the same path with the same key set (and k >= 2); a node whose masked keys
    collide, or whose order varied within one run on either side, is not compared."""
    out = Compared()
    old_orders, new_orders = old_orders or {}, new_orders or {}
    a, b = positions(old_value), positions(new_value)
    for p, pa in a.items():
        pb = b.get(p)
        if pb is None:
            continue
        ka = [_canon(x[0]) for x in pa]
        kb = [_canon(x[0]) for x in pb]
        if sorted(ka) != sorted(kb):
            continue          # another key set: structure, the existing rules' business
        if len(set(ka)) != len(ka):
            out.collisions += 1
            continue
        oa, ob = old_orders.get(p), new_orders.get(p)
        if VARIES in (oa, ob):
            out.varies.append(p)
            continue
        if UNKNOWN in (oa, ob):
            out.unknown += 1
            continue
        sa, sb = sequence(pa, oa), sequence(pb, ob)
        if sa != sb:
            out.diffs.append((p, sa, sb))
        else:
            out.agreed.append(tuple(sorted(ka)))
    return out


def keyset_id(keys: tuple[str, ...] | list[str]) -> str:
    """A dict key set as a short stable id, ``<k>:<digest>`` (replicate evidence keys)."""
    import hashlib

    ks = sorted(keys)
    return f"{len(ks)}:" + hashlib.sha1(_canon(ks).encode("utf-8")).hexdigest()[:16]


def keyset_size(ident: str) -> int:
    try:
        return int(ident.split(":", 1)[0])
    except ValueError:
        return 0


def chance(key_sets: list[tuple[str, ...]]) -> float:
    """The chance that hash-seeded orders of these distinct key sets all agree by luck:
    the product of 1/k! over the distinct key sets."""
    p = 1.0
    for ks in set(key_sets):
        p /= math.factorial(len(ks))
    return p


def describe(path: str, old: tuple[str, ...], new: tuple[str, ...], value: Any = None,
             n: int = 8) -> dict[str, Any]:
    """The report form of one order difference: path, key count, first keys of each side."""
    return {"path": path_label(path, value), "keys": len(old),
            "old": [key_label(k) for k in old[:n]], "new": [key_label(k) for k in new[:n]]}


def path_label(path: str, value: Any = None) -> str:
    """``["kwargs","__dict__",1,1]`` -> ``kwargs.<key of pair 1>``: dict keys by name when the
    value is given (else by sorted slot), list indices in brackets, structural tags dropped."""
    try:
        steps = json.loads(path)
    except ValueError:
        return path
    out = ""
    node = value
    i = 0
    while i < len(steps):
        s = steps[i]
        if s == "__dict__" and i + 1 < len(steps):
            slot = steps[i + 1]
            pairs = node.get("__dict__") if isinstance(node, dict) else None
            if isinstance(pairs, list) and isinstance(slot, int) and 0 <= slot < len(pairs):
                out += ("." if out else "") + key_label(_canon(pairs[slot][0]))
                node = pairs[slot][1]
            else:
                out += f"[{slot}]"
                node = None
            i += 3          # "__dict__", slot, 1 (the pair's value)
            continue
        if isinstance(node, dict):
            node = node.get(s)
        elif isinstance(node, list) and isinstance(s, int) and 0 <= s < len(node):
            node = node[s]
        else:
            node = None
        if isinstance(s, int):
            out += f"[{s}]"
        elif not (s.startswith("__") and s.endswith("__")):
            out += ("." if out else "") + s
        i += 1
    return out or "<value>"


def iter_diff_labels(diffs: list[tuple[str, tuple[str, ...], tuple[str, ...]]],
                     value: Any = None) -> Iterator[str]:
    for path, old, new in diffs:
        shown_old = ", ".join(key_label(k) for k in old[:4]) + (", …" if len(old) > 4 else "")
        shown_new = ", ".join(key_label(k) for k in new[:4]) + (", …" if len(new) > 4 else "")
        yield f"at {path_label(path, value)} ({len(old)} keys): {shown_old} -> {shown_new}"


def is_order_explainable(old: Any, new: Any) -> int | None:
    """A str or bytes output that is a permutation of the other side's (same type, equal
    length, equal multiset of characters or bytes, different): its length, else None. The only
    output difference a changed key order alone can explain."""
    if isinstance(old, str) and isinstance(new, str):
        if old != new and len(old) == len(new) and sorted(old) == sorted(new):
            return len(old)
        return None
    if isinstance(old, Mapping) and isinstance(new, Mapping) \
            and isinstance(old.get("__bytes__"), str) and isinstance(new.get("__bytes__"), str) \
            and len(old) == 1 and len(new) == 1:
        try:
            a, b = bytes.fromhex(old["__bytes__"]), bytes.fromhex(new["__bytes__"])
        except ValueError:
            return None
        if a != b and len(a) == len(b) and sorted(a) == sorted(b):
            return len(a)
    return None
