"""Version guards are evaluated against the target when a site is rated."""
from __future__ import annotations

import pytest

from pymolt.pyversion.guards import evaluate_guard, parse_version


@pytest.mark.parametrize("detail,expected", [
    ("sys.version_info < (3, 3)", False),
    ("sys.version_info >= (3, 0)", True),
    ("sys.version_info[0] == 2", False),
    ("sys.version_info[0] >= 3", True),
    ("sys.version_info[:2] >= (3, 12)", True),
    ("sys.version_info > (3, 12, 4)", False),
    ("sys.version_info.major == 3 and sys.version_info.minor < 10", False),
    ("PY2", False), ("not six.PY3", False), ("six.PY3", True),
    ("(3, 3) > sys.version_info", False),
    ("something_unrelated()", None),
    ("sys.version_info <", None),
])
def test_version_tests_against_3_12(detail, expected):
    assert evaluate_guard(detail, (3, 12)) is expected


def test_hasattr_guards_use_the_data():
    assert evaluate_guard("hasattr(collections, 'Iterable')", (3, 12),
                          lambda dotted: dotted != "collections.Iterable") is False
    assert evaluate_guard("hasattr(collections, 'Iterable')", (3, 12)) is None


def test_parse_version():
    assert parse_version("3.6.15") == (3, 6) and parse_version("3.12") == (3, 12)
    assert parse_version("unknown") is None and parse_version(None) is None
