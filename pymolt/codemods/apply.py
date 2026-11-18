"""
pymolt/codemods/apply.py

Apply codemod patterns (Tier-1) and codemod rules (Tier-2) to local source with
LibCST — format-preserving, so the author's whitespace, comments and layout
survive.

Two transformers cover the common Tier-1 deprecation shapes:
  - rewrite-import : same leaf, module moved → rewrite the `from … import …` line
                     (multi-name imports split onto their own lines).
  - rename-call    : leaf changed → rewrite call sites + the import name.

`apply_to_repo` walks a project's .py files (skipping virtualenvs, caches and
dot-dirs), applies the patterns, and reports what changed. It is dry-run by
default — nothing is written unless `write=True`.

`apply_rules_to_repo` / `preview_rules_repo` are the Tier-2 counterparts: they
walk the same file set and delegate the actual match/rewrite to
`pymolt.codemods.rules.apply_rule_detailed` (the declarative rule engine, which
also delegates LEGACY-kind rules straight back to the two transformers above —
see rules.py's module docstring).
"""

from __future__ import annotations

import difflib
import logging
import os
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import libcst as cst
from libcst.codemod import CodemodContext
from libcst.codemod.visitors import AddImportsVisitor, RemoveImportsVisitor
from libcst.metadata import MetadataWrapper, PositionProvider, ScopeProvider
from libcst.metadata.scope_provider import ImportAssignment

from pymolt.codemods.models import (
    BlockedRewrite,
    CodemodPattern,
    CodemodRunResult,
    FileChange,
    FilePreview,
    FileStepObserver,
    MoveGroup,
    PreviewProgress,
    ProgressSink,
    StepObserver,
    recipe_key,
)
from pymolt.codemods.rules import CodemodRule, RuleAdvisory, apply_rule_detailed

log = logging.getLogger(__name__)

# Directories we never rewrite into.
_SKIP_DIRS = {
    ".git", "__pycache__", ".venv", "venv", "env", ".env", "site-packages",
    "node_modules", "build", "dist", ".tox", ".nox", ".mypy_cache",
    ".pytest_cache", ".eggs",
}


def _leaf(qualname: str) -> str:
    return qualname.rsplit(".", 1)[-1]


def _module(qualname: str) -> str:
    return qualname.rsplit(".", 1)[0] if "." in qualname else ""


def _dotted(node: cst.BaseExpression) -> str | None:
    if isinstance(node, cst.Name):
        return node.value
    if isinstance(node, cst.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr.value}" if base else node.attr.value
    return None


def _module_expr(dotted: str) -> cst.BaseExpression:
    return cst.parse_expression(dotted)


def _clean_last_comma(alias: cst.ImportAlias) -> cst.ImportAlias:
    if isinstance(alias.comma, cst.Comma):
        return alias.with_changes(comma=cst.MaybeSentinel.DEFAULT)
    return alias


# ─────────────────────────────────────────────────────────────────────────────
# Transformers
# ─────────────────────────────────────────────────────────────────────────────


class RewriteImportCodemod(cst.CSTTransformer):
    """`from OLD_MODULE import LEAF` → `from NEW_MODULE import LEAF`."""

    def __init__(self, old_module: str, new_module: str, leaf: str) -> None:
        self.old_module = old_module
        self.new_module = new_module
        self.leaf = leaf
        self.sites = 0

    def leave_SimpleStatementLine(
        self, original: cst.SimpleStatementLine, updated: cst.SimpleStatementLine
    ) -> cst.SimpleStatementLine | cst.FlattenSentinel:
        if len(updated.body) != 1 or not isinstance(updated.body[0], cst.ImportFrom):
            return updated
        imp = updated.body[0]
        if imp.module is None or isinstance(imp.names, cst.ImportStar):
            return updated
        if _dotted(imp.module) != self.old_module:
            return updated
        if not any(a.name.value == self.leaf for a in imp.names):
            return updated

        self.sites += 1
        others = [a for a in imp.names if a.name.value != self.leaf]
        if not others:
            moved = imp.with_changes(
                module=_module_expr(self.new_module),
                names=[_clean_last_comma(a) for a in imp.names],
            )
            return updated.with_changes(body=[moved])
        kept = updated.with_changes(
            body=[imp.with_changes(names=[_clean_last_comma(a) for a in others])]
        )
        new_line = cst.SimpleStatementLine(
            body=[
                cst.ImportFrom(
                    module=_module_expr(self.new_module),
                    names=[cst.ImportAlias(name=cst.Name(self.leaf))],
                )
            ]
        )
        return cst.FlattenSentinel([kept, new_line])


class RenameCallCodemod(cst.CSTTransformer):
    """
    Rename a symbol where it is BOUND to the imported target — not by bare name.

    This is the graph-aware (not textual) rewrite: it consults LibCST's scope
    graph (ScopeProvider) so an unrelated local `old` or a same-named symbol
    imported from a DIFFERENT module is left untouched. Must run through a
    MetadataWrapper (see apply_pattern).

    Handled forms:
      - `from old_module import old_leaf [as a]` + calls to the bound name
        (calls to the bare name are renamed; aliased imports rewrite only the
        import and keep the alias-name call sites valid).
      - `import old_module [as m]` + `m.old_leaf(...)` attribute calls.
    """

    METADATA_DEPENDENCIES = (ScopeProvider,)

    def __init__(self, old_qualname: str, new_qualname: str) -> None:
        self.old_leaf = _leaf(old_qualname)
        self.new_leaf = _leaf(new_qualname)
        self.old_module = _module(old_qualname)
        self.new_module = _module(new_qualname)
        self.sites = 0

    def _import_binding(self, name_node: cst.Name) -> str | None:
        """'direct' | 'aliased' | None — is `name` bound to OUR imported symbol?"""
        bindings = self._bindings(name_node)
        if bindings is None:
            return None
        matched = None
        has_local = False
        for b in bindings:
            node = getattr(b, "node", None)
            if isinstance(b, ImportAssignment) and isinstance(node, cst.ImportFrom):
                if (
                    node.module is not None
                    and _dotted(node.module) == self.old_module
                    and not isinstance(node.names, cst.ImportStar)
                ):
                    for alias in node.names:
                        bound = alias.evaluated_alias or alias.evaluated_name
                        if alias.evaluated_name == self.old_leaf and bound == name_node.value:
                            matched = alias
            elif not isinstance(b, ImportAssignment):
                has_local = True  # a local def/assignment shadows it → leave alone
        if matched is None or has_local:
            return None
        return "aliased" if matched.asname is not None else "direct"

    def _module_binding(self, name_node: cst.Name) -> bool:
        """True if `name` is bound to `import old_module` (for m.old_leaf())."""
        bindings = self._bindings(name_node)
        if bindings is None:
            return False
        for b in bindings:
            node = getattr(b, "node", None)
            if isinstance(b, ImportAssignment) and isinstance(node, cst.Import):
                for alias in node.names:
                    bound = alias.evaluated_alias or alias.evaluated_name
                    if alias.evaluated_name == self.old_module and bound == name_node.value:
                        return True
        return False

    def _bindings(self, name_node: cst.Name):
        try:
            scope = self.get_metadata(ScopeProvider, name_node)
        except KeyError:
            return None
        if scope is None:
            return None
        try:
            return list(scope[name_node.value])
        except KeyError:
            return None

    def leave_Call(self, original: cst.Call, updated: cst.Call) -> cst.BaseExpression:
        ofunc, ufunc = original.func, updated.func
        if isinstance(ufunc, cst.Name):
            if self._import_binding(ofunc) == "direct":
                self.sites += 1
                return updated.with_changes(func=cst.Name(self.new_leaf))
            return updated
        if isinstance(ufunc, cst.Attribute) and ufunc.attr.value == self.old_leaf:
            base = ofunc.value
            if isinstance(base, cst.Name) and self._module_binding(base):
                self.sites += 1
                return updated.with_changes(
                    func=ufunc.with_changes(attr=cst.Name(self.new_leaf))
                )
        return updated

    def leave_ImportFrom(
        self, original: cst.ImportFrom, updated: cst.ImportFrom
    ) -> cst.ImportFrom:
        if updated.module is None or isinstance(updated.names, cst.ImportStar):
            return updated
        if _dotted(updated.module) != self.old_module:
            return updated
        new_names = []
        changed = False
        for alias in updated.names:
            if alias.name.value == self.old_leaf:
                changed = True
                alias = alias.with_changes(name=cst.Name(self.new_leaf))
            new_names.append(alias)
        if not changed:
            return updated
        self.sites += 1
        module = updated.module
        if self.new_module and self.new_module != self.old_module:
            module = _module_expr(self.new_module)
        return updated.with_changes(names=new_names, module=module)


