"""Keep machine-local paths out of benchmark results that get committed.

``scrub`` rewrites the repo root to a repo-relative path and the home directory to
``~`` in every string of a JSON-like value (dict keys included). Call it on a
results object, or on rendered text, right before it is written. Stdlib only.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _spellings(path: Path) -> list[str]:
    seen = {str(path), os.path.realpath(path)}
    return sorted((s.rstrip("/") for s in seen if s.rstrip("/")), key=len, reverse=True)


# Not followed by a path-name character, so /a/repo does not match /a/repo-old.
_END = r"(?![\w.-])"
_RULES = [(re.compile(re.escape(s) + "/"), "") for s in _spellings(REPO)]
_RULES += [(re.compile(re.escape(s) + _END), ".") for s in _spellings(REPO)]
_RULES += [(re.compile(re.escape(s) + _END), "~") for s in _spellings(Path.home())]


def scrub(obj):
    """Return ``obj`` with the repo root made relative and the home directory as ``~``."""
    if isinstance(obj, os.PathLike):
        obj = os.fspath(obj)
    if isinstance(obj, str):
        for pattern, repl in _RULES:
            obj = pattern.sub(repl, obj)
        return obj
    if isinstance(obj, dict):
        return {scrub(k): scrub(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(scrub(v) for v in obj)
    return obj
