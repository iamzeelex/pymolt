"""Statement anchors (``pymolt.anchors``): keys without formatting, neighbours, ordinals,
refusals, old syntax, and the frames a capture's ``func`` can name."""
from __future__ import annotations

import shutil
import subprocess
import textwrap

import pytest

from pymolt.anchors import (
    Anchor,
    AnchorError,
    StatementIndex,
    make_anchor,
    resolve_anchor,
)


def src(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def keys(text: str, python=None) -> list[str]:
    return [st.key for st in StatementIndex.build(text, python=python).statements]


def at(index: StatementIndex, line: int, **kw) -> Anchor:
    anchor = index.anchor_at(line, file="m.py", **kw)
    assert anchor is not None, line
    return anchor


# ── keys ──────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("a, b", [
    ("f('a', (b), c,)\n", 'f("a", b, c)\n'),                        # quotes, parens, comma
    ("x = ('a'\n     'b')\n", "x = 'ab'\n"),                          # implicit concatenation
    ("x = f(  # why\n    1,\n    2,\n)\n", "x = f(1, 2)\n"),          # comments, line breaks
    ("x = u'v'\n", "x = 'v'\n"),                                      # black drops ``u``
    ("del (a, b)\n", "del a, b\n"),                                   # black drops the parens
    ("if (a):\n    pass\n", "if a:\n    pass\n"),
    ("class A():\n    pass\n", "class A:\n    pass\n"),
    ("x = 0XFF + 1E3\n", "x = 0xff + 1e3\n"),
    ("a = 1; b = 2\n", "a = 1\nb = 2\n"),
])
def test_formatting_does_not_change_the_key(a, b):
    assert keys(a) == keys(b)


@pytest.mark.parametrize("a, b", [
    ("f(a, b)\n", "f(b, a)\n"),
    ("x = (a,)\n", "x = (a)\n"),                                      # a tuple is not its item
    ("x = 1\n", "x = 1.0\n"),
    ("x = 'a'\n", "x = b'a'\n"),
    ("from a import b\n", "from .a import b\n"),
])
def test_a_different_statement_has_a_different_key(a, b):
    assert keys(a) != keys(b)


def test_a_compound_statement_is_keyed_on_its_header():
    base = src("""
        @cache
        def f(x, y=1):
            return x
        if a:
            b()
        else:
            c()
    """)
    body_changed = base.replace("return x", "return y").replace("c()", "d()")
    assert keys(base)[0] == keys(body_changed)[0]          # the def header
    assert keys(base)[2] == keys(body_changed)[2]          # the if header
    assert keys(base)[0] != keys(base.replace("@cache", "@other"))[0]
    assert keys(base)[0] != keys(base.replace("y=1", "y=2"))[0]


def test_docstrings_are_keyed_on_their_cleaned_text():
    a = src('''
        def f():
            """Summary.

                Indented   detail.   """
            return 1
    ''')
    b = src('''
        def f():
          """Summary.

          Indented detail.
          """
          return 1
    ''')
    assert keys(a) == keys(b)
    assert keys(a) != keys(a.replace("detail", "details"))


def test_keys_are_pinned():
    """The serialization is pymolt's own (not ``ast.dump``, whose output changed in 3.13), so
    a key made on one host equals the key made on another. Changing these values breaks every
    saved plan's anchors."""
    index = StatementIndex.build(src("""
        def combine(s1, s2):
            return s1.append(s2, ignore_index=True)
    """))
    assert [st.key for st in index.statements] == ["d5105b8f4550fc79", "38fea36e95347fe4"]


# ── old and unparseable syntax ─────────────────────────────────────────────────────────────


PY36 = src("""
    async = 1
    def run(loop, async=False):
        await_ = loop
        return async
    """)


@pytest.mark.parametrize("python", ["3.6", (3, 6), None, "3.12"])
def test_async_as_a_name_parses_under_its_feature_version(python):
    index = StatementIndex.build(PY36, python=python)
    assert index.key_kind == "ast"
    anchor = at(index, 4)
    assert (anchor.scope, anchor.func, anchor.stmt) == ("run", "run", "return async")
    edited = "import os\n\n" + PY36.replace("async=False", "async = False")
    assert resolve_anchor(anchor, edited, python="3.6").statement.first == 6


PY2 = src("""
    import os

    def f(a, b=1):
        print "x", a
        if a <> b: return 1
        x = g(i,
              'y',)
        return x
    """)


