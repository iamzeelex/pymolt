"""Static contact map — the cheap 'where does our code touch third-party code' pass.

Built on LibCST (no PyCG): per file, LibCST's QualifiedNameProvider resolves
imported API references to fully-qualified names. Calls are only one boundary:
decorators, bases, context managers, constants, attributes, and statically
resolvable protocol operations can all break without making a direct call in
application code. We retain those contacts while excluding stdlib and
first-party imports.

Attribution is function-level (caller qualname → dependency qualname); exact
call-site lines are the dynamic trace's job (``where`` in the contract).

Honest scope: this resolves direct, syntactically-visible imported references.
Like any pure-static pass it does not infer the runtime type of local variables,
follow dynamic dispatch, monkeypatching, or ``getattr``.
"""

import hashlib
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Literal

import libcst as cst
from libcst.metadata import (
    MetadataWrapper,
    ParentNodeProvider,
    PositionProvider,
    QualifiedNameProvider,
    QualifiedNameSource,
)
from pydantic import BaseModel, Field

from pymolt.core import stdlib as stdlib_data
from pymolt.core.legacy_stdlib import describe, is_py2_stdlib

logger = logging.getLogger(__name__)

_IGNORE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "vendor",
    "__pycache__",
    ".tox",
    ".nox",
    ".mypy_cache",
    ".pytest_cache",
    ".pymolt",
    ".pymolt_cache",
    "site-packages",
    "build",
    "dist",
    ".eggs",
}

# pymolt's own injected tooling — if the watcher bundle (capture --mode attach) was written
# into the project, its `pymolt_trace` package is *not* a real dependency.
_IGNORE_DEPS = {"pymolt", "pymolt_trace"}

# Bump when the per-file records (or the way they are derived) change: it is part of the
# per-file cache key, so a bump retires every cached file.
CONTACT_MAP_VERSION = "2"

ContactKind = Literal[
    "call",
    "attribute-read",
    "decorator",
    "class-base",
    "context-manager",
    "imported-constant",
    "protocol",
]


# Behavioural contacts run dependency code at our call site, so a trace can
# witness them. Existence contacts are proven by an import that resolves; they
# are not trace evidence and do not belong in the trust denominator.
BEHAVIOURAL_KINDS: frozenset[str] = frozenset({"call", "decorator", "context-manager", "protocol"})
EXISTENCE_KINDS: frozenset[str] = frozenset({"attribute-read", "class-base", "imported-constant"})


def contact_class(kind: str) -> Literal["behavioural", "existence"]:
    """``behavioural`` (trace evidence possible) or ``existence`` (import proves it)."""
    return "behavioural" if kind in BEHAVIOURAL_KINDS else "existence"


class Contact(BaseModel):
    caller: str  # our qualname, e.g. "myapp.views.handler"
    target: str  # dependency qualname, e.g. "flask.cli.with_appcontext"
    dep: str  # top-level dependency, e.g. "flask"
    file: str | None = None  # our file the caller is defined in (best-effort)
    # Default preserves compatibility with contact maps serialized before kinds
    # were introduced: every old contact represented a direct call.
    kind: ContactKind = "call"
    line: int | None = None  # where in ``file`` this contact is (first one when repeated)


class StdlibSite(BaseModel):
    """One place project code names a standard-library module or name (raw facts only)."""

    # resolved through the file's imports: "collections.Iterable", "imp.reload", "imp"
    spelling: str
    module: str  # stdlib top-level: "collections"
    file: str
    line: int
    kind: Literal["import", "from", "attr", "call", "class-base"]
    reached_at: Literal["import", "call"]  # runs on import (module/class body) or when called
    # {"kind": "fallback" | "guarded-fallback" | "broad" | "version", "detail": str}. A syntactic
    # fact; whether it protects on a given target is decided at rating time, not here.
    guard: dict | None = None


class ContactMap(BaseModel):
    root: str
    files_scanned: int = 0
    contacts: list[Contact] = Field(default_factory=list)
    stdlib_sites: list[StdlibSite] = Field(default_factory=list)
    # Cython sources (.pyx/.pxd/.pxi) exist but are not parsed: sites in them are invisible.
    unparsed_cython: list[str] = Field(default_factory=list)
    by_dep: dict[str, list[str]] = Field(default_factory=dict)  # dep -> sorted targets
    notes: list[str] = Field(default_factory=list)


