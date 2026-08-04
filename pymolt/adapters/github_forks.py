"""GitHub fork-network adapter — thin, network-only.

Feeds pymolt's strategic fork triage (:mod:`pymolt.strategic.forknet`). These are
pure fetchers over the GitHub API: no domain models, no scoring, no caching (the
forknet service wraps them with an on-disk cache, mirroring the risk layer). Every
function degrades to ``None`` / an empty list on any failure and never raises for a
network/HTTP problem — offline-first posture. Uses ``httpx`` like the PyPI adapter.

Auth: a token from ``GITHUB_TOKEN`` / ``GH_TOKEN`` raises the rate limit to 5000/hr;
anonymous is 60/hr. Degrade (fewer forks compared), don't fail, when anonymous.

Endpoints this adapter speaks:
  - GraphQL  POST https://api.github.com/graphql
      query Repository.forks(first: 100, after: $cursor,
                             orderBy: {field: PUSHED_AT, direction: DESC})
      per node: nameWithOwner, url, pushedAt, stargazerCount, defaultBranchRef.name
      Because forks come back newest-push-first, the caller stops paging as soon as
      a page's oldest pushedAt is below the recency cutoff (cheap: a few requests
      even for a 15k-fork repo).
  - REST     GET /repos/{base}/compare/{base_branch}...{owner}:{fork_branch}
      -> {"ahead_by": int, "behind_by": int, ...}   (the only way to get divergence;
      one call PER fork, so the service runs it on the top-N survivors only)
  - Raw      GET https://raw.githubusercontent.com/{owner}/{name}/{branch}/{path}
      for requirements.txt / setup.py / pyproject.toml -> declared-dependency lines
      (the "already ported?" enrichment signal)
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

GITHUB_GRAPHQL_URL = "https://api.github.com/graphql"
GITHUB_API_URL = "https://api.github.com"
GITHUB_RAW_URL = "https://raw.githubusercontent.com"
# Manifests we probe (first hit wins) for the ported-signal enrichment.
MANIFEST_PATHS = ("requirements.txt", "setup.py", "pyproject.toml")


@dataclass
class RawFork:
    """A fork as the GraphQL API returns it (pre-domain, ISO strings preserved)."""

    name_with_owner: str  # "owner/name"
    url: str
    pushed_at: str  # ISO8601, e.g. "2025-04-01T12:00:00Z"
    stars: int
    default_branch: str


def resolve_token() -> str | None:
    """Return a GitHub token from GITHUB_TOKEN / GH_TOKEN, or None (anonymous)."""
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or None


def auth_mode(token: str | None) -> str:
    """'token' when authenticated, else 'anonymous' (for the report honesty marker)."""
    return "token" if token else "anonymous"


def _auth_headers(token: str | None) -> dict[str, str]:
    """Common headers for GitHub API/raw requests; Bearer auth when a token exists."""
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


_FORKS_QUERY = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    forks(first: 100, after: $cursor, orderBy: {field: PUSHED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        nameWithOwner
        url
        pushedAt
        stargazerCount
        defaultBranchRef { name }
      }
    }
  }
}
"""


