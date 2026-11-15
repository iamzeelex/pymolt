"""Predicted risks (baseline facts x delta facts, per call site) and their calibration
against the post-migration fact diff; stored in the plan, scored by verify."""
from __future__ import annotations

import json
import sys

import pytest

from pymolt.verify.facts import (
    CallWarning,
    SiteWarnings,
    diff_facts,
    fold_facts,
)
from pymolt.verify.predict import (
    CALIBRATION_SCHEMA,
    RISKS_SCHEMA,
    CalibrationResult,
    PredictionReport,
    RiskTier,
    calibrate,
    planned_rewrites,
    predict_plan_risks,
    predict_risks,
)

ROOT = "/proj"
CORE = ROOT + "/toyapp/core.py"
MEAN = "pandas.core.frame.DataFrame.mean"
APPEND = "pandas.core.series.Series.append"
TO_DICT = "pandas.core.series.Series.to_dict"
MEAN_WARNING = {"category": "FutureWarning",
                "message": "The default value of numeric_only in DataFrame.mean is deprecated.",
                "count": 1}
APPEND_WARNING = {"category": "FutureWarning",
                  "message": "The series.append method is deprecated", "count": 1}


def _ev(sym, line, func, *, bound=None, defaulted=(), result=1, raised=None, warnings=None,
        file=CORE):
    ev = {"v": 2, "t": "raise" if raised else "return", "q": sym, "sym": sym,
          "in": {"bound": {"self": {"__opaque__": "x"}, **(bound or {})},
                 "defaulted": list(defaulted)},
          "where": {"file": file, "line": line, "func": func}}
    if raised:
        ev["raised"] = raised
    else:
        ev["result"] = result
    if warnings:
        ev["warnings"] = warnings
    return ev


SITE_TEXT = {f"{CORE}:13": "return s1.append(s2, ignore_index=True)",
             f"{CORE}:24": "return df.mean().to_dict()",
             f"{CORE}:18": 'return df.groupby("k")["v"].sum().to_dict()'}


def _baseline(*extra, warnings_capture="on"):
    """The pandas toy's baseline, shaped as pandas 1.5.3 records it."""
    fs = fold_facts([
        _ev(APPEND, 13, "combine", bound={"to_append": [3], "ignore_index": True},
            defaulted=["verify_integrity"], result=[1, 2, 3], warnings=[APPEND_WARNING]),
        _ev("pandas.core.frame.DataFrame.groupby", 18, "summarize", bound={"by": "k"},
            defaulted=["as_index", "axis", "squeeze"], result={"__opaque__": "g"}),
        _ev(MEAN, 24, "average", defaulted=["axis", "level", "numeric_only", "skipna"],
            result={"v": 2.0}, warnings=[MEAN_WARNING]),
        _ev(TO_DICT, 24, "average", defaulted=["into"], result={"v": 2.0}),
        *extra,
    ], root=ROOT, site_text=SITE_TEXT, source="baseline.jsonl")
    fs.warnings_capture = warnings_capture
    return fs


def _delta(*facts, package="pandas", quality="complete"):
    return {"schema": "pymolt.delta/1", "package": package, "from": "1.5.3", "to": "2.2.3",
            "old_python": "3.11.13", "new_python": "3.12.11", "quality": quality,
            "facts": list(facts)}


DEFAULT_MEAN = {"symbol": MEAN, "aliases": ["pandas.DataFrame.mean"], "kind": "DEFAULT",
                "param": "numeric_only", "old": "None", "new": "False",
                "deprecated_in_old": True, "successor": None,
                "evidence": ["signature", "warning:FutureWarning: The default value ..."]}
REMOVED_APPEND = {"symbol": APPEND, "aliases": ["pandas.Series.append"], "kind": "REMOVED",
                  "successor": "pandas.concat", "evidence": ["existence"]}
CHANGES = [{"path": "toyapp/core.py",
            "old_source": "\n" * 12 + "    return s1.append(s2, ignore_index=True)\n",
            "new_source": "\n" * 12 + "    return pd.concat([s1, s2], ignore_index=True)\n",
            "recipes": ["rule:pandas.Series.append->pandas.concat"]}]


