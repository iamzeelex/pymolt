"""
axiom_graph/analyzers/comment_markers.py

Parse *implicit* deprecation markers — the human-written "use X instead" hints
that live in comments, docstrings, and warnings.warn() messages.

Where value_flow.py recovers the replacement from the *data flow* (high
confidence, but only when the function actually delegates), this module recovers
it from the *prose*: a deprecated function very often names its successor in
text even when the code doesn't forward to it (e.g. flask.total_seconds, which
tells you to use timedelta.total_seconds but reimplements the logic inline).

LibCST keeps comments as Comment nodes attached to the tree, so we can read the
lines a developer wrote above/inside a function and extract a dotted successor
name from them — alongside the docstring and any deprecation-warning string.

Pure and offline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import libcst as cst

from axiom_graph.analyzers.cfg import _find_function

# A dotted identifier, optionally wrapped in quotes/backticks or a Sphinx role.
_NAME = r"['\"`]?([A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*)['\"`]?"

# Ordered: first match wins. Each captures the replacement symbol.
_MARKER_PATTERNS = [
    re.compile(rf"use\s+:(?:meth|func|class|obj|attr):`~?{_NAME}`", re.IGNORECASE),
    re.compile(rf"use\s+{_NAME}\s+instead", re.IGNORECASE),
    re.compile(rf"replaced\s+(?:by|with)\s+{_NAME}", re.IGNORECASE),
    re.compile(rf"moved\s+to\s+{_NAME}", re.IGNORECASE),
    re.compile(rf"superseded\s+by\s+{_NAME}", re.IGNORECASE),
    re.compile(rf"use\s+{_NAME}", re.IGNORECASE),
]


@dataclass(frozen=True)
class ReplacementMarker:
    """A textual 'use X instead' hint extracted from prose."""

    target: str
    """Dotted successor name, e.g. 'werkzeug.utils.safe_join'."""
    source: str
    """Where it was found: 'comment' | 'docstring' | 'warning'."""
    raw_text: str
    """The snippet the marker was matched from (trimmed)."""


# ─────────────────────────────────────────────────────────────────────────────
# Text collection from the CST
# ─────────────────────────────────────────────────────────────────────────────


class _CommentCollector(cst.CSTVisitor):
    def __init__(self) -> None:
        self.comments: list[str] = []

    def visit_Comment(self, node: cst.Comment) -> None:
        self.comments.append(node.value.lstrip("#").strip())


def _empty_line_comments(lines) -> list[str]:
    return [
        line.comment.value.lstrip("#").strip()
        for line in lines
        if isinstance(line, cst.EmptyLine) and line.comment is not None
    ]


def _leading_and_body_comments(
    func: cst.FunctionDef, extra_leading: list[str] | None = None
) -> list[str]:
    """Comments on the lines just above the def, plus any inside its body."""
    # `extra_leading` carries comments LibCST attached to the module header
    # rather than the function (happens when the def is the module's first
    # statement) — resolved by the caller that has the module in hand.
    out: list[str] = list(extra_leading or [])
    out.extend(_empty_line_comments(func.leading_lines))
    body_collector = _CommentCollector()
    func.body.visit(body_collector)
    out.extend(body_collector.comments)
    return out


def _docstring(func: cst.FunctionDef) -> str | None:
    """The function's docstring text, if the first statement is a string."""
    body = func.body
    if not isinstance(body, cst.IndentedBlock) or not body.body:
        return None
    first = body.body[0]
    if isinstance(first, cst.SimpleStatementLine) and first.body:
        expr = first.body[0]
        if isinstance(expr, cst.Expr) and isinstance(
            expr.value, (cst.SimpleString, cst.ConcatenatedString)
        ):
            return expr.value.evaluated_value or None
    return None


