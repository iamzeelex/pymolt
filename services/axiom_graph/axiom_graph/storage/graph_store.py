"""
axiom_graph/graph_store.py

Persistence layer for the symbol-evolution knowledge graph.

Today the graph lives in a JSON file; the plan is to move to Neo4j. To make
that swap a thin adapter rather than a rewrite, this module models the graph in
a **Neo4j-native shape** and hides storage behind a Protocol:

  - GraphNode(id, labels, properties)         ≈ a Neo4j node with labels + props
  - GraphEdge(src, dst, type, properties)     ≈ a Neo4j relationship (typed)
  - GraphStore  (Protocol)                    ≈ upsert/query operations
  - JsonGraphStore                            ← used now
  - Neo4jGraphStore                           ← documented seam (MERGE mapping)

The graph-algorithm layer (NetworkX SymbolEvolution in propose.py) is loaded
*from* a store; the store itself stays dependency-light (stdlib only) so the
Neo4j adapter has nothing to fight with.

Layering recap: GraphStore = persistence; NetworkX = in-memory algorithms;
LibCST = syntax. Three layers, swappable independently.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from axiom_graph.core.models import CodemodPattern, CodemodRule

# Canonical label / relationship-type constants (Neo4j vocabulary).
SYMBOL_LABEL = "Symbol"
REPLACED_BY = "REPLACED_BY"
ANALYSIS_LABEL = "Analysis"
RULE_SET_LABEL = "RuleSet"


# ─────────────────────────────────────────────────────────────────────────────
# Neutral graph elements (map 1:1 onto Neo4j nodes/relationships)
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class GraphNode:
    id: str
    labels: list[str] = field(default_factory=lambda: [SYMBOL_LABEL])
    properties: dict = field(default_factory=dict)


@dataclass
class GraphEdge:
    src: str
    dst: str
    type: str = REPLACED_BY
    properties: dict = field(default_factory=dict)

    def key(self) -> tuple[str, str, str]:
        return (self.src, self.dst, self.type)


# ─────────────────────────────────────────────────────────────────────────────
# Store interface
# ─────────────────────────────────────────────────────────────────────────────


@runtime_checkable
class GraphStore(Protocol):
    """Storage-agnostic graph operations. JSON now, Neo4j later."""

    def upsert_node(self, node: GraphNode) -> None: ...
    def upsert_edge(self, edge: GraphEdge) -> None: ...
    def nodes(self) -> list[GraphNode]: ...
    def edges(self) -> list[GraphEdge]: ...
    def successors(self, node_id: str) -> list[GraphEdge]: ...
    def predecessors(self, node_id: str) -> list[GraphEdge]: ...
    def flush(self) -> None: ...


# ─────────────────────────────────────────────────────────────────────────────
# JSON-backed store (current)
# ─────────────────────────────────────────────────────────────────────────────


class JsonGraphStore:
    """
    A graph stored as a single JSON document {nodes:[...], edges:[...]}.

    Upserts are merge semantics (properties updated in place). Use as a context
    manager to auto-flush, or call flush() explicitly.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._nodes: dict[str, GraphNode] = {}
        self._edges: dict[tuple[str, str, str], GraphEdge] = {}
        if self.path.exists():
            self._load()

    # -- io ------------------------------------------------------------------

    def _load(self) -> None:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        for n in data.get("nodes", []):
            node = GraphNode(
                id=n["id"],
                labels=n.get("labels", [SYMBOL_LABEL]),
                properties=n.get("properties", {}),
            )
            self._nodes[node.id] = node
        for e in data.get("edges", []):
            edge = GraphEdge(
                src=e["src"],
                dst=e["dst"],
                type=e.get("type", REPLACED_BY),
                properties=e.get("properties", {}),
            )
            self._edges[edge.key()] = edge

    def flush(self) -> None:
        doc = {
            "nodes": [
                {"id": n.id, "labels": n.labels, "properties": n.properties}
                for n in self._nodes.values()
            ],
            "edges": [
                {"src": e.src, "dst": e.dst, "type": e.type, "properties": e.properties}
                for e in self._edges.values()
            ],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(doc, indent=2, sort_keys=True), encoding="utf-8")

    # -- upserts -------------------------------------------------------------

    def upsert_node(self, node: GraphNode) -> None:
        existing = self._nodes.get(node.id)
        if existing is None:
            self._nodes[node.id] = node
        else:
            existing.labels = sorted(set(existing.labels) | set(node.labels))
            existing.properties.update(node.properties)

    def upsert_edge(self, edge: GraphEdge) -> None:
        existing = self._edges.get(edge.key())
        if existing is None:
            self._edges[edge.key()] = edge
        else:
            existing.properties.update(edge.properties)

    # -- queries -------------------------------------------------------------

    def nodes(self) -> list[GraphNode]:
        return list(self._nodes.values())

    def edges(self) -> list[GraphEdge]:
        return list(self._edges.values())

    def successors(self, node_id: str) -> list[GraphEdge]:
        return [e for e in self._edges.values() if e.src == node_id]

    def predecessors(self, node_id: str) -> list[GraphEdge]:
        return [e for e in self._edges.values() if e.dst == node_id]

    # -- context manager -----------------------------------------------------

    def __enter__(self) -> JsonGraphStore:
        return self

    def __exit__(self, *exc) -> None:
        self.flush()


