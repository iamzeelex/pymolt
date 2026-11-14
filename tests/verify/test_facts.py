"""Fact model v2 (host): loading, site text, fold and the categorised fact diff."""
import json

import pytest

from pymolt.verify.facts import (
    Comparison,
    ComparisonLevel,
    FactCategory,
    Outcome,
    collect_site_text,
    compare_outcomes,
    diff_facts,
    diff_traces,
    fold_facts,
    load_facts,
    load_site_text,
    outcome_type,
    store_site_text,
    value_type,
)
from pymolt.verify.models import VerificationVerdict

ROOT = "/proj"
APP = "/proj/app.py"

S1 = {"__structure__": "pandas.core.series.Series", "shape": [1], "dtype": "int64",
      "values": {"__list__": [1]}}
S2 = {"__structure__": "pandas.core.series.Series", "shape": [1], "dtype": "int64",
      "values": {"__list__": [2]}}
S12 = {"__structure__": "pandas.core.series.Series", "shape": [2], "dtype": "int64",
       "values": {"__list__": [1, 2]}}
S13 = {"__structure__": "pandas.core.series.Series", "shape": [2], "dtype": "int64",
       "values": {"__list__": [1, 3]}}


def ev(q, bound=None, *, result=None, raised=None, sym=None, symq=None, defaulted=None,
       file=APP, line=10, func="main", v=2):
    """One JSONL event; ``v=1`` writes the phase-0 shape (no v/sym/defaulted)."""
    e = {"t": "raise" if raised else "return", "q": q, "in": {"bound": dict(bound or {})},
         "where": {"file": file, "line": line, "func": func}, "ts": 0.0}
    if raised:
        e["raised"] = raised
    else:
        e["result"] = result
    if v == 2:
        e["v"] = 2
        if sym:
            e["sym"] = sym
        if symq:
            e["symq"] = symq
        if defaulted is not None:
            e["in"]["defaulted"] = list(defaulted)
    return e


def write_trace(tmp_path, name, events, site_text=None):
    p = tmp_path / name
    p.write_text("".join(json.dumps(e) + "\n" for e in events))
    if site_text is not None:
        (tmp_path / f"{name}.meta.json").write_text(json.dumps({"site_text": site_text}))
    return p


def run_diff(tmp_path, old, new, *, old_text=None, new_text=None, profile="exact"):
    o = write_trace(tmp_path, "old.jsonl", old, old_text)
    n = write_trace(tmp_path, "new.jsonl", new, new_text)
    return diff_traces(o, n, root=ROOT, profile=profile)


def only(diff, category):
    found = diff.by_category(category)
    assert len(found) == 1, diff.counts()
    return found[0]


def text_at(line, text, file=APP):
    return {f"{file}:{line}": text}


# ── acceptance scenarios ──────────────────────────────────────────────────────────────────


def test_pandas_decorator_wrapper_and_direct_call_are_the_same_fact(tmp_path):
    concat = "pandas.core.reshape.concat.concat"
    bound = {"objs": {"__list__": [S1, S2]}}
    site = text_at(10, "s = pd.concat([s1, s2])")
    old = [ev("pandas.util._decorators.wrapper", bound, result=S12, sym=concat,
              symq="unwrapped", defaulted=["axis", "join"])]
    new = [ev(concat, bound, result=S12, sym=concat, defaulted=["axis", "join"])]
    d = run_diff(tmp_path, old, new, old_text=site, new_text=site)
    assert d.counts()["equal"] == 1
    assert d.is_clean() and d.verdict is VerificationVerdict.PASS
    entry = only(d, FactCategory.EQUAL)
    assert entry.level is ComparisonLevel.EXACT and entry.new_symbol is None


