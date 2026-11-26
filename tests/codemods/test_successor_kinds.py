"""The successor codemods (``pymolt.codemods.successors``): rename-keyword, rename-member and
forward-template. Binding-aware, gated by anchors, and checked against both signatures."""

from __future__ import annotations

import textwrap

from pymolt.anchors import StatementIndex
from pymolt.codemods.models import CodemodPattern, recipe_key
from pymolt.codemods.successors import apply_successor_pattern, binds_same, site_blocked

POK, KW = "POSITIONAL_OR_KEYWORD", "KEYWORD_ONLY"
PERCENTILE = [["a", POK, None], ["q", POK, None], ["method", POK, "'linear'"],
              ["interpolation", KW, "None"]]


def _kw(src: str, **extra) -> str:
    pattern = CodemodPattern(
        old_qualname="numpy.percentile", new_qualname="numpy.percentile", kind="rename-keyword",
        params={"interpolation": "method"},
        template={"old_shape": PERCENTILE, "new_shape": PERCENTILE[:3], **extra})
    return apply_successor_pattern(textwrap.dedent(src), pattern, path="app/x.py")[0]


def test_rename_keyword_through_the_import_binding():
    out = _kw("""
        import numpy as np
        a = np.percentile(x, 50, interpolation="lower")
        b = np.percentile(x, 50, **{"interpolation": "lower"})
    """)
    assert 'np.percentile(x, 50, method="lower")' in out
    assert '**{"method": "lower"}' in out


def test_rename_keyword_leaves_shadows_fallbacks_strings_and_opaque_mappings():
    src = """
        import numpy as np
        try:
            from numpy import percentile
        except ImportError:
            from fallback import percentile
        a = percentile(x, 50, interpolation="lower")
        b = np.percentile(x, 50, **opts, interpolation="lower")
        c = getattr(np, "percentile")(x, 50, interpolation="lower")
        d = "interpolation"
        e = np.percentile(x, 50, interpolation="lower", method="nearest")

        def f(np):
            return np.percentile(x, 50, interpolation="lower")
    """
    assert _kw(src) == textwrap.dedent(src)


def test_rename_keyword_in_a_partial():
    out = _kw("""
        import functools
        import numpy as np
        p = functools.partial(np.percentile, interpolation="lower")
    """)
    assert 'functools.partial(np.percentile, method="lower")' in out


def test_rename_keyword_is_blocked_by_another_change_the_call_meets():
    src = """
        import numpy as np
        a = np.percentile(x, 50, interpolation="lower")
    """
    blocked = [{"param": "keepdims", "kind": "DEFAULT", "old": "False", "new": "True"}]
    shape = [*PERCENTILE, ["keepdims", POK, "False"]]
    pattern = CodemodPattern(
        old_qualname="numpy.percentile", new_qualname="numpy.percentile", kind="rename-keyword",
        params={"interpolation": "method"},
        template={"old_shape": shape, "new_shape": shape, "blocked": blocked})
    assert apply_successor_pattern(textwrap.dedent(src), pattern)[1] == 0


def _anchor(src: str, line: int, file: str = "app/x.py"):
    return StatementIndex.build(src).anchor_at(line, file=file)


def test_a_member_rename_rewrites_only_at_its_anchored_statements():
    src = textwrap.dedent("""\
        def a(df):
            return df.groupby("store").sum()

        def b(df):
            return df.groupby("region").sum()
        """)
    shape = [["self", POK, None], ["by", POK, None], ["more_by", "VAR_POSITIONAL", None],
             ["maintain_order", KW, "False"]]
    pattern = CodemodPattern(
        old_qualname="pkg.DataFrame.groupby", new_qualname="pkg.DataFrame.group_by",
        kind="rename-member", sites=[_anchor(src, 2)],
        template={"old_shape": shape, "callee_old_shape": shape,
                  "callee_new_shape": [["self", POK, None], ["by", "VAR_POSITIONAL", None],
                                       ["maintain_order", KW, "False"]]})
    out, n = apply_successor_pattern(src, pattern, path="app/x.py")
    assert n == 1
    assert 'df.group_by("store")' in out and 'df.groupby("region")' in out
    # another file's anchor names no statement here
    assert apply_successor_pattern(src, pattern, path="app/y.py")[1] == 0


