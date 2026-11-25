"""Proving successors (successors v2, stage S2): forwards, conditional forwards, twins,
aliases, identity, the transfer check and chains, and the codemods only proofs make. Pure over
fake probe answers (the shapes ``pymolt/delta/probe.py`` emits)."""

from __future__ import annotations

from pymolt.delta.engine import DiffInput, as_touched, diff
from pymolt.delta.models import DeltaReport, RecordedWarning, TouchedSymbol
from pymolt.delta.provider import successor_patterns

from ._fakes import missing, res, shape, step

POK, KW = "POSITIONAL_OR_KEYWORD", "KEYWORD_ONLY"


def _body(**summary):
    """A body summary as the probe makes one: every wrapper and decorator read (none here)."""
    return {"layers": [], "decorators": [], "loads": {}, **summary}


def _diff(old, new, symbols=None, *, extra=None, old_extra=None):
    return diff(DiffInput(
        old=old, new=new, touched=as_touched(symbols if symbols is not None else list(old)),
        root_to_package={"pkg": "pkg"},
        resolve_new=lambda paths: {p: r for p, r in (extra or {}).items() if p in paths},
        resolve_old=(lambda paths: {p: r for p, r in old_extra.items() if p in paths})
        if old_extra is not None else None,
    ))["pkg"]


def _member(cls, name, **kw):
    return res("pkg", step(cls, f"pkg.{cls}", kind="class"), step(name, f"pkg.{cls}.{name}", **kw))


def _report(facts):
    return DeltaReport(package="pkg", from_version="1", to_version="2", quality="complete",
                       facts=facts)


def _removed(facts):
    return next(f for f in facts if f.kind == "REMOVED")


GROUPBY = shape(("self", POK, None), ("by", POK, None), ("maintain_order", KW, "False"))


def test_a_pass_through_forward_proves_a_member_rename():
    fwd = {"callee": "self.group_by", "args": [{"p": "by"}],
           "kwargs": [["maintain_order", {"p": "maintain_order"}]]}
    old = {"pkg.Frame.groupby": _member(
        "Frame", "groupby", shape=GROUPBY, body=_body(forward=fwd),
        callee={"member": "group_by", "shape": GROUPBY, "spellings": []},
        warns=[{"message": "`groupby` is deprecated. It has been renamed to `group_by`."}])}
    new = {"pkg.Frame.groupby": res("pkg", step("Frame", "pkg.Frame", kind="class"),
                                    step("groupby", "pkg.Frame.groupby", exists=False))}
    extra = {"pkg.Frame.group_by": _member("Frame", "group_by", shape=GROUPBY)}
    fact = _removed(_diff(old, new, extra=extra))
    rec = fact.succession
    assert fact.successor == "pkg.Frame.group_by"
    assert rec["trust"] == "proof" and rec["proof"]["kind"] == "forward"
    assert rec["transfer"] == "successor-unchanged"
    assert rec["template"]["kind"] == "rename" and rec["template"]["member"]


def test_negative_in1d_a_forward_to_a_private_callee_proves_nothing():
    """numpy 2.3's ``in1d`` says "Use np.isin" but forwards to the private ``_in1d``, which
    flattens; ``isin`` keeps the shape. The forward proves neither."""
    old = {"pkg.in1d": res("pkg", step(
        "in1d", "pkg.in1d", shape=shape(("a", POK, None)),
        warns=[{"message": "`in1d` is deprecated. Use `np.isin` instead."}],
        body=_body(forward={"callee": "_in1d", "args": [{"p": "a"}], "kwargs": []}),
        callee={"spellings": ["pkg._in1d"], "shape": shape(("a", POK, None))}))}
    new = {"pkg.in1d": missing("pkg", "in1d")}
    extra = {"pkg.isin": res("pkg", step("isin", "pkg.isin", shape=shape(("a", POK, None)))),
             "pkg._in1d": res("pkg", step("_in1d", "pkg._in1d", shape=shape(("a", POK, None))))}
    fact = _removed(_diff(old, new, extra=extra))
    assert fact.successor == "pkg.isin"
    assert fact.succession["trust"] == "named"
    assert any("private" in r["why"] for r in fact.succession["rejected"])
    patterns, _ = successor_patterns(_report([fact]), None)
    assert patterns == []


