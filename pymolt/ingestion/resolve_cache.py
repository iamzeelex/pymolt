"""Content-addressed cache for resolved dependency graphs.

Resolving a manifest (``uv pip compile``, a conda solve, a legacy pip-tools run)
is the slow part of an audit. The result is a pure function of the resolution
inputs — the manifest/lock bytes, the Python version, the constraints, the
container, and whether we recompiled — so we cache the resolved
:class:`DependencyGraph` keyed by a hash of exactly those inputs.

The cached graph is stored *before* name-mapping enrichment so re-enrichment
(cheap, offline) always runs fresh. A TTL guards against stale results from
non-deterministic live solves; callers can also bypass the cache entirely.
"""

import hashlib
import logging
import time
from pathlib import Path

from pymolt.core.graph import DependencyGraph

logger = logging.getLogger(__name__)

# Resolved graphs older than this are ignored (live solves can drift even when
# the manifest is unchanged, e.g. a moving conda channel).
CACHE_TTL_SECONDS = 24 * 3600


def _hash_file(h: "hashlib._Hash", path: Path | None) -> None:
    if not path:
        return
    try:
        h.update(Path(path).read_bytes())
    except OSError:
        pass


def compute_key(
    source_path: Path,
    base_python: str | None,
    container_id: str | None,
    force_recompile: bool,
    constraint_file: Path | None = None,
    extra_path: Path | None = None,
    tool: str | None = None,
) -> str:
    """Derive a stable cache key from everything that affects the resolution."""
    h = hashlib.sha256()
    h.update(Path(source_path).name.encode())
    _hash_file(h, source_path)
    _hash_file(h, extra_path)  # e.g. the conda-lock alongside an environment.yml
    _hash_file(h, constraint_file)
    h.update((base_python or "").encode())
    h.update((container_id or "").encode())
    h.update((tool or "").encode())  # different resolvers yield different graphs
    h.update(b"1" if force_recompile else b"0")
    return h.hexdigest()


def _cache_path(cache_root: Path, key: str) -> Path:
    return Path(cache_root) / ".pymolt" / "cache" / "resolve" / f"{key}.json"


def load(cache_root: Path, key: str) -> DependencyGraph | None:
    """Return the cached graph for ``key`` if present and within the TTL."""
    path = _cache_path(cache_root, key)
    if not path.is_file():
        return None
    try:
        if time.time() - path.stat().st_mtime > CACHE_TTL_SECONDS:
            return None
        return DependencyGraph.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.debug("Ignoring unreadable resolve cache %s: %s", path, e)
        return None


def store(cache_root: Path, key: str, graph: DependencyGraph) -> None:
    """Persist a resolved graph (best-effort; never raises)."""
    path = _cache_path(cache_root, key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(graph.model_dump_json(), encoding="utf-8")
    except OSError as e:
        logger.debug("Could not write resolve cache %s: %s", path, e)