def test_a_member_rename_skips_two_uses_on_one_statement_and_stores():
    src = textwrap.dedent("""\
        def a(df, other):
            return df.groupby("s"), other.groupby("t")

        def b(df):
            df.groupby = None
        """)
    pattern = CodemodPattern(
        old_qualname="pkg.DataFrame.groupby", new_qualname="pkg.DataFrame.group_by",
        kind="rename-member", sites=[_anchor(src, 2), _anchor(src, 5)],
        template={"attr": True})
    out, n = apply_successor_pattern(src, pattern, path="app/x.py")
    assert n == 0 and out == src


def test_a_member_rename_refuses_a_call_that_binds_differently():
    src = "def a(df):\n    return df.old(1, 2)\n"
    old = [["self", POK, None], ["x", POK, None], ["y", POK, None]]
    new = [["self", POK, None], ["y", POK, None], ["x", POK, None]]
    pattern = CodemodPattern(old_qualname="pkg.C.old", new_qualname="pkg.C.new",
                             kind="rename-member", sites=[_anchor(src, 2)],
                             template={"old_shape": old, "callee_old_shape": old,
                                       "callee_new_shape": new})
    assert apply_successor_pattern(src, pattern, path="app/x.py")[1] == 0


def test_forward_template_follows_a_condition_and_a_chain():
    """polars 1.x ``count()``: without columns it is ``F.len().alias("count")``."""
    template = {"callee": "polars.len", "args": [], "kwargs": [],
                "chain": [["alias", [{"c": "count"}], []]], "cond": {"falsy": "columns"},
                "old_shape": [["columns", "VAR_POSITIONAL", None]], "callee_old_shape": [],
                "callee_new_shape": []}
    pattern = CodemodPattern(old_qualname="polars.functions.lazy.count",
                             new_qualname="polars.len", kind="forward-template",
                             aliases=["polars.count"], template=template)
    src = "import polars as pl\nx = pl.count()\ny = pl.count('a')\n"
    out, n = apply_successor_pattern(src, pattern)
    assert n == 1
    assert "x = pl.len().alias('count')" in out and "y = pl.count('a')" in out


def test_forward_template_writes_the_old_default_the_successor_changed():
    """polars 1.x ``with_row_count(name='row_nr')`` forwards to ``with_row_index`` whose
    default name is 'index'."""
    old = [["self", POK, None], ["name", POK, "'row_nr'"], ["offset", POK, "0"]]
    new = [["self", POK, None], ["name", POK, "'index'"], ["offset", POK, "0"]]
    src = textwrap.dedent("""\
        def a(df):
            return df.with_row_count()

        def b(df):
            return df.with_row_count("row_id")
        """)
    pattern = CodemodPattern(
        old_qualname="pkg.DataFrame.with_row_count", new_qualname="pkg.DataFrame.with_row_index",
        kind="forward-template", sites=[_anchor(src, 2), _anchor(src, 5)],
        template={"callee": "pkg.DataFrame.with_row_index", "member_leaf": "with_row_count",
                  "args": [{"p": "name"}, {"p": "offset"}], "kwargs": [], "old_shape": old,
                  "callee_old_shape": new, "callee_new_shape": new})
    out, n = apply_successor_pattern(src, pattern, path="app/x.py")
    assert n == 2
    assert "df.with_row_index('row_nr')" in out and 'df.with_row_index("row_id")' in out


def test_forward_template_keeps_only_the_keywords_whose_defaults_differ():
    old = [["self", POK, None], ["include", KW, "None"], ["by_alias", KW, "False"],
           ["exclude_none", KW, "False"]]
    callee = [["self", POK, None], ["include", KW, "None"], ["by_alias", KW, "None"],
              ["exclude_none", KW, "False"], ["mode", KW, "'python'"]]
    src = "def a(user):\n    return user.dict(exclude_none=True)\n"
    pattern = CodemodPattern(
        old_qualname="pkg.BaseModel.dict", new_qualname="pkg.BaseModel.model_dump",
        kind="forward-template", sites=[_anchor(src, 2)],
        template={"callee": "pkg.BaseModel.model_dump", "member_leaf": "dict", "args": [],
                  "kwargs": [["include", {"p": "include"}], ["by_alias", {"p": "by_alias"}],
                             ["exclude_none", {"p": "exclude_none"}]],
                  "old_shape": old, "callee_old_shape": callee, "callee_new_shape": callee})
    out, n = apply_successor_pattern(src, pattern, path="app/x.py")
    assert n == 1 and "user.model_dump(by_alias=False, exclude_none=True)" in out