def test_negative_map_dict_a_chain_names_replace_strict_and_rewrites_nothing():
    """polars: ``map_dict(m)`` forwards to ``replace(m, default=default, ...)`` in 0.19; in
    1.x ``replace`` deprecates ``default`` for ``replace_strict``. The chain names
    replace_strict, an agent step: the middle hop changed."""
    old_replace = shape(("self", POK, None), ("mapping", POK, None), ("default", KW, "None"))
    new_replace = shape(("self", POK, None), ("old", POK, None), ("new", POK, "<no_default>"),
                        ("default", KW, "<no_default>"))
    fwd = {"callee": "self.replace", "args": [{"p": "mapping"}],
           "kwargs": [["default", {"p": "default"}]]}
    old = {"pkg.Expr.map_dict": _member(
        "Expr", "map_dict", shape=old_replace, body=_body(forward=fwd),
        callee={"member": "replace", "shape": old_replace, "spellings": []},
        warns=[{"message": "`map_dict` is deprecated. It has been renamed to `replace`. The "
                           "default behavior has changed."}])}
    new = {"pkg.Expr.map_dict": res("pkg", step("Expr", "pkg.Expr", kind="class"),
                                    step("map_dict", "pkg.Expr.map_dict", exists=False))}
    extra = {"pkg.Expr.replace": _member("Expr", "replace", shape=new_replace, warns=[{
        "message": "The `default` parameter for `replace` is deprecated. Use `replace_strict` "
                   "instead to set a default while replacing values."}]),
        "pkg.Expr.replace_strict": _member("Expr", "replace_strict", shape=new_replace)}
    fact = _removed(_diff(old, new, extra=extra))
    rec = fact.succession
    assert fact.successor == "pkg.Expr.replace_strict"
    assert rec["trust"] == "named" and rec["transfer"].startswith("successor-changed")
    assert rec["chain"][0]["resolved"] == "pkg.Expr.replace_strict"
    assert successor_patterns(_report([fact]), None)[0] == []


def test_a_twin_body_proves_a_rename_at_the_target():
    """aiohttp ``has_body``: its body, apart from the warning, is ``can_read_body``'s."""
    body = _body(key="same-body")
    old = {"pkg.Req.has_body": _member("Req", "has_body", kind="property", body=body)}
    new = {"pkg.Req.has_body": res("pkg", step("Req", "pkg.Req", kind="class"), step(
        "has_body", "pkg.Req.has_body", kind="property", body=body,
        deprecated_attr="Deprecated, use .can_read_body #2005"))}
    extra = {"pkg.Req.can_read_body": _member("Req", "can_read_body", kind="property",
                                              body=body)}
    fact = next(f for f in _diff(old, new, extra=extra) if f.kind == "DEPRECATED")
    assert fact.successor == "pkg.Req.can_read_body"
    assert fact.succession["proof"]["kind"] == "twin"
    assert fact.succession["transfer"] == "at-target"


def test_a_target_side_proof_needs_the_old_implementation_unchanged():
    """pydantic 2's ``BaseModel.json`` forwards to ``model_dump_json``; pydantic 1's was its
    own (compiled) implementation, writing other bytes. The target's proof says nothing about
    the old calls: no rewrite (an agent step with the suggestion)."""
    fwd = {"callee": "self.model_dump_json", "args": [], "kwargs": []}
    sig = shape(("self", POK, None))
    old = {"pkg.Model.json": _member("Model", "json", shape=sig)}
    new = {"pkg.Model.json": res("pkg", step("Model", "pkg.Model", kind="class"), step(
        "json", "pkg.Model.json", shape=sig, body=_body(forward=fwd, key="k2"),
        callee={"member": "model_dump_json", "shape": sig, "spellings": []},
        deprecated_attr="The `json` method is deprecated; use `model_dump_json` instead."))}
    extra = {"pkg.Model.model_dump_json": _member("Model", "model_dump_json", shape=sig)}
    fact = next(f for f in _diff(old, new, extra=extra) if f.kind == "DEPRECATED")
    assert fact.successor == "pkg.Model.model_dump_json"
    assert fact.succession["transfer"].startswith("old-changed")
    assert successor_patterns(_report([fact]), None)[0] == []


def test_identity_in_the_old_version_beside_the_naming_text():
    """numpy 1.26: ``np.NaN is np.nan``; numpy 2 names ``np.nan`` in its error."""
    old = {"pkg.NaN": res("pkg", step("NaN", "pkg.NaN", kind="float"))}
    new = {"pkg.NaN": res("pkg", step("NaN", "pkg.NaN", exists=False,
                                      error="AttributeError: `np.NaN` was removed. Use `np.nan` "
                                            "instead."))}
    extra = {"pkg.nan": res("pkg", step("nan", "pkg.nan", kind="float"))}
    same = {"pkg.NaN": res("pkg", step("NaN", "pkg.NaN", kind="float", oid=7)),
            "pkg.nan": res("pkg", step("nan", "pkg.nan", kind="float", oid=7))}
    fact = _removed(_diff(old, new, extra=extra, old_extra=same))
    assert fact.succession["trust"] == "identity"
    (pattern,) = successor_patterns(_report([fact]), None)[0]
    assert (pattern.kind, pattern.old_qualname, pattern.new_qualname) == (
        "replace-attr-path", "pkg.NaN", "pkg.nan")


