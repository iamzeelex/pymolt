"""Direct parsers for fully-resolved PyPI lock files.

A lock file is an authoritative, already-resolved snapshot of an environment.
Re-compiling the sibling manifest instead of reading the lock would silently
substitute a *reconstruction* for that snapshot and can drift to different
versions, so these parsers read the lock itself and only fall back to
recompilation as a last resort (handled by the caller).

Every parser returns a :class:`DependencyGraph` with ``LOCK_PARSED`` quality
(we trust the lock but did not run a solver ourselves) and ``PINNED`` fixation.
The edge convention matches :mod:`pymolt.ingestion.uv_runner`: an edge
``source -> target`` means *source requires target*.
"""

import json
import logging
import tomllib
from pathlib import Path

from pymolt.core.enums import Mode, Provenance, ResolutionQuality, SourceFixation
from pymolt.core.graph import DependencyGraph, Edge, Node

logger = logging.getLogger(__name__)


def _normalize(name: str) -> str:
    """Lower-case a distribution name to match the keys used across graphs."""
    return name.strip().lower()


def _read_poetry_pyproject_direct(manifest_dir: Path) -> set:
    """Collect the names of direct dependencies declared for a Poetry project."""
    pyproject = manifest_dir / "pyproject.toml"
    if not pyproject.is_file():
        return set()
    try:
        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.warning("Could not read %s for direct-dependency detection: %s", pyproject, e)
        return set()

    direct: set = set()
    # Poetry 1.x table.
    poetry_deps = data.get("tool", {}).get("poetry", {}).get("dependencies", {})
    for name in poetry_deps:
        if name.lower() != "python":
            direct.add(_normalize(name))
    # PEP 621 table (Poetry 2.x / hybrid projects).
    for dep in data.get("project", {}).get("dependencies", []):
        from packaging.requirements import Requirement
        try:
            direct.add(_normalize(Requirement(dep).name))
        except Exception:
            pass
    return direct


