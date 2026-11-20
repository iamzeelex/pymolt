"""The python-scope appliers: attr rewrites with imports, cross-module paths, grouped
from-imports, and the guard line allowlist."""
from __future__ import annotations

from pymolt.codemods.apply import apply_pattern, apply_patterns
from pymolt.codemods.models import CodemodPattern


def _p(kind, old, new, **kw):
    return CodemodPattern(old_qualname=old, new_qualname=new, kind=kind, **kw)


ABC_ATTR = _p("rewrite-attr", "collections.Iterable", "collections.abc.Iterable",
              requires_import="collections.abc", scope="python")
RELOAD = _p("replace-attr-path", "imp.reload", "importlib.reload", scope="python")
CURRENT = _p("replace-attr-path", "asyncio.Task.current_task", "asyncio.current_task",
             scope="python")


def test_rewrite_attr_adds_the_import_it_needs():
    out, n = apply_pattern("import collections\nx = collections.Iterable\n", ABC_ATTR)
    assert n == 1
    assert "x = collections.abc.Iterable" in out
    assert "import collections.abc" in out


def test_rewrite_attr_is_binding_aware():
    src = "import numpy as collections\nx = collections.Iterable\n"
    out, n = apply_pattern(src, ABC_ATTR)
    assert (out, n) == (src, 0)


def test_replace_attr_path_swaps_the_import():
    out, n = apply_pattern("import imp\nimp.reload(m)\n", RELOAD)
    assert n == 1 and "importlib.reload(m)" in out
    assert "import imp\n" not in out and "import importlib" in out


def test_replace_attr_path_keeps_an_import_still_in_use():
    out, _ = apply_pattern("import imp\nimp.reload(m)\nimp.find_module('x')\n", RELOAD)
    assert "import imp\n" in out and "import importlib" in out


def test_replace_attr_path_same_root_keeps_the_alias():
    out, n = apply_pattern("import asyncio as aio\nx = aio.Task.current_task()\n", CURRENT)
    assert n == 1 and "aio.current_task()" in out


def test_replace_attr_path_through_a_from_import():
    out, n = apply_pattern("from asyncio import Task, sleep\nx = Task.current_task()\n", CURRENT)
    assert n == 1
    assert out == "from asyncio import current_task, sleep\nx = current_task()\n"


def test_replace_attr_path_never_shadows_a_local():
    src = "from asyncio import Task\ndef f():\n    current_task = Task.current_task()\n"
    out, n = apply_pattern(src, CURRENT)
    assert n == 1 and "current_task = asyncio.current_task()" in out
    assert "import asyncio" in out and "from asyncio import Task" not in out


def test_lines_allowlist_skips_a_guarded_fallback():
    src = ("try:\n    from collections.abc import Iterable\nexcept ImportError:\n"
           "    from collections import Iterable\nimport collections\n"
           "x = collections.Iterable\n")
    imp = _p("rewrite-import", "collections.Iterable", "collections.abc.Iterable", scope="python")
    out, hits = apply_patterns(src, [imp, ABC_ATTR], lines_for=lambda p: frozenset({6}))
    assert "    from collections import Iterable\n" in out      # the fallback is untouched
    assert "x = collections.abc.Iterable" in out
    assert sum(n for _, n in hits) == 1


def test_from_imports_moving_to_one_module_stay_one_statement():
    src = ("from pandas.util.testing import (assert_frame_equal,\n"
           "                                 assert_series_equal)\n")
    pats = [_p("rewrite-import", f"pandas.util.testing.{n}", f"pandas.testing.{n}")
            for n in ("assert_frame_equal", "assert_series_equal")]
    out, hits = apply_patterns(src, pats)
    assert out.count("from pandas.testing import") == 1
    assert out.index("assert_frame_equal") < out.index("assert_series_equal")
    assert "(assert_frame_equal,\n" in out                       # layout kept
    assert sorted(p.old_qualname.rsplit(".", 1)[1] for p, _ in hits) == [
        "assert_frame_equal", "assert_series_equal"]


