from datetime import datetime, timedelta, timezone

import pymolt.adapters.pypi_metadata as pypi_mod
import pymolt.risk.osv as osv_mod
from pymolt.core.enums import Mode, Provenance, ResolutionQuality, RiskTier, SourceFixation
from pymolt.core.graph import DependencyGraph, Node
from pymolt.risk.assess import assess_risk


def _node(name, version):
    return Node(name=name, version=version, mode=Mode.PYPI, provenance=Provenance.PYPI)


def _graph(nodes):
    return DependencyGraph(
        nodes={n.name: n for n in nodes}, edges=[], roots=[],
        resolution_quality=ResolutionQuality.RESOLVED, source_fixation=SourceFixation.PINNED,
    )


def _fake_osv(name, version, client):
    if name == "vulnlib":
        return [{
            "id": "CVE-2024-1", "severity": [{"score": "7.5"}],
            "affected": [{"ranges": [{"events": [{"fixed": "2.0"}]}]}], "summary": "bad",
        }]
    if name == "fixedlib" and version == "1.0":
        return [{"id": "CVE-2023-9", "affected": []}]
    return []


def _fake_release(name, version, client):
    if name == "clib":  # only an sdist -> must compile
        return {"urls": [{"packagetype": "sdist", "filename": "clib-1.0.tar.gz"}]}
    return {"urls": [{"packagetype": "bdist_wheel", "filename": f"{name}-{version}-py3-none-any.whl"}]}


def _fake_package(name, client):
    if name == "deadlib":
        old = (datetime.now(timezone.utc) - timedelta(days=365 * 4)).isoformat()
        return {"releases": {"1.0": [{"upload_time_iso_8601": old}]}}
    recent = datetime.now(timezone.utc).isoformat()
    return {"releases": {"1.0": [{"upload_time_iso_8601": recent}]}}


def test_assess_risk_scores_each_signal(tmp_path, monkeypatch):
    monkeypatch.setattr(osv_mod, "query_vulns", _fake_osv)
    monkeypatch.setattr(pypi_mod, "fetch_release_json", _fake_release)
    monkeypatch.setattr(pypi_mod, "fetch_package_json", _fake_package)

    baseline = _graph([
        _node("vulnlib", "1.0"), _node("clib", "1.0"), _node("deadlib", "1.0"),
        _node("goodlib", "1.0"), _node("fixedlib", "1.0"),
    ])
    target = _graph([
        _node("vulnlib", "1.0"), _node("clib", "1.0"), _node("deadlib", "1.0"),
        _node("goodlib", "1.0"), _node("fixedlib", "2.0"),
    ])

    report = assess_risk(baseline, target, "3.12", tmp_path)
    by_name = {p.name: p for p in report.packages}

    assert report.assessed == 5
    assert report.errors == 0

    assert by_name["vulnlib"].tier == RiskTier.HIGH
    assert by_name["vulnlib"].open_cves[0].id == "CVE-2024-1"

    assert by_name["clib"].tier == RiskTier.MEDIUM
    assert by_name["clib"].needs_compilation is True
    assert by_name["clib"].wheel_status == "sdist-only"

    assert by_name["deadlib"].tier == RiskTier.MEDIUM
    assert by_name["deadlib"].abandoned is True

    assert by_name["goodlib"].tier == RiskTier.LOW

    # CVE present on the old pin, gone on the target version -> cleared by migration.
    assert by_name["fixedlib"].open_cves == []
    assert "CVE-2023-9" in by_name["fixedlib"].fixed_by_migration


def test_assess_risk_handles_network_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(osv_mod, "query_vulns", lambda *a, **k: None)
    monkeypatch.setattr(pypi_mod, "fetch_release_json", lambda *a, **k: None)
    monkeypatch.setattr(pypi_mod, "fetch_package_json", lambda *a, **k: None)

    g = _graph([_node("anything", "1.0")])
    report = assess_risk(g, g, "3.12", tmp_path)
    assert report.errors == 1
    assert report.packages[0].tier == RiskTier.LOW  # nothing known -> not penalised


def test_assess_risk_skips_unpinned(tmp_path, monkeypatch):
    monkeypatch.setattr(osv_mod, "query_vulns", _fake_osv)
    monkeypatch.setattr(pypi_mod, "fetch_release_json", _fake_release)
    monkeypatch.setattr(pypi_mod, "fetch_package_json", _fake_package)

    g = _graph([_node("nopin", None)])
    report = assess_risk(g, g, "3.12", tmp_path)
    assert report.skipped == 1
    assert "unpinned" in report.packages[0].reasons[0]
