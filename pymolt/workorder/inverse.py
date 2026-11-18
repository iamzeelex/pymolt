"""Inverse-effect rollback (design 4.2, phase 2): undo one epoch's change in a file that moved on.

HOST side, pure over texts; one optional callback asks whether other project files import a
name. ``invert(file, before, after, current)`` takes an epoch's before-text B and after-text A
(its blobs) and the file's current text C, which later steps or hand edits changed since, and
returns C with only the epoch's change undone, or why that cannot be done safely:

1. **Hunks.** B → A line by line, with the same difflib alignment the step's hunks and its
   ledger entries come from, and per hunk the statements it removed and added (the pairing of
   ``edits.make_edit``).
2. **Where they are now.** One alignment A → C. Each hunk's lines must be in C unchanged and in
   one piece; for a hunk that only removed lines, the two lines around the gap must still be
   adjacent in C (or at the file's edge). Otherwise a later edit touched the epoch's lines.
3. **Its new statements, on C** (the epoch's ``anchors_new``). Each resolves, at the lines the
   alignment gives, in its slot: its scope is defined once, and its neighbours in C are the ones
   it had in A, or what a later step in effect rewrote them to (``later``: their effects). An
   import at module scope is free in position (design 1.4), so it has no slot.
4. **Imports it wrote.** An import the epoch wrote at module scope (inserted, or the new side of
   an import it rewrote) is removed only when nothing uses it any more: no access in the undone
   text resolves to it alone (LibCST's scope analysis), its names are not in the module's
   ``__all__``, and no other project file imports them from this module
   (``imported_elsewhere``). Otherwise it stays, next to the import it replaced when there was
   one, and ``kept`` names it.
5. **The text.** C, with each hunk's lines from A replaced by its lines from B; a kept import's
   lines stay where they are.
6. **Checks.** The epoch's old statements (its ``anchors_old``) resolve where they were put back,
   in their slots; the result's statements are C's, less the epoch's new ones, plus its old ones
   (by key); LibCST parses it. The caller compiles it on the target (D) before writing.

A file that is not Python (a ``manifest-pin`` step's requirements file) has no statements: its
lines are its keys, and only steps 1, 2 and 5 apply.
"""

from __future__ import annotations

import difflib
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from pymolt.anchors import MODULE, Anchor, Statement, StatementIndex
from pymolt.workorder.edits import _cancel, _met, split_lines
from pymolt.workorder.models import Effect
from pymolt.workorder.rules import Pythons

#: ``imported_elsewhere(file, names) -> names``: the names another project file imports from
#: the module ``file`` is (a re-export to keep).
ImportedElsewhere = Callable[[str, set[str]], set[str]]


@dataclass
class Inverse:
    """The inverse of one epoch in one file: ``text`` (None: refused, with ``reasons``) and the
    imports it wrote that stay because something uses them (``kept``, display text)."""

    file: str
    text: str | None = None
    reasons: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.text is not None


@dataclass
class _Region:
    """One hunk: B lines ``i1:i2`` became A lines ``j1:j2`` (0-based, half-open), now at C lines
    ``k1:k2``; with the statements it removed (on B) and added (on A)."""

    i1: int
    i2: int
    j1: int
    j2: int
    k1: int = 0
    k2: int = 0
    removed: list[Statement] = field(default_factory=list)
    added: list[Statement] = field(default_factory=list)

    def imports_only(self) -> bool:
        """It only removed statements, all of them imports at module scope."""
        return bool(self.removed) and not self.added and all(
            st.is_import and st.scope == MODULE for st in self.removed)


def _short(text: str | None, cap: int = 60) -> str:
    one = " ".join(str(text or "").split())
    return one if len(one) <= cap else one[:cap] + "…"


def _line_map(a: list[str], b: list[str]) -> dict[int, int]:
    """Line ``n`` of ``a`` -> its line in ``b``, for the lines one alignment calls equal."""
    out: dict[int, int] = {}
    for block in difflib.SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks():
        for t in range(block.size):
            out[block.a + t] = block.b + t
    return out


