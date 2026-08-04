from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field
from pymolt.core.enums import Mode, Provenance, ResolutionQuality, SourceFixation


class NameMapping(BaseModel):
    pypi_name: Optional[str] = None
    conda_name: Optional[str] = None
    import_name: Optional[str] = None
    confidence: Optional[str] = None
    source: Optional[str] = None


class Node(BaseModel):
    name: str
    version: Optional[str] = None
    mode: Mode
    provenance: Provenance
    mapping: Optional[NameMapping] = None
    manual_bridge: bool = False
    direct: bool = False
    requires_python: Optional[str] = None
    declared_requirement: Optional[str] = None
    raw: Dict[str, Any] = Field(default_factory=dict)


class Edge(BaseModel):
    source: str
    target: str
    via: Optional[str] = None  # direct or transitive dependency trail


class DependencyGraph(BaseModel):
    nodes: Dict[str, Node] = Field(default_factory=dict)
    edges: List[Edge] = Field(default_factory=list)
    roots: List[str] = Field(default_factory=list)
    resolution_quality: ResolutionQuality
    source_fixation: SourceFixation