def fetch_recent_forks(
    base_repo: str,
    *,
    cutoff_iso: str,
    token: str | None,
    max_pages: int = 50,
) -> list[RawFork]:
    """All forks of ``base_repo`` ("owner/name") pushed at/after ``cutoff_iso``.

    Pages the GraphQL forks connection newest-push-first, stopping once a page's
    entries fall below ``cutoff_iso`` (or ``max_pages`` is hit — surfaced by the
    caller as a truncation caveat). Returns [] on failure.
    """
    owner, _, name = base_repo.partition("/")
    headers = _auth_headers(token)
    forks: list[RawFork] = []
    cursor: str | None = None

    for _page in range(max_pages):
        variables = {"owner": owner, "name": name, "cursor": cursor}
        try:
            resp = httpx.post(
                GITHUB_GRAPHQL_URL,
                json={"query": _FORKS_QUERY, "variables": variables},
                headers=headers,
                timeout=30.0,
            )
        except httpx.HTTPError as e:
            logger.info("GitHub GraphQL request failed (%s): %s", base_repo, e)
            break
        if resp.status_code != 200:
            break
        try:
            data = resp.json()
        except ValueError:
            break
        if data.get("errors"):
            logger.info("GitHub GraphQL errors for %s: %s", base_repo, data["errors"])
            break

        repository = data.get("data", {}).get("repository")
        if not repository:
            break
        forks_conn = repository.get("forks", {})
        nodes = forks_conn.get("nodes") or []

        hit_cutoff = False
        for node in nodes:
            pushed_at = node.get("pushedAt", "")
            if pushed_at < cutoff_iso:
                hit_cutoff = True
                break
            default_branch_ref = node.get("defaultBranchRef") or {}
            forks.append(
                RawFork(
                    name_with_owner=node.get("nameWithOwner", ""),
                    url=node.get("url", ""),
                    pushed_at=pushed_at,
                    stars=node.get("stargazerCount", 0),
                    default_branch=default_branch_ref.get("name") or "master",
                )
            )
        if hit_cutoff:
            break

        page_info = forks_conn.get("pageInfo", {})
        if not page_info.get("hasNextPage"):
            break
        cursor = page_info.get("endCursor")

    return forks


def fetch_default_branch(base_repo: str, *, token: str | None) -> str | None:
    """The base repo's default branch (the correct left side of a fork compare), or None.

    A fork may have renamed its default branch (``master`` -> ``main``); comparing
    against the fork's own branch name would 404 and silently lose the divergence
    signal, so the service resolves the base branch here once.
    """
    url = f"{GITHUB_API_URL}/repos/{base_repo}"
    try:
        resp = httpx.get(url, headers=_auth_headers(token), timeout=30.0)
    except httpx.HTTPError as e:
        logger.info("GitHub repo request failed (%s): %s", url, e)
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json().get("default_branch")
    except ValueError:
        return None


def compare_fork(
    base_repo: str,
    base_branch: str,
    fork_name_with_owner: str,
    fork_branch: str,
    *,
    token: str | None,
) -> dict | None:
    """{'ahead_by': int, 'behind_by': int} for a fork vs base, or None on failure."""
    fork_owner, _, _fork_name = fork_name_with_owner.partition("/")
    url = f"{GITHUB_API_URL}/repos/{base_repo}/compare/{base_branch}...{fork_owner}:{fork_branch}"
    try:
        resp = httpx.get(url, headers=_auth_headers(token), timeout=30.0)
    except httpx.HTTPError as e:
        logger.info("GitHub compare request failed (%s): %s", url, e)
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    return {"ahead_by": data.get("ahead_by"), "behind_by": data.get("behind_by")}


def fetch_manifest_deps(
    fork_name_with_owner: str,
    branch: str,
    *,
    token: str | None,
) -> list[str] | None:
    """Declared-dependency lines from the fork's first present manifest, or None.

    Tries :data:`MANIFEST_PATHS` in order via the raw endpoint; returns the file's
    non-empty lines (the forknet models classify the ported-signal from them). None
    means no manifest was reachable (→ UNKNOWN signal, not a failure).
    """
    owner, _, name = fork_name_with_owner.partition("/")
    headers = _auth_headers(token)
    for path in MANIFEST_PATHS:
        url = f"{GITHUB_RAW_URL}/{owner}/{name}/{branch}/{path}"
        try:
            resp = httpx.get(url, headers=headers, timeout=30.0)
        except httpx.HTTPError as e:
            logger.info("GitHub raw request failed (%s): %s", url, e)
            continue
        if resp.status_code != 200:
            continue
        return [line.strip() for line in resp.text.splitlines() if line.strip()]
    return None
