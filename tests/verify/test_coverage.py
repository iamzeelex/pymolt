"""The coverage run orchestration (no real coverage run)."""
from pymolt.verify.coverage import run_coverage_json


def test_run_coverage_json_orchestrates_via_injected_runner(tmp_path):
    # Fake runner: no real suite; we drop a coverage.json the orchestration then reads.
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

    data = run_coverage_json(str(tmp_path), pytest_args=["-q"],
                             json_out="coverage.json", runner=fake_runner)
    assert "/x/site-packages/flask/app.py" in data["files"]
    assert ["coverage", "run", "-m", "pytest", "-q"] in calls