def test_library_added_defaulted_parameter_is_drift_not_changed(tmp_path):
    concat = "pandas.core.reshape.concat.concat"
    bound = {"objs": {"__list__": [S1, S2]}}
    site = text_at(10, "s = pd.concat([s1, s2])")
    old = [ev(concat, bound, result=S12, sym=concat, defaulted=["axis", "join"])]
    new = [ev(concat, bound, result=S12, sym=concat, defaulted=["axis", "join", "sort"])]
    d = run_diff(tmp_path, old, new, old_text=site, new_text=site)
    drift = only(d, FactCategory.DRIFT)
    assert drift.drift and drift.comparison is Comparison.EQUAL
    assert drift.old_defaulted == [["axis", "join"]]
    assert drift.new_defaulted == [["axis", "join", "sort"]]
    assert d.is_clean() and d.verdict is VerificationVerdict.PASS
    assert d.to_boundary_diff().counts() == {"disappeared": 0, "result_changed": 0,
                                             "raise_changed": 0, "appeared": 0,
                                             "skipped_opaque": 0}


def test_changed_library_default_pairs_modulo_defaulted_names(tmp_path):
    # The caller passes flag=True; the new version's default became True, so the runtime now
    # reports `flag` as defaulted and drops it from `bound` -- same call, different key.
    site = text_at(10, "lib.f(1, flag=True)")
    old = [ev("lib.f", {"x": 1, "flag": True}, result=1, sym="lib.f", defaulted=[])]
    new = [ev("lib.f", {"x": 1}, result=1, sym="lib.f", defaulted=["flag"])]
    d = run_diff(tmp_path, old, new, old_text=site, new_text=site)
    drift = only(d, FactCategory.DRIFT)
    assert "matched-modulo-defaults" in drift.notes
    assert drift.new_input == {"bound": {"x": 1}}
    assert d.is_clean()


def _rewrite_case(tmp_path, new_result):
    old = [ev("pandas.core.series.Series.append", {"self": S1, "to_append": S2}, result=S12,
              sym="pandas.core.series.Series.append", line=10)]
    new = [ev("pandas.core.reshape.concat.concat", {"objs": {"__list__": [S1, S2]}},
              result=new_result, sym="pandas.core.reshape.concat.concat", line=10)]
    return run_diff(tmp_path, old, new, old_text=text_at(10, "s3 = s1.append(s2)"),
                    new_text=text_at(10, "s3 = pd.concat([s1, s2])"))


def test_rewritten_site_with_equal_outcome_passes(tmp_path):
    d = _rewrite_case(tmp_path, S12)
    entry = only(d, FactCategory.REWRITTEN)
    assert entry.comparison is Comparison.EQUAL and not entry.fails
    assert entry.symbol == "pandas.core.series.Series.append"
    assert entry.new_symbol == "pandas.core.reshape.concat.concat"
    assert entry.old_site.text == "s3 = s1.append(s2)"
    assert entry.new_site.text == "s3 = pd.concat([s1, s2])"
    assert "positional" in entry.notes
    assert d.is_clean() and d.verdict is VerificationVerdict.PASS


def test_rewritten_site_with_different_outcome_fails(tmp_path):
    d = _rewrite_case(tmp_path, S13)
    entry = only(d, FactCategory.REWRITTEN)
    assert entry.comparison is Comparison.CHANGED and entry.fails
    assert entry.old[0].value == S12 and entry.new[0].value == S13
    assert not d.is_clean() and d.verdict is VerificationVerdict.FAIL
    assert len(d.to_boundary_diff().result_changed) == 1


def test_int_to_float_is_changed_at_exact_and_equal_at_shape(tmp_path):
    site = text_at(10, "n = lib.count(xs)")
    old = [ev("lib.count", {"xs": 1}, result=3, sym="lib.count")]
    new = [ev("lib.count", {"xs": 1}, result=3.0, sym="lib.count")]
    exact = run_diff(tmp_path, old, new, old_text=site, new_text=site)
    changed = only(exact, FactCategory.CHANGED)
    assert changed.level is ComparisonLevel.EXACT
    assert changed.old[0].value == 3 and changed.new[0].value == 3.0
    assert exact.verdict is VerificationVerdict.FAIL
    legacy = exact.to_boundary_diff()
    assert legacy.result_changed[0]["old"] == 3 and legacy.result_changed[0]["new"] == 3.0

    shape = run_diff(tmp_path, old, new, old_text=site, new_text=site, profile="shape")
    equal = only(shape, FactCategory.EQUAL)
    assert equal.level is ComparisonLevel.TYPE
    assert shape.is_clean()


