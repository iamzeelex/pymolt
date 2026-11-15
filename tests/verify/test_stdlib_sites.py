"""Stdlib sites in the contact map: spellings, guards, reached_at, lines and the per-file cache.

The stdlib data is faked so these tests pin the *site* logic, not 1b-1's sweep.
"""

import pytest

from pymolt.core import stdlib as stdlib_data
from pymolt.scan.report import python_exposure
from pymolt.verify import contact_map as cm
from pymolt.verify.contact_map import build_contact_map

_MODULES = {"collections", "imp", "distutils", "ssl", "os", "sys", "inspect", "importlib", "six_"}
_ENDS = {
    "collections.Iterable": "3.9",
    "collections.Mapping": "3.9",
    "collections.MutableMapping": "3.9",
    "ssl.wrap_socket": "3.11",
    "inspect.getargspec": "3.10",
}
_MODULE_ENDS = {"imp": "3.11", "distutils": "3.11", "ConfigParser": "2.7"}


@pytest.fixture(autouse=True)
def fake_stdlib(monkeypatch):
    monkeypatch.setattr(stdlib_data, "is_stdlib", lambda top: top in _MODULES | set(_MODULE_ENDS))
    monkeypatch.setattr(
        stdlib_data, "name_lifetime", lambda d: ("3.0", _ENDS[d]) if d in _ENDS else None
    )
    monkeypatch.setattr(
        stdlib_data,
        "module_lifetime",
        lambda m: ("3.0", _MODULE_ENDS[m]) if m in _MODULE_ENDS else None,
    )
    monkeypatch.setattr(stdlib_data, "changes_for", lambda d: [])


def _sites(tmp_path, source: str, name: str = "app.py"):
    (tmp_path / name).write_text(source, encoding="utf-8")
    cmap = build_contact_map(tmp_path)
    return [(s.spelling, s.line, s.kind, s.reached_at, s.guard) for s in cmap.stdlib_sites], cmap


def _only(tmp_path, source: str):
    rows, _ = _sites(tmp_path, source)
    assert len(rows) == 1, rows
    return rows[0]


# ── spellings ────────────────────────────────────────────────────────────────


def test_from_import_spells_module_dot_name(tmp_path):
    spelling, line, kind, reached, guard = _only(tmp_path, "from collections import Iterable\n")
    assert (spelling, line, kind, reached, guard) == (
        "collections.Iterable", 1, "from", "import", None,
    )


def test_import_module_then_attribute(tmp_path):
    rows, _ = _sites(
        tmp_path, "import collections\nx = isinstance(1, collections.Iterable)\n"
    )
    assert rows == [("collections.Iterable", 2, "attr", "import", None)]  # plain import not a site


def test_call_through_module_is_call(tmp_path):
    rows, _ = _sites(tmp_path, "import imp\n\ndef f(m):\n    return imp.reload(m)\n")
    assert rows == [
        ("imp", 1, "import", "import", None),
        ("imp.reload", 4, "call", "call", None),
    ]


def test_bare_import_is_the_module(tmp_path):
    assert _only(tmp_path, "import imp\n")[:3] == ("imp", 1, "import")


def test_aliased_module_resolves_to_real_name(tmp_path):
    rows, _ = _sites(tmp_path, "import collections as c\nx = c.Iterable\n")
    assert [r[0] for r in rows] == ["collections.Iterable"]


def test_class_base(tmp_path):
    rows, _ = _sites(
        tmp_path, "import collections\n\nclass H(collections.MutableMapping):\n    pass\n"
    )
    assert rows == [("collections.MutableMapping", 3, "class-base", "import", None)]


def test_reached_at_call_inside_function_and_import_in_class_body(tmp_path):
    rows, _ = _sites(
        tmp_path,
        "import collections\n\n"
        "class K:\n    base = collections.Mapping\n\n"
        "def f():\n    from collections import Iterable\n    return collections.Iterable\n",
    )
    assert [(r[0], r[1], r[3]) for r in rows] == [
        ("collections.Mapping", 4, "import"),
        ("collections.Iterable", 7, "call"),
        ("collections.Iterable", 8, "call"),
    ]


def test_multiple_names_on_one_import_are_separate_sites(tmp_path):
    rows, _ = _sites(tmp_path, "from collections import Iterable, Mapping, OrderedDict\n")
    assert [r[0] for r in rows] == ["collections.Iterable", "collections.Mapping"]


def test_names_with_no_known_end_are_not_sites(tmp_path):
    rows, _ = _sites(tmp_path, "import os\nfrom os import path\nos.getcwd()\nimport sys\n")
    assert rows == []


def test_relative_and_star_imports_are_ignored(tmp_path):
    rows, _ = _sites(tmp_path, "from . import collections\nfrom collections import *\n")
    assert rows == []