def prune_walk_dirs(dirpath: str, dirnames: list[str], ignore: set[str] | None = None) -> list[str]:
    """Subdirectories of ``dirpath`` worth descending into.

    Skips hidden dirs, ``ignore`` names (default ``_IGNORE_DIRS``), ``*.egg-info`` and any
    NESTED git checkout (a child holding ``.git``, e.g. ``.claude/worktrees/*``). The walk
    root itself is never pruned because only children are filtered.
    """
    skip = _IGNORE_DIRS if ignore is None else ignore
    return [
        d for d in dirnames
        if d not in skip and not d.startswith(".") and not d.endswith(".egg-info")
        and not os.path.exists(os.path.join(dirpath, d, ".git"))
    ]


_CYTHON_SUFFIXES = (".pyx", ".pxd", ".pxi")


def _source_files(root: Path) -> tuple[list[Path], list[Path]]:
    """``(python files, cython files)`` under ``root``."""
    files: list[Path] = []
    cython: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = prune_walk_dirs(dirpath, dirnames)
        for name in filenames:
            if name.endswith(".py"):
                files.append(Path(dirpath) / name)
            elif name.endswith(_CYTHON_SUFFIXES):
                cython.append(Path(dirpath) / name)
    return files, cython


def _python_files(root: Path) -> list[Path]:
    return _source_files(root)[0]


def _file_for_qualname(qualname: str, file_by_module: dict[str, str]) -> str | None:
    """The file of the longest module prefix of a qualname (handles Class.method)."""
    parts = qualname.split(".")
    for i in range(len(parts), 0, -1):
        candidate = ".".join(parts[:i])
        if candidate in file_by_module:
            return file_by_module[candidate]
    return None


def _module_name(file: Path, root: Path) -> str:
    rel = file.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


_BINARY_PROTOCOLS: dict[type[cst.BaseBinaryOp], tuple[str, str]] = {
    cst.Add: ("__add__", "__radd__"),
    cst.Subtract: ("__sub__", "__rsub__"),
    cst.Multiply: ("__mul__", "__rmul__"),
    cst.Divide: ("__truediv__", "__rtruediv__"),
    cst.FloorDivide: ("__floordiv__", "__rfloordiv__"),
    cst.Modulo: ("__mod__", "__rmod__"),
    cst.Power: ("__pow__", "__rpow__"),
    cst.LeftShift: ("__lshift__", "__rlshift__"),
    cst.RightShift: ("__rshift__", "__rrshift__"),
    cst.BitOr: ("__or__", "__ror__"),
    cst.BitAnd: ("__and__", "__rand__"),
    cst.BitXor: ("__xor__", "__rxor__"),
    cst.MatrixMultiply: ("__matmul__", "__rmatmul__"),
}

_COMPARISON_PROTOCOLS: dict[type[cst.BaseCompOp], tuple[str, str] | None] = {
    cst.LessThan: ("__lt__", "__gt__"),
    cst.LessThanEqual: ("__le__", "__ge__"),
    cst.GreaterThan: ("__gt__", "__lt__"),
    cst.GreaterThanEqual: ("__ge__", "__le__"),
    cst.Equal: ("__eq__", "__eq__"),
    cst.NotEqual: ("__ne__", "__ne__"),
    cst.In: None,
    cst.NotIn: None,
    cst.Is: None,
    cst.IsNot: None,
}

_UNARY_PROTOCOLS: dict[type[cst.BaseUnaryOp], str] = {
    cst.Minus: "__neg__",
    cst.Plus: "__pos__",
    cst.BitInvert: "__invert__",
    cst.Not: "__bool__",
}

_BUILTIN_PROTOCOLS = {
    "bool": "__bool__",
    "bytes": "__bytes__",
    "float": "__float__",
    "hash": "__hash__",
    "int": "__int__",
    "iter": "__iter__",
    "len": "__len__",
    "next": "__next__",
    "repr": "__repr__",
    "reversed": "__reversed__",
    "str": "__str__",
}


