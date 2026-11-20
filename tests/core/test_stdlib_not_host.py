"""Stdlib membership comes from the bundled lifetimes of every Python version, never from the
host interpreter: pymolt runs on 3.12, where ``imp``, ``distutils`` and ``asynchat`` are gone,
but a project migrating from 3.6 still imports them from its standard library."""
from __future__ import annotations

import sys

import pytest

from pymolt.delta.touched import is_stdlib as touched_is_stdlib
from pymolt.pyversion.behavior import _std
from pymolt.verify.service import _stdlib_top

REMOVED = ["imp", "distutils", "asynchat"]


@pytest.mark.skipif(sys.version_info < (3, 12), reason="these left the stdlib in 3.12")
def test_the_host_no_longer_lists_them():
    assert not set(REMOVED) & set(sys.stdlib_module_names)


@pytest.mark.parametrize("top", REMOVED)
def test_a_module_removed_from_the_host_is_still_stdlib(top):
    assert touched_is_stdlib(f"{top}.anything")
    assert _stdlib_top(f"{top}.anything") == top
    assert _std(top)


def test_a_dependency_is_not_stdlib_and_typing_extensions_counts_only_where_meant():
    assert not touched_is_stdlib("requests.get") and _stdlib_top("requests") is None
    assert _std("typing_extensions") and not touched_is_stdlib("typing_extensions.Literal")
    assert _stdlib_top("__future__") is None
