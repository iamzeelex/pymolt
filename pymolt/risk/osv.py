"""Thin client for the OSV.dev vulnerability database (free, no auth)."""

import logging

import httpx

logger = logging.getLogger(__name__)

OSV_QUERY_URL = "https://api.osv.dev/v1/query"


def query_vulns(name: str, version: str, client: httpx.Client) -> list[dict] | None:
    """Return raw OSV vuln records affecting ``(PyPI name, version)``.

    Returns an empty list when the version is known-clean, or ``None`` when OSV
    could not be reached (so the caller can flag the package as unassessed).
    """
    try:
        resp = client.post(
            OSV_QUERY_URL,
            json={"package": {"ecosystem": "PyPI", "name": name}, "version": version},
        )
    except httpx.HTTPError as e:
        logger.info("OSV query failed for %s@%s: %s", name, version, e)
        return None
    if resp.status_code != 200:
        logger.info("OSV query for %s@%s returned HTTP %s", name, version, resp.status_code)
        return None
    try:
        return resp.json().get("vulns", []) or []
    except ValueError:
        return None


def extract_finding(vuln: dict) -> dict:
    """Reduce a raw OSV record to ``{id, severity, fixed_version, summary}``."""
    fixed = None
    for affected in vuln.get("affected", []):
        for rng in affected.get("ranges", []):
            for event in rng.get("events", []):
                if "fixed" in event:
                    fixed = event["fixed"]
                    break
            if fixed:
                break
        if fixed:
            break

    severity = None
    sev = vuln.get("severity")
    if isinstance(sev, list) and sev:
        severity = sev[0].get("score")

    return {
        "id": vuln.get("id", "?"),
        "severity": severity,
        "fixed_version": fixed,
        "summary": vuln.get("summary"),
    }
