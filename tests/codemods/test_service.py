"""Tests for pymolt.codemods.service — rows→migrations + run_codemods."""

from __future__ import annotations

from unittest.mock import patch

from pymolt.assess.service import AssessResult
from pymolt.codemods.models import CodemodBundle, CodemodPattern, DependencyMigration
from pymolt.codemods.service import (
    migrations_from_dependency_file,
    migrations_from_rows,
    resolve_codemod_migrations,
    run_codemods,
)
from pymolt.core.enums import Mode, Provenance, ResolutionQuality, SourceFixation
from pymolt.core.graph import DependencyGraph, Node
from pymolt.core.layers import IngestionReport
from pymolt.ingestion.config import EnvConfig, ToolChoice


class TestMigrationsFromRows:
    def test_only_upgrades(self):
        rows = [
            {"name": "flask", "baseline_version": "2.0.3",
             "target_version": "3.0.0", "status": "upgrade"},
            {"name": "click", "baseline_version": "8.1.0",
             "target_version": "8.1.0", "status": "unchanged"},
            {"name": "new", "baseline_version": None,
             "target_version": "1.0", "status": "added"},
            {"name": "gone", "baseline_version": "1.0",
             "target_version": None, "status": "removed"},
        ]
        migs = migrations_from_rows(rows)
        assert len(migs) == 1
        assert migs[0].name == "flask"
        assert (migs[0].from_version, migs[0].to_version) == ("2.0.3", "3.0.0")

    def test_skips_unpinned(self):
        rows = [
            {"name": "x", "baseline_version": "unpinned",
             "target_version": "2.0", "status": "upgrade"},
        ]
        assert migrations_from_rows(rows) == []


