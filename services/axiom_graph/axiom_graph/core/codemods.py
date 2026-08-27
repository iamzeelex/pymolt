"""
axiom_graph/codemods.py

Codemod proposals and LibCST transformers — the actionable output of the
engine. A CodemodProposal says "replace old_qualname with new_qualname";
the transformers rewrite a user's source to do exactly that, preserving the
author's formatting (LibCST round-trips comments, whitespace, and layout).

Two shapes cover the common deprecation cases:

  - rewrite-import : same leaf, different module
        flask.helpers.safe_join → werkzeug.utils.safe_join
        call sites `safe_join(...)` are unchanged; only the import line moves.
  - rename-call    : different leaf
        old_api → new_api
        call sites and the import name both change.

apply_codemod(source, proposal) returns the rewritten source. It is a no-op
(returns the input unchanged) when the symbol isn't used — safe to run broadly.

Pure and offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import libcst as cst

# ─────────────────────────────────────────────────────────────────────────────
# Proposal model
# ─────────────────────────────────────────────────────────────────────────────


def _leaf(qualname: str) -> str:
    return qualname.rsplit(".", 1)[-1]


def _module(qualname: str) -> str:
    return qualname.rsplit(".", 1)[0] if "." in qualname else ""


@dataclass
class CodemodProposal:
    """A proposed replacement of one symbol by another."""

    old_qualname: str
    new_qualname: str
    confidence: str = "medium"  # "high" | "medium" | "low"
    evidence: list[str] = field(default_factory=list)

    @property
    def kind(self) -> str:
        """'rewrite-import' when only the module moved, else 'rename-call'."""
        if _leaf(self.old_qualname) == _leaf(self.new_qualname):
            return "rewrite-import"
        return "rename-call"

    def summary(self) -> str:
        arrow = f"{self.old_qualname} → {self.new_qualname}"
        return f"[{self.kind}] {arrow}  ({self.confidence})"

    def to_dict(self) -> dict:
        return {
            "old_qualname": self.old_qualname,
            "new_qualname": self.new_qualname,
            "kind": self.kind,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Transformers
# ─────────────────────────────────────────────────────────────────────────────


def _module_expr(dotted: str) -> cst.BaseExpression:
    """Build a Name/Attribute node for a dotted module path."""
    return cst.parse_expression(dotted)


class RewriteImportCodemod(cst.CSTTransformer):
    """
    Rewrite `from OLD_MODULE import LEAF [as a]` → `from NEW_MODULE import LEAF`.

    If the import line pulls in several names, the moved one is split out onto
    its own new `from NEW_MODULE import LEAF` line and removed from the original.
    Operates at the statement-line level so the split lands on separate lines
    (not semicolon-joined).
    """

    def __init__(self, old_module: str, new_module: str, leaf: str) -> None:
        self.old_module = old_module
        self.new_module = new_module
        self.leaf = leaf
        self.changed = False

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

        self.changed = True
        others = [a for a in imp.names if a.name.value != self.leaf]
        if not others:
            # Whole line moves: rewrite the module, keep the line (and its
            # trailing comment / formatting) intact.
            moved = imp.with_changes(
                module=_module_expr(self.new_module),
                names=[_clean_last_comma(a) for a in imp.names],
            )
            return updated.with_changes(body=[moved])
        # Split into two lines: original keeps the others, new line for the move.
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
    Rename a symbol at its call sites and in its import.

    Matches calls whose target is the bare `old_leaf` (e.g. imported via
    `from m import old_leaf`) or the dotted `old_module.old_leaf`, and rewrites
    the name to `new_leaf`. The import name is rewritten too.
    """

    def __init__(self, old_qualname: str, new_qualname: str) -> None:
        self.old_leaf = _leaf(old_qualname)
        self.new_leaf = _leaf(new_qualname)
        self.old_module = _module(old_qualname)
        self.new_module = _module(new_qualname)
        self.changed = False

    def leave_Call(self, original: cst.Call, updated: cst.Call) -> cst.BaseExpression:
        func = updated.func
        if isinstance(func, cst.Name) and func.value == self.old_leaf:
            self.changed = True
            return updated.with_changes(func=cst.Name(self.new_leaf))
        if isinstance(func, cst.Attribute) and func.attr.value == self.old_leaf:
            self.changed = True
            return updated.with_changes(func=func.with_changes(attr=cst.Name(self.new_leaf)))
        return updated

    def leave_ImportFrom(
        self, original: cst.ImportFrom, updated: cst.ImportFrom
    ) -> cst.ImportFrom:
        if updated.module is None or isinstance(updated.names, cst.ImportStar):
            return updated
        if self.old_module and _dotted(updated.module) != self.old_module:
            return updated
        new_names = []
        for alias in updated.names:
            if alias.name.value == self.old_leaf:
                self.changed = True
                alias = alias.with_changes(name=cst.Name(self.new_leaf))
            new_names.append(alias)
        module = updated.module
        if self.new_module and self.old_module and self.new_module != self.old_module:
            module = _module_expr(self.new_module)
        return updated.with_changes(names=new_names, module=module)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _dotted(node: cst.BaseExpression) -> str | None:
    if isinstance(node, cst.Name):
        return node.value
    if isinstance(node, cst.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr.value}" if base else node.attr.value
    return None


def _clean_last_comma(alias: cst.ImportAlias) -> cst.ImportAlias:
    """Drop a trailing comma so a rewritten single-name import is well-formed."""
    if isinstance(alias.comma, cst.Comma):
        return alias.with_changes(comma=cst.MaybeSentinel.DEFAULT)
    return alias


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


def apply_codemod(source: str, proposal: CodemodProposal) -> str:
    """
    Apply a codemod proposal to source, returning the rewritten source with the
    author's formatting preserved. Returns the input unchanged if the symbol is
    not present.
    """
    module = cst.parse_module(source)
    if proposal.kind == "rewrite-import":
        transformer: cst.CSTTransformer = RewriteImportCodemod(
            _module(proposal.old_qualname),
            _module(proposal.new_qualname),
            _leaf(proposal.old_qualname),
        )
    else:
        transformer = RenameCallCodemod(proposal.old_qualname, proposal.new_qualname)
    return module.visit(transformer).code
