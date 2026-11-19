"""Source of one old release, for the static old side (``pymolt/delta/static_side.py``).

``acquire(root, dist, version)`` returns a directory holding that release's Python source:

1. the project's own cache, ``<project>/.pymolt/cache/static/<dist>-<version>/src`` (a release
   fetched or unpacked before);
2. **uv's cache**: an unpacked wheel (``wheels-v*/pypi/<dist>/<version>-<tag>``, pure Python
   preferred) or an unpacked sdist (``sdists-v*/pypi/<dist>/<version>/<hash>/src``). Offline,
   nothing leaves the machine;
3. only with ``online=True``: PyPI, through ``delta/fetch.py``'s ``Fetcher`` for the release
   metadata. The sdist or wheel is downloaded with a size cap, checked against PyPI's
   sha256 and unpacked with the zip-slip guarded extractors below. Only ``.py``/``.pyi`` and
   the distribution metadata are written; compiled members become empty placeholders so a
   compiled submodule still *exists* for the static reader.

Nothing is executed, imported or built. A failure is an :class:`AcquireError` carrying the
reason, which the engine turns into an INCONCLUSIVE note.

The extractors cap every read, refuse a ``..`` member, cap the total unpacked size
against zip bombs, and refuse links, devices and absolute members instead of unpacking
them.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from pymolt.delta.fetch import Fetcher, FetchError

log = logging.getLogger(__name__)

STATIC_CACHE = Path(".pymolt") / "cache" / "static"
_MARKER = "acquired.json"
MAX_DOWNLOAD_BYTES = 300 * 1024 * 1024
MAX_UNPACKED_BYTES = 1024 * 1024 * 1024
MAX_MEMBERS = 200_000
_KEEP_SUFFIXES = (".py", ".pyi")
_KEEP_NAMES = {"top_level.txt", "METADATA", "PKG-INFO"}
_PLACEHOLDER_SUFFIXES = (".so", ".pyd")


class AcquireError(RuntimeError):
    """The release's source could not be found or read."""


class UnsafeArchive(AcquireError):
    """An archive member would land outside the destination, or the archive is too large."""


@dataclass
class Acquired:
    """One release's source tree: ``search_dir`` holds the import roots (``roots``)."""

    dist: str
    version: str
    kind: str  # "wheel" | "sdist"
    file: str  # the wheel / sdist name, or the cache directory's name
    origin: str  # "uv-cache" | "project-cache" | "pypi"
    search_dir: Path
    roots: list[str] = field(default_factory=list)
    seconds: float = 0.0
    bytes_downloaded: int = 0

    @property
    def source(self) -> str:
        return f"{self.kind} {self.file}"


# ── capped reads and safe extraction ──────────────────────────────────────────────────


def read_capped(resp: Any, max_bytes: int = MAX_DOWNLOAD_BYTES) -> bytes:
    """Read a file-like response, refusing anything over ``max_bytes``."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = resp.read(1 << 16)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise UnsafeArchive(f"download exceeds the {max_bytes}-byte cap")
        chunks.append(chunk)
    return b"".join(chunks)


def _clean_member(name: str) -> PurePosixPath:
    """The member's relative path, or :class:`UnsafeArchive` when it could escape."""
    normalised = name.replace("\\", "/")
    path = PurePosixPath(normalised)
    if path.is_absolute() or re.match(r"^[A-Za-z]:", normalised) or ".." in path.parts:
        raise UnsafeArchive(f"unsafe path in archive: {name!r}")
    return path


def _strip(path: PurePosixPath, strip_top: bool) -> PurePosixPath | None:
    if not strip_top:
        return path
    return PurePosixPath(*path.parts[1:]) if len(path.parts) > 1 else None


def _wanted(path: PurePosixPath) -> bool:
    return path.suffix in _KEEP_SUFFIXES or path.name in _KEEP_NAMES


def _inside(dest_root: Path, target: Path) -> bool:
    return target == dest_root or dest_root in target.parents


def _write(dest_root: Path, rel: PurePosixPath, source: Any, budget: list[int]) -> None:
    target = (dest_root / Path(*rel.parts)).resolve()
    if not _inside(dest_root, target):
        raise UnsafeArchive(f"unsafe path in archive: {rel!s}")
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "wb") as out:
        while True:
            chunk = source.read(1 << 16)
            if not chunk:
                break
            budget[0] -= len(chunk)
            if budget[0] < 0:
                raise UnsafeArchive(f"archive unpacks to more than {MAX_UNPACKED_BYTES} bytes")
            out.write(chunk)


