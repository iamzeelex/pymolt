"""LocalDeltaProvider: delta facts → CodemodBundle → the existing plan/apply path."""

from __future__ import annotations

from pathlib import Path

import pytest
from _apply import apply_migration_plan

from pymolt.codemods import service as codemod_service
from pymolt.codemods.models import DependencyMigration
from pymolt.codemods.rules import verify_rule
from pymolt.delta import provider as provider_mod
from pymolt.delta.models import DeltaFact, DeltaReport
from pymolt.delta.provider import LocalDeltaProvider, as_legacy_rule
from pymolt.ingestion.config import EnvConfig, ToolChoice

PANDAS = DependencyMigration("pandas", "1.5.3", "2.2.3")

CORE = '''\
import pandas as pd
from pandas.io.json import json_normalize


def combine(a, b):
    s1 = pd.Series(a)
    s2 = pd.Series(b)
    return s1.append(s2, ignore_index=True)


def flat(rows):
    return json_normalize(rows)


def keep(xs):
    xs.append(1)
    return xs
'''


def _report(quality="complete") -> DeltaReport:
    removed = DeltaFact(
        symbol="pandas.core.series.Series.append", aliases=[], kind="REMOVED",
        old="pandas.core.series.Series.append", successor="pandas.concat",
        deprecated_in_old=True, evidence=["existence", "successor:warning"],
    )
    moved = DeltaFact(
        symbol="pandas.io.json.json_normalize", kind="MOVED",
        old="pandas.io.json._normalize.json_normalize",
        new="pandas.io.json._normalize.json_normalize", successor="pandas.json_normalize",
        evidence=["existence", "same object reachable as pandas.json_normalize"],
    )
    moved.detail.update(importable=["pandas.io.json.json_normalize"],
                        successor_importable=True, successor_same_signature=True)
    default = DeltaFact(
        symbol="pandas.core.frame.DataFrame.mean", aliases=["pandas.DataFrame.mean"],
        kind="DEFAULT", param="numeric_only", old="None", new="False", deprecated_in_old=True,
        evidence=["signature"],
    )
    unchanged = DeltaFact(symbol="pandas.core.series.Series.__len__", kind="UNCHANGED")
    facts = [removed, moved, default, unchanged] if quality != "inconclusive" else []
    return DeltaReport(package="pandas", from_version="1.5.3", to_version="2.2.3",
                       quality=quality, facts=facts,
                       notes=["no new interpreter: uv is not on PATH"]
                       if quality == "inconclusive" else [])


@pytest.fixture
def fake_deltas(monkeypatch):
    holder = {"report": _report(), "calls": []}

    def fake(project_dir, migrations, *, symbols=None, online=False, use_cache=True):
        holder["calls"].append({"online": online, "migrations": list(migrations)})
        return [holder["report"]]

    monkeypatch.setattr(provider_mod, "compute_deltas", fake)
    return holder


def test_bundle_maps_facts_to_rules_patterns_and_impacts(tmp_path, fake_deltas):
    (tmp_path / "core.py").write_text(CORE, encoding="utf-8")
    bundles = LocalDeltaProvider(tmp_path).fetch_bundle([PANDAS])
    bundle = bundles["pandas"]

    # impacts: every non-UNCHANGED fact
    assert {(i.kind, i.path) for i in bundle.impacts} == {
        ("REMOVED", "pandas.core.series.Series.append"),
        ("MOVED", "pandas.io.json.json_normalize"),
        ("DEFAULT", "pandas.core.frame.DataFrame.mean"),
    }
    moved = next(i for i in bundle.impacts if i.kind == "MOVED")
    assert moved.replacement_path == "pandas.json_normalize"
    assert moved.transition_confidence == "same-object"

    # the curated append → concat rule (knowledge), verified locally
    append = next(r for r in bundle.rules if r.kind is None)
    assert append.match == "$S.append($OTHER, ignore_index=$IGN)"
    assert append.confidence == "verified" and verify_rule(append)
    assert any("pymolt knowledge pandas-append-to-concat" in e for e in append.evidence)
    # the move travels as a LEGACY rule (a package with rules is applied rules-only)
    legacy = next(r for r in bundle.rules if r.kind == "rewrite-import")
    assert (legacy.old_qualname, legacy.new_qualname) == ("pandas.io.json.json_normalize",
                                                          "pandas.json_normalize")
    assert legacy.confidence == "verified"
    assert bundle.patterns == [] and bundle.downgraded == []


def test_without_rules_a_move_is_a_plain_pattern(tmp_path, fake_deltas):
    report = _report()
    # The delta probed append and found it still there: no rule.
    report.facts = [f for f in report.facts if f.kind != "REMOVED"] + [DeltaFact(
        symbol="pandas.core.series.Series.append", kind="UNCHANGED")]
    fake_deltas["report"] = report
    bundle = LocalDeltaProvider(tmp_path).fetch_bundle([PANDAS])["pandas"]
    assert bundle.rules == []
    (pattern,) = bundle.patterns
    assert (pattern.kind, pattern.old_qualname, pattern.new_qualname, pattern.confidence) == (
        "rewrite-import", "pandas.io.json.json_normalize", "pandas.json_normalize", "high")


