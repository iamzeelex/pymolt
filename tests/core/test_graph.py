from pymolt.core.graph import DependencyGraph
from pymolt.core.serialize import deserialize_graph, serialize_graph


def test_graph_serialization(sample_graph: DependencyGraph):
    loaded_graph = deserialize_graph(serialize_graph(sample_graph))

    # every field survives the round trip: nodes (mode, provenance, mapping, direct, declared
    # requirement), edges, roots, resolution quality and fixation
    assert loaded_graph == sample_graph
    assert loaded_graph.edges[0].target == "idna"
