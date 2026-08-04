"""Small on-disk JSON cache for network risk lookups.

Keeps the opt-in ``--risk`` step from re-hitting OSV/PyPI on every run and
honours the project's offline-first posture (cached answers work without a
network). Stored under ``<project>/.pymolt_cache/risk/<namespace>/``.
"""

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _path(cache_root: Path, namespace: str, key: str) -> Path:
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    return Path(cache_root) / ".pymolt_cache" / "risk" / namespace / f"{digest}.json"


def read(cache_root: Path, namespace: str, key: str, ttl: float) -> Any | None:
    """Return a cached value if present and within ``ttl`` seconds, else None."""
    path = _path(cache_root, namespace, key)
    if not path.is_file():
        return None
    try:
        if time.time() - path.stat().st_mtime > ttl:
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.debug("Ignoring unreadable risk cache %s: %s", path, e)
        return None


def write(cache_root: Path, namespace: str, key: str, obj: Any) -> None:
    """Persist ``obj`` as JSON (best-effort; never raises)."""
    path = _path(cache_root, namespace, key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj), encoding="utf-8")
    except (OSError, TypeError) as e:
        logger.debug("Could not write risk cache %s: %s", path, e)
