"""
axiom_graph/sources/version_manifest.py

Time & Versioning Management for release sequencing.

Core responsibility:
  1. Fetch versions from PyPI + optional git tags (acquisition).
  2. Validate strict chronological ordering via semantic versioning.
  3. Build a canonical "version chain" from (from_v, to_v].
  4. Optionally clone the bare repository for local version reconciliation.

Design:
  - All version comparisons use packaging.version.Version (semver semantics).
  - Bare clone is OPTIONAL and cached (~/.cache/axiom_graph/repos/{package}.git).
  - PyPI is the PRIMARY source; git tags are COMPLEMENTARY (only fetched when
    use_git=True) and catch versions PyPI may miss (RCs, unpublished tags).
  - Dedup is by normalized Version (so "1.1" and "1.1.0" collapse to one).
  - Versions are strictly ordered by parsed semver; the chain is monotonic.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

import platformdirs
from packaging.version import InvalidVersion, Version

from axiom_graph.sources.git_releases import _GIT_ENV, discover_repo_url, fetch_git_releases
from axiom_graph.sources.pypi_releases import fetch_ordered_releases, fetch_project_urls

log = logging.getLogger(__name__)

REPOS_CACHE_DIR: Path = Path(platformdirs.user_cache_dir("axiom_graph")) / "repos"


def _parse(v: str) -> Version | None:
    """Parse a version string, returning None on failure (never raises)."""
    try:
        return Version(v)
    except InvalidVersion:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class VersionRecord:
    """Single version in the manifest."""
    version_str: str
    parsed: Version
    source: str  # "pypi" | "git" | "both" | "target"

    def __lt__(self, other: VersionRecord) -> bool:
        return self.parsed < other.parsed

    def __repr__(self) -> str:
        return self.version_str


@dataclass
class VersionManifest:
    """
    Canonical manifest for a release chain.

    Guarantees:
      - All versions are strictly ordered (v_i < v_{i+1}).
      - No duplicates.
      - from_v is EXCLUDED, to_v is INCLUDED (half-open interval).
    """
    package: str
    from_v: str
    to_v: str

    records: list[VersionRecord]
    """Sorted records in chronological order."""

    pypi_source: str | None = None
    """PyPI project URL (if fetched)."""

    git_source: str | None = None
    """Git repository URL (if discovered)."""

    git_repo_path: Path | None = None
    """Local bare-clone path (if cloned)."""

    def chain(self) -> list[str]:
        """Return the ordered version strings."""
        return [r.version_str for r in self.records]

    def count(self) -> int:
        return len(self.records)

    def first(self) -> VersionRecord | None:
        return self.records[0] if self.records else None

    def last(self) -> VersionRecord | None:
        return self.records[-1] if self.records else None

    def summary(self) -> str:
        """Human-readable summary for logging."""
        if not self.records:
            return f"{self.package} [{self.from_v}→{self.to_v}]: ∅ (no releases found)"

        src_list = []
        if self.pypi_source:
            src_list.append("PyPI")
        if self.git_source:
            src_list.append(f"Git {self.git_source}")
        sources = " + ".join(src_list) if src_list else "unknown"

        chain_preview = " → ".join(self.chain()[:3])
        if len(self.chain()) > 3:
            chain_preview += f" → ... ({self.count()} total)"

        return (
            f"{self.package} [{self.from_v}→{self.to_v}]: "
            f"{self.count()} versions via {sources}\n  {chain_preview}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Bare repository management
# ─────────────────────────────────────────────────────────────────────────────


def _clone_bare_repo(repo_url: str, package: str) -> Path | None:
    """
    Clone a bare repository (--bare) to ~/.cache/axiom_graph/repos/{package}.git.

    Returns the path if successful, None if git is unavailable or clone failed.
    Does NOT raise — logs and degrades gracefully.
    """
    try:
        REPOS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        bare_path = REPOS_CACHE_DIR / f"{package}.git"

        # If already cloned, update instead
        if bare_path.exists():
            log.debug("Bare repo exists at %s, updating...", bare_path)
            result = subprocess.run(
                ["git", "-C", str(bare_path), "fetch", "origin"],
                capture_output=True,
                text=True,
                timeout=60,
                env=_GIT_ENV,
            )
            if result.returncode != 0:
                log.warning("Could not update bare repo: %s", result.stderr.strip())
                return None
            return bare_path

        # Clone --bare
        log.debug("Cloning bare repo from %s to %s", repo_url, bare_path)
        result = subprocess.run(
            ["git", "clone", "--bare", repo_url, str(bare_path)],
            capture_output=True,
            text=True,
            timeout=120,
            env=_GIT_ENV,
        )
        if result.returncode != 0:
            log.warning("Could not clone bare repo: %s", result.stderr.strip())
            return None

        log.info("Cloned bare repo to %s", bare_path)
        return bare_path

    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        log.warning("Bare clone unavailable: %s", exc)
        return None


def _list_tags_from_bare(bare_path: Path) -> list[Version]:
    """
    List all git tags from a bare repository, parsed as versions.

    Returns empty list if git is unavailable or repo is invalid.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(bare_path), "tag", "-l"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            log.warning("Could not list tags from bare repo: %s", result.stderr.strip())
            return []

        versions: list[Version] = []
        for tag in result.stdout.strip().splitlines():
            tag = tag.strip()
            if not tag:
                continue
            # Try to parse the tag as a version (strip common prefixes)
            ver_str = tag.lstrip("vV")
            try:
                versions.append(Version(ver_str))
            except InvalidVersion:
                log.debug("Skipping unparseable tag: %s", tag)

        return sorted(versions)

    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        log.warning("Could not query bare repo tags: %s", exc)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Main manifest builder
