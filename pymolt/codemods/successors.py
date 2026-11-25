"""Successor codemods: the rewrites a proof from the library's own code licenses.

HOST side (LibCST). ``pymolt.delta.provider`` makes these patterns only from facts whose
``succession`` holds a proof (a forwarding body, an alias, a twin, a registered rename helper)
that transfers to the target; prose never makes one. Three kinds:

- ``rename-keyword`` (``params``: old -> new): ``old=`` becomes ``new=`` in a call of the callee,
  in a ``**{"old": ...}`` literal passed to it, and in ``functools.partial(callee, old=...)``.
  The call is left alone when it already passes ``new``, or passes a ``**mapping`` that is no
  dict literal (the old key may be in it);
- ``rename-member``: ``.old`` becomes ``.new``, a method call or an attribute read;
- ``forward-template`` (``template``): the call is rebuilt from the old callable's forwarding
  body: the site's arguments bound to the old signature, substituted into the forward, with the
  old defaults the target changed written out and the conditions the site's arguments decide
  honoured (``pl.count()`` -> ``pl.len().alias("count")``).

Which calls: a callee reached through an import binding (LibCST's ``QualifiedNameProvider`` and
``ScopeProvider``: never a shadowed name, never a binding made in an ``except ImportError``
fallback), or a member call on a receiver, only at the statements the pattern names
(``sites``: anchors, re-resolved on the text at hand, so a re-based step re-checks them) where
the baseline recorded receivers the proof covers. Strings, ``getattr`` names and attribute
stores are never touched: only call keywords and attribute reads are. Every call is checked
against the old and the new signatures (``_binds_same``): an argument that would bind to another
parameter, or a parameter left at a default the target changed, leaves the site to the agent.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Mapping
from typing import Any

import libcst as cst
from libcst.metadata import (
    ExpressionContext,
    ExpressionContextProvider,
    MetadataWrapper,
    ParentNodeProvider,
    PositionProvider,
    QualifiedNameProvider,
    QualifiedNameSource,
    ScopeProvider,
)
from libcst.metadata.scope_provider import ImportAssignment

_POSITIONAL = ("POSITIONAL_ONLY", "POSITIONAL_OR_KEYWORD")
_IMPORT_ERRORS = {"ImportError", "ModuleNotFoundError"}
KINDS = ("rename-keyword", "rename-member", "forward-template")


# ── where a pattern may rewrite ──────────────────────────────────────────────────────────


def site_spans(text: str, sites: Iterable[Any] | None, path: str | None
               ) -> list[tuple[int, int]] | None:
    """The line spans of the statements ``sites`` (anchors) name in ``text``; None when the
    pattern names no sites (it rewrites wherever its binding holds). Anchors of another file,
    and anchors that no longer resolve, give no span."""
    if sites is None:
        return None
    from pymolt.anchors import Anchor, StatementIndex

    try:
        index = StatementIndex.build(text)
    except Exception:  # noqa: BLE001 - no index, no site
        return []
    spans: list[tuple[int, int]] = []
    for raw in sites:
        anchor = raw if isinstance(raw, Anchor) else Anchor.model_validate(raw)
        if path is not None and anchor.file != path:
            continue
        found = index.resolve(anchor)
        if found.ok and found.statement is not None:
            # the statement's own lines: a compound statement's header, never its body
            spans.append((found.statement.first, found.statement.last))
    return spans


def _in_spans(spans: list[tuple[int, int]] | None, start: int, end: int) -> bool:
    return spans is None or any(a <= start and end <= b for a, b in spans)


# ── signatures ───────────────────────────────────────────────────────────────────────────


def _params(shape: list | None, member: bool) -> list[list]:
    """A signature without its receiver: a method read on its class shows ``self`` (a
    classmethod's ``cls`` is already bound there)."""
    params = [list(p) for p in shape or ()]
    if member and params and params[0][1] in _POSITIONAL and params[0][0] in ("self", "cls"):
        params = params[1:]
    return params


def _positional_name(params: list[list], i: int) -> str | None:
    """The parameter the ``i``-th positional argument binds to (a ``*args`` takes the rest)."""
    pos = [p for p in params if p[1] in _POSITIONAL]
    if i < len(pos):
        return pos[i][0]
    star = next((p for p in params if p[1] == "VAR_POSITIONAL"), None)
    return star[0] if star else None


def _norm_default(value: Any) -> Any:
    """A default's repr as compared: only an object address (`` at 0x...``) is no part of it;
    ``<class 'float'>`` and ``<class 'int'>`` differ."""
    return None if value is None else re.sub(r" at 0x[0-9a-fA-F]+", "", str(value))


def binds_same(npos: int, keywords: Iterable[str], old: list | None, new: list | None,
               member: bool, *, ignore: Iterable[str] = ()) -> str | None:
    """Why a call with ``npos`` positional arguments and these keyword names would not behave
    alike against two signatures (an argument binding to another parameter, a keyword one side
    does not take, a parameter left at a default that differs); None when it binds alike."""
    if old is None or new is None:
        return "a signature is unknown"
    a, b = _params(old, member), _params(new, member)
    for i in range(npos):
        na, nb = _positional_name(a, i), _positional_name(b, i)
        if na is None or nb is None or na != nb:
            return f"its positional argument {i + 1} binds to {na} and then to {nb}"
    names_a = {p[0]: p for p in a}
    names_b = {p[0]: p for p in b}
    kw = list(keywords)
    for k in kw:
        pa, pb = names_a.get(k), names_b.get(k)
        if pa is None or pb is None or pa[1] not in (*_POSITIONAL, "KEYWORD_ONLY") or \
                pb[1] not in (*_POSITIONAL, "KEYWORD_ONLY"):
            return f"its keyword {k} is not a parameter of both"
    bound = {_positional_name(a, i) for i in range(npos)} | set(kw) | set(ignore)
    for name, kind, default in a:
        if name in bound or kind in ("VAR_POSITIONAL", "VAR_KEYWORD"):
            continue
        other = names_b.get(name)
        if other is None:
            return f"it leaves {name} at its default, and the new signature has no {name}"
        if _norm_default(default) != _norm_default(other[2]):
            return f"it leaves {name} at its default, which changes ({default} -> {other[2]})"
    return None


def site_blocked(npos: int, keywords: Iterable[str], shape: list | None, member: bool,
                 blocked: Iterable[Mapping[str, Any]],
                 new_shape: list | None = None) -> str | None:
    """Why another changed parameter of the same callable concerns this call: a changed
    default it leaves alone, or a removed, renamed, deprecated or re-kinded parameter it
    passes (a keyword-only move matters only when passed by position)."""
    params = _params(shape, member)
    positional = {_positional_name(params, i) for i in range(npos)}
    kws = set(keywords)
    takes_keyword = {p[0] for p in _params(new_shape, member)
                     if p[1] in ("POSITIONAL_OR_KEYWORD", "KEYWORD_ONLY")}
    for b in blocked:
        p, kind = b.get("param"), b.get("kind")
        passed = p in positional or p in kws
        if kind == "DEFAULT" and not passed:
            return f"it leaves {p} at its default, which changes"
        if kind == "SIGNATURE" and passed:
            if p not in positional and p in takes_keyword:
                continue                  # passed by keyword, and still taken by keyword
            return f"it passes {p}, whose signature changes"
        if kind == "DEPRECATED" and passed:
            return f"it passes {p}, which is deprecated"
    return None


# ── bindings ─────────────────────────────────────────────────────────────────────────────


def _head(node: cst.BaseExpression) -> cst.Name | None:
    while isinstance(node, cst.Attribute):
        node = node.value
    return node if isinstance(node, cst.Name) else None


class _Bound(cst.CSTTransformer):
    METADATA_DEPENDENCIES = (QualifiedNameProvider, ScopeProvider, ParentNodeProvider,
                             PositionProvider, ExpressionContextProvider)

    def _import_bound(self, func: cst.BaseExpression, names: set[str]) -> bool:
        """``func`` names one of ``names`` through an import binding, and only through one: no
        local definition shadows it and the import is no ``except ImportError`` fallback."""
        try:
            qnames = self.get_metadata(QualifiedNameProvider, func)
        except KeyError:
            return False
        if not qnames or any(q.source != QualifiedNameSource.IMPORT for q in qnames):
            return False
        if not all(q.name in names for q in qnames):     # a binding on every path, not some
            return False
        head = _head(func)
        if head is None:
            return False
        try:
            scope = self.get_metadata(ScopeProvider, head)
            assignments = list(scope[head.value]) if scope is not None else []
        except KeyError:
            return False
        if not assignments or not all(isinstance(a, ImportAssignment) for a in assignments):
            return False
        return not any(self._in_import_fallback(a.node) for a in assignments)

    def _in_import_fallback(self, node: cst.CSTNode) -> bool:
        cur: cst.CSTNode | None = node
        while cur is not None:
            try:
                parent = self.get_metadata(ParentNodeProvider, cur)
            except KeyError:
                return False
            if isinstance(parent, cst.ExceptHandler):
                return True
            if isinstance(parent, cst.Try) and cur is not parent.body:
                return True
            if isinstance(parent, cst.Try) and any(_catches_import_error(h)
                                                   for h in parent.handlers):
                return True
            cur = parent
        return False

    def _span(self, node: cst.CSTNode) -> tuple[int, int]:
        try:
            pos = self.get_metadata(PositionProvider, node)
        except KeyError:
            return (0, 0)
        return pos.start.line, pos.end.line

    def visit_Module(self, node: cst.Module) -> None:
        member = getattr(self, "member", None)
        self._occurrences = []
        if member:
            counter = _AttrCounter(member)
            node.visit(counter)
            self._occurrences = counter.nodes

    def _only_one(self, node: cst.CSTNode) -> bool:
        """The member occurs once in the statement holding ``node`` (its own lines): the
        receiver the baseline recorded there is this call's."""
        start, end = self._span(node)
        span = next(((a, b) for a, b in self.spans or () if a <= start and end <= b), None)
        if span is None:
            return False
        here = [n for n in self._occurrences
                if span[0] <= self._span(n)[0] and self._span(n)[1] <= span[1]]
        if len(here) != 1:
            self.skipped.append(f"its statement holds .{self.member} {len(here)} times")
            return False
        return True


def _catches_import_error(handler: cst.ExceptHandler) -> bool:
    names: set[str] = set()
    t = handler.type
    items = t.elements if isinstance(t, cst.Tuple) else [t] if t is not None else []
    for item in items:
        value = getattr(item, "value", item)
        if isinstance(value, cst.Name):
            names.add(value.value)
        elif isinstance(value, cst.Attribute):
            names.add(value.attr.value)
    return bool(names & _IMPORT_ERRORS) or t is None


def _call_args(call: cst.Call) -> tuple[int, list[str], bool]:
    """``(positional count, keyword names, opaque)``: opaque when the call unpacks a
    ``*sequence`` or a ``**mapping`` that is no dict literal."""
    npos, kws, opaque = 0, [], False
    for arg in call.args:
        if arg.star == "*":
            opaque = True
        elif arg.star == "**":
            if isinstance(arg.value, cst.Dict):
                for el in arg.value.elements:
                    key = getattr(el, "key", None)
                    if isinstance(key, cst.SimpleString):
                        kws.append(key.evaluated_value)
                    else:
                        opaque = True
            else:
                opaque = True
        elif arg.keyword is not None:
            kws.append(arg.keyword.value)
        else:
            npos += 1
    return npos, kws, opaque


# ── rename-keyword ───────────────────────────────────────────────────────────────────────


class RenameKeywordCodemod(_Bound):
    """``old=`` -> ``new=`` at calls of the callee (see the module docstring)."""

    def __init__(self, names: Iterable[str], params: Mapping[str, str], *,
                 member: str | None = None, spans: list[tuple[int, int]] | None = None,
                 old_shape: list | None = None, blocked: Iterable[Mapping] = (),
                 new_shape: list | None = None, guards: Mapping[str, str] | None = None
                 ) -> None:
        super().__init__()
        self.new_shape = new_shape
        self.guards = dict(guards or {})
        self.names = set(names)
        self.params = dict(params)
        self.member = member
        self.spans = spans
        self.old_shape = old_shape
        self.blocked = list(blocked)
        self.sites = 0
        self.skipped: list[str] = []

    def _none_by_default(self, name: str) -> bool:
        """The new parameter defaults to None on both sides: a None the old keyword skipped and
        a None passed to the new one bind alike."""
        shapes = [sh for sh in (self.old_shape, self.new_shape) if sh]
        found = [p for sh in shapes for p in sh if p and p[0] == name]
        return bool(shapes) and len(found) == len(shapes) and \
            all(len(p) > 2 and str(p[2]) == "None" for p in found)

    def _targets(self, call: cst.Call) -> bool:
        func = call.func
        if self.member is not None:
            if not (isinstance(func, cst.Attribute) and func.attr.value == self.member):
                return False
            return self.spans is not None and _in_spans(self.spans, *self._span(call)) and \
                self._only_one(func)
        return self._import_bound(func, self.names) and _in_spans(self.spans,
                                                                  *self._span(call))

    def _partial_target(self, call: cst.Call) -> bool:
        if not call.args or call.args[0].keyword is not None or call.args[0].star:
            return False
        try:
            qnames = self.get_metadata(QualifiedNameProvider, call.func)
        except KeyError:
            return False
        if not any(q.name == "functools.partial" and q.source == QualifiedNameSource.IMPORT
                   for q in qnames):
            return False
        return self.member is None and self._import_bound(call.args[0].value, self.names)

    def leave_Call(self, original: cst.Call, updated: cst.Call) -> cst.BaseExpression:
        partial = self._partial_target(original)
        if not (partial or self._targets(original)):
            return updated
        npos, kws, opaque = _call_args(original)
        if partial:
            npos -= 1
        olds = [o for o in self.params if o in kws]
        if not olds:
            return updated
        if opaque:
            self.skipped.append("it unpacks arguments pymolt cannot see")
            return updated
        if any(self.params[o] in kws for o in olds):
            self.skipped.append("it passes the new keyword already")
            return updated
        why = site_blocked(npos, kws, self.old_shape, self.member is not None, self.blocked,
                           self.new_shape)
        if why is not None:
            self.skipped.append(why)
            return updated
        new_params = _params(self.new_shape, self.member is not None)
        by_position = {_positional_name(new_params, i) for i in range(npos)}
        clash = [self.params[o] for o in olds if self.params[o] in by_position]
        if clash:
            self.skipped.append(f"it passes {clash[0]} by position already")
            return updated
        for o in olds:
            if self.guards.get(o) == "not_none" and not _never_none(
                    _keyword_value(original, o)) and not self._none_by_default(self.params[o]):
                # the old body aliases only a value other than None: a value pymolt cannot
                # read could be None, and then the old keyword did nothing (the new one kept
                # its default, which a None passed to it now replaces)
                self.skipped.append(f"it passes {o} a value that may be None")
                return updated
        args = []
        for arg in updated.args:
            if arg.keyword is not None and arg.keyword.value in self.params:
                arg = arg.with_changes(keyword=cst.Name(self.params[arg.keyword.value]))
            elif arg.star == "**" and isinstance(arg.value, cst.Dict):
                elements = []
                for el in arg.value.elements:
                    key = getattr(el, "key", None)
                    if isinstance(key, cst.SimpleString) and key.evaluated_value in self.params:
                        quote = key.value[0] if key.value[0] in "'\"" else '"'
                        el = el.with_changes(key=cst.SimpleString(
                            f"{quote}{self.params[key.evaluated_value]}{quote}"))
                    elements.append(el)
                arg = arg.with_changes(value=arg.value.with_changes(elements=elements))
            args.append(arg)
        self.sites += 1
        return updated.with_changes(args=args)


def _keyword_value(call: cst.Call, name: str) -> cst.BaseExpression | None:
    for arg in call.args:
        if arg.keyword is not None and arg.keyword.value == name:
            return arg.value
        if arg.star == "**" and isinstance(arg.value, cst.Dict):
            for el in arg.value.elements:
                key = getattr(el, "key", None)
                if isinstance(key, cst.SimpleString) and key.evaluated_value == name:
                    return getattr(el, "value", None)
    return None


def _non_none_literal(node: cst.BaseExpression | None) -> bool:
    if isinstance(node, (cst.SimpleString, cst.ConcatenatedString, cst.Integer, cst.Float,
                         cst.Imaginary)):
        return True
    if isinstance(node, cst.UnaryOperation) and isinstance(node.expression,
                                                           (cst.Integer, cst.Float)):
        return True
    return isinstance(node, cst.Name) and node.value in ("True", "False")


def _never_none(node: cst.BaseExpression | None) -> bool:
    """A value that cannot be None: a literal other than None, a display (list, dict, tuple,
    set, comprehension), an f-string, or a ``"..." % x`` format (``str.__mod__`` returns a
    string or raises)."""
    if _non_none_literal(node):
        return True
    if isinstance(node, (cst.FormattedString, cst.List, cst.Dict, cst.Tuple, cst.Set,
                         cst.ListComp, cst.DictComp, cst.SetComp, cst.Lambda)):
        return True
    return isinstance(node, cst.BinaryOperation) and isinstance(node.operator, cst.Modulo) and \
        isinstance(node.left, (cst.SimpleString, cst.ConcatenatedString, cst.FormattedString))


def _simple(node: cst.BaseExpression | None) -> bool:
    """A name or a literal: evaluating it again, later or not at all changes nothing."""
    return isinstance(node, cst.Name) or _non_none_literal(node) or (
        isinstance(node, cst.Name) and node.value == "None")


# ── rename-member ────────────────────────────────────────────────────────────────────────


class RenameMemberCodemod(_Bound):
    """``.old`` -> ``.new`` (a call or an attribute read) in the pattern's statements, where the
    statement holds that attribute exactly once. With ``call_only`` the attribute must be
    called; ``shapes`` (old, new) check the call binds alike (``binds_same``)."""

    def __init__(self, old: str, new: str, spans: list[tuple[int, int]] | None, *,
                 call_only: bool = True,
                 shapes: tuple[list | None, list | None, list | None] | None = None) -> None:
        super().__init__()
        self.old, self.new = old, new
        self.spans = spans or []
        self.call_only = call_only
        self.shapes = shapes
        self.sites = 0
        self.skipped: list[str] = []
        self._count: dict[tuple[int, int], int] = {}
        self._ok: set[int] = set()

    def visit_Module(self, node: cst.Module) -> None:
        counter = _AttrCounter(self.old)
        node.visit(counter)
        self._occurrences = counter.nodes

    def visit_Call(self, node: cst.Call) -> None:
        func = node.func
        if isinstance(func, cst.Attribute) and func.attr.value == self.old:
            npos, kws, opaque = _call_args(node)
            if opaque:
                self.skipped.append("it unpacks arguments pymolt cannot see")
                return
            if self.shapes is not None:
                # the rename does what the forward did (old member vs its callee), and the callee
                # does in the target what it did there
                old, callee_old, callee_new = self.shapes
                why = binds_same(npos, kws, old, callee_old, True) or \
                    binds_same(npos, kws, callee_old, callee_new, True)
                if why is not None:
                    self.skipped.append(why)
                    return
            self._ok.add(id(func))

    def leave_Attribute(self, original: cst.Attribute, updated: cst.Attribute
                        ) -> cst.BaseExpression:
        if original.attr.value != self.old:
            return updated
        start, end = self._span(original)
        span = next(((a, b) for a, b in self.spans if a <= start and end <= b), None)
        if span is None:
            return updated
        try:
            ctx = self.get_metadata(ExpressionContextProvider, original)
        except KeyError:
            ctx = None
        if ctx != ExpressionContext.LOAD:
            return updated
        same_span = [n for n in self._occurrences if span[0] <= self._span(n)[0]
                     and self._span(n)[1] <= span[1]]
        if len(same_span) != 1:
            self.skipped.append(f"its statement holds .{self.old} {len(same_span)} times")
            return updated
        if self.call_only and id(original) not in self._ok:
            return updated
        self.sites += 1
        return updated.with_changes(attr=cst.Name(self.new))


class _AttrCounter(cst.CSTVisitor):
    def __init__(self, attr: str) -> None:
        self.attr = attr
        self.nodes: list[cst.Attribute] = []

    def visit_Attribute(self, node: cst.Attribute) -> None:
        if node.attr.value == self.attr:
            self.nodes.append(node)


# ── forward-template ─────────────────────────────────────────────────────────────────────


def _literal(text: Any) -> cst.BaseExpression | None:
    """A default's repr as code, when it is a literal (``'row_nr'``, ``0``, ``None``)."""
    if text is None:
        return None
    try:
        value = ast.literal_eval(str(text))
    except (ValueError, SyntaxError):
        return None
    if not isinstance(value, (str, int, float, bool, type(None))):
        return None
    return cst.parse_expression(repr(value))


class ForwardTemplateCodemod(_Bound):
    """The call rebuilt from a forwarding body (see the module docstring)."""

    def __init__(self, names: Iterable[str], template: Mapping[str, Any], *,
                 member: str | None = None, spans: list[tuple[int, int]] | None = None
                 ) -> None:
        super().__init__()
        self.names = set(names)
        self.t = dict(template)
        self.member = member
        self.spans = spans
        self.sites = 0
        self.skipped: list[str] = []

    def _targets(self, call: cst.Call) -> bool:
        func = call.func
        if self.member is not None:
            return isinstance(func, cst.Attribute) and func.attr.value == self.member and \
                self.spans is not None and _in_spans(self.spans, *self._span(call)) and \
                self._only_one(func)
        return isinstance(func, cst.Attribute) and self._import_bound(func, self.names) and \
            _in_spans(self.spans, *self._span(call))

    def leave_Call(self, original: cst.Call, updated: cst.Call) -> cst.BaseExpression:
        if not self._targets(original):
            return updated
        built = self._build(updated, original)
        if isinstance(built, str):
            self.skipped.append(built)
            return updated
        self.sites += 1
        return built

    def _build(self, call: cst.Call, original: cst.Call) -> cst.BaseExpression | str:
        member = self.member is not None
        params = _params(self.t.get("old_shape"), member)
        if not params and self.t.get("old_shape") is None:
            return "the old signature is unknown"
        if any(a.star for a in call.args):
            return "it unpacks arguments pymolt cannot see"
        bound: dict[str, cst.BaseExpression] = {}
        source_order: list[str] = []      # the site's arguments, as the call evaluates them
        extra_pos: list[cst.Arg] = []
        extra_kw: list[cst.Arg] = []
        star = next((p[0] for p in params if p[1] == "VAR_POSITIONAL"), None)
        dstar = next((p[0] for p in params if p[1] == "VAR_KEYWORD"), None)
        positional = [p for p in params if p[1] in _POSITIONAL]
        names = {p[0]: p for p in params}
        i = 0
        for arg in call.args:
            if arg.keyword is None:
                if i < len(positional):
                    bound[positional[i][0]] = arg.value
                    source_order.append(positional[i][0])
                elif star:
                    extra_pos.append(arg)
                    source_order.append("*")
                else:
                    return "it passes more positional arguments than the old signature takes"
                i += 1
                continue
            k = arg.keyword.value
            if k in names and names[k][1] in (*_POSITIONAL, "KEYWORD_ONLY"):
                bound[k] = arg.value
                source_order.append(k)
            elif dstar:
                extra_kw.append(arg)
                source_order.append("**")
            else:
                return f"it passes {k}, which the old signature does not take"
        self._used: list[str] = []
        cond = self.t.get("cond")
        if cond:
            p = next(iter(cond.values()))
            if "falsy" in cond and p == star:
                if extra_pos:
                    return "it passes what makes the body take its other branch"
            elif "none" in cond and p not in bound and names.get(p, [None, None, None])[2] \
                    == "None":
                pass
            else:
                return "its arguments do not decide the body's branch"
        callee_new = self.t.get("callee_new_shape")
        n_names = {p[0]: p for p in _params(callee_new, member)}
        new_args: list[cst.Arg] = []
        for desc in self.t.get("args") or ():
            value, passed = self._value(desc, bound, names)
            if isinstance(value, str):
                return value
            if value is None:           # *args handed on
                new_args.extend(a.with_changes(comma=cst.MaybeSentinel.DEFAULT)
                                for a in extra_pos)
                if extra_pos:
                    self._used.append("*")
                continue
            new_args.append(cst.Arg(value=value))
        for key, desc in self.t.get("kwargs") or ():
            if key == "**":
                new_args.extend(extra_kw)
                if extra_kw:
                    self._used.append("**")
                continue
            value, passed = self._value(desc, bound, names)
            if isinstance(value, str):
                return value
            if value is None:
                continue
            if not passed and key in n_names and desc.get("p") and \
                    _norm_default(names[desc["p"]][2]) == _norm_default(n_names[key][2]):
                continue                 # left at a default the successor shares
            new_args.append(cst.Arg(keyword=cst.Name(key), value=value,
                                    equal=cst.AssignEqual(
                                        whitespace_before=cst.SimpleWhitespace(""),
                                        whitespace_after=cst.SimpleWhitespace(""))))
        for key, lit in (self.t.get("materialize") or {}).items():
            if any(a.keyword is not None and a.keyword.value == key for a in new_args):
                continue
            code = _literal(repr(lit))
            if code is None:
                return f"it would have to write the default of {key}, which is no literal"
            new_args.append(cst.Arg(keyword=cst.Name(key), value=code,
                                    equal=cst.AssignEqual(
                                        whitespace_before=cst.SimpleWhitespace(""),
                                        whitespace_after=cst.SimpleWhitespace(""))))
        new_args = self._trim(new_args, n_names, bound, names)
        if isinstance(new_args, str):
            return new_args
        why = self._evaluation(source_order, bound, extra_pos, extra_kw)
        if why is not None:
            return why
        npos = sum(1 for a in new_args if a.keyword is None and not a.star)
        kws = [a.keyword.value for a in new_args if a.keyword is not None]
        why = binds_same(npos, kws, self.t.get("callee_old_shape"), callee_new, member)
        if why is not None:
            return why
        func = self._callee(call.func, original.func)
        if isinstance(func, str):
            return func
        out: cst.BaseExpression = call.with_changes(func=func, args=_commas(new_args))
        for attr, args, kwargs in self.t.get("chain") or ():
            chain_args = []
            for desc in args:
                if "c" not in desc:
                    return "a call it chains takes an argument pymolt cannot rebuild"
                chain_args.append(cst.Arg(value=cst.parse_expression(repr(desc["c"]))))
            for key, desc in kwargs:
                if "c" not in desc:
                    return "a call it chains takes an argument pymolt cannot rebuild"
                chain_args.append(cst.Arg(keyword=cst.Name(key),
                                          value=cst.parse_expression(repr(desc["c"]))))
            out = cst.Call(func=cst.Attribute(value=out, attr=cst.Name(attr)),
                           args=_commas(chain_args))
        return out

    def _evaluation(self, source_order: list[str], bound: Mapping[str, cst.BaseExpression],
                    extra_pos: list[cst.Arg], extra_kw: list[cst.Arg]) -> str | None:
        """Why the rebuilt call would evaluate the site's arguments otherwise (None: it would
        not): an argument the forward drops, repeats or moves before another is allowed only
        when it is a name or a literal, which no evaluation can tell apart."""
        def simple(p: str) -> bool:
            if p == "*":
                return all(_simple(a.value) for a in extra_pos)
            if p == "**":
                return all(_simple(a.value) for a in extra_kw)
            return _simple(bound.get(p))

        for p in source_order:
            uses = self._used.count(p)
            if uses == 0:
                return f"the forward does not hand on {p}, which this call passes"
            if uses > 1 and not simple(p):
                return f"the forward evaluates {p} {uses} times"
        firsts = list(dict.fromkeys(u for u in self._used if u in source_order))
        moving = [p for p in firsts if not simple(p)]
        expected = [p for p in source_order if not simple(p)]
        if moving != expected:
            return "the forward evaluates this call's arguments in another order"
        return None

    def _value(self, desc: Mapping[str, Any], bound: Mapping[str, cst.BaseExpression],
               names: Mapping[str, list]) -> tuple[cst.BaseExpression | str | None, bool]:
        if "p" in desc:
            p = desc["p"]
            if p in bound:
                self._used.append(p)
                return bound[p], True
            spec = names.get(p)
            if spec is None:
                return f"the forward hands on {p}, which is no parameter", False
            code = _literal(spec[2])
            if code is None:
                return f"it would have to write the default of {p}, which is no literal", False
            return code, False
        if "c" in desc:
            return cst.parse_expression(repr(desc["c"])), True
        if "star" in desc:
            return None, True
        return "the forward computes an argument pymolt cannot rebuild", False

    def _trim(self, args: list[cst.Arg], n_names: Mapping[str, list],
              bound: Mapping[str, cst.BaseExpression], names: Mapping[str, list]
              ) -> list[cst.Arg] | str:
        """Drop trailing positional arguments that only restate a default the successor
        shares."""
        callee = _params(self.t.get("callee_new_shape"), self.member is not None)
        positional = [p for p in callee if p[1] in _POSITIONAL]
        out = list(args)
        while out and out[-1].keyword is None and not out[-1].star:
            i = len([a for a in out if a.keyword is None and not a.star]) - 1
            if i >= len(positional):
                break
            code = out[-1].value
            if any(code is v for v in bound.values()):
                break
            if _literal(positional[i][2]) is None or \
                    cst.Module([]).code_for_node(code) != \
                    cst.Module([]).code_for_node(_literal(positional[i][2])):
                break
            out.pop()
        return out

    def _callee(self, func: cst.BaseExpression, original: cst.BaseExpression
                ) -> cst.BaseExpression | str:
        new = str(self.t.get("callee") or "")
        if self.member is not None:
            assert isinstance(func, cst.Attribute)
            return func.with_changes(attr=cst.Name(new.rsplit(".", 1)[-1]))
        if not isinstance(func, cst.Attribute) or not isinstance(original, cst.Attribute):
            return "only a call through its module is rewritten (``alias.name(...)``)"
        try:
            qnames = self.get_metadata(QualifiedNameProvider, original.value)
        except KeyError:
            return "its module binding is unknown"
        modules = {q.name for q in qnames}
        new_mod, _, leaf = new.rpartition(".")
        if modules != {new_mod}:           # the module on every path, not on some
            return f"{new} lives in another module than the call's binding"
        return func.with_changes(attr=cst.Name(leaf))


def _commas(args: list[cst.Arg]) -> list[cst.Arg]:
    out = []
    for i, arg in enumerate(args):
        comma = cst.Comma(whitespace_after=cst.SimpleWhitespace(" ")) if i < len(args) - 1 \
            else cst.MaybeSentinel.DEFAULT
        out.append(arg.with_changes(comma=comma))
    return out


# ── entry point ──────────────────────────────────────────────────────────────────────────


def apply_successor_pattern(source: str, pattern: Any, *, path: str | None = None
                            ) -> tuple[str, int]:
    """Apply one successor pattern to ``source`` (``path``: its project-relative file, which
    its ``sites`` are matched against). Returns ``(new_source, sites)``."""
    module = cst.parse_module(source)
    spans = site_spans(source, pattern.sites, path)
    names = {pattern.old_qualname, *(pattern.aliases or ())}
    template = pattern.template or {}
    member = template.get("member_leaf")
    if pattern.kind == "rename-keyword":
        transformer: Any = RenameKeywordCodemod(
            names, pattern.params or {}, member=member, spans=spans,
            old_shape=template.get("old_shape"), blocked=template.get("blocked") or (),
            new_shape=template.get("new_shape"), guards=template.get("guards"))
    elif pattern.kind == "rename-member":
        transformer = RenameMemberCodemod(
            pattern.old_qualname.rsplit(".", 1)[-1], pattern.new_qualname.rsplit(".", 1)[-1],
            spans, call_only=not template.get("attr"),
            shapes=None if template.get("attr") else (template.get("old_shape"),
                                                      template.get("callee_old_shape"),
                                                      template.get("callee_new_shape")))
    elif pattern.kind == "forward-template":
        transformer = ForwardTemplateCodemod(names, template, member=member, spans=spans)
    else:
        raise ValueError(f"not a successor pattern: {pattern.kind}")
    out = MetadataWrapper(module).visit(transformer)
    return out.code, transformer.sites