def test_shape_profile_changed_type_still_fails(tmp_path):
    old = [ev("lib.f", {"x": 1}, result=3, sym="lib.f")]
    new = [ev("lib.f", {"x": 1}, result="3", sym="lib.f")]
    d = run_diff(tmp_path, old, new, profile="shape")
    entry = only(d, FactCategory.CHANGED)
    assert entry.old_types == [{"return": "int"}] and entry.new_types == [{"return": "str"}]


def test_bool_and_int_are_not_equal(tmp_path):
    old = [ev("lib.f", {}, result=True, sym="lib.f")]
    new = [ev("lib.f", {}, result=1, sym="lib.f")]
    assert only(run_diff(tmp_path, old, new), FactCategory.CHANGED).fails


def test_new_raise_is_changed(tmp_path):
    old = [ev("lib.parse", {"s": "x"}, result=1, sym="lib.parse")]
    new = [ev("lib.parse", {"s": "x"}, raised="ValueError", sym="lib.parse")]
    d = run_diff(tmp_path, old, new)
    entry = only(d, FactCategory.CHANGED)
    assert [o.kind for o in entry.old] == ["return"] and [o.kind for o in entry.new] == ["raise"]
    legacy = d.to_boundary_diff()
    assert legacy.raise_changed[0]["old_raised"] is None
    assert legacy.raise_changed[0]["new_raised"] == "ValueError"
    assert legacy.raise_changed[0]["qualname"] == "lib.parse"


def test_call_vanished_at_unchanged_site_is_disappeared(tmp_path):
    site = text_at(10, "a = lib.f(1) + lib.g(2)")
    old = [ev("lib.f", {"x": 1}, result=1, sym="lib.f"),
           ev("lib.g", {"x": 2}, result=2, sym="lib.g")]
    new = [ev("lib.f", {"x": 1}, result=1, sym="lib.f")]
    d = run_diff(tmp_path, old, new, old_text=site, new_text=site)
    gone = only(d, FactCategory.DISAPPEARED)
    assert gone.symbol == "lib.g" and gone.fails and gone.new is None
    assert gone.old_site.label() == "app.py:10" and gone.old_site.text == site[f"{APP}:10"]
    assert d.verdict is VerificationVerdict.FAIL
    assert d.to_boundary_diff().disappeared[0]["where"]["file"] == APP


def test_call_appearing_at_unchanged_site_is_appeared_and_fails(tmp_path):
    site = text_at(10, "a = lib.f(1) + lib.g(2)")
    old = [ev("lib.f", {"x": 1}, result=1, sym="lib.f")]
    new = [ev("lib.f", {"x": 1}, result=1, sym="lib.f"),
           ev("lib.g", {"x": 2}, result=2, sym="lib.g")]
    d = run_diff(tmp_path, old, new, old_text=site, new_text=site)
    assert only(d, FactCategory.APPEARED).fails
    assert not d.is_clean()


def test_site_not_executed_in_an_unedited_function_is_disappeared(tmp_path):
    old = [ev("lib.f", {"x": 1}, result=1, sym="lib.f", line=10),
           ev("lib.g", {"x": 2}, result=2, sym="lib.g", line=11)]
    new = [ev("lib.f", {"x": 1}, result=1, sym="lib.f", line=10)]
    texts = {**text_at(10, "lib.f(1)"), **text_at(11, "lib.g(2)")}
    d = run_diff(tmp_path, old, new, old_text=texts, new_text=text_at(10, "lib.f(1)"))
    assert only(d, FactCategory.DISAPPEARED).symbol == "lib.g"


def test_rewritten_call_without_partner_is_inconclusive(tmp_path):
    old = [ev("lib.a", {"x": 1}, result=1, sym="lib.a", line=10),
           ev("lib.b", {"x": 2}, result=2, sym="lib.b", line=11)]
    new = [ev("lib.c", {"x": 1}, result=1, sym="lib.c", line=10)]
    d = run_diff(tmp_path, old, new,
                 old_text={**text_at(10, "x = lib.a(1)"), **text_at(11, "y = lib.b(2)")},
                 new_text=text_at(10, "x = lib.c(1)"))
    rewritten = d.by_category(FactCategory.REWRITTEN)
    assert [(e.symbol, e.comparison) for e in rewritten] == [
        ("lib.a", Comparison.EQUAL), ("lib.b", Comparison.MISSING)]
    assert d.is_clean() and d.verdict is VerificationVerdict.INCONCLUSIVE
    assert d.to_boundary_diff().skipped_opaque[0]["reason"] == "rewritten-unpaired"