def test_a_partial_move_splits_into_one_statement_per_module():
    src = "from collections import Iterable, Mapping, OrderedDict\n"
    pats = [_p("rewrite-import", f"collections.{n}", f"collections.abc.{n}")
            for n in ("Iterable", "Mapping")]
    out, _ = apply_patterns(src, pats)
    assert out == ("from collections import OrderedDict\n"
                   "from collections.abc import Iterable, Mapping\n")


def test_a_single_moved_name_gets_no_parentheses():
    src = "from collections import (Iterable,\n                         OrderedDict)\n"
    out, _ = apply_patterns(src, [_p("rewrite-import", "collections.Iterable",
                                     "collections.abc.Iterable")])
    assert out == ("from collections import OrderedDict\n"
                   "from collections.abc import Iterable\n")


def test_a_moved_alias_keeps_its_as():
    out, _ = apply_patterns("from collections import Iterable as It, OrderedDict\n",
                            [_p("rewrite-import", "collections.Iterable",
                                "collections.abc.Iterable")])
    assert "from collections.abc import Iterable as It" in out


def test_python_rename_renames_every_reference():
    pat = _p("rename-call", "configparser.SafeConfigParser", "configparser.ConfigParser",
             scope="python")
    src = ("from configparser import SafeConfigParser\n"
           "class C(SafeConfigParser):\n    pass\nx = SafeConfigParser()\n")
    out, n = apply_pattern(src, pat)
    assert n == 1
    assert "SafeConfigParser" not in out and out.count("ConfigParser") == 3


MAPPING_ATTR = _p("rewrite-attr", "collections.Mapping", "collections.abc.Mapping",
                  requires_import="collections.abc", scope="python")


def test_lines_are_the_inputs_and_follow_each_earlier_change():
    """``lines_for`` names lines of the input; the first fix adds an import (and a blank line),
    so the second fix's site has moved by the time it runs."""
    src = "import collections\nA = collections.Iterable\nB = collections.Mapping\n"
    lines = {ABC_ATTR.old_qualname: frozenset({2}), MAPPING_ATTR.old_qualname: frozenset({3})}
    out, hits = apply_patterns(src, [ABC_ATTR, MAPPING_ATTR],
                               lines_for=lambda p: lines[p.old_qualname])
    assert out == ("import collections\nimport collections.abc\n\n"
                   "A = collections.abc.Iterable\nB = collections.abc.Mapping\n")
    assert [(p.old_qualname, n) for p, n in hits] == [("collections.Iterable", 1),
                                                      ("collections.Mapping", 1)]


def test_a_move_group_finds_its_statement_after_an_earlier_fix_moved_it():
    src = "import collections\nx = collections.Iterable\nfrom collections import Mapping\n"
    move = _p("rewrite-import", "collections.Mapping", "collections.abc.Mapping", scope="python")
    lines = {ABC_ATTR.old_qualname: frozenset({2}), move.old_qualname: frozenset({3})}
    out, hits = apply_patterns(src, [ABC_ATTR, move], lines_for=lambda p: lines[p.old_qualname])
    assert "from collections.abc import Mapping" in out
    assert "x = collections.abc.Iterable" in out
    assert sum(n for _, n in hits) == 2


def test_carry_lines_drops_a_deleted_line_and_shifts_the_lines_after_it():
    """How a later fix's ``lines_for`` follows an earlier change: a site on a line that change
    deleted is dropped, never moved onto another line."""
    from pymolt.codemods.apply import carry_lines, line_map

    moved = line_map("a\nb\nc\n", "a\nc\n")
    assert carry_lines(frozenset({2}), moved) == frozenset()
    assert carry_lines(frozenset({3}), moved) == frozenset({2})
    assert carry_lines(None, moved) is None
