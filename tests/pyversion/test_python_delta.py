"""``python_delta``: the stdlib as a pseudo-migration, probed on the target, base from data."""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from pymolt.delta.engine import ProbeRun
from pymolt.pyversion import facts as pyfacts
from pymolt.pyversion.facts import classify_origin, python_delta

pytestmark = pytest.mark.skipif(sys.version_info < (3, 12),
                                reason="the target interpreter under test is this one (3.12+)")


def _site(spelling, file="app.py", line=1, kind="from", reached="import", guard=None):
    return SimpleNamespace(spelling=spelling, module=spelling.split(".")[0], file=file,
                           line=line, kind=kind, reached_at=reached, guard=guard)


def _delta(spellings, tmp_path, **kwargs):
    return python_delta(tmp_path, [_site(s) for s in spellings], "3.6", "3.12",
                        target_executable=sys.executable, **kwargs)


def _by_symbol(report):
    return {f.symbol: f for f in report.facts}


def test_a_dropped_alias_is_moved_with_its_successor(tmp_path):
    report = _delta(["collections.Iterable"], tmp_path)
    fact = _by_symbol(report)["collections.Iterable"]
    assert report.subject == "python" and report.package == "python"
    assert fact.kind == "MOVED" and fact.successor == "collections.abc.Iterable"
    assert fact.basis == {"old": "knowledge", "new": "probe"}
    assert fact.removed_in == "3.10" and fact.deprecated_in == "3.3"
    assert fact.warns_since == "3.7"


def test_a_removed_module_is_removed_not_moved(tmp_path):
    facts = _by_symbol(_delta(["imp", "imp.reload"], tmp_path))
    assert facts["imp"].kind == "REMOVED" and facts["imp.reload"].kind == "REMOVED"
    assert facts["imp"].removed_in == "3.12"
    assert any(e.startswith("hint:") for e in facts["imp"].evidence)


def test_a_removed_name_carries_the_curated_hint_and_fix(tmp_path):
    fact = _by_symbol(_delta(["ssl.wrap_socket"], tmp_path))["ssl.wrap_socket"]
    assert fact.kind == "REMOVED"
    assert "fix: advisory" in fact.evidence
    assert any(e.startswith("hint:") for e in fact.evidence)


def test_what_still_exists_has_no_fact(tmp_path):
    assert _delta(["os.path.join", "collections.abc.Iterable"], tmp_path).facts == []


def test_absent_on_the_base_too_is_listed_as_pre_existing(tmp_path):
    fact = _by_symbol(_delta(["urllib2"], tmp_path))["urllib2"]
    assert fact.kind == "UNCHANGED"
    assert any("pre-existing" in e for e in fact.evidence)


def test_the_base_is_not_probed_so_the_report_is_partial_and_says_so(tmp_path):
    report = _delta(["imp"], tmp_path)
    assert report.quality == "partial"
    assert report.sides["old"].startswith("knowledge:")
    assert report.sides["new"].startswith("probe:")
    assert any("base" in n and "not probed" in n for n in report.notes)


def test_no_target_interpreter_is_data_only_and_says_why(tmp_path, monkeypatch):
    monkeypatch.setattr(pyfacts, "find_target_interpreter", lambda *a, **k: None)
    report = python_delta(tmp_path, [_site("collections.Iterable")], "3.6", "3.12")
    fact = report.facts[0]
    assert fact.kind == "MOVED" and fact.basis == {"old": "knowledge", "new": "knowledge"}
    assert report.quality == "partial"
    assert any("no interpreter found" in n for n in report.notes)
    assert report.sides["new"].startswith("knowledge:")


def test_present_only_through_a_shim_is_deprecated_with_its_origin(tmp_path, monkeypatch):
    def probe(exe, root, paths, roots, *, runner=None):
        return ProbeRun(meta={"python": "3.12.1"}, results={
            p: {"exists": True, "module": p.split(".")[0], "steps": []} for p in paths})

    monkeypatch.setattr(pyfacts, "_probe", probe)
    monkeypatch.setattr(pyfacts, "_origins",
                        lambda *a, **k: {"distutils": "site-packages:setuptools"})
    fact = _by_symbol(_delta(["distutils"], tmp_path))["distutils"]
    assert fact.kind == "DEPRECATED" and fact.origin == "site-packages:setuptools"
    assert fact.removed_in == "3.12"


def test_a_module_that_raises_on_import_is_never_removed(tmp_path, monkeypatch):
    def probe(exe, root, paths, roots, *, runner=None):
        return ProbeRun(meta={"python": "3.12.1"}, results={
            p: {"exists": False, "module": None, "error": "ImportError: boom", "steps": []}
            for p in paths})

    monkeypatch.setattr(pyfacts, "_probe", probe)
    report = _delta(["imp"], tmp_path)
    assert report.facts == [] and report.import_errors


def test_a_failed_probe_degrades_to_data(tmp_path, monkeypatch):
    monkeypatch.setattr(pyfacts, "_probe",
                        lambda *a, **k: ProbeRun(failure="probe timed out after 1s"))
    report = _delta(["collections.Iterable"], tmp_path)
    assert report.facts[0].basis["new"] == "knowledge"
    assert any("probe failed" in n for n in report.notes)


def test_a_dependency_report_still_dumps_without_the_python_keys(tmp_path):
    dump = _delta(["imp"], tmp_path).model_dump(mode="json")
    assert dump["subject"] == "python" and dump["sides"]


def test_classify_origin():
    assert classify_origin("/x/lib/python3.12/collections/__init__.py") == "stdlib"
    assert classify_origin("/x/site-packages/setuptools/_distutils/__init__.py") == \
        "site-packages:setuptools"
    assert classify_origin(None) is None