def test_resolve_codemod_migrations_prefers_cached_assess(monkeypatch):
    result = AssessResult(
        project_dir="/tmp/project",
        source_manifest="requirements.txt",
        source_fixation="pinned",
        resolution_quality="resolved",
        target_python="3.12",
        target_resolved=True,
        rows=[{
            "name": "flask",
            "baseline_version": "2.0.3",
            "target_version": "3.0.0",
            "status": "upgrade",
        }],
    )

    called = False

    def fake_dependency_file(*args, **kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(
        "pymolt.codemods.service.migrations_from_dependency_file", fake_dependency_file
    )

    migrations, source_desc = resolve_codemod_migrations("/tmp/project", assess_result=result)
    assert not called
    assert len(migrations) == 1
    assert "cached assess result" in source_desc


def test_resolve_codemod_migrations_uses_target_file_fallback(monkeypatch):
    def fake_dependency_file(
        project_dir, target_dependency_file, *, config=None, use_cache=True, warnings=None
    ):
        assert project_dir == "/tmp/project"
        assert target_dependency_file == "requirements-target.txt"
        return [DependencyMigration("flask", "2.0.3", "3.0.0")]

    monkeypatch.setattr(
        "pymolt.codemods.service.migrations_from_dependency_file", fake_dependency_file
    )

    migrations, source_desc = resolve_codemod_migrations(
        "/tmp/project",
        target_dependency_file="requirements-target.txt",
    )
    assert len(migrations) == 1
    assert "requirements-target.txt" in source_desc


def _node(name, version) -> Node:
    return Node(name=name, version=version, mode=Mode.PYPI, provenance=Provenance.PYPI, direct=True)


def _graph(nodes: dict[str, Node]) -> DependencyGraph:
    return DependencyGraph(
        nodes=nodes, resolution_quality=ResolutionQuality.RESOLVED,
        source_fixation=SourceFixation.PINNED,
    )


class TestContainerDegrade:
    """A stale/bogus configured container_id must not sink codemods resolution —
    mirrors assess.service._resolve_baseline_graph's graceful degrade."""

    def _setup_fixture(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("flask==2.0.3\n")
        target_file = tmp_path / "requirements-target.txt"
        target_file.write_text("flask==3.0.0\n")
        return target_file

    def test_migrations_from_dependency_file_degrades_on_dead_container(self, tmp_path):
        target_file = self._setup_fixture(tmp_path)
        cfg = EnvConfig(
            selected_manifest="requirements.txt",
            selected_tool=ToolChoice.CONTAINER,
            base_python="3.6.1",
            container_id="deadbeef0000",
        )

        baseline_graph = _graph({"flask": _node("flask", "2.0.3")})
        target_graph = _graph({"flask": _node("flask", "3.0.0")})
        report = IngestionReport(
            resolution_quality="resolved", source_fixation="pinned", manual_zone=[], warnings=[],
        )

        def fake_orchestrate(project_dir, *, chosen_source=None, target_python=None,
                              container_id=None, base_python=None, use_cache=True, tool=None,
                              constraint_file=None, force_recompile=False):
            if container_id:
                raise ValueError(
                    f"Specified Docker container '{container_id}' was not found running "
                    "or is not matching."
                )
            if chosen_source is not None and chosen_source.path.name == "requirements-target.txt":
                return target_graph, report
            return baseline_graph, report

        with patch("pymolt.codemods.service.orchestrate_ingestion", side_effect=fake_orchestrate):
            warnings: list[str] = []
            migrations = migrations_from_dependency_file(
                tmp_path, target_file, config=cfg, warnings=warnings,
            )

        assert len(migrations) == 1
        assert migrations[0].name == "flask"
        assert (migrations[0].from_version, migrations[0].to_version) == ("2.0.3", "3.0.0")
        assert any("deadbeef0000" in w and "not running" in w for w in warnings)

    def test_resolve_codemod_migrations_degrades_on_dead_container(self, tmp_path):
        target_file = self._setup_fixture(tmp_path)
        cfg = EnvConfig(
            selected_manifest="requirements.txt",
            selected_tool=ToolChoice.CONTAINER,
            base_python="3.6.1",
            container_id="deadbeef0000",
        ).model_dump(mode="json")

        baseline_graph = _graph({"flask": _node("flask", "2.0.3")})
        target_graph = _graph({"flask": _node("flask", "3.0.0")})
        report = IngestionReport(
            resolution_quality="resolved", source_fixation="pinned", manual_zone=[], warnings=[],
        )

        def fake_orchestrate(project_dir, *, chosen_source=None, target_python=None,
                              container_id=None, base_python=None, use_cache=True, tool=None,
                              constraint_file=None, force_recompile=False):
            if container_id:
                raise ValueError(
                    f"Specified Docker container '{container_id}' was not found running "
                    "or is not matching."
                )
            if chosen_source is not None and chosen_source.path.name == "requirements-target.txt":
                return target_graph, report
            return baseline_graph, report

        with patch("pymolt.codemods.service.orchestrate_ingestion", side_effect=fake_orchestrate):
            warnings: list[str] = []
            migrations, source_desc = resolve_codemod_migrations(
                tmp_path, target_dependency_file=target_file, config=cfg, warnings=warnings,
            )

        assert len(migrations) == 1
        assert "requirements-target.txt" in source_desc
        assert any("not running" in w for w in warnings)


class _FakeClient:
    def __init__(self, patterns):
        self._patterns = patterns
        self.calls = []

    def fetch_bundle(self, migrations, *, progress=None):
        self.calls.append(migrations)
        if progress:
            progress("analyzing")
        return {"flask": CodemodBundle(patterns=self._patterns)}

    def fetch_codemods(self, migrations):
        return {name: bundle.patterns for name, bundle in self.fetch_bundle(migrations).items()}


def test_run_codemods_fetches_and_applies(tmp_path):
    (tmp_path / "app.py").write_text(
        "from flask.helpers import safe_join\nx = safe_join(a, b)\n"
    )
    pattern = CodemodPattern(
        old_qualname="flask.helpers.safe_join",
        new_qualname="werkzeug.utils.safe_join",
        kind="rewrite-import",
        confidence="high",
    )
    fake = _FakeClient([pattern])

    by_pkg, result = run_codemods(
        tmp_path,
        [DependencyMigration("flask", "2.0.3", "3.0.0")],
        write=True,
        client=fake,
    )

    assert "flask" in by_pkg
    assert result.files_changed == 1
    assert "werkzeug.utils" in (tmp_path / "app.py").read_text()
    assert len(fake.calls) == 1  # client was used, no real network


def test_run_codemods_reports_progress(tmp_path):
    (tmp_path / "app.py").write_text("x = 1\n")
    pattern = CodemodPattern(
        old_qualname="flask.helpers.safe_join",
        new_qualname="werkzeug.utils.safe_join",
        kind="rewrite-import",
    )
    msgs: list[str] = []
    run_codemods(
        tmp_path,
        [DependencyMigration("flask", "2.0.3", "3.0.0")],
        client=_FakeClient([pattern]),
        progress=msgs.append,
    )
    # fetch phase + apply phase both surfaced a status line
    assert any("analyzing" in m for m in msgs)
    assert any("Applying" in m and "LibCST" in m for m in msgs)


def test_run_codemods_dry_run_default(tmp_path):
    (tmp_path / "app.py").write_text(
        "from flask.helpers import safe_join\nx = safe_join(a, b)\n"
    )
    pattern = CodemodPattern(
        old_qualname="flask.helpers.safe_join",
        new_qualname="werkzeug.utils.safe_join",
        kind="rewrite-import",
    )
    _, result = run_codemods(
        tmp_path,
        [DependencyMigration("flask", "2.0.3", "3.0.0")],
        client=_FakeClient([pattern]),
    )
    assert result.dry_run is True
    assert "flask.helpers" in (tmp_path / "app.py").read_text()  # untouched


# ── provider: the local delta engine ──────────────────────────────────────────

def test_codemod_provider_is_the_local_engine_and_passes_online(tmp_path):
    from pymolt.codemods.service import codemod_provider
    from pymolt.delta.provider import LocalDeltaProvider

    local = codemod_provider(tmp_path, online=True)
    assert isinstance(local, LocalDeltaProvider) and local.online is True


def test_preview_codemods_defaults_to_the_local_provider(tmp_path, monkeypatch):
    from pymolt.codemods import service
    from pymolt.delta import provider as provider_mod

    seen = {}

    class _Local:
        def __init__(self, project_dir, *, online=False, symbols=None):
            seen["project_dir"], seen["online"] = project_dir, online

        def fetch_bundle(self, migrations, *, progress=None, use_cache=None):
            return {"flask": CodemodBundle()}

    monkeypatch.setattr(provider_mod, "LocalDeltaProvider", _Local)
    (tmp_path / "app.py").write_text("x = 1\n")
    by_pkg, previews = service.preview_codemods(
        tmp_path, [DependencyMigration("flask", "2.0.3", "3.0.0")], online=True,
    )
    assert seen == {"project_dir": tmp_path, "online": True}
    assert by_pkg == {"flask": []} and previews == []


# ── from-versions witnessed by the baseline capture ─────────────────────────


def test_the_baseline_capture_version_beats_a_reresolved_unpinned_manifest():
    from pymolt.codemods.service import migrations_from_rows, witness_rows

    rows = [
        {"name": "pandas", "status": "upgrade", "baseline_version": "3.1.1",
         "target_version": "2.2.3"},
        {"name": "Requests", "status": "unchanged", "baseline_version": "2.34.2",
         "target_version": "2.34.2"},
        {"name": "pytz", "status": "unchanged", "baseline_version": "2026.6",
         "target_version": "2026.6"},
        {"name": "attrs", "status": "upgrade", "baseline_version": "23.1",
         "target_version": "24.2"},
    ]
    witnessed = {"pandas": "1.5.3", "requests": "2.31.0", "pytz": "2026.6"}
    out, notes = witness_rows(rows, witnessed)
    assert [(m.name, m.from_version, m.to_version) for m in migrations_from_rows(out)] == [
        ("pandas", "1.5.3", "2.2.3"), ("Requests", "2.31.0", "2.34.2"),
        ("attrs", "23.1", "24.2")]
    assert notes == [
        "pandas: the baseline capture ran 1.5.3 (the baseline manifest resolves to 3.1.1 today)",
        "Requests: the baseline capture ran 2.31.0 (the baseline manifest resolves to "
        "2.34.2 today)"]
    assert witness_rows(rows, None) == (rows, [])


def test_witnessed_distributions_reads_the_baseline_capture_record(tmp_path):
    import json

    from pymolt.verify.models import ContractSlot, ContractState
    from pymolt.verify.service import _state_path, witnessed_distributions

    traces = tmp_path / ".pymolt" / "contract_traces"
    traces.mkdir(parents=True)
    meta = traces / "baseline.jsonl.meta.json"
    meta.write_text(json.dumps({"processes": [
        {"pid": 1, "distributions": {"pandas": "1.5.3", "NumPy": "1.26.4"}},
        {"pid": 2, "distributions": {"pandas": "1.5.3", "numpy": "2.0.0"}},
        {"pid": 3},
    ]}))
    state = ContractState(baseline=ContractSlot(
        trace_path=".pymolt/contract_traces/baseline.jsonl", captured_at="now",
        mode="test_suite", metadata_path=".pymolt/contract_traces/baseline.jsonl.meta.json"))
    state.save(_state_path(tmp_path))
    # numpy ran at two versions across processes: ambiguous, left out.
    assert witnessed_distributions(tmp_path) == {"pandas": "1.5.3"}
    meta.write_text(json.dumps({"processes": [{"pid": 1}]}))
    assert witnessed_distributions(tmp_path) is None      # a capture that predates it


def test_a_resolver_failure_while_planning_is_a_plan_input_error(tmp_path):
    """B3: a uv failure on the target manifest is a summary + hint, not a traceback."""
    import subprocess

    import pytest

    from pymolt.codemods.service import PlanInputError

    (tmp_path / "requirements.txt").write_text("empyrical==0.5.5\n")
    target = tmp_path / "requirements-target.txt"
    target.write_text("empyrical==0.5.5\n")
    stderr = (
        "  \x1b[31m\u00d7\x1b[0m Failed to build `empyrical==0.5.5`\n"
        "  \u251c\u2500\u25b6 The build backend returned an error\n"
        "      AttributeError: module 'configparser' has no attribute 'SafeConfigParser'\n"
    )

    def boom(*args, **kwargs):
        raise subprocess.CalledProcessError(2, ["uv", "pip", "compile"], stderr=stderr)

    cfg = EnvConfig(selected_manifest="requirements.txt", selected_tool=ToolChoice.UV,
                    base_python="3.11", target_python="3.12")
    with patch("pymolt.codemods.service.orchestrate_ingestion", side_effect=boom):
        with pytest.raises(PlanInputError) as caught:
            migrations_from_dependency_file(tmp_path, target, config=cfg)

    assert "does not resolve: Failed to build `empyrical==0.5.5`" in str(caught.value)
    assert "Culprit(s): empyrical" in caught.value.hint
    assert "No source files were changed" in caught.value.hint
