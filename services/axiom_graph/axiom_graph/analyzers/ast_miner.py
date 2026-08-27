"""
axiom_graph/analyzers/ast_miner.py

AST-based deprecation hint extractor.

Scans Python source files for two patterns:
  1. @deprecated("message")  — decorator form (e.g. from typing_extensions or custom)
  2. warnings.warn("message", DeprecationWarning)  — imperative form

Returns a dict mapping dotted symbol paths to their deprecation message text.
The path is constructed from the file's relative position and the nesting
of class/function definitions visited.

Design notes:
- Pure stdlib: uses only `ast`. No griffe, no runtime imports.
- Ignores test files (test_*.py, */tests/*, */test/*).
- Decorator hint takes priority over warnings.warn hint (decorators are usually
  cleaner, more structured messages).
- Thread-safe: stateless function over immutable inputs.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AST visitor
# ---------------------------------------------------------------------------

class _DeprecationVisitor(ast.NodeVisitor):
    """
    Walks an AST and records deprecation messages for each function/class.
    Context is tracked as a stack of names to build dotted paths.
    """

    def __init__(self, module_prefix: str) -> None:
        self.hints: dict[str, str] = {}
        self._context: list[str] = [module_prefix] if module_prefix else []

    def _current_path(self) -> str:
        return ".".join(self._context)

    def _record(self, path: str, message: str, source: str) -> None:
        """Record a hint. Decorator hints take priority over warn hints."""
        existing = self.hints.get(path)
        if existing is None:
            self.hints[path] = message
        elif source == "decorator":
            # Overwrite any existing hint with the cleaner decorator message
            self.hints[path] = message

    def _check_deprecated_decorator(self, node: ast.FunctionDef | ast.ClassDef) -> None:
        """Look for @deprecated("msg") or @pkg.deprecated("msg")."""
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            func = decorator.func
            is_deprecated = (
                (isinstance(func, ast.Name) and func.id == "deprecated")
                or (isinstance(func, ast.Attribute) and func.attr == "deprecated")
            )
            if is_deprecated and decorator.args:
                if isinstance(decorator.args[0], ast.Constant):
                    self._record(self._current_path(), str(decorator.args[0].value), "decorator")

    def _check_warn_call(self, node: ast.Call) -> None:
        """Look for warnings.warn("msg", DeprecationWarning) anywhere in scope."""
        func = node.func
        is_warn = (
            (isinstance(func, ast.Name) and func.id == "warn")
            or (isinstance(func, ast.Attribute) and func.attr == "warn")
        )
        if not is_warn or not node.args:
            return
        if not isinstance(node.args[0], ast.Constant):
            return

        path = self._current_path()
        if not path:
            return

        # Check if it looks like a DeprecationWarning call
        # Accept if: 2nd arg is DeprecationWarning/PendingDeprecationWarning,
        # or no category given (bare warn with deprecation in text)
        message = str(node.args[0].value)
        is_deprecation_warn = False

        if len(node.args) >= 2:
            cat = node.args[1]
            if isinstance(cat, ast.Name) and "Deprecat" in cat.id:
                is_deprecation_warn = True
            elif isinstance(cat, ast.Attribute) and "Deprecat" in cat.attr:
                is_deprecation_warn = True
        elif "deprecated" in message.lower() or "deprecat" in message.lower():
            is_deprecation_warn = True

        if is_deprecation_warn:
            self._record(path, message, "warn")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._context.append(node.name)
        self._check_deprecated_decorator(node)
        self.generic_visit(node)
        self._context.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # same logic

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._context.append(node.name)
        self._check_deprecated_decorator(node)
        self.generic_visit(node)
        self._context.pop()

    def visit_Call(self, node: ast.Call) -> None:
        self._check_warn_call(node)
        self.generic_visit(node)


# ---------------------------------------------------------------------------
# File-level helpers
# ---------------------------------------------------------------------------

_EXCLUDE_PARTS = frozenset({"tests", "test", "__pycache__", "build", "dist"})


def _is_test_file(path: Path) -> bool:
    """Return True if the path looks like a test file."""
    if path.name.startswith("test_") or path.name.endswith("_test.py"):
        return True
    return bool(set(path.parts) & _EXCLUDE_PARTS)


def _module_prefix(py_file: Path, source_dir: Path, package_name: str) -> str:
    """
    Derive the dotted module path for a Python file.
    E.g. pandas/core/frame.py → pandas.core.frame
    """
    try:
        rel = py_file.relative_to(source_dir)
        parts = list(rel.parts)
        if parts[-1] == "__init__.py":
            parts.pop()
        else:
            parts[-1] = parts[-1][:-3]  # strip .py
        if parts:
            return ".".join([package_name] + parts)
        return package_name
    except ValueError:
        return package_name


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def mine_deprecation_hints(
    source_dir: Path,
    package_name: str,
) -> dict[str, str]:
    """
    Scan all Python source files under ``source_dir`` and extract
    deprecation messages for each public symbol.

    Args:
        source_dir: Root directory of the package source
                    (the directory containing __init__.py).
        package_name: Import name of the package (e.g. "pandas").

    Returns:
        ``{dotted.symbol.path: deprecation_message}``
        e.g. {"pandas.DataFrame.append": "DataFrame.append is deprecated..."}

    Never raises — logs warnings and skips unparseable files.
    """
    all_hints: dict[str, str] = {}

    if not source_dir.exists():
        log.warning("mine_deprecation_hints: source_dir not found: %s", source_dir)
        return all_hints

    for py_file in source_dir.rglob("*.py"):
        if not py_file.is_file():
            continue
        if _is_test_file(py_file):
            continue

        module_prefix = _module_prefix(py_file, source_dir.parent, package_name)

        try:
            source = py_file.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError as exc:
            log.debug("Skipping unparseable file %s: %s", py_file, exc)
            continue

        visitor = _DeprecationVisitor(module_prefix)
        visitor.visit(tree)

        if visitor.hints:
            all_hints.update(visitor.hints)

    log.info(
        "AST mining complete: %d deprecation hints in %s",
        len(all_hints), package_name
    )
    return all_hints
