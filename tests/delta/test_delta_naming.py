"""Naming successors (successors v2, stage S1): the attribution rule, the candidate filters,
the probe's raw extractors (decorator calls, closure cells, warning helpers one level down,
message variables) and the registered deprecation helpers. Pure over fake probe answers, plus
the probe run as a subprocess on small fake packages."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from pymolt.delta import probe as probe_mod
from pymolt.delta import succession as succ_mod
from pymolt.delta.engine import DiffInput, as_touched, diff
from pymolt.delta.models import RecordedWarning, TouchedSymbol

from ._fakes import missing, res, shape, step

POK = "POSITIONAL_OR_KEYWORD"
KW = "KEYWORD_ONLY"


def _diff(old, new, symbols=None, *, extra=None, roots=None):
    touched = as_touched(symbols if symbols is not None else list(old))
    return diff(DiffInput(
        old=old, new=new, touched=touched, root_to_package=roots or {"pkg": "pkg"},
        resolve_new=lambda paths: {p: r for p, r in (extra or {}).items() if p in paths},
    ))["pkg"]


def _live(*names):
    return {n: res(n.rsplit(".", 1)[0], step(n.rsplit(".", 1)[1], n,
                                              shape=shape(("x", POK, None))))
            for n in names}


def _one(facts, kind, param=None):
    found = [f for f in facts if f.kind == kind and f.param == param]
    assert len(found) == 1, facts
    return found[0]


# ── candidates_from ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("message, expected", [
    ("`np.NaN` was removed in the NumPy 2.0 release. Use `np.nan` instead.",
     [("np.nan", "successor")]),
    ("Deprecated, use .can_read_body #2005", [("can_read_body", "successor")]),
    ("*scatter_mapbox* is deprecated! Use *scatter_map* instead.", [("scatter_map",
                                                                     "successor")]),
    ("The append_trace method is deprecated. Please use the add_trace method with the row "
     "and col parameters.", [("add_trace", "successor")]),
    ("Use ``matplotlib.colormaps.register(name)`` instead.",
     [("matplotlib.colormaps.register", "successor")]),
    ("The 'attachment_filename' parameter has been renamed to 'download_name'.",
     [("download_name", "param")]),
    ("the `interpolation=` argument to {...} was renamed to `method=`.", [("method", "param")]),
    ("The `ci` parameter is deprecated. Use `errorbar={...}` for the same effect.",
     [("errorbar", "param")]),
    ("Please use one of Streamlit's new caching commands, `st.cache_data` or "
     "`st.cache_resource`.", [("st.cache_data", "alternative"),
                              ("st.cache_resource", "alternative")]),
])
def test_candidates_from_reads_names_and_roles(message, expected):
    got = [(c.name, c.role) for c in succ_mod.candidates_from([message])]
    assert got == expected


def test_a_bare_generic_word_is_kept_but_marked():
    (c,) = succ_mod.candidates_from(["Please use fit instead."])
    assert c.name == "fit" and c.generic
    (c,) = succ_mod.candidates_from(["Please use `fit` instead."])
    assert not c.generic                      # quoted: code


def test_caveats_and_equivalence_phrases():
    msg = ("`map_dict` is deprecated. It has been renamed to `replace`. The default behavior "
           "has changed to keep any values not present in the mapping unchanged.")
    assert succ_mod.caveats([msg]) == ["The default behavior has changed to keep any values not "
                                       "present in the mapping unchanged."]
    assert succ_mod.asserts_equivalence(["Use `errorbar=('ci', 95)` for the same effect."])
    assert not succ_mod.asserts_equivalence([msg])


# ── the attribution rule ──────────────────────────────────────────────────────────────────


def test_attribution_by_name_parameter_or_template():
    assert succ_mod.attributed("`factorplot` has been renamed to `catplot`", ["factorplot"])
    assert succ_mod.attributed("The `size` parameter has been renamed", ["lmplot"], ["size"])
    assert succ_mod.attributed("Using 'x' with Retry is deprecated", ["Retry"],
                               templates=["Using {...} with Retry is deprecated"])
    assert not succ_mod.attributed(
        "Conversion of an array with ndim > 0 to a scalar is deprecated; use np.result_type",
        ["factorplot"], ["x", "y", "kind"])


def test_negative_np_result_type_a_warning_inside_the_call_is_a_pitfall():
    """seaborn ``factorplot`` warned about numpy's ``result_type`` from inside the call: that
    warning names neither factorplot nor its parameters, so it names no successor."""
    w = RecordedWarning(category="DeprecationWarning",
                        message="`np.find_common_type` is deprecated. Please use "
                                "`np.result_type` or `np.promote_types`.")
    old = {"pkg.factorplot": res("pkg", step("factorplot", "pkg.factorplot",
                                             shape=shape(("kind", POK, "'point'"))))}
    new = {"pkg.factorplot": missing("pkg", "factorplot")}
    (fact,) = _diff(old, new, [TouchedSymbol(path="pkg.factorplot", warnings=[w])],
                    extra=_live("pkg.result_type", "pkg.np.result_type"))
    assert fact.kind == "REMOVED" and fact.successor is None
    assert fact.deprecated_in_old is False
    assert not any(e.startswith("warning:") for e in fact.evidence)


def test_negative_null_boolean_field_warning_is_no_evidence_about_is_valid():
    """DRF: NullBooleanField's warning was recorded at the ``is_valid()`` site. It names
    neither ``is_valid`` nor its parameters: no DEPRECATED fact, no successor for it."""
    w = RecordedWarning(category="RemovedInDRF314Warning",
                        message="The `NullBooleanField` is deprecated and will be removed "
                                "starting with 3.14. Instead use the `BooleanField` field and "
                                "set `allow_null=True` which does the same thing.")
    st = step("is_valid", "pkg.Serializer.is_valid",
              shape=shape(("self", POK, None), ("raise_exception", POK, "False")))
    old = {"pkg.Serializer.is_valid": res("pkg", step("Serializer", "pkg.Serializer",
                                                      kind="class"), st)}
    new = {"pkg.Serializer.is_valid": res("pkg", step("Serializer", "pkg.Serializer",
                                                      kind="class"), dict(st))}
    facts = _diff(old, new, [TouchedSymbol(path="pkg.Serializer.is_valid", warnings=[w])],
                  extra=_live("pkg.BooleanField"))
    assert not [f for f in facts if f.kind == "DEPRECATED"]
    assert all(f.successor is None for f in facts)


# ── the candidate filters ─────────────────────────────────────────────────────────────────


def test_negative_get_metric_a_named_successor_that_does_not_exist_is_rejected():
    """sklearn 1.1's SCORERS warning names ``get_scorer_names`` and a ``get_metric`` that does
    not exist: only the one that exists is the successor, the other is logged as rejected."""
    w = RecordedWarning(category="FutureWarning",
                        message="pkg.SCORERS is deprecated. Please use pkg.get_scorer_names "
                                "to list scorers. Use pkg.get_metric to get a scorer.")
    old = {"pkg.SCORERS": res("pkg", step("SCORERS", "pkg.SCORERS", kind="dict"))}
    new = {"pkg.SCORERS": missing("pkg", "SCORERS")}
    (fact,) = _diff(old, new, [TouchedSymbol(path="pkg.SCORERS", warnings=[w])],
                    extra=_live("pkg.get_scorer_names"))
    assert fact.successor == "pkg.get_scorer_names"
    assert {"name": "pkg.get_metric", "why": "does not exist in the new version"} in \
        fact.succession["rejected"]


def test_negative_core_a_private_spelling_is_never_chosen_over_the_public_home():
    """``numpy.core.numeric.normalize_axis_tuple`` warns on access in numpy 2 and its text names
    ``numpy._core`` (private); the same object's public home is ``numpy.lib.array_utils``."""
    canon = "pkg._core.numeric.normalize_axis_tuple"
    warn = [{"category": "DeprecationWarning",
             "message": "pkg.core is deprecated and has been renamed to pkg._core. Use "
                        "pkg._core.numeric instead."}]
    sp = "pkg.core.numeric.normalize_axis_tuple"
    old = {sp: res("pkg.core.numeric", step("normalize_axis_tuple", sp, canonical=sp,
                                            shape=shape(("axis", POK, None))))}
    new = {sp: res("pkg.core.numeric",
                   step("normalize_axis_tuple", sp, canonical=canon,
                        shape=shape(("axis", POK, None)),
                        same_object_home="pkg.lib.array_utils.normalize_axis_tuple"),
                   runtime_warnings=warn)}
    extra = {**_live("pkg.lib.array_utils.normalize_axis_tuple"),
             "pkg._core.numeric": res("pkg._core", step("numeric", "pkg._core.numeric",
                                                        kind="module"))}
    extra["pkg.lib.array_utils.normalize_axis_tuple"]["steps"][0]["canonical"] = canon
    facts = _diff(old, new, extra=extra)
    dep = _one(facts, "DEPRECATED")
    assert dep.successor == "pkg.lib.array_utils.normalize_axis_tuple"
    assert dep.succession["trust"] == "identity"