class _ContactVisitor(cst.CSTVisitor):
    """Collect our-code → dependency contacts in one file via LibCST.

    Tracks the enclosing def/class nesting to attribute each call to its caller
    qualname, and uses QualifiedNameProvider to resolve each imported target.
    """

    METADATA_DEPENDENCIES = (QualifiedNameProvider, ParentNodeProvider, PositionProvider)

    def __init__(self, module_name: str) -> None:
        self.module_name = module_name
        self._scope: list[str] = []
        self._fn_depth = 0
        # Raw records, one per reference: contact facts (``ckind``) and stdlib-site facts
        # (``skind``) side by side. Whether a target is stdlib or a dependency is decided when
        # the records are folded into a map, so a cached file never goes stale on the data.
        self.records: list[dict[str, Any]] = []
        # Roots consumed by a protocol operation. Their child Name/Attribute
        # nodes must not also be reported as plain reads.
        self._consumed: set[cst.CSTNode] = set()

    def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
        self._scope.append(node.name.value)
        self._fn_depth += 1

    def leave_FunctionDef(self, node: cst.FunctionDef) -> None:
        self._scope.pop()
        self._fn_depth -= 1

    def visit_Lambda(self, node: cst.Lambda) -> None:
        self._fn_depth += 1

    def leave_Lambda(self, node: cst.Lambda) -> None:
        self._fn_depth -= 1

    def visit_ClassDef(self, node: cst.ClassDef) -> None:
        self._scope.append(node.name.value)

    def leave_ClassDef(self, node: cst.ClassDef) -> None:
        self._scope.pop()

    def _caller(self) -> str:
        return ".".join([self.module_name, *self._scope]) if self._scope else self.module_name

    def _imported_names(self, node: cst.CSTNode) -> list[str]:
        return sorted(
            {
                q.name
                for q in self.get_metadata(QualifiedNameProvider, node, set())
                if q.source == QualifiedNameSource.IMPORT and not q.name.startswith(".")
            }
        )

    def _structural_kind(self, node: cst.CSTNode) -> ContactKind | None:
        """Return a definition/runtime context that is more precise than a read."""
        current = node
        while True:
            parent = self.get_metadata(ParentNodeProvider, current, None)
            if parent is None:
                return None
            if isinstance(parent, cst.Decorator):
                return "decorator"
            if isinstance(parent, cst.WithItem):
                return "context-manager"
            if isinstance(parent, cst.Arg):
                grandparent = self.get_metadata(ParentNodeProvider, parent, None)
                if isinstance(grandparent, cst.ClassDef) and parent in grandparent.bases:
                    return "class-base"
            # Crossing a statement boundary means this is not nested inside one
            # of the structural expression positions above.
            if isinstance(parent, (cst.BaseStatement, cst.BaseSuite)):
                return None
            current = parent

    def _is_consumed(self, node: cst.CSTNode) -> bool:
        current: cst.CSTNode | None = node
        while current is not None:
            if current in self._consumed:
                return True
            current = self.get_metadata(ParentNodeProvider, current, None)
        return False

    def _line(self, node: cst.CSTNode) -> int:
        return self.get_metadata(PositionProvider, node).start.line

    def _record(
        self,
        node: cst.CSTNode,
        target: str,
        ckind: str | None,
        skind: str | None,
    ) -> None:
        self.records.append({
            "caller": self._caller(),
            "target": target,
            "top": target.split(".")[0],
            "ckind": ckind,
            "skind": skind,
            "line": self._line(node),
            "reached_at": "call" if self._fn_depth else "import",
            "guard": self._guard(node) if skind else None,
        })

    def _emit(
        self,
        node: cst.CSTNode,
        kind: ContactKind,
        protocol: str | None = None,
    ) -> None:
        # Only an attribute path (``imp.reload``, ``collections.Iterable``) is a site here: a bare
        # name was bound by an import statement, and that statement is the site.
        site_kind = None
        if protocol is None and isinstance(node, cst.Attribute):
            site_kind = {"call": "call", "class-base": "class-base"}.get(kind, "attr")
        for name in self._imported_names(node):
            target = f"{name}.{protocol}" if protocol else name
            self._record(node, target, kind, site_kind)

    # ── import statements are stdlib sites, not contacts ────────────────────

    def visit_Import(self, node: cst.Import) -> None:
        for alias in node.names:
            name = _dotted(alias.name)
            if name:
                self._record(alias, name, None, "import")

    def visit_ImportFrom(self, node: cst.ImportFrom) -> None:
        if node.relative or node.module is None or isinstance(node.names, cst.ImportStar):
            return
        module = _dotted(node.module)
        for alias in node.names:
            name = _dotted(alias.name)
            if module and name:
                self._record(alias, f"{module}.{name}", None, "from")

    # ── guards: syntactic facts about what protects a site ──────────────────

    def _guard(self, node: cst.CSTNode) -> dict | None:
        """The strongest syntactic guard around ``node`` (None when unguarded).

        A ``try`` guard stops at a function boundary: the body of a def runs later, outside
        the ``try``. A version ``if`` keeps applying through it.
        """
        best: tuple[int, dict] | None = None

        def consider(kind: str, detail: str) -> None:
            nonlocal best
            rank = _GUARD_RANK[kind]
            if best is None or rank < best[0]:
                best = (rank, {"kind": kind, "detail": detail})

        child: cst.CSTNode = node
        crossed_function = False
        parent = self.get_metadata(ParentNodeProvider, child, None)
        while parent is not None:
            if isinstance(parent, (cst.FunctionDef, cst.Lambda)):
                crossed_function = True
            elif isinstance(parent, _TRY_TYPES) and not crossed_function:
                if child is parent.body:
                    self._guard_try_body(parent, consider)
                elif isinstance(child, cst.ExceptHandler):
                    caught = _caught(child)
                    if caught & _FALLBACK_EXC:
                        consider(
                            "fallback",
                            f"except {_names(caught)} handler, line {self._line(child)}",
                        )
            elif isinstance(parent, (cst.If, cst.IfExp)) and child is not parent.test:
                reason = self._version_test(parent.test)
                if reason:
                    consider("version", f"{reason}, line {self._line(parent)}")
            child, parent = parent, self.get_metadata(ParentNodeProvider, parent, None)
        return best[1] if best else None

    def _guard_try_body(self, node: Any, consider: Any) -> None:
        for handler in node.handlers:
            caught = _caught(handler)
            if caught & _FALLBACK_EXC:
                consider(
                    "guarded-fallback",
                    f"try body, except {_names(caught)}, line {self._line(handler)}",
                )
            elif not caught or caught & _BROAD_EXC or _is_pass_only(handler):
                what = f"except {_names(caught)}" if caught else "bare except"
                if _is_pass_only(handler):
                    what += ": pass"
                consider("broad", f"try body, {what}, line {self._line(handler)}")

    def _version_test(self, test: cst.BaseExpression) -> str | None:
        """What a version-looking ``if`` test reads, or None for an ordinary condition."""
        found: list[str] = []
        outer = self

        class Finder(cst.CSTVisitor):
            def visit_Attribute(self, n: cst.Attribute) -> None:
                dotted = _dotted(n)
                if dotted in _SYS_VERSION or _SYS_VERSION & set(outer._imported_names(n)):
                    found.append(dotted if dotted in _SYS_VERSION else "sys.version_info")
                elif n.attr.value in ("PY2", "PY3"):
                    found.append(dotted or n.attr.value)

            def visit_Name(self, n: cst.Name) -> None:
                if _VERSION_NAME.match(n.value):
                    found.append(n.value)
                elif _SYS_VERSION & set(outer._imported_names(n)):
                    found.append("sys.version_info")

            def visit_Call(self, n: cst.Call) -> None:
                if not (isinstance(n.func, cst.Name) and n.func.value == "hasattr"):
                    return
                if len(n.args) < 2 or not isinstance(n.args[1].value, cst.SimpleString):
                    return
                for name in outer._imported_names(n.args[0].value):
                    if stdlib_data.is_stdlib(name.split(".")[0]):
                        attr = n.args[1].value.evaluated_value
                        found.append(f"hasattr({name}, {attr!r})")
                        return

        test.visit(Finder())
        return found[0] if found else None

    def _emit_protocol(self, node: cst.BaseExpression, method: str) -> None:
        # Decorator/base/context-manager are stronger descriptions of the same
        # syntax than the implicit protocol they may trigger.
        if self._structural_kind(node) is not None:
            return
        self._emit(node, "protocol", method)
        self._consumed.add(node)

    def visit_Call(self, node: cst.Call) -> None:
        structural_kind = self._structural_kind(node)
        self._emit(node.func, structural_kind or "call")
        self._consumed.add(node.func)

        # Builtins such as len(x) are local calls but explicit protocol use on
        # x. Only resolve the argument when it is itself an imported expression.
        if isinstance(node.func, cst.Name) and node.func.value in _BUILTIN_PROTOCOLS:
            if node.args:
                self._emit_protocol(node.args[0].value, _BUILTIN_PROTOCOLS[node.func.value])

    def visit_Subscript(self, node: cst.Subscript) -> None:
        parent = self.get_metadata(ParentNodeProvider, node, None)
        if isinstance(parent, cst.AssignTarget):
            method = "__setitem__"
        elif isinstance(parent, cst.Del):
            method = "__delitem__"
        else:
            method = "__getitem__"
        self._emit_protocol(node.value, method)

    def visit_BinaryOperation(self, node: cst.BinaryOperation) -> None:
        left_method, right_method = _BINARY_PROTOCOLS[type(node.operator)]
        self._emit_protocol(node.left, left_method)
        self._emit_protocol(node.right, right_method)

    def visit_UnaryOperation(self, node: cst.UnaryOperation) -> None:
        self._emit_protocol(node.expression, _UNARY_PROTOCOLS[type(node.operator)])

    def visit_Comparison(self, node: cst.Comparison) -> None:
        left = node.left
        for comparison in node.comparisons:
            operator = comparison.operator
            if isinstance(operator, (cst.In, cst.NotIn)):
                self._emit_protocol(comparison.comparator, "__contains__")
            else:
                methods = _COMPARISON_PROTOCOLS[type(operator)]
                if methods is not None:
                    self._emit_protocol(left, methods[0])
                    self._emit_protocol(comparison.comparator, methods[1])
            left = comparison.comparator

    def visit_For(self, node: cst.For) -> None:
        method = "__aiter__" if node.asynchronous is not None else "__iter__"
        self._emit_protocol(node.iter, method)

    def visit_CompFor(self, node: cst.CompFor) -> None:
        method = "__aiter__" if node.asynchronous is not None else "__iter__"
        self._emit_protocol(node.iter, method)

    def visit_Await(self, node: cst.Await) -> None:
        self._emit_protocol(node.expression, "__await__")

    def visit_Yield(self, node: cst.Yield) -> None:
        if isinstance(node.value, cst.From):
            self._emit_protocol(node.value.item, "__iter__")

    def visit_Attribute(self, node: cst.Attribute) -> None:
        if self._is_consumed(node):
            return
        parent = self.get_metadata(ParentNodeProvider, node, None)
        if isinstance(parent, cst.Attribute):
            return  # only the longest imported attribute path is a contact
        kind = self._structural_kind(node)
        if kind is None:
            names = self._imported_names(node)
            kind = (
                "imported-constant"
                if any(_looks_like_constant(name) for name in names)
                else "attribute-read"
            )
        self._emit(node, kind)

    def visit_Name(self, node: cst.Name) -> None:
        if self._is_consumed(node):
            return
        parent = self.get_metadata(ParentNodeProvider, node, None)
        if isinstance(parent, cst.Attribute):
            return
        names = self._imported_names(node)
        if not names:
            return
        kind = self._structural_kind(node)
        if kind is None:
            kind = (
                "imported-constant"
                if any(_looks_like_constant(name) for name in names)
                else "attribute-read"
            )
        self._emit(node, kind)


