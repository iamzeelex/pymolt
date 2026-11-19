"""Change taxonomy (SIGNATURE vs DEFAULT) and touched spellings the runtime cannot name."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace

from pymolt.delta import probe as probe_mod
from pymolt.delta import touched
from pymolt.delta.engine import param_changes

POK = "POSITIONAL_OR_KEYWORD"


def _p(name, kind=POK, default=None):
    return [name, kind, default]


def test_a_parameter_that_loses_its_default_is_a_signature_change():
    old = [_p("stream"), _p("Loader", default="None")]
    new = [_p("stream"), _p("Loader")]
    (change,) = param_changes(old, new)
    assert change.kind == "SIGNATURE" and change.param == "Loader"
    assert change.note == "parameter Loader lost its default; now required"


def test_a_changed_default_value_stays_default_and_a_gained_default_is_default_too():
    assert [c.kind for c in param_changes([_p("a", default="1")], [_p("a", default="2")])] == [
        "DEFAULT"]
    assert [c.kind for c in param_changes([_p("a")], [_p("a", default="1")])] == ["DEFAULT"]


def test_a_removed_keyword_absorbed_by_kwargs_is_silent_unless_the_prose_says_removed():
    old = [_p("x"), _p("regex", "KEYWORD_ONLY", "None")]
    new = [_p("x"), _p("extra", "VAR_KEYWORD")]
    assert param_changes(old, new) == []
    prose = ["`regex` is removed. use `pattern` instead"]
    (change,) = param_changes(old, new, prose)
    assert change.kind == "SIGNATURE" and change.param == "regex"
    assert param_changes(old, new, ["the regex module is fast"]) == []


# ── the probe mines raise messages ───────────────────────────────────────────────────────


def test_probe_records_constant_raise_messages(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "def field(x, **extra):\n    if extra.pop('regex', None):\n"
        "        raise TypeError('`regex` is removed. use `pattern` instead')\n    return x\n")
    env = {**os.environ, "PYTHONPATH": str(tmp_path)}
    req = {"paths": ["pkg.field"], "roots": ["pkg"], "per_path_timeout": 20}
    run = subprocess.run([sys.executable, probe_mod.__file__], input=json.dumps(req),
                         capture_output=True, text=True, env=env, cwd=str(tmp_path / ".."),
                         timeout=60)
    step = None
    for line in run.stdout.splitlines():
        if line.startswith(probe_mod.MARK):
            msg = json.loads(line[len(probe_mod.MARK):])
            if msg.get("path") == "pkg.field":
                step = msg["result"]["steps"][0]
    assert step["raises"] == [{"message": "`regex` is removed. use `pattern` instead"}]


# ── touched spellings ───────────────────────────────────────────────────────────────────


def _fact(symbol, text, self_type, file="app/n.py", func="f"):
    return SimpleNamespace(symbol=symbol, text=text, file=file, func=func,
                           input={"bound": {"self": {"__object__": self_type}}})


def test_an_aliased_attribute_gets_the_spelling_the_project_wrote():
    f = _fact("networkx.classes.graph.Graph.nodes", "return G.node[n]['name']",
              "networkx.classes.graph.Graph")
    assert "networkx.classes.graph.Graph.node" in touched.alias_spellings([f])


def test_an_attribute_the_fact_already_names_adds_nothing():
    f = _fact("pkg.Graph.nodes", "return list(G.nodes)", "pkg.Graph")
    assert touched.alias_spellings([f]) == set()


def test_a_project_receiver_resolves_to_its_dependency_base(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "t.py").write_text(
        "import unittest\n\nclass StampCase(unittest.TestCase):\n    def test_a(self):\n"
        "        self.assertEquals(1, 1)\n")
    f = _fact("unittest.case.TestCase.assertEqual", "self.assertEquals(1, 1)",
              "app.t.StampCase")
    assert "unittest.TestCase.assertEquals" in touched.alias_spellings([f], tmp_path)


def test_inherited_attributes_resolve_to_the_first_dependency_base(tmp_path):
    (tmp_path / "app.py").write_text(
        "from pydantic import BaseModel\nimport unittest\n\n"
        "class User(BaseModel):\n    name: str\n\n    def hello(self):\n"
        "        return self.name\n\n"
        "def names():\n    return list(User.__fields__)\n\n"
        "class StampCase(unittest.TestCase):\n    def test_a(self):\n"
        "        self.assertEquals(1, 1)\n        self.helper()\n\n    def helper(self):\n"
        "        pass\n")
    got = touched.inherited_spellings(tmp_path)
    assert "pydantic.BaseModel.__fields__" in got
    assert "unittest.TestCase.assertEquals" in got
    assert not any(s.endswith((".name", ".helper", ".hello")) for s in got)


def test_stdlib_classification():
    assert touched.is_stdlib("unittest.TestCase.assertEquals")
    assert not touched.is_stdlib("pydantic.BaseModel.dict")


# ── aliases inside a class: the same object under another attribute name ─────────────────


def _probe(tmp_path, paths):
    env = {**os.environ, "PYTHONPATH": str(tmp_path)}
    req = {"paths": paths, "roots": ["pkg"], "per_path_timeout": 20}
    run = subprocess.run([sys.executable, probe_mod.__file__], input=json.dumps(req),
                         capture_output=True, text=True, env=env, cwd=str(tmp_path / ".."),
                         timeout=60)
    out = {}
    for line in run.stdout.splitlines():
        if line.startswith(probe_mod.MARK):
            msg = json.loads(line[len(probe_mod.MARK):])
            if "path" in msg:
                out[msg["path"]] = msg["result"]
    return out


def test_probe_names_a_class_attribute_alias_and_a_shared_constant(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "class Meta(type):\n    @property\n    def OLD(cls):\n        return cls.NEW\n\n"
        "class Graph(metaclass=Meta):\n    NEW = frozenset(['GET', 'PUT'])\n\n"
        "    @property\n    def nodes(self):\n        return 1\n\n    node = nodes\n\n"
        "    def other(self):\n        return 2\n")
    out = _probe(tmp_path, ["pkg.Graph.node", "pkg.Graph.OLD", "pkg.Graph.other"])
    assert out["pkg.Graph.node"]["steps"][1]["same_object_as"] == ["pkg.Graph.nodes"]
    assert out["pkg.Graph.OLD"]["steps"][1]["same_object_as"] == ["pkg.Graph.NEW"]
    assert "same_object_as" not in out["pkg.Graph.other"]["steps"][1]


def test_property_kinds_are_compatible_for_a_move():
    from pymolt.delta.engine import _compatible

    assert _compatible({"kind": "property"}, {"kind": "cached_property"}, "nodes")
    assert not _compatible({"kind": "property"}, {"kind": "int"}, "nodes")


def test_kwargs_that_only_refuse_make_every_removed_parameter_a_signature_change():
    old = [_p("columns", default="None"), _p("whereclause", default="None")]
    new = [_p("entities", "VAR_POSITIONAL"), _p("kw", "VAR_KEYWORD")]
    assert param_changes(old, new) == []
    changed = param_changes(old, new, kwargs_rejected=True)
    assert {c.param for c in changed} == {"columns", "whereclause"}
    assert all(c.kind == "SIGNATURE" for c in changed)


def test_probe_flags_a_kwargs_parameter_that_raises_when_filled(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "def select(*entities, **kw):\n    if kw:\n        raise TypeError()\n"
        "    return entities\n")
    out = _probe(tmp_path, ["pkg.select"])
    assert out["pkg.select"]["steps"][0]["rejects_kwargs"] is True


def test_probe_names_a_module_level_alias_as_the_same_object(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "def force_text(s):\n    return s\n\nforce_str = force_text\n")
    out = _probe(tmp_path, ["pkg.force_text"])
    assert out["pkg.force_text"]["steps"][0]["same_object_as"] == ["pkg.force_str"]
