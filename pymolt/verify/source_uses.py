"""What the project's own source does with the dependency objects it holds (HOST side).

verify compares a dependency object returned to the project by what the project can observe of
it (``facts``' handle projection). This module reads that from a capture's source snapshot
(``{project-relative path: text}``, the ``.sources.json`` a capture keeps), with the stdlib
``ast`` only, so it works on every capture that kept its source:

- ``read``: attribute names the project reads by name, in a non-call position (``x.name``, and
  ``getattr``/``hasattr`` with a literal name). A method name in a call (``x.close()``) is not
  read: the call itself is a recorded fact of its own. ``keys``: for an attribute the project
  only indexes with literal string keys (``app.config["SEED"]``, ``x.headers.get("ETag")``,
  ``"k" in x.meta``) or writes into (``x.config.update(...)``), the keys it reads; every other
  use of the attribute (passed on, iterated, indexed by a computed key) reads it whole
  (``whole``). An attribute of a module (``st.query_params`` after ``import streamlit as st``)
  is read off the module, not off an object the project holds: not counted.
- ``probed``: names whose *presence* the project tests (``hasattr(x, "n")``,
  ``getattr(x, "n", default)``).
- ``introspected``: class names the project checks an object against (``isinstance``,
  ``issubclass``, ``except``, ``match``, ``type(x) is C`` / ``x.__class__ == C``,
  ``singledispatch`` registrations); ``type_names_read``: the project reads a type's name
  (``type(x).__name__``, ``x.__class__.__qualname__``), so any class may be observed.
- ``dynamic``: the places the project reads attributes by a computed name (``getattr(x, var)``,
  ``vars(x)``, ``x.__dict__``, ``asdict(x)``, ``dir(x)``): what they read cannot be projected, so
  the verdict lists them.

Over-approximation is the safe direction: a name read anywhere in the project counts as read on
every object, so the projection compares more, never less.
"""

from __future__ import annotations

import ast
import warnings
from collections.abc import Mapping
from dataclasses import dataclass, field

_DYNAMIC_CALLS = {"vars": 1, "dir": 1, "asdict": 1, "astuple": 1}
_KEY_READS = ("get", "pop", "setdefault", "__getitem__", "__contains__")
_WRITES = ("update", "clear", "__setitem__", "__delitem__")


@dataclass
class SourceUses:
    read: set[str] = field(default_factory=set)
    whole: set[str] = field(default_factory=set)
    keys: dict[str, set[str]] = field(default_factory=dict)
    probed: set[str] = field(default_factory=set)
    introspected: set[str] = field(default_factory=set)
    type_names_read: bool = False
    dynamic: list[tuple[str, int, str]] = field(default_factory=list)
    files: int = 0          # files parsed
    unparsed: int = 0       # files that did not parse (their uses are unknown)


def _leaf(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _class_leaves(node: ast.AST | None) -> set[str]:
    """The class names an ``isinstance`` second argument / ``except`` clause names."""
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        out: set[str] = set()
        for e in node.elts:
            out |= _class_leaves(e)
        return out
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):   # ``A | B``
        return _class_leaves(node.left) | _class_leaves(node.right)
    leaf = _leaf(node)
    return {leaf} if leaf else set()


