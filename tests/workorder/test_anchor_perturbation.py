"""The anchor perturbation guard (work order, section 8): reformatting never moves an anchor.

Each corpus text is reformatted (black, ``ruff format`` with other quotes, widths, indents and
trailing-comma handling, a black -> ``ruff format`` round trip), and perturbed by hand
(redundant parentheses, comments and blank lines, swapped quotes, re-indenting). Every
statement must keep its key, and every anchor made on the original must resolve to the
statement at the same place in the perturbed text: never to another statement, never lost.
"""
from __future__ import annotations

import io
import re
import subprocess
import sys
import textwrap
import tokenize
from collections.abc import Callable
from pathlib import Path

import libcst as cst
import pytest

from pymolt.anchors import StatementIndex

ROOT = Path(__file__).resolve().parents[2]

CORPUS = textwrap.dedent('''
    """A corpus of the shapes anchors must survive reformatting in."""
    from __future__ import annotations

    import os, sys
    from collections import (
        OrderedDict,
        defaultdict,
    )

    LIMIT = (10)
    NAMES = ['a', "b", u'c', 'd' 'e']


    @decorator(option=1, other='x',)
    @second
    class Model(Base, metaclass=Meta):
        """A model.

            With an indented, trailing-space detail.
        """

        limit = compute(1,
                        2)
        items = [f(x) for x in range(LIMIT) if x % 2]

        @property
        def value(self):
            return self._value

        @value.setter
        def value(self, new):
            self._value = new

        async def fetch(self, session, *args, key=None, **kwargs):
            async with session.get(self.url, params={'a': 1, 'b': 2,}) as response:
                async for chunk in response.content:
                    yield chunk
            await session.close()

    def combine(s1, s2, *, ignore_index=True):
        if s1 is None: return s2
        if (s2 is None):
            return s1
        a = load(); b = save()
        result = s1.append(s2, ignore_index=ignore_index, verify_integrity=False, sort=False)
        x = df.mean()
        b = save()
        x = df.mean()
        del (a, b)
        total = first_value + second_value + third_value + fourth_value + fifth_value + sixth
        return (result)

    def flow(items):
        global LIMIT
        out = {k: v for k, v in items if v}
        gen = (i * 2 for i in items)
        fn = lambda x: x + 1
        if (n := len(items)) > LIMIT:
            raise ValueError(f"too many: {n!r} > {LIMIT}")
        try:
            value = items[0]
        except (KeyError,
                IndexError) as exc:
            raise RuntimeError('empty') from exc
        except Exception:
            pass
        else:
            value = value * 2
        finally:
            cleanup()
        while value < 10:
            value += 1
        else:
            value = 0
        for i, item in enumerate(items):
            if i: continue
        with open(path) as fh, open(other) as gh:
            data = fh.read() + \\
                gh.read()
        match value:
            case [first, *rest]:
                return first
            case {'key': inner} if inner:
                return inner
            case _:
                return None

    if sys.version_info >= (3, 8):
        def compat():
            return 1
    else:
        def compat():
            return 2

    if __name__ == "__main__":
        print((combine(None, None)))
''').lstrip("\n")

# Code only the 3.6 grammar reads (``async`` as a name): no formatter can parse it, so only
# the hand perturbations apply.
PY36 = textwrap.dedent('''
    """Old asyncio-era code."""
    import asyncio


    def schedule(coro, loop=None, async=False):
        """Run it."""
        if async:
            return asyncio.async(coro, loop=loop)
        result = loop.run_until_complete(coro)
        return result
''').lstrip("\n")

# Code no grammar reads: token keys.
PY2 = textwrap.dedent('''
    import os


    def report(items, out=None):
        print "items:", len(items)
        if out <> None: out.write('x')
        for item in items:
            print >>out, item.name, 'and', item.value,
        total = sum([i.value for i in items],
                    0,)
        return total
''').lstrip("\n")


# ── perturbations ──────────────────────────────────────────────────────────────────────────


def _black(**mode):
    black = pytest.importorskip("black")

    def run(text: str) -> str:
        return black.format_str(text, mode=black.Mode(**mode))
    return run