# ─────────────────────────────────────────────────────────────────────────────
# Application
# ─────────────────────────────────────────────────────────────────────────────


class RewriteModuleCodemod(cst.CSTTransformer):
    """Move a whole module namespace: ``OLD`` (and any ``OLD.sub``) → ``NEW`` (``NEW.sub``),
    regardless of which symbols are imported. Unlike RewriteImportCodemod (a single
    symbol), this is what a framework succession needs — e.g. one rule moves all of
    ``keras`` → ``tensorflow.keras`` (keras.layers, keras.models, keras.backend, …).

      from OLD[.sub] import a, b   →  from NEW[.sub] import a, b
      import OLD[.sub] as x        →  import NEW[.sub] as x
      import OLD                   →  import NEW as OLD   (preserve the bound name)

    A bare dotted ``import OLD.sub`` (no alias) is left untouched: rebinding it safely is
    ambiguous, so it surfaces as an unmigrated site rather than a wrong rewrite.
    """

    def __init__(self, old_module: str, new_module: str) -> None:
        self.old_module = old_module
        self.new_module = new_module
        self.sites = 0

    def _remap(self, dotted: str | None) -> str | None:
        if dotted == self.old_module:
            return self.new_module
        if dotted and dotted.startswith(self.old_module + "."):
            return self.new_module + dotted[len(self.old_module):]
        return None

    def leave_ImportFrom(
        self, original_node: cst.ImportFrom, updated_node: cst.ImportFrom
    ) -> cst.ImportFrom:
        if updated_node.module is None:
            return updated_node
        remapped = self._remap(_dotted(updated_node.module))
        if remapped is None:
            return updated_node
        self.sites += 1
        return updated_node.with_changes(module=_module_expr(remapped))

    def leave_Import(self, original_node: cst.Import, updated_node: cst.Import) -> cst.Import:
        new_names: list[cst.ImportAlias] = []
        changed = False
        for alias in updated_node.names:
            dotted = _dotted(alias.name)
            remapped = self._remap(dotted)
            if remapped is None:
                new_names.append(alias)
                continue
            if alias.asname is not None:
                new_names.append(alias.with_changes(name=_module_expr(remapped)))
                changed = True
                self.sites += 1
            elif dotted == self.old_module and "." not in self.old_module:
                # `import OLD` → `import NEW as OLD`, so `OLD.x` references keep working.
                new_names.append(alias.with_changes(
                    name=_module_expr(remapped),
                    asname=cst.AsName(name=cst.Name(self.old_module)),
                ))
                changed = True
                self.sites += 1
            else:
                new_names.append(alias)  # bare dotted import — ambiguous rebinding, skip
        return updated_node.with_changes(names=new_names) if changed else updated_node


def _common_module(old_qualname: str, new_qualname: str) -> str:
    """Longest shared leading dotted prefix (the imported root), e.g.
    ('tensorflow.Session', 'tensorflow.compat.v1.Session') → 'tensorflow'."""
    common: list[str] = []
    for a, b in zip(old_qualname.split("."), new_qualname.split("."), strict=False):
        if a != b:
            break
        common.append(a)
    return ".".join(common)


def _attr_chain(node: cst.BaseExpression) -> tuple[cst.Name | None, str]:
    """For an attribute expression, return (leftmost Name, dotted tail after it).
    ``tf.train.Optimizer`` → (Name('tf'), 'train.Optimizer'); non-attribute → (None, '')."""
    parts: list[str] = []
    cur = node
    while isinstance(cur, cst.Attribute):
        parts.append(cur.attr.value)
        cur = cur.value
    if not isinstance(cur, cst.Name):
        return None, ""
    parts.reverse()
    return cur, ".".join(parts)


def _covers(transformer: cst.MetadataDependent, node: cst.CSTNode,
            lines: frozenset[int] | None) -> bool:
    """True when ``lines`` is None (no restriction) or one of them falls inside ``node``."""
    if lines is None:
        return True
    try:
        pos = transformer.get_metadata(PositionProvider, node)
    except KeyError:
        return False
    return any(pos.start.line <= n <= pos.end.line for n in lines)


