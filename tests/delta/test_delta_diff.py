"""pymolt.delta.engine.diff over fake probe answers: every fact kind, MOVED vs REMOVED,
successor-hint order. Pure: no interpreter, no uv, no network."""

from __future__ import annotations

from pymolt.delta.engine import DiffInput, as_touched, bases_change, diff, param_changes
from pymolt.delta.models import RecordedWarning, TouchedSymbol

from ._fakes import missing, res, shape, step

POK = "POSITIONAL_OR_KEYWORD"
KW = "KEYWORD_ONLY"


def _diff(old, new, symbols=None, *, extra=None, knowledge=None, roots=None):
    touched = as_touched(symbols if symbols is not None else list(old))
    calls = []

    def resolve_new(paths):
        calls.append(sorted(paths))
        return {p: r for p, r in (extra or {}).items() if p in paths}

    facts = diff(DiffInput(
        old=old, new=new, touched=touched,
        root_to_package=roots or {"pkg": "pkg", "dep": "dep"},
        resolve_new=resolve_new, knowledge_hint=knowledge,
    ))
    return facts, calls


def _by_kind(facts, kind):
    return [f for f in facts if f.kind == kind]


# ── parameter level ─────────────────────────────────────────────────────────────────


def test_param_changes_cover_every_signature_kind():
    old = shape(("a", POK, None), ("b", POK, "1"), ("c", POK, "None"), ("args", "VAR_POSITIONAL",
                                                                         None))
    new = shape(("a", KW, None), ("b", POK, "2"), ("d", KW, None))
    changes = {(c.kind, c.param, c.old, c.new) for c in param_changes(old, new)}
    assert ("SIGNATURE", "a", POK, KW) in changes                 # kind
    assert ("DEFAULT", "b", "1", "2") in changes                  # default
    assert ("SIGNATURE", "c", POK, None) in changes               # removed
    assert ("SIGNATURE", "args", "VAR_POSITIONAL", None) in changes  # *args removed
    assert ("SIGNATURE", "d", None, f"{KW} (required)") in changes   # added as required


def test_param_changes_rename_and_kwargs_absorption():
    old = shape(("status", POK, None), ("args", "VAR_POSITIONAL", None),
                ("kwargs", "VAR_KEYWORD", None))
    new = shape(("code", POK, None), ("args", "VAR_POSITIONAL", None),
                ("kwargs", "VAR_KEYWORD", None))
    assert [(c.kind, c.param, c.old, c.new) for c in param_changes(old, new)] == [
        ("SIGNATURE", "status", "status", "code")]
    # a removed parameter the new **kwargs still binds is not a break
    gone = param_changes(shape(("x", POK, None), ("y", POK, "0")),
                         shape(("x", POK, None), ("kw", "VAR_KEYWORD", None)))
    assert gone == []


def test_sentinel_defaults_compare_equal():
    assert param_changes(shape(("a", POK, "<no_default>")),
                         shape(("a", POK, "<NoDefault.no_default>"))) == []


def test_bases_change():
    o = {"kind": "class", "bases": ["pkg.scaffold.Scaffold"]}
    n = {"kind": "class", "bases": ["pkg.sansio.app.App"]}
    assert bases_change(o, n).old == "pkg.scaffold.Scaffold"
    assert bases_change(o, {"kind": "class", "bases": ["pkg.moved.Scaffold"]}) is None


# ── REMOVED vs MOVED ────────────────────────────────────────────────────────────────


def test_removed_when_the_object_is_nowhere_in_new():
    go = shape(("self", POK, None))
    old = {"pkg.mod.Thing.go": res("pkg.mod",
                                   step("Thing", "pkg.mod.Thing", kind="class"),
                                   step("go", "pkg.mod.Thing.go", shape=go))}
    new = {"pkg.mod.Thing.go": res("pkg.mod", step("Thing", "pkg.mod.Thing", kind="class"),
                                   step("go", "pkg.mod.Thing.go", exists=False))}
    facts, _ = _diff(old, new)
    (removed,) = _by_kind(facts["pkg"], "REMOVED")
    assert removed.symbol == "pkg.mod.Thing.go" and removed.successor is None
    assert "absent in new: AttributeError: go" in removed.evidence
    assert removed.detail["online_query"] is None  # no module file known: nothing to fetch
    # the class itself (a touched prefix) is unchanged, not removed
    assert [f.symbol for f in _by_kind(facts["pkg"], "UNCHANGED")] == ["pkg.mod.Thing"]