def _risk(report, symbol, line=None):
    found = [r for r in report.risks if r.symbol == symbol
             and (line is None or r.site.line == line)]
    assert len(found) == 1, [(r.symbol, r.site.line) for r in report.risks]
    return found[0]


# ── the acceptance shape: the pandas toy ─────────────────────────────────────


def test_pandas_toy_mean_is_high_from_the_default_change_and_the_warning():
    report = predict_risks(_baseline(), [_delta(DEFAULT_MEAN, REMOVED_APPEND)],
                           changes=CHANGES, root=ROOT)
    mean = _risk(report, MEAN, 24)
    assert mean.tier is RiskTier.HIGH and not mean.fixed_by_plan
    assert mean.site.text == "return df.mean().to_dict()"
    assert mean.fact["kind"] == "DEFAULT" and mean.fact["param"] == "numeric_only"
    assert any(e.startswith("baseline warning: FutureWarning") for e in mean.evidence)
    assert any("numeric_only left at its default" in e for e in mean.evidence)
    assert mean.warnings == [CallWarning(**MEAN_WARNING)]

    append = _risk(report, APPEND, 13)
    assert append.tier is RiskTier.CERTAIN and append.fixed_by_plan
    assert append.fixed_by == ["rule:pandas.Series.append->pandas.concat"]
    assert report.status == "complete"
    assert [r.symbol for r in report.needs_attention] == [MEAN]
    assert [r.symbol for r in report.fixed] == [APPEND]
    # The site that passes nothing the delta touches carries no risk.
    assert all(r.symbol != TO_DICT for r in report.risks)


def test_the_warning_alone_predicts_high_without_delta_facts():
    report = predict_risks(_baseline(), [])
    mean = _risk(report, MEAN, 24)
    assert mean.tier is RiskTier.HIGH and mean.fact is None
    assert "FutureWarning" in mean.reason
    assert report.status == "partial"
    assert any("no delta facts" in n for n in report.notes)


def test_a_baseline_without_warning_capture_is_noted():
    report = predict_risks(_baseline(warnings_capture=None), [_delta(DEFAULT_MEAN)])
    assert report.status == "partial"
    assert any("without runtime-warning capture" in n for n in report.notes)


# ── the tier rules ───────────────────────────────────────────────────────────


def _one_site(**kw):
    return fold_facts([_ev("lib.mod.f", 5, "use", **kw)], root=ROOT)


def _tier(baseline, *facts):
    report = predict_risks(baseline, [_delta(*facts, package="lib")])
    return report.risks[0].tier if report.risks else None


def test_removed_is_certain_and_moved_is_low():
    assert _tier(_one_site(), {"symbol": "lib.mod.f", "kind": "REMOVED"}) is RiskTier.CERTAIN
    assert _tier(_one_site(), {"symbol": "lib.mod.f", "kind": "MOVED",
                               "successor": "lib.new.f"}) is RiskTier.LOW


def test_default_change_counts_only_where_the_site_relied_on_the_default():
    fact = {"symbol": "lib.mod.f", "kind": "DEFAULT", "param": "n", "old": "1", "new": "2"}
    assert _tier(_one_site(defaulted=["n"]), fact) is RiskTier.HIGH
    assert _tier(_one_site(bound={"n": 3}), fact) is None
    # Defaults not reported (v1 / unknown defaults): not passed means left at its default.
    unknown = fold_facts([{"v": 1, "t": "return", "q": "lib.mod.f", "in": {"bound": {}},
                           "result": 1, "where": {"file": CORE, "line": 5, "func": "use"}}],
                         root=ROOT)
    assert _tier(unknown, fact) is RiskTier.HIGH


def test_signature_change_is_medium_for_a_passed_or_newly_required_parameter():
    removed = {"symbol": "lib.mod.f", "kind": "SIGNATURE", "param": "n", "old": "n=1",
               "new": None}
    assert _tier(_one_site(bound={"n": 3}), removed) is RiskTier.MEDIUM
    assert _tier(_one_site(defaulted=["n"]), removed) is None
    added = {"symbol": "lib.mod.f", "kind": "SIGNATURE", "param": "m", "old": None,
             "new": "m"}
    assert _tier(_one_site(), added) is RiskTier.MEDIUM
    bases = {"symbol": "lib.mod.f", "kind": "SIGNATURE", "param": None,
             "evidence": ["bases"]}
    assert _tier(_one_site(), bases) is RiskTier.LOW


