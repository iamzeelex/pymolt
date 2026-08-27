"""
axiom_graph/acquisition/sdist_cache.py

Downloads and caches sdist archives from PyPI.
Uses XDG_CACHE / axiom_graph / sdist as the cache directory.
Idempotent: re-runs return the already-extracted path immediately.

Design notes:
- Prefers sdist over wheel (we need the tests/ directory for test_miner).
- If no sdist is available, attempts wheel extraction as fallback (tests excluded).
- Never raises on download failure — returns None and caller skips gracefully.
"""

from __future__ import annotations

import io
import json
import logging
import tarfile
import urllib.request
import zipfile
from pathlib import Path

import platformdirs

log = logging.getLogger(__name__)

CACHE_DIR: Path = Path(platformdirs.user_cache_dir("axiom_graph")) / "sdist"

_PYPI_BASE = "https://pypi.org/pypi"
_USER_AGENT = "axiom-graph/1.0 (+https://github.com/pymolt)"

# Hard cap on any single download (500 MiB to support large ML frameworks).
_MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024  # 500 MiB


def _read_capped(resp, max_bytes: int = _MAX_DOWNLOAD_BYTES) -> bytes:
    """Read an HTTP response body, refusing anything over ``max_bytes``."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = resp.read(1 << 16)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"download exceeds {max_bytes}-byte cap")
        chunks.append(chunk)
    return b"".join(chunks)


def _safe_extract_zip(z: zipfile.ZipFile, dest: Path) -> None:
    """Extract a zip, refusing any member that would escape ``dest`` (zip-slip).

    ``zipfile.extractall`` does NOT protect against ``../`` or absolute paths the
    way our tar path does, so validate every entry resolves inside ``dest`` first.
    """
    dest_root = dest.resolve()
    for member in z.namelist():
        target = (dest / member).resolve()
        if target != dest_root and dest_root not in target.parents:
            raise ValueError(f"unsafe path in archive: {member!r}")
    z.extractall(dest)


def _fetch_pypi_urls(package: str, version: str) -> list[dict]:
    """Return the 'urls' list from PyPI JSON for a specific version."""
    url = f"{_PYPI_BASE}/{package}/{version}/json"
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode())
        return data.get("urls", [])
    except Exception as exc:
        log.warning("PyPI metadata fetch failed for %s==%s: %s", package, version, exc)
        return []


def _target_dir(package: str, version: str) -> Path:
    return CACHE_DIR / f"{package}-{version}"


def _extract_sdist(url: str, dest: Path) -> bool:
    """Download and extract a .tar.gz sdist into dest. Returns True on success."""
    import os
    import shutil
    import time
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    dest_name = dest.name
    tmp_extract = dest.parent / f".tmp_{dest_name}_{os.getpid()}_{time.time_ns()}"
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = _read_capped(resp)
        tmp_extract.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            members = [
                m for m in tar.getmembers()
                if not m.name.startswith("/") and ".." not in m.name
            ]
            tar.extractall(path=tmp_extract, members=members)

        extracted = next(
            (d for d in tmp_extract.iterdir() if d.is_dir()),
            None,
        )
        if extracted:
            if not dest.exists():
                try:
                    extracted.rename(dest)
                except OSError:
                    pass
        return dest.exists()
    except Exception as exc:
        log.warning("sdist extraction failed for %s: %s", url, exc)
        return False
    finally:
        if tmp_extract.exists():
            import shutil
            shutil.rmtree(tmp_extract, ignore_errors=True)


def _extract_wheel(url: str, dest: Path) -> bool:
    """Download and extract a .whl wheel into dest. Returns True on success."""
    import os
    import shutil
    import time
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    dest_name = dest.name
    tmp_extract = dest.parent / f".tmp_{dest_name}_{os.getpid()}_{time.time_ns()}"
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = _read_capped(resp)
        tmp_extract.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            _safe_extract_zip(z, tmp_extract)

        if not dest.exists():
            try:
                tmp_extract.rename(dest)
            except OSError:
                pass
        return dest.exists()
    except Exception as exc:
        log.warning("wheel extraction failed for %s: %s", url, exc)
        return False
    finally:
        if tmp_extract.exists():
            import shutil
            shutil.rmtree(tmp_extract, ignore_errors=True)


def get_sdist(package: str, version: str) -> Path | None:
    """
    Ensure the source distribution for ``package==version`` is available locally.

    Priority:
        1. Already cached → return immediately.
        2. Download sdist (.tar.gz) → extract to CACHE_DIR/{package}-{version}/.
        3. Fallback: download wheel (.whl) → extract (no tests/ directory).

    Returns:
        Path to the extracted package directory, or None if unavailable.

    Never raises — logs warnings and returns None on failure.
    """
    dest = _target_dir(package, version)
    if dest.exists():
        log.debug("Cache hit: %s", dest)
        return dest

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    urls = _fetch_pypi_urls(package, version)

    # Prefer sdist
    sdist_urls = [f["url"] for f in urls if f.get("packagetype") == "sdist"]
    for sdist_url in sdist_urls:
        log.info("Downloading sdist: %s==%s", package, version)
        if _extract_sdist(sdist_url, dest):
            log.info("Cached to %s", dest)
            return dest

    # Fallback: any wheel (prefer none-any, then first available)
    wheel_urls = [f["url"] for f in urls if f.get("filename", "").endswith(".whl")]
    wheel_urls.sort(key=lambda u: (0 if "none-any" in u else 1))
    for wheel_url in wheel_urls:
        log.info("Downloading wheel (no sdist available): %s==%s", package, version)
        if _extract_wheel(wheel_url, dest):
            log.info("Cached wheel to %s", dest)
            return dest

    # Fallback for meta-packages without code (e.g. tensorflow -> tensorflow-cpu)
    if package.lower() in ("tensorflow", "jax"):
        sibling_pkg = f"{package}-cpu" if package.lower() == "tensorflow" else "jaxlib"
        s_urls = _fetch_pypi_urls(sibling_pkg, version)
        s_wheels = [f["url"] for f in s_urls if f.get("filename", "").endswith(".whl") and f.get("size", 0) > 10 * 1024 * 1024]
        for wheel_url in s_wheels:
            log.info("Downloading sibling wheel (%s): %s==%s", sibling_pkg, package, version)
            if _extract_wheel(wheel_url, dest):
                log.info("Cached sibling wheel to %s", dest)
                return dest

    log.warning("No downloadable distribution found for %s==%s", package, version)
    return None


def find_package_source_dir(sdist_root: Path, package: str) -> Path | None:
    """
    Given the root of an extracted sdist, find the Python package source directory.
    Handles both flat layouts (src/{package}/) and collocated layouts ({package}/).

    Returns the path containing the package's __init__.py, or None.
    """
    normalized = package.lower().replace("-", "_")

    # Try src layout first
    src_layout = sdist_root / "src" / package
    if (src_layout / "__init__.py").exists():
        return src_layout

    src_normalized = sdist_root / "src" / normalized
    if (src_normalized / "__init__.py").exists():
        return src_normalized

    # Try flat layout
    flat_layout = sdist_root / package
    if (flat_layout / "__init__.py").exists():
        return flat_layout

    flat_normalized = sdist_root / normalized
    if (flat_normalized / "__init__.py").exists():
        return flat_normalized

    # Check src/ for any module
    if (sdist_root / "src").is_dir():
        for candidate in (sdist_root / "src").iterdir():
            if candidate.is_dir() and (candidate / "__init__.py").exists():
                if candidate.name not in ("tests", "test", "docs", "doc", "benchmarks", "examples"):
                    return candidate

    # Fuzzy: search one level deep in sdist root
    for candidate in sdist_root.iterdir():
        if candidate.is_dir() and (candidate / "__init__.py").exists():
            if candidate.name not in ("tests", "test", "docs", "doc", "benchmarks", "examples"):
                return candidate

    log.warning("Could not find package source dir for %r in %s", package, sdist_root)
    return None


def find_tests_dir(sdist_root: Path) -> Path | None:
    """Find the tests/ directory in an extracted sdist root."""
    for candidate in ("tests", "test"):
        p = sdist_root / candidate
        if p.is_dir():
            return p
    return None
