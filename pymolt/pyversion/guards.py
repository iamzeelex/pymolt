"""Evaluate a stdlib site's *version* guard against the target Python.

The contact-map pass records a guard as raw text (``{"kind": "version", "detail": "sys.version_info
< (3, 3)"}``): the condition under which the site's branch runs. It is evaluated here, at rating
time, against the target. Returns True (the branch runs on the target), False (it is dead there)
or None (not evaluable: the site stays listed, not rated).
"""

from __future__ import annotations

import ast
import operator
from collections.abc import Callable

_OPS = {ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
        ast.Eq: operator.eq, ast.NotEq: operator.ne}
_PY2 = {"PY2", "IS_PY2", "PY_2", "is_py2", "PYTHON2"}
_PY3 = {"PY3", "IS_PY3", "PY_3", "is_py3", "PYTHON3"}


class _Unknown(Exception):
    pass


def parse_version(value: str | None) -> tuple[int, int] | None:
    parts = (value or "").strip().lstrip("python").split(".")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        return int(parts[0]), int(parts[1])
    return None


def evaluate_guard(detail: str, target: tuple[int, int],
                   present: Callable[[str], bool | None] | None = None) -> bool | None:
    """Does the guarded branch run on ``target``? (``present("mod.name")`` answers
    ``hasattr(mod, "name")`` guards from data.)"""
    try:
        tree = ast.parse(detail.strip(), mode="eval")
    except (SyntaxError, ValueError):
        return None
    try:
        return bool(_eval(tree.body, target, present))
    except _Unknown:
        return None


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        head = _dotted(node.value)
        return f"{head}.{node.attr}" if head else None
    return None


def _eval(node: ast.AST, target: tuple[int, int], present) -> object:
    if isinstance(node, ast.BoolOp):
        values = []
        for item in node.values:
            try:
                values.append(bool(_eval(item, target, present)))
            except _Unknown:
                values.append(None)
        if isinstance(node.op, ast.And):
            if False in values:
                return False
            if None in values:
                raise _Unknown
            return True
        if True in values:
            return True
        if None in values:
            raise _Unknown
        return False
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _eval(node.operand, target, present)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Tuple):
        return tuple(_eval(e, target, present) for e in node.elts)
    dotted = _dotted(node)
    if dotted is not None:
        leaf = dotted.rsplit(".", 1)[-1]
        if leaf in _PY2:
            return target[0] == 2
        if leaf in _PY3:
            return target[0] == 3
    if isinstance(node, ast.Call):
        func = _dotted(node.func)
        if func == "hasattr" and len(node.args) == 2 and present is not None:
            module, name = _dotted(node.args[0]), node.args[1]
            if module and isinstance(name, ast.Constant) and isinstance(name.value, str):
                found = present(f"{module}.{name.value}")
                if found is None:
                    raise _Unknown
                return found
        raise _Unknown
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        op = _OPS.get(type(node.ops[0]))
        if op is None:
            raise _Unknown
        left = _version_value(node.left, target)
        if left is not None:
            return op(left, _eval(node.comparators[0], target, present))
        # constant on the left: ``(3, 3) > sys.version_info``
        left_const = _eval(node.left, target, present)
        right_ver = _version_value(node.comparators[0], target)
        if right_ver is None:
            raise _Unknown
        return op(left_const, right_ver)
    raise _Unknown


def _version_value(node: ast.AST, target: tuple[int, int]) -> object | None:
    """``sys.version_info`` and its slices / items / fields as a value (micro treated as 0)."""
    full = (target[0], target[1], 0, "final", 0)
    dotted = _dotted(node)
    if dotted in ("sys.version_info", "version_info"):
        return full
    if dotted in ("sys.version_info.major", "version_info.major"):
        return target[0]
    if dotted in ("sys.version_info.minor", "version_info.minor"):
        return target[1]
    if isinstance(node, ast.Subscript) and _dotted(node.value) in ("sys.version_info",
                                                                   "version_info"):
        index = node.slice
        if isinstance(index, ast.Constant) and isinstance(index.value, int):
            try:
                return full[index.value]
            except IndexError:
                return None
        if isinstance(index, ast.Slice):
            def bound(part):
                return None if part is None else (part.value if isinstance(part, ast.Constant)
                                                  and isinstance(part.value, int) else False)
            lo, hi = bound(index.lower), bound(index.upper)
            if lo is False or hi is False or index.step is not None:
                return None
            return full[lo:hi]
    return None
