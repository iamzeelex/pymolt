"""Which dependency modules the project's test suite ran.

Run the suite under ``coverage`` and return coverage.py's JSON report. ``run_coverage_json``
shells out behind ``adapters/subprocess_runner`` (it needs the target environment);
``verify/service.py`` reads the report for the coverage gap summary of a test-suite capture.
"""
from collections.abc import Callable, Iterable
from typing import Any

from pymolt.adapters.subprocess_runner import run_command


def run_coverage_json(
    project_dir: str,
    pytest_args: Iterable[str] = (),
    json_out: str = "coverage.json",
    runner: Callable[..., Any] = run_command,
) -> dict[str, Any]:
    """Run the suite under coverage in ``project_dir`` and return the raw coverage.py JSON
    report (the ``{"files": {...}}`` shape).

    System-touching, so it goes through the subprocess adapter and is exercised inside the
    devcontainer (not the host unit suite). ``runner`` is injectable for testing the
    orchestration without a real suite.
    """
    args = list(pytest_args)
    runner(["coverage", "run", "-m", "pytest", *args], cwd=project_dir, check=False)
    runner(["coverage", "json", "-o", json_out], cwd=project_dir, check=False)
    import json
    import os

    path = json_out if os.path.isabs(json_out) else os.path.join(project_dir, json_out)
    with open(path) as f:
        return json.load(f)