def test_negative_timezone_utc_another_object_is_no_identity():
    """Django 3.2: ``timezone.utc`` is ``pytz.UTC``, not ``datetime.timezone.utc``: named, never
    rewritten."""
    w = RecordedWarning(category="RemovedInDjango50Warning",
                        message="The django.utils.timezone.utc alias is deprecated. Please "
                                "update your code to use datetime.timezone.utc instead.")
    old = {"pkg.utc": res("pkg", step("utc", "pkg.utc", kind="UTC"))}
    new = {"pkg.utc": missing("pkg", "utc")}
    extra = {"datetime.timezone.utc": res("datetime", step("timezone", "datetime.timezone",
                                                           kind="class"),
                                          step("utc", "datetime.timezone.utc", kind="timezone"))}
    other = {"pkg.utc": res("pkg", step("utc", "pkg.utc", kind="UTC", oid=1)),
             "datetime.timezone.utc": res("datetime", step("timezone", "datetime.timezone",
                                                           kind="class"),
                                          step("utc", "datetime.timezone.utc", oid=2))}
    fact = _removed(_diff(old, new, [TouchedSymbol(path="pkg.utc", warnings=[w])], extra=extra,
                          old_extra=other))
    assert fact.successor == "datetime.timezone.utc"
    assert fact.succession["trust"] == "named"
    assert successor_patterns(_report([fact]), None)[0] == []


def _param_world(old_extra=None, new_extra=None):
    sig_old = shape(("data", POK, None), ("height", POK, "5"), ("size", POK, "None"))
    sig_new = shape(("data", POK, None), ("height", POK, "5"))
    old = {"pkg.lmplot": res("pkg", step("lmplot", "pkg.lmplot", shape=sig_old, warns=[{
        "message": "The `size` parameter has been renamed to `height`; please update your "
                   "code."}], **(old_extra or {})))}
    new = {"pkg.lmplot": res("pkg", step("lmplot", "pkg.lmplot", shape=sig_new,
                                         **(new_extra or {})))}
    return old, new


def test_an_alias_in_the_old_body_proves_a_keyword_rename():
    old, new = _param_world({"body": _body(aliases=[{"old": "size", "new": "height",
                                                     "how": "assign"}], loads={"size": 1})})
    fact = next(f for f in _diff(old, new) if f.param == "size")
    assert fact.successor == "height"
    assert fact.succession["trust"] == "proof" and fact.succession["proof"]["kind"] == "alias"
    (pattern,) = successor_patterns(_report([fact]), None)[0]
    assert pattern.kind == "rename-keyword" and pattern.params == {"size": "height"}
    assert pattern.template["blocked"] == []


def test_a_helper_that_hands_one_parameter_back_as_another_proves_it():
    """numpy 1.26 ``percentile``: ``method = _check_interpolation_as_method(method,
    interpolation, "percentile")`` and the helper returns ``interpolation`` unchanged."""
    helper = {"helper": "pkg._check", "params": ["method", "interpolation", "fname"],
              "returns": "interpolation", "assign": "method",
              "args": [{"p": "method"}, {"p": "interpolation"}, {"c": "percentile"}],
              "kwargs": {}, "guard": "not_none",
              "messages": ["the `interpolation=` argument to {...} was renamed to `method=`"]}
    sig_old = shape(("a", POK, None), ("method", POK, "'linear'"), ("interpolation", KW, "None"))
    sig_new = shape(("a", POK, None), ("method", POK, "'linear'"))
    old = {"pkg.percentile": res("pkg", step("percentile", "pkg.percentile", shape=sig_old,
                                             helper_warns=[helper],
                                             body=_body(loads={"interpolation": 2})))}
    new = {"pkg.percentile": res("pkg", step("percentile", "pkg.percentile", shape=sig_new))}
    facts = _diff(old, new)
    fact = next(f for f in facts if f.param == "interpolation")
    assert fact.successor == "method" and fact.succession["proof"]["kind"] == "alias"
    assert not [f for f in facts if f.param == "method"]   # a rename's target: no deprecation


def test_a_named_parameter_successor_without_proof_makes_no_rewrite():
    old, new = _param_world()
    fact = next(f for f in _diff(old, new) if f.param == "size")
    assert fact.successor == "height" and fact.succession["trust"] == "named"
    assert successor_patterns(_report([fact]), None)[0] == []


def test_a_forwards_callee_is_spelled_in_the_new_version_without_any_text():
    """Django 2.2's ``url`` forwards to ``re_path`` and says nothing: the callee's own public
    spelling is looked up in the new version all the same."""
    sig = shape(("regex", POK, None), ("view", POK, None))
    callee = shape(("route", POK, None), ("view", POK, None))
    old = {"pkg.conf.url": res("pkg.conf", step(
        "url", "pkg.conf.url", shape=sig,
        body=_body(forward={"callee": "re_path", "args": [{"p": "regex"}, {"p": "view"}],
                            "kwargs": []}),
        callee={"spellings": ["pkg.urls.re_path"], "shape": callee}))}
    new = {"pkg.conf.url": missing("pkg.conf", "url")}
    extra = {"pkg.urls.re_path": res("pkg.urls", step("re_path", "pkg.urls.re_path",
                                                      shape=callee))}
    fact = _removed(_diff(old, new, extra=extra))
    assert fact.successor == "pkg.urls.re_path" and fact.succession["trust"] == "proof"
    (pattern,) = successor_patterns(_report([fact]), None)[0]
    assert pattern.kind == "rename-call"
    assert pattern.template == {"forbid_keywords": ["regex"]}