# ─────────────────────────────────────────────────────────────────────────────
# Neo4j-backed store
# ─────────────────────────────────────────────────────────────────────────────


def _safe_rel_type(rel: str) -> str:
    """Sanitize a relationship type (can't be parameterized in Cypher)."""
    return rel if re.fullmatch(r"[A-Z][A-Z0-9_]*", rel) else REPLACED_BY


class Neo4jGraphStore:
    """
    Neo4j-backed graph store — a thin mapping over the same GraphNode/GraphEdge
    shapes. Symbols are `:Symbol {id}` nodes; replacements are typed
    relationships carrying the pattern properties.

    Upserts are idempotent MERGE queries; reads are MATCH traversals. Writes are
    immediate (flush() is a no-op). Pass `driver=` to inject a driver (tests);
    otherwise a driver is created from `uri`/credentials. Use as a context
    manager to close the driver.

    Requires the `neo4j` package and a reachable database.
    """

    def __init__(
        self,
        uri: str | None = None,
        *,
        user: str = "neo4j",
        password: str = "neo4j",
        database: str | None = None,
        driver=None,
        ensure_schema: bool = True,
    ) -> None:
        if driver is not None:
            self._driver = driver
        else:
            from neo4j import GraphDatabase  # lazy: only needed for this store

            self._driver = GraphDatabase.driver(uri, auth=(user, password))
        self._database = database
        if ensure_schema:
            self._run(
                "CREATE CONSTRAINT symbol_id IF NOT EXISTS "
                "FOR (s:Symbol) REQUIRE s.id IS UNIQUE"
            )

    # -- query plumbing ------------------------------------------------------

    def _session(self):
        if self._database:
            return self._driver.session(database=self._database)
        return self._driver.session()

    def _run(self, query: str, **params):
        with self._session() as session:
            return list(session.run(query, **params))

    # -- upserts -------------------------------------------------------------

    def upsert_node(self, node: GraphNode) -> None:
        self._run(
            "MERGE (s:Symbol {id: $id}) SET s += $props",
            id=node.id,
            props=_scalar_props(node.properties),
        )

    def upsert_edge(self, edge: GraphEdge) -> None:
        rel = _safe_rel_type(edge.type)
        self._run(
            "MERGE (a:Symbol {id: $src}) "
            "MERGE (b:Symbol {id: $dst}) "
            f"MERGE (a)-[r:{rel}]->(b) SET r += $props",
            src=edge.src,
            dst=edge.dst,
            props=_scalar_props(edge.properties),
        )

    # -- queries -------------------------------------------------------------

    def nodes(self) -> list[GraphNode]:
        rows = self._run("MATCH (s:Symbol) RETURN s.id AS id, properties(s) AS props")
        out = []
        for r in rows:
            props = dict(r["props"])
            props.pop("id", None)
            out.append(GraphNode(id=r["id"], properties=props))
        return out

    def _edge_rows(self, query: str, **params) -> list[GraphEdge]:
        rows = self._run(query, **params)
        return [
            GraphEdge(
                src=r["src"], dst=r["dst"], type=r["type"],
                properties=dict(r["props"]),
            )
            for r in rows
        ]

    def edges(self) -> list[GraphEdge]:
        return self._edge_rows(
            "MATCH (a:Symbol)-[r]->(b:Symbol) "
            "RETURN a.id AS src, b.id AS dst, type(r) AS type, properties(r) AS props"
        )

    def successors(self, node_id: str) -> list[GraphEdge]:
        return self._edge_rows(
            "MATCH (a:Symbol {id: $id})-[r]->(b:Symbol) "
            "RETURN a.id AS src, b.id AS dst, type(r) AS type, properties(r) AS props",
            id=node_id,
        )

    def predecessors(self, node_id: str) -> list[GraphEdge]:
        return self._edge_rows(
            "MATCH (a:Symbol)-[r]->(b:Symbol {id: $id}) "
            "RETURN a.id AS src, b.id AS dst, type(r) AS type, properties(r) AS props",
            id=node_id,
        )

    def flush(self) -> None:
        return  # writes are committed per-query

    def close(self) -> None:
        self._driver.close()

    def __enter__(self) -> Neo4jGraphStore:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _scalar_props(props: dict) -> dict:
    """
    Neo4j properties must be scalars or homogeneous lists of scalars — drop
    nested dicts/objects (none today, but keeps the adapter total).
    """
    out = {}
    for k, v in props.items():
        if isinstance(v, (str, int, float, bool)) or (
            isinstance(v, list) and all(isinstance(x, (str, int, float, bool)) for x in v)
        ):
            out[k] = v
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Bridge: CodemodPattern ⇄ graph
# ─────────────────────────────────────────────────────────────────────────────


