"""Pure helpers for Python-version discovery and end-of-life classification.

Shared by the setup service, the CLI and (through the service) the TUI. No
prompting or rich markup lives here — callers format the returned data
themselves.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import urllib.request
from datetime import datetime

logger = logging.getLogger(__name__)

# Resolver floor: uv/pip-tools support down to 3.7.
_RESOLVER_FLOOR = [3, 7]

_EOL_FALLBACK = {
    "2.7": "2020-01-01",
    "3.0": "2009-06-27",
    "3.1": "2012-04-09",
    "3.2": "2016-02-20",
    "3.3": "2017-09-29",
    "3.4": "2019-03-18",
    "3.5": "2020-09-30",
    "3.6": "2021-12-23",
    "3.7": "2023-06-27",
    "3.8": "2024-10-07",
    "3.9": "2025-10-31",
    "3.10": "2026-10-31",
    "3.11": "2027-10-31",
    "3.12": "2028-10-31",
    "3.13": "2029-10-31",
    "3.14": "2030-10-31",
}


def version_floor(python_str: str | None) -> list[int]:
    """Lowest selectable target as ``[major, minor]``: the project's own version,
    but never below the resolver floor (3.7)."""
    try:
        floor = (
            [int(p) for p in python_str.split(".")][:2]
            if python_str
            else [sys.version_info.major, sys.version_info.minor]
        )
    except (ValueError, AttributeError):
        floor = [sys.version_info.major, sys.version_info.minor]
    return floor if floor >= _RESOLVER_FLOOR else list(_RESOLVER_FLOOR)


def get_eol_versions() -> dict[str, str]:
    """Fetch Python EOL dates from endoflife.date, falling back to a bundled map."""
    try:
        req = urllib.request.Request(
            "https://endoflife.date/api/python.json",
            headers={"User-Agent": "pymolt"},
        )
        with urllib.request.urlopen(req, timeout=2) as response:
            data = json.loads(response.read().decode())
            res = {}
            for item in data:
                cycle = item.get("cycle")
                eol = item.get("eol")
                if cycle and eol:
                    res[cycle] = str(eol)
            return res or dict(_EOL_FALLBACK)
    except Exception as e:
        logger.debug("EOL lookup failed, using fallback: %s", e)
        return dict(_EOL_FALLBACK)


def get_available_python_versions() -> list[str]:
    """List available CPython ``major.minor`` versions via uv, with a fallback."""
    try:
        res = subprocess.run(
            ["uv", "python", "list"], capture_output=True, text=True, check=True
        )
        versions = set()
        for line in res.stdout.splitlines():
            match = re.search(r"cpython-(\d+\.\d+)", line)
            if match:
                versions.add(match.group(1))
        if versions:
            return sorted(versions, key=lambda x: [int(p) for p in x.split(".")])
    except Exception as e:
        logger.debug("uv python list failed, using fallback: %s", e)
    return ["3.9", "3.10", "3.11", "3.12", "3.13", "3.14"]


def bundled_eol_map() -> dict[str, str]:
    """The offline-bundled Python EOL table (no network) — for scan/recon."""
    return dict(_EOL_FALLBACK)


def classify_eol(version: str, eol_map: dict[str, str]) -> tuple[str | None, str]:
    """Return ``(eol_date, status)`` where status is supported|soon|eol|unknown."""
    match = re.match(r"^(\d+\.\d+)", version)
    if not match:
        return None, "unknown"
    eol_date = eol_map.get(match.group(1))
    if not eol_date:
        return None, "unknown"
    today = datetime.now().strftime("%Y-%m-%d")
    if eol_date < today:
        return eol_date, "eol"
    try:
        delta = (datetime.strptime(eol_date, "%Y-%m-%d") - datetime.now()).days
        if delta <= 180:
            return eol_date, "soon"
    except ValueError:
        pass
    return eol_date, "supported"
