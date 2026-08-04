import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

from pymolt.core.graph import NameMapping

logger = logging.getLogger(__name__)

GRAYSKULL_URL = "https://raw.githubusercontent.com/conda/grayskull/master/grayskull/pypi/config.yaml"

# Curated conda -> PyPI map shipped with the package so name resolution works
# fully offline. The remote grayskull table only ever overlays on top of this.
_BUNDLED_MAP = Path(__file__).with_name("data") / "conda_pypi_map.json"

# Cache freshness window for the remote overlay (7 days).
CACHE_TTL_SECONDS = 7 * 24 * 3600


def parse_grayskull_yaml(content: str) -> dict[str, dict[str, str]]:
    """Parse grayskull's config.yaml into ``{pypi_name: {conda_forge, import_name}}``.

    Uses ``yaml.safe_load`` instead of a hand-rolled line scanner so quoting,
    flow style and comments are handled correctly.
    """
    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as e:
        logger.warning("Failed to parse grayskull YAML: %s", e)
        return {}

    if not isinstance(data, dict):
        return {}

    mapping: dict[str, dict[str, str]] = {}
    for pypi_name, info in data.items():
        key = str(pypi_name).strip().lower()
        entry: dict[str, str] = {}
        if isinstance(info, dict):
            for prop in ("conda_forge", "import_name"):
                val = info.get(prop)
                if val is not None:
                    entry[prop] = str(val).strip()
        mapping[key] = entry
    return mapping


def _reverse_from_pypi_table(pypi_to_conda: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    """Build the conda_name -> {pypi_name, import_name} reverse lookup."""
    reverse: dict[str, dict[str, str]] = {}
    for pypi_name, info in pypi_to_conda.items():
        conda_forge = (info.get("conda_forge") or pypi_name).lower()
        import_name = info.get("import_name") or pypi_name.replace("-", "_")
        reverse[conda_forge] = {"pypi_name": pypi_name, "import_name": import_name}
    return reverse


class NameMapper:
    """Resolve conda package names to their PyPI twins.

    Loading is offline-first and side-effect-free: the constructor reads the
    bundled curated table and an on-disk cache only. Refreshing from the remote
    grayskull table is an explicit, opt-in call (:meth:`refresh_from_remote`).
    """

    def __init__(self, project_dir: Path | None = None):
        self.grayskull_mapping: dict[str, dict[str, str]] = {}
        self.reverse_mapping: dict[str, dict[str, Any]] = {}
        base = Path(project_dir) if project_dir else Path.cwd()
        self.cache_dir = (base / ".pymolt_cache").resolve()
        self.cache_file = self.cache_dir / "grayskull_mapping.json"
        self._load_bundled()
        self._load_cache()

    def _load_bundled(self) -> None:
        """Seed the reverse map from the bundled curated table (always present)."""
        try:
            with open(_BUNDLED_MAP, encoding="utf-8") as f:
                data = json.load(f)
            for conda_name, info in data.get("conda_to_pypi", {}).items():
                self.reverse_mapping[conda_name.lower()] = {
                    "pypi_name": info["pypi_name"],
                    "import_name": info.get("import_name", info["pypi_name"].replace("-", "_")),
                }
        except (OSError, json.JSONDecodeError, KeyError) as e:
            logger.warning("Could not load bundled conda->pypi map: %s", e)

    def _load_cache(self) -> None:
        """Overlay a previously fetched remote table when the cache is present."""
        if not self.cache_file.is_file():
            return
        try:
            with open(self.cache_file, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Ignoring unreadable name-mapping cache %s: %s", self.cache_file, e)
            return
        self.grayskull_mapping = data.get("pypi_to_conda", {})
        # Bundled entries win; the remote overlay only extends coverage where the
        # curated table has nothing.
        for conda_name, info in data.get("conda_to_pypi", {}).items():
            self.reverse_mapping.setdefault(conda_name.lower(), info)

    def refresh_from_remote(self, force: bool = False) -> bool:
        """Fetch grayskull's table and update the cache. Opt-in, network-touching.

        Returns True on a successful refresh. Honors the cache TTL unless
        ``force`` is set. Never raises on network/parse failure.
        """
        if not force and self.cache_file.is_file():
            try:
                age = time.time() - self.cache_file.stat().st_mtime
                if age < CACHE_TTL_SECONDS:
                    return False
            except OSError:
                pass
        try:
            response = httpx.get(GRAYSKULL_URL, timeout=10.0)
        except httpx.HTTPError as e:
            logger.info("Remote name-map refresh skipped (network error): %s", e)
            return False
        if response.status_code != 200:
            logger.info("Remote name-map refresh skipped (HTTP %s).", response.status_code)
            return False

        pypi_to_conda = parse_grayskull_yaml(response.text)
        conda_to_pypi = _reverse_from_pypi_table(pypi_to_conda)
        self.grayskull_mapping = pypi_to_conda
        for conda_name, info in conda_to_pypi.items():
            self.reverse_mapping.setdefault(conda_name, info)
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            with open(self.cache_file, "w", encoding="utf-8") as f:
                json.dump(
                    {"pypi_to_conda": pypi_to_conda, "conda_to_pypi": conda_to_pypi},
                    f,
                    indent=2,
                )
        except OSError as e:
            logger.warning("Could not write name-mapping cache %s: %s", self.cache_file, e)
        return True

    def conda_to_pypi(self, conda_name: str) -> NameMapping | None:
        """Resolve a conda name to a PyPI name with a confidence cascade:
        1. AUTHORITATIVE  curated bundled / grayskull table
        2. HEURISTIC      normalization (case, ``-``/``_``)
        """
        cname = conda_name.strip().lower()

        # 1. AUTHORITATIVE: curated lookup
        if cname in self.reverse_mapping:
            info = self.reverse_mapping[cname]
            return NameMapping(
                pypi_name=info["pypi_name"],
                conda_name=cname,
                import_name=info["import_name"],
                confidence="authoritative",
                source="bundled",
            )

        # 2. HEURISTIC: best-effort self-name normalization
        return NameMapping(
            pypi_name=cname,
            conda_name=cname,
            import_name=cname.replace("-", "_"),
            confidence="heuristic",
            source="heuristic",
        )
