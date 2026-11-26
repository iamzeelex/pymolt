"""A base version pymolt did not observe is unknown, never a floor or ``0.0.0`` (QA I2).

With no legacy interpreter or container, the base graph is read from the manifest's
declarations. ``flask>=0.10`` says what may be installed, not what is: reading 0.10 as the
installed version made assess show ``0.10 → 3.1.3``, the plan fetch flask 0.10's 2013 sdist
as the old side, and ``--risk`` claim CVEs "cleared" from a release the app never ran.
"""

from __future__ import annotations

import pymolt.codemods.service as codemod_service
import pymolt.delta.provider as provider_mod
from pymolt.assess.service import build_comparison_rows
from pymolt.codemods.models import DependencyMigration
from pymolt.codemods.service import migrations_from_rows
from pymolt.core.enums import Mode, Provenance, ResolutionQuality, SourceFixation
from pymolt.core.graph import DependencyGraph, Node
from pymolt.ingestion import uv_runner
from pymolt.ingestion.config import EnvConfig, ToolChoice
from pymolt.ingestion.detect import pinned_version
from pymolt.ingestion.orchestrator import orchestrate_ingestion

FLASGGER = "Flask>=0.10\nPyYAML>=3.0\nmistune\nsix==1.10.0\nwerkzeug~=0.15\n"


def test_only_an_exact_pin_is_a_version():
    assert pinned_version("six==1.10.0") == "1.10.0"
    assert pinned_version("six===1.10.0") == "1.10.0"
    for spec in ("flask>=0.10", "werkzeug~=0.15", "mistune", "six==1.*",
                 "flask>=0.10,<2", "python=3.12", "not a requirement !!"):
        assert pinned_version(spec) is None, spec


def test_a_declared_only_base_has_no_invented_versions():
    graph = uv_runner.graph_from_declared_requirements(
        "requirements.txt",
        {"flask": "Flask>=0.10", "mistune": "mistune", "six": "six==1.10.0",
         "werkzeug": "werkzeug~=0.15"})

    assert graph.resolution_quality == ResolutionQuality.DECLARED_ONLY
    versions = {name: node.version for name, node in graph.nodes.items()}
    assert versions == {"flask": None, "mistune": None, "six": "1.10.0", "werkzeug": None}
    assert graph.nodes["flask"].declared_requirement == "Flask>=0.10"   # the constraint stays


def test_no_legacy_interpreter_says_which_versions_are_unknown(tmp_path, monkeypatch):
    """The 3.6 compile cannot run here: the graph is declared-only, and the warning says so
    after the fact, instead of announcing a pip-compile workflow that never ran."""
    from pymolt.ingestion import fallback_compiler

    def no_interpreter(*a, **k):
        raise ValueError("Python 3.6 interpreter was not found")

    monkeypatch.setattr(fallback_compiler, "compile_legacy", no_interpreter)
    (tmp_path / "requirements.txt").write_text(FLASGGER)

    graph, report = orchestrate_ingestion(tmp_path, base_python="3.6", use_cache=False)

    assert report.resolution_quality == "declared-only"
    assert graph.nodes["flask"].version is None and graph.nodes["six"].version == "1.10.0"
    text = " ".join(report.warnings)
    assert "4 base version(s) are unknown" in text
    assert "settles them" in text
    assert "pip-compile fallback workflow" not in text


def test_conda_declarations_are_not_versions(tmp_path, monkeypatch):
    import pymolt.ingestion.conda_ingest as conda_ingest

    monkeypatch.setattr(conda_ingest, "find_conda_executable_on_host", lambda: None)
    (tmp_path / "environment.yml").write_text(
        "name: t\ndependencies:\n  - python=3.12\n  - numpy>=1.22\n"
        "  - pip:\n    - requests==2.31.0\n    - flask>=2\n")

    graph, _ = orchestrate_ingestion(tmp_path, use_cache=False)

    assert graph.nodes["numpy"].version is None          # was ">=1.22"
    assert graph.nodes["python"].version is None         # was "=3.12"
    assert graph.nodes["requests"].version == "2.31.0"   # was "==2.31.0"
    assert graph.nodes["flask"].version is None


def _node(name, version):
    return Node(name=name, version=version, mode=Mode.PYPI, provenance=Provenance.PYPI,
                direct=True, declared_requirement=f"{name}>=1")


def _graph(nodes, quality=ResolutionQuality.RESOLVED):
    return DependencyGraph(nodes={n.name: n for n in nodes}, resolution_quality=quality,
                           source_fixation=SourceFixation.INTENT)


def test_assess_rows_show_an_unknown_base_as_unknown_not_an_upgrade():
    base = _graph([_node("flask", None), _node("six", "1.10.0")],
                  ResolutionQuality.DECLARED_ONLY)
    target = _graph([_node("flask", "3.1.3"), _node("six", "1.17.0")])

    rows = {r["name"]: r for r in build_comparison_rows(base, target, "3.12", None)}

    assert rows["flask"]["baseline_version"] == "unknown"
    assert rows["flask"]["status"] == "base-unknown"
    assert rows["flask"]["declared_constraint"] == ">=1"
    assert (rows["six"]["baseline_version"], rows["six"]["status"]) == ("1.10.0", "upgrade")