def test_same_call_on_an_edited_line_is_rewritten_by_identity(tmp_path):
    old = [ev("lib.f", {"x": 1}, result=1, sym="lib.f", line=10)]
    new = [ev("lib.f", {"x": 1}, result=1, sym="lib.f", line=12)]
    d = run_diff(tmp_path, old, new, old_text=text_at(10, "y = lib.f(1)"),
                 new_text=text_at(12, "result = lib.f(1)"))
    entry = only(d, FactCategory.REWRITTEN)
    assert entry.comparison is Comparison.EQUAL and "same-call" in entry.notes


def test_identity_change_at_the_same_site_compares_outcomes(tmp_path):
    site = text_at(10, "lib.f(1)")
    old = [ev("lib._impl.f", {"x": 1}, result=1, sym="lib._impl.f")]
    new = [ev("lib._core.f", {"x": 1}, result=1, sym="lib._core.f")]
    d = run_diff(tmp_path, old, new, old_text=site, new_text=site)
    entry = only(d, FactCategory.EQUAL)
    assert entry.new_symbol == "lib._core.f" and "identity-changed" in entry.notes
    assert d.to_json_dict()["entries"][0]["new_symbol"] == "lib._core.f"  # noted -> listed


# ── type level ────────────────────────────────────────────────────────────────────────────


def test_opaque_results_compare_at_type_level(tmp_path):
    resp = {"__opaque__": "requests.models.Response"}
    old = [ev("requests.get", {"url": "u"}, result=resp, sym="requests.api.get")]
    same = run_diff(tmp_path, old, [ev("requests.get", {"url": "u"}, result=resp,
                                       sym="requests.api.get")])
    assert only(same, FactCategory.EQUAL).level is ComparisonLevel.TYPE
    other = run_diff(tmp_path, old, [ev("requests.get", {"url": "u"},
                                        result={"__opaque__": "httpx.Response"},
                                        sym="requests.api.get")])
    changed = only(other, FactCategory.CHANGED)
    assert changed.level is ComparisonLevel.TYPE
    assert changed.new_types == [{"return": {"opaque": "httpx.Response"}}]


def test_opaque_meets_a_richer_rendering_of_the_same_type(tmp_path):
    frame = {"__structure__": "pandas.core.frame.DataFrame", "shape": [2, 2],
             "dtypes": ["int64", "int64"], "columns": ["a", "b"]}
    old = [ev("lib.load", {}, result={"__opaque__": "pandas.core.frame.DataFrame"},
              sym="lib.load")]
    new = [ev("lib.load", {}, result=frame, sym="lib.load")]
    assert only(run_diff(tmp_path, old, new), FactCategory.EQUAL).level is ComparisonLevel.TYPE


def test_nested_opaque_compares_rest_exactly(tmp_path):
    old = [ev("lib.f", {}, result={"__list__": [1, {"__opaque__": "datetime.datetime"}]},
              sym="lib.f")]
    eq = [ev("lib.f", {}, result={"__list__": [1, {"__datetime__": "2024-01-01T00:00:00"}]},
             sym="lib.f")]
    ne = [ev("lib.f", {}, result={"__list__": [2, {"__datetime__": "2024-01-01T00:00:00"}]},
             sym="lib.f")]
    assert only(run_diff(tmp_path, old, eq), FactCategory.EQUAL).level is ComparisonLevel.TYPE
    assert only(run_diff(tmp_path, old, ne), FactCategory.CHANGED).fails


def test_ambiguous_outcome_matches_return_none_or_raise_only():
    amb = Outcome(kind="return", value={"__opaque__": "ambiguous:return-or-raise"})
    assert compare_outcomes([amb], [Outcome(kind="return", value=None)]).comparison \
        is Comparison.EQUAL
    assert compare_outcomes([amb], [Outcome(kind="raise", value="KeyError")]).comparison \
        is Comparison.EQUAL
    other = compare_outcomes([amb], [Outcome(kind="return", value=5)])
    assert other.comparison is Comparison.CHANGED and other.level is ComparisonLevel.TYPE