def test_deprecated_is_low_unless_the_site_warned():
    fact = {"symbol": "lib.mod.f", "kind": "DEPRECATED", "deprecated_in_old": True}
    assert _tier(_one_site(), fact) is RiskTier.LOW
    warned = _one_site(warnings=[{"category": "DeprecationWarning", "message": "f is old"}])
    report = predict_risks(warned, [_delta(fact, package="lib")])
    (risk,) = report.risks
    assert risk.tier is RiskTier.HIGH and risk.fact["kind"] == "DEPRECATED"


def test_constructor_and_enclosing_matches():
    ctor = fold_facts([_ev("lib.mod.Cls.__init__", 5, "use", bound={"x": 1})], root=ROOT)
    sig = {"symbol": "lib.mod.Cls", "kind": "SIGNATURE", "param": "x", "old": "x", "new": None}
    assert _tier(ctor, sig) is RiskTier.MEDIUM
    method = fold_facts([_ev("lib.mod.Cls.run", 5, "use")], root=ROOT)
    assert _tier(method, {"symbol": "lib.mod.Cls", "kind": "REMOVED"}) is RiskTier.CERTAIN
    # A parameter fact on the class is not a fact about every method.
    assert _tier(method, sig) is None


def test_aliases_match_and_unsampled_warnings_count():
    fs = _one_site()
    fs.unsampled_warnings.append(SiteWarnings(
        symbol="lib.mod.f", file="toyapp/core.py", func="use", line=5,
        warnings=[CallWarning(category="FutureWarning", message="later", count=4)]))
    report = predict_risks(fs, [_delta({"symbol": "lib.f_public", "aliases": ["lib.mod.f"],
                                        "kind": "DEPRECATED", "deprecated_in_old": True},
                                       package="lib")])
    (risk,) = report.risks
    assert risk.tier is RiskTier.HIGH
    assert risk.warnings[0].count == 4


def test_a_change_reached_only_statically_is_a_low_blind_risk():
    contacts = [{"caller": "toyapp.core.never_run", "target": "lib.helper",
                 "dep": "lib", "file": "toyapp/core.py"}]
    report = predict_risks(_one_site(), [_delta({"symbol": "lib._impl.helper",
                                                 "aliases": ["lib.helper"],
                                                 "kind": "REMOVED"}, package="lib")],
                           static_contacts=contacts, root=ROOT)
    (risk,) = report.risks
    assert risk.basis == "static" and risk.tier is RiskTier.LOW
    assert (risk.site.file, risk.site.func, risk.site.line) == (
        "toyapp/core.py", "never_run", None)


def test_partial_delta_quality_lowers_the_status():
    report = predict_risks(_baseline(), [_delta(DEFAULT_MEAN, quality="inconclusive")])
    assert report.status == "partial"
    assert any("inconclusive" in n for n in report.notes)


def test_planned_rewrites_map_changed_old_lines():
    (block,) = planned_rewrites(CHANGES)["toyapp/core.py"]
    assert (block.start, block.end) == (13, 13)
    assert "pd.concat" in block.new_text


def test_delta_report_models_are_accepted():
    pytest.importorskip("pymolt.delta.models")
    from pymolt.delta.models import DeltaReport

    rep = DeltaReport.model_validate(_delta(DEFAULT_MEAN))
    report = predict_risks(_baseline(), [rep])
    assert _risk(report, MEAN, 24).fact["param"] == "numeric_only"
    assert report.deltas[0].from_version == "1.5.3"


def test_report_serializes_with_its_schema_and_round_trips():
    report = predict_risks(_baseline(), [_delta(DEFAULT_MEAN)])
    dumped = report.model_dump(mode="json")
    assert dumped["schema"] == RISKS_SCHEMA
    assert PredictionReport.model_validate(dumped) == report


