"""assess says so when the project's own build fails on the target Python (QA I3, I5).

Resolving ``setup.py`` runs the project's build backend under the target Python. theano's
``versioneer.py`` uses ``configparser.SafeConfigParser``, gone in 3.12: the resolve failed,
assess blamed "the target dependencies" with no culprit, wrote no target manifest, and its
next hint looped back to the same assess. Its numpy/scipy/six resolve on 3.12 fine.
"""

from __future__ import annotations

import json
import subprocess

from typer.testing import CliRunner

import pymolt.assess.service as service
from pymolt.assess.service import (
    TargetResolveError,
    describe_resolver_failure,
    own_build_failed,
    resolve_target_graph,
)
from pymolt.core.enums import Mode, Provenance, ResolutionQuality, SourceFixation
from pymolt.core.graph import DependencyGraph, Node
from pymolt.core.layers import IngestionReport
from pymolt.ingestion.detect import detect_sources

OWN_BUILD = """error: The build backend returned an error
  Caused by: Call to `setuptools.build_meta:__legacy__.build_wheel` failed (exit status: 1)

[stderr]
Traceback (most recent call last):
  File "/home/user/theano/versioneer.py", line 342, in get_config_from_root
    parser = configparser.SafeConfigParser()
AttributeError: module 'configparser' has no attribute 'SafeConfigParser'

hint: This usually indicates a problem with the package or the build environment.
"""
DEPENDENCY_BUILD = """  x Failed to build `theano==1.0.5`
  |-> The build backend returned an error
"""

SETUP_PY = """from setuptools import setup
setup(name="theano", install_requires=["numpy>=1.9.1", "scipy>=0.14", "six>=1.9.0"])
"""


def _failure(stderr):
    return subprocess.CalledProcessError(2, ["uv", "pip", "compile", "setup.py"], stderr=stderr)


def _graph(*names):
    nodes = {n: Node(name=n, version="1.0", mode=Mode.PYPI, provenance=Provenance.PYPI,
                     direct=True) for n in names}
    return DependencyGraph(nodes=nodes, resolution_quality=ResolutionQuality.RESOLVED,
                           source_fixation=SourceFixation.INTENT)


def _source(tmp_path, name="setup.py"):
    return next(s for s in detect_sources(tmp_path) if s.path.name == name)


def test_own_build_failure_is_told_apart_from_a_dependency_failure(tmp_path):
    (tmp_path / "setup.py").write_text(SETUP_PY)
    (tmp_path / "requirements.txt").write_text("numpy\n")
    own = describe_resolver_failure(_failure(OWN_BUILD))
    dep = describe_resolver_failure(_failure(DEPENDENCY_BUILD))

    assert own_build_failed(own, _source(tmp_path))
    assert not own_build_failed(dep, _source(tmp_path))                     # theano named
    assert not own_build_failed(own, _source(tmp_path, "requirements.txt"))  # no build ran


def test_the_static_read_is_resolved_instead_and_stamped(tmp_path, monkeypatch):
    (tmp_path / "setup.py").write_text(SETUP_PY)
    resolved: list[str] = []

    def orchestrate(project_dir, *, chosen_source=None, **_):
        if chosen_source.path.name == "setup.py":
            raise _failure(OWN_BUILD)
        resolved.append(chosen_source.path.read_text())
        return _graph("numpy", "scipy", "six"), None

    monkeypatch.setattr(service, "orchestrate_ingestion", orchestrate)
    notes: list[str] = []

    graph, error = resolve_target_graph(tmp_path, tmp_path, _source(tmp_path), "3.12", None,
                                        None, {}, notes=notes)

    assert error is None and sorted(graph.nodes) == ["numpy", "scipy", "six"]
    assert resolved == ["numpy>=1.9.1\nscipy>=0.14\nsix>=1.9.0\n"]
    assert len(notes) == 1
    assert "The project's own setup.py does not build on Python 3.12" in notes[0]
    assert "SafeConfigParser" in notes[0] and "static read" in notes[0]
    assert not (tmp_path / ".pymolt" / "cache" / "requirements-static.txt").exists()