def test_risk_never_clears_cves_from_an_unknown_base(tmp_path, monkeypatch):
    from pymolt.risk import assess as risk_assess

    asked: list[tuple[str, str]] = []

    def vulns(name, version, client):
        asked.append((name, version))
        return [{"id": "OLD-1"}] if version != "3.1.3" else []

    monkeypatch.setattr(risk_assess.osv, "query_vulns", vulns)
    monkeypatch.setattr(risk_assess.pypi_metadata, "fetch_release_json", lambda *a: {"urls": []})
    monkeypatch.setattr(risk_assess.pypi_metadata, "fetch_package_json", lambda *a: {})
    graph = uv_runner.graph_from_declared_requirements("requirements.txt",
                                                       {"flask": "Flask>=0.10"})

    report = risk_assess.assess_risk(graph, _graph([_node("flask", "3.1.3")]), "3.12", tmp_path)

    assert asked == [("flask", "3.1.3")]                 # never flask 0.10
    assert report.packages[0].fixed_by_migration == []


def test_a_move_from_an_unknown_base_is_an_inconclusive_delta(tmp_path, monkeypatch):
    """The plan states the move, never computes it from a guessed release, and says what
    settles it; a known move beside it is computed as before."""
    rows = [{"name": "flask", "baseline_version": "unknown", "target_version": "3.1.3",
             "status": "base-unknown"},
            {"name": "six", "baseline_version": "1.10.0", "target_version": "1.17.0",
             "status": "upgrade"}]
    migrations = migrations_from_rows(rows)
    assert [(m.name, m.from_version) for m in migrations] == [("flask", "unknown"),
                                                              ("six", "1.10.0")]

    (tmp_path / "requirements.txt").write_text("flask>=0.10\nsix==1.10.0\n")
    (tmp_path / "requirements-target.txt").write_text("flask==3.1.3\nsix==1.17.0\n")
    EnvConfig(selected_manifest="requirements.txt", selected_tool=ToolChoice.UV,
              base_python="3.11", target_python="3.11").save(
        tmp_path / ".pymolt" / "env_config.json")
    monkeypatch.setattr(codemod_service, "resolve_codemod_migrations",
                        lambda *a, **k: (migrations, "test migrations"))
    computed: list[list[DependencyMigration]] = []

    def fake_deltas(project_dir, moves, **_):
        computed.append(list(moves))
        from pymolt.delta.models import DeltaReport

        return [DeltaReport(package=m.name, from_version=m.from_version,
                            to_version=m.to_version, quality="complete") for m in moves]

    monkeypatch.setattr(provider_mod, "compute_deltas", fake_deltas)

    plan, *_ = codemod_service.build_migration_plan(tmp_path)

    assert [[m.name for m in call] for call in computed] == [["six"]]
    assert [(d.name, d.from_version, d.to_version) for d in plan.dependencies] == [
        ("flask", "unknown", "3.1.3"), ("six", "1.10.0", "1.17.0")]
    flask = [i for i in plan.impacts if i.package == "flask"]
    assert [i.kind for i in flask] == ["INCONCLUSIVE"]
    assert "base version unknown" in flask[0].explanation
    assert "a baseline capture settles it" in flask[0].explanation


def test_the_baseline_capture_settles_an_unknown_base():
    """The capture records the version the app ran: it replaces "unknown", and the move is
    computed from it."""
    rows = [{"name": "flask", "baseline_version": "unknown", "target_version": "3.1.3",
             "status": "base-unknown"}]
    rows, notes = codemod_service.witness_rows(rows, {"flask": "1.1.2"})

    assert (rows[0]["baseline_version"], rows[0]["status"]) == ("1.1.2", "upgrade")
    assert notes == ["flask: the baseline capture ran 1.1.2 (the base version was unknown)"]
    assert [(m.from_version, m.to_version) for m in migrations_from_rows(rows)] == [
        ("1.1.2", "3.1.3")]


def test_a_legacy_resolve_that_ran_is_named_as_such(tmp_path, monkeypatch):
    from pymolt.ingestion import fallback_compiler

    monkeypatch.setattr(fallback_compiler, "compile_legacy",
                        lambda *a, **k: "six==1.10.0\n    # via -r requirements.txt\n")
    (tmp_path / "requirements.txt").write_text("six>=1.0\n")

    _graph_, report = orchestrate_ingestion(tmp_path, base_python="3.6", use_cache=False)

    assert report.resolution_quality == "resolved"
    assert any("resolved with pip-compile with a local interpreter" in w
               for w in report.warnings)


def test_a_graph_cached_by_an_older_pymolt_is_not_reused(tmp_path, monkeypatch):
    """A declared-only base cached before this fix carries the floor as its version: the
    cache key names the graph format, so that graph is a miss, not the current answer."""
    from pymolt.ingestion import resolve_cache

    src = tmp_path / "requirements.txt"
    src.write_text("flask>=0.10\n")
    key = resolve_cache.compute_key(src, "3.6", None, False)
    monkeypatch.setattr(resolve_cache, "GRAPH_FORMAT", "1")
    assert resolve_cache.compute_key(src, "3.6", None, False) != key