def test_a_call_time_default_is_medium_unless_the_sites_warning_names_it():
    copy = {"symbol": "pandas.core.series.Series.__init__", "kind": "DEFAULT",
            "param": "copy", "old": "False", "new": "None", "evidence": ["signature"]}
    axis = {"symbol": MEAN, "kind": "DEFAULT", "param": "axis", "old": "<no_default>",
            "new": "0", "evidence": ["signature"]}
    observed = {"symbol": "pandas.core.frame.DataFrame.groupby", "kind": "DEFAULT",
                "param": "as_index", "old": "True", "new": "False", "evidence": ["signature"]}
    series = _ev("pandas.core.series.Series.__init__", 11, "combine", bound={"data": [1]},
                 defaulted=["copy", "dtype"], result=None)
    report = predict_risks(_baseline(series), [_delta(copy, axis, observed, DEFAULT_MEAN)])
    # False -> None: resolved at call time, and nothing at the site warned about it.
    init = _risk(report, "pandas.core.series.Series.__init__", 11)
    assert init.tier is RiskTier.MEDIUM and "resolved at call time" in init.reason
    # Between two concrete values a defaulted parameter stays high.
    groupby = _risk(report, "pandas.core.frame.DataFrame.groupby", 18)
    assert groupby.tier is RiskTier.HIGH and groupby.fact["param"] == "as_index"
    # numeric_only None -> False is named by the site's own FutureWarning: high, and the
    # deciding fact (axis <no_default> -> 0 is only medium).
    mean = _risk(report, MEAN, 24)
    assert mean.tier is RiskTier.HIGH and mean.fact["param"] == "numeric_only"
    assert "warning names numeric_only" in mean.reason


# ── calibration ──────────────────────────────────────────────────────────────


POST_SITE_TEXT = {**SITE_TEXT, f"{CORE}:13": "return pd.concat([s1, s2], ignore_index=True)"}


def _post(mean_raises=True):
    return fold_facts([
        _ev("pandas.core.reshape.concat.concat", 13, "combine",
            bound={"objs": [[1, 2], [3]], "ignore_index": True}, result=[1, 2, 3]),
        _ev("pandas.core.frame.DataFrame.groupby", 18, "summarize", bound={"by": "k"},
            defaulted=["as_index", "axis"], result={"__opaque__": "g"}),
        (_ev(MEAN, 24, "average", defaulted=["axis", "numeric_only", "skipna"],
             raised="TypeError") if mean_raises else
         _ev(MEAN, 24, "average", defaulted=["axis", "numeric_only", "skipna"],
             result={"v": 2.0})),
        *([] if mean_raises else [_ev(TO_DICT, 24, "average", defaulted=["into"],
                                      result={"v": 2.0})]),
    ], root=ROOT, site_text=POST_SITE_TEXT, source="post.jsonl")


def test_calibration_scores_hits_misses_and_unexercised_sites():
    baseline = _baseline()
    report = predict_risks(baseline, [_delta(DEFAULT_MEAN, REMOVED_APPEND)],
                           changes=CHANGES, root=ROOT)
    fdiff = diff_facts(baseline, _post())
    cal = calibrate(report.risks, fdiff, plan_id="p1")
    by_symbol = {r.symbol: r for r in cal.risks}
    mean = by_symbol[MEAN]
    assert mean.tier is RiskTier.HIGH and mean.result is CalibrationResult.HIT
    assert {o.category for o in mean.observed} >= {"changed", "disappeared"}
    append = by_symbol[APPEND]
    # The plan rewrote the removed call and the rewrite kept the outcome: prevented, which
    # neither confirms nor disproves the prediction (never a miss).
    assert append.result is CalibrationResult.PREVENTED and append.fixed_by_plan
    assert "fixed by the plan" in append.note
    assert (cal.predicted, cal.hit, cal.prevented, cal.miss, cal.not_exercised) == \
        (2, 1, 1, 0, 0)
    assert cal.by_tier["high"]["hit"] == 1 and cal.by_tier["certain"]["prevented"] == 1
    assert cal.unpredicted == []
    assert cal.model_dump(mode="json")["schema"] == CALIBRATION_SCHEMA