def _read_pipfile_direct(manifest_dir: Path) -> set:
    """Collect direct dependency names declared in a Pipfile (TOML)."""
    pipfile = manifest_dir / "Pipfile"
    if not pipfile.is_file():
        return set()
    try:
        with open(pipfile, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.warning("Could not read %s for direct-dependency detection: %s", pipfile, e)
        return set()
    direct: set = set()
    for section in ("packages", "dev-packages"):
        for name in data.get(section, {}):
            direct.add(_normalize(name))
    return direct


def parse_uv_lock(lock_path: Path) -> DependencyGraph:
    """Parse a ``uv.lock`` (TOML) into a fully-edged dependency graph."""
    with open(lock_path, "rb") as f:
        data = tomllib.load(f)

    packages = data.get("package", [])
    if not isinstance(packages, list):
        raise ValueError(f"Malformed uv.lock: 'package' is not a list in {lock_path}")

    # The project itself appears as an editable/virtual package; its declared
    # dependencies are the direct (root) set. Everything else is transitive.
    root_names: set = set()
    direct_names: set = set()
    for pkg in packages:
        source = pkg.get("source", {})
        if isinstance(source, dict) and ({"editable", "virtual"} & set(source.keys())):
            root_names.add(_normalize(pkg.get("name", "")))
            for dep in pkg.get("dependencies", []):
                if isinstance(dep, dict) and dep.get("name"):
                    direct_names.add(_normalize(dep["name"]))

    nodes: dict = {}
    edges: list = []
    for pkg in packages:
        name = _normalize(pkg.get("name", ""))
        if not name or name in root_names:
            continue  # skip the project/workspace roots themselves
        nodes[name] = Node(
            name=name,
            version=pkg.get("version"),
            mode=Mode.PYPI,
            provenance=Provenance.PYPI,
            direct=name in direct_names,
            manual_bridge=False,
            raw={"source": pkg.get("source", {})},
        )

    for pkg in packages:
        source_name = _normalize(pkg.get("name", ""))
        if source_name in root_names or source_name not in nodes:
            continue
        for dep in pkg.get("dependencies", []):
            if isinstance(dep, dict) and dep.get("name"):
                target = _normalize(dep["name"])
                if target in nodes and target != source_name:
                    edges.append(Edge(source=source_name, target=target, via="transitive"))

    if not direct_names and nodes:
        logger.warning("uv.lock %s exposed no root package; all nodes marked transitive.", lock_path)

    roots = [n for n, node in nodes.items() if node.direct]
    return DependencyGraph(
        nodes=nodes,
        edges=edges,
        roots=roots,
        resolution_quality=ResolutionQuality.LOCK_PARSED,
        source_fixation=SourceFixation.PINNED,
    )


def parse_poetry_lock(lock_path: Path) -> DependencyGraph:
    """Parse a ``poetry.lock`` (TOML) into a fully-edged dependency graph."""
    with open(lock_path, "rb") as f:
        data = tomllib.load(f)

    packages = data.get("package", [])
    if not isinstance(packages, list):
        raise ValueError(f"Malformed poetry.lock: 'package' is not a list in {lock_path}")

    # poetry.lock holds the full closure but does not flag direct deps; those
    # live in the sibling pyproject.toml.
    direct_names = _read_poetry_pyproject_direct(lock_path.parent)

    nodes: dict = {}
    for pkg in packages:
        name = _normalize(pkg.get("name", ""))
        if not name:
            continue
        nodes[name] = Node(
            name=name,
            version=pkg.get("version"),
            mode=Mode.PYPI,
            provenance=Provenance.PYPI,
            direct=(name in direct_names) if direct_names else True,
            manual_bridge=False,
            raw={"category": pkg.get("category")},
        )

    edges: list = []
    for pkg in packages:
        source_name = _normalize(pkg.get("name", ""))
        if source_name not in nodes:
            continue
        # poetry.lock encodes deps as a mapping under [package.dependencies].
        for dep_name in pkg.get("dependencies", {}):
            target = _normalize(dep_name)
            if target in nodes and target != source_name:
                edges.append(Edge(source=source_name, target=target, via="transitive"))

    if not direct_names:
        logger.warning(
            "poetry.lock %s has no sibling pyproject.toml; all packages marked direct.", lock_path
        )

    roots = [n for n, node in nodes.items() if node.direct]
    return DependencyGraph(
        nodes=nodes,
        edges=edges,
        roots=roots,
        resolution_quality=ResolutionQuality.LOCK_PARSED,
        source_fixation=SourceFixation.PINNED,
    )


def parse_pipfile_lock(lock_path: Path) -> DependencyGraph:
    """Parse a ``Pipfile.lock`` (JSON) into a dependency graph.

    Pipfile.lock stores a flattened closure with no per-package edges, so the
    graph carries pinned nodes and direct/transitive roles (recovered from the
    sibling Pipfile) but no transitive edges.
    """
    with open(lock_path, "rb") as f:
        data = json.load(f)

    direct_names = _read_pipfile_direct(lock_path.parent)

    nodes: dict = {}
    for section in ("default", "develop"):
        for raw_name, info in (data.get(section, {}) or {}).items():
            name = _normalize(raw_name)
            if not name:
                continue
            version = None
            if isinstance(info, dict):
                ver = info.get("version")
                if isinstance(ver, str):
                    version = ver.lstrip("=") or None
            nodes[name] = Node(
                name=name,
                version=version,
                mode=Mode.PYPI,
                provenance=Provenance.PYPI,
                direct=(name in direct_names) if direct_names else True,
                manual_bridge=False,
                raw={"section": section},
            )

    if not direct_names:
        logger.warning(
            "Pipfile.lock %s has no sibling Pipfile; all packages marked direct.", lock_path
        )

    roots = [n for n, node in nodes.items() if node.direct]
    return DependencyGraph(
        nodes=nodes,
        edges=[],
        roots=roots,
        resolution_quality=ResolutionQuality.LOCK_PARSED,
        source_fixation=SourceFixation.PINNED,
    )


# Dispatch table keyed by lower-cased lock file name.
_PARSERS = {
    "uv.lock": parse_uv_lock,
    "poetry.lock": parse_poetry_lock,
    "pipfile.lock": parse_pipfile_lock,
}


def parse_pypi_lock(lock_path: Path) -> DependencyGraph | None:
    """Parse a known PyPI lock file directly.

    Returns ``None`` when the lock format is unsupported *or* the lock carries
    no packages (e.g. an empty/placeholder file), signalling the caller to fall
    back to recompiling the sibling manifest. Raises on a recognised-but-corrupt
    lock so the caller can decide how to recover.
    """
    parser = _PARSERS.get(lock_path.name.lower())
    if parser is None:
        return None
    graph = parser(lock_path)
    if not graph.nodes:
        logger.info("Lock file %s yielded no packages; treating as unusable.", lock_path.name)
        return None
    return graph
