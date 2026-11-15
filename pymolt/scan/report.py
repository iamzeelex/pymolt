from pathlib import Path

from pydantic import BaseModel, Field

from pymolt.discovery.discover import build_surface_map
from pymolt.discovery.models import SurfaceMap
from pymolt.inventory.edges import DependencyInventory, build_inventory
from pymolt.verify.contact_map import ContactMap, build_contact_map


class ScanReport(BaseModel):
    """The combined as-is picture of a repository (phase 1 output)."""

    root_dir: str
    surfaces: SurfaceMap
    # Dependency-edge inventory per project root (keyed by the root's relative path).
    inventory_by_root: dict[str, DependencyInventory] = Field(default_factory=dict)
    # Where the code calls into third-party dependencies (static, no runtime).
    contact_map: ContactMap | None = None


def run_scan(project_dir: str | Path, *, contacts: bool = True) -> ScanReport:
    """Discover surfaces, inventory dependency edges per project root, and map
    where the code calls into its dependencies (``contacts``)."""
    repo_root = Path(project_dir).resolve()
    surfaces = build_surface_map(repo_root)

    inventory_by_root: dict[str, DependencyInventory] = {}
    for root in surfaces.project_roots:
        root_path = repo_root if root.path == "." else repo_root / root.path
        inventory_by_root[root.path] = build_inventory(root_path)

    return ScanReport(
        root_dir=str(repo_root),
        surfaces=surfaces,
        inventory_by_root=inventory_by_root,
        contact_map=build_contact_map(repo_root) if contacts else None,
    )
