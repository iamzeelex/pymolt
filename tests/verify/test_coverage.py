"""Coverage map parsing + blind-spot detection (pure logic; no real coverage run)."""
from pymolt.verify.coverage import (
    CoverageMap,
    blind_spots,
    build_coverage_map,
    parse_coverage_json,
)
from pymolt.verify.models import NodeRef


def test_parse_counts_only_files_with_executed_lines():
    data = {"files": {
        "/x/site-packages/flask/views.py": {"summary": {"covered_lines": 5}},
        "/x/site-packages/flask/__init__.py": {"summary": {"covered_lines": 2}},
        # imported but never run -> not exercised:
        "/x/site-packages/marshmallow/fields.py": {"summary": {"covered_lines": 0}},
    }}
    prefixes = parse_coverage_json(data)
    assert "flask.views" in prefixes
    assert "flask" in prefixes                       # __init__.py -> package name
    assert not any(p.startswith("marshmallow") for p in prefixes)


def test_coverage_map_is_exercised_matches_prefix_and_submodules():
    cov = CoverageMap(exercised_prefixes={"flask.views", "flask"})
    assert cov.is_exercised("flask") is True
    assert cov.is_exercised("flask.views") is True
    assert cov.is_exercised("marshmallow") is False


def test_blind_spots_are_unexercised_nodes():
    cov = CoverageMap(exercised_prefixes={"flask"})
    flask = NodeRef(name="flask", old_version="1", new_version="2", trace_prefix="flask")
    marsh = NodeRef(name="marshmallow", old_version="1", new_version="2",
                    trace_prefix="marshmallow")
    assert blind_spots(cov, [flask, marsh]) == [marsh]


def test_build_coverage_map_orchestrates_via_injected_runner(tmp_path):
    # Fake runner: no real suite; we drop a coverage.json the orchestration then parses.
    import json

    cov_json = tmp_path / "coverage.json"
    calls = []

    def fake_runner(args, cwd=None, check=True, env=None, timeout=None):
        calls.append(args)
        if args[:2] == ["coverage", "json"]:
            cov_json.write_text(json.dumps({"files": {
                "/x/site-packages/flask/app.py": {"summary": {"covered_lines": 3}},
            }}))

        class CP:  # minimal CompletedProcess stand-in
            returncode = 0
        return CP()

    cov = build_coverage_map(str(tmp_path), pytest_args=["-q"],
                             json_out="coverage.json", runner=fake_runner)
    assert cov.is_exercised("flask") is True
    assert ["coverage", "run", "-m", "pytest", "-q"] in calls
