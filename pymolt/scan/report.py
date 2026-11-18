from pathlib import Path

from pydantic import BaseModel, Field

from pymolt.core import stdlib as stdlib_data
from pymolt.discovery.discover import build_surface_map
from pymolt.discovery.models import SurfaceMap
from pymolt.inventory.edges import DependencyInventory, build_inventory
from pymolt.verify.contact_map import ContactMap, StdlibSite, build_contact_map


class ScanReport(BaseModel):
    """The combined as-is picture of a repository (phase 1 output)."""

    root_dir: str
    surfaces: SurfaceMap
    # Dependency-edge inventory per project root (keyed by the root's relative path).
    inventory_by_root: dict[str, DependencyInventory] = Field(default_factory=dict)
    # Where the code calls into third-party dependencies (static, no runtime).
    contact_map: ContactMap | None = None
    # Standard-library names the code uses that the data says were removed (data only, no
    # target probed): sites per removal version, guarded totals and the highest Python on which
    # every unguarded site still exists.
    python_exposure: dict | None = None


_FALLBACK_GUARDS = frozenset({"fallback", "guarded-fallback", "version"})


def _vkey(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in version.split("."))


def _next_minor(version: str) -> str:
    major, minor = (int(p) for p in version.split(".")[:2])
    return "3.0" if major == 2 else f"{major}.{minor + 1}"


def last_present(spelling: str) -> str | None:
    """The last Python version the data says ``spelling`` exists on (None = no known end)."""
    parts = spelling.split(".")
    ends: list[str] = []
    lifetime = stdlib_data.name_lifetime(spelling)
    if lifetime and lifetime[1]:
        ends.append(lifetime[1])
    for i in range(1, len(parts) + 1):
        lifetime = stdlib_data.module_lifetime(".".join(parts[:i]))
        if lifetime and lifetime[1]:
            ends.append(lifetime[1])
    for change in stdlib_data.changes_for(spelling):
        if change.removed_in:
            major, minor = (int(p) for p in change.removed_in.split(".")[:2])
            ends.append(f"{major}.{minor - 1}" if minor else "2.7")
    return min(ends, key=_vkey) if ends else None


def python_exposure(sites: list[StdlibSite]) -> dict:
    """Fold stdlib sites into the scan's ``python_exposure`` (data only: nothing is probed).

    A location (file and line) counts once, however many names it imports. ``by_version`` is
    keyed by the first version the name is gone on; Python-2-only names are ``pre_existing``
    (already gone on every 3.x).
    ``ceiling`` is the highest Python on which every unguarded site still exists.
    """
    by_version: dict[str, set[tuple[str, int]]] = {}
    guarded: set[tuple[str, int]] = set()
    pre_existing: set[tuple[str, int]] = set()
    broad: set[tuple[str, int]] = set()
    ends: list[str] = []
    names: dict[tuple[str, int], dict[str, list[str]]] = {}
    for site in sites:
        end = last_present(site.spelling)
        if end is None:
            continue
        where = (site.file, site.line)
        if _vkey(end) < (3, 0):
            pre_existing.add(where)
        elif site.guard and site.guard["kind"] in _FALLBACK_GUARDS:
            guarded.add(where)
        else:
            if site.guard:
                broad.add(where)
            by_version.setdefault(_next_minor(end), set()).add(where)
            ends.append(end)
            names.setdefault(where, {}).setdefault(_next_minor(end), []).append(site.spelling)
    unguarded = [
        f"{file}:{line} {', '.join(spellings)} (gone in {gone})"
        for (file, line), by_gone in names.items()
        for gone, spellings in by_gone.items()
    ]
    return {
        "by_version": {v: len(by_version[v]) for v in sorted(by_version, key=_vkey)},
        "sites_total": sum(len(w) for w in by_version.values()),
        "guarded_total": len(guarded),
        "pre_existing_total": len(pre_existing),
        "broad_guard_total": len(broad),
        "ceiling": min(ends, key=_vkey) if ends else None,
        "basis": "knowledge",
        "unguarded_sites": unguarded,
    }


def run_scan(project_dir: str | Path, *, contacts: bool = True) -> ScanReport:
    """Discover surfaces, inventory dependency edges per project root, and map
    where the code calls into its dependencies (``contacts``)."""
    repo_root = Path(project_dir).resolve()
    surfaces = build_surface_map(repo_root)

    inventory_by_root: dict[str, DependencyInventory] = {}
    for root in surfaces.project_roots:
        root_path = repo_root if root.path == "." else repo_root / root.path
        inventory_by_root[root.path] = build_inventory(root_path)

    contact_map = build_contact_map(repo_root) if contacts else None
    return ScanReport(
        root_dir=str(repo_root),
        surfaces=surfaces,
        inventory_by_root=inventory_by_root,
        contact_map=contact_map,
        python_exposure=python_exposure(contact_map.stdlib_sites) if contact_map else None,
    )