def test_nondeterministic_sets(tmp_path):
    def calls(*results):
        return [ev("lib.now", {}, result=r, sym="lib.now") for r in results]

    same = run_diff(tmp_path, calls(1, 2, 1), calls(2, 1))
    entry = only(same, FactCategory.EQUAL)
    assert entry.level is ComparisonLevel.EXACT and "nondeterministic" in entry.notes

    other_values = run_diff(tmp_path, calls(1, 2), calls(5))
    assert only(other_values, FactCategory.EQUAL).level is ComparisonLevel.TYPE

    other_type = run_diff(tmp_path, calls(1, 2), calls("a"))
    assert only(other_type, FactCategory.CHANGED).level is ComparisonLevel.TYPE


def test_aggregated_nondeterministic_marker_is_uncomparable(tmp_path):
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"target": "lib", "records": [
        {"qualname": "lib.now", "inputs": {"bound": {}}, "count": 3,
         "result": {"__nondeterministic__": True}, "raised": None,
         "where": {"file": APP, "line": 10, "func": "main"}}]}))
    new = write_trace(tmp_path, "new.jsonl", [ev("lib.now", {}, result=1, v=1)])
    d = diff_traces(old, new, root=ROOT)
    entry = only(d, FactCategory.UNCOMPARABLE)
    assert entry.comparison is Comparison.UNCOMPARABLE and not entry.fails
    assert d.is_clean() and d.verdict is VerificationVerdict.INCONCLUSIVE
    assert d.to_boundary_diff().counts()["skipped_opaque"] == 1
    assert any(w.startswith("aggregated:old") for w in d.warnings)


# ── back-compat and loading ───────────────────────────────────────────────────────────────


def test_v1_traces_key_on_q_and_line_and_tolerate_shifted_lines(tmp_path):
    old = [ev("lib.f", {"x": 1}, result=1, line=10, v=1),
           ev("lib.g", {"x": 1}, result=1, line=20, v=1)]
    new = [ev("lib.f", {"x": 1}, result=1, line=12, v=1),   # code above shifted by 2
           ev("lib.g", {"x": 1}, result=2, line=22, v=1)]
    o = write_trace(tmp_path, "old.jsonl", old)
    fs = load_facts(o, root=ROOT)
    assert fs.schema_versions == [1] and fs.sym_events == 0
    assert [(f.symbol, f.file, f.line, f.text, f.defaulted) for f in fs.facts] == [
        ("lib.f", "app.py", 10, None, None), ("lib.g", "app.py", 20, None, None)]
    d = diff_traces(o, write_trace(tmp_path, "new.jsonl", new), root=ROOT)
    assert d.counts()["equal"] == 1 and d.counts()["changed"] == 1
    assert "line-moved" in only(d, FactCategory.EQUAL).notes
    changed = only(d, FactCategory.CHANGED)
    assert changed.symbol == "lib.g" and changed.old_site.line == 20
    assert changed.new_site.line == 22


def test_v1_against_v2_warns_schema_mismatch(tmp_path):
    d = run_diff(tmp_path, [ev("lib.f", {}, result=1, v=1)],
                 [ev("lib.f", {}, result=1, sym="lib.f")])
    assert d.is_clean()
    assert any(w.startswith("schema-mismatch") for w in d.warnings)


