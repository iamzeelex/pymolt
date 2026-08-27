from pathlib import Path
import pytest
from pymolt.core.graph import DependencyGraph, Node
from pymolt.core.enums import Mode, Provenance, ResolutionQuality, SourceFixation


@pytest.fixture(autouse=True)
def _isolate_test_cache_and_config(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Ensure tests run with clean isolated cache and config dirs unless overridden."""
    tmp_dir = tmp_path_factory.mktemp("test_env")
    c_dir = tmp_dir / "cache"
    cfg_dir = tmp_dir / "config"
    c_dir.mkdir(exist_ok=True)
    cfg_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("XDG_CACHE_HOME", str(c_dir))


@pytest.fixture
def sample_graph() -> DependencyGraph:
    """Provide a standard minimal DependencyGraph for testing."""
    nodes = {
        "requests": Node(
            name="requests",
            version="2.31.0",
            mode=Mode.PYPI,
            provenance=Provenance.PYPI,
            direct=True,
        )
    }
    return DependencyGraph(
        nodes=nodes,
        edges=[],
        roots=["requests"],
        resolution_quality=ResolutionQuality.RESOLVED,
        source_fixation=SourceFixation.PINNED,
    )
