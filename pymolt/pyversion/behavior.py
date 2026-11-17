"""Python behaviour changes found statically: project code whose *output* changes between
minors although every name it uses still exists (``knowledge/stdlib_behavior.json``).

One detector so far, ``typeddict-inherited-annotations-iteration``, for
``typing-typeddict-annotations-order`` (Python 3.9). The ``__annotations__`` of a TypedDict
with a TypedDict base list the class's own fields first on 3.8 and the bases' fields first on
3.9+ (each base ordered the same way, recursively; a re-declared field keeps its first
position). A dict built by iterating them changes key order; its key set does not. The detector

* resolves TypedDict classes per module, through that module's own imports (``typing`` /
  ``typing_extensions``, ``import typing as t``, project modules, relative imports,
  re-exports, ``from m import *``), taking the base Python's live branch of a guarded import
  (``try``/``except ImportError``, ``if sys.version_info ...``; ``if TYPE_CHECKING`` never
  runs). A name a function binds locally shadows the module's;
* finds order-sensitive uses of ``X.__annotations__`` (also ``.keys()``/``.items()``/
  ``.values()``, ``get_type_hints(X)``, and a local name bound to one of them): ``for`` loops
  and list/dict/generator comprehensions over it, ``list``/``tuple``/``dict``/``enumerate``/
  ``zip``/``iter``/``reversed``/``map``/``filter``/``OrderedDict`` of it, ``sep.join(it)``,
  ``[*it]``, ``f(*it)``, ``{**it}`` and tuple unpacking. Membership, ``len``, ``sorted``,
  ``set`` and a generator fed to an order-free consumer are never flagged;
* computes both orders from the class bodies, as 3.8's and 3.9's metaclasses do.

Only the base's ``typing.TypedDict`` reorders: ``typing_extensions.TypedDict`` (3.7.4.2+)
already uses the 3.9 order on 3.8 and ``mypy_extensions.TypedDict`` keeps the 3.8 order on
every Python, so classes built on either are left out. So is a class whose bases or fields
cannot be read statically (a base from a dependency, fields under an ``if``): never a guessed
order.

Plain ``ast`` over the project's own files (the contact map's walk: no virtualenvs, vendored
or build trees, nested checkouts); nothing is imported or run.
"""

from __future__ import annotations

import ast
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pymolt.pyversion.guards import evaluate_guard, parse_version

CAUSE = "typing-typeddict-annotations-order"
DETECTOR = "typeddict-inherited-annotations-iteration"
_CHANGED_IN = (3, 9)

_TD_ROOTS = {"typing.TypedDict": "typing", "typing_extensions.TypedDict": "typing_extensions",
             "mypy_extensions.TypedDict": "mypy_extensions"}
_HINTS = {"typing.get_type_hints", "typing_extensions.get_type_hints"}
_MARKERS = ("__annotations__", "get_type_hints")
_KEY_VIEWS = {"keys", "items", "values"}
# builtins whose result keeps the iteration order: which positional arguments it iterates
_ORDERED_BUILTINS = {"list": (0, 1), "tuple": (0, 1), "dict": (0, 1), "enumerate": (0, 1),
                     "iter": (0, 1), "reversed": (0, 1), "zip": (0, None), "map": (1, None),
                     "filter": (1, 2)}
_ORDERED_EXTERNAL = {"collections.OrderedDict": (0, 1), "itertools.chain": (0, None)}
# a generator fed to these is consumed without its order reaching the result
_ORDER_FREE = {"set", "frozenset", "sorted", "len", "any", "all", "sum", "min", "max"}
_STDLIB = frozenset(getattr(sys, "stdlib_module_names", ())) | {"typing_extensions"}
_SKIP_DIRS = {".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules", "vendor",
              "__pycache__", ".tox", ".nox", ".mypy_cache", ".pytest_cache", ".pymolt",
              ".pymolt_cache", "site-packages", "build", "dist", ".eggs"}


# ── values a name can resolve to ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Mod:
    """A project module."""

    name: str


@dataclass(frozen=True)
class _Ext:
    """Something outside the project, by dotted name (``typing.TypedDict``)."""

    dotted: str


