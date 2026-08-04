"""Fork-network triage orchestration.

Pure orchestration over :mod:`pymolt.adapters.github_forks` (injected as
``adapter`` — the real module by default, a fake in tests): compute the
recency cutoff, fetch candidate forks, run the expensive per-fork passes
(divergence + manifest) on the top-N survivors only, classify/rank with
:mod:`pymolt.strategic.forknet.models`, and cache the resulting report.
Offline by default — the cache is the only source of truth unless
``online=True``. Core-stays-non-interactive: no prompts, just data.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pymolt.strategic.forknet import cache
from pymolt.strategic.forknet.models import (
    ForkCandidate,
    ForkNetworkReport,
    classify_ported_signal,
    rank_candidates,
)

CACHE_TTL = 24 * 3600  # 1 day
CACHE_NAMESPACE = "report"

# Mirrors the adapter's GraphQL page size / default max_pages (see
# pymolt.adapters.github_forks.fetch_recent_forks) — used only to guess
# whether paging likely hit the cap, for the truncation caveat.
_GRAPHQL_PAGE_SIZE = 100
_DEFAULT_MAX_PAGES = 50


def _cutoff_iso(cutoff_months: int) -> str:
    now = datetime.now(UTC)
    cutoff = now - timedelta(days=cutoff_months * 30)
    return cutoff.isoformat().replace("+00:00", "Z")


def _cache_key(base_repo: str, cutoff_months: int, top_n: int) -> str:
    return f"{base_repo}@{cutoff_months}m@top{top_n}"


def run_forknet(
    base_repo: str,
    *,
    cache_root: Path,
    online: bool = False,
    cutoff_months: int = 18,
    top_n: int = 25,
    adapter: Any = None,
) -> ForkNetworkReport:
    """Rank the live/ported successor forks of ``base_repo`` ("owner/name").

    Offline by default: returns the cached report from a previous
    ``online=True`` run, or an empty report noting the miss. With
    ``online=True``, fetches fresh via ``adapter`` (defaults to the real
    :mod:`pymolt.adapters.github_forks`) and refreshes the cache.
    """
    if adapter is None:
        from pymolt.adapters import github_forks as adapter

    key = _cache_key(base_repo, cutoff_months, top_n)
    token = adapter.resolve_token()
    auth = adapter.auth_mode(token)

    if not online:
        cached = cache.read(cache_root, CACHE_NAMESPACE, key, CACHE_TTL)
        if cached is not None:
            return ForkNetworkReport.model_validate(cached)
        return ForkNetworkReport(
            base_repo=base_repo,
            generated_at=datetime.now(UTC),
            cutoff_months=cutoff_months,
            forks_considered=0,
            auth_mode=auth,
            notes=["offline: no cached fork-network result; re-run with --online"],
        )

    cutoff_iso = _cutoff_iso(cutoff_months)
    raw_forks = adapter.fetch_recent_forks(base_repo, cutoff_iso=cutoff_iso, token=token)
    forks_considered = len(raw_forks)

    candidates = [
        ForkCandidate(
            name_with_owner=f.name_with_owner,
            url=f.url,
            pushed_at=f.pushed_at,
            stars=f.stars,
            default_branch=f.default_branch,
        )
        for f in raw_forks
    ]

    # Expensive per-fork passes (compare + manifest fetch) only for the top-N
    # by stars/recency — everyone else still appears in the report, just
    # uncompared with an UNKNOWN ported signal.
    survivors = sorted(candidates, key=lambda c: (c.stars, c.pushed_at), reverse=True)[:top_n]

    # Resolve the base repo's default branch once — it's the correct left side of
    # every fork compare (a fork may have renamed its own default branch).
    base_branch = adapter.fetch_default_branch(base_repo, token=token) or "master"

    compared = 0
    for c in survivors:
        result = adapter.compare_fork(
            base_repo, base_branch, c.name_with_owner, c.default_branch, token=token
        )
        if result is not None:
            c.ahead_by = result.get("ahead_by")
            c.behind_by = result.get("behind_by")
            compared += 1

        deps = adapter.fetch_manifest_deps(c.name_with_owner, c.default_branch, token=token)
        c.ported_signal, c.matched_deps = classify_ported_signal(deps)

    ranked = rank_candidates(candidates)

    notes: list[str] = []
    if forks_considered >= _GRAPHQL_PAGE_SIZE * _DEFAULT_MAX_PAGES:
        notes.append("truncated: hit the page limit; some older forks may be missing")
    if auth == "anonymous":
        # GitHub's GraphQL API (the fork listing) requires auth, so anonymous
        # runs get no forks at all — not merely a rate-limited subset. Be honest.
        notes.append(
            "anonymous: GitHub's fork listing (GraphQL) requires auth — set "
            "GITHUB_TOKEN/GH_TOKEN and re-run --online for any results"
        )

    report = ForkNetworkReport(
        base_repo=base_repo,
        generated_at=datetime.now(UTC),
        cutoff_months=cutoff_months,
        forks_considered=forks_considered,
        compared=compared,
        auth_mode=auth,
        candidates=ranked,
        notes=notes,
    )
    cache.write(cache_root, CACHE_NAMESPACE, key, report.model_dump(mode="json"))
    return report
