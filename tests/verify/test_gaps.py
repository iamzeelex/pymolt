"""Static dependency-usage scan + coverage overlay (blind-spot map). Pure, no real suite."""
from pymolt.verify.gaps import classify_gaps, scan_usage_sites

_CORE = '''\
import flask
from flask.views import MethodView
import os  # stdlib -> not a dependency

class View(MethodView):          # subclass usage (line 5)
    pass

def make():
    app = flask.Flask(__name__)  # call usage (line 9)
    return app
'''


def _project(tmp_path):
    pkg = tmp_path / "myapp"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "core.py").write_text(_CORE)
    # a test file that must be excluded from the scan
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_core.py").write_text("import flask\n")
    return tmp_path


def test_scan_finds_dependency_sites_and_skips_stdlib_and_local(tmp_path):
    sites = scan_usage_sites(str(_project(tmp_path)))
    deps = {s.dependency for s in sites}
    assert deps == {"flask"}                       # os (stdlib) and myapp (local) excluded
    kinds = {(s.kind, s.symbol) for s in sites}
    assert ("import", "flask") in kinds
    assert ("import", "flask.views.MethodView") in kinds
    assert ("subclass", "flask.views.MethodView") in kinds
    assert ("call", "flask.Flask") in kinds


def test_scan_excludes_tests_dir(tmp_path):
    sites = scan_usage_sites(str(_project(tmp_path)))
    assert all("test" not in s.rel_path for s in sites)


def test_classify_marks_covered_and_blind_by_line(tmp_path):
    proj = _project(tmp_path)
    sites = scan_usage_sites(str(proj))
    # coverage report (paths under a different root) where only line 9 of core.py executed
    coverage = {"files": {"/run/myapp/core.py": {"executed_lines": [9]}}}
    report = classify_gaps(sites, coverage, cov_root="/run")
    by_line = {(s.line, s.kind): s.covered for s in report.sites}
    assert by_line[(9, "call")] is True           # executed line -> covered
    assert by_line[(5, "subclass")] is False       # in file, not executed -> blind
    assert report.covered >= 1 and report.blind >= 1


def test_classify_file_absent_from_report_is_blind(tmp_path):
    sites = scan_usage_sites(str(_project(tmp_path)))
    # non-empty report that does NOT mention core.py => that file never ran => blind
    report = classify_gaps(sites, {"files": {"/run/other.py": {"executed_lines": [1]}}},
                           cov_root="/run")
    assert report.blind == len(sites) and report.covered == 0


def test_classify_empty_report_is_unknown_not_guessed(tmp_path):
    sites = scan_usage_sites(str(_project(tmp_path)))
    report = classify_gaps(sites, {"files": {}})
    assert report.unknown == len(sites)            # no coverage data -> honest "unknown"
    assert report.blind == 0 and report.covered == 0


def test_by_dependency_summary(tmp_path):
    sites = scan_usage_sites(str(_project(tmp_path)))
    report = classify_gaps(sites, {"files": {"/run/myapp/core.py": {"executed_lines": [9]}}},
                           cov_root="/run")
    summary = report.by_dependency()
    assert "flask" in summary
    assert summary["flask"]["covered"] + summary["flask"]["blind"] == len(sites)