def _placeholder(dest_root: Path, rel: PurePosixPath) -> None:
    target = (dest_root / Path(*rel.parts)).resolve()
    if _inside(dest_root, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()


def safe_extract_zip(path: Path, dest: Path, *, strip_top: bool = False) -> None:
    """Unpack the readable members of a zip (wheel) into ``dest``.

    Any member that is absolute or climbs out with ``..`` refuses the whole archive (before
    anything is written), as does an archive claiming more than ``MAX_UNPACKED_BYTES``.
    """
    dest.mkdir(parents=True, exist_ok=True)
    dest_root = dest.resolve()
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        if len(infos) > MAX_MEMBERS:
            raise UnsafeArchive(f"archive has {len(infos)} members")
        if sum(i.file_size for i in infos) > MAX_UNPACKED_BYTES:
            raise UnsafeArchive(f"archive unpacks to more than {MAX_UNPACKED_BYTES} bytes")
        plan = []
        for info in infos:  # validate every member before writing any
            rel = _clean_member(info.filename)
            target = (dest_root / Path(*rel.parts)).resolve() if rel.parts else dest_root
            if not _inside(dest_root, target):
                raise UnsafeArchive(f"unsafe path in archive: {info.filename!r}")
            plan.append((info, rel))
        budget = [MAX_UNPACKED_BYTES]
        for info, rel in plan:
            stripped = _strip(rel, strip_top)
            if info.is_dir() or stripped is None:
                continue
            if _wanted(stripped):
                with z.open(info) as src:
                    _write(dest_root, stripped, src, budget)
            elif stripped.suffix in _PLACEHOLDER_SUFFIXES:
                _placeholder(dest_root, stripped)


def safe_extract_tar(path: Path, dest: Path, *, strip_top: bool = True) -> None:
    """Unpack the readable members of a tar (sdist) into ``dest``; same rules as the zip.

    Links, devices and FIFOs are skipped; absolute and ``..`` members refuse the archive.
    """
    dest.mkdir(parents=True, exist_ok=True)
    dest_root = dest.resolve()
    budget = [MAX_UNPACKED_BYTES]
    with tarfile.open(path, "r:*") as tar:
        members = tar.getmembers()
        if len(members) > MAX_MEMBERS:
            raise UnsafeArchive(f"archive has {len(members)} members")
        for member in members:  # validate first: a bad name refuses the archive
            _clean_member(member.name)
        for member in members:
            if not member.isfile():
                continue
            stripped = _strip(_clean_member(member.name), strip_top)
            if stripped is None:
                continue
            if _wanted(stripped):
                src = tar.extractfile(member)
                if src is not None:
                    _write(dest_root, stripped, src, budget)
            elif stripped.suffix in _PLACEHOLDER_SUFFIXES:
                _placeholder(dest_root, stripped)


def extract_archive(path: Path, dest: Path) -> None:
    """Unpack a wheel or sdist by its extension (:class:`UnsafeArchive` on a bad archive)."""
    name = path.name.lower()
    try:
        if name.endswith((".whl", ".egg")):
            safe_extract_zip(path, dest)
        elif name.endswith(".zip"):
            safe_extract_zip(path, dest, strip_top=True)
        elif name.endswith((".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar")):
            safe_extract_tar(path, dest)
        else:
            raise AcquireError(f"unknown archive type: {path.name}")
    except (zipfile.BadZipFile, tarfile.TarError, OSError) as exc:
        raise AcquireError(f"cannot read {path.name}: {exc}") from exc


# ── import roots ──────────────────────────────────────────────────────────────────────

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _top_level_roots(tree: Path) -> list[str]:
    for pattern in ("*.dist-info/top_level.txt", "*.egg-info/top_level.txt",
                    "src/*.egg-info/top_level.txt", "*/*.egg-info/top_level.txt"):
        for f in sorted(tree.glob(pattern)):
            try:
                names = [line.strip() for line in f.read_text(encoding="utf-8").splitlines()]
            except OSError:
                continue
            roots = [n for n in names if _IDENT.match(n)]
            if roots:
                return roots
    return []


def _has_root(base: Path, name: str) -> bool:
    return (base / name).is_dir() or (base / f"{name}.py").is_file() or any(
        base.glob(f"{name}.*.so")) or any(base.glob(f"{name}.*.pyd"))


def find_layout(tree: Path, dist: str) -> tuple[Path, list[str]]:
    """``(search_dir, import roots)`` of an unpacked wheel or sdist.

    Roots come from ``top_level.txt``, else the distribution's name, else every importable
    entry at the top of the tree (or of ``src/``, ``lib/``).
    """
    bases = [tree] + [tree / d for d in ("src", "lib") if (tree / d).is_dir()]
    declared = _top_level_roots(tree)
    guess = canonicalize_name(dist).replace("-", "_")
    for base in bases:
        roots = [r for r in declared if _has_root(base, r)]
        if roots:
            return base, roots
    for base in bases:
        if _IDENT.match(guess) and _has_root(base, guess):
            return base, [guess]
    for base in bases:
        found = sorted(
            {p.name for p in base.iterdir()
             if p.is_dir() and _IDENT.match(p.name) and not p.name.startswith(("_", "test"))
             and (p / "__init__.py").is_file()}
            | {p.stem for p in base.glob("*.py")
               if _IDENT.match(p.stem) and p.stem not in ("setup", "conftest", "versioneer")}
        )
        if found:
            return base, found
    return tree, []


# ── uv's cache ────────────────────────────────────────────────────────────────────────


def uv_cache_dir() -> Path | None:
    env = os.environ.get("UV_CACHE_DIR")
    if env:
        return Path(env)
    uv = shutil.which("uv")
    if uv:
        try:
            proc = subprocess.run([uv, "cache", "dir"], capture_output=True, text=True,
                                  timeout=15)
            if proc.returncode == 0 and proc.stdout.strip():
                return Path(proc.stdout.strip())
        except (OSError, subprocess.SubprocessError):
            pass
    default = Path.home() / ".cache" / "uv"
    return default if default.is_dir() else None


def _same_version(a: str, b: str) -> bool:
    try:
        return Version(a) == Version(b)
    except InvalidVersion:
        return a.strip() == b.strip()


def _wheel_rank(tag: str) -> tuple[int, str]:
    pure = "none-any" in tag or tag.startswith(("py2.py3", "py3"))
    return (0 if pure else 1, tag)


def _uv_wheels(cache: Path, dist: str, version: str) -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    name = canonicalize_name(dist)
    for index in sorted(cache.glob("wheels-v*/pypi")):
        folder = index / name
        if not folder.is_dir():
            continue
        for entry in folder.iterdir():
            if entry.suffix in (".http", ".msgpack") or not entry.is_dir():
                continue  # metadata files, or a symlink to an archive uv pruned
            ver, _, tag = entry.name.partition("-")
            if _same_version(ver, version):
                out.append((tag, entry))
    return sorted(out, key=lambda t: _wheel_rank(t[0]))


def _uv_sdists(cache: Path, dist: str, version: str) -> list[Path]:
    out: list[Path] = []
    name = canonicalize_name(dist)
    for index in sorted(cache.glob("sdists-v*/pypi")):
        folder = index / name
        if not folder.is_dir():
            continue
        for ver_dir in folder.iterdir():
            if ver_dir.is_dir() and _same_version(ver_dir.name, version):
                out += sorted(p for p in ver_dir.glob("*/src") if p.is_dir())
    return out


def _from_uv_cache(dist: str, version: str) -> Acquired | None:
    cache = uv_cache_dir()
    if cache is None:
        return None
    wheels = _uv_wheels(cache, dist, version)
    pure = [w for w in wheels if _wheel_rank(w[0])[0] == 0]
    sdists = _uv_sdists(cache, dist, version)
    choice: tuple[str, str, Path] | None = None
    if pure:
        choice = ("wheel", f"{canonicalize_name(dist)}-{version}-{pure[0][0]}", pure[0][1])
    elif sdists:
        choice = ("sdist", f"{canonicalize_name(dist)}-{version}", sdists[0])
    elif wheels:
        choice = ("wheel", f"{canonicalize_name(dist)}-{version}-{wheels[0][0]}", wheels[0][1])
    if choice is None:
        return None
    kind, file, tree = choice
    search_dir, roots = find_layout(tree, dist)
    if not roots:
        return None
    return Acquired(dist, version, kind, file, "uv-cache", search_dir, roots)


# ── PyPI ──────────────────────────────────────────────────────────────────────────────


def _pick_file(files: list[dict]) -> dict | None:
    """The best release file: a pure wheel, else an sdist, else the smallest wheel."""
    live = [f for f in files if not f.get("yanked")]
    wheels = [f for f in live if f.get("filename", "").endswith(".whl")]
    pure = [f for f in wheels if "none-any" in f["filename"]]
    sdists = [f for f in live if f.get("packagetype") == "sdist"
              or f.get("filename", "").endswith((".tar.gz", ".zip", ".tar.bz2"))]
    if pure:
        return pure[0]
    if sdists:
        return sorted(sdists, key=lambda f: (not f["filename"].endswith(".tar.gz"),
                                             f.get("size") or 0))[0]
    if wheels:
        return min(wheels, key=lambda f: f.get("size") or 1 << 60)
    return None


def _default_download(fetcher: Fetcher, url: str, dest: Path) -> int:
    http = fetcher._http()
    total = 0
    with http.stream("GET", url) as resp:
        if resp.status_code != 200:
            raise AcquireError(f"download answered {resp.status_code} for {url}")
        with open(dest, "wb") as out:
            for chunk in resp.iter_bytes(1 << 16):
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise UnsafeArchive(f"download exceeds the {MAX_DOWNLOAD_BYTES}-byte cap")
                out.write(chunk)
    fetcher.bytes += total
    fetcher.requests += 1
    return total


def _from_pypi(root: Path, dist: str, version: str, fetcher: Fetcher,
               download: Callable[[Fetcher, str, Path], int], dest: Path) -> Acquired:
    try:
        releases = fetcher.releases(dist)
    except (FetchError, OSError, ValueError) as exc:
        raise AcquireError(f"PyPI metadata for {dist} unavailable: {exc}") from exc
    files = next((fs for v, fs in releases.items() if _same_version(v, version)), None)
    if not files:
        raise AcquireError(f"PyPI has no release {dist} {version}")
    chosen = _pick_file(files)
    if chosen is None:
        raise AcquireError(f"PyPI release {dist} {version} has no wheel or sdist")
    url, filename = chosen["url"], chosen["filename"]
    tmp = Path(tempfile.mkdtemp(prefix=".dl-", dir=str(dest.parent)))
    try:
        archive = tmp / filename
        try:
            size = download(fetcher, url, archive)
        except AcquireError:
            raise
        except Exception as exc:  # noqa: BLE001 — the network failing is a reason, not a crash
            raise AcquireError(f"download of {filename} failed: {exc}") from exc
        expected = (chosen.get("digests") or {}).get("sha256")
        if expected and hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
            raise AcquireError(f"{filename} does not match PyPI's sha256")
        unpacked = tmp / "src"
        extract_archive(archive, unpacked)
        search_dir, roots = find_layout(unpacked, dist)
        if not roots:
            raise AcquireError(f"no importable source found in {filename}")
        final_src = dest / "src"
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True)
        unpacked.rename(final_src)
        kind = "wheel" if filename.endswith(".whl") else "sdist"
        (dest / _MARKER).write_text(json.dumps({
            "dist": dist, "version": version, "kind": kind, "file": filename, "origin": "pypi",
            "url": url, "bytes": size,
        }, indent=2), encoding="utf-8")
        search_dir, roots = find_layout(final_src, dist)
        return Acquired(dist, version, kind, filename, "pypi", search_dir, roots,
                        bytes_downloaded=size)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _from_project_cache(dest: Path, dist: str, version: str) -> Acquired | None:
    marker = dest / _MARKER
    try:
        meta = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    search_dir, roots = find_layout(dest / "src", dist)
    if not roots:
        return None
    return Acquired(dist, version, meta.get("kind", "wheel"), meta.get("file", dest.name),
                    "project-cache", search_dir, roots)