def test_equivalent_migration_misses_and_unpredicted_changes_are_listed():
    baseline = _baseline()
    report = predict_risks(baseline, [_delta(DEFAULT_MEAN)])
    post = _post(mean_raises=False)
    post.facts = [f for f in post.facts if f.symbol != "pandas.core.frame.DataFrame.groupby"]
    cal = calibrate(report.risks, diff_facts(baseline, post))
    assert {r.symbol: r.result for r in cal.risks}[MEAN] is CalibrationResult.MISS
    # groupby vanished at a site no prediction named.
    assert [(u.category, u.symbol) for u in cal.unpredicted] == [
        ("disappeared", "pandas.core.frame.DataFrame.groupby")]


def test_another_call_on_the_same_line_does_not_score_a_risk():
    # ``df.groupby("k")["v"].sum()``: a drift of groupby is not GroupBy.sum's change.
    gsum = "pandas.core.groupby.groupby.GroupBy.sum"
    fact = {"symbol": gsum, "kind": "DEFAULT", "param": "min_count", "old": "0",
            "new": "1", "evidence": ["signature"]}
    extra = _ev(gsum, 18, "summarize", defaulted=["min_count"], result={"v": 3})
    baseline = _baseline(extra)
    report = predict_risks(baseline, [_delta(fact)])
    post = fold_facts([
        _ev("pandas.core.frame.DataFrame.groupby", 18, "summarize", bound={"by": "k"},
            defaulted=["as_index", "axis"], result={"__opaque__": "other"}),
        _ev(gsum, 18, "summarize", defaulted=["min_count"], result={"v": 3}),
        _ev(MEAN, 24, "average", defaulted=["axis", "numeric_only", "skipna"],
            result={"v": 2.0}),
        _ev(TO_DICT, 24, "average", defaulted=["into"], result={"v": 2.0}),
        _ev(APPEND, 13, "combine", bound={"to_append": [3], "ignore_index": True},
            defaulted=["verify_integrity"], result=[1, 2, 3]),
    ], root=ROOT, site_text=SITE_TEXT, source="post.jsonl")
    cal = calibrate(report.risks, diff_facts(baseline, post))
    assert {r.symbol: r.result for r in cal.risks}[gsum] is CalibrationResult.MISS


def test_without_a_fact_diff_nothing_is_exercised():
    report = predict_risks(_baseline(), [])
    cal = calibrate(report.risks, None, source="baseline")
    assert cal.hit == cal.miss == 0 and cal.not_exercised == cal.predicted > 0
    assert cal.notes


# ── plan and verify ──────────────────────────────────────────────────────────


WDEP_OLD = '''import warnings


def mean(rows, numeric_only=None):
    if numeric_only is None:
        warnings.warn("numeric_only default changes", FutureWarning, stacklevel=2)
    return sum(rows)
'''
# The new version's default no longer skips what it cannot average (a command capture
# records shapes, so the change must show at type level: a raise).
WDEP_NEW = WDEP_OLD.replace("return sum(rows)", "raise TypeError('cannot average')")
WORKLOAD = '''import sys
sys.path.insert(0, {vendor!r})
import wdepx


def run():
    try:
        return wdepx.mean([1, 2])
    except TypeError:
        return None


run()
'''


def _capture(tmp_path, when):
    from pymolt.verify import service
    from pymolt.verify.models import CaptureMode

    script = tmp_path / "app.py"
    script.write_text(WORKLOAD.format(vendor=str(tmp_path / "vendor")), encoding="utf-8")
    return service.capture_named_trace(
        tmp_path, when, CaptureMode.LIVE_COMMAND,
        command=[sys.executable, str(script)], target="wdepx")


@pytest.fixture
def captured(tmp_path):
    from pymolt.ingestion.config import EnvConfig, ToolChoice

    (tmp_path / "requirements.txt").write_text("wdepx==1\n", encoding="utf-8")
    EnvConfig(selected_manifest="requirements.txt", selected_tool=ToolChoice.UV,
              base_python="3.12").save(tmp_path / ".pymolt" / "env_config.json")
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    (vendor / "wdepx.py").write_text(WDEP_OLD, encoding="utf-8")
    baseline = _capture(tmp_path, "baseline")
    return tmp_path, baseline


