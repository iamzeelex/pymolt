"""Successor ranking and rename successors: the object's own home first, deprecation prose
(old and new side, intermediate releases) naming a rename, variant suffixes kept, and a rename
that is not the same object applied only when the signatures are compatible."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pymolt.delta import engine as engine_mod
from pymolt.delta import fetch as fetch_mod
from pymolt.delta import probe as probe_mod
from pymolt.delta.engine import DiffInput, as_touched, diff
from pymolt.delta.models import DeltaFact
from pymolt.delta.provider import _pattern

from ._fakes import missing, res, shape, step

POK = "POSITIONAL_OR_KEYWORD"
ROOTS = {"pkg": "pkg"}


def _diff(old, new, extra=None):
    return diff(DiffInput(
        old=old, new=new, touched=as_touched(list(old)), root_to_package=ROOTS,
        resolve_new=lambda paths: {p: r for p, r in (extra or {}).items() if p in paths},
    ))["pkg"]


# ── ranking: the object's own home first ────────────────────────────────────────────────


def _run_probe(tmp_path: Path, paths, **request) -> dict:
    env = {**os.environ, "PYTHONPATH": str(tmp_path)}
    req = {"paths": paths, "roots": ["pkg"], "per_path_timeout": 20, **request}
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


def _make_cross_package(tmp_path: Path) -> None:
    (tmp_path / "otherhome").mkdir()
    (tmp_path / "otherhome" / "__init__.py").write_text("def esc(s):\n    return s\n")
    (tmp_path / "_hidden").mkdir()
    (tmp_path / "_hidden" / "__init__.py").write_text("def hid(s):\n    return s\n")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        "from otherhome import esc\nfrom _hidden import hid\nfrom pkg.api import hid2\n")
    (pkg / "compiler.py").write_text("from otherhome import esc\n")
    (pkg / "api.py").write_text("from _hidden import hid as hid2\n")


def test_home_in_another_package_ranks_before_an_incidental_reexport(tmp_path):
    _make_cross_package(tmp_path)
    out = _run_probe(tmp_path, ["pkg.esc"])
    step_esc = out["pkg.esc"]["steps"][0]
    same = step_esc["same_object_as"]
    assert same[0] == "otherhome.esc" and step_esc["same_object_home"] == "otherhome.esc"
    assert "pkg.compiler.esc" in same  # still offered, after the home


def test_internal_home_is_never_the_public_choice(tmp_path):
    _make_cross_package(tmp_path)
    out = _run_probe(tmp_path, ["pkg.hid"])
    step_hid = out["pkg.hid"]["steps"][0]
    # the object's __module__ is the private `_hidden`: internal, so no "home" is claimed
    assert "same_object_home" not in step_hid
    same = step_hid["same_object_as"]
    assert same[0] == "pkg.hid2"  # a public re-export, ahead of the internal home
    assert same[-1] == "_hidden.hid"


def test_engine_picks_the_probes_home_over_a_shorter_incidental_spelling():
    old = {"pkg.escape": res("pkg", step(
        "escape", "pkg.escape", shape=shape(("s", POK, None)),
        same_object_as=["pkg.compiler.escape", "other.escape"], same_object_home="other.escape"))}
    new = {"pkg.escape": missing("pkg", "escape")}
    extra = {"pkg.compiler.escape": res("pkg.compiler", step("escape", "pkg.compiler.escape")),
             "other.escape": res("other", step("escape", "other.escape"))}
    (fact,) = _diff(old, new, extra)
    assert (fact.kind, fact.successor) == ("MOVED", "other.escape")


def test_engine_never_prefers_an_internal_reexport_when_a_public_one_is_live():
    old = {"pkg.f": res("pkg", step(
        "f", "pkg.f", shape=shape(("a", POK, None)),
        same_object_as=["pkg._impl.f", "pkg.api.f"]))}
    new = {"pkg.f": missing("pkg", "f")}
    extra = {"pkg._impl.f": res("pkg._impl", step("f", "pkg._impl.f")),
             "pkg.api.f": res("pkg.api", step("f", "pkg.api.f"))}
    (fact,) = _diff(old, new, extra)
    assert fact.successor == "pkg.api.f"


# ── prose extraction ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("message, expected", [
    ("The `dict` method is deprecated; use `model_dump` instead.", "model_dump"),
    ("Please use `foo.bar` now", "foo.bar"),
    ("Please use 'new_name'.", "new_name"),
    ("'contextfunction' is renamed to 'pass_context', the old name will be removed.",
     "pass_context"),
    ("This function has been renamed to new_func.", "new_func"),
    ("force_text() is deprecated in favor of force_str().", "force_str"),
    ("`old` is deprecated in favour of `pkg.new`.", "pkg.new"),
    ("old_thing is replaced by new_thing", "new_thing"),
    ("It moved to pkg.mod.func", "pkg.mod.func"),
    ("superseded by `Newer`", "Newer"),
    (".. deprecated:: 8.2 Will be removed in Click 9.0. Use Group instead.", "Group"),
    ("Will be removed in 3.1. Use :func:`~jinja2.pass_context` instead.", "jinja2.pass_context"),
    ("Import 'markupsafe.Markup' instead.", "markupsafe.Markup"),
    ("pandas.util.testing is deprecated. Instead access pandas.testing directly",
     "pandas.testing"),
    ("Pydantic V1 style `@validator` validators are deprecated. You should migrate to "
     "Pydantic V2 style `@field_validator` validators", "field_validator"),
])
def test_successor_prose_patterns(message, expected):
    assert fetch_mod.successor_from([message]) == expected


@pytest.mark.parametrize("message", [
    "Use apply to transform the values",
    "use dispatch to route the call",
    "You can use operate to do it",
    "Use this instead",
    "Use the new API instead",
    "replaced by the following",
    "moved to the docs",
    "This will be removed in a future version.",
])
def test_stopwords_and_the_dispatcher_denylist_give_no_successor(message):
    assert fetch_mod.successor_from([message]) is None


@pytest.mark.parametrize("message", [
    "if your data is JSON, use `model_validate_json`, otherwise use `model_validate` instead.",
    "Please use one of Streamlit's new caching commands, `st.cache_data` or `st.cache_resource`.",
    "Please adapt your code to use either `displot` (a figure-level function with similar "
    "flexibility) or `histplot` (an axes-level function for histograms).",
])
def test_a_text_that_offers_a_choice_names_no_single_successor(message):
    # Choosing among several candidates is an agent's or a person's job (owner's rule).
    assert fetch_mod.successor_from([message]) is None


def test_a_quoted_denylisted_word_is_still_a_successor():
    assert fetch_mod.successor_from(["Use `apply` instead."]) == "apply"


# ── rename successors from the new side's prose ──────────────────────────────────────────


def test_new_side_deprecation_prose_names_a_rename_that_exists():
    old = {"pkg.old_name": res("pkg", step("old_name", "pkg.old_name",
                                           shape=shape(("a", POK, None))))}
    new = {"pkg.old_name": res("pkg", step(
        "old_name", "pkg.old_name", shape=shape(("a", POK, None)),
        deprecated_attr="`old_name` is deprecated; use `new_name` instead."))}
    extra = {"pkg.new_name": res("pkg", step("new_name", "pkg.new_name",
                                             shape=shape(("a", POK, None))))}
    (fact,) = _diff(old, new, extra)
    assert (fact.kind, fact.successor) == ("DEPRECATED", "pkg.new_name")
    assert fact.detail["successor_exists"] is True
    assert fact.detail["successor_same_signature"] is True
    pattern = _pattern(fact)
    assert pattern is not None
    assert (pattern.kind, pattern.confidence) == ("rename-call", "medium")
    assert (pattern.old_qualname, pattern.new_qualname) == ("pkg.old_name", "pkg.new_name")


def test_new_side_rename_with_another_signature_is_flagged_incompatible():
    old = {"pkg.old_name": res("pkg", step("old_name", "pkg.old_name",
                                           shape=shape(("a", POK, None))))}
    new = {"pkg.old_name": res("pkg", step(
        "old_name", "pkg.old_name", shape=shape(("a", POK, None)),
        getattr_warns=[{"message": "old_name is deprecated, use new_name instead"}]))}
    extra = {"pkg.new_name": res("pkg", step("new_name", "pkg.new_name",
                                             shape=shape(("a", POK, None), ("b", POK, None))))}
    (fact,) = _diff(old, new, extra)
    assert fact.successor == "pkg.new_name"
    assert fact.detail["successor_same_signature"] is False
    assert _pattern(fact) is None


def test_new_side_method_rename_resolves_beside_the_old_method():
    old = {"pkg.Model.dict": res("pkg", step("Model", "pkg.Model", kind="class"),
                                 step("dict", "pkg.Model.dict", canonical="pkg.main.dict"))}
    new = {"pkg.Model.dict": res("pkg", step("Model", "pkg.Model", kind="class"),
                                 step("dict", "pkg.Model.dict", canonical="pkg.main.dict",
                                      deprecated_attr="The `dict` method is deprecated; "
                                                      "use `model_dump` instead."))}
    extra = {"pkg.Model.model_dump": res(
        "pkg", step("Model", "pkg.Model", kind="class"),
        step("model_dump", "pkg.Model.model_dump", canonical="pkg.main.model_dump"))}
    facts = _diff(old, new, extra)
    (dep,) = [f for f in facts if f.kind == "DEPRECATED"]
    assert dep.successor == "pkg.Model.model_dump"
    assert _pattern(dep) is None  # a method rename needs the receiver's type: a hint only


# ── the successor keeps its own leaf ─────────────────────────────────────────────────────


def _gettext_world(lazy_prose=None):
    shp = shape(("message", POK, None))
    lazy_extra = {"warns": [{"message": lazy_prose}]} if lazy_prose else {}
    old = {
        "pkg.ugettext": res("pkg", step("ugettext", "pkg.ugettext", canonical="pkg.gettext",
                                        shape=shp)),
        "pkg.ugettext_lazy": res("pkg", step("ugettext_lazy", "pkg.ugettext_lazy",
                                             canonical="pkg.gettext", shape=shp, **lazy_extra)),
    }
    new = {"pkg.ugettext": missing("pkg", "ugettext"),
           "pkg.ugettext_lazy": missing("pkg", "ugettext_lazy")}
    extra = {"pkg.gettext": res("pkg", step("gettext", "pkg.gettext", shape=shp)),
             "pkg.gettext_lazy": res("pkg", step("gettext_lazy", "pkg.gettext_lazy",
                                                 canonical="pkg.gettext", shape=shp))}
    return old, new, extra


def test_lazy_variant_never_becomes_the_plain_function():
    old, new, extra = _gettext_world()
    facts = _diff(old, new, extra)
    for f in facts:
        if "ugettext_lazy" in f.spellings():
            assert f.successor != "pkg.gettext"
    lazy = next(f for f in facts if "pkg.ugettext_lazy" in f.spellings()
                and "pkg.ugettext" not in f.spellings())
    assert lazy.kind == "REMOVED"
    plain = next(f for f in facts if "pkg.ugettext" in f.spellings())
    assert (plain.kind, plain.successor) == ("MOVED", "pkg.gettext")


def test_lazy_variant_takes_its_own_prose_successor():
    old, new, extra = _gettext_world(
        "pkg.ugettext_lazy() is deprecated in favor of pkg.gettext_lazy().")
    facts = _diff(old, new, extra)
    lazy = next(f for f in facts if f.kind == "REMOVED")
    assert lazy.successor == "pkg.gettext_lazy"


def test_variant_suffix_helper():
    ok = engine_mod._variant_ok
    assert ok("ugettext", "gettext") and ok("ugettext_lazy", "gettext_lazy")
    assert not ok("ugettext_lazy", "gettext") and not ok("ugettext", "gettext_lazy")


# ── a rename to a different object is auto-applied only when compatible ──────────────────


def _rename_world(new_shape):
    old = {"pkg.old_fn": res("pkg", step(
        "old_fn", "pkg.old_fn", shape=shape(("a", POK, None)),
        warns=[{"message": "old_fn is deprecated, use new_fn instead"}]))}
    new = {"pkg.old_fn": missing("pkg", "old_fn")}
    extra = {"pkg.new_fn": res("pkg", step("new_fn", "pkg.new_fn", shape=new_shape))}
    return old, new, extra


def test_compatible_rename_becomes_a_medium_confidence_rename_call():
    (fact,) = _diff(*_rename_world(shape(("a", POK, None))))
    assert (fact.kind, fact.successor) == ("REMOVED", "pkg.new_fn")
    pattern = _pattern(fact)
    assert pattern is not None
    assert (pattern.kind, pattern.confidence) == ("rename-call", "medium")
    assert (pattern.old_qualname, pattern.new_qualname) == ("pkg.old_fn", "pkg.new_fn")


def test_incompatible_rename_is_never_auto_applied_but_keeps_the_hint():
    (fact,) = _diff(*_rename_world(shape(("a", POK, None), ("b", POK, None))))
    assert fact.successor == "pkg.new_fn"  # the risk still carries it as a hint
    assert fact.detail["successor_same_signature"] is False
    assert _pattern(fact) is None


# ── online: the fetch, and what it finds is verified on the new side ─────────────────────


class _FakeFetcher:
    def __init__(self, sources):
        self.sources = sources
        self.bytes = self.requests = 0

    def releases(self, dist):
        return {"0.9": [{"f": 1}], "1.5": [{"f": 1}], "2.0": [{"f": 1}]}

    def member(self, dist, version, files, member):
        return self.sources.get(member)


def test_intermediate_release_follows_a_reexport_to_its_prose():
    sources = {
        "pkg/__init__.py": "from .utils import contextfunction\n",
        "pkg/utils.py": (
            "import warnings\n\ndef contextfunction(f):\n"
            "    warnings.warn(\"'contextfunction' is renamed to 'pass_context', the old name\"\n"
            "                  \" will be removed.\", DeprecationWarning)\n    return f\n"),
    }
    out = fetch_mod.intermediate_hint(_FakeFetcher(sources), "pkg", "pkg/__init__.py",
                                      ["contextfunction"], "1.0", "2.0")
    assert out["hint"] == "pass_context" and out["last_version_with_symbol"] == "1.5"


def test_intermediate_prose_with_a_different_leaf_is_read():
    sources = {"pkg/enc.py": ("import warnings\n\ndef force_text(s):\n"
                              "    warnings.warn('force_text() is deprecated in favor of "
                              "force_str().')\n    return s\n")}
    out = fetch_mod.intermediate_hint(_FakeFetcher(sources), "pkg", "pkg/enc.py",
                                      ["force_text"], "1.0", "2.0")
    assert out["hint"] == "force_str"


def test_online_successor_is_resolved_and_signature_checked_on_the_new_side():
    verify = engine_mod._Run._verify_successor
    shp = shape(("s", POK, None))

    def check_for(new_shape):
        return lambda paths: {p: res("pkg.enc", step(p.rsplit(".", 1)[1], p, shape=new_shape))
                              for p in paths if p == "pkg.enc.force_str"}

    def fact(successor):
        f = DeltaFact(symbol="pkg.enc.force_text", kind="REMOVED", successor=successor,
                      old="pkg.enc.force_text")
        f.detail.update(old_shape=shp, importable=["pkg.enc.force_text"])
        return f

    ok = fact("force_str")
    verify(ok, check_for(shp))
    assert ok.successor == "pkg.enc.force_str"
    assert ok.detail["successor_same_signature"] is True and ok.detail["successor_importable"]
    assert _pattern(ok) is not None

    other = fact("force_str")
    verify(other, check_for(shape(("s", POK, None), ("e", POK, None))))
    assert other.detail["successor_same_signature"] is False
    assert _pattern(other) is None

    gone = fact("nothing_here")
    verify(gone, check_for(shp))
    assert gone.successor == "nothing_here" and gone.detail["successor_exists"] is False