def _ruff(*config: str):
    def run(text: str) -> str:
        args = [sys.executable, "-m", "ruff", "format", "--isolated"]
        for item in config:
            args += ["--config", item]
        try:
            done = subprocess.run([*args, "-"], input=text, capture_output=True, text=True,
                                  timeout=60)
        except (OSError, subprocess.TimeoutExpired) as exc:
            pytest.skip(f"ruff unavailable: {exc}")
        if done.returncode != 0:
            pytest.skip(f"ruff format failed: {done.stderr.strip()[:200]}")
        return done.stdout
    return run


class _Parens(cst.CSTTransformer):
    """Redundant parentheses around call arguments, return values and assigned values."""

    @staticmethod
    def _wrap(node):
        if node is None or not hasattr(node, "lpar"):
            return node
        return node.with_changes(lpar=[cst.LeftParen(), *node.lpar],
                                 rpar=[*node.rpar, cst.RightParen()])

    def leave_Arg(self, original, updated):
        return updated.with_changes(value=self._wrap(updated.value))

    def leave_Return(self, original, updated):
        return updated.with_changes(value=self._wrap(updated.value))

    def leave_Assign(self, original, updated):
        return updated.with_changes(value=self._wrap(updated.value))


def parenthesize(text: str) -> str:
    return cst.parse_module(text).visit(_Parens()).code


def _tokens(text: str) -> list[tokenize.TokenInfo]:
    return list(tokenize.generate_tokens(io.StringIO(text).readline))


def _edit(text: str, edits: list[tuple[int, int, int, int, str]]) -> str:
    """Replace (row, col)..(row, col) spans, given in tokenize coordinates."""
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    for r1, c1, r2, c2, new in sorted(edits, reverse=True):
        a, b = offsets[r1 - 1] + c1, offsets[r2 - 1] + c2
        text = text[:a] + new + text[b:]
    return text


def comments_and_blank_lines(text: str) -> str:
    """A comment line and a blank line before every logical line, and a trailing comment on
    every line that ends code (inside brackets too)."""
    edits = []
    starts_line = True
    for tok in _tokens(text):
        if tok.type in (tokenize.NEWLINE, tokenize.NL) and tok.string:
            if tok.line[:tok.start[1]].strip() and "#" not in tok.line:
                edits.append((*tok.start, *tok.start, "  # tail"))
            starts_line = tok.type == tokenize.NEWLINE or starts_line
            continue
        if tok.type in (tokenize.INDENT, tokenize.DEDENT, tokenize.COMMENT, tokenize.ENCODING,
                        tokenize.ENDMARKER):
            continue
        if starts_line:
            indent = tok.line[:len(tok.line) - len(tok.line.lstrip())]
            edits.append((tok.start[0], 0, tok.start[0], 0, f"{indent}# perturbed\n\n"))
            starts_line = False
    return _edit(text, edits)


_PLAIN = re.compile(r"^([rRbBuU]?)(['\"])([^'\"\\\n]*)\2$")


def swap_quotes(text: str) -> str:
    edits = []
    for tok in _tokens(text):
        m = _PLAIN.match(tok.string) if tok.type == tokenize.STRING else None
        if m:
            quote = '"' if m[2] == "'" else "'"
            edits.append((*tok.start, *tok.end, f"{m[1]}{quote}{m[3]}{quote}"))
    return _edit(text, edits)


def drop_trailing_commas(text: str) -> str:
    """A comma before a closing bracket dropped, unless it makes a one-element tuple."""
    toks = [t for t in _tokens(text) if t.type not in (tokenize.NL, tokenize.COMMENT)]
    edits, commas = [], [0]                     # top-level commas per open bracket
    for t, nxt in zip(toks, toks[1:], strict=False):
        if t.type != tokenize.OP:
            continue
        if t.string in "([{":
            commas.append(0)
        elif t.string in ")]}" and len(commas) > 1:
            commas.pop()
        elif t.string == ",":
            commas[-1] += 1
            if nxt.string in (")", "]", "}") and commas[-1] > 1:
                edits.append((*t.start, *t.end, ""))
    return _edit(text, edits)


