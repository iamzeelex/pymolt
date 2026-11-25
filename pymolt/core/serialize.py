from pathlib import Path
from typing import Union
from pymolt.core.graph import DependencyGraph


def serialize_graph(graph: DependencyGraph) -> str:
    """Serialize the DependencyGraph to a JSON string."""
    return graph.model_dump_json(indent=2)


def deserialize_graph(json_str: str) -> DependencyGraph:
    """Deserialize a DependencyGraph from a JSON string."""
    return DependencyGraph.model_validate_json(json_str)


def save_graph_to_file(graph: DependencyGraph, file_path: Union[str, Path]) -> None:
    """Save the DependencyGraph to a JSON file."""
    from pymolt.state_io import atomic_write_text

    atomic_write_text(file_path, serialize_graph(graph))


def load_graph_from_file(file_path: Union[str, Path]) -> DependencyGraph:
    """Load a DependencyGraph from a JSON file."""
    path = Path(file_path)
    return deserialize_graph(path.read_text(encoding="utf-8"))
