import pytest

from pymolt.core.enums import Mode, Provenance, ResolutionQuality, SourceFixation
from pymolt.core.graph import DependencyGraph, Edge, NameMapping, Node


@pytest.fixture(autouse=True)
def _isolate_test_cache_and_config(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Ensure tests run with clean isolated cache and config dirs unless overridden."""
    tmp_dir = tmp_path_factory.mktemp("test_env")
    c_dir = tmp_dir / "cache"
    cfg_dir = tmp_dir / "config"
    c_dir.mkdir(exist_ok=True)
    cfg_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("XDG_CACHE_HOME", str(c_dir))
    # Person attestations (pymolt.attest) live outside the project, in the user's state and
    # config folders: a test never writes the real ones.
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_dir / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_dir))


@pytest.fixture
def sample_graph() -> DependencyGraph:
    """A small DependencyGraph: one direct node, one transitive node and the edge between them."""
    nodes = {
        "requests": Node(
            name="requests",
            version="2.31.0",
            mode=Mode.PYPI,
            provenance=Provenance.PYPI,
            direct=True,
            declared_requirement="requests>=2.0",
            mapping=NameMapping(pypi_name="requests", import_name="requests",
                                confidence="authoritative"),
        ),
        "idna": Node(
            name="idna",
            version="3.6",
            mode=Mode.PYPI,
            provenance=Provenance.PYPI,
        ),
    }
    return DependencyGraph(
        nodes=nodes,
        edges=[Edge(source="requests", target="idna", via="requests")],
        roots=["requests"],
        resolution_quality=ResolutionQuality.RESOLVED,
        source_fixation=SourceFixation.PINNED,
    )