def test_dropped_alias_is_moved_to_the_same_object():
    old = {"pkg.Markup": res("pkg", step("Markup", "pkg.Markup", kind="class",
                                         canonical="dep.Markup"))}
    new = {"pkg.Markup": missing("pkg", "Markup")}
    extra = {"dep.Markup": res("dep", step("Markup", "dep.Markup", kind="class"))}
    facts, calls = _diff(old, new, extra=extra)
    assert calls == [["dep.Markup"]]  # the second pass asks only for the old object's name
    (moved,) = facts["pkg"]
    assert (moved.kind, moved.symbol, moved.successor) == ("MOVED", "pkg.Markup", "dep.Markup")
    assert moved.old == "dep.Markup" and moved.new == "dep.Markup"
    assert moved.detail["importable"] == ["pkg.Markup"]
    assert moved.detail["successor_importable"] is True


def test_base_class_rehome_is_moved_not_removed():
    # flask 3.0: Scaffold.route moved into flask.sansio; app.route still works. The trace
    # recorded the old defining name; the project spells the public one.
    route = shape(("self", POK, None), ("rule", POK, None))
    old = {
        "pkg.App.route": res("pkg", step("App", "pkg.App", kind="class", canonical="pkg.app.App",
                                         bases=["pkg.scaffold.Scaffold"]),
                             step("route", "pkg.scaffold.Scaffold.route", shape=route)),
        "pkg.scaffold.Scaffold.route": res(
            "pkg.scaffold", step("Scaffold", "pkg.scaffold.Scaffold", kind="class"),
            step("route", "pkg.scaffold.Scaffold.route", shape=route)),
    }
    new = {
        "pkg.App.route": res("pkg", step("App", "pkg.App", kind="class", canonical="pkg.app.App",
                                         bases=["pkg.scaffold.Scaffold"]),
                             step("route", "pkg.sansio.Scaffold.route", shape=route)),
        "pkg.scaffold.Scaffold.route": missing("pkg", "scaffold"),
    }
    symbols = ["pkg.App.route", TouchedSymbol(path="pkg.scaffold.Scaffold.route", origin="trace")]
    facts, _ = _diff(old, new, symbols)
    kinds = {(f.kind, f.symbol) for f in facts["pkg"]}
    assert ("MOVED", "pkg.scaffold.Scaffold.route") in kinds
    assert not _by_kind(facts["pkg"], "REMOVED")  # neither the method nor its old class
    (moved,) = _by_kind(facts["pkg"], "MOVED")
    assert moved.successor == "pkg.sansio.Scaffold.route"
    assert moved.detail["importable"] == []  # a class member: no import to rewrite
    unchanged = {f.symbol for f in _by_kind(facts["pkg"], "UNCHANGED")}
    assert "pkg.app.App" in unchanged


def test_rebound_spelling_is_reported_on_the_spelling():
    old = {"pkg.abort": res("pkg", step("abort", "pkg.abort", canonical="dep.abort",
                                        shape=shape(("status", POK, None))))}
    new = {"pkg.abort": res("pkg", step("abort", "pkg.abort", canonical="pkg.helpers.abort",
                                        shape=shape(("code", POK, None))))}
    extra = {"dep.abort": res("dep", step("abort", "dep.abort",
                                          shape=shape(("status", POK, None))))}
    facts, _ = _diff(old, new, extra=extra)
    (sig,) = facts["pkg"]
    assert (sig.kind, sig.symbol, sig.param, sig.new) == ("SIGNATURE", "pkg.abort", "status",
                                                          "code")
    assert "rebound: dep.abort -> pkg.helpers.abort" in sig.evidence


# ── SIGNATURE / DEFAULT / DEPRECATED / UNCHANGED ─────────────────────────────────────