def write_patterns(
    store: GraphStore,
    patterns: list[CodemodPattern],
    *,
    package: str = "",
    from_version: str = "",
    to_version: str = "",
) -> None:
    """Persist codemod patterns as Symbol nodes + REPLACED_BY edges."""
    for p in patterns:
        store.upsert_node(GraphNode(id=p.old_qualname))
        store.upsert_node(GraphNode(id=p.new_qualname))
        store.upsert_edge(
            GraphEdge(
                src=p.old_qualname,
                dst=p.new_qualname,
                type=REPLACED_BY,
                properties={
                    "kind": p.kind,
                    "confidence": p.confidence,
                    "evidence": list(p.evidence),
                    "package": package,
                    "from_version": from_version,
                    "to_version": to_version,
                },
            )
        )
    store.flush()


def read_patterns(store: GraphStore) -> list[CodemodPattern]:
    """Reconstruct codemod patterns from the REPLACED_BY edges in a store."""
    out: list[CodemodPattern] = []
    for e in store.edges():
        if e.type != REPLACED_BY:
            continue
        props = e.properties
        out.append(
            CodemodPattern(
                old_qualname=e.src,
                new_qualname=e.dst,
                kind=props.get("kind", "rename-call"),
                confidence=props.get("confidence", "low"),
                evidence=list(props.get("evidence", [])),
            )
        )
    return out


