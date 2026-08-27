"""
axiom_graph/analyzers/upstream_compat.py

Mines upstream compatibility and migration dictionaries built directly by package authors.
Many major frameworks (TensorFlow, JAX, SQLAlchemy, Pydantic) include official AST/symbol
transformation dictionaries inside their source distributions.

Examples:
- tensorflow: `tensorflow/tools/compatibility/renames_v2.py` -> `renames = {...}`, `symbol_renames`
- jax: `jax/_src/deprecations.py` -> `_deprecated_function_replacements = {...}`
- general: `compat.py`, `deprecated.py`, `migration.py` dict literals mapping old -> new symbols.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Any

from axiom_graph.core.models import ApiState, BreakingChange, ChangeRisk, CodemodPattern

log = logging.getLogger(__name__)

# Known author-provided migration table variable names
KNOWN_MIGRATION_DICT_NAMES = {
    "renames",
    "renames_v2",
    "symbol_renames",
    "keyword_renames",
    "replacements",
    "function_renames",
    "deprecated_renames",
    "_deprecated_function_replacements",
    "DEPRECATED_NAMES",
    "RENAMED_ATTRIBUTES",
    "MOVED_SYMBOLS",
}

# Known file substrings that likely contain upstream migration tables
TARGET_FILE_SUBSTRINGS = (
    "compat",
    "rename",
    "deprecat",
    "upgrade",
    "migration",
    "v2",
    "v1",
)


def _extract_dict_from_ast(node: ast.Dict) -> dict[str, str]:
    """Safely extract string-to-string mappings from an ast.Dict node."""
    mapping: dict[str, str] = {}
    for key_node, val_node in zip(node.keys, node.values):
        if not key_node or not val_node:
            continue
        key_str = None
        val_str = None

        if isinstance(key_node, ast.Constant) and isinstance(key_node.value, str):
            key_str = key_node.value
        elif isinstance(key_node, ast.Attribute):
            parts = []
            cur = key_node
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name):
                parts.append(cur.id)
                key_str = ".".join(reversed(parts))

        if isinstance(val_node, ast.Constant) and isinstance(val_node.value, str):
            val_str = val_node.value
        elif isinstance(val_node, ast.Attribute):
            parts = []
            cur = val_node
            while isinstance(cur, ast.Attribute):
                parts.append(cur.attr)
                cur = cur.value
            if isinstance(cur, ast.Name):
                parts.append(cur.id)
                val_str = ".".join(reversed(parts))

        if key_str and val_str and key_str != val_str:
            mapping[key_str] = val_str

    return mapping


class _UpstreamCompatVisitor(ast.NodeVisitor):
    """AST visitor that finds assignment of migration dictionaries."""

    def __init__(self) -> None:
        self.mappings: dict[str, str] = {}

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            name = None
            if isinstance(target, ast.Name):
                name = target.id
            elif isinstance(target, ast.Attribute):
                name = target.attr

            if name and (name in KNOWN_MIGRATION_DICT_NAMES or any(k in name.lower() for k in ("rename", "deprecated_map", "replacement"))):
                if isinstance(node.value, ast.Dict):
                    self.mappings.update(_extract_dict_from_ast(node.value))
        self.generic_visit(node)


def mine_upstream_compatibility_tables(
    source_root: Path,
    package: str,
) -> tuple[list[BreakingChange], list[CodemodPattern]]:
    """
    Scan the source tree for official upstream compatibility and rename tables.

    Returns:
        tuple (breaking_changes, codemod_patterns) synthesized from official tables.
    """
    if not source_root or not source_root.is_dir():
        return [], []

    extracted_mappings: dict[str, str] = {}

    # Look for candidate python files
    for py_file in source_root.rglob("*.py"):
        fname_lower = py_file.name.lower()
        parent_lower = py_file.parent.name.lower()
        if not any(sub in fname_lower or sub in parent_lower for sub in TARGET_FILE_SUBSTRINGS):
            continue

        try:
            tree = ast.parse(py_file.read_text(encoding="utf-8", errors="replace"), filename=str(py_file))
            visitor = _UpstreamCompatVisitor()
            visitor.visit(tree)
            if visitor.mappings:
                log.info("Found %d upstream renames in %s", len(visitor.mappings), py_file.name)
                extracted_mappings.update(visitor.mappings)
        except Exception as exc:
            log.debug("Could not parse candidate compat file %s: %s", py_file, exc)

    if not extracted_mappings:
        return [], []

    log.info("Extracted total %d author-provided renames for %s", len(extracted_mappings), package)

    breaking_changes: list[BreakingChange] = []
    patterns: list[CodemodPattern] = []

    for old_sym, new_sym in extracted_mappings.items():
        kind = "rename-call"
        if "." in old_sym and "." in new_sym:
            old_mod = old_sym.rsplit(".", 1)[0]
            new_mod = new_sym.rsplit(".", 1)[0]
            if old_mod != new_mod:
                kind = "rewrite-import"

        pattern = CodemodPattern(
            old_qualname=old_sym,
            new_qualname=new_sym,
            kind=kind,
            confidence="high",
            evidence=[f"upstream_author_compat_table({package})"],
        )
        patterns.append(pattern)

        change = BreakingChange(
            path=old_sym,
            kind="object-removed",
            risk=ChangeRisk.MECHANICAL,
            explanation=f"Public symbol replaced by {new_sym} in official {package} migration table",
            state=ApiState.REMOVED,
            deprecation_hint=f"Official upstream replacement: {new_sym}",
            migration_patterns=[pattern.model_dump()],
        )
        breaking_changes.append(change)

    return breaking_changes, patterns