class RewriteAttrCodemod(cst.CSTTransformer):
    """Insert/rewrite a module segment inside an attribute chain: ``m.OLD_TAIL`` →
    ``m.NEW_TAIL`` where ``m`` is bound to ``import ROOT [as m]``. This is the form
    a leaf rename / import move can't express — e.g. ``tensorflow.Session`` →
    ``tensorflow.compat.v1.Session`` rewrites ``tf.Session(...)`` to
    ``tf.compat.v1.Session(...)`` (what tf_upgrade_v2 does for TF1→TF2). Binding-aware
    (ScopeProvider): only true references to the imported module are moved.

    ``lines`` restricts the rewrite to nodes covering one of those source lines (python
    fixes only touch flagged, unguarded sites). ``needed_imports`` collects the modules the
    rewritten code needs (``requires_import``), for :func:`_finish_imports`.
    """

    METADATA_DEPENDENCIES = (ScopeProvider, PositionProvider)

    def __init__(self, old_qualname: str, new_qualname: str, *,
                 requires_import: str | None = None,
                 lines: frozenset[int] | None = None) -> None:
        self.root = _common_module(old_qualname, new_qualname)
        self.old_tail = old_qualname[len(self.root) + 1:] if self.root else ""
        self.new_tail = new_qualname[len(self.root) + 1:] if self.root else ""
        self.requires_import = requires_import
        self.lines = lines
        self.needed_imports: set[tuple[str, str | None]] = set()
        self.sites = 0

    def _bound_to_root(self, name_node: cst.Name) -> bool:
        return _bound_to_module(self, name_node, self.root)

    def leave_Attribute(
        self, original_node: cst.Attribute, updated_node: cst.Attribute
    ) -> cst.BaseExpression:
        if not self.old_tail:
            return updated_node
        base, tail = _attr_chain(original_node)
        if base is None or tail != self.old_tail or not self._bound_to_root(base):
            return updated_node
        if not _covers(self, original_node, self.lines):
            return updated_node
        self.sites += 1
        if self.requires_import:
            self.needed_imports.add((self.requires_import, None))
        expr: cst.BaseExpression = cst.Name(base.value)
        for seg in self.new_tail.split("."):
            expr = cst.Attribute(value=expr, attr=cst.Name(seg))
        return expr.with_changes(lpar=updated_node.lpar, rpar=updated_node.rpar)


def _bound_to_module(transformer: cst.MetadataDependent, name_node: cst.Name,
                     module: str) -> bool:
    """Is ``name_node`` bound by ``import MODULE [as name]`` (and by nothing else)?"""
    try:
        scope = transformer.get_metadata(ScopeProvider, name_node)
    except KeyError:
        return False
    if scope is None:
        return False
    try:
        bindings = list(scope[name_node.value])
    except KeyError:
        return False
    found = False
    for b in bindings:
        node = getattr(b, "node", None)
        if isinstance(b, ImportAssignment) and isinstance(node, cst.Import):
            for alias in node.names:
                bound = alias.evaluated_alias or alias.evaluated_name
                if alias.evaluated_name == module and bound == name_node.value:
                    found = True
    return found


class _NameCollector(cst.CSTVisitor):
    def __init__(self) -> None:
        self.names: set[str] = set()

    def visit_Name(self, node: cst.Name) -> None:
        self.names.add(node.value)

    def visit_Attribute(self, node: cst.Attribute) -> bool:
        node.value.visit(self)      # ``a.current_task``: the attribute is not a binding
        return False

    # Import statements name modules, not variables: only the names they bind (``as x``,
    # ``from m import x``) count.
    def visit_ImportFrom(self, node: cst.ImportFrom) -> bool:
        if not isinstance(node.names, cst.ImportStar):
            for alias in node.names:
                self.names.add(alias.evaluated_alias or alias.evaluated_name)
        return False

    def visit_Import(self, node: cst.Import) -> bool:
        for alias in node.names:
            if alias.evaluated_alias:
                self.names.add(alias.evaluated_alias)
        return False


def _from_binding(transformer: cst.MetadataDependent,
                  name_node: cst.Name) -> tuple[str, str, str | None] | None:
    """``(module, name, asname)`` when ``name_node`` is bound only by ``from MODULE import name
    [as asname]`` (absolute import), else None."""
    try:
        scope = transformer.get_metadata(ScopeProvider, name_node)
        bindings = list(scope[name_node.value]) if scope is not None else []
    except KeyError:
        return None
    found: tuple[str, str, str | None] | None = None
    for b in bindings:
        node = getattr(b, "node", None)
        if not (isinstance(b, ImportAssignment) and isinstance(node, cst.ImportFrom)):
            return None
        if node.module is None or node.relative or isinstance(node.names, cst.ImportStar):
            return None
        for alias in node.names:
            bound = alias.evaluated_alias or alias.evaluated_name
            if bound == name_node.value:
                found = (_dotted(node.module) or "", alias.evaluated_name,
                         alias.evaluated_alias)
    return found


class ReplaceAttrPathCodemod(cst.CSTTransformer):
    """A cross-module rename of an attribute path: ``imp.reload`` → ``importlib.reload``,
    ``asyncio.Task.current_task`` → ``asyncio.current_task``.

    The leftmost name must be bound to ``import OLD_ROOT [as a]``, or to a ``from M import N``
    whose ``M.N`` leads the old path (``from asyncio import Task`` + ``Task.current_task()``;
    the new path must then be ``MODULE.name``, written as ``name`` plus ``from MODULE import
    name``). When the new path lives in the same root module the alias is kept
    (``a.current_task``); otherwise the new root is named in full and imported
    (``requires_import``, default the new root). ``needed_imports`` and ``dropped`` (imports that
    may now be unused) feed :func:`_finish_imports`.
    """

    METADATA_DEPENDENCIES = (ScopeProvider, PositionProvider)

    def __init__(self, old_qualname: str, new_qualname: str, *,
                 requires_import: str | None = None,
                 lines: frozenset[int] | None = None) -> None:
        self.old_qualname = old_qualname
        self.old_root = old_qualname.split(".")[0]
        self.old_tail = old_qualname[len(self.old_root) + 1:]
        self.new_qualname = new_qualname
        self.new_root = new_qualname.split(".")[0]
        self.new_tail = new_qualname[len(self.new_root) + 1:]
        self.requires_import = requires_import
        self.lines = lines
        self.needed_imports: set[tuple[str, str | None]] = set()
        self.dropped: set[tuple[str, str | None, str | None]] = set()
        self.sites = 0
        self._identifiers: set[str] = set()

    def visit_Module(self, node: cst.Module) -> None:
        # every identifier in the file: a new bare name must not collide with one of them
        collector = _NameCollector()
        node.visit(collector)
        self._identifiers = collector.names

    def leave_Attribute(
        self, original_node: cst.Attribute, updated_node: cst.Attribute
    ) -> cst.BaseExpression:
        base, tail = _attr_chain(original_node)
        if base is None or not tail or not _covers(self, original_node, self.lines):
            return updated_node
        expr: cst.BaseExpression | None = None
        if tail == self.old_tail and _bound_to_module(self, base, self.old_root):
            expr = self._via_import(base)
        else:
            bound = _from_binding(self, base)
            if bound is not None and f"{bound[0]}.{bound[1]}.{tail}" == self.old_qualname:
                expr = self._via_from_import(bound)
        if expr is None:
            return updated_node
        self.sites += 1
        return expr.with_changes(lpar=updated_node.lpar, rpar=updated_node.rpar)

    def _via_import(self, base: cst.Name) -> cst.BaseExpression:
        if self.new_root == self.old_root and self.new_tail:
            expr: cst.BaseExpression = cst.Name(base.value)
            for seg in self.new_tail.split("."):
                expr = cst.Attribute(value=expr, attr=cst.Name(seg))
            return expr
        self.needed_imports.add((self.requires_import or self.new_root, None))
        self.dropped.add((self.old_root, None, None if base.value == self.old_root
                          else base.value))
        return cst.parse_expression(self.new_qualname)

    def _via_from_import(self, bound: tuple[str, str, str | None]) -> cst.BaseExpression | None:
        new_module, new_leaf = _module(self.new_qualname), _leaf(self.new_qualname)
        if not new_module:
            return None
        self.dropped.add((bound[0], bound[1], bound[2]))
        if new_leaf not in self._identifiers:
            self.needed_imports.add((new_module, new_leaf))
            return cst.Name(new_leaf)
        # the bare name is already used here (``current_task = Task.current_task()``): name the
        # module instead, unless that name is taken too
        if new_module.split(".")[0] in self._identifiers:
            self.dropped.discard((bound[0], bound[1], bound[2]))
            return None
        self.needed_imports.add((new_module, None))
        return cst.parse_expression(self.new_qualname)


