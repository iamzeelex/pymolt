"""
axiom_graph/sources/git_releases.py

Fetches release versions from a git repository's tags.
Complements pypi_releases.py for pre-releases, RCs, and packages
that don't register all versions on PyPI.

Uses `git ls-remote --tags` via subprocess — this is an ACQUISITION step
(network + git), isolated here, never called from analysis paths.
"""

from __future__ import annotations

import os
import re
import subprocess
import logging
from urllib.parse import urlparse
from packaging.version import Version, InvalidVersion

log = logging.getLogger(__name__)

# Only these hosts, and only over https, may be handed to git. The repo URL comes
# from an attacker-nominated package's PyPI metadata, so a substring check is not
# enough: "ext::sh -c '…' github.com" would pass it and git's ext:: transport runs
# the command (RCE); "http://169.254.169.254/github.com" would be an SSRF. Parse
# the URL and require an exact host + https scheme.
_GIT_HOSTS = ("github.com", "gitlab.com", "bitbucket.org", "sr.ht")

# Defence in depth: even for a validated https URL, forbid git from using any
# transport other than https (neutralises ext::/file:// should a URL ever slip in).
_GIT_ENV = {**os.environ, "GIT_ALLOW_PROTOCOL": "https"}


def _is_allowed_repo_url(url: str) -> bool:
    """True only for an https URL whose host is (a subdomain of) an allowed forge."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in _GIT_HOSTS)

# Common tag prefixes used by Python projects: v1.2.3, rel-1.2.3, 1.2.3, etc.
_TAG_VERSION_RE = re.compile(
    r"refs/tags/(?:v|ver|release[-_]?|rel[-_.]?)?(\d+\.\d+[\w.\-]*)$"
)


def _parse_version_safe(v: str) -> Version | None:
    try:
        return Version(v)
    except InvalidVersion:
        return None


def _list_remote_tags(repo_url: str) -> list[str]:
    """
    Run `git ls-remote --tags --refs <repo_url>` and return raw refs.
    Does NOT require the repo to be cloned.
    """
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--tags", "--refs", repo_url],
            capture_output=True,
            text=True,
            timeout=30,
            env=_GIT_ENV,
        )
        if result.returncode != 0:
            log.warning("git ls-remote failed for %s: %s", repo_url, result.stderr.strip())
            return []
        return result.stdout.strip().splitlines()
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        log.warning("git ls-remote unavailable: %s", exc)
        return []


def _extract_versions_from_refs(refs: list[str]) -> list[Version]:
    """Parse version strings out of git ref lines."""
    versions: list[Version] = []
    for line in refs:
        # Format: "<sha>\trefs/tags/<tag>"
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        ref = parts[1].strip()
        match = _TAG_VERSION_RE.match(ref)
        if not match:
            continue
        parsed = _parse_version_safe(match.group(1))
        if parsed is not None:
            versions.append(parsed)
    return versions


def fetch_git_releases(
    repo_url: str,
    from_v: str,
    to_v: str,
    *,
    include_prereleases: bool = False,
) -> list[str]:
    """
    Fetch all release versions from a git repository's tags and return the
    ordered subset in the half-open interval (from_v, to_v].

    Args:
        repo_url: Git remote URL (e.g. "https://github.com/pandas-dev/pandas").
        from_v:   Start version (exclusive).
        to_v:     End version (inclusive).
        include_prereleases: If True, alpha/beta/rc tags are included.

    Returns:
        Sorted list of version strings. Empty list if git is unavailable
        or no matching tags are found. Never raises — degrades gracefully.
    """
    if not repo_url:
        return []

    from_ver = _parse_version_safe(from_v)
    to_ver = _parse_version_safe(to_v)
    if from_ver is None or to_ver is None:
        log.warning("Invalid version bounds: %r, %r", from_v, to_v)
        return []

    refs = _list_remote_tags(repo_url)
    if not refs:
        return []

    versions = _extract_versions_from_refs(refs)
    result: list[Version] = []
    for v in versions:
        if not include_prereleases and v.is_prerelease:
            continue
        if from_ver < v <= to_ver:
            result.append(v)

    result.sort()
    return [str(v) for v in result]


def discover_repo_url(project_urls: dict[str, str]) -> str | None:
    """
    Extract the most likely git repository URL from PyPI project_urls.
    Checks keys: Source, Repository, Code, Homepage (in that priority order).
    Only returns GitHub/GitLab/Bitbucket URLs (avoids docs/changelog pages).
    """
    priority_keys = ["Source", "source", "Repository", "Code", "Homepage", "homepage"]

    for key in priority_keys:
        url = project_urls.get(key, "")
        if url and _is_allowed_repo_url(url):
            # Strip trailing slashes and tree/blob paths
            url = url.rstrip("/")
            if "/tree/" in url:
                url = url.split("/tree/")[0]
            return url

    return None