def _warning_messages(func: cst.FunctionDef) -> list[str]:
    """String arguments of any warnings.warn(...) / warn(...) call in the body."""
    messages: list[str] = []

    class _WarnVisitor(cst.CSTVisitor):
        def visit_Call(self, node: cst.Call) -> None:
            func_name = node.func
            is_warn = (
                isinstance(func_name, cst.Name) and func_name.value == "warn"
            ) or (
                isinstance(func_name, cst.Attribute)
                and func_name.attr.value == "warn"
            )
            if not is_warn:
                return
            for arg in node.args:
                val = arg.value
                if isinstance(val, (cst.SimpleString, cst.ConcatenatedString)):
                    try:
                        text = val.evaluated_value
                    except Exception:
                        text = None
                    if text:
                        messages.append(text)

    func.body.visit(_WarnVisitor())
    return messages


# ─────────────────────────────────────────────────────────────────────────────
# Marker extraction
# ─────────────────────────────────────────────────────────────────────────────

# Common words that follow "use …" in prose but are not successor symbols.
# Without this the weakest `use {NAME}` pattern matches "use this function",
# "use it", "use the new API" etc. and emits garbage targets.
_STOPWORDS = frozenset({
    "this", "it", "the", "a", "an", "that", "these", "those", "them", "they",
    "your", "our", "my", "his", "her", "its", "their", "one", "any", "either",
    "both", "such", "same", "other", "another", "instead", "rather", "only",
})


def _match_marker(text: str) -> str | None:
    for pattern in _MARKER_PATTERNS:
        m = pattern.search(text)
        if m:
            candidate = m.group(1)
            # A bare english word ("this", "it") is prose, not a symbol; a
            # dotted name ("other.total_seconds") is structurally a symbol.
            if "." not in candidate and candidate.lower() in _STOPWORDS:
                continue
            return candidate
    return None


def extract_replacement_marker(
    func: cst.FunctionDef, extra_leading: list[str] | None = None
) -> ReplacementMarker | None:
    """
    Extract a 'use X instead' replacement target from a function's prose.

    Priority: deprecation warning > docstring > comments (the warning is the
    most authoritative, the comment the most informal). Returns None if no
    successor name is found. `extra_leading` lets the caller supply leading
    comments LibCST attached to the module header (see extract_from_source).
    """
    self_name = func.name.value

    # 1. warnings.warn messages — the most authoritative.
    for msg in _warning_messages(func):
        target = _match_marker(msg)
        if target and target != self_name:
            return ReplacementMarker(target=target, source="warning", raw_text=msg.strip())

    # 2. docstring.
    doc = _docstring(func)
    if doc:
        target = _match_marker(doc)
        if target and target != self_name:
            snippet = _first_matching_line(doc, target)
            return ReplacementMarker(target=target, source="docstring", raw_text=snippet)

    # 3. comments (leading + body).
    for comment in _leading_and_body_comments(func, extra_leading):
        target = _match_marker(comment)
        if target and target != self_name:
            return ReplacementMarker(target=target, source="comment", raw_text=comment)

    return None


def _first_matching_line(text: str, target: str) -> str:
    """The first line of `text` mentioning the bare or dotted target name."""
    leaf = target.split(".")[-1]
    for line in text.splitlines():
        if target in line or leaf in line:
            return line.strip()
    return text.strip().splitlines()[0] if text.strip() else target


def extract_from_source(
    source: str, func_name: str | None = None
) -> ReplacementMarker | None:
    """Parse source, find the function, and extract its replacement marker."""
    module = cst.parse_module(source)
    func = _find_function(module, func_name)
    if func is None:
        what = f"function {func_name!r}" if func_name else "any function"
        raise ValueError(f"Could not find {what} in source")
    # When the def is the module's first statement, LibCST attaches the
    # comment lines above it to the module header, not func.leading_lines.
    extra_leading: list[str] = []
    if module.body and module.body[0] is func:
        extra_leading = _empty_line_comments(module.header)
    return extract_replacement_marker(func, extra_leading=extra_leading)
