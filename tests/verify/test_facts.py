"""Fact model v2 (host): loading, site text, fold and the categorised fact diff."""
import json
from pathlib import Path

import pytest

from pymolt.verify._sinks import FactSink, JsonlSink, write_snapshot_file
from pymolt.verify.facts import (
    Comparison,
    ComparisonLevel,
    DiffContext,
    FactCategory,
    Outcome,
    collect_site_text,
    compare_outcomes,
    diff_facts,
    diff_traces,
    fold_facts,
    iter_snapshot,
    iter_trace_records,
    load_facts,
    load_site_text,
    merge_fact_snapshots,
    outcome_type,
    project_snapshot,
    record_site_text,
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


def test_constructor_receiver_compares_whenever_a_backend_rendered_it(tmp_path):
    # setprofile renders __init__'s self before the body runs (opaque), the wrap backend
    # after the call (the finished Series): the same pd.Series(a) call is one fact.
    init = "pandas.core.series.Series.__init__"
    site = text_at(10, "s1 = pd.Series(a)")
    old = [ev(init, {"self": {"__opaque__": "pandas.core.series.Series"},
                     "data": {"__list__": [1]}}, sym=init, defaulted=["copy"])]
    new = [ev(init, {"self": S1, "data": {"__list__": [1]}}, sym=init, defaulted=["copy"])]
    d = run_diff(tmp_path, old, new, old_text=site, new_text=site)
    assert d.counts()["equal"] == 1 and d.is_clean(), d.counts()


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
    assert "anchored" in entry.notes
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
    exact = run_diff(tmp_path, old, new)  # same symbol and site, input differs: paired
    assert only(exact, FactCategory.CHANGED).notes[0] == "matched-modulo-input"
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
    # /2 (verify noise, reporting): the ``summary`` of findings comes first; entries follow
    assert first["schema"] == "pymolt.fact-diff/2" and first["verdict"] == "fail"
    assert next(iter(k for k in first if k != "schema")) == "summary"
    assert first["summary"]["failing"][0]["symbol"] in ("lib.f", "lib.g")
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


# ── facts snapshots (phase 2: FactSink) ───────────────────────────────────────────────────


def play(sink, calls):
    """Drive a sink the way a backend does: ``(sym, bound, outcome, line[, defaulted])``
    where ``outcome`` is a result or ``("raise", Type)``."""
    for call in calls:
        sym, bound, outcome, line = call[:4]
        inputs = {"bound": dict(bound)}
        if len(call) > 4:
            inputs["defaulted"] = list(call[4])
        site = {"file": APP, "line": line, "func": "main"}
        if isinstance(outcome, tuple) and outcome[:1] == ("raise",):
            sink.on_raise(sym, inputs, outcome[1], site=site, sym=sym, dur=0.001)
        else:
            sink.on_return(sym, inputs, outcome, site=site, sym=sym, dur=0.001)
    sink.close()
    return sink


def snapshot_of(tmp_path, name, calls, **kw):
    kw.setdefault("flush_seconds", 0)
    return Path(play(FactSink(str(tmp_path / f"{name}.jsonl"), **kw), calls).path)


def jsonl_of(tmp_path, name, calls):
    return Path(play(JsonlSink(str(tmp_path / f"{name}.jsonl")), calls).path)


def categories(d):
    return sorted((e.category.value, e.symbol, e.site.line) for e in d.entries)


OLD_CALLS = [
    ("m.a", {"x": 1}, 1, 10), ("m.a", {"x": 1}, 1, 10),
    ("m.b", {"x": 1}, 1, 11),
    ("m.c", {}, None, 12),
    ("m.d", {}, "gone", 13),
    ("m.f", {}, 1, 14), ("m.f", {}, 2, 14), ("m.f", {}, 3, 14),
    ("m.g", {}, "ok", 15), ("m.g", {}, "ok", 15),
    ("m.h", {}, 0, 16, ["a"]),
]
NEW_CALLS = [
    ("m.a", {"x": 1}, 1, 10),
    ("m.b", {"x": 1}, 1.0, 11),
    ("m.c", {}, ("raise", "TypeError"), 12),
    ("m.e", {}, "new", 17),
    ("m.f", {}, 3, 14), ("m.f", {}, 1, 14), ("m.f", {}, 2, 14),
    ("m.g", {}, "ok", 15), ("m.g", {}, "ok", 15), ("m.g", {}, "ok", 15),
    ("m.h", {}, 0, 16, ["a", "b"]),
]


def test_diff_of_snapshots_equals_diff_of_the_equivalent_jsonl(tmp_path):
    from_jsonl = diff_traces(jsonl_of(tmp_path, "jo", OLD_CALLS),
                             jsonl_of(tmp_path, "jn", NEW_CALLS), root=ROOT)
    from_facts = diff_traces(snapshot_of(tmp_path, "fo", OLD_CALLS),
                             snapshot_of(tmp_path, "fn", NEW_CALLS), root=ROOT)
    assert categories(from_facts) == categories(from_jsonl)
    assert {c for c, *_ in categories(from_facts)} == {
        "equal", "changed", "disappeared", "appeared", "drift"}
    assert from_facts.verdict is from_jsonl.verdict is VerificationVerdict.FAIL
    equal = {e.symbol: e for e in from_facts.by_category("equal")}
    assert (equal["m.a"].old_count, equal["m.a"].new_count) == (2, 1)  # counts survive


def test_load_facts_reads_a_snapshot_directly_or_behind_an_empty_trace(tmp_path):
    snap = snapshot_of(tmp_path, "t", OLD_CALLS)
    direct = load_facts(snap, root=ROOT)
    trace = tmp_path / "t.jsonl"
    trace.write_text("")
    behind = load_facts(trace, root=ROOT)
    reference = load_facts(jsonl_of(tmp_path, "ref", OLD_CALLS), root=ROOT)
    for fs in (direct, behind):
        assert fs.snapshot and fs.calls == len(OLD_CALLS) and fs.evicted == 0
        assert [(f.symbol, f.line, f.count, [(o.kind, o.value, o.count) for o in f.outcomes])
                for f in fs.facts] == [
            (f.symbol, f.line, f.count, [(o.kind, o.value, o.count) for o in f.outcomes])
            for f in reference.facts]
    assert list(iter_trace_records(trace))  # snapshot-aware for every JSONL reader


def test_projection_into_the_jsonl_loads_like_the_snapshot(tmp_path):
    snap = snapshot_of(tmp_path, "p", OLD_CALLS)
    trace = tmp_path / "p.jsonl"
    written = project_snapshot(snap, trace)
    assert written == sum(len(f.outcomes) for f in load_facts(snap, root=ROOT).facts)
    lines = [json.loads(line) for line in trace.read_text().splitlines()]
    assert all(rec["src"] == "facts" and rec["count"] >= 1 for rec in lines)
    a = load_facts(trace, root=ROOT)
    b = load_facts(snap, root=ROOT)
    assert a.snapshot and b.snapshot
    assert [f.model_dump() for f in a.facts] == [f.model_dump() for f in b.facts]


def test_host_merge_of_two_process_snapshots_sums_counts(tmp_path):
    p1 = snapshot_of(tmp_path, "trace-1", [
        ("m.a", {"x": 1}, 1, 10), ("m.a", {"x": 1}, 1, 10),
        ("m.b", {}, ("raise", "KeyError"), 11),
    ])
    p2 = snapshot_of(tmp_path, "trace-2", [
        ("m.a", {"x": 1}, 1, 10), ("m.a", {"x": 1}, 2, 10), ("m.c", {}, None, 12),
    ])
    out = tmp_path / "merged.jsonl.facts.json"
    header = merge_fact_snapshots([p1, p2], out)
    assert header["merged"] is True and len(header["processes"]) == 2
    assert header["counters"]["calls"] == 6 and header["counters"]["facts"] == 3
    fs = load_facts(out, root=ROOT)
    a = next(f for f in fs.facts if f.symbol == "m.a")
    assert a.count == 4
    assert sorted((o.value, o.count) for o in a.outcomes) == [(1, 3), (2, 1)]
    tallies = [t for k, t in iter_snapshot(out) if k == "tally"]
    assert sum(t["count"] for t in tallies) == 6
    a_tally = next(t for t in tallies if t["sym"] == "m.a")
    assert a_tally["dur"]["n"] == 4 and sum(a_tally["dur"]["hist"]) == 4


def test_host_merge_reapplies_the_outcome_cap_and_the_fact_cap(tmp_path):
    p1 = snapshot_of(tmp_path, "trace-1", [("m.n", {}, i, 10) for i in range(8)])
    p2 = snapshot_of(tmp_path, "trace-2", [("m.n", {}, i, 10) for i in range(8, 10)]
                     + [("m.solo", {}, 1, 20)])
    out = tmp_path / "m.facts.json"
    merge_fact_snapshots([p1, p2], out)
    fs = load_facts(out, root=ROOT)
    n = next(f for f in fs.facts if f.symbol == "m.n")
    assert n.count == 10
    assert {"__opaque__": "int"} in [o.value for o in n.outcomes]  # the overflow, by type
    capped = merge_fact_snapshots([p1, p2], tmp_path / "c.facts.json", max_facts=1)
    assert capped["counters"]["facts"] == 1 and capped["counters"]["evicted"] == 1


def test_an_evicting_snapshot_never_passes(tmp_path):
    calls = [("m.keep", {}, 1, 10)] * 5 + [("m.x", {}, 1, line) for line in range(20, 40)]
    old = load_facts(snapshot_of(tmp_path, "o", calls, max_facts=8), root=ROOT)
    new = load_facts(snapshot_of(tmp_path, "n", calls, max_facts=8), root=ROOT)
    assert old.evicted > 0
    d = diff_facts(old, new)
    assert d.is_clean() and d.incomplete
    assert d.verdict is VerificationVerdict.INCONCLUSIVE
    assert any(w.startswith("evicted:old") for w in d.warnings)
    assert d.to_json_dict()["incomplete"] is True


def test_a_fact_the_evicting_side_dropped_is_unsampled_under_complete_tallies(tmp_path):
    calls = [("m.keep", {}, 1, 10)] * 5 + [("m.x", {}, 1, line) for line in range(20, 40)]
    old = load_facts(snapshot_of(tmp_path, "o", calls), root=ROOT)
    new = load_facts(snapshot_of(tmp_path, "n", calls, max_facts=8), root=ROOT)
    assert new.tallies_complete
    d = diff_facts(old, new)
    assert not d.by_category("disappeared") and d.unsampled > 0
    assert d.verdict is VerificationVerdict.INCONCLUSIVE  # eviction still blocks PASS


def test_a_fact_the_evicting_side_dropped_is_uncomparable_not_disappeared(tmp_path):
    calls = [("m.keep", {}, 1, 10)] * 5 + [("m.x", {}, 1, line) for line in range(20, 40)]
    old = load_facts(snapshot_of(tmp_path, "o", calls), root=ROOT)
    # Overflowing tallies: they cannot prove absence, only eviction explains the gap.
    new = load_facts(snapshot_of(tmp_path, "n", calls, max_facts=8, max_tallies=1), root=ROOT)
    assert not new.tallies_complete
    d = diff_facts(old, new)
    assert not d.by_category("disappeared")
    dropped = d.by_category("uncomparable")
    assert dropped and all("possibly-evicted:new" in e.notes for e in dropped)
    assert d.verdict is VerificationVerdict.INCONCLUSIVE
    assert d.to_boundary_diff().skipped_opaque[0]["reason"] == "evicted"


def test_a_call_the_tallies_never_saw_still_disappears_under_eviction(tmp_path):
    common = [("m.keep", {}, 1, 10)] * 5 + [("m.x", {}, 1, line) for line in range(20, 40)]
    old = load_facts(snapshot_of(tmp_path, "o", common + [("m.gone", {}, 1, 50)]), root=ROOT)
    new = load_facts(snapshot_of(tmp_path, "n", common, max_facts=8), root=ROOT)
    d = diff_facts(old, new)
    gone = only(d, "disappeared")
    assert gone.symbol == "m.gone"
    assert d.verdict is VerificationVerdict.FAIL


def test_widened_sites_compare_at_type_level_whatever_the_call_order(tmp_path):
    old = snapshot_of(tmp_path, "o", [("m.get", {"id": i}, i, 10) for i in range(10)],
                      facts_per_site=4)
    new = snapshot_of(tmp_path, "n", [("m.get", {"id": i}, i, 10) for i in reversed(range(10))],
                      facts_per_site=4)
    d = diff_traces(old, new, root=ROOT)
    entry = only(d, "equal")
    assert entry.input == {"__widened__": True}
    assert entry.level is ComparisonLevel.TYPE and "widened" in entry.notes
    assert d.verdict is VerificationVerdict.PASS
    assert any(w.startswith("widened:old") for w in d.warnings)


def test_a_type_change_at_a_widened_site_fails(tmp_path):
    old = snapshot_of(tmp_path, "o", [("m.get", {"id": i}, i, 10) for i in range(10)],
                      facts_per_site=4)
    new = snapshot_of(tmp_path, "n", [("m.get", {"id": i}, str(i), 10) for i in range(10)],
                      facts_per_site=4)
    d = diff_traces(old, new, root=ROOT)
    assert only(d, "changed").input == {"__widened__": True}


def test_a_widened_baseline_meets_an_exact_jsonl_at_type_level(tmp_path):
    calls = [("m.get", {"id": i}, i, 10) for i in range(10)]
    d = diff_traces(snapshot_of(tmp_path, "o", calls, facts_per_site=4),
                    jsonl_of(tmp_path, "n", calls), root=ROOT)
    assert only(d, "equal").level is ComparisonLevel.TYPE


def test_outcomes_whose_type_was_lost_are_uncomparable(tmp_path):
    path = tmp_path / "lost.facts.json"
    fact = {"q": "m.f", "sym": "m.f", "where": {"file": APP, "line": 10, "func": "main"},
            "in": {"bound": {}}, "count": 12, "first_ts": 1.0,
            "outcomes": [{"t": "return", "result": 1, "count": 2},
                         {"t": "other", "count": 10, "types": [
                             {"t": "return", "type": "int", "count": 4}], "lost": 6}]}
    write_snapshot_file(str(path), {"schema": "pymolt.facts-snapshot/1", "counters": {}},
                        [json.dumps(fact)], [])
    fs = load_facts(path, root=ROOT)
    (f,) = fs.facts
    assert sorted(o.count for o in f.outcomes) == [2, 4, 6]
    d = diff_facts(fs, fs)
    assert only(d, "uncomparable").comparison is Comparison.UNCOMPARABLE


# ── backend families: untyped raises (setprofile < 3.12) and C-level raise facts ──────────


def test_untyped_raise_matches_a_typed_raise_at_type_level(tmp_path):
    d = run_diff(tmp_path, [ev("m.f", raised="<unknown>")],
                 [ev("m.f", raised="builtins.TypeError")])
    e = only(d, "equal")
    assert e.level is ComparisonLevel.TYPE
    assert "exception type unknown on one side" in e.notes
    assert d.verdict is VerificationVerdict.PASS


def test_untyped_raise_still_differs_from_a_return(tmp_path):
    d = run_diff(tmp_path, [ev("m.f", raised="<unknown>")], [ev("m.f", result=None)])
    assert only(d, "changed").fails


def test_known_raise_types_still_differ_and_builtins_spellings_agree(tmp_path):
    d = run_diff(tmp_path, [ev("m.f", raised="KeyError")], [ev("m.f", raised="TypeError")])
    assert only(d, "changed")
    d = run_diff(tmp_path, [ev("m.f", raised="TypeError")],
                 [ev("m.f", raised="builtins.TypeError")])
    assert only(d, "equal").level is ComparisonLevel.EXACT


def test_untyped_raise_in_a_distribution_matches_at_type_level(tmp_path):
    old = [ev("m.f", result=1), ev("m.f", raised="<unknown>")]
    new = [ev("m.f", result=1), ev("m.f", raised="builtins.ValueError")]
    e = only(run_diff(tmp_path, old, new), "equal")
    assert e.level is ComparisonLevel.TYPE
    assert "exception type unknown on one side" in e.notes


def _with_backend(tmp_path, name, events, backend, *, processes=False):
    p = write_trace(tmp_path, name, events)
    meta = ({"processes": [{"backend": backend}]} if processes else {"backend": backend})
    (tmp_path / f"{name}.meta.json").write_text(json.dumps(meta))
    return p


C_RAISE = {"t": "raise", "q": "dep._speedups.parse", "sym": "dep._speedups.parse", "v": 2,
           "in": {"c_level": True}, "raised": "Exception",
           "where": {"file": APP, "line": 20, "func": "main"}}


def test_c_level_facts_are_excluded_across_backend_families(tmp_path):
    common = [ev("m.f", result=1, sym="m.f")]
    old = _with_backend(tmp_path, "old.jsonl", common + [C_RAISE], "setprofile")
    new = _with_backend(tmp_path, "new.jsonl", common, "monitoring", processes=True)
    d = diff_traces(old, new, root=ROOT)
    assert d.counts()["disappeared"] == 0
    assert d.verdict is VerificationVerdict.PASS
    assert any(w.startswith("c-level-excluded:1") for w in d.warnings)


def test_c_level_facts_still_compare_within_one_backend_family(tmp_path):
    common = [ev("m.f", result=1, sym="m.f")]
    old = _with_backend(tmp_path, "old.jsonl", common + [C_RAISE], "setprofile")
    new = _with_backend(tmp_path, "new.jsonl", common, "setprofile")
    d = diff_traces(old, new, root=ROOT)
    assert only(d, "disappeared").symbol == "dep._speedups.parse"
    assert not any(w.startswith("c-level-excluded") for w in d.warnings)


def test_snapshot_header_names_the_backend_when_the_trace_has_no_sidecar(tmp_path):
    sink = FactSink(str(tmp_path / "s.jsonl"), flush_seconds=0)
    sink.set_metadata(backend="monitoring")
    sink.on_return("m.f", {"bound": {}}, 1, site={"file": APP, "line": 10, "func": "main"},
                   sym="m.f")
    sink.close()
    (tmp_path / "s.jsonl.meta.json").unlink()
    assert load_facts(sink.path, root=ROOT).backends == ["monitoring"]


# ── tally-aware diff (sampled values, every call counted) ────────────────────────────────

import random as _random  # noqa: E402


def sampled_snapshot(tmp_path, monkeypatch, name, calls, *, rate="0.1", first="4", seed=0):
    """A FactSink driven with the backends' protocol: ``admit(key)`` first; an admitted call
    records its values, a declined one is tallied."""
    monkeypatch.setenv("PYMOLT_TRACE_SAMPLE_RATE", rate)
    monkeypatch.setenv("PYMOLT_TRACE_SAMPLE_FIRST", first)
    _random.seed(seed)
    sink = FactSink(str(tmp_path / f"{name}.jsonl"), flush_seconds=0)
    for sym, bound, outcome, line in calls:
        site = {"file": APP, "line": line, "func": "main"}
        raising = isinstance(outcome, tuple)
        if sink.admit(key=(sym, APP, line), force=raising):
            if raising:
                sink.on_raise(sym, {"bound": dict(bound)}, outcome[1], site=site,
                              admitted=True, sym=sym)
            else:
                sink.on_return(sym, {"bound": dict(bound)}, outcome, site=site,
                               admitted=True, sym=sym)
        else:
            sink.tally(sym, sym, site, "raise" if raising else "return",
                       raised=outcome[1] if raising else None)
    sink.close()
    monkeypatch.delenv("PYMOLT_TRACE_SAMPLE_RATE")
    monkeypatch.delenv("PYMOLT_TRACE_SAMPLE_FIRST")
    return load_facts(sink.path, root=ROOT)


WORKLOAD = [("m.get", {"id": i % 40}, i % 40, 10) for i in range(600)] + \
    [("m.put", {"v": i % 7}, None, 11) for i in range(300)] + \
    [("m.bad", {}, ("raise", "KeyError"), 12) for _ in range(50)]


def test_same_workload_sampled_with_different_seeds_has_no_failures(tmp_path, monkeypatch):
    old = sampled_snapshot(tmp_path, monkeypatch, "o", WORKLOAD, seed=1)
    new = sampled_snapshot(tmp_path, monkeypatch, "n", WORKLOAD, seed=2)
    assert old.tallies_complete and new.tallies_complete
    assert old.sampled < old.calls  # values really were sampled
    d = diff_facts(old, new)
    assert d.failures() == []
    assert d.unsampled > 0  # inputs one seed sampled and the other did not
    assert d.verdict is VerificationVerdict.PASS
    assert d.to_json_dict()["unsampled"] == d.unsampled
    assert d.old_summary.tallies_complete and d.new_summary.tallies_complete


def test_a_vanished_call_site_is_disappeared_exactly(tmp_path, monkeypatch):
    old = sampled_snapshot(tmp_path, monkeypatch, "o", WORKLOAD)
    new = sampled_snapshot(tmp_path, monkeypatch, "n",
                           [c for c in WORKLOAD if c[0] != "m.put"])
    d = diff_facts(old, new)
    gone = d.by_category("disappeared")
    assert gone and {e.symbol for e in gone} == {"m.put"}
    assert all(e.level is ComparisonLevel.EXACT and "absent-from-tallies" in e.notes
               for e in gone)
    assert d.verdict is VerificationVerdict.FAIL


def test_a_site_only_tallies_saw_still_disappears(tmp_path, monkeypatch):
    calls = WORKLOAD + [("m.rare", {}, 1, 13)] * 3
    old = sampled_snapshot(tmp_path, monkeypatch, "o", calls, first="0", rate="0.0001")
    new = sampled_snapshot(tmp_path, monkeypatch, "n", WORKLOAD, first="0", rate="0.0001")
    assert not any(f.symbol == "m.rare" for f in old.facts)  # never sampled
    e = next(e for e in diff_facts(old, new).by_category("disappeared") if e.symbol == "m.rare")
    assert "from-tallies" in e.notes and e.old_count == 3


def test_outcome_kinds_from_tallies_change_at_kind_level(tmp_path, monkeypatch):
    old_calls = [("m.f", {"x": i}, i, 10) for i in range(50)] + \
        [("m.f", {"x": -1}, ("raise", "ValueError"), 10)]
    new_calls = [("m.f", {"x": -1}, ("raise", "ValueError"), 10)] * 51
    old = sampled_snapshot(tmp_path, monkeypatch, "o", old_calls, first="0", rate="0.0001")
    new = sampled_snapshot(tmp_path, monkeypatch, "n", new_calls, first="0", rate="0.0001")
    e = only(diff_facts(old, new), "changed")
    assert e.level is ComparisonLevel.KIND and "from-tallies" in e.notes
    assert [o.kind for o in e.old] == ["return", "raise"]
    assert [o.kind for o in e.new] == ["raise"]


def test_kind_level_untyped_raise_matches_a_typed_one():
    from pymolt.verify.facts import _raise_sets_match

    assert _raise_sets_match({"<unknown>": 2}, {"TypeError": 2})
    assert not _raise_sets_match({"KeyError": 1}, {"TypeError": 1})
    assert not _raise_sets_match({"KeyError": 1}, {})


def test_an_input_missing_from_a_fully_sampled_site_is_still_disappeared(tmp_path,
                                                                         monkeypatch):
    old = sampled_snapshot(tmp_path, monkeypatch, "o",
                           [("m.f", {"x": 1}, 1, 10), ("m.f", {"x": 2}, 2, 10)], first="32")
    new = sampled_snapshot(tmp_path, monkeypatch, "n",
                           [("m.f", {"x": 1}, 1, 10), ("m.f", {"x": 3}, 3, 10)], first="32")
    d = diff_facts(old, new)
    assert only(d, "disappeared").input == {"bound": {"x": 2}}
    assert "site-fully-sampled" in only(d, "appeared").notes
    assert d.unsampled == 0


def test_without_complete_tallies_the_diff_is_unchanged(tmp_path, monkeypatch):
    old = sampled_snapshot(tmp_path, monkeypatch, "o", WORKLOAD, seed=1)
    new = sampled_snapshot(tmp_path, monkeypatch, "n", WORKLOAD, seed=2)
    new.tallies_complete = False  # e.g. a declined call that was never tallied
    d = diff_facts(old, new)
    assert d.by_category("disappeared")  # unsampled inputs look vanished: no PASS
    assert d.verdict is VerificationVerdict.FAIL


def test_untallied_declines_make_the_tallies_incomplete(tmp_path, monkeypatch):
    monkeypatch.setenv("PYMOLT_TRACE_SAMPLE_RATE", "0.0001")
    monkeypatch.setenv("PYMOLT_TRACE_SAMPLE_FIRST", "0")
    sink = FactSink(str(tmp_path / "x.jsonl"), flush_seconds=0)
    assert sink.admit(key=("m.f", APP, 1)) is False  # declined, never tallied
    sink.on_raise("m.f", {"bound": {}}, "E", site={"file": APP, "line": 1, "func": "main"},
                  sym="m.f")
    sink.close()
    assert not load_facts(sink.path, root=ROOT).tallies_complete


# ── container paths ──────────────────────────────────────────────────────────

def _container_project(tmp_path, *, with_slot_runner=True):
    from pymolt.adapters.python_runner import Mount, RunnerSpec

    root = tmp_path / "proj"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.py").write_text("x = 1\nresult = dep.call(x)\n", encoding="utf-8")
    trace = root / ".pymolt" / "contract_traces" / "baseline.jsonl"
    trace.parent.mkdir(parents=True)
    rec = {"v": 2, "t": "return", "q": "dep.call", "sym": "dep.call", "result": 1,
           "in": {"bound": {}}, "where": {"file": "/work/pkg/a.py", "func": "f", "line": 2}}
    trace.write_text(json.dumps(rec) + "\n", encoding="utf-8")
    spec = RunnerSpec(name="c", container_id="cid", mounts=[Mount(source=str(root),
                                                                  destination="/work")])
    slot = {"trace_path": ".pymolt/contract_traces/baseline.jsonl"}
    if with_slot_runner:
        slot["runner"] = spec.model_dump()
    (root / ".pymolt" / "contract_state.json").write_text(
        json.dumps({"schema_version": 1, "baseline": slot}), encoding="utf-8")
    return root, trace, spec


def test_container_sites_are_mapped_to_project_relative_paths(tmp_path):
    root, trace, spec = _container_project(tmp_path)
    fs = load_facts(trace, root=root)  # the slot's runner is found from the project's state
    assert [f.file for f in fs.facts] == ["pkg/a.py"]
    assert fs.path_map_source == "slot" and fs.paths_mapped == 1 and not fs.notes


def test_explicit_runner_and_none_override_discovery(tmp_path):
    root, trace, spec = _container_project(tmp_path)
    assert load_facts(trace, root=root, runner=spec).facts[0].file == "pkg/a.py"
    fs = load_facts(trace, root=root, runner=None)
    assert fs.facts[0].file == "/work/pkg/a.py"
    assert "container paths not mapped" in fs.notes[0]


def test_old_trace_without_any_runner_is_left_and_noted(tmp_path):
    root, trace, _spec = _container_project(tmp_path, with_slot_runner=False)
    fs = load_facts(trace, root=root)
    assert fs.facts[0].file == "/work/pkg/a.py"
    assert fs.path_map_source is None
    assert "container paths not mapped" in fs.notes[0]


def test_config_runner_maps_an_old_trace(tmp_path):
    root, trace, spec = _container_project(tmp_path, with_slot_runner=False)
    (root / ".pymolt" / "env_config.json").write_text(
        json.dumps({"runner": spec.model_dump()}), encoding="utf-8")
    fs = load_facts(trace, root=root)
    assert fs.facts[0].file == "pkg/a.py" and fs.path_map_source == "config"


def test_site_text_is_collected_from_the_host_file_through_the_runner(tmp_path):
    root, trace, spec = _container_project(tmp_path)
    assert collect_site_text(trace, root) == {}
    assert collect_site_text(trace, root, spec) == {"/work/pkg/a.py:2": "result = dep.call(x)"}
    assert record_site_text(trace, root, spec) == 1
    fs = load_facts(trace, root=root)
    assert fs.facts[0].text == "result = dep.call(x)"


# ── Wave G (V): pairing at a site, binding raises, relocation, object markers ─────────────

SITE = text_at(10, "return Retry(")


def rv(q, inputs, *, result=None, sym=None, line=10, **extra):
    e = {"t": "return", "q": q, "in": inputs, "result": result, "v": 2, "ts": 0.0,
         "sym": sym or q, "where": {"file": APP, "line": line, "func": "main"}}
    e.update(extra)
    return e


def diff_sites(tmp_path, old, new):
    return run_diff(tmp_path, old, new, old_text=SITE, new_text=SITE)


def test_same_site_same_symbol_other_input_pairs_and_compares(tmp_path):
    old = [rv("lib.Retry", {"bound": {"method_whitelist": 1}})]
    new = [rv("lib.Retry", {"bound": {"allowed_methods": 1}})]
    d = diff_sites(tmp_path, old, new)
    e = only(d, FactCategory.EQUAL)
    assert "matched-modulo-input" in e.notes and e.new_input == {"bound": {"allowed_methods": 1}}
    assert d.verdict is VerificationVerdict.PASS


def test_same_site_other_input_other_outcome_is_changed(tmp_path):
    old = [rv("lib.f", {"bound": {"a": 1}}, result=1)]
    new = [rv("lib.f", {"bound": {"b": 1}}, result=2)]
    assert only(diff_sites(tmp_path, old, new), FactCategory.CHANGED)


def test_ambiguous_same_site_group_stays_unpaired(tmp_path):
    old = [rv("lib.f", {"bound": {"a": 1}}), rv("lib.f", {"bound": {"a": 2}})]
    new = [rv("lib.f", {"bound": {"b": 1}})]
    d = diff_sites(tmp_path, old, new)
    assert d.counts()["disappeared"] == 2 and d.counts()["appeared"] == 1


def test_relocated_symbol_at_same_site_pairs(tmp_path):
    old = [rv("lib.engine.create_engine", {"bound": {"u": 1}}, result=5)]
    new = [rv("lib.engine.create.create_engine", {"bound": {"u": 1, "future": True}},
              result=5)]
    d = diff_sites(tmp_path, old, new)
    # paired across the relocation; ``future=True`` is an explicit argument (not reported as
    # defaulted), so the input reaching the call changed: a review item, not a pass
    (e,) = d.entries
    assert e.new_symbol == "lib.engine.create.create_engine"
    assert e.review.startswith("input changed") and not e.fails


def test_unrelated_symbol_at_same_site_is_not_paired(tmp_path):
    old = [rv("lib.a", {"bound": {"u": 1}})]
    new = [rv("lib.b", {"bound": {"x": 1}})]
    d = diff_sites(tmp_path, old, new)
    assert d.counts()["disappeared"] == 1 and d.counts()["appeared"] == 1


def test_binding_raise_pairs_with_the_normal_call_and_changes(tmp_path):
    old = [rv("lib.load", {"bound": {"s": "x"}}, result=1)]
    new = [{**rv("lib.load", {"args": ["x"]}), "t": "raise", "raised": "TypeError",
            "binding": True}]
    new[0].pop("result")
    d = diff_sites(tmp_path, old, new)
    e = only(d, FactCategory.CHANGED)
    assert "now raises TypeError while binding its arguments" in e.notes
    assert d.counts()["disappeared"] == 0 and d.verdict is VerificationVerdict.FAIL


def test_binding_raise_pairs_even_when_the_line_text_moved(tmp_path):
    old = [rv("lib.load", {"bound": {"s": "x"}}, result=1, line=10)]
    new = [{**rv("lib.load", {"args": ["x"]}, line=12), "t": "raise", "raised": "TypeError",
            "binding": True}]
    d = run_diff(tmp_path, old, new, old_text=text_at(10, "a = lib.load(s)"),
                 new_text=text_at(12, "a = lib.load(s, 1)"))
    assert d.counts()["disappeared"] == 0
    assert any("while binding" in n for e in d.entries for n in e.notes)


def test_c_call_inputs_match_by_symbol_and_site(tmp_path):
    old = [rv("lib.c", {"__c_call__": True, "arg0": 1}, result=1)]
    new = [rv("lib.c", {"__c_call__": True}, result=1)]
    d = diff_sites(tmp_path, old, new)
    assert only(d, FactCategory.EQUAL).notes == []


def test_relocated_type_is_equal_at_type_level_with_a_note(tmp_path):
    old = [rv("lib.declarative_base", {"bound": {}},
              result={"__type__": "lib.ext.declarative.api.Base"})]
    new = [rv("lib.declarative_base", {"bound": {}},
              result={"__type__": "lib.orm.decl_api.Base"})]
    e = only(diff_sites(tmp_path, old, new), FactCategory.EQUAL)
    assert e.level is ComparisonLevel.TYPE
    assert any(n.startswith("relocated:") for n in e.notes)


def test_other_package_or_name_is_still_changed():
    for new_name in ("other.orm.Base", "lib.orm.Other"):
        for key in ("__type__", "__opaque__", "__object__"):
            a = [Outcome(kind="return", value={key: "lib.ext.Base"})]
            b = [Outcome(kind="return", value={key: new_name})]
            assert compare_outcomes(a, b).comparison is Comparison.CHANGED


def test_relocated_opaque_and_object_names_are_equal():
    for key in ("__opaque__", "__object__"):
        a = [Outcome(kind="return", value={key: "lib.a.Base"})]
        b = [Outcome(kind="return", value={key: "lib.b.Base"})]
        c = compare_outcomes(a, b)
        assert c.comparison is Comparison.EQUAL and c.level is ComparisonLevel.TYPE
        assert c.notes == ["relocated: lib.a.Base -> lib.b.Base"]


def obj(name, **attrs):
    return {"__object__": name, "attrs": attrs}


def cmp_obj(a, b):
    return compare_outcomes([Outcome(kind="return", value=a)],
                            [Outcome(kind="return", value=b)])


def test_object_attributes_compare_exactly_when_complete():
    same = cmp_obj(obj("lib.R", exit_code=0), obj("lib.R", exit_code=0))
    assert same.comparison is Comparison.EQUAL and same.level is ComparisonLevel.EXACT
    diff = cmp_obj(obj("lib.R", exit_code=0), obj("lib.R", exit_code=2))
    assert diff.comparison is Comparison.CHANGED


def test_truncated_object_compares_by_type():
    a = {**obj("lib.R", exit_code=0), "__truncated__": True}
    c = cmp_obj(a, obj("lib.R", exit_code=2))
    assert c.comparison is Comparison.EQUAL and c.level is ComparisonLevel.TYPE


def test_object_attr_on_one_side_only_is_drift():
    c = cmp_obj(obj("lib.R", exit_code=0), obj("lib.R", exit_code=0, output_bytes=1))
    assert c.comparison is Comparison.EQUAL and c.drift
    assert "attribute added: output_bytes" in c.notes


def test_none_vs_empty_attr_is_drift_and_other_values_change():
    for empty in ("", {"__bytes__": ""}, {"__list__": []}, []):
        c = cmp_obj(obj("lib.R", e=None), obj("lib.R", e=empty))
        assert c.comparison is Comparison.EQUAL and c.drift, empty
    assert cmp_obj(obj("lib.R", e=None), obj("lib.R", e="x")).comparison is Comparison.CHANGED


def test_nested_objects_compare_by_type_only():
    a = obj("lib.R", runner=obj("lib.Runner", env=1), exc=[obj("lib.E", a=1)])
    b = obj("lib.R", runner=obj("lib.Runner", env=2, extra=3), exc=[obj("lib.E", a=9)])
    c = cmp_obj(a, b)
    assert c.comparison is Comparison.EQUAL and c.level is ComparisonLevel.TYPE
    other = obj("lib.R", runner=obj("lib.Other"), exc=[obj("lib.E")])
    assert cmp_obj(a, other).comparison is Comparison.CHANGED


def test_object_drift_makes_a_drift_entry(tmp_path):
    old = [rv("lib.invoke", {"bound": {}}, result=obj("lib.R", exit_code=0, e=None))]
    new = [rv("lib.invoke", {"bound": {}}, result=obj("lib.R", exit_code=0, e={"__bytes__": ""},
                                                      o=1))]
    d = diff_sites(tmp_path, old, new)
    assert only(d, FactCategory.DRIFT) and d.verdict is VerificationVerdict.PASS


def test_objects_in_inputs_are_keyed_as_opaque(tmp_path):
    old = [rv("lib.f", {"bound": {"x": obj("lib.R", a=1)}}, result=1)]
    new = [rv("lib.f", {"bound": {"x": obj("lib.R", a=2)}}, result=1)]
    d = diff_sites(tmp_path, old, new)
    e = only(d, FactCategory.EQUAL)
    assert e.input == {"bound": {"x": {"__opaque__": "lib.R"}}} and e.notes == []


# ── Wave G round 2 (V): handles, sentinels, unobservable edits, getattr, closures ─────────


def gobj(**attrs):
    return {"__object__": "lib.G", "attrs": attrs}


def test_object_that_is_the_receiver_of_an_operation_is_a_handle(tmp_path):
    old = [rv("lib.mk", {"bound": {}}, result=gobj(k=1)),
           rv("lib.G.get", {"bound": {"self": {"__opaque__": "lib.G"}, "key": "v"}}, line=11)]
    new = [rv("lib.mk", {"bound": {}}, result=gobj(k=2)),
           rv("lib.G.get", {"bound": {"self": {"__opaque__": "lib.G"}, "key": "v"}}, line=11)]
    d = run_diff(tmp_path, old, new, old_text={**text_at(10, "g = lib.mk()"),
                                               **text_at(11, "g.get('v')")},
                 new_text={**text_at(10, "g = lib.mk()"), **text_at(11, "g.get('v')")})
    e = [x for x in d.entries if x.symbol == "lib.mk"][0]
    assert e.category is FactCategory.DRIFT and "state of a handle differs" in e.notes[0]
    assert d.verdict is VerificationVerdict.PASS


def test_property_read_does_not_make_a_handle(tmp_path):
    old = [rv("lib.mk", {"bound": {}}, result=gobj(exit_code=0)),
           rv("lib.G.output", {"bound": {"self": {"__opaque__": "lib.G"}}}, line=11)]
    new = [rv("lib.mk", {"bound": {}}, result=gobj(exit_code=2)),
           rv("lib.G.output", {"bound": {"self": {"__opaque__": "lib.G"}}}, line=11)]
    d = run_diff(tmp_path, old, new)
    assert [x.symbol for x in d.by_category(FactCategory.CHANGED)] == ["lib.mk"]


def test_value_object_keeps_exact_attribute_comparison(tmp_path):
    d = run_diff(tmp_path, [rv("lib.mk", {"bound": {}}, result=gobj(exit_code=0))],
                 [rv("lib.mk", {"bound": {}}, result=gobj(exit_code=2))])
    assert only(d, FactCategory.CHANGED)


def test_sentinel_attribute_resolved_is_drift():
    nd = {"__enum__": "lib._NoDefault.no_default"}
    c = cmp_obj(obj("lib.R", group_keys=nd), obj("lib.R", group_keys=True))
    assert c.comparison is Comparison.EQUAL and c.drift
    assert "sentinel resolved: group_keys" in c.notes
    assert cmp_obj(obj("lib.R", k="a"), obj("lib.R", k=True)).comparison is Comparison.CHANGED
    for name in ("lib._MISSING_TYPE", "lib.NOTSET", "lib.Unset"):
        c = cmp_obj(obj("lib.R", k={"__enum__": name + ".v"}), obj("lib.R", k=1))
        assert c.drift and c.comparison is Comparison.EQUAL, name


def test_edited_line_without_a_recorded_replacement_is_unobservable(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    src = root / "app.py"
    src.write_text("x = 1\nreturn r.headers.get('a')\n")
    file = str(src)
    old = [rv("lib.getheader", {"bound": {}}, result="a",
              **{"where": {"file": file, "line": 2, "func": "main"}})]
    o = write_trace(tmp_path, "o.jsonl", old,
                    {f"{file}:2": "return r.getheader('a')"})
    other = rv("lib.other", {"bound": {}}, line=1,
               **{"where": {"file": file, "line": 1, "func": "other"}})
    n = write_trace(tmp_path, "n.jsonl", [other], {f"{file}:1": "x = 1"})
    d = diff_traces(o, n, root=root)
    e = only(d, FactCategory.REWRITTEN)
    assert "the replacement on this line was not observable" in e.notes
    assert e.inconclusive and not e.fails


def test_vanished_call_on_an_unedited_line_is_still_disappeared(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    src = root / "app.py"
    src.write_text("x = 1\nreturn r.getheader('a')\n")
    file = str(src)
    o = write_trace(tmp_path, "o.jsonl", [rv("lib.getheader", {"bound": {}}, result="a",
                    **{"where": {"file": file, "line": 2, "func": "main"}})],
                    {f"{file}:2": "return r.getheader('a')"})
    n = write_trace(tmp_path, "n.jsonl", [rv("lib.other", {"bound": {}},
                    **{"where": {"file": file, "line": 1, "func": "other"}})],
                    {f"{file}:1": "x = 1"})
    assert only(diff_traces(o, n, root=root), FactCategory.DISAPPEARED)


def test_module_getattr_is_an_attribute_access_never_a_failure(tmp_path):
    ga = rv("click.__getattr__", {"bound": {"name": "Mapping"}}, result=1,
            warnings=[{"category": "DeprecationWarning", "message": "m", "count": 1}])
    d = diff_sites(tmp_path, [], [ga])
    e = only(d, FactCategory.DRIFT)
    assert e.symbol == "click.Mapping" and "via click.__getattr__" in e.notes
    assert e.new_warnings and d.verdict is VerificationVerdict.PASS
    d = diff_sites(tmp_path, [ga], [])
    assert only(d, FactCategory.DRIFT).symbol == "click.Mapping"
    # a class's __getattr__ (has a receiver) is an ordinary call
    cls = rv("lib.C.__getattr__", {"bound": {"self": {"__opaque__": "lib.C"}, "name": "x"}})
    assert only(diff_sites(tmp_path, [cls], []), FactCategory.DISAPPEARED)


def test_closure_with_a_different_enclosing_function_is_equal_at_type_level():
    a = [Outcome(kind="return", value={"__callable__": "lib.version_option.<locals>.decorator"})]
    b = [Outcome(kind="return", value={"__callable__": "lib.option.<locals>.decorator"})]
    c = compare_outcomes(a, b)
    assert c.comparison is Comparison.EQUAL and c.level is ComparisonLevel.TYPE
    assert c.notes == ["closure: lib.version_option.<locals>.decorator -> "
                       "lib.option.<locals>.decorator"]
    other = [Outcome(kind="return", value={"__callable__": "lib.option.<locals>.other"})]
    assert compare_outcomes(a, other).comparison is Comparison.CHANGED


def test_unobserved_c_return_is_compared_by_kind_only():
    un = [Outcome(kind="return", value={"__opaque__": "unobserved:c-return"})]
    real = [Outcome(kind="return", value={"__list__": [1]})]
    c = compare_outcomes(un, real)
    assert c.comparison is Comparison.EQUAL and c.level is ComparisonLevel.TYPE
    assert "c-return unobserved" in c.notes
    assert compare_outcomes(un, un).comparison is Comparison.EQUAL
    boom = [Outcome(kind="raise", value="ValueError")]
    assert compare_outcomes(un, boom).comparison is Comparison.CHANGED
    unk = [Outcome(kind="raise", value="<unknown>")]
    assert compare_outcomes(unk, boom).comparison is Comparison.EQUAL


def test_wrapped_compiled_call_pairs_with_a_c_call_event(tmp_path):
    old = [rv("lib.c", {"bound": {"a": 1}}, result=[1], line=10),
           rv("lib.c", {"bound": {"a": 2}}, result=[2], line=10)]
    new = [rv("lib.c", {"__c_call__": True}, result={"__opaque__": "unobserved:c-return"})]
    d = diff_sites(tmp_path, old, new)
    # The compiled-call fact pairs with one wrapped call; the other cannot pair by input (the
    # new side recorded no inputs there), so it is not comparable, not disappeared.
    assert d.counts()["equal"] == 1 and d.counts()["disappeared"] == 0
    (e,) = [e for e in d.entries if e.category is FactCategory.UNCOMPARABLE]
    assert e.notes[-1] == "call-shape:new" and e.explained.startswith("call-shape:")


def test_comprehension_scope_is_normalized_to_the_enclosing_function(tmp_path):
    text = {APP + ":10": "return [lib.f(x) for x in xs]"}
    old = [rv("lib.f", {"bound": {"x": 1}}, result=1, line=10) | {
               "where": {"file": APP, "line": 10, "func": "<listcomp>"}},
           rv("lib.g", {"bound": {}}, result=1, line=10)]
    new = [rv("lib.f", {"bound": {"x": 1}}, result=1, line=10),
           rv("lib.g", {"bound": {}}, result=1, line=10)]
    d = run_diff(tmp_path, old, new, old_text=text, new_text=text)
    assert d.counts()["equal"] == 2 and d.verdict is VerificationVerdict.PASS


def test_qualified_comprehension_scope_strips_to_the_function(tmp_path):
    text = {APP + ":10": "return {k: lib.f(k) for k in ks}"}
    old = [rv("lib.f", {"bound": {"x": 1}}, result=1) | {
        "where": {"file": APP, "line": 10, "func": "main.<locals>.<dictcomp>"}}]
    new = [rv("lib.f", {"bound": {"x": 1}}, result=1)]
    d = run_diff(tmp_path, old, new, old_text=text, new_text=text)
    assert d.counts()["equal"] == 1


def test_func_mismatch_at_identical_line_text_pairs_with_a_changed_input(tmp_path):
    text = {APP + ":10": "x = lib.f(a=1)"}
    old = [rv("lib.f", {"bound": {"a": 1}}, result=1) | {
        "where": {"file": APP, "line": 10, "func": "outer"}}]
    new = [rv("lib.f", {"bound": {"b": 1}}, result=1) | {
        "where": {"file": APP, "line": 10, "func": "inner"}}]
    d = run_diff(tmp_path, old, new, old_text=text, new_text=text)
    e = only(d, FactCategory.EQUAL)
    assert "site-moved" in e.notes and "matched-modulo-input" in e.notes


# ── Round 3 (V): edit provenance ─────────────────────────────────────────────────────────

OLD_SRC = "def main():\n    a = lib.f(1)\n    return lib.g(a)\n"
NEW_SRC = "def main():\n    a = lib.f(1)\n    return lib.h(a)\n"


def prov_diff(tmp_path, old, new, *, old_src=OLD_SRC, new_src=NEW_SRC, edits=(),
              texts=None, **ctx):
    old_t = texts or {}
    context = DiffContext(old_sources={"app.py": old_src}, new_sources={"app.py": new_src},
                          edits=list(edits), **ctx)
    o = write_trace(tmp_path, "old.jsonl", old, old_t.get("old", {
        APP + ":2": "a = lib.f(1)", APP + ":3": "return lib.g(a)"}))
    n = write_trace(tmp_path, "new.jsonl", new, old_t.get("new", {
        APP + ":2": "a = lib.f(1)", APP + ":3": "return lib.h(a)"}))
    return diff_traces(o, n, root=ROOT, context=context)


def call(q, line, result, bound=None, **kw):
    return rv(q, {"bound": bound or {}}, result=result, line=line, **kw)


def test_unedited_line_difference_is_a_strict_failure(tmp_path):
    d = prov_diff(tmp_path, [call("lib.f", 2, 1)], [call("lib.f", 2, 2)])
    assert only(d, FactCategory.CHANGED).fails and d.verdict is VerificationVerdict.FAIL


def test_hand_edit_equal_value_across_symbols_is_anchored_and_equal(tmp_path):
    d = prov_diff(tmp_path, [call("lib.g", 3, 5, {"x": 1})], [call("lib.h", 3, 5, {"y": 1})])
    e = only(d, FactCategory.REWRITTEN)
    assert e.comparison is Comparison.EQUAL and e.provenance == "hand" and "anchored" in e.notes
    assert d.verdict is VerificationVerdict.PASS
    assert d.edited_summary() == ("1 edited sites: 1 equal, 0 representation-only, "
                                  "0 need review, 0 unverified")


def test_hand_edit_value_difference_is_a_review_item(tmp_path):
    d = prov_diff(tmp_path, [call("lib.g", 3, 5)], [call("lib.h", 3, 6)])
    e = only(d, FactCategory.REWRITTEN)
    assert e.review and e.inconclusive and not e.fails
    assert d.verdict is VerificationVerdict.INCONCLUSIVE and not d.failures()
    assert "1 need review" in d.edited_summary()


def test_hand_edit_representation_only_is_drift(tmp_path):
    old = [call("lib.g", 3, {"__list__": [1, 2]})]
    new = [call("lib.h", 3, {"__tuple__": [1, 2]})]
    d = prov_diff(tmp_path, old, new)
    e = only(d, FactCategory.DRIFT)
    assert "representation changed" in e.notes and d.verdict is VerificationVerdict.PASS
    assert "1 representation-only" in d.edited_summary()


def test_representation_only_needs_both_sides_rendered(tmp_path):
    old = [call("lib.g", 3, {"__list__": [1, 2]})]
    new = [call("lib.h", 3, {"__opaque__": "lib.View"})]
    assert only(prov_diff(tmp_path, old, new), FactCategory.REWRITTEN).review


def test_matrix_vs_ndarray_with_equal_values_is_representation_only(tmp_path):
    mk = lambda name: {"__structure__": name, "shape": [1], "values": {"__list__": [[1]]}}  # noqa: E731
    d = prov_diff(tmp_path, [call("lib.g", 3, mk("numpy.matrix"))],
                  [call("lib.h", 3, mk("numpy.ndarray"))])
    assert only(d, FactCategory.DRIFT)


def test_namedtuple_object_vs_tuple_is_representation_only(tmp_path):
    nt = {"__object__": "lib.Pt", "attrs": {"x": 1, "y": 2}}
    d = prov_diff(tmp_path, [call("lib.g", 3, {"__tuple__": [1, 2]})], [call("lib.h", 3, nt)])
    assert only(d, FactCategory.DRIFT)


def test_hand_edit_new_raise_fails(tmp_path):
    new = [rv("lib.h", {"bound": {}}, line=3) | {"t": "raise", "raised": "TypeError"}]
    new[0].pop("result")
    d = prov_diff(tmp_path, [call("lib.g", 3, 5)], new)
    assert d.failures() and d.verdict is VerificationVerdict.FAIL


def test_ledger_edit_with_a_strict_claim_fails_on_a_difference(tmp_path):
    edit = {"file": "app.py", "old_lines": [3, 3], "new_lines": [3, 3], "claim": "same-object",
            "old_symbol": "lib.g", "new_symbol": "lib.h"}
    d = prov_diff(tmp_path, [call("lib.g", 3, 5)], [call("lib.h", 3, 6)], edits=[edit])
    e = only(d, FactCategory.REWRITTEN)
    assert e.provenance == "ledger" and e.fails


def test_ledger_edit_with_a_null_claim_is_judged_like_a_hand_edit(tmp_path):
    edit = {"file": "app.py", "old_lines": [3, 3], "new_lines": [3, 3], "claim": None,
            "old_symbol": "lib.g", "new_symbol": "lib.h"}
    d = prov_diff(tmp_path, [call("lib.g", 3, 5)], [call("lib.h", 3, 6)], edits=[edit])
    assert only(d, FactCategory.REWRITTEN).review


def test_no_positional_pairing_across_unrelated_symbols(tmp_path):
    src_old = "def main():\n    a = lib.f(1)\n    lib.g(a)\n"
    src_new = "def main():\n    a = lib.f(1)\n    lib.h(a)\n"
    d = prov_diff(tmp_path, [call("lib.g", 3, 5)], [call("lib.h", 3, 6)],
                  old_src=src_old, new_src=src_new)
    assert d.counts()["rewritten"] == 2 and not d.failures()
    assert all(e.unverified for e in d.entries) and d.verdict is VerificationVerdict.PASS
    assert "2 unverified" in d.edited_summary()


def test_scalar_values_choose_among_anchored_candidates(tmp_path):
    src_old = "def main():\n    return q.get(1)\n"
    src_new = "def main():\n    return session.get(Book, 1)\n"
    old = [call("lib.Query.get", 2, "a", {"ident": 1}), call("lib.Query.get", 2, "b", {"ident": 2})]
    new = [call("lib.Session.get", 2, "b", {"entity": {"__opaque__": "Book"}, "ident": 2}),
           call("lib.Session.get", 2, "a", {"entity": {"__opaque__": "Book"}, "ident": 1})]
    texts = {"old": {APP + ":2": "return q.get(1)"},
             "new": {APP + ":2": "return session.get(Book, 1)"}}
    d = prov_diff(tmp_path, old, new, old_src=src_old, new_src=src_new, texts=texts)
    assert d.counts()["rewritten"] == 2 and not d.failures()
    assert all(e.comparison is Comparison.EQUAL for e in d.entries)


def test_vanished_call_on_an_unedited_line_fails_unless_the_project_redefined_the_name(
        tmp_path):
    old = [call("lib.f", 2, 1), call("lib.g", 3, 5, {"x": 1})]
    new = [call("lib.f", 2, 1)]
    src = "def main():\n    a = lib.f(1)\n    return lib.g(a)\n"
    d = prov_diff(tmp_path, old, new, old_src=src, new_src=src)
    assert only(d, FactCategory.DISAPPEARED).fails
    # The contact map alone never downgrades: a removed dependency symbol is the break itself.
    gone = {"app.py": {3: set()}}
    d = prov_diff(tmp_path, old, new, old_src=src, new_src=src,
                  old_contacts={"app.py": {3: {"lib.g"}}}, new_contacts=gone)
    assert only(d, FactCategory.DISAPPEARED).fails
    # ... but a name the project now defines itself is no longer a dependency call.
    local = src + "\n\nclass lib:\n    def g(a):\n        return a\n"
    d = prov_diff(tmp_path, old, new, old_src=src, new_src=local)
    e = only(d, FactCategory.REWRITTEN)
    assert e.review == "no longer a dependency call here" and not d.failures()
    assert d.verdict is VerificationVerdict.INCONCLUSIVE


def test_a_relative_import_counts_as_project_code_and_a_dependency_import_does_not(tmp_path):
    old = [call("lib.f", 2, 1), call("lib.g", 3, 5, {"x": 1})]
    new = [call("lib.f", 2, 1)]
    src = "def main():\n    a = lib.f(1)\n    return lib.g(a)\n"
    mine = src + "\n\nfrom . import lib\n"
    d = prov_diff(tmp_path, old, new, old_src=src, new_src=mine)
    assert only(d, FactCategory.REWRITTEN).review and not d.failures()
    theirs = src + "\n\nimport lib\n"
    assert prov_diff(tmp_path, old, new, old_src=src, new_src=theirs).failures()


def test_vanished_call_in_a_function_that_never_ran_is_not_reached(tmp_path):
    old = [call("lib.g", 3, 5, {"x": 1})]
    new = [rv("lib.z", {"bound": {}}, line=9) | {
        "where": {"file": APP, "line": 9, "func": "other"}}]
    src = "def main():\n    a = lib.f(1)\n    return lib.g(a)\n"
    d = prov_diff(tmp_path, old, new, old_src=src, new_src=src)
    assert any(e.review and "not reached" in e.review for e in d.entries)


def test_noise_is_dropped_or_never_a_verdict_driver(tmp_path):
    src = "def main():\n    a = lib.f(1)\n    return lib.g(a)\n"
    new = [call("lib.C.__del__", 2, None), call("importlib._bootstrap._find_and_load", 2, 1),
           call("lib.M.__init_subclass__", 2, None)]
    d = prov_diff(tmp_path, [], new, old_src=src, new_src=src)
    assert d.verdict is VerificationVerdict.INCONCLUSIVE or d.verdict is VerificationVerdict.PASS
    assert not d.failures()
    assert not any(e.symbol.endswith("__del__") for e in d.entries)
    assert d.counts()["drift"] == 2


def test_type_relocations_apply_to_raises_and_markers(tmp_path):
    a = [Outcome(kind="raise", value="pydantic.ValidationError")]
    b = [Outcome(kind="raise", value="pydantic_core.ValidationError")]
    assert compare_outcomes(a, b).comparison is Comparison.CHANGED
    old = [call("lib.f", 2, {"__opaque__": "pydantic.Model"})]
    new = [call("lib.f", 2, {"__opaque__": "pydantic_core.Model"})]
    d = prov_diff(tmp_path, old, new, type_relocations={"pydantic.Model": "pydantic_core.Model"})
    assert d.counts()["equal"] == 1 and not d.failures()


def test_downstream_of_an_edit_is_judged_as_a_hand_edit(tmp_path):
    src_old = "def main():\n    m = lib.make()\n    return m.sum()\n"
    src_new = "def main():\n    m = lib.make2()\n    return m.sum()\n"
    old = [call("lib.make", 2, {"__opaque__": "lib.Mat"}),
           rv("lib.Mat.sum", {"bound": {"self": {"__opaque__": "lib.Mat"}}}, result=3, line=3)]
    new = [call("lib.make2", 2, {"__opaque__": "lib.Arr"}),
           rv("lib.Arr.sum", {"bound": {"self": {"__opaque__": "lib.Arr"}}}, result=4, line=3)]
    texts = {"old": {APP + ":2": "m = lib.make()", APP + ":3": "return m.sum()"},
             "new": {APP + ":2": "m = lib.make2()", APP + ":3": "return m.sum()"}}
    d = prov_diff(tmp_path, old, new, old_src=src_old, new_src=src_new, texts=texts)
    assert not d.failures()


def test_without_a_context_values_on_rewritten_lines_still_fail(tmp_path):
    d = _rewrite_case(tmp_path, S13)
    assert d.verdict is VerificationVerdict.FAIL


# ── Round 3b ─────────────────────────────────────────────────────────────────────────────

SAME_SRC = "def main():\n    a = lib.f(1)\n    return lib.g(a)\n"


def test_non_dunder_call_not_named_on_its_line_stays_strict(tmp_path):
    src = "def main():\n    f = lib.func\n    return f()\n"
    texts = {"old": {APP + ":3": "return f()"}, "new": {APP + ":3": "return f()"}}
    other = rv("lib.other", {"bound": {}}, line=2)
    d = prov_diff(tmp_path, [call("lib.func", 3, 1)], [other], old_src=src, new_src=src,
                  texts={**texts, "new": {**texts["new"], APP + ":2": "f = lib.func"}})
    assert only(d, FactCategory.DISAPPEARED).fails


def test_dunder_call_not_named_on_its_line_is_drift(tmp_path):
    src = "def main():\n    with lib.ctx():\n        pass\n"
    texts = {"old": {APP + ":2": "with lib.ctx():"}, "new": {APP + ":2": "with lib.ctx():"}}
    other = rv("lib.other", {"bound": {}}, line=3)
    other_old = rv("lib.other", {"bound": {}}, line=3)
    d = prov_diff(tmp_path, [call("lib.C.__enter__", 2, 1), other_old], [other], old_src=src,
                  new_src=src, texts={"old": {**texts["old"], APP + ":3": "pass"},
                                      "new": {**texts["new"], APP + ":3": "pass"}})
    assert only(d, FactCategory.DRIFT) and not d.failures()


def test_no_ledger_judges_edits_as_hand_edits(tmp_path):
    ctx = DiffContext(old_sources={"app.py": OLD_SRC}, new_sources={"app.py": NEW_SRC})
    o = write_trace(tmp_path, "o.jsonl", [call("lib.g", 3, 5)], {APP + ":3": "return lib.g(a)"})
    n = write_trace(tmp_path, "n.jsonl", [call("lib.h", 3, 6)], {APP + ":3": "return lib.h(a)"})
    d = diff_traces(o, n, root=ROOT, context=ctx)
    assert only(d, FactCategory.REWRITTEN).review and d.verdict is VerificationVerdict.INCONCLUSIVE
    assert "no apply ledger: edits judged as hand edits" in d.edited_summary()


def test_chained_statement_pairs_across_line_attribution(tmp_path):
    src = "def main():\n    return (select(a)\n        .where(b)\n        .order_by(c))\n"
    old = [call("lib.select", 2, 1), call("lib.where", 2, 2), call("lib.order_by", 2, 3)]
    new = [call("lib.select", 2, 1), call("lib.where", 3, 2), call("lib.order_by", 4, 3)]
    texts = {"old": {APP + ":2": "return (select(a)"},
             "new": {APP + ":2": "return (select(a)", APP + ":3": ".where(b)",
                     APP + ":4": ".order_by(c))"}}
    d = prov_diff(tmp_path, old, new, old_src=src, new_src=src, texts=texts)
    assert d.counts()["disappeared"] == 0 and d.counts()["appeared"] == 0 and not d.failures()


def test_decorator_wrapper_naming_pairs_one_to_one_at_an_unedited_statement(tmp_path):
    src = "def main():\n    return nx.pagerank(G)\n"
    texts = {"old": {APP + ":2": "return nx.pagerank(G)"},
             "new": {APP + ":2": "return nx.pagerank(G)"}}
    d = prov_diff(tmp_path, [call("decorator.fun", 2, {"__dict__": [["a", 1]]}, {"x": 1})],
                  [call("nx.argmap.__call__.<locals>.func", 2, {"__dict__": [["a", 2]]},
                        {"y": 1})],
                  old_src=src, new_src=src, texts=texts)
    e = only(d, FactCategory.EQUAL)
    assert "wrapper-naming" in e.notes
    two = prov_diff(tmp_path, [call("decorator.fun", 2, 1), call("lib.x", 2, 1)],
                    [call("nx.argmap.__call__.<locals>.func", 2, 1), call("lib.y", 2, 1)],
                    old_src=src, new_src=src, texts=texts)
    assert not any("wrapper-naming" in e.notes for e in two.entries)


def test_wrapper_internals_and_instrumented_constructors_are_noise(tmp_path):
    src = "def main():\n    G = nx.Graph(name=1)\n    return G\n"
    line = {APP + ":2": "G = nx.Graph(name=1)"}
    texts = {"old": line, "new": line}
    new = [call("nx.argmap.__call__.<locals>.func", 2, 1), call("nx.argmap___new___1", 2, 1),
           call("sa.orm.instrumentation.__init__", 2, None)]
    d = prov_diff(tmp_path, [], new, old_src=src, new_src=src, texts=texts)
    assert d.counts()["drift"] == 3 and not d.failures()


def test_a_call_result_from_project_code_is_a_runtime_value_not_project_code(tmp_path):
    old = [call("lib.f", 2, 1), call("lib.keys", 3, 5)]
    new = [call("lib.f", 2, 1)]
    src = "def main():\n    row = make()\n    return row.keys()\n"
    mine = src + "\n\ndef make():\n    return lib.row()\n"
    texts = {"old": {APP + ":2": "row = make()", APP + ":3": "return row.keys()"},
             "new": {APP + ":2": "row = make()", APP + ":3": "return row.keys()"}}
    d = prov_diff(tmp_path, [call("lib.f", 2, 1), call("lib.keys", 3, 5)], new, old_src=src,
                  new_src=mine, texts=texts)
    assert only(d, FactCategory.DISAPPEARED).fails
    assert old


# ── Round 3d ─────────────────────────────────────────────────────────────────────────────

def self_call(q, line, result, self_type="lib.G"):
    return rv(q, {"bound": {"self": {"__opaque__": self_type}}}, result=result, line=line)


def test_vanished_getter_whose_value_was_a_receiver_is_an_attribute_read(tmp_path):
    src = "def main(G):\n    return sorted(G.nodes())\n"
    texts = {"old": {APP + ":2": "return sorted(G.nodes())"},
             "new": {APP + ":2": "return sorted(G.nodes())"}}
    old = [self_call("lib.G.nodes", 2, {"__opaque__": "lib.NodeView"}),
           self_call("lib.NodeView.__call__", 2, [1], "lib.NodeView")]
    new = [self_call("lib.NodeView.__call__", 2, [1], "lib.NodeView")]
    d = prov_diff(tmp_path, old, new, old_src=src, new_src=src, texts=texts)
    e = only(d, FactCategory.DRIFT)
    assert "attribute read no longer goes through a traced getter" in e.notes
    assert not d.failures()


def test_vanished_getter_called_with_arguments_it_did_not_bind_is_an_attribute_read(tmp_path):
    src = "def main(G):\n    return list(G.edges(data=True))\n"
    texts = {"old": {APP + ":2": "return list(G.edges(data=True))"},
             "new": {APP + ":2": "return list(G.edges(data=True))"}}
    d = prov_diff(tmp_path, [self_call("lib.G.edges", 2, 1)],
                  [rv("lib.other", {"bound": {}}, line=2)], old_src=src, new_src=src,
                  texts=texts)
    assert only(d, FactCategory.DRIFT)
    assert not [e for e in d.failures() if e.symbol.endswith("edges")]


def test_vanished_no_argument_call_nobody_uses_as_a_receiver_stays_strict(tmp_path):
    src = "def main(row):\n    return list(row.keys())\n"
    texts = {"old": {APP + ":2": "return list(row.keys())"},
             "new": {APP + ":2": "return list(row.keys())"}}
    d = prov_diff(tmp_path, [self_call("lib.Row.keys", 2, {"__list__": ["a"]}, "lib.Row")],
                  [rv("lib.other", {"bound": {}}, line=2)], old_src=src, new_src=src,
                  texts=texts)
    assert only(d, FactCategory.DISAPPEARED).fails


def test_fact_lands_on_the_consumer_of_a_generator_but_belongs_to_the_naming_statement(
        tmp_path):
    old_src = "def main(G):\n    parts = nx.subgraphs(G)\n    return max(parts)\n"
    new_src = "def main(G):\n    parts = (g for g in nx.components(G))\n    return max(parts)\n"
    texts = {"old": {APP + ":3": "return max(parts)"}, "new": {APP + ":3": "return max(parts)"}}
    call_ = rv("nx.subgraphs", {"bound": {"G": {"__opaque__": "nx.G"}}}, result=1, line=3)
    d = prov_diff(tmp_path, [call_], [rv("lib.o", {"bound": {}}, line=3)], old_src=old_src,
                  new_src=new_src, texts=texts)
    gone = [e for e in d.entries if e.symbol == "nx.subgraphs"][0]
    assert gone.unverified and not gone.fails
    # an alias whose naming statement was NOT edited stays strict
    same = old_src
    d = prov_diff(tmp_path, [call_], [rv("lib.o", {"bound": {}}, line=3)], old_src=old_src,
                  new_src=same, texts=texts)
    assert [e for e in d.entries if e.symbol == "nx.subgraphs"][0].fails


# ── Round 3e ─────────────────────────────────────────────────────────────────────────────

def test_opaque_attribute_does_not_hide_a_known_attribute_difference():
    exc = {"__opaque__": "SystemExit"}
    a = obj("lib.Result", exit_code=0, exception=exc, exc_info={"__tuple__": [exc]})
    b = obj("lib.Result", exit_code=2, exception=exc, exc_info={"__tuple__": [exc]})
    assert cmp_obj(a, b).comparison is Comparison.CHANGED
    same = cmp_obj(a, a)
    assert same.comparison is Comparison.EQUAL and same.level is ComparisonLevel.TYPE


def test_opaque_attributes_compare_by_type_and_a_truncated_object_is_type_only():
    a = obj("lib.R", exit_code=0, exception={"__opaque__": "lib.a.Err"})
    b = obj("lib.R", exit_code=0, exception={"__opaque__": "lib.b.Err"})
    assert cmp_obj(a, b).comparison is Comparison.EQUAL
    c = obj("lib.R", exit_code=0, exception={"__opaque__": "lib.Other"})
    assert cmp_obj(a, c).comparison is Comparison.CHANGED
    cut = {**obj("lib.R", exit_code=0), "__truncated__": True}
    assert cmp_obj(cut, obj("lib.R", exit_code=2)).comparison is Comparison.EQUAL


def test_type_level_object_comparison_ignores_one_sided_attributes():
    a = [Outcome(kind="return", value=obj("lib.S", a=1)),
         Outcome(kind="return", value=obj("lib.S", a=2))]
    b = [Outcome(kind="return", value=obj("lib.S", a=1, extra=3)),
         Outcome(kind="return", value=obj("lib.S", a=2, extra=4))]
    assert compare_outcomes(a, b).comparison is Comparison.EQUAL