class _LocatedRewriteModule(RewriteModuleCodemod):
    """``RewriteModuleCodemod`` limited to statements covering ``lines``."""

    METADATA_DEPENDENCIES = (PositionProvider,)

    def __init__(self, old_module: str, new_module: str, lines: frozenset[int] | None) -> None:
        super().__init__(old_module, new_module)
        self.lines = lines

    def leave_ImportFrom(self, original_node, updated_node):
        if not _covers(self, original_node, self.lines):
            return updated_node
        return super().leave_ImportFrom(original_node, updated_node)

    def leave_Import(self, original_node, updated_node):
        if not _covers(self, original_node, self.lines):
            return updated_node
        return super().leave_Import(original_node, updated_node)


class MoveNamesCodemod(cst.CSTTransformer):
    """Move (and optionally rename) names out of ``from OLD import ...`` in ONE pass.

    ``renames`` maps each old leaf to its new leaf. Every statement ``from OLD import a, b, c``
    is rewritten as a whole, so the names that move to ``NEW`` land in one statement and the
    rest stay in the original:

      from OLD import a, b, c     →  from OLD import c      (the names left behind)
                                     from NEW import a, b    (the moved names, in order)

    When every name moves, the statement is edited in place (module swapped), keeping its
    parentheses, line breaks and comments. A leaf rename without ``as`` also renames every
    reference to the bound name, found through the scope graph; if the name is rebound
    elsewhere in its scope the alias is left alone (a half-rewrite would break the file).
    ``lines`` restricts the rewrite to statements covering one of those source lines.
    """

    METADATA_DEPENDENCIES = (ScopeProvider, PositionProvider)

    def __init__(self, old_module: str, new_module: str, renames: dict[str, str], *,
                 lines: frozenset[int] | None = None) -> None:
        self.old_module = old_module
        self.new_module = new_module
        self.renames = renames
        self.lines = lines
        self.hits: dict[str, int] = {}
        self.sites = 0
        self._ref_renames: dict[cst.Name, str] = {}
        self._movable: dict[cst.ImportFrom, set[str]] = {}

    # a statement's movable leaves are decided on the way in, with the scope in hand
    def visit_ImportFrom(self, node: cst.ImportFrom) -> bool | None:
        if node.module is None or isinstance(node.names, cst.ImportStar) or node.relative:
            return None
        if _dotted(node.module) != self.old_module or not _covers(self, node, self.lines):
            return None
        movable: set[str] = set()
        for alias in node.names:
            leaf = alias.name.value if isinstance(alias.name, cst.Name) else None
            if leaf not in self.renames:
                continue
            new_leaf = self.renames[leaf]
            if new_leaf != leaf and alias.asname is None:
                refs = self._references(node, leaf)
                if refs is None:
                    continue
                for ref in refs:
                    self._ref_renames[ref] = new_leaf
            movable.add(leaf)
        if movable:
            self._movable[node] = movable
        return None

    def _references(self, node: cst.ImportFrom, name: str) -> list[cst.Name] | None:
        try:
            scope = self.get_metadata(ScopeProvider, node)
            assignments = list(scope[name])
        except KeyError:
            return None
        mine = [a for a in assignments if getattr(a, "node", None) is node]
        if not mine or len(mine) != len(assignments):
            return None            # rebound elsewhere in the scope
        return [acc.node for a in mine for acc in a.references
                if isinstance(acc.node, cst.Name)]

    def leave_Name(self, original_node: cst.Name, updated_node: cst.Name) -> cst.Name:
        new = self._ref_renames.get(original_node)
        return updated_node.with_changes(value=new) if new else updated_node

    def _moved_alias(self, alias: cst.ImportAlias) -> cst.ImportAlias:
        leaf = alias.name.value
        return _clean_last_comma(alias.with_changes(name=cst.Name(self.renames[leaf])))

    def leave_ImportFrom(self, original_node: cst.ImportFrom,
                         updated_node: cst.ImportFrom) -> cst.ImportFrom:
        movable = self._movable.get(original_node)
        if not movable or isinstance(updated_node.names, cst.ImportStar):
            return updated_node
        if all(a.name.value in movable for a in updated_node.names):
            names = [a.with_changes(name=cst.Name(self.renames[a.name.value]))
                     if a.name.value in movable else a for a in updated_node.names]
            self._count(movable)
            module = (updated_node.module if self.new_module == self.old_module
                      else _module_expr(self.new_module))
            return updated_node.with_changes(module=module, names=names)
        return updated_node

    def _count(self, leaves) -> None:
        for leaf in leaves:
            self.hits[leaf] = self.hits.get(leaf, 0) + 1
            self.sites += 1

    def leave_SimpleStatementLine(
        self, original: cst.SimpleStatementLine, updated: cst.SimpleStatementLine
    ) -> cst.SimpleStatementLine | cst.FlattenSentinel:
        if len(original.body) != 1 or not isinstance(original.body[0], cst.ImportFrom):
            return updated
        movable = self._movable.get(original.body[0])
        imp = updated.body[0]
        if (not movable or not isinstance(imp, cst.ImportFrom)
                or isinstance(imp.names, cst.ImportStar)
                or imp.module is None or _dotted(imp.module) != self.old_module):
            return updated          # nothing to do, or leave_ImportFrom already moved it all
        moved = [a for a in imp.names if a.name.value in movable]
        kept = [a for a in imp.names if a.name.value not in movable]
        if not moved or not kept:
            return updated
        self._count(movable)
        old_line = updated.with_changes(
            body=[imp.with_changes(names=[*kept[:-1], _clean_last_comma(kept[-1])],
                                   lpar=imp.lpar if len(kept) > 1 else None,
                                   rpar=imp.rpar if len(kept) > 1 else None)])
        new_line = cst.SimpleStatementLine(body=[cst.ImportFrom(
            module=_module_expr(self.new_module),
            names=[self._moved_alias(a).with_changes(comma=cst.MaybeSentinel.DEFAULT)
                   if i == len(moved) - 1 else self._moved_alias(a).with_changes(
                       comma=cst.Comma(whitespace_after=cst.SimpleWhitespace(" ")))
                   for i, a in enumerate(moved)])])
        return cst.FlattenSentinel([old_line, new_line])