def test_binds_same_and_site_blocked():
    a = [["x", POK, None], ["y", POK, "1"]]
    assert binds_same(1, ["y"], a, a, False) is None
    assert "binds to" in binds_same(2, [], a, [["y", POK, None], ["x", POK, "1"]], False)
    assert "changes" in binds_same(1, [], a, [["x", POK, None], ["y", POK, "2"]], False)
    blocked = [{"param": "y", "kind": "SIGNATURE", "old": POK, "new": KW}]
    assert site_blocked(1, ["y"], a, False, blocked, [["x", POK, None], ["y", KW, "1"]]) is None
    assert site_blocked(2, [], a, False, blocked, [["x", POK, None], ["y", KW, "1"]])


def test_a_keyword_rename_has_its_own_recipe_key():
    p = CodemodPattern(old_qualname="m.f", new_qualname="m.f", kind="rename-keyword",
                       params={"b": "c", "a": "d"})
    assert recipe_key(p) == "rename-keyword[a=d,b=c]:m.f→m.f"


def test_a_rename_call_whose_successor_names_a_parameter_otherwise_refuses_keyword_calls():
    """Django 2.2 ``url(regex, view)`` forwards to ``re_path(route, view)``: ``url(regex=...)``
    would not bind to ``re_path``. A file with such a call is left whole."""
    from pymolt.codemods.apply import apply_pattern

    p = CodemodPattern(old_qualname="pkg.conf.url", new_qualname="pkg.urls.re_path",
                       kind="rename-call", template={"forbid_keywords": ["regex"]})
    ok = "from pkg.conf import url\nurls = [url(r'^a$', view)]\n"
    out, n = apply_pattern(ok, p)
    assert n == 2 and "re_path(r'^a$', view)" in out
    bad = "from pkg.conf import url\nurls = [url(r'^a$', view), url(regex=r'^b$', view=v)]\n"
    assert apply_pattern(bad, p) == (bad, 0)


# ── the sites a rewrite may touch (review round) ─────────────────────────────────────────

SHIFT_OLD = [["self", POK, None], ["periods", POK, "1"]]
SHIFT_NEW = [["self", POK, None], ["n", POK, "1"]]


def _shift(src: str, line: int) -> CodemodPattern:
    return CodemodPattern(
        old_qualname="pkg.Frame.shift", new_qualname="pkg.Frame.shift", kind="rename-keyword",
        params={"periods": "n"}, sites=[_anchor(src, line)],
        template={"old_shape": SHIFT_OLD, "new_shape": SHIFT_NEW, "member_leaf": "shift"})


def test_a_member_keyword_rename_needs_the_member_once_on_the_statement():
    """The baseline saw one receiver on the line; another receiver's ``shift(periods=)``
    (pandas has no ``n``) is no proven call."""
    src = "def f(a, b):\n    return a.shift(periods=1) + b.shift(periods=2)\n"
    assert apply_successor_pattern(src, _shift(src, 2), path="app/x.py") == (src, 0)
    one = "def f(a):\n    return a.shift(periods=1)\n"
    assert "a.shift(n=1)" in apply_successor_pattern(one, _shift(one, 2), path="app/x.py")[0]


def test_a_compound_statement_site_is_its_header_only():
    src = textwrap.dedent("""\
        def f(a, b):
            for row in a.shift(periods=1):
                b.shift(periods=2)
        """)
    out, n = apply_successor_pattern(src, _shift(src, 2), path="app/x.py")
    assert n == 1 and "a.shift(n=1)" in out and "b.shift(periods=2)" in out


def test_a_binding_must_hold_on_every_path():
    src = textwrap.dedent("""\
        import sys
        if sys.flag:
            import numpy as np
        else:
            import mylib as np
        a = np.percentile(x, 50, interpolation="lower")
        """)
    assert _kw(src) == src


def test_a_keyword_rename_never_names_a_parameter_bound_by_position():
    old = [["data", POK, None], ["height", POK, "5"], ["size", POK, "None"]]
    new = [["data", POK, None], ["height", POK, "5"]]
    p = CodemodPattern(old_qualname="pkg.plot", new_qualname="pkg.plot", kind="rename-keyword",
                       params={"size": "height"},
                       template={"old_shape": old, "new_shape": new})
    src = "import pkg\ny = pkg.plot(d, 4, size=3)\n"
    assert apply_successor_pattern(src, p) == (src, 0)
    ok = "import pkg\ny = pkg.plot(d, size=3)\n"
    assert "pkg.plot(d, height=3)" in apply_successor_pattern(ok, p)[0]


