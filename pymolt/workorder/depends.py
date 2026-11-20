"""``depends_on``: which earlier steps a pymolt step needs, from effect overlap (design 1.5.3).

HOST side, pure over the steps' edits. For each file and each pair of steps i < j that both
change it, j depends on i when one of these holds:

1. **statement overlap**: a statement j rewrites or removes is one i produced (same scope and
   key): j rewrites what i wrote;
2. **a shared inserted import**: i and j insert the same import, or j's new statements use a
   name that only an import i inserted provides in j's before-text;
3. **the fallback**, "same file, same scope": when a side's effect cannot be keyed (token keys,
   or no statements at all), or when more than ``CROWDED`` steps change one scope. Steps then
   depend on the earlier ones that change a scope they change, never on every earlier step.

Only hard dependencies: the union over files. Manual steps get none (verify judges them on the
final code), and nothing depends on an env step.
"""

from __future__ import annotations

import ast
import io
import tokenize
from collections import Counter, defaultdict
from collections.abc import Callable

from pymolt.anchors import MODULE
from pymolt.workorder.models import PlanStep, StepEdit

#: More steps than this changing one scope of one file: they depend on each other in order.
CROWDED = 8
_ANY = "*"          # an edit with no anchors: its scope is unknown, so it meets every scope


def edit_scopes(edit: StepEdit) -> set[str]:
    scopes = {a.scope for a in (*edit.anchors_old, *edit.anchors_new)}
    if edit.effect.imports_inserted:
        scopes.add(MODULE)
    return scopes or {_ANY}


def _meet(a: set[str], b: set[str]) -> bool:
    return bool(a & b) or _ANY in a or _ANY in b


def _keyed(edit: StepEdit) -> bool:
    return edit.effect.key_kind == "ast" and bool(edit.anchors_old or edit.anchors_new)


def _produced(edit: StepEdit) -> set[tuple[str, str]]:
    keys = edit.effect.new_keys()
    return {(a.scope, a.key) for a in edit.anchors_new if a.key in keys}


def _consumed(edit: StepEdit) -> set[tuple[str, str]]:
    keys = edit.effect.old_keys()
    return {(a.scope, a.key) for a in edit.anchors_old if a.key in keys}


# ── names ─────────────────────────────────────────────────────────────────────────────────


def _parse(text: str) -> ast.AST | None:
    for candidate in (text, text.rstrip() + "\n    pass\n"):
        try:
            return ast.parse(candidate)
        except (SyntaxError, ValueError):
            continue
    return None


def provided_names(import_text: str) -> set[str]:
    """What an import statement binds: ``import a.b`` → ``a.b`` (and ``a``), ``import a.b as c``
    → ``c``, ``from m import x as y`` → ``y``."""
    tree = _parse(import_text)
    out: set[str] = set()
    for node in ast.walk(tree) if tree is not None else ():
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    out.add(alias.asname)
                else:
                    parts = alias.name.split(".")
                    out.update(".".join(parts[:i]) for i in range(1, len(parts) + 1))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name != "*":
                    out.add(alias.asname or alias.name)
    return out


def used_names(text: str) -> set[str]:
    """The dotted names a statement reads (``collections.abc.Iterable`` and its prefixes)."""
    tree = _parse(text)
    out: set[str] = set()
    if tree is not None:
        for node in ast.walk(tree):
            chain: list[str] = []
            cur = node
            while isinstance(cur, ast.Attribute):
                chain.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name) and (chain or isinstance(node, ast.Name)):
                parts = [cur.id, *reversed(chain)]
                out.update(".".join(parts[:i]) for i in range(1, len(parts) + 1))
        return out
    try:                                  # no grammar parses it: its NAME tokens
        toks = tokenize.generate_tokens(io.StringIO(text).readline)
        return {t.string for t in toks if t.type == tokenize.NAME}
    except (tokenize.TokenError, SyntaxError):
        return set()