def test_default_change_with_baseline_warning_matches_the_schema_example():
    mean_old = shape(("self", POK, None), ("numeric_only", POK, "None"))
    mean_new = shape(("self", POK, None), ("numeric_only", POK, "False"))
    canon = "pandas.core.frame.DataFrame.mean"

    def mean(sh):
        return res("pandas", step("DataFrame", "pandas.DataFrame", kind="class",
                                  canonical="pandas.core.frame.DataFrame"),
                   step("mean", canon, shape=sh))

    warn = RecordedWarning(category="FutureWarning",
                           message="The default value of numeric_only in DataFrame.mean is "
                                   "deprecated.")
    symbols = [TouchedSymbol(path="pandas.DataFrame.mean", origin="contact", warnings=[warn])]
    facts, _ = _diff({"pandas.DataFrame.mean": mean(mean_old)},
                     {"pandas.DataFrame.mean": mean(mean_new)}, symbols,
                     roots={"pandas": "pandas"})
    (default,) = _by_kind(facts["pandas"], "DEFAULT")
    assert default.model_dump() == {
        "symbol": canon, "aliases": ["pandas.DataFrame.mean"], "kind": "DEFAULT",
        "param": "numeric_only", "old": "None", "new": "False", "deprecated_in_old": True,
        "successor": None,
        "evidence": ["signature", f"warning:FutureWarning: {warn.message}"],
    }


def test_bases_and_signature_facts():
    old = {"pkg.Widget": res("pkg", step("Widget", "pkg.Widget", kind="class",
                                         canonical="pkg._impl.Widget", bases=["pkg.core.Base"],
                                         shape=shape(("self", POK, None))))}
    new = {"pkg.Widget": res("pkg", step("Widget", "pkg.Widget", kind="class",
                                         canonical="pkg._impl.Widget", bases=["pkg.core.Runner"],
                                         shape=shape(("self", POK, None), ("depth", KW, None))))}
    facts, _ = _diff(old, new)
    sigs = {(f.param, f.symbol, tuple(f.aliases)) for f in _by_kind(facts["pkg"], "SIGNATURE")}
    assert (None, "pkg._impl.Widget", ("pkg.Widget",)) in sigs      # bases
    assert ("depth", "pkg._impl.Widget", ("pkg.Widget",)) in sigs   # added as required
    bases = next(f for f in facts["pkg"] if f.param is None)
    assert bases.evidence[0] == "bases"


def test_deprecated_in_new_from_a_pep562_branch():
    getattr_warn = [{"category": "DeprecationWarning",
                     "message": "The '__version__' attribute is deprecated. Use "
                                "importlib.metadata.version instead."}]
    old = {"pkg.__version__": res("pkg", step("__version__", "pkg.__version__", kind="str"))}
    new = {"pkg.__version__": res("pkg", step("__version__", "pkg.__version__", kind="str",
                                              getattr_warns=getattr_warn))}
    live = {"importlib.metadata.version": res(
        "importlib.metadata", step("version", "importlib.metadata.version"))}
    facts, _ = _diff(old, new, extra=live)
    (dep,) = facts["pkg"]
    assert dep.kind == "DEPRECATED" and dep.deprecated_in_old is False
    assert dep.evidence[0].startswith("deprecated-in-new:getattr:")
    assert dep.successor == "importlib.metadata.version"
    # named but absent from the new version: rejected, with the reason
    facts, _ = _diff(old, new)
    (dep,) = facts["pkg"]
    assert dep.successor is None
    assert dep.succession["rejected"][0]["name"] == "importlib.metadata.version"


def test_deprecated_in_old_only_from_the_baseline_warning():
    fn = shape(("x", POK, None))
    old = {"pkg.f": res("pkg", step("f", "pkg.f", shape=fn))}
    new = {"pkg.f": res("pkg", step("f", "pkg.f", shape=fn))}
    w = RecordedWarning(category="DeprecationWarning", message="f is deprecated")
    facts, _ = _diff(old, new, [TouchedSymbol(path="pkg.f", warnings=[w])])
    (dep,) = facts["pkg"]
    assert dep.kind == "DEPRECATED" and dep.deprecated_in_old is True
    assert dep.evidence == ["warning:DeprecationWarning: f is deprecated"]


def test_unchanged_and_package_attribution():
    fn = shape(("x", POK, None))
    old = {"pkg.f": res("pkg", step("f", "pkg.f", shape=fn)),
           "dep.g": res("dep", step("g", "dep.g", shape=fn))}
    new = {"pkg.f": res("pkg", step("f", "pkg.f", shape=fn)),
           "dep.g": res("dep", step("g", "dep.g", shape=shape(("y", POK, None))))}
    facts, _ = _diff(old, new, roots={"pkg": "Pkg", "dep": "Dep"})
    assert [f.kind for f in facts["Pkg"]] == ["UNCHANGED"]
    assert [f.kind for f in facts["Dep"]] == ["SIGNATURE"]


