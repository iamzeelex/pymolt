"""PyPI JSON API adapter — wheel/compilation and release-recency signals.

Pure HTTP fetchers (no caching here; the risk layer wraps these with its
on-disk cache). Each returns the parsed JSON dict or ``None`` on any failure.
"""

import logging

import httpx

logger = logging.getLogger(__name__)

PYPI_PACKAGE_URL = "https://pypi.org/pypi/{name}/json"
PYPI_RELEASE_URL = "https://pypi.org/pypi/{name}/{version}/json"


def _get_json(url: str, client: httpx.Client) -> dict | None:
    try:
        resp = client.get(url)
    except httpx.HTTPError as e:
        logger.info("PyPI request failed (%s): %s", url, e)
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def fetch_release_json(name: str, version: str, client: httpx.Client) -> dict | None:
    """Return the PyPI JSON for a specific release (has the per-file ``urls``)."""
    return _get_json(PYPI_RELEASE_URL.format(name=name, version=version), client)


def fetch_package_json(name: str, client: httpx.Client) -> dict | None:
    """Return the package-level PyPI JSON (all releases + upload times)."""
    return _get_json(PYPI_PACKAGE_URL.format(name=name), client)
