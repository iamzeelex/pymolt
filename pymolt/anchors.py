"""Statement anchors: name a statement by what it is and what surrounds it, never by its line.

HOST side (3.12+; the stdlib's ``ast``, ``tokenize`` and ``inspect``, and a pydantic model). A
plan step, a risk site or a ledger entry has to find its statement again after lines move: an
earlier step skipped, a hand edit above it, a reformat. An ``Anchor`` names the statement in one
text, and ``StatementIndex.resolve`` finds it in another text, or says it cannot.

What an anchor holds (design: ``docs/design/apply-work-order.md`` 1.4):

==============  ==================================================================================
``key``         sha1 of the statement with its formatting stripped. The AST drops quote style,
                implicit string concatenation, redundant parentheses, magic trailing commas,
                whitespace, comments and line breaks. A compound statement keeps only its header:
                its body becomes ``pass`` and its ``else``/``except``/``finally``/``case`` parts are
                dropped. A docstring, or any bare string statement, is keyed on its text after
                ``inspect.cleandoc``, with whitespace runs collapsed per line (so black and
                ``ruff format`` re-indenting it, or stripping its trailing spaces, change nothing).
                The text is parsed with ``feature_version`` set to its Python, so 3.6 code that
                uses ``async`` as a name parses. A text that no grammar parses gets a token key
                (``key_kind: "tokens"``), which does not normalise redundant parentheses. Keys
                are pymolt's own serialization, the same on every host Python (``ast.dump``'s
                output changed in 3.13).
``scope``       the dotted ``class``/``def`` names around the statement; ``<module>`` at top level
``func``        the innermost of those names, or ``<module>``: a capture's ``co_name``
``frames``      the other code objects a call on the site's line can run in (``<listcomp>``,
                ``<genexpr>``, ``<lambda>``, ...). Python 3.12 inlines list, set and dict
                comprehensions into ``func`` (PEP 709), earlier versions do not, so a capture's
                ``func`` joins the anchor when it is ``func`` or one of these (``Anchor.joins``)
``prev``/``next``  the keys of the statements before and after it in its scope; ``^`` and ``$``
                at the scope's edges
``nth``/``count``  its ordinal among the statements of its scope with the same key, and how many
                there were, in the text the anchor was made on
``line``        where it was when made: a hint, never an identity
==============  ==================================================================================

A **statement** here is a simple statement, a compound statement's header (decorators
included), or an ``except``/``case`` clause. A scope's statements are those in its body, in
source order, with nested blocks (``if``, ``for``, ``try``, ...) flattened; a ``def`` or
``class`` body is a scope of its own, and its header belongs to the enclosing one.

**Resolution** on a text never guesses:

1. the scope must exist. A path several definitions share (a property's getter and setter, a
   conditional ``def``) names all of them, and ``nth``/``count`` run over their union;
2. the candidates are the scope's statements with the anchor's key; none: **lost**;
3. one candidate: a match, when the anchor was unique when made (``count == 1``) or the
   candidate has the anchor's neighbours. A lone survivor of a tie with other neighbours is
   **ambiguous**;
4. several: the one whose ``prev`` and ``next`` equal the anchor's, if exactly one has both;
5. still several: the ``nth`` one, only when the candidates number ``count`` (the tie the anchor
   was made in) and no neighbour match contradicts it. Otherwise **ambiguous**, which counts as
   lost.

``StatementIndex.slot`` serves the work order's apply rule 4 (an effect already on disk): the
statements between the anchor's neighbours, when exactly one place in the scope has them that
far apart.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import io
import re
import textwrap
import tokenize
import warnings
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

KeyKind = Literal["ast", "tokens"]
Status = Literal["match", "lost", "ambiguous"]

MODULE = "<module>"
START, END = "^", "$"

# The code objects a call can run in besides its def, class or module (spec 1.4, ``func``).
_FRAME_NAMES: dict[type, str] = {
    ast.ListComp: "<listcomp>", ast.SetComp: "<setcomp>", ast.DictComp: "<dictcomp>",
    ast.GeneratorExp: "<genexpr>", ast.Lambda: "<lambda>",
}
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
# A compound statement's parts other than its header, in source order.
_PARTS = ("body", "handlers", "orelse", "finalbody", "cases")
_STMT_CAP = 500                     # display text only; the key covers the whole statement
_NEWLINE = re.compile(r"\r\n|\r|\n")
_OPEN, _CLOSE = {"(", "[", "{"}, {")", "]", "}"}


class AnchorError(ValueError):
    """The text has no statements to anchor: it neither parses nor tokenizes."""


class Anchor(BaseModel):
    """A statement named by content and context, made on one text (see the module docstring)."""

    id: str
    file: str
    scope: str
    func: str
    symbol: str | None = None
    stmt: str                                    # display: the statement or the header
    key: str = Field(pattern=r"^[0-9a-f]{16}$")
    key_kind: KeyKind = "ast"
    prev: str
    next: str
    nth: int = Field(ge=0)
    count: int = Field(ge=1)
    site_text: str | None = None                 # the stripped source line: ``Fact.text``
    line: int | None = None                      # a hint, never an identity
    frames: list[str] = Field(default_factory=list)

    def joins(self, func: str | None) -> bool:
        """Whether a capture's ``func`` (the frame's ``co_name``) can name this site: ``func``
        itself, or a comprehension, generator expression or lambda on the site's line. Both
        sides of PEP 709 join: ``<listcomp>`` (3.6-3.11) and the enclosing def (3.12+)."""
        return func is not None and (func == self.func or func in self.frames)


@dataclass(frozen=True, slots=True)
class Statement:
    """One statement of an indexed text."""

    key: str
    key_kind: KeyKind
    node: str           # the ast node type ("Expr", "FunctionDef", "ExceptHandler", "match_case",
    #                     "Docstring"), or a token unit's first word ("def", "import", ...)
    scope: str
    func: str
    first: int          # its own lines, 1-based and inclusive: the statement, or the header
    last: int
    end: int            # the last line of the whole statement, its body included
    text: str           # display text, dedented and capped
    prev: str
    next: str
    nth: int
    count: int
    instance: int       # which definition of ``scope`` holds it (0 unless the path repeats)
    position: int       # its index among that definition's statements

    @property
    def is_import(self) -> bool:
        return self.node in ("Import", "ImportFrom", "import", "from")


@dataclass(frozen=True, slots=True)
class Resolution:
    status: Status
    statement: Statement | None = None
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "match"


@dataclass(frozen=True, slots=True)
class Slot:
    """The statements between an anchor's neighbours (``StatementIndex.slot``)."""

    status: Status
    statements: tuple[Statement, ...] = ()
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "match"