def test_a_complete_delta_silent_about_the_rules_symbols_keeps_the_rule(tmp_path, fake_deltas):
    """zipline (legacy QA P3): ``df = df.append(...)`` on a local in a test the baseline never
    ran. The delta never probed ``DataFrame.append``; its silence rules nothing out."""
    report = _report()
    report.facts = [f for f in report.facts if f.kind != "REMOVED"]
    fake_deltas["report"] = report
    bundle = LocalDeltaProvider(tmp_path).fetch_bundle([PANDAS])["pandas"]
    assert "$S.append($OTHER, ignore_index=$IGN)" in [r.match for r in bundle.rules]


def test_inconclusive_delta_is_an_impact_never_silence(tmp_path, fake_deltas):
    fake_deltas["report"] = _report("inconclusive")
    bundle = LocalDeltaProvider(tmp_path).fetch_bundle([PANDAS])["pandas"]
    (impact,) = bundle.impacts
    assert impact.kind == "INCONCLUSIVE" and "uv is not on PATH" in impact.explanation
    # the curated rule still applies to the window: the engine could not rule it out
    assert [r.match for r in bundle.rules] == ["$S.append($OTHER, ignore_index=$IGN)"]


def test_rule_needing_an_unbound_alias_is_downgraded(tmp_path, fake_deltas):
    (tmp_path / "core.py").write_text(CORE.replace("import pandas as pd", "import pandas"),
                                      encoding="utf-8")
    bundle = LocalDeltaProvider(tmp_path).fetch_bundle([PANDAS])["pandas"]
    append = next(r for r in bundle.rules if r.kind is None)
    assert append.confidence == "heuristic"
    assert any("import pandas as pd" in d and "core.py" in d for d in bundle.downgraded)


def test_legacy_rule_for_a_rename_verifies():
    from pymolt.codemods.models import CodemodPattern

    rule = as_legacy_rule(CodemodPattern(old_qualname="pkg.old_func",
                                         new_qualname="pkg.new_func", kind="rename-call"),
                          _report())
    assert rule.confidence == "verified"


def _project(root: Path) -> None:
    (root / "core.py").write_text(CORE, encoding="utf-8")
    (root / "requirements.txt").write_text("pandas==1.5.3\n", encoding="utf-8")
    (root / "requirements-target.txt").write_text("pandas==2.2.3\n", encoding="utf-8")
    EnvConfig(selected_manifest="requirements.txt", selected_tool=ToolChoice.UV,
              base_python="3.11", target_python="3.12").save(
        root / ".pymolt" / "env_config.json")


def test_plan_then_apply_writes_the_expected_rewrite(tmp_path, fake_deltas, monkeypatch):
    from pymolt.migration_plan import MigrationPlan

    _project(tmp_path)
    monkeypatch.setattr(codemod_service, "resolve_codemod_migrations",
                        lambda *a, **k: ([PANDAS], "test migrations"))

    plan, previews, _source, by_pkg = codemod_service.build_migration_plan(tmp_path)

    assert fake_deltas["calls"] and fake_deltas["calls"][0]["online"] is False
    assert [c.path for c in plan.changes] == ["core.py"]
    assert {i.kind for i in plan.impacts} == {"REMOVED", "MOVED", "DEFAULT"}
    assert (tmp_path / "core.py").read_text() == CORE  # planning writes nothing
    plan.save(tmp_path)

    apply_migration_plan(tmp_path, MigrationPlan.load(tmp_path), allow_no_baseline=True)

    after = (tmp_path / "core.py").read_text()
    assert "return pd.concat([s1, s2], ignore_index=True)" in after
    assert "from pandas import json_normalize" in after
    assert "xs.append(1)" in after  # list.append is not a pandas call: untouched


def test_the_gate_takes_a_wrapper_record_only_from_the_members_own_wrapper(tmp_path):
    """A polars call recorded under ``deprecate_renamed_parameter``'s generic wrapper lends its
    receiver to no other call on the line (pandas ``pdf.groupby``)."""
    from types import SimpleNamespace

    from pymolt.delta.provider import BaselineSites

    (tmp_path / "app").mkdir()
    src = "def f(pdf, pl_df):\n    return pdf.groupby('k').sum(), pl_df.shift(periods=1)\n"
    (tmp_path / "app" / "x.py").write_text(src)
    wrapper = ("polars.utils.deprecation.deprecate_renamed_parameter.<locals>.decorate."
               "<locals>.wrapper")
    sites = BaselineSites(tmp_path)
    sites.facts = [SimpleNamespace(
        symbol=wrapper, text=src.splitlines()[1], file="app/x.py", line=2, lines=[2],
        input={"bound": {}, "varargs": {"__tuple__": [
            {"__object__": "polars.dataframe.frame.DataFrame"}]}})]
    old = {"polars.dataframe.frame.DataFrame.groupby", "polars.DataFrame.groupby"}
    groupby_wrapper = ["polars.utils.deprecation.deprecate_function.<locals>.decorate."
                       "<locals>.wrapper"]
    assert sites.gate(old, "groupby", "group_by", (), groupby_wrapper)[0] == []
    assert sites.gate(old, "shift", "shift", (), [wrapper])[0]       # its own wrapper: a site