def test_aggregated_json_folds_like_jsonl(tmp_path):
    events = [ev("lib.f", {"x": 1}, result=1, sym="lib.f"),
              ev("lib.f", {"x": 1}, result=1, sym="lib.f"),
              ev("lib.g", {}, raised="KeyError", sym="lib.g", line=11)]
    jsonl = load_facts(write_trace(tmp_path, "t.jsonl", events), root=ROOT)
    agg_path = tmp_path / "t.json"
    agg_path.write_text(json.dumps({"target": "lib", "records": [
        {"qualname": "lib.f", "sym": "lib.f", "inputs": {"bound": {"x": 1}}, "result": 1,
         "raised": None, "count": 2, "where": {"file": APP, "line": 10, "func": "main"}},
        {"qualname": "lib.g", "sym": "lib.g", "inputs": {"bound": {}}, "result": None,
         "raised": "KeyError", "count": 1, "where": {"file": APP, "line": 11, "func": "main"}},
    ]}, indent=2))
    agg = load_facts(agg_path, root=ROOT)
    assert agg.aggregated and not jsonl.aggregated

    def shape(fs):
        return {k: [(o.kind, o.value, o.count) for o in f.outcomes]
                for k, f in fs.keyed().items()}

    assert shape(agg) == shape(jsonl)
    assert diff_facts(agg, jsonl).verdict is VerificationVerdict.PASS


def test_invalid_jsonl_lines_are_counted_not_fatal(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps(ev("lib.f", {}, result=1)) + "\n{\"t\": \"return\", \"q\n")
    fs = load_facts(p, root=ROOT)
    assert len(fs.facts) == 1 and fs.invalid_lines == 1


def test_missing_site_text_on_one_side_falls_back_to_lines(tmp_path):
    old = [ev("lib.f", {"x": 1}, result=1, sym="lib.f")]
    new = [ev("lib.f", {"x": 1}, result=1, sym="lib.f")]
    d = run_diff(tmp_path, old, new, old_text=text_at(10, "lib.f(1)"))
    assert d.counts()["equal"] == 1
    assert any(w.startswith("site-text-missing:new") for w in d.warnings)


def test_shape_profile_matches_inputs_by_type(tmp_path):
    old = [ev("lib.open", {"path": "/venv311/x"}, result="a", sym="lib.open")]
    new = [ev("lib.open", {"path": "/venv312/x"}, result="b", sym="lib.open")]
    exact = run_diff(tmp_path, old, new)
    assert exact.counts()["disappeared"] == 1 and exact.counts()["appeared"] == 1
    shape = run_diff(tmp_path, old, new, profile="shape")
    assert only(shape, FactCategory.EQUAL).input == {"bound": {"path": "str"}}


def test_no_evidence_is_inconclusive(tmp_path):
    d = run_diff(tmp_path, [], [])
    assert d.is_clean() and d.verdict is VerificationVerdict.INCONCLUSIVE


def test_json_form_is_stable_and_self_describing(tmp_path):
    site = text_at(10, "a = lib.f(1) + lib.g(2)")
    old = [ev("lib.g", {"x": 2}, result=2, sym="lib.g"),
           ev("lib.f", {"x": 1}, result=1, sym="lib.f")]
    new = [ev("lib.f", {"x": 1}, result=1.0, sym="lib.f")]
    first = run_diff(tmp_path, old, new, old_text=site, new_text=site).to_json_dict()
    second = run_diff(tmp_path, list(reversed(old)), new, old_text=site,
                      new_text=site).to_json_dict()
    for doc in (first, second):  # first_seen differs; only the summaries mention sources
        doc.pop("old"), doc.pop("new")
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first["schema"] == "pymolt.fact-diff/1" and first["verdict"] == "fail"
    assert [e["category"] for e in first["entries"]] == ["changed", "disappeared"]
    changed = first["entries"][0]
    assert changed["symbol"] == "lib.f" and changed["fails"] is True
    assert changed["old_site"] == {"file": "app.py", "func": "main", "line": 10,
                                   "text": "a = lib.f(1) + lib.g(2)"}
    assert changed["old"] == [{"kind": "return", "value": 1, "count": 1}]
    assert changed["new"] == [{"kind": "return", "value": 1.0, "count": 1}]
    full = run_diff(tmp_path, old, new, old_text=site, new_text=site)
    assert len(full.to_json_dict(include_equal=True)["entries"]) == 2


# ── site text ─────────────────────────────────────────────────────────────────────────────


