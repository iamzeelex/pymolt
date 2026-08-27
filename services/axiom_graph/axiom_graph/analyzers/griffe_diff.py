"""
axiom_graph/analyzers/griffe_diff.py

Structural API diff using griffe.
Wraps griffe.find_breaking_changes and maps each BreakageKind
to a ChangeRisk tier (MECHANICAL / BEHAVIORAL / STRUCTURAL).

This is the "always-on" Level-0 contour: deterministic, no network
after the initial wheel download, no LLM.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import platformdirs

log = logging.getLogger(__name__)

try:
    import griffe
    from griffe import BreakageKind, LoadingError
    try:
        import griffe._internal.merger as _merger
        _orig_merge_overload = _merger._merge_overload_annotations

        def _safe_merge_overload_annotations(function, overloads):
            if not hasattr(function, "parameters"):
                return
            return _orig_merge_overload(function, overloads)

        _merger._merge_overload_annotations = _safe_merge_overload_annotations
    except Exception:
        pass
except ImportError as exc:
    raise ImportError("griffe is required: pip install 'griffe[pypi]'") from exc

from axiom_graph.core.models import ChangeRisk


# ---------------------------------------------------------------------------
# Risk tier mapping (griffe BreakageKind → ChangeRisk)
# ---------------------------------------------------------------------------

RISK_TIERS: dict[BreakageKind, ChangeRisk] = {
    # Mechanical — high-confidence automated codemod
    BreakageKind.PARAMETER_MOVED:            ChangeRisk.MECHANICAL,
    BreakageKind.PARAMETER_CHANGED_KIND:     ChangeRisk.MECHANICAL,
    BreakageKind.OBJECT_CHANGED_KIND:        ChangeRisk.MECHANICAL,
    # Behavioral — fixable but must be test-verified
    BreakageKind.PARAMETER_CHANGED_DEFAULT:  ChangeRisk.BEHAVIORAL,
    BreakageKind.PARAMETER_CHANGED_REQUIRED: ChangeRisk.BEHAVIORAL,
    BreakageKind.PARAMETER_ADDED_REQUIRED:   ChangeRisk.BEHAVIORAL,
    BreakageKind.RETURN_CHANGED_TYPE:        ChangeRisk.BEHAVIORAL,
    BreakageKind.ATTRIBUTE_CHANGED_TYPE:     ChangeRisk.BEHAVIORAL,
    BreakageKind.ATTRIBUTE_CHANGED_VALUE:    ChangeRisk.BEHAVIORAL,
    # Structural — may require architectural redesign
    BreakageKind.PARAMETER_REMOVED:          ChangeRisk.STRUCTURAL,
    BreakageKind.OBJECT_REMOVED:             ChangeRisk.STRUCTURAL,
    BreakageKind.CLASS_REMOVED_BASE:         ChangeRisk.STRUCTURAL,
}

_TIER_ORDER = {ChangeRisk.STRUCTURAL: 0, ChangeRisk.BEHAVIORAL: 1, ChangeRisk.MECHANICAL: 2}


# ---------------------------------------------------------------------------
# Raw result dataclass (pre-fusion, no enrichment yet)
# ---------------------------------------------------------------------------

@dataclass
class RawBreakingChange:
    kind: str
    path: str
    risk: ChangeRisk
    explanation: str
    location: str | None = None
    deprecation_hint: str | None = None  # from griffe docstring scan


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_docstring_hint(member: Any) -> str | None:
    """Quick scan of griffe member docstring for a deprecation sentence."""
    try:
        if member and member.docstring:
            doc = member.docstring.value
            if "deprecated" in doc.lower():
                match = re.search(r"([^.]*deprecated[^.]*\.)", doc, re.IGNORECASE)
                if match:
                    return match.group(1).strip()
                return "Deprecated"
    except Exception:
        pass
    return None


def _get_all_public_members(module: Any, prefix: str = "") -> dict[str, Any]:
    """Recursively collect all public members as {dotted.path: member}."""
    members: dict[str, Any] = {}
    try:
        for name, member in module.members.items():
            if name.startswith("_") and not name.startswith("__"):
                continue
            path = f"{prefix}.{name}" if prefix else name
            members[path] = member
            if isinstance(member, (griffe.Module, griffe.Class)):
                try:
                    members.update(_get_all_public_members(member, path))
                except Exception:
                    pass
    except Exception:
        pass
    return members


def _find_name_transitions(
    old_members: dict[str, Any],
    new_members: dict[str, Any],
) -> list[dict]:
    """
    Detect renamed/moved symbols by exact name match across different paths.
    Returns list of {"from_path", "to_path", "confidence", "reason"}.
    """
    # Only care about items that disappeared from old and appeared in new
    old_removed = {
        obj_path: member
        for obj_path, member in old_members.items()
        if obj_path not in new_members
    }
    new_added = {
        obj_path: member
        for obj_path, member in new_members.items()
        if obj_path not in old_members
    }

    # Index by unqualified name
    old_by_name: dict[str, list[str]] = {}
    for path in old_removed:
        name = path.split(".")[-1]
        old_by_name.setdefault(name, []).append(path)

    new_by_name: dict[str, list[str]] = {}
    for path in new_added:
        name = path.split(".")[-1]
        new_by_name.setdefault(name, []).append(path)

    transitions = []
    for name, old_paths in old_by_name.items():
        new_paths = new_by_name.get(name, [])
        if len(old_paths) == 1 and len(new_paths) == 1:
            transitions.append({
                "from_path": old_paths[0],
                "to_path": new_paths[0],
                "confidence": "high",
                "reason": "exact name match after path change",
            })
    return transitions


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def _ensure_griffe_cache(package: str, version: str) -> None:
    """Ensure griffe has the wheel or sdist cached for offline analysis."""
    import io
    import json
    import os
    import tarfile
    import time
    import urllib.request
    import zipfile
    import platformdirs

    cache_dir = Path(platformdirs.user_cache_dir("griffe"))
    install_dir = cache_dir / f"{package}=={version}"
    if install_dir.exists():
        return

    log.info("Pre-caching package for griffe: %s==%s", package, version)
    tmp_dir = cache_dir / f".tmp_{package}_{version}_{os.getpid()}_{time.time_ns()}"
    try:
        url = f"https://pypi.org/pypi/{package}/{version}/json"
        req = urllib.request.Request(url, headers={"User-Agent": "axiom-graph/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())

        urls_list = data.get("urls", [])
        wheels = [f for f in urls_list if f.get("filename", "").endswith(".whl")]
        sdists = [f for f in urls_list if f.get("packagetype") == "sdist" or f.get("filename", "").endswith((".tar.gz", ".zip"))]

        tmp_dir.mkdir(parents=True, exist_ok=True)

        if wheels:
            # Prefer pure-python wheel, else smallest wheel by size
            pure_python = [w for w in wheels if "none-any" in w.get("filename", "")]
            chosen_wheel = pure_python[0] if pure_python else min(wheels, key=lambda w: w.get("size", 10**12))
            wheel_url = chosen_wheel["url"]

            req_whl = urllib.request.Request(wheel_url, headers={"User-Agent": "axiom-graph/1.0"})
            with urllib.request.urlopen(req_whl, timeout=120) as resp:
                zip_data = resp.read()

            with zipfile.ZipFile(io.BytesIO(zip_data)) as z:
                z.extractall(tmp_dir)

            # Check if meta-package without code (e.g. tensorflow without backend)
            top_mods = [
                d for d in tmp_dir.iterdir()
                if d.is_dir() and not d.name.endswith((".dist-info", ".egg-info"))
            ]
            if not top_mods and package.lower() in ("tensorflow", "jax"):
                # Try sibling distribution (e.g. tensorflow-cpu)
                sibling_pkg = f"{package}-cpu" if package.lower() == "tensorflow" else "jaxlib"
                try:
                    s_url = f"https://pypi.org/pypi/{sibling_pkg}/{version}/json"
                    s_req = urllib.request.Request(s_url, headers={"User-Agent": "axiom-graph/1.0"})
                    with urllib.request.urlopen(s_req, timeout=15) as s_resp:
                        s_data = json.loads(s_resp.read().decode())
                    s_wheels = [
                        f for f in s_data.get("urls", [])
                        if f.get("filename", "").endswith(".whl") and f.get("size", 0) > 10 * 1024 * 1024
                    ]
                    if s_wheels:
                        s_chosen = min(s_wheels, key=lambda w: w.get("size", 10**12))
                        s_req_whl = urllib.request.Request(s_chosen["url"], headers={"User-Agent": "axiom-graph/1.0"})
                        with urllib.request.urlopen(s_req_whl, timeout=120) as s_resp2:
                            s_zip = s_resp2.read()
                        with zipfile.ZipFile(io.BytesIO(s_zip)) as z2:
                            z2.extractall(tmp_dir)
                except Exception as s_exc:
                    log.debug("Sibling package resolution failed for %s==%s: %s", sibling_pkg, version, s_exc)

        elif sdists:
            sdist_url = sdists[0]["url"]
            req_sdist = urllib.request.Request(sdist_url, headers={"User-Agent": "axiom-graph/1.0"})
            with urllib.request.urlopen(req_sdist, timeout=120) as resp:
                sdist_data = resp.read()

            if sdist_url.endswith(".zip"):
                with zipfile.ZipFile(io.BytesIO(sdist_data)) as z:
                    z.extractall(tmp_dir)
            else:
                with tarfile.open(fileobj=io.BytesIO(sdist_data), mode="r:gz") as tar:
                    members = [m for m in tar.getmembers() if not m.name.startswith("/") and ".." not in m.name]
                    tar.extractall(path=tmp_dir, members=members)

            # Move contents of extracted top-level subfolder if sdist extracted into a subfolder
            subdirs = [d for d in tmp_dir.iterdir() if d.is_dir()]
            if len(subdirs) == 1 and not (tmp_dir / "__init__.py").exists():
                inner = subdirs[0]
                for item in inner.iterdir():
                    target = tmp_dir / item.name
                    if not target.exists():
                        item.rename(target)

        if not install_dir.exists():
            try:
                tmp_dir.rename(install_dir)
            except OSError:
                pass
    except Exception as exc:
        log.warning("griffe package pre-cache failed for %s==%s: %s", package, version, exc)
    finally:
        if tmp_dir.exists():
            import shutil
            shutil.rmtree(tmp_dir, ignore_errors=True)


def griffe_diff(
    package: str,
    old_v: str,
    new_v: str,
    *,
    allow_inspection: bool = False,
) -> tuple[list[RawBreakingChange], list[dict]]:
    """
    Compute structural API diff between two versions using griffe.

    Returns:
        (changes, transitions) where:
        - changes: list of RawBreakingChange sorted by risk (structural first)
        - transitions: list of rename/move dicts detected via name-matching
    """
    cache_dir = Path(platformdirs.user_cache_dir("griffe"))
    cache_dir.mkdir(parents=True, exist_ok=True)

    _ensure_griffe_cache(package, old_v)
    _ensure_griffe_cache(package, new_v)

    old_dir = cache_dir / f"{package}=={old_v}"
    new_dir = cache_dir / f"{package}=={new_v}"

    try:
        # Discover top-level python module in extracted wheel
        pkg_norm = package.replace("-", "_").lower()
        old_mods = [
            d.name for d in old_dir.iterdir()
            if d.is_dir() and not d.name.endswith((".dist-info", ".egg-info")) and not d.name.startswith((".", "_"))
        ] if old_dir.exists() else []
        if pkg_norm in old_mods:
            module_name = pkg_norm
        else:
            module_name = old_mods[0] if old_mods else pkg_norm

        old_api = griffe.load(
            module_name,
            search_paths=[old_dir],
            allow_inspection=allow_inspection,
            resolve_aliases=False,
        )
        new_api = griffe.load(
            module_name,
            search_paths=[new_dir],
            allow_inspection=allow_inspection,
            resolve_aliases=False,
        )
    except (LoadingError, Exception) as exc:
        log.warning("griffe.load failed for %s %s→%s: %s", package, old_v, new_v, exc)
        return [], []

    old_members = _get_all_public_members(old_api, package)
    new_members = _get_all_public_members(new_api, package)
    transitions = _find_name_transitions(old_members, new_members)

    changes: list[RawBreakingChange] = []
    for b in griffe.find_breaking_changes(old_api, new_api):
        risk = RISK_TIERS.get(b.kind, ChangeRisk.BEHAVIORAL)
        raw = b.as_dict()
        path = raw.get("object_path") or getattr(b, "_canonical_path", "")

        location = None
        try:
            if b._relative_filepath:
                location = str(b._relative_filepath)
        except Exception:
            pass

        hint = old_members.get(path) and _extract_docstring_hint(old_members[path])

        changes.append(RawBreakingChange(
            kind=b.kind.value,
            path=path,
            risk=risk,
            explanation=b._explain_oneline(),
            location=location,
            deprecation_hint=hint or None,
        ))

    changes.sort(key=lambda c: (_TIER_ORDER.get(c.risk, 9), c.path))
    return changes, transitions