def python_minor(python: Any) -> tuple[int, int] | None:
    """``(3, 6)`` from ``"3.6"``, ``"3.6.15"``, ``(3, 6)`` or ``(3, 6, 15)``; None otherwise."""
    if python is None:
        return None
    if isinstance(python, tuple | list) and len(python) >= 2:
        return int(python[0]), int(python[1])
    m = re.match(r"\s*(\d+)\.(\d+)", str(python))
    return (int(m[1]), int(m[2])) if m else None


# ── keys ──────────────────────────────────────────────────────────────────────────────────


class _Text(str):
    """Serializer punctuation, told apart from string values in the AST."""


def _flat_targets(targets: list) -> list:
    """``del (a, b)`` and ``del a, b`` delete the same names (black rewrites the first)."""
    out: list = []
    stack = list(reversed(targets))
    while stack:
        t = stack.pop()
        if isinstance(t, ast.Tuple | ast.List):
            stack.extend(reversed(t.elts))
        else:
            out.append(t)
    return out


def _fields(node: ast.AST, overrides: dict[str, Any] | None) -> list[tuple[str, Any]]:
    out = []
    for name in sorted(node._fields):
        value = overrides[name] if overrides and name in overrides else getattr(node, name, None)
        if name == "type_comment" or (name == "kind" and isinstance(node, ast.Constant)):
            continue                     # ``u"x"`` is ``"x"``; black drops the prefix
        if name == "targets" and isinstance(node, ast.Delete):
            value = _flat_targets(value)
        if value is None or (isinstance(value, list) and not value):
            continue
        out.append((name, value))
    return out


