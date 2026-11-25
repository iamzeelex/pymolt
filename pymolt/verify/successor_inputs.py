"""A successor rewrite's inputs, read back in the old call's terms.

HOST side. A ``rename-keyword`` step turns ``shift(periods=1)`` into ``shift(n=1)``; a
``forward-template`` step may write out a default the successor changed
(``with_row_count()`` -> ``with_row_index("row_nr")``). The post-migration capture then records
the call with other keys than the baseline did, and the inputs would compare as changed: an input
change, a review item, on a line the library's own code proves the same.

The apply ledger says what each such rewrite renamed and wrote out (``param_map``,
``materialized``; ``apply_ledger.successor_maps``). :func:`normalize` maps the new side's
facts at those statements back before their inputs are keyed: each renamed keyword gets its old
name, and a written-out default whose value is the old default's is moved back to the
defaulted parameters. Nothing else changes: a value that differs still differs.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping
from typing import Any


def _lines(entry: Mapping[str, Any]) -> set[int]:
    located = (entry.get("located") or {}).get("new")
    if located:
        return {int(n) for n in located}
    span = entry.get("new_lines")
    if span:
        return set(range(int(span[0]), int(span[1]) + 1))
    return set()


def _literal(text: Any) -> Any:
    try:
        return ast.literal_eval(str(text))
    except (ValueError, SyntaxError):
        return _NOT_LITERAL


_NOT_LITERAL = object()


def _rename(mapping: Any, back: Mapping[str, str]) -> Any:
    if isinstance(mapping, dict):
        if isinstance(mapping.get("__dict__"), list):      # a recorded **kwargs dict
            return {**mapping, "__dict__": [[back.get(k, k) if isinstance(k, str) else k, v]
                                            for k, v in mapping["__dict__"]]}
        return {back.get(k, k): v for k, v in mapping.items()}
    return mapping


def normalize(facts: list[Any], edits: Iterable[Mapping[str, Any]] | None) -> list[Any]:
    """``facts`` (the post-migration side's) with each fact at a statement a successor rewrite
    made taken back to the old call's keys (see the module docstring)."""
    rules = []
    for e in edits or ():
        params, written = e.get("param_map"), e.get("materialized")
        if not (params or written) or not e.get("file"):
            continue
        lines = _lines(e)
        if lines:
            rules.append((e["file"], lines, {new: old for old, new in (params or {}).items()},
                          dict(written or {})))
    if not rules:
        return facts
    out = []
    for f in facts:
        here = [r for r in rules if r[0] == f.file and
                ({f.line, *(f.lines or ())} & r[1])]
        if not here or not isinstance(f.input, dict):
            out.append(f)
            continue
        inp = dict(f.input)
        defaulted = [list(d) for d in f.defaulted] if f.defaulted is not None else None
        for _file, _lines_, back, written in here:
            if isinstance(inp.get("bound"), dict):
                bound = _rename(inp["bound"], back)
                for name, old_default in written.items():
                    old = back.get(name, name)
                    value = _literal(old_default)
                    if old in bound and value is not _NOT_LITERAL and bound[old] == value:
                        bound = {k: v for k, v in bound.items() if k != old}
                        if defaulted is not None:
                            defaulted = [sorted({*d, old}) for d in defaulted] or [[old]]
                inp["bound"] = bound
            if "kwargs" in inp:
                inp["kwargs"] = _rename(inp["kwargs"], back)
        update: dict[str, Any] = {"input": inp}
        if defaulted is not None:
            update["defaulted"] = defaulted
        out.append(f.model_copy(update=update))
    return out


__all__ = ["normalize"]