_GUARD_RANK = {"fallback": 0, "guarded-fallback": 1, "version": 2, "broad": 3}
_FALLBACK_EXC = {"ImportError", "ModuleNotFoundError", "AttributeError"}
_BROAD_EXC = {"Exception", "BaseException"}
_SYS_VERSION = {"sys.version_info", "sys.version"}
_VERSION_NAME = re.compile(r"^(?:is_?)?py(?:thon)?_?[23](?:\d+|k)?$", re.IGNORECASE)
_TRY_TYPES = tuple(t for t in (cst.Try, getattr(cst, "TryStar", None)) if t is not None)


def _dotted(node: cst.BaseExpression | None) -> str:
    """``a.b.c`` for a Name/Attribute chain ('' for anything else)."""
    parts: list[str] = []
    while isinstance(node, cst.Attribute):
        parts.append(node.attr.value)
        node = node.value
    if isinstance(node, cst.Name):
        parts.append(node.value)
        return ".".join(reversed(parts))
    return ""


def _caught(handler: cst.ExceptHandler) -> set[str]:
    """Exception names a handler catches ('' for a bare ``except:`` is the empty set)."""
    out: set[str] = set()

    def walk(expr: cst.BaseExpression | None) -> None:
        if isinstance(expr, cst.Name):
            out.add(expr.value)
        elif isinstance(expr, cst.Attribute):
            out.add(expr.attr.value)
        elif isinstance(expr, cst.Tuple):
            for el in expr.elements:
                walk(el.value)

    walk(handler.type)
    return out