def _ast_key(node: ast.AST, overrides: dict[str, Any] | None = None) -> str:
    """sha1 of the node's structure, ``overrides`` replacing fields of the node itself.

    Not ``ast.dump``: its format changed in 3.13 (empty fields omitted), and keys made on one
    host must equal keys made on another. Fields are sorted, None and empty lists are left
    out (so fields a newer Python adds, empty in older code, change nothing), and the
    serialization is iterative (a 3,000-term ``+`` chain is as deep as it is long)."""
    h = hashlib.sha1(b"ast\x1f")
    stack: list[Any] = [node]
    while stack:
        item = stack.pop()
        if type(item) is _Text:
            h.update(item.encode())
        elif isinstance(item, ast.AST):
            parts: list[Any] = [_Text(type(item).__name__ + "(")]
            for name, value in _fields(item, overrides if item is node else None):
                parts += [_Text(name + "="), value, _Text(",")]
            parts.append(_Text(")"))
            stack.extend(reversed(parts))
        elif isinstance(item, list):
            parts = [_Text("[")]
            for value in item:
                parts += [value, _Text(",")]
            parts.append(_Text("]"))
            stack.extend(reversed(parts))
        else:
            h.update(f"{type(item).__name__}:{item!r}".encode("utf-8", "surrogatepass"))
    return h.hexdigest()[:16]