def test_negative_pydantic_v1_a_compat_namespace_loses_to_a_forward_successor():
    w = RecordedWarning(category="DeprecationWarning",
                        message="`old` is deprecated. Use `pkg.v1.old` to keep v1 semantics, or "
                                "use `pkg.fresh` instead.")
    old = {"pkg.old": res("pkg", step("old", "pkg.old", shape=shape(("x", POK, None))))}
    new = {"pkg.old": missing("pkg", "old")}
    (fact,) = _diff(old, new, [TouchedSymbol(path="pkg.old", warnings=[w])],
                    extra=_live("pkg.v1.old", "pkg.fresh"))
    assert fact.successor == "pkg.fresh"
    assert any(r["name"] == "pkg.v1.old" and "compatibility" in r["why"]
               for r in fact.succession["rejected"])


def test_a_generic_word_counts_only_as_a_member_of_the_class():
    w = RecordedWarning(category="DeprecationWarning", message="old is deprecated, use update")
    member = "pkg.Thing.old"
    old = {member: res("pkg", step("Thing", "pkg.Thing", kind="class"),
                       step("old", member, shape=shape(("self", POK, None))))}
    new = {member: res("pkg", step("Thing", "pkg.Thing", kind="class"),
                       step("old", member, exists=False))}
    fact = _one(_diff(old, new, [TouchedSymbol(path=member, warnings=[w])],
                      extra=_live("pkg.update")), "REMOVED")
    assert fact.successor is None                      # pkg.update is not Thing's
    thing = {"pkg.Thing.update": res("pkg", step("Thing", "pkg.Thing", kind="class"),
                                     step("update", "pkg.Thing.update"))}
    fact = _one(_diff(old, new, [TouchedSymbol(path=member, warnings=[w])], extra=thing),
                "REMOVED")
    assert fact.successor == "pkg.Thing.update"


