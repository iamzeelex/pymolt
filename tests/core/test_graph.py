from pymolt.core.graph import DependencyGraph
from pymolt.core.serialize import serialize_graph, deserialize_graph


def test_graph_serialization(sample_graph: DependencyGraph):
    json_str = serialize_graph(sample_graph)
    loaded_graph = deserialize_graph(json_str)
    
    assert loaded_graph.roots == sample_graph.roots
    assert "requests" in loaded_graph.nodes
    assert loaded_graph.nodes["requests"].version == "2.31.0"
