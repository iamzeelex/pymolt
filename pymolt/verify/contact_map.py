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

import logging
import os
import sys
from pathlib import Path
from typing import Literal

import libcst as cst
from libcst.metadata import (
    MetadataWrapper,
    ParentNodeProvider,
    QualifiedNameProvider,
    QualifiedNameSource,
)
from pydantic import BaseModel, Field

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

# pymolt's own injected tooling — if the watcher bundle (export-watcher) was written
# into the project, its `pymolt_trace` package is *not* a real dependency.
_IGNORE_DEPS = {"pymolt", "pymolt_trace"}

ContactKind = Literal[
    "call",
    "attribute-read",
    "decorator",
    "class-base",
    "context-manager",
    "imported-constant",
    "protocol",
]


class Contact(BaseModel):
    caller: str  # our qualname, e.g. "myapp.views.handler"
    target: str  # dependency qualname, e.g. "flask.cli.with_appcontext"
    dep: str  # top-level dependency, e.g. "flask"
    file: str | None = None  # our file the caller is defined in (best-effort)
    # Default preserves compatibility with contact maps serialized before kinds
    # were introduced: every old contact represented a direct call.
    kind: ContactKind = "call"


class ContactMap(BaseModel):
    root: str
    files_scanned: int = 0
    contacts: list[Contact] = Field(default_factory=list)
    by_dep: dict[str, list[str]] = Field(default_factory=dict)  # dep -> sorted targets
    notes: list[str] = Field(default_factory=list)


def _python_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS and not d.endswith(".egg-info")]
        for name in filenames:
            if name.endswith(".py"):
                files.append(Path(dirpath) / name)
    return files


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

    METADATA_DEPENDENCIES = (QualifiedNameProvider, ParentNodeProvider)

    def __init__(self, module_name: str) -> None:
        self.module_name = module_name
        self._scope: list[str] = []
        self.contacts: list[tuple[str, str, str, ContactKind]] = []
        # Roots consumed by a protocol operation. Their child Name/Attribute
        # nodes must not also be reported as plain reads.
        self._consumed: set[cst.CSTNode] = set()

    def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
        self._scope.append(node.name.value)

    def leave_FunctionDef(self, node: cst.FunctionDef) -> None:
        self._scope.pop()

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

    def _emit(
        self,
        node: cst.CSTNode,
        kind: ContactKind,
        protocol: str | None = None,
    ) -> None:
        for name in self._imported_names(node):
            target = f"{name}.{protocol}" if protocol else name
            self.contacts.append((self._caller(), target, name.split(".")[0], kind))

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


def _looks_like_constant(qualname: str) -> bool:
    leaf = qualname.rsplit(".", 1)[-1]
    return bool(leaf) and leaf.upper() == leaf and any(char.isalpha() for char in leaf)


def build_contact_map(project_dir: str | Path, dependencies: set[str] | None = None) -> ContactMap:
    """Resolve our-code → dependency contacts statically with LibCST.

    ``dependencies`` (top-level names) optionally restricts what counts as a
    third-party target; otherwise anything that is neither stdlib nor first-party
    is treated as a dependency.
    """
    root = Path(project_dir).resolve()
    cmap = ContactMap(root=str(root))
    files = _python_files(root)
    cmap.files_scanned = len(files)
    if not files:
        cmap.notes.append("no Python files found")
        return cmap

    our_top_levels = {_module_name(f, root).split(".")[0] for f in files if _module_name(f, root)}
    file_by_module = {_module_name(f, root): str(f.relative_to(root)) for f in files}
    stdlib = sys.stdlib_module_names
    by_dep: dict[str, set[str]] = {}
    legacy_imports: set[str] = set()

    for file in files:
        module_name = _module_name(file, root)
        try:
            wrapper = MetadataWrapper(cst.parse_module(file.read_text(encoding="utf-8")))
            visitor = _ContactVisitor(module_name)
            wrapper.visit(visitor)
        except Exception as e:  # noqa: BLE001 — best-effort; one bad file never aborts
            logger.warning("Contact scan failed for %s: %s", file, e)
            cmap.notes.append(f"scan failed for {file.relative_to(root)}: {e}")
            continue

        for caller, target, dep_top, kind in visitor.contacts:
            if dep_top in our_top_levels or dep_top in stdlib or dep_top in _IGNORE_DEPS:
                continue  # internal, stdlib, or pymolt's own tooling — not a contact
            if is_py2_stdlib(dep_top):
                # Python 2's standard library is absent from this interpreter's
                # stdlib_module_names, so it reads as third-party. It is neither:
                # it is evidence the code has not been ported yet, reported below.
                legacy_imports.add(dep_top)
                continue
            if dependencies is not None and dep_top not in dependencies:
                continue
            call_file = _file_for_qualname(caller, file_by_module)
            cmap.contacts.append(
                Contact(caller=caller, target=target, dep=dep_top, file=call_file, kind=kind)
            )
            by_dep.setdefault(dep_top, set()).add(target)

    # The same symbol can legitimately appear under multiple contact kinds, but
    # repeated syntax of one kind does not add evidence.
    seen: set[tuple[str, str, ContactKind]] = set()
    unique: list[Contact] = []
    for c in sorted(cmap.contacts, key=lambda c: (c.dep, c.target, c.caller, c.kind)):
        key = (c.caller, c.target, c.kind)
        if key not in seen:
            seen.add(key)
            unique.append(c)
    cmap.contacts = unique
    cmap.by_dep = {dep: sorted(targets) for dep, targets in sorted(by_dep.items())}
    if legacy_imports:
        cmap.notes.append(
            f"Python-2 stdlib imported ({len(legacy_imports)}): {describe(legacy_imports)} "
            "— not dependencies; this code has not been ported to Python 3 yet."
        )
    return cmap