def _place(regions: list[_Region], map_ac: dict[int, int], a_len: int, c_len: int
           ) -> list[str]:
    """Locate every hunk in C (step 2); the reasons when one cannot be."""
    why: list[str] = []
    for r in regions:
        if r.j2 > r.j1:
            k1 = map_ac.get(r.j1)
            if k1 is None or any(map_ac.get(j) != k1 + (j - r.j1) for j in range(r.j1, r.j2)):
                why.append(f"line {r.j1 + 1}: the lines it wrote there were changed since")
                continue
            r.k1, r.k2 = k1, k1 + (r.j2 - r.j1)
            continue
        # It only removed lines: the gap between A's lines j1-1 and j1 must still be one. An
        # import at module scope is free in position: one of the two lines is enough, and the
        # import goes back after the line before it.
        before = map_ac.get(r.j1 - 1) if r.j1 > 0 else -1
        after = map_ac.get(r.j1) if r.j1 < a_len else c_len
        if before is not None and after is not None and after == before + 1:
            r.k1 = r.k2 = after
        elif r.imports_only() and (before is not None or after is not None):
            r.k1 = r.k2 = before + 1 if before is not None else after
        else:
            why.append(f"line {r.j1 + 1}: the lines around what it removed there were changed "
                       "since")
    return why


@dataclass
class _Later:
    """What the later steps in effect did to the file's statements, by key: ``options(key)`` is
    the key and what they rewrote it to; ``inserted`` and ``deleted`` the statements they added
    and removed (passed over when neighbours are compared)."""

    rewrites: dict[str, set[str]]
    inserted: set[str]
    deleted: set[str]

    @classmethod
    def of(cls, later: Iterable[Effect]) -> _Later:
        rewrites: dict[str, set[str]] = defaultdict(set)
        inserted: set[str] = set()
        deleted: set[str] = set()
        for effect in later:
            for old, new in effect.replace:
                rewrites[old].add(new)
            inserted.update(effect.insert, effect.imports_inserted)
            deleted.update(effect.delete)
        return cls(rewrites, inserted, deleted)

    def options(self, key: str) -> set[str]:
        seen, todo = {key}, [key]
        while todo:
            for nxt in self.rewrites.get(todo.pop(), ()):
                if nxt not in seen:
                    seen.add(nxt)
                    todo.append(nxt)
        return seen


def _around(index: StatementIndex, st: Statement, skip_keys: set[str],
            skip_lines: set[int] = frozenset()) -> tuple[str, str]:
    """The keys of the statements before and after ``st`` in its scope (``^``/``$`` at the
    edges), passing over those with a key in ``skip_keys`` or beginning on a line of
    ``skip_lines`` (imports that stay)."""
    members = [m for m in index.in_scope(st.scope) if m.instance == st.instance]
    at = members.index(st)

    def counts(m: Statement) -> bool:
        return m.key not in skip_keys and m.first not in skip_lines
    prev = next((m.key for m in reversed(members[:at]) if counts(m)), "^")
    nxt = next((m.key for m in members[at + 1:] if counts(m)), "$")
    return prev, nxt


def _in_slot(index: StatementIndex, st: Statement, then: StatementIndex, st_then: Statement,
             later: _Later, skip_lines: set[int] = frozenset()) -> str | None:
    """Why ``st`` (in ``index``) is not in the slot ``st_then`` had in ``then`` (None: it is):
    its scope must be defined once, and its neighbours must be the ones it had then, or what
    later steps rewrote them to. The statements later steps inserted or deleted are passed
    over on either side."""
    defined = index.definitions(st.scope)
    if defined != 1:
        state = "missing" if defined == 0 else f"defined {defined} times"
        return f"its scope {st.scope} is {state}"
    want = _around(then, st_then, later.deleted)
    prev, nxt = _around(index, st, later.inserted, skip_lines)
    if prev not in later.options(want[0]) or nxt not in later.options(want[1]):
        return "the statements around it are no longer the ones around it then"
    return None


def _at(index: StatementIndex, anchor: Anchor, lines: list[int | None]):
    """``(statement, None)``: the anchor resolves on ``index`` to the statement at ``lines``
    (0-based); else ``(None, why)``."""
    found = index.resolve(anchor)
    if not found.ok:
        return None, found.status + (f" ({found.reason})" if found.reason else "")
    st = found.statement
    if None in lines or lines != list(range(st.first - 1, st.last)):
        return None, "found elsewhere than the lines it was on"
    return st, None