def _is_type_of(node: ast.AST) -> bool:
    """``type(x)`` (one argument) or ``x.__class__``."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
            and node.func.id == "type" and len(node.args) == 1:
        return True
    return isinstance(node, ast.Attribute) and node.attr == "__class__"


def _literal_name(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value:
        return node.value
    return None


class _Visitor(ast.NodeVisitor):
    def __init__(self, uses: SourceUses, file: str, lines: list[str],
                 parents: dict[int, ast.AST], modules: set[str]) -> None:
        self.uses = uses
        self.file = file
        self.lines = lines
        self.parents = parents
        self.modules = modules          # names an ``import`` statement binds to a module
        self.called: set[int] = set()   # ids of Attribute nodes that are a call's func

    def _attribute_use(self, node: ast.Attribute) -> tuple[str, str | None]:
        """How a read attribute's value is used: ``("key", k)`` (indexed by a literal string
        key), ``("write", None)`` (written into) or ``("whole", None)`` (anything else)."""
        parent = self.parents.get(id(node))
        if isinstance(parent, ast.Subscript) and parent.value is node:
            if not isinstance(parent.ctx, ast.Load):
                return ("write", None)
            key = _literal_name(parent.slice)
            return ("key", key) if key is not None else ("whole", None)
        if isinstance(parent, ast.Attribute) and parent.value is node:
            call = self.parents.get(id(parent))
            if isinstance(call, ast.Call) and call.func is parent:
                if parent.attr in _WRITES:
                    return ("write", None)
                if parent.attr in _KEY_READS and call.args:
                    key = _literal_name(call.args[0])
                    if key is not None:
                        return ("key", key)
            return ("whole", None)
        if isinstance(parent, ast.Compare) and node in parent.comparators:
            i = parent.comparators.index(node)
            left = parent.left if i == 0 else parent.comparators[i - 1]
            if isinstance(parent.ops[i], ast.In | ast.NotIn):
                key = _literal_name(left)
                if key is not None:
                    return ("key", key)
        return ("whole", None)

    def _dynamic(self, node: ast.AST, what: str) -> None:
        line = getattr(node, "lineno", 0)
        self.uses.dynamic.append((self.file, line, what))

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute):
            self.called.add(id(node.func))
        name = _leaf(node.func)
        if name in ("getattr", "hasattr") and len(node.args) >= 2:
            lit = _literal_name(node.args[1])
            if lit is None:
                if name == "getattr":
                    self._dynamic(node, "getattr(x, <computed name>)")
            else:
                self.uses.read.add(lit)
                self.uses.whole.add(lit)
                if name == "hasattr" or len(node.args) >= 3:
                    self.uses.probed.add(lit)
        elif name in ("isinstance", "issubclass") and len(node.args) >= 2:
            self.uses.introspected |= _class_leaves(node.args[1])
        elif name == "attrgetter":
            for a in node.args:
                lit = _literal_name(a)
                if lit:
                    self.uses.read.update(lit.split("."))
                    self.uses.whole.update(lit.split("."))
        elif name in _DYNAMIC_CALLS and len(node.args) == _DYNAMIC_CALLS[name]:
            self._dynamic(node, f"{name}(x)")
        elif name == "register" and node.args:              # singledispatch ``f.register(C)``
            self.uses.introspected |= _class_leaves(node.args[0])
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        module_attr = isinstance(node.value, ast.Name) and node.value.id in self.modules
        if isinstance(node.ctx, ast.Load) and id(node) not in self.called and not module_attr:
            if node.attr == "__dict__":
                self._dynamic(node, "x.__dict__")
            self.uses.read.add(node.attr)
            use, key = self._attribute_use(node)
            if use == "key" and key is not None:
                self.uses.keys.setdefault(node.attr, set()).add(key)
            elif use == "whole":
                self.uses.whole.add(node.attr)
            if node.attr in ("__name__", "__qualname__", "__module__") and _is_type_of(node.value):
                self.uses.type_names_read = True
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        sides = [node.left, *node.comparators]
        if any(_is_type_of(s) for s in sides):
            for s in sides:
                if not _is_type_of(s):
                    self.uses.introspected |= _class_leaves(s)
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self.uses.introspected |= _class_leaves(node.type)
        self.generic_visit(node)

    def visit_MatchClass(self, node: ast.MatchClass) -> None:
        leaf = _leaf(node.cls)
        if leaf:
            self.uses.introspected.add(leaf)
        self.generic_visit(node)

    def _register_annotation(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for d in node.decorator_list:
            if isinstance(d, ast.Attribute) and d.attr == "register":
                args = [*node.args.posonlyargs, *node.args.args]
                if args and args[0].annotation is not None:
                    self.uses.introspected |= _class_leaves(args[0].annotation)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._register_annotation(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._register_annotation(node)
        self.generic_visit(node)


def parse(text: str) -> ast.AST | None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            return ast.parse(text)
        except (SyntaxError, ValueError):
            return None


def source_uses(sources: Mapping[str, str] | None) -> SourceUses | None:
    """The uses of one capture's source snapshot; None without a snapshot."""
    if not sources:
        return None
    uses = SourceUses()
    for file in sorted(sources):
        if not file.endswith(".py"):
            continue
        text = sources[file]
        tree = parse(text)
        if tree is None:
            uses.unparsed += 1
            continue
        uses.files += 1
        parents = {id(c): n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
        modules = {(a.asname or a.name).split(".")[0] for n in ast.walk(tree)
                   if isinstance(n, ast.Import) for a in n.names}
        v = _Visitor(uses, file, text.splitlines(), parents, modules)
        # ``called`` must be known before an Attribute is visited: a Call is visited before
        # its func (generic_visit walks fields in order), so one pass suffices.
        v.visit(tree)
    uses.dynamic.sort()
    return uses


def project_modules(*sources: Mapping[str, str] | None) -> frozenset[str]:
    """Top-level module names the project's own files define (``app/views.py`` -> ``app``,
    ``tests/test_x.py`` -> ``tests`` and ``test_x``: pytest may import a test module by its
    bare name), plus ``__main__``."""
    out = {"__main__"}
    for src in sources:
        rels = {r.replace("\\", "/") for r in src or () if r.endswith(".py")}
        for rel in rels:
            parts = rel[:-3].split("/")
            package = "/".join(parts[:-1])
            if parts[-1] == "__init__":
                parts = parts[:-1]
            if not parts:
                continue
            out.add(parts[0])
            if f"{package}/__init__.py" not in rels:   # not a package: imported by its name
                out.add(parts[-1])
    return frozenset(out)