@dataclass(frozen=True, eq=False)
class _Cls:
    """A class statement of a project module."""

    module: str
    node: ast.ClassDef


@dataclass(frozen=True, eq=False)
class _Call:
    """``Name = callee(...)`` at module level: a functional TypedDict when the callee is one."""

    module: str
    node: ast.Call
    name: str


_Value = _Mod | _Ext | _Cls | _Call


@dataclass
class _TD:
    """A TypedDict whose fields and TypedDict bases were read from the source."""

    key: str                     # "<module>.<Class>"
    name: str
    file: str                    # project-relative, posix
    line: int
    own: list[str]
    bases: list[_TD]
    machinery: str               # typing | typing_extensions | mypy_extensions


_UNREADABLE = object()           # a TypedDict (maybe) whose order cannot be read statically


# ── the project: modules, their names, their module-level bindings ─────────────────────────


def _project_files(root: Path) -> list[Path]:
    """The contact map's walk: hidden, virtualenv, vendored and build directories and nested
    git checkouts are not the project's own code."""
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.startswith(".") and not d.endswith(".egg-info")
            and not os.path.exists(os.path.join(dirpath, d, ".git")))
        files.extend(Path(dirpath) / n for n in sorted(filenames) if n.endswith(".py"))
    return files


class _Project:
    def __init__(self, root: Path, base: tuple[int, int]):
        self.root = root
        self.base = base
        self.path: dict[str, Path] = {}          # module id (root-relative dotted) -> file
        self.is_pkg: dict[str, bool] = {}
        self.canonical: dict[str, str] = {}      # module id -> importable name
        self._by_name: dict[str, str | None] = {}   # any spelling -> module id (None: ambiguous)
        self._trees: dict[str, ast.Module | None] = {}
        self._bindings: dict[str, tuple[dict[str, tuple], list[str]]] = {}
        self._tds: dict[int, Any] = {}
        for path in _project_files(root):
            self._add(path)

    def _add(self, path: Path) -> None:
        rel = path.relative_to(self.root)
        parts = list(rel.with_suffix("").parts)
        is_pkg = parts[-1] == "__init__"
        if is_pkg:
            parts.pop()
        if not parts:
            return
        module_id = ".".join(parts)
        importable = all(p.isidentifier() for p in parts)
        # the importable name: from the topmost directory that is not a package (sys.path root)
        dirs = list(rel.parent.parts)
        top = len(dirs)
        while top > 0 and (self.root.joinpath(*dirs[:top]) / "__init__.py").exists():
            top -= 1
        canonical_parts = parts[top:]
        canonical = ".".join(canonical_parts) or module_id
        self.path[module_id] = path
        self.is_pkg[module_id] = is_pkg
        self.canonical[module_id] = canonical
        for name in ({module_id, canonical} if importable else set()):
            known = self._by_name.get(name, module_id)
            self._by_name[name] = module_id if known == module_id else None

    def module(self, dotted: str | None) -> str | None:
        if not dotted:
            return None
        return self._by_name.get(dotted)

    def rel(self, module_id: str) -> str:
        return self.path[module_id].relative_to(self.root).as_posix()

    def tree(self, module_id: str, text: str | None = None) -> ast.Module | None:
        if module_id not in self._trees:
            try:
                source = text if text is not None else self.path[module_id].read_text(
                    encoding="utf-8", errors="replace")
                self._trees[module_id] = ast.parse(source)
            except (OSError, SyntaxError, ValueError):
                self._trees[module_id] = None
        return self._trees[module_id]

    # imports

    def absolute(self, module_id: str, node: ast.ImportFrom) -> list[str]:
        """The absolute module spellings an ``ImportFrom`` may name (a relative import is tried
        against both the root-relative and the importable name of the importing module)."""
        if not node.level:
            return [node.module or ""]
        out = []
        for own in dict.fromkeys([module_id, self.canonical.get(module_id, module_id)]):
            pkg = own.split(".") if self.is_pkg.get(module_id) else own.split(".")[:-1]
            if node.level - 1 > len(pkg):
                continue
            pkg = pkg[:len(pkg) - (node.level - 1)]
            out.append(".".join([*pkg, node.module] if node.module else pkg))
        return out

    def module_value(self, dotted: str) -> _Value:
        found = self.module(dotted) if dotted.split(".")[0] not in _STDLIB else None
        return _Mod(found) if found else _Ext(dotted)

    def from_value(self, sources: list[str], name: str, seen: frozenset) -> _Value | None:
        for source in sources:
            if source.split(".")[0] in _STDLIB or self.module(source) is None:
                continue
            target = self.module(source)
            value = self.value_of(target, name, seen)
            if value is not None:
                return value
            sub = self.module(f"{source}.{name}")
            return _Mod(sub) if sub else None
        source = sources[0] if sources else ""
        return _Ext(f"{source}.{name}" if source else name)

    # module-level bindings, as the base Python runs the module

    def bindings(self, module_id: str) -> tuple[dict[str, tuple], list[str]]:
        if module_id not in self._bindings:
            out: dict[str, tuple] = {}
            stars: list[str] = []
            tree = self.tree(module_id)
            if tree is not None:
                self._collect(module_id, tree.body, out, stars)
            self._bindings[module_id] = (out, stars)
        return self._bindings[module_id]

    def _collect(self, module_id: str, body: list[ast.stmt], out: dict[str, tuple],
                 stars: list[str]) -> None:
        for stmt in body:
            if isinstance(stmt, ast.If):
                live = _branch(stmt.test, self.base)
                if live is True:
                    self._collect(module_id, stmt.body, out, stars)
                elif live is False:
                    self._collect(module_id, stmt.orelse, out, stars)
                else:  # not evaluable: a name either branch binds is ambiguous
                    for branch in (stmt.body, stmt.orelse):
                        scratch: dict[str, tuple] = {}
                        self._collect(module_id, branch, scratch, stars)
                        for name in scratch:
                            out[name] = ("ambiguous",)
            elif isinstance(stmt, ast.Try) or type(stmt).__name__ == "TryStar":
                if self._imports_fail(stmt.body):
                    for handler in stmt.handlers:
                        if _catches_import_error(handler):
                            self._collect(module_id, handler.body, out, stars)
                            break
                else:
                    self._collect(module_id, stmt.body, out, stars)
                    self._collect(module_id, stmt.orelse, out, stars)
                self._collect(module_id, stmt.finalbody, out, stars)
            elif isinstance(stmt, (ast.With, ast.AsyncWith)):
                self._collect(module_id, stmt.body, out, stars)
            elif isinstance(stmt, ast.Import):
                for alias in stmt.names:
                    if alias.asname:
                        out[alias.asname] = ("import", alias.name)
                    else:
                        top = alias.name.split(".")[0]
                        out[top] = ("import", top)
            elif isinstance(stmt, ast.ImportFrom):
                sources = self.absolute(module_id, stmt)
                for alias in stmt.names:
                    if alias.name == "*":
                        stars.extend(sources)
                    elif self._absent_on_base(stmt, alias.name):
                        out[alias.asname or alias.name] = ("absent",)
                    else:
                        out[alias.asname or alias.name] = ("from", tuple(sources), alias.name)
            elif isinstance(stmt, ast.ClassDef):
                out[stmt.name] = ("class", stmt)
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out[stmt.name] = ("other",)
            elif isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                value = getattr(stmt, "value", None)
                single = (len(targets) == 1 and isinstance(targets[0], ast.Name)
                          and not isinstance(stmt, ast.AugAssign) and value is not None)
                for target in targets:
                    for node in ast.walk(target):
                        if isinstance(node, ast.Name):
                            out[node.id] = ("other",)
                if single and isinstance(value, ast.Call):
                    out[targets[0].id] = ("call", value)
                elif single and isinstance(value, (ast.Name, ast.Attribute)):
                    out[targets[0].id] = ("alias", value)

    def _absent_on_base(self, stmt: ast.ImportFrom, name: str) -> bool:
        """``from typing import TypedDict`` on a base that has no ``typing.TypedDict``."""
        if stmt.level or not stmt.module or stmt.module.split(".")[0] not in _STDLIB:
            return False
        return _present(f"{stmt.module}.{name}", self.base) is False

    def _imports_fail(self, body: list[ast.stmt]) -> bool:
        for stmt in body:
            if isinstance(stmt, ast.ImportFrom) and any(
                    self._absent_on_base(stmt, a.name) for a in stmt.names):
                return True
            if isinstance(stmt, ast.Import) and any(
                    a.name.split(".")[0] in _STDLIB and a.name != "typing_extensions"
                    and _present(a.name, self.base) is False for a in stmt.names):
                return True
        return False

    # resolution

    def is_bound(self, module_id: str, name: str) -> bool:
        return name in self.bindings(module_id)[0]

    def value_of(self, module_id: str, name: str, seen: frozenset = frozenset()
                 ) -> _Value | None:
        key = (module_id, name)
        if key in seen:
            return None
        seen = seen | {key}
        table, stars = self.bindings(module_id)
        binding = table.get(name)
        if binding is None:
            for source in stars:
                dotted = f"{source}.{name}"
                if (dotted in _TD_ROOTS or dotted in _HINTS) and _present(
                        dotted, self.base) is not False:
                    return _Ext(dotted)         # ``from typing import *``
                target = self.module(source)
                if target is not None:
                    value = self.value_of(target, name, seen)
                    if value is not None:
                        return value
            return None
        return self.binding_value(module_id, binding, seen)

    def binding_value(self, module_id: str, binding: tuple, seen: frozenset = frozenset()
                      ) -> _Value | None:
        kind = binding[0]
        if kind == "class":
            return _Cls(module_id, binding[1])
        if kind == "import":
            return self.module_value(binding[1])
        if kind == "from":
            return self.from_value(list(binding[1]), binding[2], seen)
        if kind == "call":
            call = binding[1]
            callee = self.expr_value(module_id, call.func, seen=seen)
            if isinstance(callee, _Ext) and callee.dotted in _TD_ROOTS:
                name = call.args[0].value if (call.args and isinstance(call.args[0],
                                                                         ast.Constant)) else "?"
                return _Call(module_id, call, str(name))
            return None
        if kind == "alias":
            return self.expr_value(module_id, binding[1], seen=seen)
        return None

    def expr_value(self, module_id: str, node: ast.AST, lookup=None,
                   seen: frozenset = frozenset()) -> _Value | None:
        if isinstance(node, ast.Name):
            if lookup is not None:
                return lookup(node.id)
            return self.value_of(module_id, node.id, seen)
        if isinstance(node, ast.Attribute):
            head = self.expr_value(module_id, node.value, lookup, seen)
            if isinstance(head, _Ext):
                return _Ext(f"{head.dotted}.{node.attr}")
            if isinstance(head, _Mod):
                value = self.value_of(head.name, node.attr, seen)
                if value is None:
                    sub = self.module(f"{head.name}.{node.attr}")
                    value = _Mod(sub) if sub else None
                return value
        return None

    # TypedDicts

    def typeddict(self, value: _Value | None, seen: frozenset = frozenset()) -> Any:
        """``_TD``, ``_UNREADABLE`` or None (not a TypedDict this detector can see)."""
        if not isinstance(value, (_Cls, _Call)):
            return None
        ident = id(value.node)
        if ident in self._tds:
            return self._tds[ident]
        if ident in seen:
            return _UNREADABLE
        result = (self._class_td(value, seen | {ident}) if isinstance(value, _Cls)
                  else self._functional_td(value))
        self._tds[ident] = result
        return result

    def _functional_td(self, value: _Call) -> Any:
        call = value.node
        callee = self.expr_value(value.module, call.func)
        machinery = _TD_ROOTS[callee.dotted] if isinstance(callee, _Ext) else "typing"
        fields: list[str] = []
        if len(call.args) >= 2:
            spec = call.args[1]
            if not isinstance(spec, ast.Dict) or not all(
                    isinstance(k, ast.Constant) and isinstance(k.value, str) for k in spec.keys):
                return _UNREADABLE
            fields = [k.value for k in spec.keys]
        else:
            fields = [kw.arg for kw in call.keywords if kw.arg and kw.arg != "total"]
            if any(kw.arg is None for kw in call.keywords):
                return _UNREADABLE
        return _TD(key=f"{self.canonical.get(value.module, value.module)}.{value.name}",
                   name=value.name, file=self.rel(value.module), line=call.lineno,
                   own=fields, bases=[], machinery=machinery)

    def _class_td(self, value: _Cls, seen: frozenset) -> Any:
        node = value.node
        roots: list[str] = []
        bases: list[_TD] = []
        other = False
        for expr in node.bases:
            base = self.expr_value(value.module, expr)
            if isinstance(base, _Ext) and base.dotted in _TD_ROOTS:
                roots.append(_TD_ROOTS[base.dotted])
                continue
            td = self.typeddict(base, seen)
            if td is _UNREADABLE:
                return _UNREADABLE
            if isinstance(td, _TD):
                bases.append(td)
            else:
                other = True
        if not roots and not bases:
            return None
        if other or any(kw.arg != "total" for kw in node.keywords):
            return _UNREADABLE              # a non-TypedDict base or a metaclass: not read
        machineries = {*roots, *(b.machinery for b in bases)}
        if len(machineries) != 1:
            return _UNREADABLE
        own: list[str] = []
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign):
                if stmt.simple and isinstance(stmt.target, ast.Name):
                    own.append(stmt.target.id)
            elif any(isinstance(n, ast.AnnAssign) for n in ast.walk(stmt)):
                return _UNREADABLE          # fields under an if/try: not known statically
        return _TD(key=f"{self.canonical.get(value.module, value.module)}.{node.name}",
                   name=node.name, file=self.rel(value.module), line=node.lineno, own=own,
                   bases=bases, machinery=machineries.pop())


