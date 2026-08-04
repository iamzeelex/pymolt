import pytest
from pymolt.core.graph import DependencyGraph, Node
from pymolt.core.enums import Mode, Provenance, ResolutionQuality, SourceFixation


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