def test_new_side_error_text_names_the_successor_of_a_removed_name():
    """numpy 2's module ``__getattr__`` raises "Use `np.nan` instead": an import alias head
    stands for the package root."""
    old = {"pkg.NaN": res("pkg", step("NaN", "pkg.NaN", kind="float"))}
    new = {"pkg.NaN": res("pkg", step("NaN", "pkg.NaN", exists=False,
                                      error="AttributeError: `np.NaN` was removed in the "
                                            "NumPy 2.0 release. Use `np.nan` instead."))}
    extra = {"pkg.nan": res("pkg", step("nan", "pkg.nan", kind="float"))}
    (fact,) = _diff(old, new, extra=extra)
    assert fact.kind == "REMOVED" and fact.successor == "pkg.nan"
    assert fact.succession["sources"] == ["error"]


def test_a_body_that_warns_on_some_calls_names_its_successor():
    """polars 1.x ``count()`` warns only without columns, so it is not marked deprecated; its
    body still names ``pl.len``."""
    sp = "pkg.count"
    old = {sp: res("pkg", step("count", sp, shape=shape(("columns", "VAR_POSITIONAL", None))))}
    new = {sp: res("pkg", step("count", sp, shape=shape(("columns", "VAR_POSITIONAL", None)),
                               warns=[{"category": None, "message": "`pl.count()` is "
                                       "deprecated. Please use `pl.len()` instead."}]))}
    facts = _diff(old, new, extra=_live("pkg.len"))
    dep = _one(facts, "DEPRECATED")
    assert dep.successor == "pkg.len"
    assert dep.evidence[0].startswith("deprecated-in-new:in its body")