def test_unparseable_code_falls_back_to_token_keys():
    index = StatementIndex.build(PY2, python="2.7")
    assert index.key_kind == "tokens"
    anchor = at(index, 6)
    assert (anchor.key_kind, anchor.scope, anchor.stmt) == ("tokens", "f", "x = g(i,\n      'y',)")
    # quotes and a trailing comma are normalised, comments and line breaks are dropped
    other = PY2.replace("'y',)", '"y")  # c').replace("print \"x\", a", "print 'x', a")
    assert resolve_anchor(anchor, other).statement.first == 6
    assert resolve_anchor(at(index, 4), other).ok
    # redundant parentheses are not normalised by tokens: the anchor is lost, never moved
    assert not resolve_anchor(anchor, PY2.replace("x = g(i,", "x = (g(i,").replace(
        "'y',)", "'y',))")).ok


def test_token_anchors_resolve_on_a_text_that_now_parses():
    index = StatementIndex.build(PY2)
    anchor = at(index, 8)                                     # return x
    fixed = PY2.replace('print "x", a', 'print("x", a)').replace("a <> b", "a != b")
    assert StatementIndex.build(fixed).key_kind == "ast"
    assert resolve_anchor(anchor, fixed).statement.first == 8


def test_an_ast_anchor_is_lost_on_a_text_that_does_not_parse():
    anchor = make_anchor("x = 1\ny = 2\n", 2, file="m.py")
    res = resolve_anchor(anchor, "x = 1\ny = 2\nprint 'z'\n")
    assert res.status == "lost" and "does not parse" in res.reason


def test_a_text_that_does_not_tokenize_has_no_statements():
    with pytest.raises(AnchorError):
        StatementIndex.build("x = '''unterminated\n")


# ── neighbours, ordinals, refusals ─────────────────────────────────────────────────────────


DUPES = src("""
    def f(df):
        a = load()
        x = df.mean()
        b = save()
        x = df.mean()
        c = done()
    """)


def test_duplicate_statements_are_told_apart_by_their_neighbours():
    index = StatementIndex.build(DUPES)
    first, second = at(index, 3), at(index, 5)
    assert (first.nth, first.count, second.nth, second.count) == (0, 2, 1, 2)
    assert first.key == second.key and first.prev != second.prev
    # a third copy elsewhere: count no longer matches, the neighbours decide
    edited = DUPES.replace("    c = done()", "    c = done()\n    x = df.mean()")
    edited = "import os\n" + edited
    assert resolve_anchor(first, edited).statement.first == 4
    assert resolve_anchor(second, edited).statement.first == 6


TIE = src("""
    def f(df):
        a = load()
        x = df.mean()
        a = load()
        x = df.mean()
        a = load()
    """)


def test_an_ambiguous_tie_is_refused():
    index = StatementIndex.build(TIE)
    first, second = at(index, 3), at(index, 5)
    assert first.prev == second.prev and first.next == second.next
    # the tie the anchors were made in: nth decides, shifted or not
    shifted = "import os\n\n" + TIE
    assert resolve_anchor(first, shifted).statement.first == 5
    assert resolve_anchor(second, shifted).statement.first == 7
    # another copy with the same neighbours: nobody can tell which one was meant
    grown = TIE + "    x = df.mean()\n    a = load()\n"
    for anchor in (first, second):
        res = resolve_anchor(anchor, grown)
        assert res.status == "ambiguous" and res.statement is None
        assert "do not tell them apart" in res.reason


def test_the_one_survivor_of_a_tie_with_other_neighbours_is_refused():
    index = StatementIndex.build(DUPES)
    first = at(index, 3)
    only = DUPES.replace("    x = df.mean()\n    b = save()\n", "    b = save()\n", 1)
    assert resolve_anchor(first, only).status == "ambiguous"
    # with its own neighbours kept, the survivor is the statement
    same_place = DUPES.replace("    b = save()\n    x = df.mean()\n", "    b = save()\n")
    assert resolve_anchor(first, same_place).statement.first == 3


def test_lost_when_the_scope_or_the_statement_is_gone():
    index = StatementIndex.build(DUPES)
    anchor = at(index, 3)
    assert resolve_anchor(anchor, DUPES.replace("def f(", "def g(")).status == "lost"
    gone = resolve_anchor(anchor, DUPES.replace("df.mean()", "df.median()"))
    assert gone.status == "lost" and "x = df.mean()" in gone.reason