def _finish_imports(module: cst.Module, needed: set[tuple[str, str | None]],
                    dropped: set[tuple[str, str | None, str | None]]) -> cst.Module:
    """Add the imports the rewrite needs ``(module, name-or-None)``, then drop the imports it
    left unused ``(module, name-or-None, asname)``."""
    if not needed and not dropped:
        return module
    context = CodemodContext()
    for name, obj in sorted(needed, key=lambda t: (t[0], t[1] or "")):
        AddImportsVisitor.add_needed_import(context, name, obj)
    for name, obj, asname in sorted(dropped, key=lambda t: (t[0], t[1] or "", t[2] or "")):
        RemoveImportsVisitor.remove_unused_import(context, name, obj, asname)
    if needed:
        module = AddImportsVisitor(context).transform_module(module)
    if dropped:
        module = RemoveImportsVisitor(context).transform_module(module)
    return module


def _move_renames(patterns: list[CodemodPattern]) -> dict[tuple[str, str], dict[str, str]]:
    """``{(old_module, new_module): {old_leaf: new_leaf}}`` for the import-move patterns."""
    groups: dict[tuple[str, str], dict[str, str]] = {}
    for p in patterns:
        if p.kind == "rewrite-import" or (p.kind == "rename-call" and p.scope == "python"):
            groups.setdefault((_module(p.old_qualname), _module(p.new_qualname)), {})[
                _leaf(p.old_qualname)] = _leaf(p.new_qualname)
    return groups


def apply_pattern(source: str, pattern: CodemodPattern, *,
                  lines: frozenset[int] | None = None) -> tuple[str, int]:
    """Apply one pattern to source. Returns (new_source, sites_changed).

    ``lines`` (python fixes) restricts the rewrite to nodes covering those source lines."""
    module = cst.parse_module(source)
    if pattern.kind == "rewrite-attr":
        # Attribute-path insertion (m.OLD_TAIL → m.NEW_TAIL); binding-aware.
        transformer = RewriteAttrCodemod(pattern.old_qualname, pattern.new_qualname,
                                         requires_import=pattern.requires_import, lines=lines)
        out = MetadataWrapper(module).visit(transformer)
        if transformer.sites:
            out = _finish_imports(out, transformer.needed_imports, set())
        return out.code, transformer.sites
    if pattern.kind == "replace-attr-path":
        transformer = ReplaceAttrPathCodemod(pattern.old_qualname, pattern.new_qualname,
                                             requires_import=pattern.requires_import,
                                             lines=lines)
        out = MetadataWrapper(module).visit(transformer)
        if transformer.sites:
            out = _finish_imports(out, transformer.needed_imports, transformer.dropped)
        return out.code, transformer.sites
    if pattern.kind == "rewrite-module":
        # Module-scoped: old_qualname/new_qualname are module names (not module.leaf).
        if lines is not None:
            located = _LocatedRewriteModule(pattern.old_qualname, pattern.new_qualname, lines)
            return MetadataWrapper(module).visit(located).code, located.sites
        transformer = RewriteModuleCodemod(pattern.old_qualname, pattern.new_qualname)
        return module.visit(transformer).code, transformer.sites
    if pattern.kind == "rewrite-import" or (
            pattern.kind == "rename-call" and pattern.scope == "python"):
        mover = MoveNamesCodemod(_module(pattern.old_qualname), _module(pattern.new_qualname),
                                 {_leaf(pattern.old_qualname): _leaf(pattern.new_qualname)},
                                 lines=lines)
        return MetadataWrapper(module).visit(mover).code, mover.sites

    # rename-call is binding-aware → run through a MetadataWrapper so the
    # ScopeProvider is resolved and only true bindings are rewritten.
    rename = RenameCallCodemod(pattern.old_qualname, pattern.new_qualname)
    new_module = MetadataWrapper(module).visit(rename)
    return new_module.code, rename.sites


def apply_patterns(
    source: str, patterns: list[CodemodPattern], *,
    lines_for: Callable[[CodemodPattern], frozenset[int] | None] | None = None,
    on_step: StepObserver = None,
) -> tuple[str, list[tuple[CodemodPattern, int]]]:
    """Apply a sequence of patterns. Returns (new_source, [(pattern, sites)…]).

    The import-move patterns (``rewrite-import``, python ``rename-call``) that share an old and
    a new module are applied together, so one ``from OLD import a, b, c`` statement becomes at
    most one statement per target module instead of one per name. ``lines_for(pattern)`` gives
    the source lines a pattern may touch (None: anywhere). ``on_step(recipe, before, after)``
    observes each pattern or move group (a ``MoveGroup``) that changes the text."""
    hits: list[tuple[CodemodPattern, int]] = []
    groups = _move_renames(patterns)
    done_groups: set[tuple[str, str]] = set()
    for pattern in patterns:
        key = (_module(pattern.old_qualname), _module(pattern.new_qualname))
        if _move_renames([pattern]):
            if key in done_groups:
                continue
            done_groups.add(key)
            members = [p for p in patterns if _move_renames([p]) and (
                _module(p.old_qualname), _module(p.new_qualname)) == key]
            lines: frozenset[int] | None = None
            if lines_for is not None:
                found = [lines_for(p) for p in members]
                lines = (frozenset().union(*[f for f in found if f is not None])
                         if any(f is not None for f in found) else None)
            mover = MoveNamesCodemod(key[0], key[1], groups[key], lines=lines)
            before = source
            source = MetadataWrapper(cst.parse_module(source)).visit(mover).code
            group_hits = []
            for member in members:
                n = mover.hits.get(_leaf(member.old_qualname), 0)
                if n:
                    hits.append((member, n))
                    group_hits.append((recipe_key(member), n))
            if source != before:
                _observe(on_step, MoveGroup(key[0], key[1], tuple(members), tuple(group_hits)),
                         before, source)
            continue
        before = source
        source, sites = apply_pattern(
            source, pattern, lines=lines_for(pattern) if lines_for else None)
        if sites:
            hits.append((pattern, sites))
        if source != before:
            _observe(on_step, pattern, before, source)
    return source, hits


def _observe(on_step: StepObserver, recipe, before: str, after: str) -> None:
    """Report one recipe's change to a step observer. A failing observer cannot change the
    walk: the plan then misses that step's chain and keeps the file's change whole."""
    if on_step is None:
        return
    try:
        on_step(recipe, before, after)
    except Exception:  # noqa: BLE001 - an observer cannot break the walk
        log.warning("step observer raised", exc_info=True)


