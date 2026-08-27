"""
axiom_graph/analyzers/fingerprint.py

Semantic fingerprint of a function via a custom LibCST visitor.

We hash the *structure* of a function — the sequence of meaningful syntactic
nodes plus their token values (names, literals, operators) — while ignoring
whitespace, line breaks, redundant parentheses, and trailing commas. Comments
are captured into a *separate* hash so we can tell three kinds of change apart:

  - formatting-only : logic_hash equal, comment_hash equal  → no real change
  - comment-only    : logic_hash equal, comment_hash differs → docs/markers moved
  - logic changed   : logic_hash differs                     → an operator/call
                                                               /literal changed

This is the precision tool behind Layer 1: Griffe says "the signature is the
same"; the fingerprint says whether the *body* actually changed or was merely
reformatted — so we don't waste Layer-2 analysis on no-op diffs.

Pure and offline.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import libcst as cst

from axiom_graph.analyzers.cfg import _find_function

# Whitespace / punctuation / comment leaves that carry no logic. Returning
# False on these in the logic visitor means we never descend into them, so the
# comments they contain are excluded from the logic stream.
_SKIP_TYPES = (
    cst.BaseParenthesizableWhitespace,  # SimpleWhitespace, ParenthesizedWhitespace
    cst.EmptyLine,
    cst.TrailingWhitespace,
    cst.Newline,
    cst.Comment,
    cst.LeftParen,
    cst.RightParen,
    cst.LeftSquareBracket,
    cst.RightSquareBracket,
    cst.LeftCurlyBrace,
    cst.RightCurlyBrace,
    cst.Comma,
    cst.Dot,
    cst.Colon,
    cst.Semicolon,
)


# ─────────────────────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FunctionFingerprint:
    """Structural + comment hashes for one function."""

    func_name: str
    logic_hash: str
    """sha256 over the structural token stream (whitespace/comment-insensitive)."""
    comment_hash: str
    """sha256 over the ordered comment texts (logic-insensitive)."""

    @property
    def full_hash(self) -> str:
        return hashlib.sha256(
            f"{self.logic_hash}:{self.comment_hash}".encode()
        ).hexdigest()


@dataclass(frozen=True)
class FingerprintDiff:
    """Classification of the change between two fingerprints of one function."""

    logic_changed: bool
    comments_changed: bool

    @property
    def classification(self) -> str:
        if self.logic_changed:
            return "logic-changed"
        if self.comments_changed:
            return "comment-only"
        return "formatting-only"


# ─────────────────────────────────────────────────────────────────────────────
# Visitors
# ─────────────────────────────────────────────────────────────────────────────


class _LogicTokenVisitor(cst.CSTVisitor):
    """Emit a canonical token stream of meaningful nodes, skipping formatting."""

    def __init__(self) -> None:
        self.tokens: list[str] = []

    def on_visit(self, node: cst.CSTNode) -> bool:
        if isinstance(node, _SKIP_TYPES):
            return False  # don't emit, don't descend (drops nested comments)
        if isinstance(node, cst.Name):
            self.tokens.append(f"N:{node.value}")
            return False
        if isinstance(node, (cst.Integer, cst.Float, cst.Imaginary)):
            self.tokens.append(f"#:{node.value}")
            return False
        if isinstance(node, cst.SimpleString):
            self.tokens.append(f"S:{node.value}")
            return False
        # A structural node: record its type and descend into its children.
        self.tokens.append(type(node).__name__)
        return True


class _CommentVisitor(cst.CSTVisitor):
    """Collect every comment's normalized text in source order."""

    def __init__(self) -> None:
        self.comments: list[str] = []

    def visit_Comment(self, node: cst.Comment) -> None:
        self.comments.append(node.value.lstrip("#").strip())


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


def fingerprint_function(func: cst.FunctionDef) -> FunctionFingerprint:
    """Compute the structural + comment fingerprint of a FunctionDef."""
    # Fingerprint the body only (the signature is Griffe's job); this makes the
    # hash reflect *implementation* change, not parameter/return annotations.
    body = func.body

    logic_v = _LogicTokenVisitor()
    body.visit(logic_v)
    logic_hash = hashlib.sha256("|".join(logic_v.tokens).encode()).hexdigest()

    comment_v = _CommentVisitor()
    func.visit(comment_v)  # whole def: leading + inline + body comments
    comment_hash = hashlib.sha256("\n".join(comment_v.comments).encode()).hexdigest()

    return FunctionFingerprint(
        func_name=func.name.value,
        logic_hash=logic_hash,
        comment_hash=comment_hash,
    )


def fingerprint_source(source: str, func_name: str | None = None) -> FunctionFingerprint:
    """Parse source, find the function, and fingerprint it."""
    module = cst.parse_module(source)
    func = _find_function(module, func_name)
    if func is None:
        what = f"function {func_name!r}" if func_name else "any function"
        raise ValueError(f"Could not find {what} in source")
    return fingerprint_function(func)


def classify_change(
    old: FunctionFingerprint, new: FunctionFingerprint
) -> FingerprintDiff:
    """Compare two fingerprints of the same function."""
    return FingerprintDiff(
        logic_changed=old.logic_hash != new.logic_hash,
        comments_changed=old.comment_hash != new.comment_hash,
    )