def test_without_a_static_read_the_error_names_the_build_and_the_way_forward(
        tmp_path, monkeypatch):
    (tmp_path / "setup.py").write_text(
        "from setuptools import setup\nsetup(name='x', install_requires=open('r').read())\n")
    (tmp_path / "requirements-dev.txt").write_text("pytest\n")
    monkeypatch.setattr(service, "orchestrate_ingestion",
                        lambda *a, **k: (_ for _ in ()).throw(_failure(OWN_BUILD)))

    graph, error = resolve_target_graph(tmp_path, tmp_path, _source(tmp_path), "3.12", None,
                                        None, {}, notes=[])

    assert graph is None
    assert str(error).startswith("the project's own setup.py does not build on Python 3.12")
    assert "computed install_requires not read" in str(error)
    assert error.culprits == []
    assert "`pymolt setup . --manifest requirements-dev.txt`" in error.hint


def test_a_dependency_failure_stays_what_it_was(tmp_path, monkeypatch):
    (tmp_path / "setup.py").write_text(SETUP_PY)
    monkeypatch.setattr(service, "orchestrate_ingestion",
                        lambda *a, **k: (_ for _ in ()).throw(_failure(DEPENDENCY_BUILD)))
    notes: list[str] = []

    graph, error = resolve_target_graph(tmp_path, tmp_path, _source(tmp_path), "3.12", None,
                                        None, {}, notes=notes)

    assert graph is None and error.culprits == ["theano"] and error.hint is None
    assert notes == []


def _cli_project(tmp_path, monkeypatch, target):
    from pymolt.interfaces.cli import commands

    (tmp_path / "setup.py").write_text(SETUP_PY)
    report = IngestionReport(resolution_quality="resolved", source_fixation="intent",
                             manual_zone=[], warnings=[], detected_python="3.11")
    monkeypatch.setattr(commands, "_resolve_baseline_graph",
                        lambda **k: (_graph("numpy"), report, None))
    monkeypatch.setattr(commands, "_resolve_target_graph", target)
    monkeypatch.setattr(commands, "get_eol_versions", lambda: {})
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    return commands.app


def test_a_failed_target_ends_on_its_way_forward_not_the_same_assess(tmp_path, monkeypatch):
    hint = "Fix the project's build for Python 3.12, or point setup at a requirements file."
    app = _cli_project(tmp_path, monkeypatch, lambda *a, **k: (None, TargetResolveError(
        "the project's own setup.py does not build on Python 3.12", "tail", [], hint)))

    result = CliRunner().invoke(app, ["assess", str(tmp_path), "--target-python", "3.12"])

    assert "All package twins resolved" not in result.output
    assert f"next → {hint}" in " ".join(result.output.split())
    assert "next → pymolt assess" not in result.output


def test_agent_view_carries_the_basis_and_the_hint(tmp_path, monkeypatch):
    hint = "Fix the project's build for Python 3.12."
    app = _cli_project(tmp_path, monkeypatch, lambda *a, **k: (None, TargetResolveError(
        "the project's own setup.py does not build on Python 3.12", "tail", [], hint)))

    result = CliRunner().invoke(app, ["assess", str(tmp_path), "--target-python", "3.12",
                                      "--json"])
    payload = json.loads(result.stdout)

    assert payload["target_hint"] == hint and payload["target_basis"] == "manifest"

    def static(*a, notes=None, **k):
        notes.append("The project's own setup.py does not build on Python 3.12: static read.")
        return _graph("numpy"), None

    app = _cli_project(tmp_path, monkeypatch, static)
    result = CliRunner().invoke(app, ["assess", str(tmp_path), "--target-python", "3.12",
                                      "--agent", "--no-write"])
    view = json.loads(result.stdout)
    assert view["data"]["target_basis"] == "static-read"