def test_stdlib_not_decided_by_the_host(tmp_path):
    """imp is stdlib on 3.0-3.11 only; the host (3.12) must not turn it into a dependency."""
    _, cmap = _sites(tmp_path, "import imp\nimp.reload(1)\nimport requests\nrequests.get(1)\n")
    assert set(cmap.by_dep) == {"requests"}


def test_contacts_carry_a_line(tmp_path):
    _, cmap = _sites(tmp_path, "import requests\n\n\nrequests.get(1)\nrequests.get(2)\n")
    assert [(c.target, c.line) for c in cmap.contacts] == [("requests.get", 4)]


# ── guards ───────────────────────────────────────────────────────────────────


def _guard(tmp_path, source: str, spelling: str):
    rows, _ = _sites(tmp_path, source)
    match = [r for r in rows if r[0] == spelling]
    assert len(match) == 1, rows
    return match[0][4]


def test_try_importerror_body_is_guarded_fallback_and_handler_is_fallback(tmp_path):
    src = (
        "try:\n    from importlib import reload\nexcept ImportError:\n    from imp import reload\n"
    )
    rows, _ = _sites(tmp_path, src)
    assert [(r[0], r[4]["kind"]) for r in rows] == [("imp.reload", "fallback")]
    src2 = "try:\n    from imp import reload\nexcept ImportError:\n    reload = None\n"
    assert _guard(tmp_path, src2, "imp.reload")["kind"] == "guarded-fallback"


@pytest.mark.parametrize("exc", ["ImportError", "ModuleNotFoundError", "AttributeError",
                                 "(ValueError, ImportError)"])
def test_each_fallback_exception_guards(tmp_path, exc):
    src = f"try:\n    from collections import Iterable\nexcept {exc}:\n    Iterable = None\n"
    assert _guard(tmp_path, src, "collections.Iterable")["kind"] == "guarded-fallback"


def test_except_exception_and_bare_and_pass_are_broad(tmp_path):
    for handler in ("except Exception:\n    x = 1", "except:\n    x = 1",
                    "except ValueError:\n    pass"):
        src = "import imp\ntry:\n    imp.reload(1)\n" + handler + "\n"
        assert _guard(tmp_path, src, "imp.reload")["kind"] == "broad", handler


def test_unrelated_except_is_no_guard(tmp_path):
    src = "import imp\ntry:\n    imp.reload(1)\nexcept ValueError:\n    raise\n"
    assert _guard(tmp_path, src, "imp.reload") is None


def test_fallback_outranks_broad(tmp_path):
    src = (
        "try:\n    try:\n        from imp import reload\n    except ImportError:\n        pass\n"
        "except Exception:\n    pass\n"
    )
    assert _guard(tmp_path, src, "imp.reload")["kind"] == "guarded-fallback"


def test_try_guard_does_not_reach_into_a_function_body(tmp_path):
    src = (
        "import imp\ntry:\n    def f():\n        return imp.reload(1)\n"
        "except ImportError:\n    pass\n"
    )
    assert _guard(tmp_path, src, "imp.reload") is None


def test_try_else_and_finally_are_not_guarded(tmp_path):
    src = (
        "import imp\ntry:\n    pass\nexcept ImportError:\n    pass\n"
        "else:\n    imp.reload(1)\nfinally:\n    imp.reload(2)\n"
    )
    rows, _ = _sites(tmp_path, src)
    assert [r[4] for r in rows if r[0] == "imp.reload"] == [None, None]


@pytest.mark.parametrize("test", [
    "sys.version_info[0] == 2",
    "sys.version_info >= (3, 4)",
    "sys.version.startswith('2')",
    "PY2",
    "PY3",
    "six.PY2",
    "not six.PY3",
    "is_py2",
    "IS_PY3",
    "hasattr(collections, 'Iterable')",
    "not hasattr(collections, 'Iterable')",
    "foo and sys.version_info < (3,)",
])
def test_version_tests_guard_both_branches(tmp_path, test):
    for branch in ("if {t}:\n    x = collections.Iterable\n",
                   "if {t}:\n    pass\nelse:\n    x = collections.Iterable\n",
                   "if other:\n    pass\nelif {t}:\n    x = collections.Iterable\n"):
        src = "import collections, sys, six\n" + branch.format(t=test)
        guard = _guard(tmp_path, src, "collections.Iterable")
        assert guard is not None and guard["kind"] == "version", (test, branch)


def test_version_ternary_guards(tmp_path):
    src = (
        "import collections, sys\n"
        "x = collections.Iterable if sys.version_info[0] == 3 else list\n"
    )
    assert _guard(tmp_path, src, "collections.Iterable")["kind"] == "version"