def _regions(index_b: StatementIndex | None, index_a: StatementIndex | None,
             ops: list[tuple[str, int, int, int, int]]) -> list[_Region]:
    """The hunks with the statements each removed and added, paired as ``make_edit`` does."""
    seen_old: set[int] = set()
    seen_new: set[int] = set()
    out = []
    for _tag, i1, i2, j1, j2 in ops:
        olds = [st for st in _met(index_b, i1, i2) if id(st) not in seen_old]
        news = [st for st in _met(index_a, j1, j2) if id(st) not in seen_new]
        seen_old.update(id(st) for st in olds)
        seen_new.update(id(st) for st in news)
        removed, added = _cancel(olds, news)
        out.append(_Region(i1, i2, j1, j2, removed=removed, added=added))
    return out


def _build(regions: list[_Region], a: list[str], b: list[str], c: list[str],
           keep: set[int]) -> tuple[list[str], list[tuple[str, int]]]:
    """The undone text's lines, and each line's origin (``("C", k)`` or ``("B", i)``). ``keep``:
    the A lines of the imports it wrote that stay; they stay where they are in their hunk, before
    its B lines when they open it, after them otherwise."""
    out: list[str] = []
    origin: list[tuple[str, int]] = []
    pos = 0
    for r in sorted(regions, key=lambda r: r.k1):
        for k in range(pos, r.k1):
            out.append(c[k])
            origin.append(("C", k))
        kept = [j for j in range(r.j1, r.j2) if j in keep]
        lead = []
        for j in range(r.j1, r.j2):
            if j not in keep:
                break
            lead.append(j)
        for j in lead:
            out.append(a[j])
            origin.append(("C", r.k1 + (j - r.j1)))
        for i in range(r.i1, r.i2):
            out.append(b[i])
            origin.append(("B", i))
        for j in kept[len(lead):]:
            out.append(a[j])
            origin.append(("C", r.k1 + (j - r.j1)))
        pos = r.k2
    for k in range(pos, len(c)):
        out.append(c[k])
        origin.append(("C", k))
    return out, origin


