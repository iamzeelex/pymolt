"""Assemble per-package migration risk from CVE, wheel and recency signals."""

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx

from pymolt.adapters import pypi_metadata
from pymolt.core.enums import RiskTier
from pymolt.core.graph import DependencyGraph
from pymolt.risk import cache, osv
from pymolt.risk.models import CveFinding, PackageRisk, RiskReport

logger = logging.getLogger(__name__)

CACHE_TTL = 24 * 3600
ABANDONED_DAYS = 365 * 2  # no release in 2 years -> flagged as likely abandoned

_TIER_ORDER = {RiskTier.LOW: 0, RiskTier.MEDIUM: 1, RiskTier.HIGH: 2}


def _raise_tier(current: RiskTier, candidate: RiskTier) -> RiskTier:
    return candidate if _TIER_ORDER[candidate] > _TIER_ORDER[current] else current


def _cp_tag(target_python: str | None) -> str | None:
    """'3.12' -> 'cp312' (the CPython wheel ABI tag), or None."""
    if not target_python:
        return None
    match = re.match(r"^(\d+)\.(\d+)", target_python)
    return f"cp{match.group(1)}{match.group(2)}" if match else None


def _classify_wheels(files: list[dict], py_tag: str | None) -> tuple[str, bool]:
    """Return (wheel_status, needs_compilation) for a release's file list."""
    wheels = [f.get("filename", "") for f in files if f.get("packagetype") == "bdist_wheel"]
    has_sdist = any(f.get("packagetype") == "sdist" for f in files)
    if not wheels and not has_sdist:
        return "unknown", False
    if any(fn.endswith("-none-any.whl") for fn in wheels):
        return "pure-wheel", False  # platform-independent: no build needed
    if py_tag and any(py_tag in fn for fn in wheels):
        return "target-wheel", False
    if wheels:
        return "no-target-wheel", True  # binary wheels exist but none for target -> build from sdist
    return "sdist-only", True


def _last_release_iso(pkg_json: dict) -> str | None:
    times: list[str] = []
    for files in (pkg_json.get("releases") or {}).values():
        for f in files:
            t = f.get("upload_time_iso_8601") or f.get("upload_time")
            if t:
                times.append(t)
    if not times:
        for f in pkg_json.get("urls", []):
            t = f.get("upload_time_iso_8601") or f.get("upload_time")
            if t:
                times.append(t)
    return max(times) if times else None


def _days_since(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).days
    except ValueError:
        return None


def _cached_vulns(cache_root, name, version, client) -> list[dict] | None:
    key = f"{name}@{version}"
    hit = cache.read(cache_root, "osv", key, CACHE_TTL)
    if hit is not None:
        return hit
    vulns = osv.query_vulns(name, version, client)
    if vulns is not None:
        cache.write(cache_root, "osv", key, vulns)
    return vulns


def _cached_release(cache_root, name, version, client) -> dict | None:
    key = f"{name}@{version}"
    hit = cache.read(cache_root, "pypi_release", key, CACHE_TTL)
    if hit is not None:
        return hit
    data = pypi_metadata.fetch_release_json(name, version, client)
    if data is not None:
        cache.write(cache_root, "pypi_release", key, data)
    return data


def _cached_package(cache_root, name, client) -> dict | None:
    hit = cache.read(cache_root, "pypi_pkg", name, CACHE_TTL)
    if hit is not None:
        return hit
    data = pypi_metadata.fetch_package_json(name, client)
    if data is not None:
        cache.write(cache_root, "pypi_pkg", name, data)
    return data


def _assess_one(name, b_node, t_node, py_tag, cache_root, client) -> tuple[PackageRisk, bool]:
    """Build risk for one package. Second tuple item flags a data-source failure."""
    baseline_version = b_node.version if b_node else None
    target_version = (t_node.version if t_node else None) or baseline_version
    risk = PackageRisk(name=name, baseline_version=baseline_version, target_version=target_version)

    # Unpinned packages have no concrete version to query.
    if not target_version:
        risk.reasons.append("unpinned — not assessed")
        return risk, False

    had_error = False

    # 1. CVEs (delta: which the migration clears, which remain open on the target).
    target_vulns = _cached_vulns(cache_root, name, target_version, client)
    if target_vulns is None:
        had_error = True
    else:
        risk.open_cves = [CveFinding(**osv.extract_finding(v)) for v in target_vulns]
    if baseline_version and baseline_version != target_version:
        base_vulns = _cached_vulns(cache_root, name, baseline_version, client)
        if base_vulns is None:
            had_error = True
        elif target_vulns is not None:
            open_ids = {c.id for c in risk.open_cves}
            risk.fixed_by_migration = [
                v.get("id", "?") for v in base_vulns if v.get("id") not in open_ids
            ]

    # 2. Wheel / compilation status of the version you would run.
    release = _cached_release(cache_root, name, target_version, client)
    if release is None:
        had_error = True
    else:
        risk.wheel_status, risk.needs_compilation = _classify_wheels(
            release.get("urls", []), py_tag
        )

    # 3. Abandonment (last release recency).
    pkg = _cached_package(cache_root, name, client)
    if pkg is None:
        had_error = True
    else:
        risk.last_release = _last_release_iso(pkg)
        risk.days_since_release = _days_since(risk.last_release)
        risk.abandoned = bool(risk.days_since_release and risk.days_since_release > ABANDONED_DAYS)

    _score(risk)
    return risk, had_error


def _score(risk: PackageRisk) -> None:
    tier = RiskTier.LOW
    if risk.open_cves:
        tier = _raise_tier(tier, RiskTier.HIGH)
        risk.reasons.append(
            f"{len(risk.open_cves)} open CVE(s) still affect {risk.target_version}"
        )
    if risk.needs_compilation:
        tier = _raise_tier(tier, RiskTier.MEDIUM)
        risk.reasons.append(
            "no binary wheel for target Python — builds from source (C toolchain)"
            if risk.wheel_status == "no-target-wheel"
            else "sdist-only — builds from source (C toolchain)"
        )
    if risk.abandoned:
        tier = _raise_tier(tier, RiskTier.MEDIUM)
        risk.reasons.append(f"no release in ~{risk.days_since_release // 365} years")
    if risk.fixed_by_migration:
        risk.reasons.append(f"migration clears {len(risk.fixed_by_migration)} CVE(s)")
    if not risk.reasons:
        risk.reasons.append("no known issues")
    risk.tier = tier


def assess_risk(
    baseline_graph: DependencyGraph,
    target_graph: DependencyGraph | None,
    target_python: str | None,
    cache_root: Path,
    timeout: float = 10.0,
) -> RiskReport:
    """Assess every package in the union of the baseline/target graphs.

    Network-backed (OSV + PyPI) but cached on disk; failures degrade per-package
    rather than aborting. Returns a :class:`RiskReport`.
    """
    names = sorted(set(baseline_graph.nodes) | set(target_graph.nodes if target_graph else []))
    py_tag = _cp_tag(target_python)
    report = RiskReport(target_python=target_python)

    with httpx.Client(timeout=timeout, headers={"User-Agent": "pymolt"}) as client:
        for name in names:
            b = baseline_graph.nodes.get(name)
            t = target_graph.nodes.get(name) if target_graph else None
            risk, had_error = _assess_one(name, b, t, py_tag, cache_root, client)
            report.packages.append(risk)
            if not risk.target_version:
                report.skipped += 1
            else:
                report.assessed += 1
            if had_error:
                report.errors += 1

    return report