def reindent(text: str) -> str:
    """Four-space indentation to two (continuation lines and docstrings included)."""
    out = []
    for line in text.splitlines(keepends=True):
        n = len(line) - len(line.lstrip(" "))
        out.append(" " * (n // 2) + line[n:] if n % 4 == 0 else line)
    return "".join(out)


def chain(*steps: Callable[[str], str]) -> Callable[[str], str]:
    def run(text: str) -> str:
        for step in steps:
            text = step(text)
        return text
    return run


FORMATTERS: dict[str, Callable[[str], str]] = {
    "black": _black(),
    "black-narrow-no-magic-comma": _black(line_length=40, magic_trailing_comma=False),
    "ruff": _ruff(),
    "ruff-single-quotes-indent-2-narrow": _ruff("format.quote-style='single'", "indent-width=2",
                                                "line-length=40"),
    "ruff-tabs-skip-magic-comma": _ruff("format.indent-style='tab'",
                                        "format.skip-magic-trailing-comma=true",
                                        "line-length=120"),
    "black-ruff-round-trip": chain(_black(), _ruff("format.quote-style='single'"), _black()),
}
BY_HAND: dict[str, Callable[[str], str]] = {
    "parenthesized": parenthesize,
    "comments-and-blank-lines": comments_and_blank_lines,
    "quotes-swapped": swap_quotes,
    "trailing-commas-dropped": drop_trailing_commas,
    "reindented": reindent,
    "everything": chain(parenthesize, _black(line_length=50), swap_quotes,
                        comments_and_blank_lines),
}
# What applies where: the formatters and LibCST need a text the host grammar reads.
TOKEN_SAFE = ("comments-and-blank-lines", "quotes-swapped", "trailing-commas-dropped",
              "reindented")


def _corpus() -> dict[str, str]:
    files = {"corpus": CORPUS}
    for rel in ("pymolt/verify/predict.py",):                  # real code, 1,500 lines
        files[rel] = (ROOT / rel).read_text()
    return files


def _check(original: str, perturbed: str, python=None) -> None:
    before = StatementIndex.build(original, python=python)
    after = StatementIndex.build(perturbed, python=python)
    assert after.key_kind == before.key_kind
    shape = [(st.scope, st.key) for st in before.statements]
    moved = [(st.scope, st.key) for st in after.statements]
    if shape != moved:
        bad = next(i for i, (a, b) in enumerate(zip(shape, moved, strict=False)) if a != b) \
            if len(shape) == len(moved) else min(len(shape), len(moved))
        old = before.statements[bad] if bad < len(shape) else None
        new = after.statements[bad] if bad < len(moved) else None
        pytest.fail(f"statement {bad} changed key: {old and old.text!r} -> {new and new.text!r}")
    for st, twin in zip(before.statements, after.statements, strict=True):
        anchor = before.anchor(st, file="m.py")
        res = after.resolve(anchor)
        assert res.ok, (st.first, st.text, res.reason)
        assert res.statement is twin, (st.first, st.text, res.statement.first, twin.first)


@pytest.mark.parametrize("name", [*FORMATTERS, *BY_HAND])
@pytest.mark.parametrize("corpus", list(_corpus()))
def test_reformatting_never_moves_an_anchor(corpus, name):
    original = _corpus()[corpus]
    perturb = FORMATTERS.get(name) or BY_HAND[name]
    perturbed = perturb(original)
    assert perturbed != original
    _check(original, perturbed)


@pytest.mark.parametrize("name", TOKEN_SAFE)
def test_old_syntax_anchors_survive_perturbation(name):
    perturbed = BY_HAND[name](PY36)
    index = StatementIndex.build(PY36, python="3.6")
    assert index.key_kind == "ast"
    _check(PY36, perturbed, python="3.6")


@pytest.mark.parametrize("name", TOKEN_SAFE)
def test_token_anchors_survive_perturbation(name):
    perturbed = BY_HAND[name](PY2)
    assert StatementIndex.build(PY2).key_kind == "tokens"
    _check(PY2, perturbed)