def _module_imports(before: str | None) -> list[tuple[str, set[str]]]:
    """``(key-less text, names it provides)`` for each module-level import of a text."""
    if before is None:
        return []
    tree = _parse(before)
    if tree is None:
        return []
    out = []
    stack = list(getattr(tree, "body", ()))
    while stack:                          # module level, through if/try blocks, not defs
        node = stack.pop(0)
        if isinstance(node, ast.Import | ast.ImportFrom):
            out.append((ast.unparse(node), provided_names(ast.unparse(node))))
        elif not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            for part in ("body", "orelse", "handlers", "finalbody"):
                stack.extend(getattr(node, part, None) or ())
    return out


def _needs_import_of(ei: StepEdit, ej: StepEdit, before_j: str | None) -> bool:
    """Rule 2: a shared inserted import, or a name in j's new statements that only an import i
    inserted provides in j's before-text."""
    if set(ei.effect.imports_inserted) & set(ej.effect.imports_inserted):
        return True
    inserted = [a for a in ei.anchors_new if a.key in set(ei.effect.imports_inserted)]
    if not inserted:
        return False
    provided: set[str] = set()
    inserted_texts = set()
    for anchor in inserted:
        provided |= provided_names(anchor.stmt)
        tree = _parse(anchor.stmt)
        if tree is not None:
            inserted_texts.update(ast.unparse(n) for n in getattr(tree, "body", ()))
    new_keys = {new for _old, new in ej.effect.replace} | set(ej.effect.insert)
    used: set[str] = set()
    for anchor in ej.anchors_new:
        if anchor.key in new_keys:
            used |= used_names(anchor.stmt)
    wanted = provided & used
    if not wanted:
        return False
    others: set[str] = set()
    for text, names in _module_imports(before_j):
        if text not in inserted_texts:
            others |= names
    return bool(wanted - others)


# ── the assignment ────────────────────────────────────────────────────────────────────────


def depends(ei: StepEdit, ej: StepEdit, *, before_j: str | None = None,
            crowded: set[str] = frozenset()) -> str | None:
    """Why j (``ej``) depends on i (``ei``) in one file, or None. ``before_j``: j's before-text
    (for rule 2's "only i provides"); ``crowded``: scopes more than ``CROWDED`` steps change."""
    si, sj = edit_scopes(ei), edit_scopes(ej)
    if not (_keyed(ei) and _keyed(ej)):
        return "same scope (an effect has no statement keys)" if _meet(si, sj) else None
    if si & sj & crowded:
        return f"same scope ({sorted(si & sj & crowded)[0]} is changed by more than " \
               f"{CROWDED} steps)"
    if _consumed(ej) & _produced(ei):
        return "rewrites a statement it produced"
    if _needs_import_of(ei, ej, before_j):
        return "needs an import it inserted"
    return None


def assign_depends_on(steps: list[PlanStep],
                      before_text: Callable[[str, str], str | None] | None = None
                      ) -> dict[tuple[str, str], str]:
    """Set every pymolt step's ``depends_on`` (in plan order) and return why, per
    ``(step id, dependency id)``. ``before_text(step_id, file)``: the step's before-text in
    that file, when known."""
    by_file: dict[str, list[tuple[PlanStep, StepEdit]]] = defaultdict(list)
    for step in steps:
        if step.executor != "pymolt" or step.phase == "env":
            continue
        for edit in step.edits:
            by_file[edit.file].append((step, edit))
    deps: dict[str, list[str]] = defaultdict(list)
    why: dict[tuple[str, str], str] = {}
    for file, items in by_file.items():
        counts = Counter(scope for _s, e in items for scope in edit_scopes(e) if scope != _ANY)
        crowded = {scope for scope, n in counts.items() if n > CROWDED}
        for j, (sj, ej) in enumerate(items):
            before = before_text(sj.id, file) if before_text else None
            for si, ei in items[:j]:
                if si.id in deps[sj.id] or si.id == sj.id:
                    continue
                reason = depends(ei, ej, before_j=before, crowded=crowded)
                if reason:
                    deps[sj.id].append(si.id)
                    why[(sj.id, si.id)] = f"{file}: {reason}"
    order = {s.id: n for n, s in enumerate(steps)}
    for step in steps:
        step.depends_on = sorted(dict.fromkeys(deps.get(step.id, ())), key=order.get)
    return why