class _FileSteps:
    """Buffers one file's step events; the walker hands them on only for a file it keeps."""

    def __init__(self, on_step: FileStepObserver, path: Path):
        self.sink, self.path = on_step, str(path)
        self.events: list[tuple] = []

    @property
    def hook(self) -> StepObserver:
        if self.sink is None:
            return None
        return lambda recipe, before, after: self.events.append((recipe, before, after))

    def flush(self) -> None:
        if self.sink is None:
            return
        for recipe, before, after in self.events:
            try:
                self.sink(recipe, self.path, before, after)
            except Exception:  # noqa: BLE001 - an observer cannot break the walk
                log.warning("step observer raised on %s", self.path, exc_info=True)
        self.events = []


def _emit(sink: ProgressSink, path: Path, hits: list[tuple] | None = None) -> None:
    """Report one scanned file to a progress sink, if there is one.

    A misbehaving observer must not take the walk down with it — a progress
    view is never worth failing a preview over.
    """
    if sink is None:
        return
    hits = hits or []
    try:
        sink(
            PreviewProgress(
                path=str(path),
                sites=sum(n for _, n in hits),
                recipes=tuple(recipe_key(item) for item, _ in hits),
            )
        )
    except Exception:  # noqa: BLE001 — an observer cannot break the walk
        log.warning("progress sink raised on %s", path, exc_info=True)


def _iter_py_files(root: Path):
    for path in sorted(root.rglob("*.py")):
        try:
            rel = path.relative_to(root)
            if any(
                part in _SKIP_DIRS or (part.startswith(".") and part != ".")
                for part in rel.parts
            ):
                continue
        except Exception:
            if any(
                part in _SKIP_DIRS or (part.startswith(".") and part != ".")
                for part in path.parts
            ):
                continue
        yield path


@dataclass(frozen=True, slots=True)
class _PendingRewrite:
    """One fully computed source rewrite, before any project file is replaced."""

    path: Path
    original: bytes
    replacement: bytes


@dataclass(frozen=True, slots=True)
class _PreparedRewrite:
    """Same-directory files that make one rewrite atomic and reversible."""

    rewrite: _PendingRewrite
    staged: Path
    rollback: Path


def _read_source(path: Path) -> tuple[bytes, str]:
    """Read once so the rollback copy is byte-identical to the original file."""
    original = path.read_bytes()
    return original, original.decode("utf-8")


def _write_transaction_file(path: Path, payload: bytes, *, kind: str, mode: int) -> Path:
    """Create and fsync a hidden same-directory transaction file."""
    fd, raw_path = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.pymolt-{kind}-",
        suffix=".tmp",
    )
    temp_path = Path(raw_path)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, mode)
    except BaseException:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise
    return temp_path


def _cleanup_transaction_files(prepared: list[_PreparedRewrite]) -> None:
    for item in prepared:
        for path in (item.staged, item.rollback):
            try:
                path.unlink()
            except OSError:
                pass


def _commit_rewrites(rewrites: list[_PendingRewrite]) -> None:
    """Atomically apply a set of rewrites, rolling the whole set back on failure.

    Every replacement and rollback file is validated and materialized beside its
    destination before the first source file is touched.  ``os.replace`` then
    makes each individual transition atomic.  A later failure restores every
    destination already replaced in this transaction.
    """
    if not rewrites:
        return

    prepared: list[_PreparedRewrite] = []
    try:
        # Prepare the complete transaction before replacing any project source.
        for rewrite in rewrites:
            if rewrite.path.is_symlink():
                raise OSError(f"refusing to replace symlink: {rewrite.path}")
            if rewrite.path.read_bytes() != rewrite.original:
                raise RuntimeError(
                    f"file changed while codemods were being computed: {rewrite.path}"
                )

            # A generated module must parse before it is allowed into the write set.
            cst.parse_module(rewrite.replacement.decode("utf-8"))
            mode = stat.S_IMODE(rewrite.path.stat().st_mode)
            rollback = _write_transaction_file(
                rewrite.path, rewrite.original, kind="rollback", mode=mode
            )
            try:
                staged = _write_transaction_file(
                    rewrite.path, rewrite.replacement, kind="new", mode=mode
                )
            except BaseException:
                try:
                    rollback.unlink()
                except OSError:
                    pass
                raise
            prepared.append(
                _PreparedRewrite(rewrite=rewrite, staged=staged, rollback=rollback)
            )

        replaced: list[_PreparedRewrite] = []
        try:
            for item in prepared:
                # Catch edits made after preparation but before this destination's commit.
                if item.rewrite.path.read_bytes() != item.rewrite.original:
                    raise RuntimeError(
                        f"file changed while codemods were being committed: {item.rewrite.path}"
                    )
                os.replace(item.staged, item.rewrite.path)
                replaced.append(item)
        except BaseException as exc:
            rollback_errors: list[str] = []
            for item in reversed(replaced):
                try:
                    os.replace(item.rollback, item.rewrite.path)
                except OSError as rollback_exc:
                    rollback_errors.append(f"{item.rewrite.path}: {rollback_exc}")
            if rollback_errors:
                raise RuntimeError(
                    "codemod transaction failed and rollback was incomplete: "
                    + "; ".join(rollback_errors)
                ) from exc
            raise
    finally:
        _cleanup_transaction_files(prepared)


def apply_to_repo(
    root: str | Path,
    patterns: list[CodemodPattern],
    *,
    write: bool = False,
) -> CodemodRunResult:
    """
    Apply patterns across every .py file under `root`.

    Dry-run by default (nothing written); pass write=True to persist. Files where
    no pattern matches are left untouched. Parse errors on a file are skipped
    (logged), never abort the run.
    """
    root = Path(root)
    result = CodemodRunResult(root=str(root), dry_run=not write)

    rewrites: list[_PendingRewrite] = []
    for path in _iter_py_files(root):
        result.files_scanned += 1
        try:
            original_bytes, original = _read_source(path)
        except (OSError, UnicodeDecodeError) as exc:
            log.warning("skip %s: %s", path, exc)
            continue
        try:
            new_source, hits = apply_patterns(original, patterns)
        except Exception as exc:  # libcst parse error → skip the file
            log.warning("skip %s (parse error): %s", path, exc)
            continue
        if not hits or new_source == original:
            continue

        total_sites = sum(n for _, n in hits)
        result.changes.append(
            FileChange(
                path=str(path),
                sites=total_sites,
                patterns=[p.summary() for p, _ in hits],
            )
        )
        result.patterns_applied += total_sites
        rewrites.append(
            _PendingRewrite(
                path=path,
                original=original_bytes,
                replacement=new_source.encode("utf-8"),
            )
        )

    if write:
        _commit_rewrites(rewrites)

    return result