# ── parameters ───────────────────────────────────────────────────────────────────────────


def _helper_call(name, qualname, *args, **kwargs):
    return {"name": name, "qualname": qualname, "args": list(args), "kwargs": kwargs}


def test_a_rename_helper_alias_gone_in_the_new_version_is_a_removed_parameter():
    """polars 0.19 ``Expr.shift`` accepted ``periods`` only through
    ``deprecate_renamed_parameter("periods", "n")``; both signatures read ``(self, n=1)``."""
    sig = shape(("self", POK, None), ("n", POK, "1"))
    sp = "pkg.Expr.shift"
    helper = _helper_call("deprecate_renamed_parameter",
                          "polars.utils.deprecation.deprecate_renamed_parameter", "periods", "n",
                          version="0.19.11")
    old = {sp: res("pkg", step("Expr", "pkg.Expr", kind="class"),
                   step("shift", sp, shape=sig, decorator_calls=[helper],
                        body={"layers": [], "loads": {},
                              "decorators": ["polars.utils.deprecation."
                                             "deprecate_renamed_parameter"]}))}
    new = {sp: res("pkg", step("Expr", "pkg.Expr", kind="class"),
                   step("shift", sp, shape=sig))}
    fact = _one(_diff(old, new), "SIGNATURE", "periods")
    assert fact.successor == "n"
    assert fact.succession["trust"] == "proof"
    assert fact.succession["params"] == {"periods": "n"}
    assert fact.succession["proof"] == {
        "kind": "helper", "locus": "polars.utils.deprecation.deprecate_renamed_parameter",
        "version": "0.19.11", "side": "old"}


def test_a_closure_read_generically_names_but_proves_nothing():
    sig = shape(("self", POK, None), ("n", POK, "1"))
    sp = "pkg.Expr.shift"
    cells = [{"code": "renamer.<locals>.wrapper", "module": "pkg._util",
              "cells": {"old_name": "periods", "new_name": "n"}}]
    old = {sp: res("pkg", step("shift", sp, shape=sig, cells=cells))}
    new = {sp: res("pkg", step("shift", sp, shape=sig))}
    fact = _one(_diff(old, new), "SIGNATURE", "periods")
    assert fact.successor == "n" and fact.succession["trust"] == "named"


def test_a_parameter_the_new_version_renames_with_a_helper_is_deprecated_there():
    """matplotlib 3.10 ``boxplot`` still takes ``labels`` through ``rename_parameter``."""
    sp = "pkg.Axes.boxplot"
    helper = _helper_call("_api.rename_parameter", "matplotlib._api.deprecation.rename_parameter",
                          "3.9", "labels", "tick_labels")
    old = {sp: res("pkg", step("boxplot", sp, shape=shape(("self", POK, None), ("x", POK, None),
                                                          ("labels", POK, "None"))))}
    new = {sp: res("pkg", step("boxplot", sp, shape=shape(("self", POK, None), ("x", POK, None),
                                                          ("tick_labels", POK, "None")),
                               decorator_calls=[helper],
                               body={"layers": [], "loads": {}, "decorators": [
                                   "matplotlib._api.deprecation.rename_parameter"]}))}
    facts = _diff(old, new)
    fact = _one(facts, "DEPRECATED", "labels")
    assert fact.successor == "tick_labels"
    assert fact.succession["proof"]["side"] == "new"
    assert not [f for f in facts if f.kind == "SIGNATURE" and f.param == "labels"]