def invert(file: str, before: str, after: str, current: str, *, pythons: Pythons = Pythons(),
           later: Iterable[Effect] = (), imported_elsewhere: ImportedElsewhere | None = None,
           code: bool | None = None) -> Inverse:
    """``current`` with only the change ``before`` → ``after`` undone (module docstring).
    ``later``: the effects of the later steps in effect on the file; ``code``: whether the file
    is Python (default: by its suffix)."""
    if current == after:
        return Inverse(file, text=before)
    a, b, c = split_lines(after), split_lines(before), split_lines(current)
    every = difflib.SequenceMatcher(None, b, a, autojunk=False).get_opcodes()
    ops = [op for op in every if op[0] != "equal"]
    if not ops:
        return Inverse(file, text=current)
    code = file.endswith(".py") if code is None else code
    map_ac = _line_map(a, c)
    a_of = {k: j for j, k in map_ac.items()}
    if not code:
        regions = [_Region(i1, i2, j1, j2) for _t, i1, i2, j1, j2 in ops]
        why = _place(regions, map_ac, len(a), len(c))
        if why:
            return Inverse(file, reasons=[f"{file}, {w}" for w in why])
        return Inverse(file, text="".join(_build(regions, a, b, c, set())[0]))

    index_b, index_a, index_c = pythons.index(before), pythons.index(after), \
        pythons.index(current)
    if index_b is None or index_a is None or index_c is None:
        return Inverse(file, reasons=[f"{file}: a text neither parses nor tokenizes, so its "
                                      "statements cannot be found"])
    regions = _regions(index_b, index_a, ops)
    why = _place(regions, map_ac, len(a), len(c))
    if why:
        return Inverse(file, reasons=[f"{file}, {w}" for w in why])
    after_them = _Later.of(later)
    reasons: list[str] = []

    # 3. Its new statements, on C.
    imports: list[Statement] = []
    for r in regions:
        for st in r.added:
            anchor = index_a.anchor(st, file=file)
            found, problem = _at(index_c, anchor, [map_ac.get(n - 1)
                                                   for n in range(st.first, st.last + 1)])
            if found is None:
                reasons.append(f"{file}: `{_short(st.text)}`, which it wrote in {st.scope}, "
                               f"is {problem} now")
                continue
            if st.is_import and st.scope == MODULE:
                imports.append(st)
                continue
            problem = _in_slot(index_c, found, index_a, st, after_them)
            if problem:
                reasons.append(f"{file}: `{_short(st.text)}` is not in its slot: {problem}")
    if reasons:
        return Inverse(file, reasons=reasons)

    # 4. The imports it wrote: which stay.
    keep: set[int] = set()
    kept: list[str] = []
    if imports:
        lines, origin = _build(regions, a, b, c,
                               {j for st in imports for j in range(st.first - 1, st.last)})
        firsts = {st.first - 1 for st in imports}
        where = {a_of[k] + 1: n + 1 for n, (side, k) in enumerate(origin)
                 if side == "C" and a_of.get(k) in firsts}
        used, problem = _used_imports(file, "".join(lines), where, imported_elsewhere)
        if problem:
            return Inverse(file, reasons=[f"{file}: {problem}"])
        for st in imports:
            if st.first in used:
                keep.update(range(st.first - 1, st.last))
                kept.append(_short(st.text))

    # 5. The text.
    lines, origin = _build(regions, a, b, c, keep)
    text = "".join(lines)

    # 6. Checks.
    index_r = pythons.index(text)
    if index_r is None:
        return Inverse(file, reasons=[f"{file}: the text with its change undone neither "
                                      "parses nor tokenizes"])
    b_to_r = _b_to_r(origin, a_of, every)
    skip = {n + 1 for n, (side, k) in enumerate(origin) if side == "C" and a_of.get(k) in keep}
    for r in regions:
        for st in r.removed:
            anchor = index_b.anchor(st, file=file)
            found, problem = _at(index_r, anchor, [b_to_r.get(n - 1)
                                                   for n in range(st.first, st.last + 1)])
            if found is None:
                reasons.append(f"{file}: `{_short(st.text)}`, put back in {st.scope}, is "
                               f"{problem} there")
                continue
            if st.is_import and st.scope == MODULE:
                continue
            problem = _in_slot(index_r, found, index_b, st, after_them, skip)
            if problem:
                reasons.append(f"{file}: `{_short(st.text)}`, put back, is not in its slot: "
                               f"{problem}")
    if reasons:
        return Inverse(file, reasons=reasons)
    want = Counter(st.key for st in index_c.statements)
    want.subtract(st.key for r in regions for st in r.added if st.first - 1 not in keep)
    want.update(st.key for r in regions for st in r.removed)
    if want != Counter(st.key for st in index_r.statements):
        return Inverse(file, reasons=[f"{file}: undoing its change would change other "
                                      "statements than its own"])
    try:
        import libcst

        libcst.parse_module(text)
    except Exception as exc:  # noqa: BLE001 - any parser failure refuses the inverse
        return Inverse(file, reasons=[f"{file}: the text with its change undone does not "
                                      f"parse ({type(exc).__name__})"])
    return Inverse(file, text=text, kept=kept)


def _b_to_r(origin: list[tuple[str, int]], a_of: dict[int, int],
            every: list[tuple[str, int, int, int, int]]) -> dict[int, int]:
    """B's line -> the undone text's line: put back from B, or kept through A and C."""
    b_of: dict[int, int] = {}                       # A line -> B line, where B → A kept it
    for tag, i1, i2, j1, _j2 in every:
        if tag == "equal":
            b_of.update({j1 + t: i1 + t for t in range(i2 - i1)})
    out: dict[int, int] = {}
    for n, (side, k) in enumerate(origin):
        if side == "B":
            out[k] = n
            continue
        j = a_of.get(k)
        if j is not None and j in b_of:
            out[b_of[j]] = n
    return out