def test_collect_site_text_reads_lines_under_root_only(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    app = proj / "app.py"
    app.write_text("import lib\n\n    x = lib.f(1)   \n" + "y = '" + "z" * 300 + "'\n")
    legacy = proj / "legacy.py"
    legacy.write_bytes(b"# -*- coding: latin-1 -*-\nname = lib.g('caf\xe9')\n")
    outside = tmp_path / "elsewhere.py"
    outside.write_text("lib.h()\n")
    events = [
        ev("lib.f", {}, result=1, file=str(app), line=3),
        ev("lib.f", {}, result=1, file=str(app), line=4),
        ev("lib.f", {}, result=1, file=str(app), line=99),         # past EOF
        ev("lib.g", {}, result=1, file=str(legacy), line=2),
        ev("lib.h", {}, result=1, file=str(outside), line=1),      # not under root
        ev("lib.i", {}, result=1, file=str(proj / "gone.py"), line=1),  # missing file
    ]
    trace = write_trace(tmp_path, "t.jsonl", events)
    text = collect_site_text(trace, proj)
    assert text[f"{app}:3"] == "x = lib.f(1)"
    assert len(text[f"{app}:4"]) == 240 and text[f"{app}:4"].startswith("y = 'zzz")
    assert text[f"{legacy}:2"] == "name = lib.g('café')"
    assert set(text) == {f"{app}:3", f"{app}:4", f"{legacy}:2"}

    (tmp_path / "t.jsonl.meta.json").write_text(json.dumps({"backend": "profile"}))
    assert store_site_text(trace, text) is not None
    meta = json.loads((tmp_path / "t.jsonl.meta.json").read_text())
    assert meta["backend"] == "profile" and load_site_text(trace) == text

    fs = load_facts(trace, root=proj)
    by_line = {(f.file, f.line): f.text for f in fs.facts}
    assert by_line[("app.py", 3)] == "x = lib.f(1)"
    assert by_line[("legacy.py", 2)] == "name = lib.g('café')"
    assert by_line[("app.py", 99)] is None


def test_site_text_is_robust_to_unreadable_traces(tmp_path):
    assert collect_site_text(tmp_path / "missing.jsonl", tmp_path) == {}
    assert load_site_text(tmp_path / "missing.jsonl") == {}


# ── type reduction ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value, expected", [
    (None, "NoneType"), (True, "bool"), (3, "int"), (3.0, "float"), ("s", "str"),
    ({"__shape__": "int"}, "int"),
    ({"__bytes__": "00"}, "bytes"),
    ({"__opaque__": "float-nonfinite"}, "float"),
    ({"__opaque__": "socket.socket"}, {"opaque": "socket.socket"}),
    ({"__datetime__": "2024-01-01T10:00:00"}, "datetime.datetime"),
    ({"__datetime__": "2024-01-01"}, "datetime.date"),
    ({"__datetime__": "10:00:00"}, "datetime.time"),
    ({"__decimal__": "1.5"}, "decimal.Decimal"),
    ({"__enum__": "mod.Color.RED"}, {"enum": "mod.Color"}),
    ({"__list__": [1, 2, "a"]}, {"list": ["int", "str"]}),
    ({"__tuple__": [1, "a"]}, {"tuple": ["int", "str"]}),
    ({"__dict__": [["a", 1], ["b", None]]}, {"dict": {"a": "int", "b": "NoneType"}}),
    (S12, {"structure": "pandas.core.series.Series", "shape": [2], "dtype": "int64"}),
    ({"__dataclass__": "m.P", "fields": [["x", 1]]},
     {"dataclass": "m.P", "fields": {"x": "int"}}),
])
def test_value_type(value, expected):
    assert value_type(value) == expected


def test_outcome_type():
    assert outcome_type(Outcome(kind="raise", value="KeyError")) == {"raise": "KeyError"}
    assert outcome_type(Outcome(kind="return", value={"__list__": [1]})) == {
        "return": {"list": ["int"]}}


def test_fold_accepts_in_memory_records():
    fs = fold_facts([ev("lib.f", {"x": 1}, result=1, sym="lib.f", defaulted=["y"]),
                     ev("lib.f", {"x": 1}, result=2, sym="lib.f", defaulted=["y"]),
                     {"t": "call", "q": "ignored"}], root=ROOT)
    (fact,) = fs.facts
    assert fact.input == {"bound": {"x": 1}} and fact.defaulted == [["y"]]
    assert fact.count == 2 and fact.nondeterministic and fs.events == 2
