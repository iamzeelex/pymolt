"""The assess service is the shared compute core for interfaces:
graphs in, comparison rows + result out — no prompts, no printing."""

from unittest.mock import patch

import pytest

from pymolt.assess import (
    build_comparison_rows,
    resolve_chosen_source,
    run_assess,
)
from pymolt.core.enums import Mode, Provenance, ResolutionQuality, SourceFixation
from pymolt.core.graph import DependencyGraph, Node
from pymolt.core.layers import IngestionReport


def _node(name, version, direct=True, declared=None) -> Node:
    return Node(
        name=name, version=version, mode=Mode.PYPI, provenance=Provenance.PYPI,
        direct=direct, declared_requirement=declared,
    )


def _graph(nodes: dict[str, Node]) -> DependencyGraph:
    return DependencyGraph(
        nodes=nodes,
        resolution_quality=ResolutionQuality.RESOLVED,
        source_fixation=SourceFixation.PINNED,
    )


def test_build_comparison_rows_classifies_status():
    baseline = _graph({
        "requests": _node("requests", "2.31.0", declared="requests==2.31.0"),
        "old": _node("old", "1.0"),
    })
    target = _graph({
        "requests": _node("requests", "2.32.0", declared="requests==2.31.0"),
        "new": _node("new", "0.1"),
    })
    rows = {r["name"]: r for r in build_comparison_rows(baseline, target, "3.12", None)}

    assert rows["requests"]["status"] == "upgrade"
    assert rows["new"]["status"] == "added"
    assert rows["old"]["status"] == "removed"


def test_build_comparison_rows_conflict_on_target_error():
    baseline = _graph({"requests": _node("requests", "2.31.0")})
    rows = build_comparison_rows(baseline, None, "3.12", "resolution failed")
    assert all(r["status"] == "conflict" for r in rows)


def test_resolve_chosen_source_missing_raises():
    class Src:
        def __init__(self, name, is_lock=False):
            self.path = type("P", (), {"name": name})()
            self.is_lock = is_lock

    sources = [Src("requirements.txt"), Src("uv.lock", is_lock=True)]
    # Named -> exact match.
    assert resolve_chosen_source(sources, "requirements.txt").path.name == "requirements.txt"
    # Default -> first lock.
    assert resolve_chosen_source(sources, None).path.name == "uv.lock"
    with pytest.raises(ValueError):
        resolve_chosen_source(sources, "nope.txt")


def test_run_assess_resolves_and_compares(tmp_path):
    (tmp_path / "requirements.txt").write_text("requests==2.31.0\n")
    baseline = _graph({"requests": _node("requests", "2.31.0", declared="requests==2.31.0")})
    target = _graph({"requests": _node("requests", "2.32.0", declared="requests==2.31.0")})
    report = IngestionReport(
        resolution_quality="resolved", source_fixation="pinned",
        manual_zone=[], warnings=[], detected_python="3.8",
    )

    with patch("pymolt.assess.service.orchestrate_ingestion") as orch:
        orch.side_effect = [(baseline, report), (target, report)]
        result = run_assess(tmp_path, target_python="3.12", config={})

    assert result.target_resolved
    assert result.target_python == "3.12"
    assert result.counts_by_status().get("upgrade") == 1
    assert orch.call_count == 2  # baseline + target