def test_a_path_several_definitions_share_names_all_of_them():
    text = src("""
        class A:
            @property
            def x(self):
                return self._x

            @x.setter
            def x(self, v):
                self._x = v
                return self._x
        """)
    index = StatementIndex.build(text)
    getter, setter = at(index, 4), at(index, 9)
    assert (getter.scope, setter.scope) == ("A.x", "A.x")
    assert (getter.nth, getter.count, setter.nth, setter.count) == (0, 2, 1, 2)
    assert index.definitions("A.x") == 2
    reformatted = text.replace("self._x = v", "self._x = (v)  # set")
    assert resolve_anchor(getter, reformatted).statement.first == 4
    assert resolve_anchor(setter, reformatted).statement.first == 9


def test_resolution_survives_lines_moving():
    index = StatementIndex.build(DUPES)
    anchors = [at(index, n) for n in (2, 3, 4, 5, 6)]
    moved = "import os\nimport sys\n\n\n" + DUPES.replace("        ", "    ")
    assert [resolve_anchor(a, moved).statement.first for a in anchors] == [6, 7, 8, 9, 10]
    assert all(a.line in (2, 3, 4, 5, 6) for a in anchors)        # the hints are unchanged


# ── what a statement is ─────────────────────────────────────────────────────────────────────


SHAPES = src('''
    """Module."""
    import os


    @decorator(
        option=1,
    )
    class Model(Base):
        """A model."""

        limit = compute(
            1,
            2,
        )

        def method(self, frame):
            if frame.empty: return frame.append(other)
            try:
                return frame.mean()
            except (ValueError,
                    TypeError) as exc:
                raise Wrapped(exc)
            finally:
                close()
    ''')


def test_a_decorator_line_anchors_the_decorated_header():
    index = StatementIndex.build(SHAPES)
    for line in (5, 6, 7, 8):
        anchor = at(index, line)
        assert anchor.stmt.startswith("@decorator(") and anchor.stmt.endswith("class Model(Base):")
        assert (anchor.scope, anchor.func, anchor.line) == ("<module>", "<module>", line)


def test_any_line_of_a_multi_line_statement_anchors_it():
    index = StatementIndex.build(SHAPES)
    anchors = [at(index, n) for n in (11, 12, 13, 14)]
    assert {a.key for a in anchors} == {anchors[0].key}
    assert [a.site_text for a in anchors] == ["limit = compute(", "1,", "2,", ")"]
    clause = at(index, 21)
    assert clause.stmt == "except (ValueError,\n        TypeError) as exc:"
    assert at(index, 20).key == clause.key


def test_class_bodies_are_their_own_scope():
    index = StatementIndex.build(SHAPES)
    doc, limit, method = at(index, 9), at(index, 11), at(index, 16)
    assert [(a.scope, a.func) for a in (doc, limit, method)] == [("Model", "Model")] * 3
    assert (doc.prev, method.next) == ("^", "$")
    body = at(index, 19)
    assert (body.scope, body.func) == ("Model.method", "method")


def test_one_line_statements_are_told_apart_by_the_symbol():
    index = StatementIndex.build(SHAPES)
    assert at(index, 17).stmt == "if frame.empty:"
    assert at(index, 17, symbol="pandas.Series.append").stmt == "return frame.append(other)"
    two = StatementIndex.build("a = f(); b = g()\n")
    assert at(two, 1, symbol="mod.g").stmt == "b = g()"
    assert at(two, 1).stmt == "a = f()"


def test_lines_no_statement_owns():
    index = StatementIndex.build(SHAPES)
    assert index.anchor_at(3, file="m.py") is None              # blank
    assert index.anchor_at(23, file="m.py") is None             # finally:
    assert [st.first for st in index.statements_in(21, 24)] == [20, 22, 24]


def test_form_feeds_do_not_shift_lines():
    text = "import os\n\x0c\ndef f():\n    return os.sep\n"
    anchor = make_anchor(text, 4, file="m.py")
    assert anchor.stmt == "return os.sep" and anchor.site_text == "return os.sep"


# ── func and frames: what a capture records ──────────────────────────────────────────────────


COMPS = src("""
    def f(xs):
        a = [g(x) for x in xs]
        b = list(g(x) for x in xs)
        c = (lambda: g(1))()
        d = [g(y) for y in [g(0)]]
        e = g(2)
    class C:
        items = [g(x) for x in range(2)]
    """)