def _names(caught: set[str]) -> str:
    return "/".join(sorted(caught & _FALLBACK_EXC) or sorted(caught))


def _is_pass_only(handler: cst.ExceptHandler) -> bool:
    body = handler.body
    stmts = body.body
    if isinstance(body, cst.IndentedBlock):
        if len(stmts) != 1 or not isinstance(stmts[0], cst.SimpleStatementLine):
            return False
        stmts = stmts[0].body
    return len(stmts) == 1 and isinstance(stmts[0], cst.Pass)


def _looks_like_constant(qualname: str) -> bool:
    leaf = qualname.rsplit(".", 1)[-1]
    return bool(leaf) and leaf.upper() == leaf and any(char.isalpha() for char in leaf)


def _cache_path(root: Path, module_name: str, text: str) -> Path:
    key = hashlib.sha256(
        f"{CONTACT_MAP_VERSION}\0{module_name}\0{text}".encode("utf-8", "surrogatepass")
    ).hexdigest()
    return root / ".pymolt" / "cache" / "contacts" / f"{key}.json"


def _file_records(root: Path, file: Path, module_name: str) -> list[dict[str, Any]]:
    """Raw per-file records, from the content-keyed cache when this exact file was scanned."""
    text = file.read_text(encoding="utf-8")
    cache = _cache_path(root, module_name, text)
    try:
        cached = json.loads(cache.read_text(encoding="utf-8"))
        if cached.get("version") == CONTACT_MAP_VERSION:
            return cached["records"]
    except (OSError, ValueError, KeyError, AttributeError):
        pass
    wrapper = MetadataWrapper(cst.parse_module(text))
    visitor = _ContactVisitor(module_name)
    wrapper.visit(visitor)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(
            json.dumps({"version": CONTACT_MAP_VERSION, "records": visitor.records}),
            encoding="utf-8",
        )
        os.replace(tmp, cache)
    except OSError:  # a read-only project still scans; it just re-parses next time
        pass
    return visitor.records