def acquire(
    root: Path, dist: str, version: str, *, online: bool = False,
    fetcher: Fetcher | None = None,
    download: Callable[[Fetcher, str, Path], int] | None = None,
) -> Acquired:
    """The source of ``dist==version``: project cache, uv's cache, then (``online``) PyPI.

    Raises :class:`AcquireError` with the reason (offline and not in any cache, no such
    release, a download over the cap, an unsafe archive, ...).
    """
    t0 = time.perf_counter()
    root = Path(root)
    dest = root / STATIC_CACHE / f"{canonicalize_name(dist)}-{version}"
    found = _from_project_cache(dest, dist, version) or _from_uv_cache(dist, version)
    if found is not None:
        found.seconds = round(time.perf_counter() - t0, 3)
        return found
    if not online:
        raise AcquireError(
            f"{dist} {version} is in neither the project's static cache nor uv's cache "
            "(rerun with --online to download it)")
    dest.parent.mkdir(parents=True, exist_ok=True)
    own = fetcher is None
    fetcher = fetcher or Fetcher(root / ".pymolt" / "cache" / "delta")
    try:
        acquired = _from_pypi(root, dist, version, fetcher, download or _default_download,
                              dest)
    finally:
        if own:
            fetcher.close()
    acquired.seconds = round(time.perf_counter() - t0, 3)
    return acquired