def _present(dotted: str, base: tuple[int, int]) -> bool | None:
    try:
        from pymolt.core import stdlib
    except ImportError:
        return None
    try:
        return stdlib.present_in(dotted, f"{base[0]}.{base[1]}")
    except Exception:  # noqa: BLE001 - no data is "unknown"
        return None


def _is_type_checking(test: ast.AST) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")


def _branch(test: ast.AST, base: tuple[int, int]) -> bool | None:
    """Does the ``if`` body run on the base? (``TYPE_CHECKING`` is False at runtime.)"""
    if _is_type_checking(test):
        return False
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) and _is_type_checking(
            test.operand):
        return True
    try:
        return evaluate_guard(ast.unparse(test), base)
    except Exception:  # noqa: BLE001 - not evaluable
        return None


def _catches_import_error(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    names = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(isinstance(n, (ast.Name, ast.Attribute)) and
               (n.id if isinstance(n, ast.Name) else n.attr)
               in ("ImportError", "ModuleNotFoundError", "Exception", "BaseException")
               for n in names)


# ── orders ────────────────────────────────────────────────────────────────────────────────


def _dedupe(keys: list[str]) -> list[str]:
    return list(dict.fromkeys(keys))


def order_38(td: _TD) -> list[str]:
    """3.8's ``_TypedDictMeta``: own annotations, then ``update()`` from each base."""
    return _dedupe([*td.own, *(k for b in td.bases for k in order_38(b))])


def order_39(td: _TD) -> list[str]:
    """3.9+'s ``_TypedDictMeta``: ``update()`` from each base, then the own annotations."""
    return _dedupe([*(k for b in td.bases for k in order_39(b)), *td.own])


# ── order-sensitive uses ──────────────────────────────────────────────────────────────────


@dataclass
class _Scope:
    kind: str                                    # module | class | function | comprehension
    locals: dict[str, tuple] = field(default_factory=dict)  # name -> binding (or ("other",))
    aliases: dict[str, tuple[_Value, str]] = field(default_factory=dict)


@dataclass
class _Site:
    value: _Value
    line: int
    how: str


def _bound_names(body: list[ast.stmt]) -> dict[str, tuple]:
    """Names a function (or class) body binds, without descending into nested scopes; an
    import keeps its binding, anything else shadows (``("other",)``)."""
    out: dict[str, tuple] = {}
    declared: set[str] = set()

    def visit(node: ast.AST) -> None:
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            declared.update(node.names)
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out[node.name] = ("other",)
            for deco in node.decorator_list:
                visit(deco)
            return
        if isinstance(node, (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp,
                             ast.GeneratorExp)):
            return
        if isinstance(node, ast.Import):
            for a in node.names:
                name = a.asname or a.name.split(".")[0]
                out[name] = ("other",) if name in out else (
                    "import", a.name if a.asname else a.name.split(".")[0])
            return
        if isinstance(node, ast.ImportFrom):
            out.update({(a.asname or a.name): ("local-from", node, a.name)
                        for a in node.names if a.name != "*"})
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            out[node.id] = ("other",)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            out[node.name] = ("other",)
        elif isinstance(node, ast.arg):
            out[node.arg] = ("other",)
        for child in ast.iter_child_nodes(node):
            visit(child)

    for stmt in body:
        visit(stmt)
    for name in declared:
        out.pop(name, None)
    return out


def _params(args: ast.arguments) -> dict[str, tuple]:
    every = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
    return {a.arg: ("other",) for a in every if a is not None}


class _SiteFinder(ast.NodeVisitor):
    """Order-sensitive iteration of a TypedDict's annotations in one module."""

    def __init__(self, project: _Project, module_id: str):
        self.project = project
        self.module_id = module_id
        self.scopes: list[_Scope] = [_Scope("module")]
        self.sites: list[_Site] = []
        self._order_free: set[int] = set()

    # name resolution

    def lookup(self, name: str) -> _Value | None:
        innermost = len(self.scopes) - 1
        for i in range(innermost, 0, -1):
            scope = self.scopes[i]
            if scope.kind == "class" and i != innermost:
                continue                # a class body is not visible from nested scopes
            if name in scope.locals:
                binding = scope.locals[name]
                if binding[0] == "local-from":
                    node = binding[1]
                    return self.project.from_value(
                        self.project.absolute(self.module_id, node), binding[2], frozenset())
                return self.project.binding_value(self.module_id, binding)
        return self.project.value_of(self.module_id, name)

    def _shadowed(self, name: str) -> bool:
        innermost = len(self.scopes) - 1
        return any(name in s.locals for i, s in enumerate(self.scopes)
                   if i > 0 and (s.kind != "class" or i == innermost))

    def value(self, node: ast.AST) -> _Value | None:
        return self.project.expr_value(self.module_id, node, self.lookup)

    def _builtin(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name) and not self._shadowed(node.id) \
                and not self.project.is_bound(self.module_id, node.id):
            return node.id
        return None

    # what an expression iterates

    def subject(self, node: ast.AST) -> tuple[_Value, str] | None:
        """``(class value, source text)`` when ``node`` is a TypedDict's annotations in order."""
        form = ""
        inner = node
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in _KEY_VIEWS and not node.args and not node.keywords):
            inner, form = node.func.value, f".{node.func.attr}()"
        if isinstance(inner, ast.Attribute) and inner.attr == "__annotations__":
            value = self.value(inner.value)
            if isinstance(value, (_Cls, _Call)):
                return value, f"{ast.unparse(inner.value)}.__annotations__{form}"
            return None
        if isinstance(inner, ast.Call) and inner.args:
            func = self.value(inner.func)
            if isinstance(func, _Ext) and func.dotted in _HINTS:
                value = self.value(inner.args[0])
                if isinstance(value, (_Cls, _Call)):
                    return value, f"get_type_hints({ast.unparse(inner.args[0])}){form}"
            return None
        if isinstance(inner, ast.Name):
            alias = self.scopes[-1].aliases.get(inner.id)
            if alias is not None:
                return alias[0], f"{inner.id} (= {alias[1]}){form}"
        return None

    def check(self, node: ast.AST | None, how: str) -> None:
        if node is None:
            return
        found = self.subject(node)
        if found is not None:
            value, text = found
            self.sites.append(_Site(value, getattr(node, "lineno", 0), f"{how} {text}"))

    # scopes

    def _visit_function(self, node: ast.AST, args: ast.arguments, body: list) -> None:
        for default in [*args.defaults, *(d for d in args.kw_defaults if d is not None)]:
            self.visit(default)
        for deco in getattr(node, "decorator_list", ()):
            self.visit(deco)
        scope = _Scope("function", {**_params(args), **_bound_names(body)})
        self.scopes.append(scope)
        for stmt in body:
            self.visit(stmt)
        self.scopes.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node, node.args, node.body)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._visit_function(node, node.args, [ast.Expr(node.body)])

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for expr in [*node.decorator_list, *node.bases, *(k.value for k in node.keywords)]:
            self.visit(expr)
        self.scopes.append(_Scope("class", _bound_names(node.body)))
        for stmt in node.body:
            self.visit(stmt)
        self.scopes.pop()

    def _visit_comprehension(self, node: ast.AST, ordered: bool) -> None:
        generators = node.generators
        if ordered:
            for gen in generators:
                self.check(gen.iter, "comprehension over")
        self.visit(generators[0].iter)          # evaluated in the enclosing scope
        targets = {n.id: ("other",) for gen in generators for n in ast.walk(gen.target)
                   if isinstance(n, ast.Name)}
        self.scopes.append(_Scope("comprehension", targets))
        for i, gen in enumerate(generators):
            if i:
                self.visit(gen.iter)
            for cond in gen.ifs:
                self.visit(cond)
        for part in ("elt", "key", "value"):
            if getattr(node, part, None) is not None:
                self.visit(getattr(node, part))
        self.scopes.pop()

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node, True)

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node, True)

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension(node, False)    # a set's order is its hash order

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension(node, id(node) not in self._order_free)

    # order-sensitive positions

    def visit_For(self, node: ast.For) -> None:
        self.check(node.iter, "for loop over")
        self.generic_visit(node)

    visit_AsyncFor = visit_For

    def visit_Call(self, node: ast.Call) -> None:
        name = self._builtin(node.func)
        span = _ORDERED_BUILTINS.get(name) if name else None
        if span is None:
            func = self.value(node.func) if not name else None
            if isinstance(func, _Ext):
                span = _ORDERED_EXTERNAL.get(func.dotted)
        if span is not None:
            start, stop = span
            for arg in node.args[start:stop]:
                self.check(arg, f"{ast.unparse(node.func)}() of")
        elif isinstance(node.func, ast.Attribute) and node.func.attr == "join" \
                and len(node.args) == 1:
            self.check(node.args[0], "join() of")
        if name in _ORDER_FREE:
            self._order_free.update(id(a) for a in node.args if isinstance(a, ast.GeneratorExp))
        for arg in node.args:
            if isinstance(arg, ast.Starred):
                self.check(arg.value, "*-unpacking of")
        if name == "dict":
            for kw in node.keywords:
                if kw.arg is None:
                    self.check(kw.value, "**-unpacking of")
        self.generic_visit(node)

    def visit_List(self, node: ast.List) -> None:
        if isinstance(node.ctx, ast.Load):
            for elt in node.elts:
                if isinstance(elt, ast.Starred):
                    self.check(elt.value, "*-unpacking of")
        self.generic_visit(node)

    visit_Tuple = visit_List

    def visit_Dict(self, node: ast.Dict) -> None:
        for key, val in zip(node.keys, node.values, strict=False):
            if key is None:
                self.check(val, "**-unpacking of")
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        found = self.subject(node.value)
        scope = self.scopes[-1]
        for target in node.targets:
            if isinstance(target, (ast.Tuple, ast.List)):
                self.check(node.value, "unpacking of")
            self.visit(target)
            for n in ast.walk(target):
                if isinstance(n, ast.Name):
                    scope.aliases.pop(n.id, None)
        if found is not None and len(node.targets) == 1 and isinstance(node.targets[0],
                                                                        ast.Name):
            scope.aliases[node.targets[0].id] = found

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        found = self.subject(node.value) if node.value is not None else None
        self.generic_visit(node)
        if isinstance(node.target, ast.Name):
            self.scopes[-1].aliases.pop(node.target.id, None)
            if found is not None:
                self.scopes[-1].aliases[node.target.id] = found