def _is_interesting_site(spelling: str) -> bool:
    """Keep a stdlib reference only when the data says something can happen to it.

    Plain ``import os`` / ``os.path.join`` are everywhere and never break; a name whose lifetime
    ends, or a curated change, is what the later phases rate.
    """
    parts = spelling.split(".")
    if stdlib_data.name_lifetime(spelling) is not None and stdlib_data.name_lifetime(spelling)[1]:
        return True
    for i in range(1, len(parts) + 1):
        lifetime = stdlib_data.module_lifetime(".".join(parts[:i]))
        if lifetime is not None and lifetime[1]:
            return True
    return bool(stdlib_data.changes_for(spelling))


def build_contact_map(project_dir: str | Path, dependencies: set[str] | None = None) -> ContactMap:
    """Resolve our-code → dependency contacts statically with LibCST.

    ``dependencies`` (top-level names) optionally restricts what counts as a
    third-party target; otherwise anything that is neither stdlib nor first-party
    is treated as a dependency. Standard-library references whose lifetime the data knows to
    end (or that have a curated change) are kept as ``stdlib_sites``.
    """
    root = Path(project_dir).resolve()
    cmap = ContactMap(root=str(root))
    files, cython = _source_files(root)
    cmap.files_scanned = len(files)
    if cython:
        cmap.unparsed_cython = sorted(str(f.relative_to(root)) for f in cython)
        cmap.notes.append(
            f"{len(cython)} Cython file(s) (.pyx/.pxd/.pxi) are not parsed: standard-library "
            "and dependency use inside them is not seen."
        )
    if not files:
        cmap.notes.append("no Python files found")
        return cmap

    our_top_levels = {_module_name(f, root).split(".")[0] for f in files if _module_name(f, root)}
    file_by_module = {_module_name(f, root): str(f.relative_to(root)) for f in files}
    by_dep: dict[str, set[str]] = {}
    legacy_imports: set[str] = set()
    sites: dict[tuple[str, int, str, str], StdlibSite] = {}
    interesting: dict[str, bool] = {}

    for file in files:
        module_name = _module_name(file, root)
        rel = str(file.relative_to(root))
        try:
            records = _file_records(root, file, module_name)
        except Exception as e:  # noqa: BLE001 — best-effort; one bad file never aborts
            logger.warning("Contact scan failed for %s: %s", file, e)
            cmap.notes.append(f"scan failed for {rel}: {e}")
            continue

        for rec in records:
            dep_top = rec["top"]
            if dep_top in our_top_levels or dep_top in _IGNORE_DEPS:
                continue  # internal, or pymolt's own tooling — not a contact
            if is_py2_stdlib(dep_top):
                # Python 2's standard library is absent from Python 3's stdlib list, so it
                # reads as third-party. It is neither: it is evidence the code has not been
                # ported yet, reported below.
                legacy_imports.add(dep_top)
            if stdlib_data.is_stdlib(dep_top) or is_py2_stdlib(dep_top):
                if rec["skind"]:
                    spelling = rec["target"]
                    if spelling not in interesting:
                        interesting[spelling] = _is_interesting_site(spelling)
                    if interesting[spelling]:
                        sites.setdefault(
                            (rel, rec["line"], spelling, rec["skind"]),
                            StdlibSite(
                                spelling=spelling, module=dep_top, file=rel, line=rec["line"],
                                kind=rec["skind"], reached_at=rec["reached_at"],
                                guard=rec["guard"],
                            ),
                        )
                continue
            if rec["ckind"] is None:
                continue  # an import statement: a stdlib site only
            if dependencies is not None and dep_top not in dependencies:
                continue
            caller = rec["caller"]
            cmap.contacts.append(
                Contact(
                    caller=caller, target=rec["target"], dep=dep_top,
                    file=_file_for_qualname(caller, file_by_module), kind=rec["ckind"],
                    line=rec["line"],
                )
            )
            by_dep.setdefault(dep_top, set()).add(rec["target"])

    # The same symbol can legitimately appear under multiple contact kinds, but
    # repeated syntax of one kind does not add evidence.
    seen: set[tuple[str, str, ContactKind]] = set()
    unique: list[Contact] = []
    for c in sorted(cmap.contacts, key=lambda c: (c.dep, c.target, c.caller, c.kind, c.line or 0)):
        key = (c.caller, c.target, c.kind)
        if key not in seen:
            seen.add(key)
            unique.append(c)
    cmap.contacts = unique
    cmap.by_dep = {dep: sorted(targets) for dep, targets in sorted(by_dep.items())}
    cmap.stdlib_sites = sorted(sites.values(), key=lambda x: (x.file, x.line, x.spelling, x.kind))
    if legacy_imports:
        cmap.notes.append(
            f"Python-2 stdlib imported ({len(legacy_imports)}): {describe(legacy_imports)} "
            "— not dependencies; this code has not been ported to Python 3 yet."
        )
    return cmap