def persist_full_delta(store: GraphStore, delta) -> None:
    """
    Persist a FullDelta's transitively-resolved codemods and rules into a
    store, tagged with the package + version range. Duck-typed on .codemods /
    .rules / .package / .from_version / .to_version so it stays decoupled
    from the models import.
    """
    write_patterns(
        store,
        list(delta.codemods),
        package=getattr(delta, "package", ""),
        from_version=getattr(delta, "from_version", ""),
        to_version=getattr(delta, "to_version", ""),
    )
    write_rules(
        store,
        list(getattr(delta, "rules", [])),
        package=getattr(delta, "package", ""),
        from_version=getattr(delta, "from_version", ""),
        to_version=getattr(delta, "to_version", ""),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Bridge: CodemodRule ⇄ graph
#
# Rules don't share the CodemodPattern old→new edge shape — a template rule
# (e.g. the pandas .lookup() expansion) has no old_qualname/new_qualname at
# all. Rather than force-fit them onto REPLACED_BY edges, each rule is
# JSON-serialized to a STRING (Neo4j properties must be scalars or
# homogeneous lists of scalars — a list of dicts would be silently dropped by
# _scalar_props) and stashed as a `rules` property list on one node keyed to
# the (library, from_version, to_version) triple being analyzed.
# ─────────────────────────────────────────────────────────────────────────────


_RULES_PREFIX = "rules::"


def _rules_id(package: str, from_version: str, to_version: str) -> str:
    return f"{_RULES_PREFIX}{package}::{from_version}::{to_version}"


def write_rules(
    store: GraphStore,
    rules: list[CodemodRule],
    *,
    package: str = "",
    from_version: str = "",
    to_version: str = "",
) -> None:
    """Persist codemod rules as a JSON-string list property on one node."""
    if not rules:
        return
    store.upsert_node(
        GraphNode(
            id=_rules_id(package, from_version, to_version),
            labels=[RULE_SET_LABEL],
            properties={
                "package": package,
                "from_version": from_version,
                "to_version": to_version,
                "rules": [r.model_dump_json() for r in rules],
            },
        )
    )
    store.flush()


def read_rules(
    store: GraphStore, package: str, from_version: str, to_version: str
) -> list[CodemodRule]:
    """Reconstruct codemod rules persisted for a (package, from, to) migration."""
    target = _rules_id(package, from_version, to_version)
    for n in store.nodes():
        if n.id == target:
            return [CodemodRule.model_validate_json(s) for s in n.properties.get("rules", [])]
    return []


# ─────────────────────────────────────────────────────────────────────────────
# Analysis markers — record that a (package, from, to) migration was processed,
# so a re-run can skip it. Stored as an `:Analysis` node via the plain node
# Protocol, so it works on JSON and Neo4j alike. Crucially this tracks a
# migration even when it yielded ZERO codemods (which leaves no edges) — so
# "already analyzed" ≠ "has codemods".
# ─────────────────────────────────────────────────────────────────────────────


_ANALYSIS_PREFIX = "analysis::"


def _analysis_id(package: str, from_version: str, to_version: str) -> str:
    return f"{_ANALYSIS_PREFIX}{package}::{from_version}::{to_version}"


def mark_analyzed(store: GraphStore, package: str, from_version: str, to_version: str) -> None:
    """Record that this migration has been processed (idempotent)."""
    store.upsert_node(
        GraphNode(
            id=_analysis_id(package, from_version, to_version),
            labels=[ANALYSIS_LABEL],
            properties={
                "package": package,
                "from_version": from_version,
                "to_version": to_version,
            },
        )
    )


def analyzed_migrations(store: GraphStore) -> set[tuple[str, str, str]]:
    """
    The set of (package, from_version, to_version) already processed — the union
    of explicit `:Analysis` markers and any migration that left REPLACED_BY
    edges (so older stores written before markers existed still count).
    """
    done: set[tuple[str, str, str]] = set()
    for n in store.nodes():
        # Detect by id prefix, not label: the Neo4j store persists every node as
        # :Symbol and does not round-trip custom labels, so the id is the only
        # reliable marker across both backends.
        if ANALYSIS_LABEL in n.labels or n.id.startswith(_ANALYSIS_PREFIX):
            p = n.properties
            done.add((p.get("package", ""), p.get("from_version", ""), p.get("to_version", "")))
    for e in store.edges():
        if e.type == REPLACED_BY:
            p = e.properties
            done.add((p.get("package", ""), p.get("from_version", ""), p.get("to_version", "")))
    done.discard(("", "", ""))
    return done


# ─────────────────────────────────────────────────────────────────────────────
# Store factory — pick JSON or Neo4j by target / environment
# ─────────────────────────────────────────────────────────────────────────────

_NEO4J_SCHEMES = ("bolt://", "neo4j://", "bolt+s://", "neo4j+s://", "bolt+ssc://", "neo4j+ssc://")


def open_store(target: str | Path | None = None, **neo4j_kwargs) -> GraphStore:
    """
    Open a graph store by target:
      - a bolt://… / neo4j://… URI → Neo4jGraphStore
      - any other path (default axiom_graph.json) → JsonGraphStore

    For Neo4j, pass user/password/database via kwargs (or use defaults). This is
    the single switch the API and the bench harness use to go JSON↔Neo4j.
    """
    if target is None:
        target = "axiom_graph.json"
    s = str(target)
    if s.startswith(_NEO4J_SCHEMES):
        return Neo4jGraphStore(s, **neo4j_kwargs)
    return JsonGraphStore(target)


def store_from_env() -> GraphStore:
    """
    Open the store configured by environment:
      AXIOM_GRAPH_STORE   — JSON path or bolt:// URI (default: axiom_graph.json)
      NEO4J_USER / NEO4J_PASSWORD / NEO4J_DATABASE — Neo4j credentials
    """
    import os

    target = os.environ.get("AXIOM_GRAPH_STORE", "axiom_graph.json")
    kwargs = {}
    if str(target).startswith(_NEO4J_SCHEMES):
        kwargs = {
            "user": os.environ.get("NEO4J_USER", "neo4j"),
            "password": os.environ.get("NEO4J_PASSWORD", "neo4j"),
            "database": os.environ.get("NEO4J_DATABASE") or None,
        }
    return open_store(target, **kwargs)
