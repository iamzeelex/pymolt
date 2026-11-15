"""The runtime's record of which installed distributions a captured process ran."""
from __future__ import annotations

import types

from pymolt.verify._backend import loaded_distributions


def _dist(site, dirname, name, version, *, top_level=None, record=None):
    meta = site / dirname
    meta.mkdir()
    (meta / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\n"
                                   f"Version: {version}\n\nlong description\nVersion: 9\n")
    if top_level is not None:
        (meta / "top_level.txt").write_text("\n".join(top_level) + "\n")
    if record is not None:
        (meta / "RECORD").write_text("\n".join(f"{p},," for p in record) + "\n")


def _module(site, name):
    return types.SimpleNamespace(__file__=str(site / name / "__init__.py"))


def test_records_the_versions_of_loaded_distributions_only(tmp_path):
    site = tmp_path / "site-packages"
    site.mkdir()
    _dist(site, "pandas-1.5.3.dist-info", "pandas", "1.5.3")
    _dist(site, "PyYAML-6.0.1.dist-info", "PyYAML", "6.0.1", top_level=["_yaml", "yaml"])
    _dist(site, "python_dateutil-2.9.0.dist-info", "python-dateutil", "2.9.0",
          record=["dateutil/__init__.py", "dateutil/tz/tz.py",
                  "python_dateutil-2.9.0.dist-info/RECORD"])
    _dist(site, "requests-2.32.0.dist-info", "requests", "2.32.0")   # installed, not loaded
    modules = {
        "pandas": _module(site, "pandas"),
        "pandas.core": _module(site, "pandas/core"),     # submodules are not distributions
        "yaml": _module(site, "yaml"),
        "dateutil": _module(site, "dateutil"),
        "app": types.SimpleNamespace(__file__=str(tmp_path / "app.py")),  # not installed
        "sys": types.SimpleNamespace(),                                   # no __file__
    }
    found = loaded_distributions(modules, roots=(str(site) + "/",))
    # Name and Version come from the header block, never from the long description.
    assert found == {"pandas": "1.5.3", "PyYAML": "6.0.1", "python-dateutil": "2.9.0"}


def test_nothing_loaded_from_the_site_roots_records_nothing(tmp_path):
    assert loaded_distributions({"app": types.SimpleNamespace(__file__="/x/app.py")},
                                roots=(str(tmp_path) + "/",)) == {}