# ─────────────────────────────────────────────────────────────────────────────


def build_version_manifest(
    package: str,
    from_v: str,
    to_v: str,
    *,
    use_git: bool = True,
    clone_bare: bool = False,
    include_prereleases: bool = False,
) -> VersionManifest:
    """
    Build a complete version manifest for a release chain.

    Sequence:
      1. Fetch PyPI versions (primary source).
      2. Optionally clone bare repo and extract git tags (complementary).
      3. Merge, deduplicate, validate strict ordering.
      4. Return canonical manifest.

    Args:
        package: PyPI package name.
        from_v: Exclusive lower bound.
        to_v: Inclusive upper bound.
        use_git: If True, try to fetch git tags as well.
        clone_bare: If True, attempt to clone a bare repository for local validation.
        include_prereleases: If True, include alpha/beta/rc versions.

    Returns:
        VersionManifest with validated, ordered version chain.
        Never raises — degrades gracefully on any acquisition failure.
    """
    log.info(
        "Building version manifest for %s [%s → %s] "
        "(git=%s, bare_clone=%s, prereleases=%s)",
        package, from_v, to_v, use_git, clone_bare, include_prereleases
    )

    from_ver: Version | None = None
    to_ver: Version | None = None
    try:
        from_ver = Version(from_v)
        to_ver = Version(to_v)
        if from_ver >= to_ver:
            log.error("Invalid bounds: from_v (%s) >= to_v (%s)", from_v, to_v)
            return VersionManifest(
                package=package, from_v=from_v, to_v=to_v, records=[]
            )
    except InvalidVersion as exc:
        log.error("Invalid version string: %s", exc)
        return VersionManifest(
            package=package, from_v=from_v, to_v=to_v, records=[]
        )

    # Step 1: PyPI
    pypi_versions: list[str] = []
    pypi_ok = False
    try:
        pypi_versions = fetch_ordered_releases(
            package, from_v, to_v, include_prereleases=include_prereleases
        )
        pypi_ok = True
        log.info("PyPI: found %d versions", len(pypi_versions))
    except Exception as exc:
        log.warning("PyPI fetch failed: %s", exc)

    # Step 2: Git tags (optional). Only touches the network when use_git=True.
    git_versions: list[str] = []
    git_repo_url: str | None = None
    bare_path: Path | None = None

    if use_git:
        try:
            git_repo_url = discover_repo_url(fetch_project_urls(package))
            if git_repo_url:
                # Optionally clone bare repo for local tag extraction
                if clone_bare:
                    bare_path = _clone_bare_repo(git_repo_url, package)
                    if bare_path:
                        git_tag_versions = _list_tags_from_bare(bare_path)
                        git_versions = [str(v) for v in git_tag_versions]
                        log.info("Git (bare): found %d versions from local tags", len(git_versions))
                else:
                    # Remote git-ls-remote fallback
                    git_versions = fetch_git_releases(
                        git_repo_url, from_v, to_v,
                        include_prereleases=include_prereleases
                    )
                    log.info("Git (remote): found %d versions", len(git_versions))
        except Exception as exc:
            log.warning("Git fetch failed: %s", exc)

    # Step 3: Merge & deduplicate by NORMALIZED version (not raw string).
    # PyPI and git may spell the same release differently ("1.1" vs "1.1.0");
    # those are one release. Version objects normalize trailing zeros and hash
    # equal, so we key the dedup on the parsed Version itself.
    pypi_keys = {p for v in pypi_versions if (p := _parse(v)) is not None}
    git_keys = {p for v in git_versions if (p := _parse(v)) is not None}
    to_key = _parse(to_v)

    # First seen spelling wins per normalized key; we feed PyPI first for
    # stable spelling, then git, then the explicit target.
    by_key: dict[Version, VersionRecord] = {}

    def _consider(v_str: str, default_source: str) -> None:
        parsed = _parse(v_str)
        if parsed is None:
            log.debug("Skipping unparseable version: %s", v_str)
            return
        if not (from_ver < parsed <= to_ver):
            return
        in_pypi = parsed in pypi_keys
        in_git = parsed in git_keys
        if in_pypi and in_git:
            source = "both"
        elif in_pypi:
            source = "pypi"
        elif in_git:
            source = "git"
        else:
            source = default_source
        if parsed not in by_key:
            by_key[parsed] = VersionRecord(v_str, parsed, source)

    for v in pypi_versions:
        _consider(v, "pypi")
    for v in git_versions:
        _consider(v, "git")
    # Ensure the target is always present (it may post-date what we fetched);
    # only inject if not already covered by a fetched release.
    if to_key is not None and to_key not in by_key:
        _consider(to_v, "target")

    records = sorted(by_key.values(), key=lambda r: r.parsed)

    manifest = VersionManifest(
        package=package,
        from_v=from_v,
        to_v=to_v,
        records=records,
        pypi_source="PyPI" if pypi_ok and pypi_versions else None,
        git_source=git_repo_url,
        git_repo_path=bare_path,
    )

    log.info(manifest.summary())
    return manifest