def preview_repo(
    root: str | Path,
    patterns: list[CodemodPattern],
    *,
    on_file: ProgressSink = None,
    on_step: FileStepObserver = None,
) -> list[FilePreview]:
    """
    Compute per-file before/after for every file the patterns would change —
    without writing anything. This allows caller interfaces to render diff cards so the
    engineer can accept/reject each application individually.

    `on_file`, if given, is called once per file *scanned* — including files
    that matched nothing or could not be parsed — so a live view can show the
    real denominator instead of only the handful of hits. It is a pure
    observer: it can never change what the walk returns. So is `on_step(recipe, path, before,
    after)`, called for each recipe that changed a file the walk keeps, in application order.
    """
    root = Path(root)
    previews: list[FilePreview] = []
    for path in _iter_py_files(root):
        try:
            _original_bytes, original = _read_source(path)
        except (OSError, UnicodeDecodeError):
            _emit(on_file, path)
            continue
        steps = _FileSteps(on_step, path)
        try:
            new_source, hits = apply_patterns(original, patterns, on_step=steps.hook)
        except Exception as exc:  # libcst parse error → skip
            log.warning("skip %s (parse error): %s", path, exc)
            _emit(on_file, path)
            continue
        if not hits or new_source == original:
            _emit(on_file, path)
            continue
        previews.append(
            FilePreview(
                path=str(path),
                old_source=original,
                new_source=new_source,
                sites=sum(n for _, n in hits),
                patterns=[p for p, _ in hits],
            )
        )
        steps.flush()
        _emit(on_file, path, hits)
    return previews


def write_preview(preview: FilePreview) -> None:
    """Persist a single reviewed preview's new source to disk (on Accept)."""
    write_previews([preview])


def write_previews(previews: list[FilePreview]) -> None:
    """Persist an exact reviewed preview batch in one rollback boundary.

    Unlike re-running recipes, this writes precisely the source the engineer
    reviewed. Every file must still match ``old_source``; otherwise the entire
    batch is rejected before any destination is replaced.
    """
    rewrites: list[_PendingRewrite] = []
    for preview in previews:
        if preview.old_source == preview.new_source:
            continue
        path = Path(preview.path)
        original, source = _read_source(path)
        if source != preview.old_source:
            raise RuntimeError(
                f"file changed since the codemod preview was computed: {path}"
            )
        rewrites.append(
            _PendingRewrite(
                path=path,
                original=original,
                replacement=preview.new_source.encode("utf-8"),
            )
        )
    _commit_rewrites(rewrites)
# ─────────────────────────────────────────────────────────────────────────────
# Tier-2: rules (declarative match/rewrite + advisories)
# ─────────────────────────────────────────────────────────────────────────────


def _rule_summary(rule: CodemodRule) -> str:
    """A one-line summary of a rule for FileChange.patterns (mirrors
    CodemodPattern.summary())."""
    if rule.old_qualname and rule.new_qualname:
        target = f"{rule.old_qualname} → {rule.new_qualname}"
    elif rule.rewrite:
        target = f"{rule.match} → {rule.rewrite[0]}"
    else:
        target = rule.match
    return f"[{rule.kind or 'template'}] {target}  ({rule.confidence})"


def apply_rules(
    source: str,
    rules: list[CodemodRule],
    *,
    allow: Callable[[CodemodRule], bool] | None = None,
    blocked: list[BlockedRewrite] | None = None,
    on_step: StepObserver = None,
) -> tuple[str, list[tuple[CodemodRule, int]], list[CodemodRule], list[RuleAdvisory]]:
    """
    Apply `rules` to `source` sequentially — each rule is fed the PREVIOUS
    rule's output source (see `rules.apply_rule_detailed`).

    Returns (new_source, hits, touched, advisories):
      - hits: (rule, sites) for every rule whose rewrite was ADOPTED into the
        chained source
      - touched: every rule that either rewrote or raised an advisory, in rule
        order (for a FilePreview's info header — includes advisory-only rules)
      - advisories: every RuleAdvisory raised, regardless of `allow`

    `allow`, if given, gates whether a rule's rewrite is ADOPTED into the
    chained source — its advisories are still collected either way, and a
    blocked rewrite is simply discarded (the chain continues from the
    unmodified source). Used by `apply_rules_to_repo` to keep unverified rules
    detect-only when writing to disk. `allow=None` (the default; always used by
    `preview_rules_repo`) adopts every rewrite unconditionally. ``blocked``, when
    given, receives each rule whose rewrite matched but was not adopted, with the
    lines it would have changed. ``on_step(rule, before, after)`` observes each adopted rewrite
    that changed the text. Each advisory names its rule (``RuleAdvisory.rule``).
    """
    hits: list[tuple[CodemodRule, int]] = []
    touched: list[CodemodRule] = []
    advisories: list[RuleAdvisory] = []
    for rule in rules:
        app = apply_rule_detailed(source, rule)
        if app.sites or app.advisories:
            touched.append(rule)
        key = recipe_key(rule)
        advisories.extend(a if a.rule else a.model_copy(update={"rule": key})
                          for a in app.advisories)
        if app.sites and (allow is None or allow(rule)):
            hits.append((rule, app.sites))
            if app.new_source != source:
                _observe(on_step, rule, source, app.new_source)
            source = app.new_source
        elif app.sites and blocked is not None:
            blocked.append(BlockedRewrite(rule=rule, sites=app.sites,
                                          lines=_changed_lines(source, app.new_source)))
    return source, hits, touched, advisories


def _changed_lines(old: str, new: str) -> list[int]:
    """The 1-based lines of ``old`` that ``new`` changes (an insertion: the line after)."""
    old_lines = old.splitlines()
    out: list[int] = []
    matcher = difflib.SequenceMatcher(None, old_lines, new.splitlines(), autojunk=False)
    for tag, i1, i2, _j1, _j2 in matcher.get_opcodes():
        if tag != "equal":
            out.extend(range(i1 + 1, i2 + 1) if i2 > i1 else [i1 + 1])
    return sorted(set(out))