def test_ordinary_if_and_isinstance_elif_are_not_guards(tmp_path):
    # hyper/http11/connection.py:393 — an `elif isinstance(...)` is a type switch, not a guard.
    src = (
        "import collections\n\n"
        "def send(body):\n"
        "    if body is None:\n        return\n"
        "    elif isinstance(body, bytes):\n        return 1\n"
        "    elif isinstance(body, collections.Iterable):\n        return 2\n"
    )
    assert _guard(tmp_path, src, "collections.Iterable") is None
    assert _guard(tmp_path, "import collections\nif x:\n    y = collections.Iterable\n",
                  "collections.Iterable") is None


def test_hasattr_must_name_a_stdlib_module(tmp_path):
    src = "import collections\nif hasattr(thing, 'Iterable'):\n    x = collections.Iterable\n"
    assert _guard(tmp_path, src, "collections.Iterable") is None


def test_site_in_the_if_test_itself_is_not_guarded(tmp_path):
    src = "import sys, collections\nif sys.version_info and collections.Iterable:\n    pass\n"
    assert _guard(tmp_path, src, "collections.Iterable") is None


def test_py2_stdlib_module_is_a_site_not_a_dependency(tmp_path):
    rows, cmap = _sites(tmp_path, "import ConfigParser\n")
    assert [r[0] for r in rows] == ["ConfigParser"]
    assert cmap.by_dep == {}
    assert any("Python-2 stdlib" in n for n in cmap.notes)


# ── cython ───────────────────────────────────────────────────────────────────


def test_cython_files_are_stated(tmp_path):
    (tmp_path / "fast.pyx").write_text("cimport x\n", encoding="utf-8")
    _, cmap = _sites(tmp_path, "x = 1\n")
    assert cmap.unparsed_cython == ["fast.pyx"]
    assert any(".pyx" in n for n in cmap.notes)


# ── per-file cache ───────────────────────────────────────────────────────────


def test_cache_is_written_keyed_by_content_and_reused(tmp_path, monkeypatch):
    src = "import imp\nimp.reload(1)\n"
    rows, _ = _sites(tmp_path, src)
    cache = list((tmp_path / ".pymolt" / "cache" / "contacts").glob("*.json"))
    assert len(cache) == 1 and len(cache[0].stem) == 64

    calls = []
    real = cm._ContactVisitor.__init__

    def counting(self, *a, **k):
        calls.append(1)
        real(self, *a, **k)

    monkeypatch.setattr(cm._ContactVisitor, "__init__", counting)
    again, _ = _sites(tmp_path, src)
    assert again == rows and calls == []  # warm: nothing parsed

    again, _ = _sites(tmp_path, src + "\nimp.reload(2)\n")  # content changed -> new key
    assert calls == [1] and len(again) == 3
    assert len(list((tmp_path / ".pymolt" / "cache" / "contacts").glob("*.json"))) == 2


def test_cache_version_bump_retires_entries(tmp_path, monkeypatch):
    _sites(tmp_path, "import imp\n")
    monkeypatch.setattr(cm, "CONTACT_MAP_VERSION", "next")
    calls = []
    real = cm._ContactVisitor.__init__
    monkeypatch.setattr(
        cm._ContactVisitor, "__init__", lambda s, *a, **k: (calls.append(1), real(s, *a, **k))[1]
    )
    _sites(tmp_path, "import imp\n")
    assert calls == [1]


def test_unwritable_cache_still_scans(tmp_path):
    (tmp_path / ".pymolt").write_text("a file, so the cache dir cannot be made", encoding="utf-8")
    rows, _ = _sites(tmp_path, "import imp\n")
    assert [r[0] for r in rows] == ["imp"]


# ── exposure ─────────────────────────────────────────────────────────────────


def test_python_exposure_ceiling_and_guards(tmp_path):
    src = (
        "import collections, imp\n"
        "from ssl import wrap_socket\n"
        "x = collections.Iterable\n"
        "try:\n    from importlib import reload\nexcept ImportError:\n    from imp import reload\n"
        "import ConfigParser\n"
    )
    _, cmap = _sites(tmp_path, src)
    exposure = python_exposure(cmap.stdlib_sites)
    # imp (line 1) and wrap_socket (line 2) go in 3.12, Iterable (line 3) in 3.10.
    assert exposure["by_version"] == {"3.10": 1, "3.12": 2}
    assert exposure["sites_total"] == 3
    assert exposure["guarded_total"] == 1
    assert exposure["pre_existing_total"] == 1
    assert exposure["ceiling"] == "3.9"
    assert exposure["basis"] == "knowledge"


def test_python_exposure_empty(tmp_path):
    exposure = python_exposure([])
    assert exposure["sites_total"] == 0 and exposure["ceiling"] is None
