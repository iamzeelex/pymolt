"""The plan's python part: risks with no baseline, the summary line, the view data."""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from pymolt.interfaces.views import plan_view, python_summary_line
from pymolt.migration_plan import MigrationPlan, create_migration_plan
from pymolt.pyversion import plan as pyplan
from pymolt.pyversion.plan import same_python_minor

pytestmark = pytest.mark.skipif(sys.version_info < (3, 12), reason="targets this interpreter")


def _project(tmp_path):
    (tmp_path / "app.py").write_text(
        "import collections\nfrom collections import Iterable\nimport imp\n"
        "print(collections.Mapping, imp.reload)\n")
    (tmp_path / "py2.py").write_text("print 'x'\n")
    return tmp_path


def _sites():
    def site(spelling, line, kind="from", reached="import"):
        return SimpleNamespace(spelling=spelling, module=spelling.split(".")[0],
                               file="app.py", line=line, kind=kind, reached_at=reached,
                               guard=None)
    return [site("collections.Iterable", 2), site("imp", 3, "import"),
            site("collections.Mapping", 4, "attr", "call")]


def test_same_python_minor():
    assert same_python_minor("3.12.12", "3.12") and not same_python_minor("3.11", "3.12")
    assert not same_python_minor(None, "3.12")


def test_plan_python_rates_sites_and_reports_the_move(tmp_path, monkeypatch):
    monkeypatch.setattr("pymolt.pyversion.facts.find_target_interpreter",
                        lambda *a, **k: sys.executable)
    python, risks, findings = pyplan.plan_python(_project(tmp_path), "3.6", "3.12", _sites())
    assert python["from"] == "3.6" and python["to"] == "3.12"
    assert python["quality"] == "partial" and python["sides"]["new"].startswith("probe:")
    assert {r.symbol: r.tier.value for r in risks.risks if r.scope == "python"} == {
        "collections.Iterable": "certain", "imp": "certain", "collections.Mapping": "certain"}
    assert [f["file"] for f in findings if f["kind"] == "SyntaxError"] == ["py2.py"]


def test_python_risks_are_predicted_with_no_baseline(tmp_path, monkeypatch):
    monkeypatch.setattr("pymolt.pyversion.facts.find_target_interpreter",
                        lambda *a, **k: sys.executable)
    monkeypatch.setattr("pymolt.verify.contact_map.build_contact_map",
                        lambda root, *a, **k: SimpleNamespace(stdlib_sites=_sites()))
    root = _project(tmp_path)
    plan = create_migration_plan(root, [], [], target_python="3.12", source_manifest="r.txt",
                                 target_manifest_path=None, files_scanned=0,
                                 python_move=("3.6", "3.12"))
    assert plan.baseline_trace_path is None and plan.risks is not None
    assert plan.python["to"] == "3.12" and plan.compile_findings
    assert all(r.scope == "python" and not r.fixed_by_plan for r in plan.risks.risks)
    assert plan.risks.status == "partial"
    # it round-trips through the saved plan
    plan.save(root)
    loaded = MigrationPlan.load(root)
    assert loaded.python == plan.python and len(loaded.risks.risks) == len(plan.risks.risks)


def test_no_python_move_leaves_the_plan_unchanged(tmp_path):
    plan = create_migration_plan(_project(tmp_path), [], [], target_python="3.12",
                                 source_manifest="r.txt", target_manifest_path=None,
                                 files_scanned=0)
    assert plan.risks is None and plan.python is None and plan.compile_findings == []


def test_a_failed_python_part_never_costs_the_plan(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("no")

    monkeypatch.setattr(pyplan, "plan_python", boom)
    plan = create_migration_plan(_project(tmp_path), [], [], target_python="3.12",
                                 source_manifest="r.txt", target_manifest_path=None,
                                 files_scanned=0, python_move=("3.6", "3.12"))
    assert plan.python["quality"] == "inconclusive"
    assert any("python risk prediction failed" in n for n in plan.risks.notes)


def test_the_summary_line_and_view_data(tmp_path, monkeypatch):
    monkeypatch.setattr("pymolt.pyversion.facts.find_target_interpreter",
                        lambda *a, **k: sys.executable)
    monkeypatch.setattr("pymolt.verify.contact_map.build_contact_map",
                        lambda root, *a, **k: SimpleNamespace(stdlib_sites=_sites()))
    plan = create_migration_plan(_project(tmp_path), [], [], target_python="3.12",
                                 source_manifest="r.txt", target_manifest_path=None,
                                 files_scanned=0, python_move=("3.6", "3.12"))
    line = python_summary_line(plan)
    assert line.startswith("Python 3.6→3.12: 3 risk(s) (3 certain); 0 fixed by the plan; "
                           "needs attention: ")
    assert "imp (manual)" in line and "evidence partial" in line
    view = plan_view(plan, {}, "test", plan_path=tmp_path / "plan.json")
    data = view["data"]
    assert data["python"]["risks_total"] == 3 and data["python"]["quality"] == "partial"
    assert data["compile_findings_total"] == len(plan.compile_findings) >= 1
    assert data["baseline_bound"] is False
    # no baseline: the dependency risk line is not printed, so no "0 need attention"
    assert "need attention," not in view["summary"] and data["risks_status"] is None
    assert "Python 3.6→3.12" in view["summary"]


def test_a_zero_risk_move_does_not_claim_all_clear_while_evidence_is_partial():
    plan = SimpleNamespace(python={"from": "3.11", "to": "3.12", "quality": "partial"},
                           risks=None, compile_findings=[])
    assert python_summary_line(plan) == (
        "Python 3.11→3.12: 0 risk(s) (0 certain); 0 fixed by the plan; none rated, the "
        "evidence is incomplete (evidence partial: see data.python.notes)")
    assert python_summary_line(SimpleNamespace(python=None)) is None