def test_param_successor_from_a_warning_helper_one_level_down():
    """flask 2.0 ``send_file``: ``_prepare_send_file_kwargs`` warns that 'attachment_filename'
    was renamed to 'download_name'."""
    sp = "pkg.send_file"
    helper = {"helper": "pkg.helpers._prepare", "params": ["download_name",
                                                           "attachment_filename"],
              "messages": ["The 'attachment_filename' parameter has been renamed to "
                           "'download_name'. The old name will be removed in 2.1."],
              "returns": None, "args": [], "kwargs": {}}
    old = {sp: res("pkg", step("send_file", sp, helper_warns=[helper], shape=shape(
        ("path", POK, None), ("download_name", POK, "None"),
        ("attachment_filename", POK, "None"))))}
    new = {sp: res("pkg", step("send_file", sp, shape=shape(
        ("path", POK, None), ("download_name", POK, "None"))))}
    fact = _one(_diff(old, new), "SIGNATURE", "attachment_filename")
    assert fact.successor == "download_name"
    assert fact.succession["trust"] == "named"


def test_a_parameter_both_versions_have_that_the_new_body_deprecates():
    """seaborn 0.13 ``kdeplot(shade=)``: ``fill`` replaces it; both signatures have both."""
    sp = "pkg.kdeplot"
    sig = shape(("shade", POK, "None"), ("fill", POK, "None"))
    old = {sp: res("pkg", step("kdeplot", sp, shape=sig))}
    new = {sp: res("pkg", step("kdeplot", sp, shape=sig, warns=[{
        "category": "FutureWarning",
        "message": "`shade` is now deprecated in favor of `fill`; setting `fill={...}`."}]))}
    fact = _one(_diff(old, new), "DEPRECATED", "shade")
    assert fact.successor == "fill"


def test_a_positional_pairing_is_a_hint_never_a_successor():
    sp = "pkg.reshape"
    old = {sp: res("pkg", step("reshape", sp, shape=shape(("a", POK, None),
                                                          ("newshape", POK, None))))}
    new = {sp: res("pkg", step("reshape", sp, shape=shape(("a", POK, None),
                                                          ("shape", POK, None))))}
    fact = _one(_diff(old, new), "SIGNATURE", "newshape")
    assert fact.successor is None
    assert fact.succession["candidates"] == [{"name": "shape", "source": "position"}]


# ── registered helpers ────────────────────────────────────────────────────────────────────


_FIXTURES = [(e, f) for e in succ_mod.registered_helpers()
             for f in (e.get("fixtures") or [e["fixture"]])]


@pytest.mark.parametrize("entry, fixture", _FIXTURES,
                         ids=[f"{e['qualname']}-{i}" for i, (e, _f) in enumerate(_FIXTURES)])
def test_every_registered_helper_reads_its_fixture(entry, fixture):
    if "bare" in fixture or fixture["expect"] == {"kind": "transparent"}:
        # a wrapper that only passes calls on: no fact, and it is known as such
        assert entry.get("wrapper") in ("warn", "dispatch", "keywords", "rename")
        assert succ_mod.helper_facts({"decorator_calls": [
            {"name": "x", "qualname": entry["qualname"], "args": [], "kwargs": {}}]}) == []
        return
    src = fixture.get("decorator") or fixture["call"]
    call = ast.parse(src, mode="eval").body
    args = [probe_mod._lit(a) for a in call.args]
    kwargs = {kw.arg: probe_mod._lit(kw.value) for kw in call.keywords}
    if "call" in fixture:
        stp = {"warns": [{"callee": entry["qualname"].rsplit(".", 1)[1], "args": args,
                          "kwargs": kwargs, "message": ""}]}
    else:
        stp = {"decorator_calls": [{"name": "x", "qualname": entry["qualname"], "args": args,
                                    "kwargs": kwargs}]}
    (fact,) = succ_mod.helper_facts(stp)
    got = {k: getattr(fact, k) for k in fixture["expect"]}
    assert got == fixture["expect"]


# ── the probe's raw extractors ────────────────────────────────────────────────────────────