def test_the_runtime_warning_reaches_the_baseline_facts(captured):
    from pymolt.verify.facts import load_facts

    root, baseline = captured
    fs = load_facts(root / baseline.trace_path if not baseline.trace_path.startswith("/")
                    else baseline.trace_path, root=root)
    (fact,) = [f for f in fs.facts if f.symbol == "wdepx.mean"]
    assert fact.warnings[0].category == "FutureWarning"
    assert fs.warnings_capture == "on"


def test_plan_stores_risks_and_verify_calibrates_them(captured):
    from pymolt.codemods.client import DependencyMigration
    from pymolt.migration_plan import MigrationPlan, create_migration_plan
    from pymolt.verify import service

    root, _baseline_slot = captured
    delta = _delta({"symbol": "wdepx.mean", "kind": "DEFAULT", "param": "numeric_only",
                    "old": "None", "new": "False", "deprecated_in_old": True},
                   package="wdepx")
    plan = create_migration_plan(
        root, [], [DependencyMigration("wdepx", "1", "2")], target_python="3.12",
        source_manifest="requirements.txt", target_manifest_path=None, files_scanned=0,
        delta_reports=[delta])
    plan.save(root)
    stored = json.loads((root / ".pymolt" / "migration_plan.json").read_text())["risks"]
    assert stored["schema"] == RISKS_SCHEMA
    (risk,) = stored["risks"]
    assert (risk["symbol"], risk["tier"], risk["site"]["func"]) == ("wdepx.mean", "high", "run")
    assert MigrationPlan.load(root).risks.risks[0].tier is RiskTier.HIGH

    (root / "vendor" / "wdepx.py").write_text(WDEP_NEW, encoding="utf-8")
    _capture(root, "post_migration")
    report = service.build_contract_report_from_state(root)
    cal = report.calibration
    assert cal.source == "plan" and cal.plan_id == plan.plan_id
    (scored,) = cal.risks
    assert scored.result is CalibrationResult.HIT
    assert report.model_dump(mode="json")["calibration"]["schema"] == CALIBRATION_SCHEMA


def test_verify_without_a_plan_checks_the_baselines_own_warnings(captured):
    from pymolt.verify import service

    root, _ = captured
    (root / "vendor" / "wdepx.py").write_text(WDEP_NEW, encoding="utf-8")
    _capture(root, "post_migration")
    cal = service.build_contract_report_from_state(root).calibration
    assert cal.source == "baseline"
    (scored,) = cal.risks
    assert scored.symbol == "wdepx.mean" and scored.result is CalibrationResult.HIT
    assert any("no plan predictions" in n for n in cal.notes)


def test_a_failed_prediction_never_costs_the_plan(captured, monkeypatch):
    from pymolt.migration_plan import create_migration_plan
    from pymolt.verify import predict

    def boom(*_a, **_k):
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(predict, "predict_plan_risks", boom)
    root, _ = captured
    plan = create_migration_plan(
        root, [], [], target_python="3.12", source_manifest="requirements.txt",
        target_manifest_path=None, files_scanned=0)
    assert plan.risks.status == "unavailable"
    assert "probe exploded" in plan.risks.notes[0]


def test_no_baseline_means_no_risks(tmp_path):
    from pymolt.migration_plan import create_migration_plan

    plan = create_migration_plan(
        tmp_path, [], [], target_python="3.12", source_manifest="requirements.txt",
        target_manifest_path=None, files_scanned=0)
    assert plan.risks is None


def test_predict_plan_risks_degrades_when_the_delta_engine_is_missing(captured, monkeypatch):
    import builtins

    from pymolt.codemods.client import DependencyMigration

    real_import = builtins.__import__

    def no_delta(name, *args, **kwargs):
        if name == "pymolt.delta" or name.startswith("pymolt.delta."):
            raise ImportError("not built yet")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_delta)
    root, baseline = captured
    report = predict_plan_risks(root, baseline.trace_path,
                                [DependencyMigration("wdepx", "1", "2")])
    assert report.status == "partial"
    assert any("delta engine unavailable" in n for n in report.notes)
    assert report.risks[0].tier is RiskTier.HIGH        # the warning still predicts it