def _bound(node) -> set[str]:
    """The names an import statement binds (``import a.b`` binds ``a``)."""
    import libcst as cst
    from libcst.helpers import get_full_name_for_node

    names: set[str] = set()
    if isinstance(node.names, cst.ImportStar):
        return names
    for alias in node.names:
        if alias.asname is not None:
            names.add(get_full_name_for_node(alias.asname.name) or "")
            continue
        full = get_full_name_for_node(alias.name) or ""
        names.add(full.split(".")[0] if isinstance(node, cst.Import) else full)
    return names - {""}


def _all_names(module) -> set[str] | None:
    """The strings the module puts in ``__all__`` at module level; None when it builds it in a
    way that cannot be read (so any name may be in it)."""
    import ast

    import libcst as cst
    from libcst import matchers as m

    found: set[str] = set()
    for stmt in module.body:
        if not isinstance(stmt, cst.SimpleStatementLine):
            continue
        for small in stmt.body:
            value = None
            if isinstance(small, cst.Assign) and any(
                    m.matches(t.target, m.Name("__all__")) for t in small.targets):
                value = small.value
            elif isinstance(small, cst.AugAssign | cst.AnnAssign) and m.matches(
                    small.target, m.Name("__all__")):
                value = small.value
            elif isinstance(small, cst.Expr) and m.matches(small.value, m.Call(
                    func=m.Attribute(value=m.Name("__all__")))):
                value = small.value.args[0].value if small.value.args else None
                if value is not None and m.matches(small.value.func.attr, m.Name("append")):
                    value = cst.List([cst.Element(value)])
            else:
                continue
            if value is None:
                continue
            try:
                items = ast.literal_eval(module.code_for_node(value).strip())
            except (ValueError, SyntaxError):
                return None
            if isinstance(items, str):
                items = [items]
            if not isinstance(items, list | tuple) or not all(isinstance(x, str) for x in items):
                return None
            found.update(items)
    return found


def _used_imports(file: str, text: str, lines: dict[int, int],
                  imported_elsewhere: ImportedElsewhere | None) -> tuple[set[int], str | None]:
    """Of the imports it wrote (their first line in A -> in ``text``, which keeps them all),
    the ones something still uses, by their first line in A; or why that cannot be told."""
    import libcst as cst
    from libcst.metadata import (
        BuiltinAssignment,
        ImportAssignment,
        MetadataWrapper,
        PositionProvider,
        ScopeProvider,
    )

    try:
        wrapper = MetadataWrapper(cst.parse_module(text))
        positions = wrapper.resolve(PositionProvider)
        scopes = wrapper.resolve(ScopeProvider)
    except Exception as exc:  # noqa: BLE001
        return set(), (f"whether the imports it wrote are still used cannot be told: the "
                       f"text does not parse ({type(exc).__name__})")
    by_line = {line: a_line for a_line, line in lines.items()}
    targets: dict[int, int] = {}                     # id(import node) -> its first line in A
    nodes = {}
    for node, pos in positions.items():
        if isinstance(node, cst.Import | cst.ImportFrom) and pos.start.line in by_line:
            if isinstance(node, cst.ImportFrom) and isinstance(node.names, cst.ImportStar):
                return set(), ("it wrote a `*` import, and what that import provides cannot "
                               "be told")
            targets[id(node)] = by_line[pos.start.line]
            nodes[id(node)] = node
    used: set[int] = set()
    for scope in {s for s in scopes.values() if s is not None}:
        for access in scope.accesses:
            refs = [r for r in access.referents if not isinstance(r, BuiltinAssignment)]
            mine = [r for r in refs if id(getattr(r, "node", None)) in targets]
            if not mine:
                continue
            others = [r for r in refs if id(getattr(r, "node", None)) not in targets
                      and isinstance(r, ImportAssignment)]
            if not others:
                used.update(targets[id(r.node)] for r in mine)
    exported = _all_names(wrapper.module)
    for key, node in nodes.items():
        names = _bound(node)
        if exported is None or names & exported:
            used.add(targets[key])
        elif imported_elsewhere is not None and imported_elsewhere(file, names):
            used.add(targets[key])
    return used, None


__all__ = ["ImportedElsewhere", "Inverse", "invert"]
