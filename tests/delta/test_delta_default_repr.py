"""A parameter default is rendered without the places the code lives in.

The old and new sides of a delta are probed in two environments, installed in two places. A
default whose repr carries a path (a module, a path computed from ``__file__``) would compare
the places, not the values: django's ``LazySettings.configure(default_settings=global_settings)``
gave a DEFAULT fact or not depending on where the bench ran."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import types

from pymolt.delta import probe as probe_mod
from pymolt.delta.engine import param_changes

_PKG = (
    "import os\n"
    "import pathlib\n"
    "from pkg import {settings} as _settings\n"
    "HERE = os.path.dirname(os.path.abspath(__file__))\n"
    "class LazySettings:\n"
    "    def configure(self, default_settings=_settings, root=HERE,\n"
    "                  locale=os.path.join(HERE, {locale!r}), base=pathlib.Path(HERE)):\n"
    "        return default_settings\n"
)


def _install(site, *, settings="global_settings", locale="locale"):
    pkg = site / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(_PKG.format(settings=settings, locale=locale))
    for name in ("global_settings", "other_settings"):
        (pkg / f"{name}.py").write_text("DEBUG = False\n")
    return site


def _shape(site, cwd):
    env = {**os.environ, "PYTHONPATH": str(site)}
    req = {"paths": ["pkg.LazySettings.configure"], "roots": ["pkg"], "per_path_timeout": 20}
    run = subprocess.run([sys.executable, "-B", probe_mod.__file__], input=json.dumps(req),
                         capture_output=True, text=True, env=env, cwd=str(cwd), timeout=60)
    for line in run.stdout.splitlines():
        if line.startswith(probe_mod.MARK):
            msg = json.loads(line[len(probe_mod.MARK):])
            if msg.get("path") == "pkg.LazySettings.configure":
                return msg["result"]["steps"][-1]["shape"]
    raise AssertionError(run.stderr[-2000:])


def _defaults(shape):
    return {name: default for name, _kind, default in shape}


def test_the_same_defaults_installed_in_two_places_compare_equal(tmp_path):
    old = _install(tmp_path / "a" / "lib" / "site-packages")
    new = _install(tmp_path / "elsewhere" / "env" / "lib" / "python3.12" / "site-packages")
    (tmp_path / "proj").mkdir()
    old_shape, new_shape = _shape(old, tmp_path / "proj"), _shape(new, tmp_path / "proj")
    assert old_shape == new_shape
    assert param_changes(old_shape, new_shape) == []
    shown = _defaults(new_shape)
    assert shown["default_settings"] == "<module 'pkg.global_settings'>"
    assert shown["root"] == "'<site-packages>/pkg'"
    assert shown["locale"] == "'<site-packages>/pkg/locale'"
    assert shown["base"].endswith("('<site-packages>/pkg')")           # PosixPath(...)


def test_a_module_default_that_becomes_another_module_is_still_a_change(tmp_path):
    old = _install(tmp_path / "a" / "site-packages")
    new = _install(tmp_path / "b" / "site-packages", settings="other_settings")
    (change,) = param_changes(_shape(old, tmp_path), _shape(new, tmp_path))
    assert (change.kind, change.param) == ("DEFAULT", "default_settings")
    assert (change.old, change.new) == ("<module 'pkg.global_settings'>",
                                        "<module 'pkg.other_settings'>")


def test_a_path_default_that_changes_below_the_install_place_is_still_a_change(tmp_path):
    old = _install(tmp_path / "a" / "site-packages")
    new = _install(tmp_path / "b" / "site-packages", locale="translations")
    (change,) = param_changes(_shape(old, tmp_path), _shape(new, tmp_path))
    assert (change.kind, change.param) == ("DEFAULT", "locale")
    assert change.new == "'<site-packages>/pkg/translations'"


# ── the rendering itself ──────────────────────────────────────────────────────────────────


def test_a_module_renders_by_name_only():
    one = types.ModuleType("django.conf.global_settings")
    one.__file__ = "/Users/someone/Documents/projects/app/.envs/django/old/lib/python3.9/" \
                   "site-packages/django/conf/global_settings.py"
    two = types.ModuleType("django.conf.global_settings")
    two.__file__ = "/private/tmp/scratch/.pymolt/cache/envs/0123abcd/lib/python3.12/" \
                   "site-packages/django/conf/global_settings.py"
    assert probe_mod.default_repr(one) == probe_mod.default_repr(two) == (
        "<module 'django.conf.global_settings'>")
    assert probe_mod.default_repr(types.ModuleType("sys")) == "<module 'sys'>"


def test_a_place_is_replaced_only_as_a_whole_path_component(monkeypatch):
    monkeypatch.setattr(probe_mod, "_PLACES", probe_mod.places_pattern(
        {"/srv/env/lib/site-packages": "<site-packages>", "/srv/proj": "<project>"}))
    assert probe_mod.default_repr("/srv/env/lib/site-packages/pkg/data") == (
        "'<site-packages>/pkg/data'")
    assert probe_mod.default_repr("/srv/proj") == "'<project>'"
    assert probe_mod.default_repr("/srv/project2/x") == "'/srv/project2/x'"
    assert probe_mod.default_repr("/x/srv/proj/y") == "'/x/srv/proj/y'"
    assert probe_mod.default_repr(("/srv/proj/a", 1)) == "('<project>/a', 1)"


def test_the_places_are_the_environment_and_the_project_longest_first(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    site = project / ".venv" / "lib" / "site-packages"   # an environment inside the project
    site.mkdir(parents=True)
    table = probe_mod.place_table([str(site)], str(project))
    assert table[str(site)] == "<site-packages>" and table[str(project)] == "<project>"
    monkeypatch.setattr(probe_mod, "_PLACES", probe_mod.places_pattern(table))
    assert probe_mod.default_repr(str(site / "pkg")) == "'<site-packages>/pkg'"
    assert probe_mod.default_repr(str(project / "conf")) == "'<project>/conf'"


def test_a_set_default_renders_the_same_under_any_hash_seed(tmp_path, monkeypatch):
    """A set's repr lists its members in hash order, and string hashes change per process:
    urllib3's ``Retry(allowed_methods=frozenset({...}))`` read differently on each probe."""
    site = tmp_path / "site-packages"
    (site / "pkg").mkdir(parents=True)
    (site / "pkg" / "__init__.py").write_text(
        "class LazySettings:\n"
        "    def configure(self, methods=frozenset({'GET', 'PUT', 'HEAD', 'DELETE', 'TRACE'}),\n"
        "                  tags={'b', 'a', 'c'}, none=frozenset()):\n"
        "        pass\n")
    shapes = []
    for seed in ("1", "2", "3"):
        monkeypatch.setenv("PYTHONHASHSEED", seed)
        shapes.append(_shape(site, tmp_path))
    assert shapes[0] == shapes[1] == shapes[2]
    shown = _defaults(shapes[0])
    assert shown["methods"] == "frozenset({'DELETE', 'GET', 'HEAD', 'PUT', 'TRACE'})"
    assert shown["tags"] == "{'a', 'b', 'c'}" and shown["none"] == "frozenset()"


def test_a_set_default_that_gains_a_member_is_still_a_change():
    old = [["methods", "POSITIONAL_OR_KEYWORD", probe_mod.default_repr(frozenset({"GET"}))]]
    new = [["methods", "POSITIONAL_OR_KEYWORD",
            probe_mod.default_repr(frozenset({"GET", "PUT"}))]]
    (change,) = param_changes(old, new)
    assert (change.kind, change.old, change.new) == (
        "DEFAULT", "frozenset({'GET'})", "frozenset({'GET', 'PUT'})")