# ── successor hints, in the documented order ─────────────────────────────────────────


def _removed_with(doc=None, warns=None, symbols=None, knowledge=None, extra=None):
    st = step("old", "pkg.old", shape=shape(("x", POK, None)), owner_file="pkg/__init__.py",
              **({"doc": doc} if doc else {}), **({"warns": warns} if warns else {}))
    old = {"pkg.old": res("pkg", st)}
    new = {"pkg.old": missing("pkg", "old")}
    facts, _ = _diff(old, new, symbols, knowledge=knowledge, extra=extra)
    (removed,) = _by_kind(facts["pkg"], "REMOVED")
    return removed


def _live(*names):
    return {n: res("pkg", step(n.rsplit(".", 1)[1], n, shape=shape(("x", POK, None))))
            for n in names}


def test_hint_1_warning_and_docstring_that_agree_name_the_successor():
    w = RecordedWarning(category="DeprecationWarning", message="old() is deprecated. "
                        "Use pkg.new_one instead.")
    fact = _removed_with(doc={"directive": [".. deprecated:: 1.0 Use :func:`new_one` instead."]},
                         symbols=[TouchedSymbol(path="pkg.old", warnings=[w])],
                         knowledge=lambda pkg, names: ("pkg.from_knowledge", "knowledge:k"),
                         extra=_live("pkg.new_one", "pkg.from_knowledge"))
    assert fact.successor == "pkg.new_one"
    assert "successor:warning" in fact.evidence
    assert fact.succession["sources"] == ["warning", "docstring"]
    assert fact.succession["trust"] == "named"
    assert fact.deprecated_in_old is True


def test_hint_1_texts_naming_two_objects_name_candidates_not_a_successor():
    # The owner's rule: choosing among several candidates is an agent's or a person's job.
    w = RecordedWarning(category="DeprecationWarning", message="old() is deprecated. "
                        "Use pkg.from_warning instead.")
    fact = _removed_with(doc={"directive": [".. deprecated:: 1.0 Use pkg.from_doc instead."]},
                         symbols=[TouchedSymbol(path="pkg.old", warnings=[w])],
                         extra=_live("pkg.from_warning", "pkg.from_doc"))
    assert fact.successor is None
    assert [c["name"] for c in fact.succession["candidates"]] == ["pkg.from_warning",
                                                                  "pkg.from_doc"]


def test_hint_1_a_named_successor_must_exist_in_the_new_version():
    w = RecordedWarning(category="DeprecationWarning", message="old() is deprecated. "
                        "Use pkg.get_metric instead.")
    fact = _removed_with(symbols=[TouchedSymbol(path="pkg.old", warnings=[w])])
    assert fact.successor is None
    assert fact.succession["rejected"] == [{"name": "pkg.get_metric",
                                            "why": "does not exist in the new version"}]


def test_hint_2_docstring_and_sphinx_roles_resolve_against_the_new_version():
    same = shape(("x", POK, None))
    fact = _removed_with(
        doc={"directive": [".. deprecated:: 1.4.0 Use :func:`concat` instead."]},
        knowledge=lambda pkg, names: ("pkg.from_knowledge", "knowledge:k"),
        extra={"pkg.concat": res("pkg", step("concat", "pkg.concat", shape=same))},
    )
    assert fact.successor == "pkg.concat"  # "concat" qualified because pkg.concat exists in new
    assert "successor:docstring" in fact.evidence
    assert fact.detail["successor_same_signature"] is True
    assert fact.detail["successor_importable"] is True


def test_hint_3_knowledge_when_the_old_version_says_nothing():
    fact = _removed_with(knowledge=lambda pkg, names: ("pkg.from_knowledge", "knowledge:k"),
                         extra=_live("pkg.from_knowledge"))
    assert fact.successor == "pkg.from_knowledge"
    assert "successor:knowledge:k" in fact.evidence
    assert fact.succession["trust"] == "curated"


def test_hint_4_is_left_to_the_online_fetch():
    fact = _removed_with()
    assert fact.successor is None
    assert fact.detail["online_query"] == {"member": "pkg/__init__.py", "chain": ["old"]}