def test_comprehension_frames_join_on_311_and_312():
    index = StatementIndex.build(COMPS)
    listcomp, genexpr, lam, plain, klass = (at(index, n) for n in (2, 3, 4, 6, 8))
    assert listcomp.joins("<listcomp>") and listcomp.joins("f")     # 3.11 / 3.12 (PEP 709)
    assert genexpr.joins("<genexpr>") and lam.joins("<lambda>")      # frames on both
    assert klass.joins("<listcomp>") and klass.joins("C")
    assert plain.frames == [] and not plain.joins("<listcomp>")
    assert not listcomp.joins("other") and not listcomp.joins(None)


PROBE = src("""
    import sys
    SEEN = []
    def g(v=None):
        frame = sys._getframe(1)
        SEEN.append((frame.f_code.co_name, frame.f_lineno))
        return v
    """)


@pytest.mark.parametrize("python", ["python3.11", "python3.12"])
def test_comprehension_frames_join_real_captures(python, tmp_path):
    exe = shutil.which(python)
    if exe is None:
        pytest.skip(f"{python} is not on PATH")
    script = tmp_path / "probe.py"
    offset = PROBE.count("\n")
    script.write_text(PROBE + COMPS + "f([1, 2])\nprint(SEEN)\n")
    out = subprocess.run([exe, str(script)], capture_output=True, text=True, check=True)
    seen = eval(out.stdout)                                     # noqa: S307 - our own output
    index = StatementIndex.build(COMPS)
    assert {name for name, _ in seen} >= ({"<listcomp>", "<genexpr>", "<lambda>"}
                                          if python == "python3.11" else {"f", "C", "<genexpr>"})
    for name, line in seen:
        assert at(index, line - offset).joins(name), (name, line - offset)


# ── slots (apply rule 4) ──────────────────────────────────────────────────────────────────────


def test_a_slot_is_the_statement_between_the_anchors_neighbours():
    before = src("""
        def f(s1, s2):
            a = prepare()
            r = s1.append(s2)
            return r
        """)
    anchor = at(StatementIndex.build(before), 3)
    after = StatementIndex.build(before.replace("s1.append(s2)", "pd.concat([s1, s2])"))
    slot = after.slot(anchor)
    assert slot.ok and [st.text for st in slot.statements] == ["r = pd.concat([s1, s2])"]
    deleted = StatementIndex.build(before.replace("    r = s1.append(s2)\n", ""))
    assert deleted.slot(anchor, width=0).ok and deleted.slot(anchor, width=0).statements == ()
    assert not deleted.slot(anchor).ok
    twice = StatementIndex.build(before.replace("    return r\n",
                                                "    return r\n    a = prepare()\n    x = 1\n"
                                                "    return r\n"))
    assert twice.slot(anchor).status == "ambiguous"
    assert StatementIndex.build(before.replace("def f(", "def g(")).slot(anchor).status == "lost"


def test_anchors_serialize():
    anchor = at(StatementIndex.build(COMPS), 2, symbol="mod.g")
    again = Anchor.model_validate_json(anchor.model_dump_json())
    assert again == anchor and again.frames == ["<listcomp>"] and again.symbol == "mod.g"


def test_headers_end_at_their_colon():
    index = StatementIndex.build(src("""
        def f(value):
            match value:
                case [first, *rest]:
                    return first
                case {"k": v} if v: return v
            a = 1; b = g(
                2)
            @dec
            def h(): ...
        """))
    shown = [(st.first, st.last, st.text) for st in index.statements]
    assert shown == [
        (1, 1, "def f(value):"), (2, 2, "match value:"), (3, 3, "case [first, *rest]:"),
        (4, 4, "return first"), (5, 5, 'case {"k": v} if v:'), (5, 5, "return v"),
        (6, 6, "a = 1"), (6, 7, "b = g(\n    2)"), (8, 9, "@dec\ndef h():"), (9, 9, "..."),
    ]


def test_indexing_the_projects_code_does_not_warn(recwarn):
    StatementIndex.build('pattern = "\\d+"\n')                   # an invalid escape
    StatementIndex.build('print "\\d+"\n')                       # the token fallback
    assert not [w for w in recwarn if issubclass(w.category, SyntaxWarning | DeprecationWarning)]
