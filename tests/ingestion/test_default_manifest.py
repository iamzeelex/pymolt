"""One default manifest for every command, and readable base-resolve failures (QA I4).

scan's ``chosen_manifest`` took the alphabetically first name, setup its own ranking, and
assess without setup the first one discovered: on flasgger they picked
requirements-dev.txt, requirements.txt and requirements-dev.txt; on blaze setup picked
requirements-strict.txt and assess requirements-rtd.txt (whose ``git+git://`` line then
failed as a bare ``Command [...] returned non-zero exit status 2``).
"""

from __future__ import annotations

import json
import subprocess

from typer.testing import CliRunner

from pymolt.assess.service import resolve_chosen_source
from pymolt.ingestion.detect import detect_sources, pick_default_source
from pymolt.ingestion.orchestrator import orchestrate_ingestion

FLASGGER = ("requirements-dev.txt", "requirements.txt", "setup.py")
BLAZE = ("requirements-310.txt", "requirements-39.txt", "requirements-rtd.txt",
         "requirements-strict.txt", "setup.py")


def _project(tmp_path, names):
    for name in names:
        text = ("from setuptools import setup\nsetup(install_requires=['six'])\n"
                if name == "setup.py" else "six==1.16.0\n")
        (tmp_path / name).write_text(text)
    (tmp_path / "setup.cfg").write_text("[flake8]\nmax-line-length = 100\n")
    return tmp_path


def _cli():
    from pymolt.interfaces.cli.commands import app

    return CliRunner(), app


def test_assess_without_setup_assumes_what_setup_assumes(tmp_path):
    from pymolt.interfaces.cli.commands import _load_or_autodetect_assess_config
    from pymolt.setup.service import gather_setup_options

    for names, expected in ((FLASGGER, "requirements.txt"), (BLAZE, "requirements-strict.txt")):
        root = tmp_path / expected.replace(".", "-")
        root.mkdir()
        _project(root, names)
        sources = detect_sources(root, include_subroots=True)

        setup_default = next(m.name for m in gather_setup_options(root).manifests
                             if m.is_default)
        assert setup_default == expected
        assert resolve_chosen_source(sources, None).path.name == expected      # assess, plan
        assert _load_or_autodetect_assess_config(root, quiet=True)[
            "selected_manifest"] == expected                                    # CLI assess


def test_scan_chooses_the_same_manifest(tmp_path):
    runner, app = _cli()
    _project(tmp_path, BLAZE)

    result = runner.invoke(app, ["scan", str(tmp_path), "--agent"])

    roots = json.loads(result.stdout)["data"]["roots"]
    assert roots[0]["chosen_manifest"] == "requirements-strict.txt"


def test_ingestion_without_a_choice_takes_the_same_default(tmp_path, monkeypatch):
    from pymolt.ingestion import uv_runner

    _project(tmp_path, FLASGGER)
    seen = []

    def ingest(source, **_):
        seen.append(source.path.name)
        return uv_runner.graph_from_declared_requirements(source.path.name, {})

    monkeypatch.setattr(uv_runner, "ingest", ingest)
    orchestrate_ingestion(tmp_path, base_python="3.11", use_cache=False)
    assert seen == ["requirements.txt"]


def test_a_setup_cfg_without_dependencies_is_not_offered(tmp_path):
    _project(tmp_path, ("requirements.txt",))
    assert [s.path.name for s in detect_sources(tmp_path)] == ["requirements.txt"]

    (tmp_path / "setup.cfg").write_text("[options]\ninstall_requires =\n    six\n")
    assert "setup.cfg" in [s.path.name for s in detect_sources(tmp_path)]
    (tmp_path / "setup.cfg").write_text("[options]\ninstall_requires = file: reqs.txt\n")
    assert "setup.cfg" in [s.path.name for s in detect_sources(tmp_path)]


def test_a_base_resolve_failure_carries_the_resolver_output_and_a_hint(tmp_path, monkeypatch):
    from pymolt.interfaces.cli import commands

    _project(tmp_path, BLAZE)
    stderr = ("Updating git://github.com/blaze/odo (HEAD)\n"
              "error: Git operation failed\n"
              "  Caused by: failed to fetch from git+git://github.com/blaze/odo.git\n")

    def boom(**_):
        raise subprocess.CalledProcessError(
            2, ["uv", "pip", "compile", "requirements-strict.txt"], stderr=stderr)

    monkeypatch.setattr(commands, "_resolve_baseline_graph", boom)
    monkeypatch.setattr(commands, "get_eol_versions", lambda: {})
    runner, app = _cli()

    result = runner.invoke(app, ["assess", str(tmp_path), "--target-python", "3.12", "--json"])

    payload = json.loads(result.stdout)
    assert result.exit_code == 3 and payload["ok"] is False
    assert payload["error"] == ("the base manifest requirements-strict.txt does not resolve: "
                                "Git operation failed")
    assert "returned non-zero exit status" not in payload["error"]
    assert "failed to fetch from git+git://" in payload["detail"]
    assert payload["culprits"] == ["odo"]
    assert payload["hint"].startswith("Culprit(s): odo. Fix requirements-strict.txt, or "
                                      "assess another manifest: `pymolt setup . --manifest ")


def test_the_service_raises_the_same_failure(tmp_path, monkeypatch):
    import pytest

    import pymolt.assess.service as service

    _project(tmp_path, FLASGGER)

    def boom(*a, **k):
        raise subprocess.CalledProcessError(2, ["uv"], stderr="error: No solution found\n")

    monkeypatch.setattr(service, "orchestrate_ingestion", boom)
    with pytest.raises(service.BaseResolveError) as caught:
        service.run_assess(tmp_path, target_python="3.12", config={}, base_python="3.11")
    assert str(caught.value) == ("the base manifest requirements.txt does not resolve: "
                                 "No solution found")
    assert "assess another manifest" in caught.value.hint


def test_pick_default_source_of_nothing_is_none():
    assert pick_default_source([]) is None