def test_an_alias_guarded_by_not_none_renames_only_a_literal():
    """``if size is not None: height = size``: ``size=s`` did nothing when ``s`` was None."""
    old = [["data", POK, None], ["height", POK, "5"], ["size", POK, "None"]]
    p = CodemodPattern(old_qualname="pkg.plot", new_qualname="pkg.plot", kind="rename-keyword",
                       params={"size": "height"},
                       template={"old_shape": old, "new_shape": old[:2],
                                 "guards": {"size": "not_none"}})
    assert apply_successor_pattern("import pkg\ny = pkg.plot(d, size=s)\n", p)[1] == 0
    assert apply_successor_pattern("import pkg\ny = pkg.plot(d, size=None)\n", p)[1] == 0
    assert apply_successor_pattern("import pkg\ny = pkg.plot(d, size=3)\n", p)[1] == 1
    assert apply_successor_pattern('import pkg\ny = pkg.plot(d, size="%s" % n)\n', p)[1] == 1
    assert apply_successor_pattern('import pkg\ny = pkg.plot(d, size=f"{n}")\n', p)[1] == 1
    assert apply_successor_pattern('import pkg\ny = pkg.plot(d, size=n % 2)\n', p)[1] == 0


def test_a_not_none_alias_onto_a_none_default_renames_any_value():
    """flask's ``if attachment_filename is not None: download_name = attachment_filename`` with
    ``download_name=None``: a skipped None and a None passed to the new name bind alike."""
    old = [["path", POK, None], ["download_name", POK, "None"],
           ["attachment_filename", POK, "None"], ["etag", POK, "True"], ["add_etags", POK, "None"]]
    new = [["path", POK, None], ["download_name", POK, "None"], ["etag", POK, "True"]]
    p = CodemodPattern(old_qualname="pkg.send", new_qualname="pkg.send", kind="rename-keyword",
                       params={"attachment_filename": "download_name", "add_etags": "etag"},
                       template={"old_shape": old, "new_shape": new,
                                 "guards": {"attachment_filename": "not_none",
                                            "add_etags": "not_none"}})
    out, n = apply_successor_pattern("import pkg\ny = pkg.send(b, attachment_filename=name)\n", p)
    assert n == 1 and "download_name=name" in out
    # etag defaults to True: add_etags=None kept it True, etag=None would not
    assert apply_successor_pattern("import pkg\ny = pkg.send(b, add_etags=v)\n", p)[1] == 0


def _fwd(src: str, args: list, sig: list, callee: list, kwargs: list | None = None):
    p = CodemodPattern(old_qualname="pkg.C.old", new_qualname="pkg.C.new",
                       kind="forward-template", sites=[_anchor(src, 2)],
                       template={"callee": "pkg.C.new", "member_leaf": "old", "args": args,
                                 "kwargs": kwargs or [], "old_shape": sig,
                                 "callee_old_shape": callee, "callee_new_shape": callee})
    return apply_successor_pattern(src, p, path="app/x.py")


def test_forward_template_keeps_the_evaluation_of_the_sites_arguments():
    sig = [["self", POK, None], ["a", POK, None], ["b", POK, None]]
    swapped = [{"p": "b"}, {"p": "a"}]
    # reordered: f() then g() would become g() then f()
    assert _fwd("def h(df):\n    return df.old(f(), g())\n", swapped, sig, sig)[1] == 0
    # names and literals may move
    out, n = _fwd("def h(df, x):\n    return df.old(x, 2)\n", swapped, sig, sig)
    assert n == 1 and "df.new(2, x)" in out
    # repeated, or dropped
    two = [["self", POK, None], ["x", POK, None], ["y", POK, None]]
    one = [["self", POK, None], ["a", POK, None]]
    assert _fwd("def h(df, it):\n    return df.old(next(it))\n", [{"p": "a"}, {"p": "a"}],
                one, two)[1] == 0
    assert _fwd("def h(df):\n    return df.old(g(), h())\n", [{"p": "a"}], sig,
                [["self", POK, None], ["x", POK, None]])[1] == 0


def test_a_changed_class_default_is_no_equal_default():
    assert binds_same(1, [], [["x", POK, None], ["dtype", POK, "<class 'float'>"]],
                      [["x", POK, None], ["dtype", POK, "<class 'int'>"]], False)
    assert binds_same(1, [], [["x", POK, None], ["f", POK, "<function g at 0x10>"]],
                      [["x", POK, None], ["f", POK, "<function g at 0x20>"]], False) is None
