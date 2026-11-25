"""Re-basing (design 3.2, rule 3): a step's recipe run again on another text, at its statements.

HOST side, pure over texts. A step whose file moved on (an earlier step skipped or rolled back,
a hand edit elsewhere, a reformat) is re-based when its old statements still resolve: the
recipe runs on the current text with its sites restricted to the lines of those statements,
and ``workorder.rules`` keeps the result only when its effect equals the step's and it changes
exactly those statements.

How a recipe is restricted:

- the import moves and the Python fixes (``rewrite-import``, the move groups, ``rewrite-attr``,
  ``replace-attr-path``, ``rewrite-module``, a python ``rename-call``) take the lines
  themselves (``apply_patterns(lines_for=...)``): they rewrite only the statements covering
  them, and a rename follows its binding through the whole scope, so a new reference made by
  hand is renamed too, changes the effect, and makes the step stale;
- a dependency ``rename-call`` and a rule cannot be told where to stop: they run on the whole
  text, and only the changes that meet the lines are kept, with any import they insert at module
  level. A successor pattern (``rename-keyword``, ``rename-member``, ``forward-template``) runs
  the same way, on its own anchors re-resolved in the current text, so a site whose statement
  changed is no longer one of them. When that leaves out a change and the kept ones remove or
  replace an import, re-basing is refused: the left-out site may still need it.

The recipe sees the current text, binding-aware (LibCST's scope analysis), so a statement whose
context changed its meaning gets the recipe's answer for the new context; the effect check and
apply's compile do the rest.
"""

from __future__ import annotations

from pymolt.workorder.edits import opcodes, split_lines
from pymolt.workorder.models import PlanStep, StepEdit

_NATIVE = ("rewrite-import", "rewrite-attr", "replace-attr-path", "rewrite-module")


def _native(pattern) -> bool:
    return pattern.kind in _NATIVE or (pattern.kind == "rename-call"
                                       and getattr(pattern, "scope", "dependency") == "python")


def rerun(step: PlanStep, edit: StepEdit, text: str, lines: frozenset[int] | None) -> str:
    """``text`` after the step's recipe, restricted to ``lines`` (None: anywhere). Raises
    ``ValueError`` when the recipe cannot be re-run or re-basing would be unsafe."""
    from pymolt.codemods.apply import apply_patterns
    from pymolt.codemods.rules import apply_rule_detailed

    recipe = step.do.recipe
    if recipe is None or not recipe.rerunnable:
        raise ValueError("the step's recipe cannot be re-run")
    if recipe.kind == "rule" and recipe.rule is not None:
        full = apply_rule_detailed(text, recipe.rule).new_source
        return _keep_at(text, full, lines)
    if recipe.kind in ("pattern", "move-group") and recipe.patterns:
        patterns = list(recipe.patterns)
        if all(_native(p) for p in patterns):
            restrict = None if lines is None else (lambda _p: lines)
            return apply_patterns(text, patterns, lines_for=restrict, path=edit.file)[0]
        return _keep_at(text, apply_patterns(text, patterns, path=edit.file)[0], lines)
    raise ValueError(f"a {recipe.kind} recipe is not re-run on Python source")


def _is_import(lines: list[str]) -> bool:
    body = [line.strip() for line in lines if line.strip()]
    return bool(body) and all(line.startswith(("import ", "from ")) for line in body)


def _keep_at(text: str, full: str, lines: frozenset[int] | None) -> str:
    """``full``'s changes to ``text`` that meet ``lines``, with the imports it inserts."""
    if lines is None or full == text:
        return full
    a, b = split_lines(text), split_lines(full)
    out: list[str] = []
    pos = 0
    dropped = False
    kept_import_change = False
    for _tag, i1, i2, j1, j2 in opcodes(text, full):
        meets = any(n in lines for n in range(i1 + 1, i2 + 1))
        inserts_import = i1 == i2 and _is_import(b[j1:j2])
        if meets or inserts_import:
            if i2 > i1 and _is_import(a[i1:i2]):
                kept_import_change = True
            out.extend(a[pos:i1])
            out.extend(b[j1:j2])
            pos = i2
        else:
            dropped = True
    out.extend(a[pos:])
    if dropped and kept_import_change:
        raise ValueError("the recipe also changes a site outside the step's statements, and "
                         "the step removes or replaces an import that site may still use")
    return "".join(out)


__all__ = ["rerun"]