_FAKE = '''
import functools, warnings


def renamer(old_name, new_name, version=None):
    def decorate(function):
        @functools.wraps(function)
        def wrapper(*args, **kwargs):
            if old_name in kwargs:
                warnings.warn("renamed", DeprecationWarning)
                kwargs[new_name] = kwargs.pop(old_name)
            return function(*args, **kwargs)
        return wrapper
    return decorate


def _check(new, old, fname):
    if new is not None:
        raise TypeError("pass only one")
    warnings.warn(f"the `old=` argument to {fname} was renamed to `new=`",
                  DeprecationWarning)
    return old


def issue_deprecation_warning(message, version):
    warnings.warn(message, DeprecationWarning)


class Expr:
    @renamer("periods", "n", version="0.1")
    def shift(self, n=1):
        return n

    def total(self, new=None, old=None):
        if old is not None:
            new = _check(new, old, "total")
        return new

    def count(self, *columns):
        if not columns:
            issue_deprecation_warning("`count()` is deprecated. Use `len()` instead.", "1.0")
        msg = (
            "`count` is going away. "
            "Use `size` instead."
        )
        warnings.warn(msg, FutureWarning)
        return 0
'''


def _probe(tmp_path, paths):
    pkg = tmp_path / "fakepkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(textwrap.dedent(_FAKE))
    req = {"paths": paths, "roots": ["fakepkg"], "per_path_timeout": 20}
    run = subprocess.run([sys.executable, probe_mod.__file__], input=json.dumps(req),
                         capture_output=True, text=True, cwd=str(tmp_path / ".."),
                         env={**os.environ, "PYTHONPATH": str(tmp_path)}, timeout=60)
    out = {}
    for line in run.stdout.splitlines():
        if line.startswith(probe_mod.MARK):
            msg = json.loads(line[len(probe_mod.MARK):])
            if "path" in msg:
                out[msg["path"]] = msg["result"]["steps"][-1]
    return out


def test_probe_reads_decorator_calls_and_closure_cells(tmp_path):
    st = _probe(tmp_path, ["fakepkg.Expr.shift"])["fakepkg.Expr.shift"]
    assert st["decorator_calls"] == [{"name": "renamer", "qualname": "fakepkg.renamer",
                                      "args": ["periods", "n"], "kwargs": {"version": "0.1"}}]
    (cells,) = st["cells"]
    assert cells["code"].startswith("renamer.<locals>.decorate.<locals>.wrapper") or \
        cells["code"] == "wrapper"
    assert cells["cells"]["old_name"] == "periods" and cells["cells"]["new_name"] == "n"
    # read generically (no registered helper): a rename, stamped as such
    (fact,) = succ_mod.helper_facts(st)
    assert (fact.kind, fact.old, fact.new, fact.registered) == ("rename-parameter", "periods",
                                                                "n", False)


def test_probe_reads_warning_helpers_one_level_down(tmp_path):
    st = _probe(tmp_path, ["fakepkg.Expr.total"])["fakepkg.Expr.total"]
    (helper,) = st["helper_warns"]
    assert helper["helper"] == "fakepkg._check"
    assert helper["returns"] == "old" and helper["assign"] == "new"
    assert helper["args"] == [{"p": "new"}, {"p": "old"}, {"c": "total"}]
    assert "renamed to `new=`" in helper["messages"][0]


def test_probe_follows_message_variables_and_package_warning_helpers(tmp_path):
    st = _probe(tmp_path, ["fakepkg.Expr.count"])["fakepkg.Expr.count"]
    messages = [w["message"] for w in st["warns"]]
    assert "`count()` is deprecated. Use `len()` instead." in messages
    assert "`count` is going away. Use `size` instead." in messages


def test_probe_extractors_keep_the_legacy_syntax():
    tree = ast.parse(Path(probe_mod.__file__).read_text(encoding="utf-8"), feature_version=(3, 6))
    assert not any(isinstance(n, ast.JoinedStr) for n in ast.walk(tree))
    assert probe_mod.PROBE_VERSION >= 9
