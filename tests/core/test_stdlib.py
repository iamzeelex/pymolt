"""The stdlib lifetimes API (``pymolt.core.stdlib``) over the shipped knowledge files."""

from __future__ import annotations

import pytest

from pymolt.core import stdlib
from pymolt.knowledge import KnowledgeError, stdlib_changes, stdlib_lifetimes


class TestModules:
    def test_is_stdlib_is_the_union_over_versions(self):
        assert stdlib.is_stdlib("imp")  # removed in 3.12
        assert stdlib.is_stdlib("os")
        assert stdlib.is_stdlib("tomllib")  # added in 3.11
        assert stdlib.is_stdlib("ConfigParser")  # Python 2 only
        assert not stdlib.is_stdlib("requests")

    def test_module_lifetime(self):
        assert stdlib.module_lifetime("imp") == ("3.0", "3.11")
        assert stdlib.module_lifetime("os") == ("3.0", None)
        assert stdlib.module_lifetime("ConfigParser") == ("2.7", "2.7")
        assert stdlib.module_lifetime("nope") is None

    def test_unlisted_submodule_inherits_the_parent(self):
        assert stdlib.module_lifetime("xml.etree.ElementTree") == stdlib.module_lifetime("xml")


class TestNames:
    def test_collections_aliases_end_with_3_9(self):
        first, last = stdlib.name_lifetime("collections.Iterable")
        assert first in ("3.0", "3.3") and last == "3.9"
        for abc in ("Mapping", "MutableMapping", "MutableSet", "Callable", "Sequence"):
            assert stdlib.name_lifetime(f"collections.{abc}")[1] == "3.9"

    def test_present_in(self):
        assert stdlib.present_in("imp.reload", "3.12") is False
        assert stdlib.present_in("imp.reload", "3.11") is True
        assert stdlib.present_in("collections.abc.Iterable", "3.12") is True
        assert stdlib.present_in("collections.Iterable", "3.9") is True
        assert stdlib.present_in("collections.Iterable", "3.10") is False
        assert stdlib.present_in("inspect.getargspec", "3.11") is False

    def test_unknown_is_none_not_false(self):
        assert stdlib.present_in("os.no_such_name_anywhere", "3.12") is None
        assert stdlib.present_in("collections.Iterable", "3.2") is None  # older than any swept
        assert stdlib.present_in("collections.Iterable", "garbage") is None

    def test_a_removed_module_takes_its_names_with_it(self):
        assert stdlib.present_in("imp", "3.12") is False
        assert stdlib.present_in("distutils.version.LooseVersion", "3.12") is False

    def test_data_stamp(self):
        assert stdlib.data_stamp().startswith("knowledge:stdlib_lifetimes@")


class TestChanges:
    def test_module_entry_covers_its_attributes(self):
        ids = [c.id for c in stdlib.changes_for("imp.find_module")]
        assert "imp" in ids

    def test_most_specific_entry_first(self):
        ids = [c.id for c in stdlib.changes_for("imp.reload")]
        assert ids[0] == "imp-reload" and "imp" in ids

    def test_collections_abc_is_the_identical_object(self):
        changes = stdlib.changes_for("collections.Mapping")
        (change,) = [c for c in changes if c.id == "collections-abc"]
        assert change.same_object is True
        assert change.removed_in == "3.10"
        assert change.successor_for("collections.Mapping") == "collections.abc.Mapping"

    def test_distutils_is_shimmed_by_setuptools(self):
        (change,) = [c for c in stdlib.changes_for("distutils") if c.id == "distutils"]
        assert change.shimmed_by == "setuptools>=60" and change.removed_in == "3.12"

    def test_unrelated_symbol_has_no_change(self):
        assert stdlib.changes_for("os.path.join") == []
        assert stdlib.changes_for("collections.OrderedDict") == []

    def test_every_entry_is_sourced_and_valid(self):
        entries = stdlib_changes()
        assert len(entries) >= 40
        ids = [e.id for e in entries]
        assert len(ids) == len(set(ids))
        for e in entries:
            assert e.source and e.date and e.symbols
            if e.kind != "deprecated":
                assert e.removed_in, e.id
            if e.same_object:
                assert e.successor, e.id


def test_lifetimes_file_is_valid_and_stamped():
    data = stdlib_lifetimes()
    assert data["date"] and data["source"] and data["pythons"]
    assert data["modules"] and data["names"]


def test_an_unknown_knowledge_file_raises():
    from pymolt import knowledge

    with pytest.raises(KnowledgeError):
        knowledge._load_raw("does_not_exist")


def test_typeshed_only_stub_packages_are_not_stdlib():
    """typeshed's stdlib/VERSIONS lists stubs for typing_extensions (a PyPI package) and
    _typeshed (stub-only); neither is in any interpreter's standard library."""
    from pymolt.core import stdlib

    assert not stdlib.is_stdlib("typing_extensions")
    assert not stdlib.is_stdlib("_typeshed")
    assert stdlib.is_stdlib("typing") and stdlib.is_stdlib("asyncio")
