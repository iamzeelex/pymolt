"""
axiom_graph/sources/pypi_releases.py

Fetches the ordered list of release versions for a package from PyPI JSON API.
Filters pre-releases by default, sorts via packaging.version.Version.
"""

from __future__ import annotations

import httpx
from packaging.version import Version, InvalidVersion


_PYPI_BASE = "https://pypi.org/pypi"


def _parse_version_safe(v: str) -> Version | None:
    try:
        return Version(v)
    except InvalidVersion:
        return None


def fetch_pypi_metadata(package: str) -> dict:
    """
    Fetch the full PyPI JSON metadata for a package.
    Returns the parsed JSON dict.
    Raises httpx.HTTPStatusError on 4xx/5xx.
    """
    url = f"{_PYPI_BASE}/{package}/json"
    response = httpx.get(url, headers={"User-Agent": "axiom-graph/1.0"}, timeout=15)
    response.raise_for_status()
    return response.json()


def fetch_ordered_releases(
    package: str,
    from_v: str,
    to_v: str,
    *,
    include_prereleases: bool = False,
) -> list[str]:
    """
    Fetch all release versions of `package` from PyPI and return the ordered
    subset that falls in the half-open interval (from_v, to_v].

    Args:
        package: PyPI package name (e.g. "pandas").
        from_v:  Start version (exclusive). The base we're migrating FROM.
        to_v:    End version (inclusive). The target we're migrating TO.
        include_prereleases: If True, alpha/beta/rc releases are included.

    Returns:
        Sorted list of version strings, e.g. ["1.4.0", "1.4.1", ..., "2.0.0"].
        from_v itself is NOT included; to_v IS included.

    Raises:
        httpx.HTTPStatusError: If PyPI returns a non-2xx response.
        ValueError: If from_v or to_v are not valid version strings.
    """
    from_ver = Version(from_v)
    to_ver = Version(to_v)

    if from_ver >= to_ver:
        raise ValueError(
            f"from_v ({from_v!r}) must be strictly less than to_v ({to_v!r})"
        )

    data = fetch_pypi_metadata(package)
    raw_releases: dict[str, list] = data.get("releases", {})

    versions: list[Version] = []
    for v_str in raw_releases:
        parsed = _parse_version_safe(v_str)
        if parsed is None:
            continue
        if not include_prereleases and parsed.is_prerelease:
            continue
        # Half-open interval: (from_ver, to_ver]
        if from_ver < parsed <= to_ver:
            versions.append(parsed)

    versions.sort()
    return [str(v) for v in versions]


def fetch_project_urls(package: str) -> dict[str, str]:
    """
    Return the project_urls dict from PyPI metadata.
    Used by git_releases.py to discover the source repository URL.

    Example return:
        {
            "Source": "https://github.com/pandas-dev/pandas",
            "Homepage": "https://pandas.pydata.org",
        }
    """
    data = fetch_pypi_metadata(package)
    info = data.get("info", {})
    urls: dict[str, str] = {}
    if project_urls := info.get("project_urls"):
        urls.update(project_urls)
    if home_page := info.get("home_page"):
        urls.setdefault("Homepage", home_page)
    return urls