def _sites(project: _Project, module_id: str) -> Iterator[_Site]:
    tree = project.tree(module_id)
    if tree is None:
        return iter(())
    finder = _SiteFinder(project, module_id)
    finder.visit(tree)
    return iter(finder.sites)


# ── public API ────────────────────────────────────────────────────────────────────────────


def _short(keys: list[str], limit: int = 4) -> str:
    return ", ".join(keys[:limit]) + (", ..." if len(keys) > limit else "")


def scan_order_causes(project_root: str | Path, base_python: str | None,
                      target_python: str | None) -> tuple[list[dict[str, Any]], list[str]]:
    """``(causes, notes)``: :func:`predicted_order_causes`, plus a note for every TypedDict the
    project iterates in order whose order was not predicted, with the reason (built on
    ``typing_extensions``, or not readable statically)."""
    base, target = parse_version(base_python), parse_version(target_python)
    if base is None or target is None or not (base < _CHANGED_IN <= target):
        return [], []
    root = Path(project_root).resolve()
    project = _Project(root, base)
    found: dict[tuple[str, int], tuple[_TD, list[dict[str, Any]]]] = {}
    skipped: dict[tuple[str, int], str] = {}
    for module_id in sorted(project.path):
        try:
            text = project.path[module_id].read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not any(marker in text for marker in _MARKERS):
            continue
        project.tree(module_id, text)
        for site in _sites(project, module_id):
            td = project.typeddict(site.value)
            where = f"{project.rel(module_id)}:{site.line}"
            if td is _UNREADABLE:
                node = site.value.node
                skipped.setdefault(
                    (project.rel(site.value.module), node.lineno),
                    f"TypedDict {getattr(node, 'name', None) or site.value.name} "
                    f"({project.rel(site.value.module)}:{node.lineno}) is iterated in order at "
                    f"{where}, but its bases or fields cannot be read statically: its Python "
                    "3.9 annotation order was not predicted")
                continue
            if not isinstance(td, _TD) or not td.bases:
                continue
            if td.machinery != "typing":
                skipped.setdefault(
                    (td.file, td.line),
                    f"TypedDict {td.name} ({td.file}:{td.line}) is iterated in order at {where}; "
                    f"it is built on {td.machinery}.TypedDict, whose order does not change "
                    "with Python 3.9 (typing_extensions 3.7.4.2+ already uses the 3.9 order, "
                    "mypy_extensions keeps the 3.8 one)")
                continue
            entry = found.setdefault((td.file, td.line), (td, []))
            record = {"file": project.rel(module_id), "line": site.line, "how": site.how}
            if record not in entry[1]:
                entry[1].append(record)
    out: list[dict[str, Any]] = []
    for td, sites in found.values():
        old, new = order_38(td), order_39(td)
        if old == new:
            continue
        where = "; ".join(f"{s['file']}:{s['line']} ({s['how']})" for s in sites)
        bases = [b.name for b in td.bases]
        detail = (f"{td.name} ({td.file}:{td.line}) is a TypedDict over {', '.join(bases)}: "
                  f"Python {base[0]}.{base[1]} orders its __annotations__ own fields first "
                  f"({_short(old)}), Python 3.9+ base fields first ({_short(new)}); iterated "
                  f"in order at {where}")
        out.append({"keys": sorted(old), "old": old, "new": new, "cause": CAUSE,
                    "detail": detail, "class": td.key, "file": td.file, "line": td.line,
                    "bases": bases, "sites": sites})
    out.sort(key=lambda c: (c["file"], c["line"]))
    return out, [skipped[k] for k in sorted(skipped)]


def predicted_order_causes(project_root: str | Path, base_python: str | None,
                           target_python: str | None) -> list[dict[str, Any]]:
    """The project's TypedDicts whose annotation order Python 3.9 changes and that the
    project iterates in order, when the move crosses 3.9 (``base < 3.9 <= target``).

    One dict per class: ``keys`` (the field set, sorted), ``old`` / ``new`` (the base's and
    the target's ``__annotations__`` order), ``cause`` (the knowledge id), ``detail`` (the
    class, its ``file:line`` and the iteration sites, in words), plus ``class`` (dotted),
    ``file``, ``line``, ``bases`` and ``sites`` (``[{"file", "line", "how"}]``)."""
    return scan_order_causes(project_root, base_python, target_python)[0]


__all__ = ["CAUSE", "DETECTOR", "order_38", "order_39", "predicted_order_causes",
           "scan_order_causes"]