def apply_rules_to_repo(
    root: str | Path,
    rules: list[CodemodRule],
    *,
    write: bool = False,
) -> CodemodRunResult:
    """
    Apply rules across every .py file under `root`, sequentially chaining each
    rule's output source into the next.

    Dry-run by default (nothing written); pass write=True to persist. Auto-
    apply policy: when write=True, only confidence=="verified" rules may
    modify a file — heuristic (unverified) rules still run so their advisories
    surface, but their rewrite is never adopted/written. In dry-run every
    rule's rewrite is previewed regardless of confidence. `advisories_by_file`
    is populated for every file that raised >=1 advisory, even one with zero
    rewrites. Parse errors (or a malformed rule) on a file are skipped
    (logged), never abort the run.
    """
    root = Path(root)
    result = CodemodRunResult(root=str(root), dry_run=not write)
    allow = (lambda rule: rule.confidence == "verified") if write else None

    rewrites: list[_PendingRewrite] = []
    for path in _iter_py_files(root):
        result.files_scanned += 1
        try:
            original_bytes, original = _read_source(path)
        except (OSError, UnicodeDecodeError) as exc:
            log.warning("skip %s: %s", path, exc)
            continue
        try:
            new_source, hits, _touched, advisories = apply_rules(original, rules, allow=allow)
        except Exception as exc:  # malformed rule / libcst parse error → skip
            log.warning("skip %s (rule error): %s", path, exc)
            continue

        if advisories:
            result.advisories_by_file[str(path)] = advisories
        if not hits or new_source == original:
            continue

        total_sites = sum(n for _, n in hits)
        result.changes.append(
            FileChange(
                path=str(path),
                sites=total_sites,
                patterns=[_rule_summary(r) for r, _ in hits],
                advisories=advisories,
            )
        )
        result.patterns_applied += total_sites
        rewrites.append(
            _PendingRewrite(
                path=path,
                original=original_bytes,
                replacement=new_source.encode("utf-8"),
            )
        )

    if write:
        _commit_rewrites(rewrites)

    return result


def apply_mixed_to_repo(
    root: str | Path,
    rules: list[CodemodRule],
    patterns: list[CodemodPattern],
    *,
    write: bool = False,
) -> CodemodRunResult:
    """Apply Tier-2 rules then legacy patterns in one repository transaction.

    The two recipe sets belong to disjoint packages, but may still touch the
    same source file. Computing both passes in memory before one commit keeps a
    mixed server response just as atomic as a rules-only or patterns-only run.
    """
    root = Path(root)
    result = CodemodRunResult(root=str(root), dry_run=not write)
    allow = (lambda rule: rule.confidence == "verified") if write else None
    rewrites: list[_PendingRewrite] = []

    for path in _iter_py_files(root):
        result.files_scanned += 1
        try:
            original_bytes, original = _read_source(path)
        except (OSError, UnicodeDecodeError) as exc:
            log.warning("skip %s: %s", path, exc)
            continue

        current = original
        rule_hits: list[tuple[CodemodRule, int]] = []
        advisories: list[RuleAdvisory] = []
        try:
            current, rule_hits, _touched, advisories = apply_rules(
                current, rules, allow=allow
            )
        except Exception as exc:
            log.warning("skip rules for %s (rule error): %s", path, exc)

        pattern_hits: list[tuple[CodemodPattern, int]] = []
        try:
            current, pattern_hits = apply_patterns(current, patterns)
        except Exception as exc:
            log.warning("skip patterns for %s (parse error): %s", path, exc)

        if advisories:
            result.advisories_by_file[str(path)] = advisories
        if current == original or not (rule_hits or pattern_hits):
            continue

        sites = sum(count for _, count in rule_hits) + sum(
            count for _, count in pattern_hits
        )
        summaries = [_rule_summary(rule) for rule, _ in rule_hits]
        summaries.extend(pattern.summary() for pattern, _ in pattern_hits)
        result.changes.append(
            FileChange(
                path=str(path), sites=sites, patterns=summaries,
                advisories=advisories,
            )
        )
        result.patterns_applied += sites
        rewrites.append(
            _PendingRewrite(
                path=path,
                original=original_bytes,
                replacement=current.encode("utf-8"),
            )
        )

    if write:
        _commit_rewrites(rewrites)
    return result


def preview_mixed_repo(
    root: str | Path,
    rules: list[CodemodRule],
    patterns: list[CodemodPattern],
    *,
    on_file: ProgressSink = None,
    verified_only: bool = False,
    on_step: FileStepObserver = None,
) -> list[FilePreview]:
    """Preview the exact chained output that :func:`apply_mixed_to_repo` writes. ``on_step``
    observes each recipe's change to a kept file, as in :func:`preview_repo`."""
    root = Path(root)
    previews: list[FilePreview] = []
    allow = (lambda rule: rule.confidence == "verified") if verified_only else None
    for path in _iter_py_files(root):
        try:
            _original_bytes, original = _read_source(path)
        except (OSError, UnicodeDecodeError):
            _emit(on_file, path)
            continue
        blocked: list[BlockedRewrite] = []
        steps = _FileSteps(on_step, path)
        try:
            after_rules, rule_hits, touched, advisories = apply_rules(
                original, rules, allow=allow, blocked=blocked, on_step=steps.hook
            )
            new_source, pattern_hits = apply_patterns(after_rules, patterns,
                                                      on_step=steps.hook)
        except Exception as exc:
            log.warning("skip %s (mixed preview error): %s", path, exc)
            _emit(on_file, path)
            continue
        if not (touched or pattern_hits):
            _emit(on_file, path)
            continue
        previews.append(
            FilePreview(
                path=str(path),
                old_source=original,
                new_source=new_source,
                sites=sum(count for _, count in rule_hits)
                + sum(count for _, count in pattern_hits),
                patterns=[pattern for pattern, _ in pattern_hits],
                rules=touched,
                advisories=advisories,
                blocked=blocked,
            )
        )
        steps.flush()
        _emit(on_file, path, [*rule_hits, *pattern_hits])
    return previews


def preview_rules_repo(
    root: str | Path,
    rules: list[CodemodRule],
    *,
    on_file: ProgressSink = None,
    verified_only: bool = False,
    on_step: FileStepObserver = None,
) -> list[FilePreview]:
    """
    Compute per-file before/after for every file the rules would change OR
    advise on — without writing anything (confidence never gates a preview).

    A file with only advisories and no text change still yields a preview
    (old_source == new_source, sites=0, advisories set) so caller interfaces can render
    the manual-review card instead of silently dropping it.
    """
    root = Path(root)
    previews: list[FilePreview] = []
    allow = (lambda rule: rule.confidence == "verified") if verified_only else None
    for path in _iter_py_files(root):
        try:
            _original_bytes, original = _read_source(path)
        except (OSError, UnicodeDecodeError):
            _emit(on_file, path)
            continue
        blocked: list[BlockedRewrite] = []
        steps = _FileSteps(on_step, path)
        try:
            new_source, hits, touched, advisories = apply_rules(
                original, rules, allow=allow, blocked=blocked, on_step=steps.hook
            )
        except Exception as exc:  # malformed rule / libcst parse error → skip
            log.warning("skip %s (rule error): %s", path, exc)
            _emit(on_file, path)
            continue
        if not touched:
            _emit(on_file, path)
            continue
        previews.append(
            FilePreview(
                path=str(path),
                old_source=original,
                new_source=new_source,
                sites=sum(n for _, n in hits),
                rules=touched,
                advisories=advisories,
                blocked=blocked,
            )
        )
        steps.flush()
    return previews