def _text_key(value: str) -> str:
    """A bare string statement (a docstring): its text, cleaned as ``inspect.cleandoc`` does,
    with whitespace runs collapsed per line."""
    cleaned = inspect.cleandoc(value)
    norm = "\n".join(" ".join(line.split()) for line in cleaned.split("\n"))
    return hashlib.sha1(b"str\x1f" + norm.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def _literal(token: str) -> Any:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")        # ``"\d"``: an invalid escape warns
            return ast.literal_eval(token)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return None


def _token_key(toks: list[tokenize.TokenInfo]) -> str:
    """The token fallback: string and number tokens by their value (adjacent strings joined),
    a comma before a closing bracket dropped. Comments and layout tokens never get here."""
    parts: list[list[Any]] = []          # [kind, text, literal value or None]
    for j, tok in enumerate(toks):
        if (tok.type == tokenize.OP and tok.string == ","
                and j + 1 < len(toks) and toks[j + 1].string in _CLOSE):
            continue
        if tok.type == tokenize.STRING:
            value = _literal(tok.string)
            if isinstance(value, str | bytes):
                last = parts[-1] if parts else None
                if last and last[0] == "S" and type(last[2]) is type(value):
                    last[2] = last[2] + value
                    continue
                parts.append(["S", None, value])
                continue
        if tok.type == tokenize.NUMBER:
            value = _literal(tok.string)
            if isinstance(value, int | float | complex):
                parts.append(["N", repr(value), None])
                continue
        parts.append([tokenize.tok_name[tok.type], tok.string, None])
    h = hashlib.sha1(b"tok\x1f")
    for kind, text, value in parts:
        h.update(f"{kind}\x1e{repr(value) if kind == 'S' else text}\x1f".encode(
            "utf-8", "surrogatepass"))
    return h.hexdigest()[:16]


# ── text helpers ──────────────────────────────────────────────────────────────────────────


def _lines(text: str) -> list[str]:
    """Physical lines as the tokenizer and ``linecache`` count them (``str.splitlines`` also
    splits on form feeds and other separators, which would misnumber the lines)."""
    return _NEWLINE.split(text)


def _line(lines: list[str], n: int) -> str:
    return lines[n - 1] if 0 < n <= len(lines) else ""


def _chars(line: str, col: int) -> int:
    """A character index from an AST column (a UTF-8 byte offset)."""
    return len(line.encode("utf-8", "surrogatepass")[:col].decode("utf-8", "ignore"))


def _display(lines: list[str], first: int, last: int, start: int | None = None,
             stop: int | None = None) -> str:
    """The statement's text: its lines dedented; on one line, from ``start`` to ``stop``
    (character offsets), so one of several ``;``-separated statements shows alone."""
    seg = [_line(lines, n) for n in range(first, last + 1)]
    if stop is not None:
        seg[-1] = seg[-1][:stop]
    if start is not None:
        indent = len(seg[0]) - len(seg[0].lstrip())
        seg[0] = seg[0][:indent] + seg[0][max(start, indent):]
    text = "\n".join(line.rstrip() for line in textwrap.dedent("\n".join(seg)).split("\n"))
    text = text.strip("\n")
    return text if len(text) <= _STMT_CAP else text[:_STMT_CAP] + " …"


def _blank_or_comment(line: str) -> bool:
    s = line.strip()
    return not s or s.startswith("#")


# ── units, before neighbours and ordinals are known ──────────────────────────────────────


@dataclass(slots=True)
class _Unit:
    key: str
    node: str
    scope: str
    instance: int
    func: str
    first: int
    last: int
    end: int
    text: str


class _Scopes:
    """Definition instances per scope path."""

    def __init__(self) -> None:
        self.count: dict[str, int] = defaultdict(int)
        self.count[MODULE] = 1

    def open(self, parent: str, name: str) -> tuple[str, int]:
        path = name if parent == MODULE else f"{parent}.{name}"
        instance = self.count[path]
        self.count[path] += 1
        return path, instance


def _first_line(node: ast.AST) -> int:
    decorators = getattr(node, "decorator_list", None) or ()
    if isinstance(node, ast.match_case):
        return node.pattern.lineno
    return min([node.lineno, *(d.lineno for d in decorators)])


def _starts_its_line(node: ast.AST) -> bool:
    """A decorated definition or a ``case`` clause always begins a line (and its own column
    is not where it begins: that is the decorator's expression, or the pattern)."""
    return isinstance(node, ast.match_case) or bool(getattr(node, "decorator_list", None))


class _AstBuilder:
    def __init__(self, lines: list[str]):
        self.lines = lines
        self.units: list[_Unit] = []
        self.scopes = _Scopes()

    def module(self, tree: ast.Module) -> list[_Unit]:
        self.block(tree.body, MODULE, 0, MODULE, docstring=True)
        return self.units

    def block(self, stmts: Iterable[ast.AST], scope: str, instance: int, func: str,
              docstring: bool = False) -> None:
        for i, node in enumerate(stmts):
            self.stmt(node, scope, instance, func, docstring and i == 0)

    def add(self, key: str, node: str, scope: str, instance: int, func: str, first: int,
            last: int, end: int, text: str) -> None:
        self.units.append(_Unit(key, node, scope, instance, func, first, last, end, text))

    def header_last(self, first: int, body0: ast.AST) -> tuple[int, int | None]:
        """The header's last line, and on a one-liner (``if x: y()``) the character offset
        where its body starts on that line."""
        line_no = _first_line(body0)
        if not _starts_its_line(body0):
            line = _line(self.lines, line_no)
            start = _chars(line, body0.col_offset)
            if line[:start].strip():
                return line_no, start
        last = max(first, line_no - 1)
        while last > first and _blank_or_comment(_line(self.lines, last)):
            last -= 1
        return last, None

    def header(self, node: ast.AST, key: str, kind: str, scope: str, instance: int,
               func: str, body: list) -> None:
        first = _first_line(node)
        end = getattr(node, "end_lineno", None) or (body[-1].end_lineno if body else first)
        last, stop = self.header_last(first, body[0]) if body else (first, None)
        # A header begins its line: no statement can precede it there.
        self.add(key, kind, scope, instance, func, first, last, end,
                 _display(self.lines, first, last, None, stop))

    def stmt(self, node: ast.AST, scope: str, instance: int, func: str, first_in_body: bool
             ) -> None:
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            self.simple(node, _text_key(node.value.value),
                        "Docstring" if first_in_body else "Expr", scope, instance, func)
            return
        if isinstance(node, _DEFS):
            key = _ast_key(node, {"body": [ast.Pass()]})
            self.header(node, key, type(node).__name__, scope, instance, func, node.body)
            path, inner = self.scopes.open(scope, node.name)
            self.block(node.body, path, inner, node.name, docstring=True)
            return
        parts = [p for p in _PARTS if isinstance(getattr(node, p, None), list)]
        if not parts:
            self.simple(node, _ast_key(node), type(node).__name__, scope, instance, func)
            return
        overrides: dict[str, Any] = {p: [] for p in parts}
        if "body" in overrides:
            overrides["body"] = [ast.Pass()]
        body = node.body if "body" in parts else node.cases
        self.header(node, _ast_key(node, overrides), type(node).__name__, scope, instance,
                    func, body)
        for part in parts:
            for child in getattr(node, part):
                if isinstance(child, ast.ExceptHandler | ast.match_case):
                    self.header(child, _ast_key(child, {"body": [ast.Pass()]}),
                                type(child).__name__, scope, instance, func, child.body)
                    self.block(child.body, scope, instance, func)
                else:
                    self.stmt(child, scope, instance, func, False)

    def simple(self, node: ast.AST, key: str, kind: str, scope: str, instance: int,
               func: str) -> None:
        first, last = node.lineno, node.end_lineno or node.lineno
        start = _chars(_line(self.lines, first), node.col_offset)
        stop = (_chars(_line(self.lines, last), node.end_col_offset)
                if node.end_col_offset is not None else None)
        self.add(key, kind, scope, instance, func, first, last, last,
                 _display(self.lines, first, last, start, stop))


def _parse(text: str, python: Any) -> ast.Module | None:
    """The text's AST under its own Python, else the host's grammar, else 3.6's (``async``
    and ``await`` as names). Whichever grammar accepts a text gives it the same tree."""
    tried: list[tuple[int, int] | None] = []
    for version in (python_minor(python), None, (3, 6)):
        if version in tried:
            continue
        tried.append(version)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")    # the project's own SyntaxWarnings
                return ast.parse(text, feature_version=version)
        except (SyntaxError, ValueError, RecursionError, MemoryError):
            continue
    return None


# ── the token fallback ────────────────────────────────────────────────────────────────────

_COMPOUND = {"if", "elif", "else", "for", "while", "try", "except", "finally", "with", "def",
             "class"}
_SOFT = {"match", "case"}
_NOT_SOFT = {":", "=", ".", ",", ")", "]", "}", "+=", "-=", "*=", "/=", "|=", "&=", "->"}
_SKIP = {tokenize.COMMENT, tokenize.NL, tokenize.ENCODING}


def _depth0(toks: list[tokenize.TokenInfo], char: str) -> list[int]:
    out, depth = [], 0
    for i, tok in enumerate(toks):
        if tok.type != tokenize.OP:
            continue
        if tok.string in _OPEN:
            depth += 1
        elif tok.string in _CLOSE:
            depth -= 1
        elif tok.string == char and depth == 0:
            out.append(i)
    return out


class _TokenBuilder:
    """Statements from tokens, for a text no grammar parses (Python 2 code, a broken file)."""

    def __init__(self, text: str, lines: list[str]):
        self.text, self.lines = text, lines
        self.units: list[_Unit] = []
        self.scopes = _Scopes()
        # block stack: (path, instance, func) of a def/class body, or None for another block
        self.blocks: list[tuple[str, int, str] | None] = []
        self.pending: tuple[str, str] | None = None     # what the next INDENT opens
        self.decorators: list[list[tokenize.TokenInfo]] = []
        self.expect_doc = True

    def scope(self) -> tuple[str, int, str]:
        for entry in reversed(self.blocks):
            if entry is not None:
                return entry
        return MODULE, 0, MODULE

    def build(self) -> list[_Unit]:
        try:
            toks = list(tokenize.generate_tokens(io.StringIO(self.text).readline))
        except (tokenize.TokenError, SyntaxError) as exc:
            raise AnchorError(f"the text neither parses nor tokenizes: {exc}") from exc
        logical: list[tokenize.TokenInfo] = []
        for tok in toks:
            if tok.type in _SKIP:
                continue
            if tok.type == tokenize.INDENT:
                self.indent()
            elif tok.type == tokenize.DEDENT:
                if self.blocks:
                    self.blocks.pop()
            elif tok.type in (tokenize.NEWLINE, tokenize.ENDMARKER):
                if logical:
                    self.logical(logical)
                logical = []
            else:
                logical.append(tok)
        if logical:
            self.logical(logical)
        self.flush_decorators()
        return self.units

    def indent(self) -> None:
        pending, self.pending = self.pending, None
        if pending is not None and pending[0] == "def":
            parent = self.scope()[0]
            path, instance = self.scopes.open(parent, pending[1])
            self.blocks.append((path, instance, pending[1]))
            self.expect_doc = True
        else:
            self.blocks.append(None)

    def unit(self, toks: list[tokenize.TokenInfo], where: tuple[str, int, str],
             node: str | None = None) -> None:
        if not toks:
            return
        doc = self.expect_doc
        self.expect_doc = False
        first, last = toks[0].start[0], toks[-1].end[0]
        if all(t.type == tokenize.STRING for t in toks):
            values = [_literal(t.string) for t in toks]
            if all(isinstance(v, str) for v in values):
                self.units.append(_Unit(_text_key("".join(values)),
                                        "Docstring" if doc else "Expr", *where[:2], where[2],
                                        first, last, last, _display(self.lines, first, last)))
                return
        start = toks[0].start[1] if first == last else None
        stop = toks[-1].end[1] if first == last else None
        self.units.append(_Unit(_token_key(toks), node or toks[0].string, where[0], where[1],
                                where[2],
                                first, last, last,
                                _display(self.lines, first, last, start, stop)))

    def flush_decorators(self) -> None:
        for dec in self.decorators:
            self.unit(dec, self.scope())
        self.decorators = []

    def simple(self, toks: list[tokenize.TokenInfo], where: tuple[str, int, str]) -> None:
        cuts = [-1, *_depth0(toks, ";"), len(toks)]
        for a, b in zip(cuts, cuts[1:], strict=False):
            self.unit(toks[a + 1:b], where)

    def logical(self, toks: list[tokenize.TokenInfo]) -> None:
        if toks[0].type == tokenize.OP and toks[0].string == "@":
            self.decorators.append(toks)
            return
        words = [t.string for t in toks[:2]]
        word = words[1] if words[0] == "async" and len(words) > 1 else words[0]
        colons = _depth0(toks, ":")
        compound = toks[0].type == tokenize.NAME and colons and (
            word in _COMPOUND
            or (word in _SOFT and len(toks) > 1 and toks[1].string not in _NOT_SOFT))
        if not compound:
            self.flush_decorators()
            self.simple(toks, self.scope())
            return
        header, rest = toks[:colons[0] + 1], toks[colons[0] + 1:]
        where = self.scope()
        if word in ("def", "class"):
            name_at = 2 if words[0] == "async" else 1
            name = toks[name_at].string if len(toks) > name_at else "?"
            merged = [t for dec in self.decorators for t in dec] + header
            self.decorators = []
            self.unit(merged, where, word)
            if rest:                                    # ``def f(): return 1``
                path, instance = self.scopes.open(where[0], name)
                self.expect_doc = True
                self.simple(rest, (path, instance, name))
            else:
                self.pending = ("def", name)
            return
        self.flush_decorators()
        self.unit(header, where)
        if rest:
            self.simple(rest, where)
        else:
            self.pending = ("block", "")


# ── the index ─────────────────────────────────────────────────────────────────────────────


def _finish(units: list[_Unit], kind: KeyKind) -> tuple[Statement, ...]:
    """Neighbours per definition instance, ordinals per scope path (instances in source order)."""
    by_instance: dict[tuple[str, int], list[int]] = defaultdict(list)
    by_key: dict[tuple[str, str], list[int]] = defaultdict(list)
    for i, u in enumerate(units):
        by_instance[(u.scope, u.instance)].append(i)
        by_key[(u.scope, u.key)].append(i)
    prev: dict[int, str] = {}
    nxt: dict[int, str] = {}
    position: dict[int, int] = {}
    for members in by_instance.values():
        for p, i in enumerate(members):
            position[i] = p
            prev[i] = units[members[p - 1]].key if p else START
            nxt[i] = units[members[p + 1]].key if p + 1 < len(members) else END
    nth: dict[int, tuple[int, int]] = {}
    for members in by_key.values():
        for n, i in enumerate(members):
            nth[i] = (n, len(members))
    return tuple(
        Statement(key=u.key, key_kind=kind, node=u.node, scope=u.scope, func=u.func,
                  first=u.first, last=u.last, end=u.end, text=u.text, prev=prev[i], next=nxt[i],
                  nth=nth[i][0], count=nth[i][1], instance=u.instance, position=position[i])
        for i, u in enumerate(units))


def _leaves(symbol: str | None) -> tuple[str, ...]:
    if not symbol:
        return ()
    parts = symbol.split(".")
    if len(parts) > 1 and parts[-1] in ("__init__", "__new__", "__call__"):
        return parts[-1], parts[-2]
    return (parts[-1],)


def _anchor_id(file: str, st: Statement) -> str:
    raw = "|".join((file, st.scope, st.key, str(st.nth), str(st.count)))
    return "a" + hashlib.sha1(raw.encode("utf-8", "surrogatepass")).hexdigest()[:8]


class StatementIndex:
    """The statements of one text, with everything anchors need. Build once per text and make
    or resolve as many anchors on it as needed."""

    def __init__(self, text: str, lines: list[str], python: tuple[int, int] | None,
                 key_kind: KeyKind, statements: tuple[Statement, ...],
                 frames: list[tuple[int, int, str]] | None = None):
        self.text, self.lines, self.python = text, lines, python
        self.key_kind: KeyKind = key_kind
        self.statements = statements
        self._frames = frames or []
        self._frames_by_line: dict[int, set[str]] | None = None
        self._order = {id(st): i for i, st in enumerate(statements)}
        self._by_line: dict[int, list[Statement]] = defaultdict(list)
        self._by_scope: dict[str, list[Statement]] = defaultdict(list)
        self._by_key: dict[tuple[str, str], list[Statement]] = defaultdict(list)
        self._instances: dict[str, dict[int, list[Statement]]] = defaultdict(dict)
        for st in statements:
            for n in range(st.first, st.last + 1):
                self._by_line[n].append(st)
            self._by_scope[st.scope].append(st)
            self._by_key[(st.scope, st.key)].append(st)
            self._instances[st.scope].setdefault(st.instance, []).append(st)
        self._other: dict[KeyKind, StatementIndex | None] = {}

    @classmethod
    def build(cls, text: str, *, python: Any = None, key_kind: KeyKind | None = None
              ) -> StatementIndex:
        """Index ``text``. ``python``: the Python the text is for (``"3.6"``, ``(3, 6)``): the
        base for the baseline's text, the target for after-texts. ``key_kind``: force one kind
        (the kind of the anchors to resolve); by default AST keys, token keys when no grammar
        parses the text. Raises ``AnchorError`` when there are no statements to be had
        (``key_kind="ast"`` on a text that does not parse, or one that does not tokenize)."""
        lines = _lines(text)
        minor = python_minor(python)
        if key_kind in (None, "ast"):
            tree = _parse(text, python)
            if tree is not None:
                frames = [(n.lineno, n.end_lineno or n.lineno, _FRAME_NAMES[type(n)])
                          for n in ast.walk(tree) if type(n) in _FRAME_NAMES]
                units = _AstBuilder(lines).module(tree)
                return cls(text, lines, minor, "ast", _finish(units, "ast"), frames)
            if key_kind == "ast":
                raise AnchorError("the text does not parse, so it has no AST keys")
        units = _TokenBuilder(text, lines).build()
        return cls(text, lines, minor, "tokens", _finish(units, "tokens"))

    def of_kind(self, kind: KeyKind) -> StatementIndex | None:
        """This text indexed with ``kind`` keys (None: it cannot be)."""
        if kind == self.key_kind:
            return self
        if kind not in self._other:
            try:
                self._other[kind] = StatementIndex.build(self.text, python=self.python,
                                                         key_kind=kind)
            except AnchorError:
                self._other[kind] = None
        return self._other[kind]

    # lookups

    def statement_at(self, line: int, symbol: str | None = None) -> Statement | None:
        """The statement whose own lines hold ``line``. Several (``a(); b()``, or ``if x: y()``):
        the first whose text names the symbol's leaf, else the first. None on a line no
        statement owns (blank, comment, ``else:``)."""
        found = self._by_line.get(line) or []
        if len(found) > 1:
            for leaf in _leaves(symbol):
                named = [st for st in found if re.search(rf"\b{re.escape(leaf)}\b", st.text)]
                if named:
                    return named[0]
        return found[0] if found else None

    def statements_in(self, first: int, last: int) -> list[Statement]:
        """The statements whose own lines meet ``first..last``, in source order."""
        seen: set[int] = set()
        out: list[Statement] = []
        for n in range(first, last + 1):
            for st in self._by_line.get(n) or ():
                if id(st) not in seen:
                    seen.add(id(st))
                    out.append(st)
        return sorted(out, key=lambda st: self._order[id(st)])

    def in_scope(self, scope: str) -> list[Statement]:
        """The statements of every definition with this path, in source order."""
        return list(self._by_scope.get(scope) or ())

    def definitions(self, scope: str) -> int:
        """How many definitions share the path (``<module>``: 1; unknown: 0)."""
        return len(self._instances.get(scope) or ())

    def frames_at(self, line: int) -> list[str]:
        """The comprehension, generator-expression and lambda frames covering ``line``."""
        if self._frames_by_line is None:
            self._frames_by_line = defaultdict(set)
            for first, last, name in self._frames:
                for n in range(first, last + 1):
                    self._frames_by_line[n].add(name)
        return sorted(self._frames_by_line.get(line) or ())

    # anchors

    def anchor(self, statement: Statement, *, file: str, id: str | None = None,
               symbol: str | None = None, site_line: int | None = None) -> Anchor:
        """The anchor of one of this index's statements. ``site_line``: the line a risk site or
        capture names (gives ``site_text``, ``frames`` and the ``line`` hint)."""
        site_text = _line(self.lines, site_line).strip() if site_line else None
        return Anchor(
            id=id or _anchor_id(file, statement), file=file, scope=statement.scope,
            func=statement.func, symbol=symbol, stmt=statement.text, key=statement.key,
            key_kind=statement.key_kind, prev=statement.prev, next=statement.next,
            nth=statement.nth, count=statement.count, site_text=site_text,
            line=site_line or statement.first,
            frames=self.frames_at(site_line) if site_line else [])

    def anchor_at(self, line: int, *, file: str, id: str | None = None,
                  symbol: str | None = None) -> Anchor | None:
        """The anchor of the statement holding ``line`` (None: no statement owns it)."""
        st = self.statement_at(line, symbol)
        if st is None:
            return None
        return self.anchor(st, file=file, id=id, symbol=symbol, site_line=line)

    # resolution

    def resolve(self, anchor: Anchor) -> Resolution:
        """Find the anchor's statement in this text (rules in the module docstring)."""
        index = self.of_kind(anchor.key_kind)
        if index is None:
            return Resolution("lost", reason="the text does not parse, so it has no AST keys")
        if anchor.scope not in index._by_scope:
            return Resolution("lost", reason=f"no scope {anchor.scope} in the text")
        cands = index._by_key.get((anchor.scope, anchor.key)) or []
        if not cands:
            return Resolution("lost", reason=f"no statement of {anchor.scope} is "
                                             f"`{_short(anchor.stmt)}`")
        near = [st for st in cands
                if st.prev == anchor.prev and st.next == anchor.next]
        if len(cands) == 1:
            if anchor.count == 1 or near:
                return Resolution("match", cands[0])
            return Resolution("ambiguous", reason=(
                f"`{_short(anchor.stmt)}` was one of {anchor.count} equal statements of "
                f"{anchor.scope}; the one left has other neighbours"))
        if len(near) == 1:
            return Resolution("match", near[0])
        if len(cands) == anchor.count:
            pick = cands[anchor.nth]
            if not near or pick in near:
                return Resolution("match", pick)
        return Resolution("ambiguous", reason=(
            f"{len(cands)} statements of {anchor.scope} are `{_short(anchor.stmt)}` (the anchor "
            f"was made among {anchor.count}) and their neighbours do not tell them apart"))

    def slot(self, anchor: Anchor, *, width: int = 1) -> Slot:
        """The ``width`` statements between the anchor's ``prev`` and ``next`` (0: the two are
        adjacent, a deletion; 1: around one statement, a replacement), when exactly one place
        in the anchor's scope has them so. Apply's rule 4 then checks that statement's key."""
        index = self.of_kind(anchor.key_kind)
        if index is None:
            return Slot("lost", reason="the text does not parse, so it has no AST keys")
        instances = index._instances.get(anchor.scope)
        if not instances:
            return Slot("lost", reason=f"no scope {anchor.scope} in the text")
        found: list[tuple[Statement, ...]] = []
        for members in instances.values():
            n = len(members)
            for i in range(-1, n):
                if (anchor.prev == START) != (i == -1):
                    continue
                if i >= 0 and members[i].key != anchor.prev:
                    continue
                j = i + width + 1
                if (j == n and anchor.next == END) or (j < n and members[j].key == anchor.next):
                    found.append(tuple(members[i + 1:j]))
        if len(found) == 1:
            return Slot("match", found[0])
        if not found:
            return Slot("lost", reason=f"no place in {anchor.scope} has the anchor's neighbours "
                                       f"{width} statement(s) apart")
        return Slot("ambiguous", reason=f"{len(found)} places in {anchor.scope} have the "
                                        "anchor's neighbours")


def _short(text: str, cap: int = 60) -> str:
    one = " ".join(text.split())
    return one if len(one) <= cap else one[:cap] + "…"


def make_anchor(text: str, line: int, *, file: str, python: Any = None,
                symbol: str | None = None, id: str | None = None) -> Anchor | None:
    """The anchor of the statement holding ``line`` in ``text`` (one-off; build a
    ``StatementIndex`` to make several on the same text)."""
    return StatementIndex.build(text, python=python).anchor_at(line, file=file, symbol=symbol,
                                                               id=id)


def resolve_anchor(anchor: Anchor, text: str, *, python: Any = None) -> Resolution:
    """Resolve ``anchor`` on ``text`` (one-off)."""
    try:
        index = StatementIndex.build(text, python=python, key_kind=anchor.key_kind)
    except AnchorError as exc:
        return Resolution("lost", reason=str(exc))
    return index.resolve(anchor)


__all__ = [
    "END", "MODULE", "START", "Anchor", "AnchorError", "KeyKind", "Resolution", "Slot",
    "Statement", "StatementIndex", "make_anchor", "python_minor", "resolve_anchor",
]
