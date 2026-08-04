"""Detect the frameworks a project declares, to query /succession.

Returns the project's DIRECT dependency names (from its manifest). We send the full
direct set and let the server match the ones it has succession knowledge for — the
catalog authority stays server-side, so pymolt needs no hardcoded framework list.
"""

from __future__ import annotations

from pathlib import Path

from pymolt.ingestion.detect import detect_sources, extract_declared_requirements


def detect_frameworks(project_dir: str | Path) -> list[str]:
    """Direct dependency names declared by the project (sorted, de-duplicated)."""
    names: set[str] = set()
    for source in detect_sources(project_dir):
        try:
            names.update(extract_declared_requirements(source.path))
        except Exception:  # a manifest we can't parse must not sink detection
            continue
    return sorted(names)
